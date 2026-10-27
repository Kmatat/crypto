"""The desk's notebook: what it does, decides and learns, as Markdown anyone (or Claude) can read.

The SQLite journal is the record the code trusts. The notebook renders the same facts for people and
for AI review. It is rebuilt from the journal after every market check, so it can never disagree with
it, and it can be rebuilt from any journal backup (`python -m trader notes`). Nothing in it is written
by hand, and nothing in it can change how the desk trades.

  data/notes/README.md           what is here, how to use it with Claude, the fixed guardrails
  data/notes/STRATEGY.md         the champion, its exam, every challenger with the reasons, the lineage
  data/notes/strategies/<id>.md  one file per strategy card: parameters, exam, forward test, live record,
                                 and the exact command that replicates its backtest
  data/notes/TRADES.md           every trade with the decision, the agents' reports and the checklist at entry
  data/notes/DIARY.md            the notable moments in order: starts, trades, brakes, scout, lab, promotions
  data/notes/PERFORMANCE.md      results, goals, the week-by-week learning curve, the Claude scorecard
  data/notes/weekly/<week>.md    one snapshot a week, written once: what was known and decided at the time

With the daily trend rule (STRATEGY=trend, trader/trend.py) STRATEGY.md describes the rule, its settings and each
coin's latest daily decision instead; the hourly strategy's cards are kept as they were, for STRATEGY=hourly.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import zipfile
from pathlib import Path
from types import SimpleNamespace

from . import trend
from .ai_router import effective_order
from .config import Settings
from .journal import Journal
from .report import goals, goals_text, performance, report_text
from .strategy import BOUNDS, MAX_CHANGES, StrategyCard, summary_line, thresholds_line

log = logging.getLogger(__name__)

STAMP = "%Y-%m-%d %H:%M"
ICONS = {"start": "🚀", "trade": "💱", "order": "❌", "brake": "🛑", "watchlist": "🔭", "protection": "🛡️",
         "pace": "⏱️", "lab": "🔬", "strategy": "🏆", "challenger": "🧪", "rollback": "⏪", "error": "⚠️",
         "update": "🔄", "review": "🧠", "owner": "👤", "lesson": "📝"}
PARAM_LABELS = [
    ("buy_threshold", "Buy when the weighted score is at least"),
    ("stop_loss_atr", "Stop-loss distance (ATR)"),
    ("take_profit_atr", "Take-profit distance (ATR)"),
    ("trail_atr", "Trailing stop distance (ATR, 0 = off)"),
    ("checks", "Master traders' checklist"),
    ("cooldown_hours", "Cooling-off after a stop-loss (hours)"),
    ("min_reward_risk", "Minimum reward-to-risk"),
    ("risk_per_trade_pct", "Risk per trade (share of equity)"),
    ("max_position_pct", "Position cap (share of equity)"),
    ("max_open_positions", "Positions at once"),
    ("interval", "Candle size"),
    ("weights", "Agent weights"),
    ("coin_thresholds", "Per-coin buy thresholds (from the threshold study)"),
]
STATUS_WORDS = {"proposed": "proposed, not yet examined", "lab_passed": "passed the exam, in forward test",
                "lab_failed": "failed the exam", "ready": "passed exam and forward test: awaiting the owner",
                "forward_failed": "failed the forward test", "promoted": "promoted to champion",
                "rejected": "rejected"}
TREND_LABEL = "daily trend rule"
ADOPTED = "{}: the position opened by the hourly strategy now follows the trend rule"   # the orchestrator's diary line
SWITCHED_BACK = "The owner switched the desk back to the hourly agents"


# ---------------------------------------------------------------- small helpers
def _ts(iso, fmt: str = STAMP) -> str:
    if not iso:
        return ""
    try:
        return dt.datetime.fromisoformat(str(iso)).strftime(fmt)
    except ValueError:
        return str(iso)[:16].replace("T", " ")


def _hours(a: str, b: str) -> float:
    try:
        return (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() / 3600
    except (ValueError, TypeError):
        return 0.0


def _px(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:.6g}"


def _pct(v, signed: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%" if signed else f"{v:.2f}%"


def _esc(s) -> str:
    return str("" if s is None else s).replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_none yet_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _week(iso: str) -> str:
    return dt.datetime.fromisoformat(iso).strftime("%G-W%V")


def _loads(text):
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


def _in_era(era: tuple[str, str | None] | None, ts: str) -> bool:
    """Is the ISO time `ts` inside the (start, end-or-None) period?"""
    return era is not None and era[0] <= ts and (era[1] is None or ts < era[1])


# ---------------------------------------------------------------- strategy helpers
def champion_at(history: list[dict], ts: str) -> dict | None:
    """The promotion record in force at `ts` (the last promotion at or before it)."""
    current = None
    for h in history:
        if h.get("event") == "promote" and h.get("ts", "") <= ts:
            current = h
    return current


def card_periods(history: list[dict], card_id: str) -> list[tuple[str, str | None]]:
    """When was this card the champion? [(from, until-or-None), ...]"""
    promotions = [h for h in history if h.get("event") == "promote"]
    out = []
    for i, h in enumerate(promotions):
        if h.get("id") == card_id:
            until = promotions[i + 1]["ts"] if i + 1 < len(promotions) else None
            out.append((h["ts"], until))
    return out


def replicate_command(card: StrategyCard, s: Settings, days: int = 180) -> str:
    coins = ",".join(x.removesuffix("USDT") for x in s.symbols)
    return (f"python -m trader backtest --days {days} --symbols {coins} --balance {s.starting_balance:g} "
            f"--interval {card.interval} --buy {card.buy_threshold:g} --sell {-card.buy_threshold:g} "
            f"--stop {card.stop_loss_atr:g} --take {card.take_profit_atr:g} --trail {card.trail_atr:g} "
            f"--checks {','.join(card.checks) or 'none'} --rr {card.min_reward_risk:g} "
            f"--cooldown {card.cooldown_hours:g} --risk {card.risk_per_trade_pct * 100:g} "
            f"--position {card.max_position_pct:g} --max-positions {card.max_open_positions}"
            + (" --coin-buy " + ",".join(f"{k.removesuffix('USDT')}={v:g}" for k, v in sorted(card.coin_thresholds.items()))
               if card.coin_thresholds else ""))


def params_table(card: StrategyCard) -> str:
    rows = []
    for key, label in PARAM_LABELS:
        v = getattr(card, key)
        if key == "checks":
            shown = ", ".join(v) or "none"
        elif key == "weights":
            shown = ", ".join(f"{k} {w:g}" for k, w in v.items())
        elif key == "coin_thresholds":
            shown = thresholds_line(card) if v else "none: every coin uses the common threshold"
        elif key in ("risk_per_trade_pct", "max_position_pct"):
            shown = f"{v * 100:g}%"
        else:
            shown = f"{v}"
        b = BOUNDS.get(key)
        rng = ("one of " + ", ".join(b)) if key == "interval" else (f"{b[0]:g} to {b[1]:g}" if b else "fixed")
        if key in ("risk_per_trade_pct", "max_position_pct") and b:
            rng = f"{b[0] * 100:g}% to {b[1] * 100:g}%"
        rows.append([label, shown, rng])
    return _table(["Parameter", "Value", "Allowed range (laboratory bounds)"], rows)


def exam_table(ev: dict | None) -> str:
    if not ev or not ev.get("tests"):
        return "_not examined yet_"
    rows = []
    for t in ev["tests"]:
        when = f"{t['days']} days ending " + ("now" if not t.get("ending_days_ago") else f"{t['ending_days_ago']} days ago")
        rows.append([t["coins"], when, _pct(t["return_pct"]), t["trades"], t.get("profit_factor") or "n/a",
                     _pct(t.get("max_drawdown_pct"), signed=False),
                     _pct(t.get("basket_hold_pct")) if t.get("basket_hold_pct") is not None else "n/a"])
    table = _table(["Coins", "Period", "Return", "Trades", "Profit factor", "Worst drop", "Buy & hold"], rows)
    return (table + f"\n\nMean {_pct(ev['mean_return'])} · worst {_pct(ev['worst_return'])} · {ev['total_trades']} trades"
            f" · average profit factor {ev.get('mean_profit_factor') or 'n/a'} · examined {_ts(ev.get('evaluated_at'))} UTC")


def live_record(trades: list[dict], periods: list[tuple[str, str | None]]) -> dict:
    mine = [t for t in trades if any(a <= t["opened_at"] and (b is None or t["opened_at"] < b) for a, b in periods)]
    wins = [t for t in mine if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in mine if t["pnl"] <= 0)
    return {"trades": len(mine), "wins": len(wins), "pnl": round(sum(t["pnl"] for t in mine), 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (None if not wins else float("inf")),
            "win_rate_pct": round(len(wins) / len(mine) * 100, 1) if mine else None}


def live_record_text(rec: dict) -> str:
    if not rec["trades"]:
        return "no closed trades under this strategy yet"
    pf = "∞" if rec["profit_factor"] == float("inf") else rec["profit_factor"]
    return (f"{rec['trades']} closed trades · {rec['wins']} winners ({rec['win_rate_pct']}%) · "
            f"P&L {rec['pnl']:+,.2f} USDT · profit factor {pf}")


def lineage_text(j: Journal, limit: int = 15) -> str:
    """Compact history of what was tried and how it ended, for the research agent and for reviews."""
    state = j.get("strategy_champion") or {}
    lines = []
    if state:
        champ = StrategyCard.from_dict(state["card"])
        lines.append(f"Champion: {summary_line(champ)} (since {_ts(state.get('since'))}, {state.get('by')})")
    items = j.get("strategy_challengers") or []
    if items:
        lines.append("Already tried (do not propose these again; build on what passed):")
        champ_card = StrategyCard.from_dict(state["card"]) if state else None
        for c in items[-limit:]:
            card = StrategyCard.from_dict(c["card"])
            bits = [f"[{card.id}] {card.name} ({card.source or 'proposal'}): {STATUS_WORDS.get(c['status'], c['status'])}"]
            if champ_card is not None:
                bits.append(f"changes: {card.describe_diff(champ_card)}")
            if c.get("lab"):
                v = c["lab"].get("verdict", {})
                bits.append(f"exam mean {_pct(c['lab']['mean_return'])}, beat champion {v.get('wins')}/{v.get('tests')}")
            if c.get("forward") and c["forward"].get("ready"):
                bits.append(f"forward {_pct(c['forward']['challenger_return'])} vs champion "
                            f"{_pct(c['forward']['champion_return'])}")
            if c.get("notes"):
                bits.append("why: " + "; ".join(str(n) for n in c["notes"][:2]))
            lines.append("- " + " · ".join(bits))
    hist = j.get("strategy_history") or []
    if hist:
        lines.append("Promotions and rollbacks: " + "; ".join(
            f"{_ts(h['ts'], '%Y-%m-%d')} {h['event']} {h.get('name')} [{h.get('id')}] by {h.get('by')}"
            + (f" ({h['note']})" if h.get("note") else "") for h in hist[-8:]))
    lessons = j.get("lessons") or {}
    if lessons.get("notes"):
        lines.append("Lessons from live trading: " + "; ".join(lessons["notes"]))
    return "\n".join(lines)


def default_dir(s: Settings, journal_path: str) -> str | None:
    """data/notes next to data/journal.db (data/notes_offline next to the offline journal); None for :memory:."""
    if s.notes_dir:
        return s.notes_dir
    if journal_path == ":memory:":
        return None
    p = Path(journal_path)
    return str(p.with_name("notes" + p.stem.removeprefix("journal")))


# ---------------------------------------------------------------- the notebook
class Notes:
    def __init__(self, journal: Journal, directory: str | Path | None, settings: Settings, version_fn=None):
        self.j = journal
        self.dir = Path(directory) if directory else None
        self.s = settings
        self._version_fn = version_fn
        self._version: str | None = None

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    def version(self) -> str:
        if self._version is None:
            try:
                if self._version_fn is None:
                    from .updater import version_string
                    self._version_fn = version_string
                self._version = self._version_fn()
            except Exception:  # noqa: BLE001 - not a git checkout, git missing: the notebook still works
                from . import __version__
                self._version = __version__
        return self._version

    # ---- writing ----
    def _write(self, rel: str, text: str) -> Path:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def refresh(self, prices: dict[str, float] | None = None, scorecard: str = "") -> list[Path] | None:
        """Rebuild every file from the journal. Never raises: a notebook problem must not stop trading."""
        if not self.enabled:
            return None
        try:
            prices = prices or {}
            hist = self.j.get("strategy_history") or []
            trades = [dict(r) for r in self.j.db.execute("SELECT * FROM closed_trades ORDER BY id")]
            written = [self._write("README.md", self.readme_md()),
                       self._write("STRATEGY.md", self.strategy_md(trades, hist)),
                       self._write("TRADES.md", self.trades_md(trades, hist, prices)),
                       self._write("DIARY.md", self.diary_md()),
                       self._write("PERFORMANCE.md", self.performance_md(trades, hist, prices, scorecard))]
            for card, entry in self.cards():
                written.append(self._write(f"strategies/{card.id}.md", self.card_md(card, entry, trades, hist)))
            return written
        except Exception as e:  # noqa: BLE001 - deliberately broad: the notebook is a by-product
            log.warning("Notebook refresh failed: %s", e)
            return None

    def weekly_snapshot(self, week: str, sections: list[tuple[str, str]]) -> Path | None:
        """Written once per week and never rewritten: the record of what was known and decided then."""
        if not self.enabled:
            return None
        path = self.dir / "weekly" / f"{week}.md"
        if path.exists():
            return path
        body = [f"# Week {week}", f"_Snapshot written {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. "
                f"Running version: {self.version()}._", ""]
        for title, text in sections:
            if text:
                body += [f"## {title}", "", text.strip(), ""]
        try:
            return self._write(f"weekly/{week}.md", "\n".join(body))
        except OSError as e:
            log.warning("Weekly snapshot failed: %s", e)
            return None

    def bundle(self) -> Path | None:
        """Zip the notebook (for Telegram /notes). The caller deletes the file after sending."""
        if not self.enabled or not self.dir.exists():
            return None
        target = self.dir.parent / f"notes-{dt.datetime.now(dt.timezone.utc):%Y-%m-%d}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(self.dir.rglob("*.md")):
                z.write(p, p.relative_to(self.dir.parent).as_posix())
        return target

    # ---- data ----
    def cards(self) -> list[tuple[StrategyCard, dict | None]]:
        """Every card the desk knows: the champion (entry None) and all challengers."""
        out = []
        state = self.j.get("strategy_champion")
        if state:
            out.append((StrategyCard.from_dict(state["card"]), None))
        seen = {c.id for c, _ in out}
        for e in self.j.get("strategy_challengers") or []:
            card = StrategyCard.from_dict(e["card"])
            if card.id not in seen:
                seen.add(card.id)
                out.append((card, e))
        return out

    def entry_decision(self, symbol: str, opened_at: str) -> dict | None:
        r = self.j.db.execute("SELECT * FROM decisions WHERE symbol=? AND action='BUY' AND risk_approved=1 AND ts<=? "
                              "ORDER BY id DESC LIMIT 1", (symbol, opened_at)).fetchone()
        if not r or _hours(r["ts"], opened_at) > 1:
            return None
        return dict(r)

    def exit_decision(self, symbol: str, closed_at: str) -> dict | None:
        r = self.j.db.execute("SELECT * FROM decisions WHERE symbol=? AND action='SELL' AND risk_approved=1 AND ts<=? "
                              "ORDER BY id DESC LIMIT 1", (symbol, closed_at)).fetchone()
        if not r or _hours(r["ts"], closed_at) > 1:
            return None
        return dict(r)

    # ---- the daily trend strategy (STRATEGY=trend) ----
    def trend_since(self) -> str:
        """The day the daily trend rule's record began (its first decisions), for the owner's texts."""
        start = (self.j.get("trend_start") or {}).get("ts")
        return start[:10] if start else "its first check"

    def trend_era(self) -> tuple[str, str | None] | None:
        """(start, end or None) of the daily trend rule's latest record: from its first decisions until the owner
        switched back to the hourly strategy. None before the rule has decided anything."""
        start = (self.j.get("trend_start") or {}).get("ts")
        if not start:
            return None
        end = self.j.db.execute("SELECT ts FROM events WHERE kind='strategy' AND text LIKE ? AND ts >= ? ORDER BY id "
                                "LIMIT 1", (SWITCHED_BACK + "%", start)).fetchone()
        return start, end[0] if end else None

    def _taken_over(self, symbol: str, opened_at: str, closed_at: str | None = None) -> str | None:
        """When the daily trend rule took over a position the hourly strategy had opened, or None. An open position
        counts only while the desk trades the rule and the rule still manages it (trend_positions)."""
        start = (self.j.get("trend_start") or {}).get("ts")
        if not start or opened_at >= start:   # never traded the rule, or opened under it: nothing was taken over
            return None
        if closed_at is None and not (self.s.trend and (self.j.get("trend_positions") or {}).get(symbol) == opened_at):
            return None
        row = self.j.db.execute("SELECT ts FROM events WHERE kind='strategy' AND text LIKE ? AND ts >= ? AND ts <= ? "
                                "ORDER BY id LIMIT 1", (ADOPTED.format(symbol) + "%", opened_at, closed_at or "9999")).fetchone()
        return row[0] if row else (None if closed_at else start)

    def _strategy_label(self, symbol: str, opened_at: str, hist: list[dict], entry: dict | None,
                        closed_at: str | None = None) -> str:
        """Which strategy a position or trade belongs to: the daily trend rule's own buys (`entry` is the decision
        logged for the buy), a position of the hourly strategy the rule took over, or the hourly champion in force."""
        if (entry and entry.get("source") == "trend") or (entry is None and _in_era(self.trend_era(), opened_at)):
            return TREND_LABEL
        who = champion_at(hist, opened_at)
        label = f"{who.get('name')} [{who.get('id')}]" if who else "owner settings"
        if since := self._taken_over(symbol, opened_at, closed_at):
            return f"{label}, managed by the daily trend rule since {since[:10]}"
        return label

    # ---- README ----
    def readme_md(self) -> str:
        s = self.s
        now = dt.datetime.now(dt.timezone.utc).strftime(STAMP)
        mode = {"testnet": "Binance demo exchange (testnet, fake money)", "simulation": "paper trading at real prices"}
        ai_cap = (f"${s.ai_daily_budget_usd:.2f} a day"
                  + (f" and at most {s.ai_pct_month():g}% of the account a month" if s.ai_pct_month()
                     else " (no percentage cap on demo money; the owner's /aibudget changes it)"))
        account = [["Trading mode", mode.get(s.mode, s.mode) + (" - offline test" if s.offline else "")],
                   ["Starting balance", f"${s.starting_balance:,.0f}"]]
        updates = ["Code updates", "tested before they restart the desk; rolled back if they fail" +
                   ("" if s.auto_update else " (installed only when the owner sends /update)")]
        if s.trend:
            return self._trend_readme(now, account, ai_cap, updates)
        rows = [*account,
                ["Position cap", f"{s.max_position_pct:.0%} of equity, at most {s.max_open_positions} positions"],
                ["Daily loss brake", f"{s.max_daily_loss_pct:.0%}: no new buys for the rest of the day"],
                ["Overall loss brake", f"{s.max_drawdown_pct:.0%} below the start: pause {s.brake_pause_days} days, "
                                       f"then half size until half the loss is recovered"],
                ["AI budget", ai_cap + "; the free rules take over when it is spent"],
                ["Free AI", (f"{s.free_ai_provider} ({', '.join(s.free_ai_models) or 'its recommended models'})"
                             + (f", {effective_order(s, self.j)}" if s.use_claude else "")
                             + ": it costs nothing and is rationed by the provider's daily allowance"
                             + ("; web research, the lab's proposals and the notebook review stay with Claude"
                                if s.use_claude else "")) if s.use_free_ai else "not configured"],
                ["Strategy changes", f"at most {MAX_CHANGES} parameters at a time, inside the bounds, after the exam and "
                                     f"the forward test, " + ("promoted automatically" if s.auto_promote else
                                                             "and only with the owner's /approve")],
                updates]
        files = [["README.md", "this file"],
                 ["STRATEGY.md", "the champion strategy, its exam, every challenger and why it passed or failed, the lineage"],
                 ["strategies/<id>.md", "one file per strategy card, with the exact command that replicates its backtest"],
                 ["TRADES.md", "every trade: the decision, the agents' reports and the checklist at entry, the exit"],
                 ["DIARY.md", "the notable moments in order (starts, trades, brakes, coin list, lab, promotions, rollbacks)"],
                 ["PERFORMANCE.md", "results, demo goals, the week-by-week learning curve, the Claude scorecard"],
                 ["weekly/<week>.md", "one snapshot a week, never rewritten: what was known and decided at the time"]]
        return "\n".join([
            "# Trading desk notebook", "",
            f"_Generated {now} UTC by the desk itself from its trade journal, after every market check. "
            f"Nothing here is written by hand. Running version: {self.version()}._", "",
            "## How to use this with Claude", "",
            "Upload this folder (or the zip that `/notes` sends on Telegram) to Claude and ask, for example:", "",
            "- \"Read STRATEGY.md and TRADES.md. What is failing, and which single change inside the bounds would you "
            "test first? Why?\"",
            "- \"Compare the champion's exam with the challengers'. Is the forward test telling us anything yet?\"",
            "- \"In TRADES.md, find trades where the reasoning at entry did not match the outcome. What pattern do you see?\"",
            "- \"Read DIARY.md for the last two weeks. Did anything break, and did the desk recover correctly?\"", "",
            "Claude can propose; the laboratory tests; the owner approves on Telegram. The guardrails below are fixed "
            "in code and in the owner's settings; no strategy, no AI answer and no note in this folder can change them.", "",
            "## Files", "", _table(["File", "What it holds"], files), "",
            "## Fixed guardrails", "", _table(["Rule", "Setting"], rows), "",
            "## Laboratory bounds (what a self-made strategy may vary)", "",
            params_table(StrategyCard.from_settings(self.s)),
            "", "_The 'Value' column shows the owner's settings; a champion may differ within the allowed range._"])

    def _trend_readme(self, now: str, account: list[list], ai_cap: str, updates: list) -> str:
        """README for the daily trend rule: no laboratory, no champion; the rule is fixed in code."""
        s = self.s
        sma_days, vol_target, stop_pct = trend.params(s)
        rows = [*account,
                ["Strategy", "the daily trend rule (STRATEGY=trend): one decision a day, at the first check after 00:00 "
                             "UTC, on finished daily closes; no AI in the decisions"],
                ["Position cap", f"each coin gets an equal share of the account (1/{max(1, len(s.symbols))}), made smaller "
                                 f"for coins that swing more than {vol_target:.0%} a year, and never more than "
                                 f"{s.max_position_pct:.0%} of equity; at most {s.max_open_positions} positions"],
                ["Emergency stop", f"{stop_pct:.0%} below the entry, on the exchange; after it fires the coin is bought "
                                   "again only once its trend has reset"],
                ["Take-profit", f"an order at {trend.TAKE_MULT:g}x the price (Binance's protective pair needs one), raised "
                                f"whenever the price comes within {trend.RAISE_AT - 1:.0%} of it: no profit cap"],
                ["Daily loss brake", f"{s.max_daily_loss_pct:.0%}: no new buys for the rest of the day"],
                ["Overall loss brake", f"{s.max_drawdown_pct:.0%} below the start: no new buys for {s.brake_pause_days} "
                                       "days, then half size until half the loss is recovered; coins already held ride "
                                       "on with their emergency stop"],
                ["AI budget", ai_cap + "; no AI takes part in the trading decisions"],
                ["Free AI", f"{s.free_ai_provider}: not asked about trades under the daily trend rule"
                            if s.use_free_ai else "not configured"],
                ["Strategy changes", "none by the desk: the rule is fixed in code (trader/trend.py); only the owner's "
                                     "TREND_SMA_DAYS, TREND_VOL_TARGET and TREND_STOP_PCT (desk setup) change it"],
                updates]
        files = [["README.md", "this file"],
                 ["STRATEGY.md", "the daily trend rule, its settings, each coin's latest daily decision, its live record"],
                 ["strategies/<id>.md", "the hourly strategy's cards, kept unchanged for STRATEGY=hourly (not used now)"],
                 ["TRADES.md", "every trade: the decision at entry, the exit, which strategy it belonged to"],
                 ["DIARY.md", "the notable moments in order (starts, trades, brakes, take-profit raises, strategy switches)"],
                 ["PERFORMANCE.md", "results, the trend rule's goals (counted from its start), the week-by-week record"],
                 ["weekly/<week>.md", "the hourly strategy's weekly pages, never rewritten (the trend rule adds none)"]]
        return "\n".join([
            "# Trading desk notebook", "",
            f"_Generated {now} UTC by the desk itself from its trade journal, after every market check. "
            f"Nothing here is written by hand. Running version: {self.version()}._", "",
            f"The desk trades the daily trend rule (STRATEGY=trend) since {self.trend_since()}. There is no laboratory "
            "and no AI in the decisions: the rule is fixed, and it is expected to win only 20-30% of its trades, with a "
            "few long winners paying for many small losses.", "",
            "## How to use this with Claude", "",
            "Upload this folder (or the zip that `/notes` sends on Telegram) to Claude and ask, for example:", "",
            "- \"Read TRADES.md: are the losses small and the winners long? Compare the live record with /trendtest's "
            "replay of the same weeks.\"",
            "- \"Read STRATEGY.md and DIARY.md: did the desk follow its rule every day (one decision after 00:00 UTC, a "
            "sale after each close at or below the average, no buy straight back after an emergency stop)?\"",
            "- \"Read PERFORMANCE.md: judged on the profit factor, the worst drop and the result against just holding "
            "the same coins, is there enough evidence yet, and what does it say?\"",
            "- \"Read DIARY.md for the last two weeks. Did anything break, and did the desk recover correctly?\"", "",
            "Claude can comment; it cannot change anything. Only the owner changes the rule's settings (desk setup). The "
            "guardrails below are fixed in code and in the owner's settings; no AI answer and no note in this folder can "
            "change them.", "",
            "## Files", "", _table(["File", "What it holds"], files), "",
            "## Fixed guardrails", "", _table(["Rule", "Setting"], rows)])

    # ---- STRATEGY ----
    def strategy_md(self, trades: list[dict], hist: list[dict]) -> str:
        if self.s.trend:
            return self.trend_strategy_md(trades)
        state = self.j.get("strategy_champion")
        lines = ["# Strategy", ""]
        if not state:
            lines.append("_No strategy recorded yet: the first market check writes the owner's settings as the champion._")
            return "\n".join(lines)
        champ = StrategyCard.from_dict(state["card"])
        lines += [f"## Champion: {champ.name} [{champ.id}]", "",
                  f"Champion since {_ts(state.get('since'))} UTC ({state.get('by')}). Source: {champ.source or 'owner'}.",
                  "Rationale: " + (champ.rationale or "the owner's settings, chosen from the backtest evidence described in the "
                                                      "project README ('Measuring the strategy before trusting it')."),
                  "", params_table(champ), "",
                  "### Exam (how the champion scores on the laboratory's tests)", "",
                  exam_table(self.j.get("strategy_champion_eval")), "",
                  "### Live record under this champion", "",
                  live_record_text(live_record(trades, card_periods(hist, champ.id) or [(state.get("since", ""), None)])),
                  "", f"Replicate the backtest: `{replicate_command(champ, self.s)}`", ""]
        study = self.j.get("threshold_study")
        if study:
            lines += [f"### Threshold study ({_ts(study.get('studied_at'), '%Y-%m-%d')}, {study['study_days']} days per coin, "
                      f"the last {study['holdout_days']} days held out)", "",
                      _table(["Coin", "Chosen", "Best tested", "Trades at best", "Return at best", f"Return at common {study['base']:g}", "Why"],
                             [[sym.removesuffix("USDT"), f"{c['threshold']:g}", c.get("best") if c.get("best") is not None else "-",
                               c.get("trades"), _pct(c.get("return_pct")) if c.get("return_pct") is not None else "-",
                               _pct(c.get("base_return_pct")) if c.get("base_return_pct") is not None else "-", c["why"]]
                              for sym, c in study["coins"].items()]), ""]
        items = self.j.get("strategy_challengers") or []
        lines += ["## Challengers", ""]
        rows = []
        for c in items[::-1]:
            card = StrategyCard.from_dict(c["card"])
            lab = c.get("lab") or {}
            v = lab.get("verdict", {})
            exam = f"{_pct(lab['mean_return'])}, beat champion {v.get('wins')}/{v.get('tests')}" if lab else "not yet"
            fwd = c.get("forward") or {}
            forward = (f"{_pct(fwd['challenger_return'])} vs {_pct(fwd['champion_return'])} over {fwd['days']:.0f} days"
                       if fwd.get("ready") else (f"{fwd.get('days', 0):.0f} days in, needs more" if fwd else "not yet"))
            rows.append([f"[{card.id}](strategies/{card.id}.md)", card.name, card.source or "proposal",
                         _ts(c.get("proposed_at"), "%Y-%m-%d"), card.describe_diff(champ),
                         STATUS_WORDS.get(c["status"], c["status"]), exam, forward,
                         "; ".join(str(n) for n in (c.get("notes") or [])[:2])])
        lines += [_table(["Id", "Name", "Source", "Proposed", "Changes vs champion", "Status", "Exam", "Forward test",
                          "Why"], rows), ""]
        lines += ["## Lineage (every promotion and rollback)", "",
                  _table(["When (UTC)", "Event", "Strategy", "By", "Note"],
                         [[_ts(h["ts"]), h["event"], f"{h.get('name')} [{h.get('id')}]", h.get("by"), h.get("note", "")]
                          for h in hist[::-1]]), ""]
        lessons = self.j.get("lessons") or {}
        if lessons:
            lines += ["## Lessons (post-mortem of the last 30 days, written by the research agent)", ""]
            lines += [f"- {n}" for n in lessons.get("notes", [])] or ["- nothing notable"]
            lines += ["", f"Trades {lessons.get('trades')} · losers {lessons.get('losers')} · stopped out within two hours "
                          f"{lessons.get('noise_stops')} · average loss {lessons.get('avg_loss_pct')}% · average win "
                          f"{lessons.get('avg_win_pct')}% · exits by reason {lessons.get('exits')}", ""]
        last = self.j.get("lab_last_run")
        lines.append(f"_Last laboratory run: {_ts(last) + ' UTC' if last else 'never'}._")
        return "\n".join(lines)

    def trend_strategy_md(self, trades: list[dict]) -> str:
        """STRATEGY.md for the daily trend rule: the rule, its settings, and what it last decided for each coin."""
        from .health import trend_line
        s = self.s
        sma_days, vol_target, stop_pct = trend.params(s)
        coins = [c.removesuffix("USDT") for c in s.symbols]
        lines = [f"# Strategy: the daily trend rule (STRATEGY=trend), in force since {self.trend_since()}", "",
                 trend_line(SimpleNamespace(base=s), self.j), "",
                 "## The rule", "",
                 "- Once a day, at the first check after the daily close (00:00 UTC), the desk reads each coin's finished "
                 "daily closes. No AI takes part in any decision.",
                 f"- It holds a coin while its close is above its {sma_days}-day average, and sells it after a close at or "
                 "below the average.",
                 f"- A coin it buys gets an equal share of the account (1/{max(1, len(coins))}), made smaller when the "
                 f"coin swings more than {vol_target:.0%} a year, and never more than {s.max_position_pct:.0%} "
                 "(MAX_POSITION_PCT).",
                 f"- When more coins qualify than there are free positions, the strongest {trend.MOMENTUM_DAYS}-day gain "
                 "goes first.",
                 f"- An emergency stop {stop_pct:.0%} below the entry price sits on the exchange. After it fires, the coin "
                 "is bought again only once it has closed at or below its average and then risen above it again.",
                 "- There is no profit target: the rule expects to win only 20-30% of its trades, and a few long trends "
                 "pay for the many small losses. Binance's protective order pair still needs a take-profit price, so the "
                 f"desk places it at {trend.TAKE_MULT:g}x the price and raises it whenever the price comes within "
                 f"{trend.RAISE_AT - 1:.0%} of it.",
                 "- The rule is fixed in code (trader/trend.py). Only the owner changes its settings, with desk setup.", "",
                 "## Settings", "",
                 _table(["Setting", "In force", "As tested"], [
                     ["Average the close is compared with (TREND_SMA_DAYS)", f"{sma_days} days", f"{trend.TESTED[0]} days"],
                     ["Yearly swing above which a coin's share is cut (TREND_VOL_TARGET)", f"{vol_target:.0%}",
                      f"{trend.TESTED[1]:.0%}"],
                     ["Emergency stop below the entry (TREND_STOP_PCT)", f"{stop_pct:.0%}", f"{trend.TESTED[2]:.0%}"],
                     ["Positions at once (MAX_OPEN_POSITIONS)", f"{s.max_open_positions}",
                      f"one per coin ({len(trend.TESTED_COINS)})"],
                     ["Coins (SYMBOLS)", ", ".join(coins) or "none",
                      ", ".join(c.removesuffix("USDT") for c in trend.TESTED_COINS)],
                     ["Take-profit order", f"{trend.TAKE_MULT:g}x the price, raised within {trend.RAISE_AT - 1:.0%}",
                      f"{trend.TAKE_MULT:g}x the price, raised within {trend.RAISE_AT - 1:.0%}"]]), ""]
        if (s.trend_sma_days, s.trend_vol_target, s.trend_stop_pct) != (sma_days, vol_target, stop_pct):
            lines += [f"Your settings (TREND_SMA_DAYS {s.trend_sma_days}, TREND_VOL_TARGET {s.trend_vol_target:g}, "
                      f"TREND_STOP_PCT {s.trend_stop_pct:g}) are outside the allowed ranges (10-400 days, 0.05-2, "
                      "0.05-0.5), so the tested values are used.", ""]
        if s.max_open_positions < len(coins):
            lines += [f"Only {s.max_open_positions} positions at once for {len(coins)} coins: the rule holds only the "
                      f"strongest {s.max_open_positions}. It was tested with a position for every coin.", ""]
        rows = []
        for sym in s.symbols:
            r = self.j.db.execute("SELECT ts, action, risk_reason, reasoning FROM decisions WHERE symbol=? AND "
                                  "source='trend' ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
            if r is None:
                rows.append([sym.removesuffix("USDT"), "-", "no daily decision yet", "", ""])
                continue
            day = (dt.date.fromisoformat(r["ts"][:10]) - dt.timedelta(days=1)).isoformat()
            rows.append([sym.removesuffix("USDT"), day, r["action"], r["risk_reason"], r["reasoning"]])
        lines += ["## Each coin's latest daily decision", "",
                  _table(["Coin", "Day", "Decision", "Risk check", "Why"], rows), "",
                  "_Day: the finished UTC day the decision is based on; the desk decides at its first check after "
                  "00:00 UTC the next day._", ""]
        era = self.trend_era()
        lines += [f"## Live record (trades the rule opened since {self.trend_since()})", "",
                  live_record_text(live_record(trades, [era] if era else [])), "",
                  "Compare it with the replay: /trendtest on Telegram replays the same rule on past daily prices with "
                  "these settings.", ""]
        cards = self.cards()
        if cards:
            champ = next((c for c, e in cards if e is None), None)
            lines += ["## The hourly strategy's cards", "",
                      f"{len(cards)} card(s) of the hourly strategy"
                      + (f" (champion {champ.name} [{champ.id}])" if champ else "")
                      + " are kept unchanged in strategies/ for STRATEGY=hourly; they are not used while STRATEGY=trend.", ""]
        return "\n".join(lines)

    def card_md(self, card: StrategyCard, entry: dict | None, trades: list[dict], hist: list[dict]) -> str:
        state = self.j.get("strategy_champion") or {}
        champ = StrategyCard.from_dict(state["card"]) if state else None
        is_champ = champ is not None and champ.id == card.id
        status = "champion" if is_champ else STATUS_WORDS.get(entry["status"], entry["status"]) if entry else "known"
        if is_champ and self.s.trend:
            status = "champion of the hourly strategy (not in force while STRATEGY=trend)"
        lines = [f"# {card.name} [{card.id}]", "",
                 f"Status: **{status}**. Source: {card.source or 'owner'}. Created {_ts(card.created_at)} UTC"
                 + (f", proposed to the laboratory {_ts(entry.get('proposed_at'))} UTC." if entry else "."), ""]
        if card.rationale:
            lines += [f"Rationale: {card.rationale}", ""]
        if champ is not None and not is_champ:
            lines += [f"Changes versus the current champion [{champ.id}]: {card.describe_diff(champ)}", ""]
        lines += ["## Parameters", "", params_table(card), ""]
        ev = self.j.get("strategy_champion_eval") if is_champ else (entry or {}).get("lab")
        lines += ["## Exam", "", exam_table(ev)]
        if ev and ev.get("verdict"):
            v = ev["verdict"]
            lines += ["", f"Verdict: {'passed' if v['passed'] else 'failed'} - beat the champion on {v['wins']} of "
                          f"{v['tests']} tests, worst gap {v['worst_gap']} points"
                      + ("; " + "; ".join(v["reasons"]) if v.get("reasons") else "")]
        lines.append("")
        fwd = (entry or {}).get("forward")
        if fwd:
            lines += ["## Forward test (the weeks after the proposal)", ""]
            if fwd.get("ready"):
                lines.append(f"{'Passed' if fwd['passed'] else 'Failed'}: challenger {_pct(fwd['challenger_return'])} vs "
                             f"champion {_pct(fwd['champion_return'])} over {fwd['days']:.0f} days, "
                             f"{fwd.get('challenger_trades')} trades" + ("; " + "; ".join(fwd["reasons"]) if fwd.get("reasons") else ""))
            else:
                lines.append(fwd.get("reason", "waiting"))
            lines.append("")
        periods = card_periods(hist, card.id)
        if periods:
            lines += ["## Live record (trades opened while this card was champion)", "",
                      live_record_text(live_record(trades, periods)), "",
                      "Periods: " + "; ".join(f"{_ts(a)} to {_ts(b) if b else 'now'}" for a, b in periods), ""]
        lines += ["## Replicate", "", "Run this on any computer with the project to reproduce the backtest "
                  "(180 days on the core coins; add `--days 365` or other coins to vary):", "",
                  "```", replicate_command(card, self.s), "```", "",
                  "_Agent weights are not a backtest flag; they are the card's `weights` above, applied through "
                  "`trader/agents/decision.py`._"]
        return "\n".join(lines)

    # ---- TRADES ----
    def trades_md(self, trades: list[dict], hist: list[dict], prices: dict[str, float]) -> str:
        wins = [t for t in trades if t["pnl"] > 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
        fees = self.j.db.execute("SELECT COALESCE(SUM(fee), 0) FROM orders WHERE ok=1").fetchone()[0]
        failed = [dict(r) for r in self.j.db.execute("SELECT * FROM orders WHERE ok=0 ORDER BY id DESC LIMIT 50")]
        lines = ["# Trades", "",
                 f"{len(trades)} closed trades · {len(wins)} winners"
                 f" ({round(len(wins) / len(trades) * 100, 1) if trades else 'n/a'}%) · realized P&L "
                 f"{sum(t['pnl'] for t in trades):+,.2f} USDT · profit factor "
                 f"{round(gross_win / gross_loss, 2) if gross_loss else 'n/a'} · fees paid {fees:,.2f} USDT", ""]
        lines += ["## Open positions", ""]
        rows, ruled_any = [], False
        managed = (self.j.get("trend_positions") or {}) if self.s.trend else {}
        for sym, p in self.j.positions().items():
            px = prices.get(sym, p["entry_price"])
            ruled = managed.get(sym) == p["opened_at"]   # the rule's exits: an emergency stop, a take-profit it raises
            ruled_any |= ruled
            rows.append([sym, _ts(p["opened_at"]), _px(p["entry_price"]), _px(px), f"{px * p['qty'] - p['cost']:+,.2f}",
                         f"emergency stop {_px(p['stop_loss'])}" if ruled else _px(p["stop_loss"]),
                         f"take-profit order {_px(p['take_profit'])} (raised as the price rises; no target)" if ruled
                         else _px(p["take_profit"]), p.get("protected") or "none",
                         self._strategy_label(sym, p["opened_at"], hist, self.entry_decision(sym, p["opened_at"]))])
        lines += [_table(["Coin", "Opened (UTC)", "Entry", "Now", "P&L USDT", "Stop", "Take-profit" if ruled_any else "Target",
                          "Protection", "Strategy"], rows), ""]
        lines += ["## Closed trades (newest first)", ""]
        if not trades:
            lines.append("_none yet_")
        for t in trades[::-1]:
            lines += self._trade_block(t, hist)
        if failed:
            lines += ["## Failed orders (newest first)", "",
                      _table(["When (UTC)", "Coin", "Side", "Reason", "Error"],
                             [[_ts(o["ts"]), o["symbol"], o["side"], o.get("reason"), o.get("error")] for o in failed]), ""]
        return "\n".join(lines)

    def _trade_block(self, t: dict, hist: list[dict]) -> list[str]:
        coin = t["symbol"].removesuffix("USDT")
        d = self.entry_decision(t["symbol"], t["opened_at"])
        head = (f"### {_ts(t['closed_at'], '%Y-%m-%d')} {coin} {t['pnl_pct']:+.2f}% ({t['pnl']:+,.2f} USDT) · "
                f"{t['exit_reason']}")
        out = [head, "",
               f"- Opened {_ts(t['opened_at'])} at {_px(t['entry_price'])}, closed {_ts(t['closed_at'])} at "
               f"{_px(t['exit_price'])} after {_hours(t['opened_at'], t['closed_at']):.1f} h · quantity {t['qty']:.6g} · "
               f"cost {t['cost']:,.2f} USDT",
               f"- Strategy: {self._strategy_label(t['symbol'], t['opened_at'], hist, d, t['closed_at'])}"]
        if d and d.get("source") == "trend":   # the rule has no confidence and no agents' reports
            out.append(f"- Decision at entry: BUY (daily trend rule): {_esc(d.get('reasoning'))}")
        elif d:
            out.append(f"- Decision at entry: BUY, confidence {d['confidence']:.0%} ({d.get('source') or 'rules'}"
                       + (f", rules said {d['rules_action']}" if d.get("rules_action") and d["rules_action"] != "BUY" else "")
                       + f"): {_esc(d.get('reasoning'))}")
            rep = _loads(d.get("reports"))
            tech, pred, news, fx = rep.get("technical", {}), rep.get("prediction", {}), rep.get("news", {}), rep.get("forex", {})
            parts = []
            if tech:
                parts.append(f"technical {tech.get('score', 0):+.2f} (RSI {tech.get('rsi', 'n/a')}, ATR {tech.get('atr', 0):.4g})")
            if pred:
                parts.append(f"prediction {pred.get('score', 0):+.2f}")
            if news:
                parts.append(f"news {news.get('score', 0):+.2f} \"{_esc(news.get('summary', ''))[:120]}\"")
            if fx:
                parts.append(f"forex {fx.get('regime', 'n/a')}")
            if parts:
                out.append("- Agents at entry: " + " · ".join(parts))
            if rep.get("mentor"):
                out.append(f"- Checklist at entry: {_esc(rep['mentor'].get('checklist'))}")
            if rep.get("head"):
                out.append(f"- Buy threshold for this check: {rep['head'].get('threshold')} ({_esc(rep['head'].get('why'))})")
            if rep.get("watchlist", {}).get("why_watched"):
                out.append(f"- Why this coin was watched: {_esc(rep['watchlist']['why_watched'])[:160]}")
        else:
            out.append("- Decision at entry: not found in the journal (older record)")
        if t["exit_reason"].startswith(("signal", "trend:")):
            x = self.exit_decision(t["symbol"], t["closed_at"])
            if x and x.get("source") == "trend":
                out.append(f"- Decision at exit: SELL (daily trend rule): {_esc(x.get('reasoning'))}")
            elif x:
                out.append(f"- Decision at exit: SELL, confidence {x['confidence']:.0%} ({x.get('source') or 'rules'}): "
                           f"{_esc(x.get('reasoning'))}")
        out.append("")
        return out

    # ---- DIARY ----
    def diary_md(self) -> str:
        events = self.j.events()
        lines = ["# Diary", "", "_The notable moments, newest day first. Every market check also writes a decision for "
                 "every coin to the journal; those are summarised in TRADES.md and not repeated here._", ""]
        if not events:
            lines.append("_nothing recorded yet_")
            return "\n".join(lines)
        by_day: dict[str, list[dict]] = {}
        for e in events:
            by_day.setdefault(e["ts"][:10], []).append(e)
        for day in sorted(by_day, reverse=True):
            lines += [f"## {day}", ""]
            for e in by_day[day]:
                lines.append(f"- {e['ts'][11:16]} {ICONS.get(e['kind'], '•')} {e['kind']}: {e['text']}")
            lines.append("")
        return "\n".join(lines)

    # ---- PERFORMANCE ----
    def weekly_table(self, trades: list[dict], hist: list[dict]) -> str:
        eq_rows = self.j.db.execute("SELECT ts, equity FROM equity ORDER BY rowid").fetchall()
        if not eq_rows:
            return "_no weeks recorded yet_"
        start = float(self.j.get("starting_balance") or eq_rows[0]["equity"])
        weeks: dict[str, dict] = {}
        for r in eq_rows:
            w = _week(r["ts"])
            weeks.setdefault(w, {"equity": r["equity"], "last_ts": r["ts"], "trades": 0, "wins": 0, "pnl": 0.0})
            weeks[w].update(equity=r["equity"], last_ts=r["ts"])
        for t in trades:
            w = _week(t["closed_at"])
            cell = weeks.setdefault(w, {"equity": None, "last_ts": t["closed_at"], "trades": 0, "wins": 0, "pnl": 0.0})
            cell["trades"] += 1
            cell["wins"] += t["pnl"] > 0
            cell["pnl"] += t["pnl"]
        rows, trend_weeks, era = [], False, self.trend_era()
        gw = gl = 0.0
        for w in sorted(weeks):
            c = weeks[w]
            for t in trades:
                if _week(t["closed_at"]) == w:
                    gw += max(t["pnl"], 0)
                    gl += max(-t["pnl"], 0)
            who = champion_at(hist, c["last_ts"])
            ruled = _in_era(era, c["last_ts"])   # the week ended under the daily trend rule
            trend_weeks |= ruled
            rows.append([w, f"{c['equity']:,.2f}" if c["equity"] is not None else "n/a",
                         _pct((c["equity"] / start - 1) * 100) if c["equity"] else "n/a", c["trades"], c["wins"],
                         f"{c['pnl']:+,.2f}", f"{gw / gl:.2f}" if gl else ("n/a" if not gw else "∞"),
                         TREND_LABEL if ruled else f"{who.get('name')} [{who.get('id')}]" if who else "owner settings"])
        return _table(["Week", "Equity at end", "Return to date", "Trades closed", "Wins", "P&L in week",
                       "Profit factor to date", "Strategy" if trend_weeks else "Champion"], rows)

    def performance_md(self, trades: list[dict], hist: list[dict], prices: dict[str, float], scorecard: str) -> str:
        perf = performance(self.j, prices)
        g = goals(self.j, perf, prices, trend=getattr(self.s, "trend", False))
        lines = ["# Performance", "", f"_Figures as of {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. Open positions "
                 "are valued at the latest prices the desk saw._", "",
                 "## Now", "", "```", report_text(perf, title="Performance"), "", goals_text(g, perf), "```", ""]
        if scorecard and not self.s.trend:   # the stored scorecard judges the hourly agents; the trend rule asks no AI
            lines += ["## The AI versus the free rules (computed from real later prices)", "", "```", scorecard, "```", ""]
        lines += ["## Week by week (the learning curve)", "", self.weekly_table(trades, hist), ""]
        ai_total = float(self.j.get("ai_spend_total") or 0)
        lines += ["## Costs", "", f"- Exchange fees paid: {perf['fees_paid']:,.2f} USDT",
                  f"- Claude (AI) spend so far: ${ai_total:,.2f} ({g['ai_cost_pct']:.2f}% of the starting balance)",
                  f"- Decisions where an AI chose differently from the rules: {perf['claude_overrode_rules']}", ""]
        return "\n".join(lines)

    # ---- for Claude's /review ----
    def digest(self, prices: dict[str, float] | None = None, scorecard: str = "", trades_limit: int = 60,
               events_limit: int = 80, max_chars: int = 60_000) -> str:
        """Compact text of the notebook for an AI review: strategy, performance, recent trades and events."""
        prices = prices or {}
        hist = self.j.get("strategy_history") or []
        trades = [dict(r) for r in self.j.db.execute("SELECT * FROM closed_trades ORDER BY id")]
        parts = [self.strategy_md(trades, hist), self.performance_md(trades, hist, prices, scorecard)]
        if self.s.trend:   # the frame for the review: a fixed rule to audit, not a laboratory to steer
            parts.insert(0, f"The desk runs the fixed daily trend rule (STRATEGY=trend) since {self.trend_since()}: no "
                            "laboratory, no AI in the decisions; the rule is expected to win 20-30% of its trades with a "
                            "few large winners. Judge whether it follows its rule and how its live record compares with "
                            "its replay.")
        recent = ["# Recent trades (newest first)", ""]
        for t in trades[::-1][:trades_limit]:
            recent += self._trade_block(t, hist)
        parts.append("\n".join(recent))
        ev = self.j.events()[-events_limit:]
        parts.append("# Recent diary\n\n" + "\n".join(f"- {_ts(e['ts'])} {e['kind']}: {e['text']}" for e in ev))
        text = "\n\n".join(parts)
        return text if len(text) <= max_chars else text[:max_chars] + "\n\n[truncated]"
