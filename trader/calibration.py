"""The threshold study: what buy threshold has each coin earned?

One fixed threshold for every coin treats Bitcoin's signals and a small alt-coin's signals as equally
trustworthy. They are not: a noisy coin produces more false signals, and every false signal costs fees
and a stop-loss. The study replays the rule core coin by coin at every allowed threshold, over a past
window that deliberately stops HOLDOUT_DAYS ago, and picks the threshold with the best after-fee result.
It then pulls that pick back towards the common threshold in proportion to how little evidence the coin
produced (a handful of trades proves nothing). The result becomes a strategy card ("per-coin
thresholds") that must pass the laboratory's exam - whose most recent window is exactly the held-out
period - and the forward test, and be approved by the owner, like any other change. Nothing here touches
the live desk directly.
"""
from __future__ import annotations

import datetime as dt

from .agents import decision as dmod
from .agents.decision import DecisionAgent
from .backtest import Backtest
from .config import Settings
from .strategy import BOUNDS, StrategyCard

CANDIDATES = [round(0.15 + 0.05 * i, 2) for i in range(10)]   # 0.15 .. 0.60, the same range the cards allow
MIN_TRADES = 12        # fewer trades at a threshold is not evidence for it
SHRINK_TRADES = 20     # with this many trades the coin's own evidence weighs 50% against the common threshold
HOLDOUT_DAYS = 90      # the most recent days are never used: the exam's 90-day window judges the choice
STUDY_DAYS = 365       # a full year before the holdout: the checklist keeps the desk in cash for months at a time
STEP = 0.05


def _round_step(x: float) -> float:
    return round(round(x / STEP) * STEP, 2)


def choose(rows: list[dict], base: float, max_drawdown_pct: float) -> dict:
    """The threshold a coin has earned, from its rows (one per candidate threshold)."""
    at_base = next((r for r in rows if abs(r["threshold"] - base) < 1e-9), None)
    ok = [r for r in rows if r["trades"] >= MIN_TRADES and r["max_drawdown_pct"] <= max_drawdown_pct]
    if not ok:
        return {"threshold": base, "best": None, "trades": max((r["trades"] for r in rows), default=0),
                "return_pct": None, "base_return_pct": at_base["return_pct"] if at_base else None,
                "why": f"fewer than {MIN_TRADES} trades at every threshold: the common threshold {base:g} stays"}
    best = max(ok, key=lambda r: (r["return_pct"], r["profit_factor"] or 0.0, -abs(r["threshold"] - base)))
    n = best["trades"]
    shrunk = base + (best["threshold"] - base) * n / (n + SHRINK_TRADES)
    lo, hi = BOUNDS["coin_thresholds"]
    final = min(hi, max(lo, _round_step(shrunk)))
    why = (f"best {best['threshold']:g} ({best['return_pct']:+.2f}% on {n} trades"
           + (f" vs {at_base['return_pct']:+.2f}% at the common {base:g}" if at_base else "") + ")"
           + (f", pulled towards {base:g} for the small sample" if abs(final - best["threshold"]) > 1e-9 else ""))
    return {"threshold": final, "best": best["threshold"], "trades": n, "return_pct": best["return_pct"],
            "base_return_pct": at_base["return_pct"] if at_base else None, "why": why}


def study(card: StrategyCard, s: Settings, history: dict[str, list[dict]], btc: list[dict], symbols: list[str],
          now_ms: int | None = None) -> dict:
    """Replay every candidate threshold for every coin on the study window (before the holdout)."""
    saved = (dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD, dict(dmod.WEIGHTS), dict(dmod.COIN_THRESHOLDS))
    out = {"card": card.id, "base": card.buy_threshold, "holdout_days": HOLDOUT_DAYS, "study_days": STUDY_DAYS,
           "coins": {}, "studied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    try:
        card.apply_decision_rules()
        settings = card.settings(s)
        for sym in symbols:
            candles = history.get(sym)
            if not candles:
                continue
            end = (now_ms or candles[-1]["time"]) - HOLDOUT_DAYS * 86_400_000
            cache: dict = {}  # indicators do not depend on the threshold: computed once for all ten candidates
            rows = []
            for t in CANDIDATES:
                r = Backtest(settings, {sym: candles}, decision=DecisionAgent(coin_thresholds={sym: t}),
                             days=STUDY_DAYS, btc_history=btc, end_ms=end, analysis_cache=cache).run()
                rows.append({"threshold": t, "return_pct": r["return_pct"], "trades": r["trades"],
                             "profit_factor": r["profit_factor"], "max_drawdown_pct": r["max_drawdown_pct"]})
            out["coins"][sym] = {**choose(rows, card.buy_threshold, s.max_drawdown_pct * 100), "rows": rows}
    finally:
        dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD = saved[0], saved[1]
        dmod.WEIGHTS.clear()
        dmod.WEIGHTS.update(saved[2])
        dmod.COIN_THRESHOLDS.clear()
        dmod.COIN_THRESHOLDS.update(saved[3])
    return out


def calibrated_card(card: StrategyCard, result: dict, now: str | None = None) -> StrategyCard | None:
    """The champion with the studied thresholds, as a challenger for the laboratory; None if nothing changes."""
    merged = dict(card.coin_thresholds)
    for sym, c in result["coins"].items():
        if abs(c["threshold"] - card.buy_threshold) > 1e-9:
            merged[sym] = c["threshold"]
        else:
            merged.pop(sym, None)
    if merged == card.coin_thresholds:
        return None
    new = StrategyCard.from_dict(card.to_dict())
    new.coin_thresholds = merged
    stamp = (now or result["studied_at"])[:10]
    new.name = f"per-coin thresholds (study {stamp})"
    new.rationale = ("Threshold study over the year before the last 90 days: "
                     + "; ".join(f"{sym.removesuffix('USDT')} {c['threshold']:g} - {c['why']}"
                                 for sym, c in result["coins"].items()))[:600]
    new.source = "study"
    new.created_at = now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return new


def study_text(result: dict) -> str:
    lines = [f"📐 Threshold study: {result['study_days']} days of history per coin, the most recent "
             f"{result['holdout_days']} days held out for the exam; common threshold {result['base']:g}"]
    for sym, c in result["coins"].items():
        arrow = f"{result['base']:g} → {c['threshold']:g}" if abs(c["threshold"] - result["base"]) > 1e-9 else f"stays {result['base']:g}"
        lines.append(f"• {sym.removesuffix('USDT')}: {arrow} - {c['why']}")
    return "\n".join(lines)
