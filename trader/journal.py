"""Trade journal (SQLite): every decision, order, position and equity snapshot is recorded
so performance can be audited later."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, action TEXT, confidence REAL, source TEXT,
  combined_score REAL, reasoning TEXT, risk_approved INTEGER, risk_reason TEXT, reports TEXT, rules_action TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, side TEXT, ok INTEGER, order_id TEXT, qty REAL,
  avg_price REAL, quote REAL, fee REAL, reason TEXT, error TEXT, market_price REAL);
CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY, qty REAL, entry_price REAL, cost REAL, stop_loss REAL, take_profit REAL, opened_at TEXT,
  protected TEXT DEFAULT 'none', protection TEXT, high_water REAL);
CREATE TABLE IF NOT EXISTS closed_trades (
  id INTEGER PRIMARY KEY, symbol TEXT, opened_at TEXT, closed_at TEXT, entry_price REAL, exit_price REAL,
  qty REAL, cost REAL, proceeds REAL, pnl REAL, pnl_pct REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, text TEXT, data TEXT);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str, starting_balance: float):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # One connection may be used from the trading loop and from a /lab or /review thread; the desk serialises
        # them with its trade lock, and the Telegram thread opens its own connections (with_journal).
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        if self.get("cash") is None:
            self.set("cash", starting_balance)
            self.set("starting_balance", starting_balance)

    def _migrate(self) -> None:
        """Add columns introduced after the first release to older journal files."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(orders)")}
        if "market_price" not in cols:
            self.db.execute("ALTER TABLE orders ADD COLUMN market_price REAL")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(decisions)")}
        if "rules_action" not in cols:
            self.db.execute("ALTER TABLE decisions ADD COLUMN rules_action TEXT")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(positions)")}
        if "protected" not in cols:  # exchange-side stop-loss / take-profit orders
            self.db.execute("ALTER TABLE positions ADD COLUMN protected TEXT DEFAULT 'none'")
            self.db.execute("ALTER TABLE positions ADD COLUMN protection TEXT")
        if "high_water" not in cols:  # highest price since entry, for trailing stops
            self.db.execute("ALTER TABLE positions ADD COLUMN high_water REAL")
        self.db.commit()

    # ---- key/value state ----
    def get(self, key: str):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    @property
    def cash(self) -> float:
        return float(self.get("cash"))

    # ---- positions ----
    def positions(self) -> dict[str, dict]:
        out = {}
        for r in self.db.execute("SELECT * FROM positions"):
            p = dict(r)
            p["protected"] = p.get("protected") or "none"
            p["protection"] = json.loads(p["protection"]) if p.get("protection") else None
            p["high_water"] = p.get("high_water") or p["entry_price"]
            out[p["symbol"]] = p
        return out

    def update_stop(self, symbol: str, stop: float, high_water: float) -> None:
        self.db.execute("UPDATE positions SET stop_loss=?, high_water=? WHERE symbol=?", (stop, high_water, symbol))
        self.db.commit()

    def set_high_water(self, symbol: str, high_water: float) -> None:
        self.db.execute("UPDATE positions SET high_water=? WHERE symbol=?", (high_water, symbol))
        self.db.commit()

    def last_stop_loss_ms(self, symbol: str) -> int | None:
        row = self.db.execute("SELECT closed_at FROM closed_trades WHERE symbol=? AND exit_reason LIKE 'stop-loss%' "
                              "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return int(dt.datetime.fromisoformat(row[0]).timestamp() * 1000) if row else None

    def open_position(self, symbol: str, fill: dict, stop: float, take: float) -> None:
        self.db.execute("INSERT INTO positions (symbol, qty, entry_price, cost, stop_loss, take_profit, opened_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (symbol, fill["qty"], fill["avg_price"], fill["quote"], stop, take, now()))
        self.set("cash", self.cash - fill["quote"])

    def set_protection(self, symbol: str, protection: dict | None, state: str) -> None:
        """state: exchange (OCO live on Binance) | emulated (paper) | partial (one leg part-filled) | none."""
        self.db.execute("UPDATE positions SET protected=?, protection=? WHERE symbol=?",
                        (state, json.dumps(protection) if protection else None, symbol))
        self.db.commit()

    def close_position(self, symbol: str, fill: dict, reason: str) -> dict:
        """Record a sale. A partial sale (thin testnet order book) keeps the rest of the position open."""
        p = self.positions()[symbol]
        sold = min(1.0, fill["qty"] / p["qty"]) if p["qty"] else 1.0
        cost = p["cost"] * sold
        pnl = fill["quote"] - cost
        trade = {"symbol": symbol, "opened_at": p["opened_at"], "closed_at": now(), "entry_price": p["entry_price"],
                 "exit_price": fill["avg_price"], "qty": fill["qty"], "cost": cost, "proceeds": fill["quote"],
                 "pnl": pnl, "pnl_pct": pnl / cost * 100, "exit_reason": reason}
        self.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost,"
                        " proceeds, pnl, pnl_pct, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(trade.values()))
        if sold >= 0.999:  # leftover dust from rounding to the exchange's step size is ignored
            self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        else:
            self.db.execute("UPDATE positions SET qty = qty - ?, cost = cost - ? WHERE symbol=?",
                            (fill["qty"], cost, symbol))
        self.set("cash", self.cash + fill["quote"])
        return trade

    # ---- logs ----
    def log_decision(self, symbol: str, d: dict, risk: dict, reports: dict) -> None:
        self.db.execute(
            "INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, risk_approved,"
            " risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now(), symbol, d["action"], d["confidence"], d.get("source"), d.get("combined_score"), d["reasoning"],
             int(risk["approved"]), risk["reason"], json.dumps(reports, default=str), d.get("rules_action")))
        self.db.commit()

    def log_order(self, symbol: str, side: str, result: dict, reason: str) -> None:
        self.db.execute(
            "INSERT INTO orders (ts, symbol, side, ok, order_id, qty, avg_price, quote, fee, reason, error,"
            " market_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), symbol, side, int(result["ok"]), result.get("order_id"), result.get("qty"),
             result.get("avg_price"), result.get("quote"), result.get("fee_quote"), reason, result.get("error"),
             result.get("market_price")))
        self.db.commit()

    # ---- diary events: the notable moments, in order, for the notebook (data/notes/DIARY.md) ----
    def event(self, kind: str, text: str, data: dict | None = None) -> None:
        self.db.execute("INSERT INTO events (ts, kind, text, data) VALUES (?,?,?,?)",
                        (now(), kind, text, json.dumps(data, default=str) if data else None))
        self.db.commit()

    def events(self, since: str | None = None, limit: int = 3000) -> list[dict]:
        """Oldest first. `since` is an ISO timestamp."""
        if since:
            rows = self.db.execute("SELECT * FROM events WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, limit))
        else:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        out = [dict(r) for r in rows][::-1]
        for e in out:
            e["data"] = json.loads(e["data"]) if e.get("data") else None
        return out

    def log_equity(self, equity: float) -> None:
        self.db.execute("INSERT INTO equity VALUES (?,?,?)", (now(), equity, self.cash))
        self.db.commit()

    def day_start_equity(self, equity: float) -> float:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()  # same clock as the daily report
        rec = self.get("day_start")
        if not rec or rec["date"] != today:
            rec = {"date": today, "equity": equity}
            self.set("day_start", rec)
        return rec["equity"]
