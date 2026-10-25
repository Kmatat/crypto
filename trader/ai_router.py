"""One door to the desk's AIs: the free AI and Claude, in the order the owner chose, then the free rules.

Every agent asks its question here exactly as it asked Claude before. Free-first (the default when a free AI is
configured) sends each question to the free tier and only pays Claude when the free AI cannot answer (daily
allowance used, refused, an answer in the wrong form). Claude-first keeps Claude's judgement while its daily budget
lasts and uses the free AI after. Three things stay with Claude whatever the order:
- web research (the coin scout's and the weekly lab's): free tiers have no web search the desk can use;
- the weekly lab's strategy proposals, which are built from the notebook's lessons and strategy history;
- the /review of the notebook: it holds the desk's private records, and free tiers may train on what they read.

Every answer carries who gave it ("_ai": "claude" or "ai:<provider>"), so the scorecard judges each AI separately.
"""
from __future__ import annotations

import datetime as dt
import logging
import math

log = logging.getLogger(__name__)

ORDERS = ("free-first", "claude-first")
CLAUDE_ONLY = {"review", "research"}


def effective_order(s, journal) -> str:
    """The order in force: the owner's /ai choice while the settings file still says what it said when the choice was
    made (a later desk setup wins), else AI_ORDER."""
    ov = journal.get("ai_order_override")
    if isinstance(ov, dict) and ov.get("order") in ORDERS and ov.get("settings") == s.ai_order:
        return ov["order"]
    return s.ai_order if s.ai_order in ORDERS else "free-first"


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
            try:
                data = client.ask(system, prompt, schema, effort=effort, caller=caller)
            except Exception:  # noqa: BLE001 - one AI failing must leave the next one, and the rules, to answer
                log.exception("AI %s failed", self.label(client))
                data = None
            if data is not None:
                return {**data, "_ai": self.label(client)}
        return None

    def _claude_has_budget(self) -> bool:
        budget = self.claude.effective_daily_budget()
        return not budget or self.claude.spent_today < budget

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
        news = self.chain("news")
        # who reads the news for the rest of the day: the first in the news order, and the free AI also when Claude
        # comes first but has spent its budget; Claude reserves only the refreshes the free AI cannot cover
        free_reads = bool(news) and self.free in news and (news[0] is self.free or not self._claude_has_budget())
        free_cover = 0.0
        if self.free and free_reads and news[0] is self.free:
            free_cover = max(0.0, self.free.requests_left() - self.free.minutes_to_reset() / 60 / max(0.5, scout_hours))
        for client in self.chain("decision"):
            if client is self.free:
                mins = client.minutes_to_reset()
                left = client.requests_left()
                reserve = (mins / max(1.0, news_refresh_minutes) if free_reads else 0.0) \
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
                refreshes = minutes_left / max(1.0, news_refresh_minutes)
                if news and news[0] is not c:
                    refreshes = max(0.0, refreshes - free_cover)   # the free AI reads first, as long as it can
                news_reserve = c.avg_call_cost("news") * refreshes
                remaining = max(0.0, budget - c.spent_today - news_reserve)
                per_call = max(c.avg_call_cost("decision"), 1e-6)
                checks = max(1, math.ceil(minutes_left / pace))
                rate += remaining / per_call / checks
                cap += 99
                parts.append(f"Claude: ${remaining:.2f} left today, about ${per_call:.3f} a call"
                             + (f" (${news_reserve:.2f} kept for the news agent)" if news_reserve else ""))
        return rate, cap, "; ".join(parts) or "no AI configured"
