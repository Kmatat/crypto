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


class ClaudeJSON:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120)

    def ask(self, system: str, prompt: str, schema: dict, effort: str = "medium") -> dict | None:
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                # If a safety classifier declines, re-run on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
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
