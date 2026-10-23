"""News agent: reads crypto news headlines and scores sentiment per coin.

With a Claude key, Claude reads the headlines and judges market impact per coin
(it understands context like "ETF approved" vs "ETF rejected"). Without a key,
a simple keyword list is used.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

import requests

from ..llm import ClaudeJSON

log = logging.getLogger(__name__)

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]
COIN_NAMES = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth", "ether"], "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin"], "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"],
}
POSITIVE = ["surge", "rally", "soar", "jump", "gain", "bull", "record high", "approval", "approved", "adopt",
            "inflow", "partnership", "upgrade", "breakout", "all-time high", "buy"]
NEGATIVE = ["crash", "plunge", "drop", "fall", "bear", "hack", "exploit", "lawsuit", "ban", "sec charges",
            "outflow", "liquidat", "fraud", "selloff", "sell-off", "reject", "delay", "fine"]

SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "coins": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "coin": {"type": "string"},
                    "score": {"type": "number", "description": "-1 very bearish .. +1 very bullish"},
                    "summary": {"type": "string"},
                },
                "required": ["coin", "score", "summary"],
                "additionalProperties": False,
            },
        },
        "market_mood": {"type": "string"},
    },
    "required": ["coins", "market_mood"],
    "additionalProperties": False,
}


class NewsAgent:
    name = "news"

    def __init__(self, llm: ClaudeJSON | None = None, offline: bool = False, cache_seconds: int = 900):
        self.llm = llm
        self.offline = offline
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict] | None = None

    def run(self, coins: list[str]) -> dict:
        """Returns {"BTC": {"score": .., "summary": .., "headlines": [..]}, ..., "_mood": ".."}"""
        if self._cache and time.time() - self._cache[0] < self.cache_seconds:
            return self._cache[1]
        headlines = [] if self.offline else self.fetch_headlines()
        result = None
        if headlines and self.llm:
            result = self._claude_sentiment(headlines, coins)
        if result is None:
            result = self.keyword_sentiment(headlines, coins)
        self._cache = (time.time(), result)
        return result

    def fetch_headlines(self, per_feed: int = 25) -> list[str]:
        out = []
        for url in FEEDS:
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 crypto-agents"})
                r.raise_for_status()
                root = ET.fromstring(r.content)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    if title:
                        out.append(title)
                    if len(out) >= per_feed * len(FEEDS):
                        break
            except (requests.RequestException, ET.ParseError) as e:
                log.warning("News feed %s failed: %s", url, e)
        return list(dict.fromkeys(out))  # de-duplicate, keep order

    @staticmethod
    def mentions(headline: str, coin: str) -> bool:
        words = f" {headline.lower()} "
        return any(f" {n} " in words or f" {n}'" in words or f" {n}," in words for n in COIN_NAMES.get(coin, [coin.lower()]))

    @classmethod
    def keyword_sentiment(cls, headlines: list[str], coins: list[str]) -> dict:
        result: dict = {}
        for coin in coins:
            related = [h for h in headlines if cls.mentions(h, coin)]
            score = 0
            for h in related:
                low = h.lower()
                score += sum(w in low for w in POSITIVE) - sum(w in low for w in NEGATIVE)
            norm = max(-1.0, min(1.0, score / max(3, len(related))))
            result[coin] = {"score": round(norm, 3), "summary": f"{len(related)} related headlines (keyword scoring)",
                            "headlines": related[:5]}
        result["_mood"] = "unknown (keyword mode)" if headlines else "no news (offline or feeds down)"
        return result

    def _claude_sentiment(self, headlines: list[str], coins: list[str]) -> dict | None:
        prompt = (
            f"Coins: {', '.join(coins)}\n\nLatest crypto news headlines:\n"
            + "\n".join(f"- {h}" for h in headlines[:75])
            + "\n\nFor each coin, rate the likely short-term (next 24h) price impact of this news from -1 "
              "(very bearish) to +1 (very bullish). Market-wide news (regulation, macro, ETFs, big hacks) affects "
              "all coins. Use 0 when there is no meaningful news. Give a one-sentence summary per coin."
        )
        data = self.llm.ask(
            "You are a crypto news analyst on a trading desk. Be calibrated: most news is noise; "
            "reserve scores beyond +/-0.6 for genuinely market-moving events.",
            prompt, SENTIMENT_SCHEMA, effort="low",
        )
        if not data:
            return None
        result: dict = {"_mood": data["market_mood"]}
        by_coin = {c["coin"].upper(): c for c in data["coins"]}
        for coin in coins:
            c = by_coin.get(coin, {"score": 0, "summary": "no coverage"})
            result[coin] = {"score": max(-1.0, min(1.0, float(c["score"]))), "summary": c["summary"],
                            "headlines": [h for h in headlines if self.mentions(h, coin)][:5]}
        return result
