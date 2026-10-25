"""One door to the desk's AIs: the free AI and Claude, in the order the owner chose, then the free rules.

Every agent asks its question here exactly as it asked Claude before. Free-first (the default when a free AI is
configured) sends each question to the free tier and only pays Claude when the free AI cannot answer (daily
allowance used, refused, an answer in the wrong form). Claude-first keeps Claude's judgement while its daily budget
lasts and uses the free AI after. Two things stay with Claude whatever the order:
- web research (the coin scout's and the weekly lab's): free tiers have no web search the desk can use;
- the /review of the notebook: it holds the desk's private records, and free tiers may train on what they read.

Every answer carries who gave it ("_ai": "claude" or "ai:<provider>"), so the scorecard judges each AI separately.
"""
from __future__ import annotations

import datetime as dt
import math

ORDERS = ("free-first", "claude-first")
CLAUDE_ONLY = {"review"}


def is_ai_source(source) -> bool:
    """A decision or score made by an AI (Claude or a free AI), not by the free rules or keywords."""
    return str(source or "").startswith(("claude", "ai:"))


def ai_name(source) -> str:
    """'claude', 'ai:gemini-reused' -> 'claude', 'gemini'."""
    s = str(source or "")
    if s.startswith("ai:"):
        return s[3:].removesuffix("-reused")
    return "claude" if s.startswith("claude") else s


class AIRouter:
    def __init__(self, claude=None, free=None, order: str = "free-first"):
        self.claude, self.free = claude, free
        self.order = order if order in ORDERS else "free-first"

    def __bool__(self) -> bool:
        return bool(self.claude or self.free)

    def chain(self, caller: str) -> list:
        if caller in CLAUDE_ONLY:
            return [self.claude] if self.claude else []
        pair = [self.free, self.claude] if self.order == "free-first" else [self.claude, self.free]
        return [c for c in pair if c]

    def label(self, client) -> str:
        return "claude" if client is self.claude else f"ai:{client.provider}"

    def ask(self, system: str, prompt: str, schema: dict, effort: str = "medium", caller: str = "other") -> dict | None:
        for client in self.chain(caller):
            data = client.ask(system, prompt, schema, effort=effort, caller=caller)
            if data is not None:
                return {**data, "_ai": self.label(client)}
        return None

    def research(self, system: str, prompt: str, max_searches: int = 5, effort: str = "medium",
                 caller: str = "research") -> dict | None:
        return self.claude.research(system, prompt, max_searches=max_searches, effort=effort, caller=caller) \
            if self.claude else None

    # ---- the head trader's pacing ----
    def allowance(self, now: dt.datetime, pace_minutes: float, news_refresh_minutes: float,
                  scout_hours: float = 6) -> tuple[float, int, str]:
        """(buy/sell questions the AIs can afford per market check on average for the rest of their day, the most in
        one check, why). A free AI is rationed by its requests left before its daily reset (minus what the news
        agent and the scout will need), Claude by dollars, exactly as before."""
        rate, cap, parts = 0.0, 0, []
        pace = max(1.0, pace_minutes)
        news_first = (self.chain("news") or [None])[0]
        for client in self.chain("decision"):
            if client is self.free:
                mins = client.minutes_to_reset()
                left = client.requests_left()
                reserve = (mins / max(1.0, news_refresh_minutes) if news_first is client else 0.0) \
                    + mins / 60 / max(0.5, scout_hours)
                spare = max(0.0, left - reserve)
                checks = max(1, math.ceil(mins / pace))
                rate += spare / checks
                cap += client.preset.burst
                parts.append(f"{client.label}: {left:.0f} requests left until its daily reset in {mins / 60:.1f} h"
                             + (f" ({reserve:.0f} kept for news and the scout)" if reserve >= 1 else ""))
            else:
                c = client
                budget = c.effective_daily_budget()
                if not budget:
                    rate, cap = math.inf, cap + 99
                    parts.append("Claude: no daily cap set")
                    continue
                minutes_left = max(1.0, 24 * 60 - (now.hour * 60 + now.minute))
                news_reserve = c.avg_call_cost("news") * minutes_left / max(1.0, news_refresh_minutes) \
                    if news_first is c else 0.0
                remaining = max(0.0, budget - c.spent_today - news_reserve)
                per_call = max(c.avg_call_cost("decision"), 1e-6)
                checks = max(1, math.ceil(minutes_left / pace))
                rate += remaining / per_call / checks
                cap += 99
                parts.append(f"Claude: ${remaining:.2f} left today, about ${per_call:.3f} a call"
                             + (f" (${news_reserve:.2f} kept for the news agent)" if news_reserve else ""))
        return rate, cap, "; ".join(parts) or "no AI configured"
