"""Technical Analysis agent: turns candles into standard indicators and a score.

score is in [-1, +1]: positive = bullish, negative = bearish.
"""
from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    j = 1 - k
    prev = values[0]
    out = [prev]
    append = out.append
    for v in values[1:]:
        prev = v * k + prev * j
        append(prev)
    return out


def ema_last(values: list[float], period: int) -> float:
    """The last value of ema(values, period), without building the series (same arithmetic, same result)."""
    k = 2 / (period + 1)
    j = 1 - k
    prev = values[0]
    for v in values[1:]:
        prev = v * k + prev * j
    return prev


def rsi(closes: list[float], period: int = 14) -> float:
    # the first `period` changes are averaged simply, the rest with Wilder smoothing; identical to summing
    # max(b - a, 0) and max(a - b, 0) lists, but without building them
    avg_g = avg_l = 0.0
    n = len(closes)
    for i in range(1, min(period, n - 1) + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            avg_g += d
        elif d < 0:
            avg_l += -d
    avg_g /= period
    avg_l /= period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(candles: list[dict], period: int = 14) -> float:
    recent = candles[-(period + 1):]  # only the last `period` true ranges are needed
    trs = []
    for prev, c in zip(recent[:-1], recent[1:]):
        trs.append(max(c["high"] - c["low"], abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"])))
    return sum(trs) / len(trs)


def bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> tuple[float, float, float]:
    window = closes[-period:]
    mid = sum(window) / period
    sd = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return mid - k * sd, mid, mid + k * sd


class TechnicalAnalysisAgent:
    name = "technical"

    def run(self, candles: list[dict]) -> dict:
        closes = [c["close"] for c in candles]
        price = closes[-1]
        ema_fast, ema_slow = ema(closes, 12), ema(closes, 26)
        macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
        macd_hist = macd_line[-1] - ema_last(macd_line, 9)
        r = rsi(closes)
        lo, mid, hi = bollinger(closes)
        ema50 = ema_last(closes, 50)

        votes = []
        votes.append(1 if ema_fast[-1] > ema_slow[-1] else -1)          # short-term trend
        votes.append(1 if price > ema50 else -1)                         # medium-term trend
        votes.append(1 if macd_hist > 0 else -1)                         # momentum
        votes.append(1 if r < 30 else -1 if r > 70 else 0)               # oversold / overbought
        votes.append(1 if price < lo else -1 if price > hi else 0)       # band extremes
        score = sum(votes) / len(votes)

        return {
            "price": price,
            "rsi": round(r, 2),
            "macd_hist": macd_hist,
            "ema12": ema_fast[-1],
            "ema26": ema_slow[-1],
            "ema50": ema50,
            "bb_low": lo, "bb_mid": mid, "bb_high": hi,
            "atr": atr(candles),
            "score": round(score, 3),
        }
