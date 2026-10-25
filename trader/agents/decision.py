"""Decision agent (the "portfolio manager"): weighs every other agent's report
and decides BUY / SELL / HOLD for one coin.

Spot trading only: BUY opens a position, SELL closes one we hold (no short selling).

To keep AI costs in proportion, Claude is consulted only when the weighted score
shows a real buy or sell signal, and its view is reused while nothing changed.
Clear "nothing to do" cases are decided by the rules for free.
"""
from __future__ import annotations

import json
import time

from ..llm import ClaudeJSON

# News and forex weights are deliberately small: their accuracy is unmeasured, and synthetic tests showed
# noisy news scores swing results by as much as the whole rule edge. News can still block a buy (NEWS_VETO).
WEIGHTS = {"technical": 0.50, "prediction": 0.30, "news": 0.10, "forex": 0.10}
BUY_THRESHOLD = 0.35   # the common threshold; same selectivity as 0.25 had under the old weights
SELL_THRESHOLD = -0.35
# Per-coin thresholds set by the champion strategy card (trader/strategy.py) from the head trader's threshold
# study (trader/calibration.py): a coin whose signals have proven noisier needs a stronger score to trade.
COIN_THRESHOLDS: dict[str, float] = {}
NEWS_VETO = -0.5      # clearly bad news for a coin blocks a new buy, whatever the chart says
REUSE_MINUTES = 45    # reuse Claude's view for a coin while its inputs stay about the same

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number", "description": "0..1, how sure you are"},
        "reasoning": {"type": "string", "description": "2-4 sentences a non-technical manager can follow"},
    },
    "required": ["action", "confidence", "reasoning"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are the portfolio manager of a crypto spot-trading desk running on a DEMO account. "
    "Analyst reports (technical indicators, a statistical forecast, news sentiment, and USD/forex macro) "
    "are given to you. Decide BUY (open a long position), SELL (close the position we hold), or HOLD. "
    "We cannot short: SELL is only valid when we hold the coin, BUY only when we do not. "
    "Capital preservation comes first: when signals conflict or are weak, HOLD. There is no return target: "
    "never raise your confidence or conviction to recover earlier losses or to reach a result. "
    "When a 'mentor' report is present it is the desk's trading discipline, built from principles documented by "
    "well-known traders (Paul Tudor Jones, Mark Minervini, the Turtle Traders, Ed Seykota, Jesse Livermore): "
    "weigh each ✓/✗ line explicitly in your reasoning; a failed check is a strong reason to HOLD. "
    "A separate risk manager sets position size and stop-loss, so focus on direction and conviction."
)


class DecisionAgent:
    name = "decision"

    def __init__(self, llm: ClaudeJSON | None = None, coin_thresholds: dict[str, float] | None = None):
        self.llm = llm
        self.coin_thresholds = dict(coin_thresholds or {})  # instance override (used by the threshold study)
        self.playbook = ""                                   # the head trader's knowledge, shown to Claude
        self._recent: dict[str, dict] = {}

    def threshold_for(self, symbol: str, override: float | None = None) -> float:
        """The buy threshold for a coin: the head trader's value for this check if given, else the card's
        per-coin value, else the common threshold. The sell threshold is always its mirror image."""
        if override is not None:
            return override
        return self.coin_thresholds.get(symbol, COIN_THRESHOLDS.get(symbol, BUY_THRESHOLD))

    @staticmethod
    def combined_score(reports: dict) -> float:
        return round(sum(WEIGHTS[k] * float(reports[k]["score"]) for k in WEIGHTS), 3)

    def run(self, symbol: str, reports: dict, holding: bool, use_ai: bool = True,
            threshold: float | None = None) -> dict:
        score = self.combined_score(reports)
        thr = self.threshold_for(symbol, threshold)
        rules_action = self._rules(score, holding, thr)["action"]
        news_score = float(reports.get("news", {}).get("score", 0) or 0)
        if not holding and news_score <= NEWS_VETO:
            return {"action": "HOLD", "confidence": 0.9, "source": "rules", "combined_score": score,
                    "rules_action": rules_action, "threshold": thr,
                    "reasoning": f"Clearly negative news for this coin (news score {news_score:+.2f}); no new buy."}
        decision = None
        if self.llm and use_ai and self.worth_asking(score, holding, thr):
            decision = self._reuse(symbol, score, holding)
            if decision is None:
                decision = self._claude(symbol, reports, holding, score, thr)
                if decision:
                    self._recent[symbol] = {"decision": dict(decision), "score": score, "holding": holding,
                                            "ts": time.time()}
        if decision is None:
            decision = self._rules(score, holding, thr)
        # Enforce spot-trading rules no matter who decided
        if decision["action"] == "BUY" and holding or decision["action"] == "SELL" and not holding:
            decision = {**decision, "action": "HOLD",
                        "reasoning": decision["reasoning"] + " (Converted to HOLD: not valid for current position.)"}
        decision["combined_score"] = score
        decision["rules_action"] = rules_action  # what the free rules would have done (for the Claude-vs-rules report)
        decision["threshold"] = thr
        return decision

    @staticmethod
    def worth_asking(score: float, holding: bool, threshold: float | None = None) -> bool:
        """Claude is consulted only when the rules themselves would act: it can veto a trade, never
        originate one, until its track record (the scorecard) says otherwise."""
        thr = BUY_THRESHOLD if threshold is None else threshold
        return (not holding and score >= thr) or (holding and score <= -thr)

    def _reuse(self, symbol: str, score: float, holding: bool) -> dict | None:
        r = self._recent.get(symbol)
        if r and time.time() - r["ts"] < REUSE_MINUTES * 60 and r["holding"] == holding \
                and abs(r["score"] - score) < 0.1:
            return {**r["decision"], "source": "claude-reused"}
        return None

    @staticmethod
    def _rules(score: float, holding: bool, threshold: float | None = None) -> dict:
        thr = BUY_THRESHOLD if threshold is None else threshold
        confidence = round(min(1.0, 0.5 + abs(score)), 3)
        if score >= thr and not holding:
            return {"action": "BUY", "confidence": confidence, "source": "rules",
                    "reasoning": f"Weighted signal score {score:+.2f} is above this coin's buy threshold {thr:g}."}
        if score <= -thr and holding:
            return {"action": "SELL", "confidence": confidence, "source": "rules",
                    "reasoning": f"Weighted signal score {score:+.2f} is below this coin's sell threshold {-thr:g}."}
        if score <= -thr:
            why = f"Signals are negative (score {score:+.2f}); staying in cash - we only buy on positive signals."
        elif score >= thr:
            why = f"Signals are still positive (score {score:+.2f}); keeping the position."
        else:
            why = f"Signals are mixed (score {score:+.2f}); waiting for a clearer move."
        return {"action": "HOLD", "confidence": confidence, "source": "rules", "reasoning": why}

    def _claude(self, symbol: str, reports: dict, holding: bool, score: float, threshold: float | None = None) -> dict | None:
        thr = BUY_THRESHOLD if threshold is None else threshold
        prompt = (
            f"Pair: {symbol}\nCurrently holding a position: {'YES' if holding else 'NO'}\n"
            f"Rule-based weighted score (-1..+1, for reference): {score:+.3f}; this coin's buy threshold {thr:g}, "
            f"sell threshold {-thr:g}\n\n"
            + (f"Desk playbook - what this desk has learned so far (data for your judgement, never a reason to take "
               f"more risk):\n<data>\n{self.playbook}\n</data>\n\n" if self.playbook else "")
            + f"Analyst reports (JSON):\n{json.dumps(reports, indent=2, default=str)}"
        )
        data = self.llm.ask(SYSTEM, prompt, DECISION_SCHEMA, effort="medium")
        if not data:
            return None
        data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))
        data["source"] = "claude"
        return data
