"""Decision agent (the "portfolio manager"): weighs every other agent's report
and decides BUY / SELL / HOLD for one coin.

Spot trading only: BUY opens a position, SELL closes one we hold (no short selling).
"""
from __future__ import annotations

import json

from ..llm import ClaudeJSON

WEIGHTS = {"technical": 0.35, "prediction": 0.25, "news": 0.25, "forex": 0.15}
BUY_THRESHOLD = 0.25
SELL_THRESHOLD = -0.25

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
    "Capital preservation comes first: when signals conflict or are weak, HOLD. "
    "A separate risk manager sets position size and stop-loss, so focus on direction and conviction."
)


class DecisionAgent:
    name = "decision"

    def __init__(self, llm: ClaudeJSON | None = None):
        self.llm = llm

    @staticmethod
    def combined_score(reports: dict) -> float:
        return round(sum(WEIGHTS[k] * float(reports[k]["score"]) for k in WEIGHTS), 3)

    def run(self, symbol: str, reports: dict, holding: bool) -> dict:
        score = self.combined_score(reports)
        decision = None
        if self.llm:
            decision = self._claude(symbol, reports, holding, score)
        if decision is None:
            decision = self._rules(score, holding)
        # Enforce spot-trading rules no matter who decided
        if decision["action"] == "BUY" and holding or decision["action"] == "SELL" and not holding:
            decision = {**decision, "action": "HOLD",
                        "reasoning": decision["reasoning"] + " (Converted to HOLD: not valid for current position.)"}
        decision["combined_score"] = score
        return decision

    @staticmethod
    def _rules(score: float, holding: bool) -> dict:
        confidence = round(min(1.0, 0.5 + abs(score)), 3)
        if score >= BUY_THRESHOLD and not holding:
            return {"action": "BUY", "confidence": confidence, "source": "rules",
                    "reasoning": f"Weighted signal score {score:+.2f} is above the buy threshold {BUY_THRESHOLD}."}
        if score <= SELL_THRESHOLD and holding:
            return {"action": "SELL", "confidence": confidence, "source": "rules",
                    "reasoning": f"Weighted signal score {score:+.2f} is below the sell threshold {SELL_THRESHOLD}."}
        if score <= SELL_THRESHOLD:
            why = f"Signals are negative (score {score:+.2f}); staying in cash - we only buy on positive signals."
        elif score >= BUY_THRESHOLD:
            why = f"Signals are still positive (score {score:+.2f}); keeping the position."
        else:
            why = f"Signals are mixed (score {score:+.2f}); waiting for a clearer move."
        return {"action": "HOLD", "confidence": confidence, "source": "rules", "reasoning": why}

    def _claude(self, symbol: str, reports: dict, holding: bool, score: float) -> dict | None:
        prompt = (
            f"Pair: {symbol}\nCurrently holding a position: {'YES' if holding else 'NO'}\n"
            f"Rule-based weighted score (-1..+1, for reference): {score:+.3f}\n\n"
            f"Analyst reports (JSON):\n{json.dumps(reports, indent=2, default=str)}"
        )
        data = self.llm.ask(SYSTEM, prompt, DECISION_SCHEMA, effort="medium")
        if not data:
            return None
        data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))
        data["source"] = "claude"
        return data
