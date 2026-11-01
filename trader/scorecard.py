"""Scorecard: is each AI worth its place? Computed by code, never by an AI.

For every decision where an AI (Claude, or the free AI, each judged on its own) chose differently from the free
rules (today that means it vetoed a buy or a sell the rules wanted), the trade the rules would have made is
replayed with the real prices that followed, using the same stop-loss, take-profit and
trailing-stop rules. Summing those counterfactuals gives "money the vetoes saved" and
"money the vetoes cost". The news agent gets the same treatment: each coin score is
compared with the coin's actual move over the next 24 hours.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from .agents.execution import PAPER_FEE, STOP_SLIPPAGE
from .config import Settings
from .journal import Journal

log = logging.getLogger(__name__)
HORIZON_HOURS = 120   # a counterfactual trade is followed for at most five days


class CandleCache:
    """candles_after(symbol, since_ms) for one scorecard run: each coin's hourly prices are downloaded once (up to
    1,000 hours from the earliest moment asked for) and every case is cut from that copy. Asking the exchange once per
    case took minutes on a busy journal and held up every Telegram command behind /report."""
    LIMIT = 130    # hourly candles per case, the same as one direct request returned
    SPAN = 1000    # the exchange's largest single download

    TRIES = 2      # a coin whose download fails twice is skipped for the rest of this run (judged next time)
    OUTAGE = 3     # failed downloads in a row, any coin: the exchange is down, skip everything left

    def __init__(self, fetch):
        self.fetch = fetch   # fetch(symbol, start_ms, limit) -> hourly candles opening at or after start_ms
        self.rows: dict[str, tuple[int, list[dict], bool]] = {}
        self.failed: dict[str, tuple[int, Exception]] = {}
        self.streak, self.last_error = 0, None

    def __call__(self, symbol: str, since_ms: int) -> list[dict]:
        tries, error = self.failed.get(symbol, (0, None))
        if tries >= self.TRIES or self.streak >= self.OUTAGE:   # each try may wait 10 s: never one per case
            raise error or self.last_error
        try:
            out = self._candles(symbol, since_ms)
        except Exception as e:
            self.failed[symbol] = (tries + 1, e)
            self.streak, self.last_error = self.streak + 1, e
            raise
        self.streak = 0
        return out

    def _candles(self, symbol: str, since_ms: int) -> list[dict]:
        got = self.rows.get(symbol)
        if got is None or since_ms < got[0]:
            rows = self.fetch(symbol, since_ms, self.SPAN)
            got = self.rows[symbol] = (since_ms, rows, len(rows) < self.SPAN)   # fewer than asked: reaches now
        _, rows, to_now = got
        out = [r for r in rows if r["time"] >= since_ms][:self.LIMIT]
        if len(out) < self.LIMIT and not to_now:   # past the end of the copy: ask for this case directly
            return self.fetch(symbol, since_ms, self.LIMIT)
        return out


def replay_trade(candles: list[dict], entry: float, atr: float, s: Settings) -> dict:
    """What a rules trade opened at `entry` would have done over the candles that followed."""
    stop = entry - s.stop_loss_atr * atr
    take = entry + s.take_profit_atr * atr
    high = entry
    for i, c in enumerate(candles[:HORIZON_HOURS]):
        if c["low"] <= stop:
            px = stop * (1 - STOP_SLIPPAGE)
            return {"exit": "stop-loss", "pnl_pct": (px * (1 - PAPER_FEE) / (entry * (1 + PAPER_FEE)) - 1) * 100, "hours": i + 1}
        if c["high"] >= take:
            return {"exit": "take-profit", "pnl_pct": (take * (1 - PAPER_FEE) / (entry * (1 + PAPER_FEE)) - 1) * 100, "hours": i + 1}
        high = max(high, c["close"])
        if s.trail_atr:
            stop = max(stop, high - s.trail_atr * atr)
    if not candles:
        return {"exit": "unknown", "pnl_pct": 0.0, "hours": 0}
    last = candles[min(len(candles), HORIZON_HOURS) - 1]["close"]
    return {"exit": "open", "pnl_pct": (last * (1 - PAPER_FEE) / (entry * (1 + PAPER_FEE)) - 1) * 100,
            "hours": min(len(candles), HORIZON_HOURS)}


def claude_scorecard(j: Journal, s: Settings, candles_after, position_usd: float | None = None,
                     source: str = "claude", name: str = "Claude") -> dict:
    """candles_after(symbol, since_ms) -> hourly candles after a moment. Returns totals and a per-case list.
    `source` picks the AI: "claude", or "ai:gemini" (a free AI) - each AI has its own verdict."""
    size = position_usd or s.starting_balance * s.max_position_pct
    rows = j.db.execute("SELECT id, ts, symbol, action, rules_action, reports FROM decisions WHERE source LIKE ? "
                        "AND rules_action IS NOT NULL AND action != rules_action ORDER BY id", (source + "%",)).fetchall()
    cases, saved, cost = [], 0.0, 0.0
    cached = j.get("scorecard_cases") or {}
    for r in rows:
        key = str(r["id"])
        if key in cached and cached[key].get("final"):
            case = cached[key]
        else:
            ts_ms = int(dt.datetime.fromisoformat(r["ts"]).timestamp() * 1000)
            try:
                reports = json.loads(r["reports"])
                price, atr = reports["technical"]["price"], reports["technical"]["atr"]
            except (KeyError, TypeError, ValueError):
                continue
            try:
                candles = candles_after(r["symbol"], ts_ms)
            except Exception as e:
                log.warning("Scorecard: no prices for %s: %s", r["symbol"], e)
                continue
            if r["rules_action"] == "BUY":          # Claude vetoed a buy: would the buy have made money?
                out = replay_trade(candles, price, atr, s)
                pnl = size * out["pnl_pct"] / 100
                case = {"symbol": r["symbol"], "ts": r["ts"], "kind": "vetoed buy", "pnl_usd": round(pnl, 2),
                        "exit": out["exit"], "final": out["exit"] != "open" or len(candles) >= HORIZON_HOURS}
            else:                                   # Claude vetoed a sell: did holding on pay?
                last = candles[min(len(candles), 24) - 1]["close"] if candles else price
                pnl = size * (last / price - 1)
                case = {"symbol": r["symbol"], "ts": r["ts"], "kind": "vetoed sell", "pnl_usd": round(pnl, 2),
                        "exit": "24h later", "final": len(candles) >= 24}
            cached[key] = case
        cases.append(case)
    j.set("scorecard_cases", cached)
    for c in cases:
        if c["kind"] == "vetoed buy":
            # the rules' trade would have made pnl_usd; a veto "saved" money if that trade lost
            if c["pnl_usd"] < 0:
                saved += -c["pnl_usd"]
            else:
                cost += c["pnl_usd"]
        else:
            # Claude kept a position the rules would have sold; it "saved" money if the price rose afterwards
            if c["pnl_usd"] > 0:
                saved += c["pnl_usd"]
            else:
                cost += -c["pnl_usd"]
    net = saved - cost
    verdict = "not enough divergent decisions yet (need 50)" if len(cases) < 50 else \
        (f"{name} is adding value" if net > 0 else f"{name} is costing money versus the rules")
    return {"cases": len(cases), "saved_usd": round(saved, 2), "cost_usd": round(cost, 2), "net_usd": round(net, 2),
            "verdict": verdict}


CHECKLIST_REASON = "master traders' checklist: {} not met"   # the buys loop's reason, one rule refused alone
MEMORY_MIN_CASES = 30


def checklist_scorecard(j: Journal, s: Settings, candles_after, check: str = "memory",
                        position_usd: float | None = None) -> dict:
    """The memory rule's refusals judged on real later prices, like the AI vetoes: each buy that `check` alone refused
    (another failed rule means the memory did not decide) is replayed with the same stop, target and trail the rules
    would have used (replay_trade). One case per coin while the previous case's what-if trade of that coin would still
    be open (the desk holds one position per coin). A refusal "saved" money when the what-if trade lost, "cost" money
    when it won. Cached in state memory_cases like scorecard_cases."""
    size = position_usd or s.starting_balance * s.max_position_pct
    rows = j.db.execute("SELECT id, ts, symbol, reports FROM decisions WHERE action='BUY' AND risk_approved=0 AND "
                        "risk_reason=? ORDER BY id", (CHECKLIST_REASON.format(check),)).fetchall()
    out = {"cases": 0, "losers": 0, "saved_usd": 0.0, "cost_usd": 0.0, "net_usd": 0.0, "base_loss_pct": None,
           "verdict": f"not enough refused buys judged yet (need {MEMORY_MIN_CASES})"}
    if not rows:
        return out
    cached = j.get("memory_cases") or {}
    busy: dict[str, int] = {}
    cases = []
    for r in rows:
        ts_ms = int(dt.datetime.fromisoformat(r["ts"]).timestamp() * 1000)
        if ts_ms < busy.get(r["symbol"], 0):
            continue
        key = str(r["id"])
        if key in cached and cached[key].get("final"):
            case = cached[key]
        else:
            try:
                reports = json.loads(r["reports"])
                price, atr = reports["technical"]["price"], reports["technical"]["atr"]
            except (KeyError, TypeError, ValueError):
                continue
            try:
                candles = candles_after(r["symbol"], ts_ms)
            except Exception as e:
                log.warning("Memory scorecard: no prices for %s: %s", r["symbol"], e)
                continue
            res = replay_trade(candles, price, atr, s)
            case = {"symbol": r["symbol"], "ts": r["ts"], "pnl_usd": round(size * res["pnl_pct"] / 100, 2),
                    "pnl_pct": res["pnl_pct"], "exit": res["exit"], "hours": res["hours"],
                    "final": res["exit"] != "open" or len(candles) >= HORIZON_HOURS}
            cached[key] = case
        busy[r["symbol"]] = ts_ms + int(case.get("hours") or 0) * 3_600_000
        cases.append(case)
    j.set("memory_cases", cached)
    saved = sum(-c["pnl_usd"] for c in cases if c["pnl_usd"] < 0)
    cost = sum(c["pnl_usd"] for c in cases if c["pnl_usd"] >= 0)
    first = min((c["ts"] for c in cases), default=None)
    made = [p for p in j.closed_positions() if first and p["opened_at"] >= first]
    out.update(cases=len(cases), losers=sum(1 for c in cases if c["pnl_pct"] <= 0), saved_usd=round(saved, 2),
               cost_usd=round(cost, 2), net_usd=round(saved - cost, 2),
               base_loss_pct=round(sum(1 for p in made if p["loss"]) / len(made) * 100, 1) if made else None)
    if len(cases) >= MEMORY_MIN_CASES:
        out["verdict"] = "the memory rule saved money" if saved > cost else "the memory rule cost money versus buying anyway"
    return out


def news_scorecard(j: Journal, candles_after, days: int = 30) -> dict:
    """Hit rate of the news scores: did a positive score precede a rise over the next 24 hours?"""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows = j.db.execute("SELECT ts, symbol, reports FROM decisions WHERE ts >= ? ORDER BY id", (since,)).fetchall()
    seen, hits, total, by = set(), 0, 0, {}
    for r in rows:
        try:
            news = json.loads(r["reports"])["news"]
            score = float(news["score"])
        except (KeyError, TypeError, ValueError):
            continue
        src = str(news.get("source") or "unlabelled").removesuffix("-reused")
        day = r["ts"][:13]  # one judgement per coin per hour
        if abs(score) < 0.3 or (r["symbol"], day) in seen:
            continue
        seen.add((r["symbol"], day))
        ts_ms = int(dt.datetime.fromisoformat(r["ts"]).timestamp() * 1000)
        try:
            candles = candles_after(r["symbol"], ts_ms)
        except Exception:
            continue
        if len(candles) < 24:
            continue
        move = candles[23]["close"] / candles[0]["open"] - 1
        hit = (move > 0) == (score > 0)
        total += 1
        hits += hit
        b = by.setdefault(src, [0, 0])
        b[0], b[1] = b[0] + 1, b[1] + hit
    return {"judged": total, "hit_rate_pct": round(hits / total * 100, 1) if total else None,
            "by_source": {k: {"judged": n, "hit_rate_pct": round(h / n * 100, 1)} for k, (n, h) in by.items()},
            "note": "50% is a coin flip; below 55% the news weight is not earning its place" if total >= 30 else
                    "not enough strong news scores judged yet (need 30)"}


def scorecard_text(claude: dict, news: dict, ai_cost_usd: float, free: list[tuple[str, dict]] | None = None,
                   memory: dict | None = None) -> str:
    """`free`: [(label, scorecard)] for each free AI that made decisions; it costs nothing, so no bill line. `memory`:
    the memory rule's refusals judged (checklist_scorecard), shown only when there are cases."""
    lines = ["🧪 Claude vs the free rules (computed from real later prices)",
             f"Divergent decisions judged: {claude['cases']} · vetoes saved ${claude['saved_usd']:,.2f} · "
             f"vetoes cost ${claude['cost_usd']:,.2f} · net ${claude['net_usd']:+,.2f} · AI bill so far ${ai_cost_usd:,.2f}",
             f"Verdict: {claude['verdict']}"]
    for label, card in free or []:
        lines += [f"🆓 {label} vs the free rules: {card['cases']} judged · vetoes saved ${card['saved_usd']:,.2f} · "
                  f"cost ${card['cost_usd']:,.2f} · net ${card['net_usd']:+,.2f} · bill $0", f"Verdict: {card['verdict']}"]
    lines.append(f"News scores judged: {news['judged']} · hit rate "
                 f"{news['hit_rate_pct'] if news['hit_rate_pct'] is not None else 'n/a'}% - {news['note']}")
    by = news.get("by_source") or {}
    if len(by) > 1:
        lines.append("News hit rate by reader: " + ", ".join(f"{k} {v['hit_rate_pct']}% ({v['judged']})"
                                                           for k, v in sorted(by.items())))
    if memory and memory.get("cases"):
        from .postmortem_report import memory_score_line
        lines.append(memory_score_line(memory))
    return "\n".join(lines)
