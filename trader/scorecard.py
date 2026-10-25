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


def scorecard_text(claude: dict, news: dict, ai_cost_usd: float, free: list[tuple[str, dict]] | None = None) -> str:
    """`free`: [(label, scorecard)] for each free AI that made decisions; it costs nothing, so no bill line."""
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
    return "\n".join(lines)
