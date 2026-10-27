"""Market Data agent: collects price candles for each crypto pair."""
from __future__ import annotations

import math
import random
import time
import zlib

from ..binance_client import BinanceClient

_SEED_PRICES = {"BTCUSDT": 65_000.0, "ETHUSDT": 3_200.0, "SOLUSDT": 150.0, "BNBUSDT": 580.0}


class MarketDataAgent:
    name = "market"

    def __init__(self, client: BinanceClient | None, interval: str = "1h", offline: bool = False):
        self.client = client
        self.interval = interval
        self.offline = offline
        self._synthetic: dict[str, list[dict]] = {}
        self._daily: dict[str, tuple[float, list[float]]] = {}
        self._daily_candles: dict[tuple[str, int], tuple[float, list[dict]]] = {}
        self._rng: dict[str, random.Random] = {}

    def candles(self, symbol: str, limit: int = 200) -> list[dict]:
        if self.offline or self.client is None:
            return self._synthetic_candles(symbol, limit)
        return self.client.klines(symbol, self.interval, limit)

    def closed(self, candles: list[dict]) -> list[dict]:
        """Binance returns the still-forming candle as the last row. Indicators and decisions use finished
        candles only, exactly as the backtester and the laboratory do (otherwise the live desk would be an
        unmeasured, more trade-happy variant of the strategy). Offline synthetic candles are all finished."""
        if self.offline or self.client is None or len(candles) < 2:
            return candles
        return candles[:-1]

    def daily_closes(self, symbol: str, limit: int = 260) -> list[float]:
        """Daily closing prices (for the 50- and 200-day averages). Cached for an hour; offline: none."""
        if self.offline or self.client is None:
            return []
        cached = self._daily.get(symbol)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        closes = [c["close"] for c in self.client.klines(symbol, "1d", limit)]
        self._daily[symbol] = (time.time(), closes)
        return closes

    def daily_candles(self, symbol: str, limit: int = 130) -> list[dict]:
        """Daily candles, the newest (still forming) day last, for the daily trend strategy. Cached for an hour.
        Offline: a deterministic synthetic series, so the trend mode can be tried without a network."""
        if self.offline or self.client is None:
            return self._synthetic_daily(symbol, limit)
        cached = self._daily_candles.get((symbol, limit))
        # never across midnight UTC: a day read before its close must not pass for the finished day afterwards
        if cached and time.time() - cached[0] < 3600 and int(cached[0] // 86400) == int(time.time() // 86400):
            return cached[1]
        rows = self.client.klines(symbol, "1d", limit)
        self._daily_candles[(symbol, limit)] = (time.time(), rows)
        return rows

    def price(self, symbol: str) -> float:
        """The live price (offline: the newest synthetic price)."""
        if self.offline or self.client is None:
            return self.candles(symbol)[-1]["close"]
        return float(self.client.price(symbol))

    def _synthetic_daily(self, symbol: str, limit: int) -> list[dict]:
        rng = random.Random(zlib.crc32(b"daily-" + symbol.encode()))
        day = 86_400_000
        today = int(time.time() * 1000) // day * day
        price, out = _SEED_PRICES.get(symbol, 100.0), []
        for i in range(limit):
            close = price * (1 + 0.004 * math.sin(i / 20) + rng.gauss(0, 0.03))
            out.append({"time": today - (limit - 1 - i) * day, "open": price, "high": max(price, close) * 1.01,
                        "low": min(price, close) * 0.99, "close": close, "volume": rng.uniform(1000, 5000)})
            price = close
        return out

    def range_since(self, symbol: str, since_ms: int) -> tuple[float, float, int] | None:
        """(lowest, highest, time of the last minute) traded since `since_ms` - the price path
        between two checks, used to emulate exchange-side stop orders in paper trading."""
        if self.offline or self.client is None:
            series = self._synthetic.get(symbol)
            if not series:
                return None
            last = series[-1]  # offline: the newest synthetic candle is "what happened since the last check"
            return last["low"], last["high"], last["time"]
        rows = self.client.klines(symbol, "1m", limit=1000, start_ms=since_ms)
        if not rows:
            return None
        return min(r["low"] for r in rows), max(r["high"] for r in rows), rows[-1]["time"]

    def _synthetic_candles(self, symbol: str, limit: int) -> list[dict]:
        """Random-walk prices for offline testing. Each call advances one candle."""
        series = self._synthetic.get(symbol)
        # one deterministic generator per coin: offline runs and tests repeat exactly
        rng = self._rng.setdefault(symbol, random.Random(zlib.crc32(symbol.encode())))
        if series is None:
            price, series = _SEED_PRICES.get(symbol, 100.0), []
            now = int(time.time() * 1000)
            for i in range(limit):
                series.append(self._next(price, now - (limit - i) * 3_600_000, rng, i))
                price = series[-1]["close"]
            self._synthetic[symbol] = series
        else:
            last = series[-1]
            series.append(self._next(last["close"], last["time"] + 3_600_000, rng, len(series)))
        return series[-limit:]

    @staticmethod
    def _next(price: float, t: int, rng: random.Random, i: int) -> dict:
        drift = 0.002 * math.sin(i / 15)  # gentle trending regimes so signals appear
        close = price * (1 + drift + rng.gauss(0, 0.01))
        high = max(price, close) * (1 + abs(rng.gauss(0, 0.004)))
        low = min(price, close) * (1 - abs(rng.gauss(0, 0.004)))
        return {"time": t, "open": price, "high": high, "low": low, "close": close, "volume": rng.uniform(100, 1000)}
