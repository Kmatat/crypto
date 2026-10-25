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
from fractions import Fraction

from .agents.decision import DecisionAgent
from .backtest import Backtest
from .config import Settings
from .strategy import BOUNDS, StrategyCard

CANDIDATES = [round(0.15 + 0.05 * i, 2) for i in range(10)]   # 0.15 .. 0.60, the same range the cards allow
MIN_TRADES = 12        # trades needed in EACH half of the study window, at the chosen and at the common threshold
MIN_EDGE_POINTS = 1.0  # the chosen threshold must beat the common one by this many points over the study window
SHRINK_TRADES = 20     # evidence weight n/(n+20): a pick k steps away moves round(k*n/(n+20)) steps (ties stay)
HOLDOUT_DAYS = 90      # the most recent days are never used: the exam's 90-day window judges the choice
STUDY_DAYS = 365       # a full year before the holdout, judged as two halves (the market changes within a year)
STEP = 0.05


def _shrink(base: float, best: float, n: int) -> float:
    """Pull the pick towards the common threshold in whole grid steps, with exact arithmetic: a pick k steps away
    moves k*n/(n+SHRINK_TRADES) steps, rounded towards the common threshold on a tie. So one step needs more than
    20 trades and two steps need more than 60."""
    steps = round((best - base) / STEP)
    moved = Fraction(steps * n, n + SHRINK_TRADES)
    whole = int(moved)                       # towards zero, i.e. towards the common threshold
    if abs(moved - whole) > Fraction(1, 2):  # strictly more than half a step left over: one more step
        whole += 1 if moved > 0 else -1
    return round(base + whole * STEP, 2)


def choose(rows: list[dict], base: float, max_drawdown_pct: float) -> dict:
    """The threshold a coin has earned, from its rows (one per candidate threshold). Each row covers the study window
    and its two halves. A different threshold is chosen only when the evidence is strong on every count: profitable
    after fees, clearly better than the common threshold, better in BOTH halves with enough trades in each, and then
    pulled back towards the common threshold in proportion to how little evidence there is."""
    at_base = next((r for r in rows if abs(r["threshold"] - base) < 1e-9), None)
    keep = {"threshold": base, "best": None, "trades": max((r["trades"] for r in rows), default=0), "return_pct": None,
            "base_return_pct": at_base["return_pct"] if at_base else None, "edge": 0.0}
    ok = [r for r in rows if r["max_drawdown_pct"] <= max_drawdown_pct and all(h["trades"] >= MIN_TRADES for h in r["halves"])]
    if not ok or at_base is None:
        return {**keep, "why": f"fewer than {MIN_TRADES} trades in each half of the study window at every threshold: "
                               f"the common threshold {base:g} stays"}
    best = max(ok, key=lambda r: (r["return_pct"], r["profit_factor"] or 0.0, -abs(r["threshold"] - base)))
    keep.update(best=best["threshold"], trades=best["trades"], return_pct=best["return_pct"])
    halves = "/".join(str(h["trades"]) for h in best["halves"])
    if abs(best["threshold"] - base) < 1e-9:
        return {**keep, "why": f"the common threshold {base:g} was already the best ({best['return_pct']:+.2f}% on "
                               f"{best['trades']} trades, halves {halves})"}
    if best["return_pct"] <= 0 or (best["profit_factor"] or 0) < 1.0:
        return {**keep, "why": f"no threshold made money after fees in the study window (best {best['threshold']:g}: "
                               f"{best['return_pct']:+.2f}%, profit factor {best['profit_factor']}): {base:g} stays"}
    edge = best["return_pct"] - at_base["return_pct"]
    if edge < MIN_EDGE_POINTS:
        return {**keep, "why": f"best {best['threshold']:g} beat {base:g} by only {edge:+.2f} points in the study window "
                               f"(need {MIN_EDGE_POINTS:g}): not evidence"}
    if any(h["trades"] < MIN_TRADES for h in at_base["halves"]):
        return {**keep, "why": f"too few trades at the common threshold in one half of the study window to compare"}
    lost = [i + 1 for i, (h, b) in enumerate(zip(best["halves"], at_base["halves"])) if h["return_pct"] <= b["return_pct"]]
    if lost:
        return {**keep, "why": f"best {best['threshold']:g} did not beat {base:g} in half {lost[0]} of the study window "
                               "(a pick that works in only one market phase is not evidence)"}
    final = _shrink(base, best["threshold"], best["trades"])
    lo, hi = BOUNDS["coin_thresholds"]
    final = min(hi, max(lo, final))
    why = (f"in the study window (ending {HOLDOUT_DAYS} days ago) {best['threshold']:g} made {best['return_pct']:+.2f}% "
           f"on {best['trades']} trades (halves {halves}) vs {at_base['return_pct']:+.2f}% at {base:g}, and won both halves"
           + (f"; moved only to {final:g} for the sample size" if abs(final - best["threshold"]) > 1e-9 else ""))
    if abs(final - base) < 1e-9:
        why = f"{best['threshold']:g} looked better ({why}) but {best['trades']} trades are too few to move a full step"
    return {**keep, "threshold": final, "edge": round(edge, 2), "why": why}


def study(card: StrategyCard, s: Settings, history: dict[str, list[dict]], btc: list[dict], symbols: list[str],
          now_ms: int | None = None, should_stop=None) -> dict:
    """Replay every candidate threshold for every coin on the study window (before the holdout) and its two halves.
    The card's own rules are used, so the study never changes what the live desk is doing meanwhile."""
    out = {"card": card.id, "base": card.buy_threshold, "holdout_days": HOLDOUT_DAYS, "study_days": STUDY_DAYS,
           "coins": {}, "studied_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    settings = card.settings(s)
    half = STUDY_DAYS // 2
    for sym in symbols:
        if should_stop and should_stop():
            from .lab import LabInterrupted
            raise LabInterrupted()
        candles = history.get(sym)
        if not candles:
            continue
        end = (now_ms or candles[-1]["time"]) - HOLDOUT_DAYS * 86_400_000
        cache: dict = {}  # indicators do not depend on the threshold: computed once for all ten candidates
        rows = []
        for t in CANDIDATES:
            agent = DecisionAgent.for_card(card, coin_thresholds={**card.coin_thresholds, sym: t})

            def run(days, end_ms):
                return Backtest(settings, {sym: candles}, decision=agent, days=days, btc_history=btc, end_ms=end_ms,
                                analysis_cache=cache).run()
            r = run(STUDY_DAYS, end)
            first, second = run(STUDY_DAYS - half, end - half * 86_400_000), run(half, end)
            rows.append({"threshold": t, "return_pct": r["return_pct"], "trades": r["trades"],
                         "profit_factor": r["profit_factor"], "max_drawdown_pct": r["max_drawdown_pct"],
                         "halves": [{"return_pct": h["return_pct"], "trades": h["trades"]} for h in (first, second)]})
        out["coins"][sym] = {**choose(rows, card.buy_threshold, s.max_drawdown_pct * 100), "rows": rows}
    return out


def calibrated_card(card: StrategyCard, result: dict, now: str | None = None, max_coins: int | None = None) -> StrategyCard | None:
    """The champion with the studied thresholds, as a challenger for the laboratory; None if nothing changes. At most
    `max_coins` coins change (the laboratory's two-changes rule), those with the clearest evidence first."""
    from .strategy import MAX_CHANGES
    max_coins = MAX_CHANGES if max_coins is None else max_coins
    moved = sorted(((sym, c) for sym, c in result["coins"].items() if abs(c["threshold"] - card.buy_threshold) > 1e-9),
                   key=lambda x: -x[1].get("edge", 0.0))[:max_coins]
    merged = dict(card.coin_thresholds)
    for sym, c in moved:
        merged[sym] = c["threshold"]
    if merged == card.coin_thresholds:
        return None
    new = StrategyCard.from_dict(card.to_dict())
    new.coin_thresholds = merged
    stamp = (now or result["studied_at"])[:10]
    new.name = f"per-coin thresholds (study {stamp})"
    new.rationale = ("Threshold study on the year before the last 90 days, judged in two halves: "
                     + "; ".join(f"{sym.removesuffix('USDT')} {c['threshold']:g} - {c['why']}" for sym, c in moved))[:600]
    new.source = "study"
    new.created_at = now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return new


def study_text(result: dict) -> str:
    lines = [f"📐 Threshold study: per coin, the year before the last {result['holdout_days']} days, judged in two halves "
             f"(the last {result['holdout_days']} days are kept for the exam); common threshold {result['base']:g}"]
    for sym, c in result["coins"].items():
        arrow = f"{result['base']:g} → {c['threshold']:g}" if abs(c["threshold"] - result["base"]) > 1e-9 else f"stays {result['base']:g}"
        lines.append(f"• {sym.removesuffix('USDT')}: {arrow} - {c['why']}")
    return "\n".join(lines)
