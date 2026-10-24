"""Thin wrapper around Claude for agents that need judgement (news sentiment, final decision).

Every call returns JSON matching a schema, or None on any failure so the caller
can fall back to its rule-based logic. A trading loop must never crash because
an AI call failed.
"""
from __future__ import annotations

import json
import logging

import anthropic

log = logging.getLogger(__name__)

# Models that accept server-side refusal fallbacks ("fallbacks": "default").
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

# Choices offered by the setup wizard: (model id, label, rough monthly cost at default settings)
MODEL_CHOICES = [
    ("claude-opus-5-5", "Claude Opus 5.5 - best judgement", "about $60/month"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 - balanced", "about $30/month"),
    ("claude-haiku-5-5", "Claude Haiku 5.5 - fastest, cheapest", "about $2/month"),
]


class ClaudeJSON:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120)

    def ask(self, system: str, prompt: str, schema: dict, effort: str = "medium") -> dict | None:
        params = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self.model in FALLBACK_MODELS:
                # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
                resp = self.client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                resp = self.client.messages.create(**params)
        except anthropic.RateLimitError:
            log.warning("Claude rate limited - using rule-based fallback")
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API error %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError:
            log.warning("Claude unreachable - using rule-based fallback")
            return None

        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("Claude stopped with %s - using rule-based fallback", resp.stop_reason)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError:
            return None
