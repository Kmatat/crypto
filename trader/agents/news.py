"""News agent: reads crypto news headlines and scores sentiment per coin.

With a Claude key, Claude reads the headlines and judges market impact per coin
(it understands context like "ETF approved" vs "ETF rejected"), and advises how
often the desk should check the market right now (the "pace"). Without a key,
a simple keyword list is used and the pace stays at the normal setting.
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
# Names used to match headlines in keyword mode. Tickers that are ordinary English words
# (near, link, ton, apt, uni, dot, render, algo...) are matched by their project name only.
COIN_NAMES = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth", "ether"], "SOL": ["solana", "sol"],
    "BNB": ["bnb", "binance coin"], "XRP": ["xrp", "ripple"], "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink"],
    "DOT": ["polkadot"], "TRX": ["tron", "trx"], "TON": ["toncoin"], "LTC": ["litecoin", "ltc"],
    "BCH": ["bitcoin cash", "bch"], "NEAR": ["near protocol"], "SUI": ["sui"], "APT": ["aptos"],
    "ARB": ["arbitrum"], "OP": ["optimism network", "op mainnet"], "UNI": ["uniswap"], "AAVE": ["aave"],
    "ATOM": ["cosmos hub", "atom"], "XLM": ["stellar", "xlm"], "HBAR": ["hedera", "hbar"], "PEPE": ["pepe"],
    "SHIB": ["shiba inu", "shib"], "WLD": ["worldcoin", "wld"], "TAO": ["bittensor"], "ZEC": ["zcash", "zec"],
    "INJ": ["injective"], "ENA": ["ethena"], "ONDO": ["ondo"], "HYPE": ["hyperliquid"],
    "FIL": ["filecoin"], "ICP": ["internet computer", "icp"], "TIA": ["celestia"], "SEI": ["sei network"],
    "ETC": ["ethereum classic"], "RENDER": ["render network"], "FET": ["fetch.ai"], "JUP": ["jupiter exchange"],
    "POL": ["polygon"], "ALGO": ["algorand"], "VET": ["vechain"], "XMR": ["monero", "xmr"],
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
        "check_every_minutes": {"type": "integer", "description": "how often to re-check the market, 5-60"},
        "pace_reason": {"type": "string"},
    },
    "required": ["coins", "market_mood", "check_every_minutes", "pace_reason"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a crypto news analyst on a trading desk. Be calibrated: most news is noise; reserve scores "
    "beyond +/-0.6 for genuinely market-moving events. You also set the desk's pace: the normal check interval "
    "is 15 minutes. Suggest 5-10 minutes when market-moving news is breaking, a major scheduled event is about "
    "to happen (central bank decision, inflation data, big token unlock), or prices are moving unusually fast. "
    "Suggest 30-60 minutes only when markets are calm and nothing important is scheduled."
)


class NewsAgent:
    name = "news"

    def __init__(self, llm: ClaudeJSON | None = None, offline: bool = False, cache_seconds: int = 1800):
        self.llm = llm
        self.offline = offline
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict, set] | None = None

    def run(self, coins: list[str], snapshot: dict | None = None) -> dict:
        """Returns {"BTC": {"score": .., "summary": .., "headlines": [..]}, ..., "_mood": "..", "_pace": {..}|None}"""
        if (self._cache and time.time() - self._cache[0] < self.cache_seconds
                and set(coins) <= self._cache[2]):
            return self._cache[1]
        headlines = [] if self.offline else self.fetch_headlines()
        result = None
        if headlines and self.llm:
            result = self._claude_sentiment(headlines, coins, snapshot or {})
        if result is None:
            result = self.keyword_sentiment(headlines, coins)
        self._cache = (time.time(), result, set(coins))
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
        c = coin.lower()
        names = COIN_NAMES.get(coin) or [f"{c} token", f"{c} coin", f"${c}"]  # unknown tickers may be plain words
        return any(f" {n} " in words or f" {n}'" in words or f" {n}," in words for n in names)

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
        result["_pace"] = None
        return result

    def _claude_sentiment(self, headlines: list[str], coins: list[str], snapshot: dict) -> dict | None:
        market = "\n".join(
            f"- {c}: 1h {m['change_1h_pct']:+.2f}%, 24h {m['change_24h_pct']:+.2f}%, typical hourly move "
            f"{m['volatility_pct']:.2f}%{', we hold it' if m['holding'] else ''}" for c, m in snapshot.items())
        prompt = (
            f"Coins: {', '.join(coins)}\n\n"
            + (f"Market right now:\n{market}\n\n" if market else "")
            + "Latest crypto news headlines:\n" + "\n".join(f"- {h}" for h in headlines[:75])
            + "\n\nFor each coin, rate the likely short-term (next 24h) price impact of this news from -1 "
              "(very bearish) to +1 (very bullish). Market-wide news (regulation, macro, ETFs, big hacks) affects "
              "all coins. Use 0 when there is no meaningful news. Give a one-sentence summary per coin. "
              "Then set how often the desk should check the market for the next half hour, with a short reason."
        )
        data = self.llm.ask(SYSTEM, prompt, SENTIMENT_SCHEMA, effort="low")
        if not data:
            return None
        result: dict = {"_mood": data["market_mood"],
                        "_pace": {"minutes": int(data["check_every_minutes"]), "reason": data["pace_reason"]}}
        by_coin = {c["coin"].upper(): c for c in data["coins"]}
        for coin in coins:
            c = by_coin.get(coin, {"score": 0, "summary": "no coverage"})
            result[coin] = {"score": max(-1.0, min(1.0, float(c["score"]))), "summary": c["summary"],
                            "headlines": [h for h in headlines if self.mentions(h, coin)][:5]}
        return result
