"""Forex / Macro agent: watches the US dollar against major currencies.

Why it matters for crypto: a quickly strengthening dollar usually means
"risk-off" (money leaving speculative assets), which tends to weigh on crypto.
Data: European Central Bank reference rates via the free Frankfurter API (daily).
"""
from __future__ import annotations

import datetime as dt
import logging

import requests

log = logging.getLogger(__name__)
API = "https://api.frankfurter.dev/v1"
CURRENCIES = ["EUR", "GBP", "JPY", "CHF"]


class ForexAgent:
    name = "forex"

    def __init__(self, offline: bool = False):
        self.offline = offline
        self._cache: tuple[str, dict] | None = None

    def run(self) -> dict:
        today = dt.date.today().isoformat()
        if self._cache and self._cache[0] == today:
            return self._cache[1]
        result = self._neutral("offline mode") if self.offline else self._fetch()
        self._cache = (today, result)
        return result

    def _fetch(self) -> dict:
        start = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        try:
            r = requests.get(f"{API}/{start}..", params={"from": "USD", "to": ",".join(CURRENCIES)}, timeout=10)
            r.raise_for_status()
            rates = r.json()["rates"]
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("Forex data unavailable: %s", e)
            return self._neutral("data unavailable")
        days = sorted(rates)
        if len(days) < 2:
            return self._neutral("not enough data")
        first, last = rates[days[0]], rates[days[-1]]
        # Dollar strength = average % change of USD vs each currency over ~1 week
        changes = {c: (last[c] / first[c] - 1) * 100 for c in CURRENCIES if c in first and c in last}
        usd_change = sum(changes.values()) / len(changes)
        # +1% dollar move in a week is a strong risk-off signal -> score -1
        score = max(-1.0, min(1.0, -usd_change))
        return {
            "usd_strength_change_pct": round(usd_change, 3),
            "pairs_change_pct": {f"USD/{k}": round(v, 3) for k, v in changes.items()},
            "regime": "risk-off" if usd_change > 0.3 else "risk-on" if usd_change < -0.3 else "neutral",
            "score": round(score, 3),
        }

    @staticmethod
    def _neutral(reason: str) -> dict:
        return {"usd_strength_change_pct": 0.0, "pairs_change_pct": {}, "regime": "neutral", "score": 0.0, "note": reason}
