"""Market Data agent: collects price candles for each crypto pair."""
from __future__ import annotations

import math
import random
import time

from ..binance_client import BinanceClient

_SEED_PRICES = {"BTCUSDT": 65_000.0, "ETHUSDT": 3_200.0, "SOLUSDT": 150.0, "BNBUSDT": 580.0}


class MarketDataAgent:
    name = "market"

    def __init__(self, client: BinanceClient | None, interval: str = "1h", offline: bool = False):
        self.client = client
        self.interval = interval
        self.offline = offline
        self._synthetic: dict[str, list[dict]] = {}

    def candles(self, symbol: str, limit: int = 200) -> list[dict]:
        if self.offline or self.client is None:
            return self._synthetic_candles(symbol, limit)
        return self.client.klines(symbol, self.interval, limit)

    def _synthetic_candles(self, symbol: str, limit: int) -> list[dict]:
        """Random-walk prices for offline testing. Each call advances one candle."""
        series = self._synthetic.get(symbol)
        rng = random.Random(hash(symbol) & 0xFFFF)
        if series is None:
            price, series = _SEED_PRICES.get(symbol, 100.0), []
            now = int(time.time() * 1000)
            for i in range(limit):
                series.append(self._next(price, now - (limit - i) * 3_600_000, rng, i))
                price = series[-1]["close"]
            self._synthetic[symbol] = series
        else:
            last = series[-1]
            series.append(self._next(last["close"], last["time"] + 3_600_000, random.Random(), len(series)))
        return series[-limit:]

    @staticmethod
    def _next(price: float, t: int, rng: random.Random, i: int) -> dict:
        drift = 0.002 * math.sin(i / 15)  # gentle trending regimes so signals appear
        close = price * (1 + drift + rng.gauss(0, 0.01))
        high = max(price, close) * (1 + abs(rng.gauss(0, 0.004)))
        low = min(price, close) * (1 - abs(rng.gauss(0, 0.004)))
        return {"time": t, "open": price, "high": high, "low": low, "close": close, "volume": rng.uniform(100, 1000)}
