"""Thin wrapper around Claude for agents that need judgement (news, scouting, final decision).

Every call returns its answer, or None on any failure so the caller can fall
back to its rule-based logic. A trading loop must never crash because an AI
call failed. Spending is estimated from each response and capped per day.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import anthropic

log = logging.getLogger(__name__)

# Models that accept server-side refusal fallbacks ("fallbacks": "default").
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Models that take the newer web search tool (dynamic filtering); others use the basic version.
DYNAMIC_SEARCH_MODELS = {"claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                         "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6"}
# USD per million tokens (input, output), used only to estimate spend for the daily cap.
PRICES_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches

# Choices offered by the setup wizard: (model id, label, rough monthly cost with 15-min checks and up to 8 coins)
MODEL_CHOICES = [
    ("claude-opus-5-5", "Claude Opus 5.5 - best judgement", "typically $100-200/month"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 - balanced", "typically $50-100/month"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 - fastest, cheapest", "typically $5-15/month"),
]

API_ERRORS = (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)


class ClaudeJSON:
    def __init__(self, model: str, api_key: str, daily_budget_usd: float = 0, store=None):
        """`store` (the journal) keeps today's spend across restarts, so a restart never resets the cap."""
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120)
        self.daily_budget = daily_budget_usd
        self.budget_hit = False
        self.store = store
        self._day: dt.date | None = None
        self._spent = 0.0
        saved = store.get("ai_spend") if store else None
        if saved and saved.get("date") == dt.datetime.now(dt.timezone.utc).date().isoformat():
            self._day, self._spent = dt.date.fromisoformat(saved["date"]), float(saved["usd"])

    # ---- spending ----
    def _roll_day(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).date()
        if self._day != today:
            self._day, self._spent, self.budget_hit = today, 0.0, False

    def _save(self) -> None:
        if self.store:
            self.store.set("ai_spend", {"date": self._day.isoformat(), "usd": round(self._spent, 6)})

    @property
    def spent_today(self) -> float:
        self._roll_day()
        return self._spent

    def _allowed(self) -> bool:
        self._roll_day()
        if self.daily_budget and self._spent >= self.daily_budget:
            if not self.budget_hit:
                log.warning("Claude daily budget $%.2f reached - using rule-based logic until tomorrow (UTC)",
                            self.daily_budget)
            self.budget_hit = True
            return False
        return True

    def _track(self, resp) -> None:
        price_in, price_out = PRICES_PER_MTOK.get(self.model, PRICES_PER_MTOK["claude-opus-5-5"])
        u = resp.usage
        tokens_in = ((u.input_tokens or 0) + 1.25 * (getattr(u, "cache_creation_input_tokens", 0) or 0)
                     + 0.1 * (getattr(u, "cache_read_input_tokens", 0) or 0))
        stu = getattr(u, "server_tool_use", None)
        searches = (stu.get("web_search_requests") if isinstance(stu, dict)
                    else getattr(stu, "web_search_requests", 0)) or 0
        self._roll_day()
        self._spent += tokens_in / 1e6 * price_in + (u.output_tokens or 0) / 1e6 * price_out + searches * WEB_SEARCH_USD
        self._save()

    def _create(self, timeout: float | None = None, **params):
        client = self.client.with_options(timeout=timeout) if timeout else self.client
        if self.model in FALLBACK_MODELS:
            # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
            return client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return client.messages.create(**params)

    @staticmethod
    def _log_error(e: Exception) -> None:
        if isinstance(e, anthropic.RateLimitError):
            log.warning("Claude rate limited - using rule-based fallback")
        elif isinstance(e, anthropic.APIStatusError):
            log.warning("Claude API error %s: %s", e.status_code, e.message)
        else:
            log.warning("Claude unreachable - using rule-based fallback")

    # ---- calls ----
    def ask(self, system: str, prompt: str, schema: dict, effort: str = "medium") -> dict | None:
        """One question, answered as JSON matching `schema`."""
        if not self._allowed():
            return None
        try:
            resp = self._create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except API_ERRORS as e:
            self._log_error(e)
            return None
        self._track(resp)
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("Claude stopped with %s - using rule-based fallback", resp.stop_reason)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError:
            return None

    def research(self, system: str, prompt: str, max_searches: int = 5, effort: str = "medium") -> dict | None:
        """Let Claude search the web; returns {"text": notes, "sources": [urls]}."""
        if not self._allowed():
            return None
        tool_type = "web_search_20260209" if self.model in DYNAMIC_SEARCH_MODELS else "web_search_20250305"
        tools = [{"type": tool_type, "name": "web_search", "max_uses": max_searches}]
        messages: list = [{"role": "user", "content": prompt}]
        assistant: list = []
        texts, sources = [], []
        resp = None
        for _ in range(4):  # first request + up to 3 continuations of a long search turn
            if not self._allowed():
                break
            try:
                resp = self._create(timeout=300, model=self.model, max_tokens=16000, system=system,
                                    messages=messages, tools=tools, output_config={"effort": effort})
            except API_ERRORS as e:
                self._log_error(e)
                break
            self._track(resp)
            for b in resp.content:
                if b.type == "text":
                    texts.append(b.text)
                    for c in getattr(b, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url and url not in sources:
                            sources.append(url)
            if resp.stop_reason != "pause_turn":
                break
            # The server paused a long search loop: send the turn back and it resumes where it stopped.
            assistant += list(resp.content)
            messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": assistant}]
        if resp is not None and resp.stop_reason == "refusal":
            log.warning("Claude declined the research request - using market data only")
            return None
        text = "".join(texts).strip()
        return {"text": text, "sources": sources[:5]} if text else None
