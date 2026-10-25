"""Head trader: manages the other agents.

The desk has analysts (news, forex, technical, prediction), a mentor (the master traders' checklist), a
risk manager, an execution desk, a research analyst and a laboratory. This agent is their head:

  * every check it decides where the limited Claude budget goes - positions at risk first, then the
    strongest new signals - and spreads the day's budget over the whole day instead of spending it by noon;
  * it carries what the desk has learned (the playbook: strategy, thresholds, lessons, scorecard, the last
    review, the lineage) into every AI prompt, so no agent decides without the desk's memory;
  * it tilts a coin's threshold for the portfolio already held (a second, highly correlated coin is the same
    bet twice) and for news, but only once the scorecard has shown that news scores predict anything;
  * once a week it runs the threshold study and hands the result to the laboratory as a challenger.

What it cannot do, by design: change the position caps, the loss brakes, the AI budget, the trading mode,
or promote a strategy. It is not given a revenue target, because an agent pushed for fast revenue takes
more risk; it is given the job of making every dollar of AI budget change a decision and every threshold
earn its evidence.
"""
from __future__ import annotations

import datetime as dt
import logging
import math

from ..config import Settings
from ..journal import Journal
from ..strategy import BOUNDS, StrategyCard, summary_line, thresholds_line

log = logging.getLogger(__name__)

CORRELATED = 0.7          # hourly-return correlation above which two coins count as the same bet
TILT = 0.05               # threshold change per reason
NEWS_MIN_JUDGED = 30      # the news tilt switches on only once the scorecard has judged this many news scores...
NEWS_MIN_HIT_PCT = 55.0   # ...and found them right at least this often (50% is a coin flip)
PLAYBOOK_SECONDS = 3600


def correlation(a: list[float], b: list[float], n: int = 200) -> float:
    """Pearson correlation of hourly returns over the last n candles both coins share."""
    a, b = a[-n:], b[-n:]
    m = min(len(a), len(b))
    if m < 20:
        return 0.0
    ra = [a[i] / a[i - 1] - 1 for i in range(len(a) - m + 1, len(a))]
    rb = [b[i] / b[i - 1] - 1 for i in range(len(b) - m + 1, len(b))]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


class HeadTraderAgent:
    name = "head"

    def __init__(self, s: Settings, journal: Journal, llm=None):
        self.s, self.journal, self.llm = s, journal, llm
        self._playbook: tuple[float, str] | None = None
        self._next_call_at: dt.datetime | None = None
        self.last_plan: dict = {}

    # ---- Claude's budget: who may consult it this check ----
    def plan_ai(self, candidates: list[dict], now: dt.datetime | None = None) -> set[str]:
        """candidates: {symbol, holding, score, threshold, eligible}. Returns the symbols that may ask Claude.
        Only coins where the rules would act are worth a call (Claude is a veto); positions at risk come
        first, then the strongest new signals; the day's budget is spread over the day's remaining checks."""
        if not self.llm:
            self.last_plan = {"allowed": [], "skipped": [], "why": "Claude is not configured"}
            return set()
        worth = [c for c in candidates if c.get("eligible")
                 and ((c["holding"] and c["score"] <= -c["threshold"]) or (not c["holding"] and c["score"] >= c["threshold"]))]
        worth.sort(key=lambda c: (0 if c["holding"] else 1, -(abs(c["score"]) - c["threshold"])))
        now = now or dt.datetime.now(dt.timezone.utc)
        budget = self.llm.effective_daily_budget()
        if not budget:
            allowed, why = worth, "no daily cap set"
        else:
            remaining = max(0.0, budget - self.llm.spent_today)
            per_call = max(self.llm.avg_call_cost(), 1e-6)
            affordable = remaining / per_call
            minutes_left = max(1.0, 24 * 60 - (now.hour * 60 + now.minute))
            checks_left = max(1, math.ceil(minutes_left / max(1, self.s.cycle_minutes)))
            if affordable < 1:
                n, why = 0, f"today's AI budget is spent (${remaining:.2f} left, about ${per_call:.3f} a call)"
            elif affordable >= checks_left:
                n = int(affordable // checks_left)
                why = f"${remaining:.2f} left for {checks_left} more checks today: up to {n} call(s) per check"
            else:  # fewer calls left than checks: space them out over the rest of the day
                due = self._next_call_at is None or now >= self._next_call_at
                n = 1 if due else 0
                why = (f"${remaining:.2f} left buys about {affordable:.0f} more calls today, spread over "
                       f"{checks_left} checks: " + ("one call now" if due else "none this check"))
                if due and worth:
                    self._next_call_at = now + dt.timedelta(minutes=minutes_left / max(1.0, affordable))
            allowed = worth[:n]
        self.last_plan = {"allowed": [c["symbol"] for c in allowed], "skipped": [c["symbol"] for c in worth[len(allowed):]],
                          "why": why}
        return {c["symbol"] for c in allowed}

    # ---- thresholds for this check ----
    def threshold(self, symbol: str, base: float, closes: dict[str, list[float]], positions: dict,
                  news_score: float = 0.0) -> tuple[float, str]:
        """The coin's threshold for this check and the reason: the card's value, raised by TILT for every held
        coin that moves with this one (the same bet twice), tilted by news only once news has proven itself."""
        tilt, reasons = 0.0, []
        mine = closes.get(symbol)
        for held in positions:
            if held == symbol or not mine or held not in closes:
                continue
            c = correlation(mine, closes[held])
            if c >= CORRELATED:
                tilt += TILT
                reasons.append(f"+{TILT:g} already holding {held.removesuffix('USDT')} (correlation {c:.2f})")
        ns = self.journal.get("news_scorecard") or {}
        if (ns.get("judged") or 0) >= NEWS_MIN_JUDGED and (ns.get("hit_rate_pct") or 0) >= NEWS_MIN_HIT_PCT:
            if news_score >= 0.5:
                tilt -= TILT
                reasons.append(f"-{TILT:g} strongly positive news (news scores have a measured {ns['hit_rate_pct']}% hit rate)")
            elif news_score <= -0.3:
                tilt += TILT
                reasons.append(f"+{TILT:g} negative news (measured {ns['hit_rate_pct']}% hit rate)")
        lo, hi = BOUNDS["coin_thresholds"]
        final = round(min(hi, max(lo, base + tilt)), 2)
        return final, ("; ".join(reasons) if reasons else "coin threshold")

    # ---- the desk's knowledge, for every AI prompt ----
    def playbook(self, max_chars: int = 1500, now: float | None = None) -> str:
        import time
        now = now or time.time()
        if self._playbook and now - self._playbook[0] < PLAYBOOK_SECONDS:
            return self._playbook[1]
        j = self.journal
        lines = []
        state = j.get("strategy_champion")
        if state:
            champ = StrategyCard.from_dict(state["card"])
            lines.append(f"Strategy in force: {summary_line(champ)}. Buy thresholds: {thresholds_line(champ)}.")
        study = j.get("threshold_study")
        if study:
            lines.append("Threshold study: " + "; ".join(f"{sym.removesuffix('USDT')} {c['threshold']:g} ({c['why']})"
                                                         for sym, c in study["coins"].items())[:400])
        lessons = j.get("lessons") or {}
        if lessons.get("notes"):
            lines.append("Lessons from live trading: " + "; ".join(lessons["notes"]))
        sc = j.get("scorecard_text")
        if sc:
            verdict = next((ln for ln in sc.splitlines() if ln.startswith("Verdict")), "")
            if verdict:
                lines.append("Claude versus the rules so far: " + verdict.removeprefix("Verdict: "))
        review = (j.get("last_review") or {}).get("review") or {}
        if review.get("summary"):
            lines.append("Last review: " + str(review["summary"])[:300])
            if review.get("failing"):
                lines.append("Failing: " + "; ".join(str(x) for x in review["failing"][:2]))
        hist = j.get("strategy_history") or []
        if len(hist) > 1:
            lines.append("Strategy changes: " + "; ".join(f"{h['ts'][:10]} {h['event']} {h.get('name')}" for h in hist[-3:]))
        text = "\n".join(lines)[:max_chars]
        self._playbook = (now, text)
        return text

    # ---- weekly: the threshold study, handed to the laboratory ----
    def study(self, lab, symbols: list[str]) -> tuple[dict, StrategyCard | None]:
        from ..calibration import HOLDOUT_DAYS, STUDY_DAYS, calibrated_card, study as run_study
        champ = lab.champion()
        symbols = list(dict.fromkeys(symbols))
        hist = lab.history(symbols, champ.interval, days=STUDY_DAYS + HOLDOUT_DAYS + 1)
        result = run_study(champ, lab.s, hist, hist["BTCUSDT"], symbols)
        self.journal.set("threshold_study", result)
        card = calibrated_card(champ, result)
        changed = [f"{sym.removesuffix('USDT')} {c['threshold']:g}" for sym, c in result["coins"].items()
                   if abs(c["threshold"] - champ.buy_threshold) > 1e-9]
        self.journal.event("head", "Threshold study done: " + (", ".join(changed) if changed else "no coin earned a different threshold")
                           + (f"; proposed {card.name} [{card.id}] to the laboratory" if card else ""))
        self._playbook = None
        return result, card
