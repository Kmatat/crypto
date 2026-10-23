"""Prediction agent: estimates the next-period direction from recent price behaviour.

Deliberately simple and transparent: a least-squares trend line over the last N
closes, scaled by volatility, then squashed into a probability. Treat it as one
vote among several, not an oracle.
"""
from __future__ import annotations

import math


class PredictionAgent:
    name = "prediction"

    def __init__(self, lookback: int = 24):
        self.lookback = lookback

    def run(self, candles: list[dict]) -> dict:
        closes = [c["close"] for c in candles[-self.lookback:]]
        n = len(closes)
        xs = range(n)
        x_mean, y_mean = (n - 1) / 2, sum(closes) / n
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, closes)) / sum((x - x_mean) ** 2 for x in xs)

        returns = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
        vol = (sum(r * r for r in returns) / len(returns)) ** 0.5 or 1e-9
        expected_return = slope / closes[-1]          # per candle
        z = expected_return / vol                     # trend strength vs noise
        prob_up = 1 / (1 + math.exp(-3 * z))

        return {
            "expected_return_pct": round(expected_return * 100, 3),
            "volatility_pct": round(vol * 100, 3),
            "prob_up": round(prob_up, 3),
            "forecast_price": closes[-1] * (1 + expected_return),
            "score": round(2 * prob_up - 1, 3),
        }
