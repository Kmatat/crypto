import math

import pytest

from trader.agents import decision as dmod
from trader.backtest import WARMUP, Backtest
from trader.config import Settings


def synthetic(symbol: str, hours: int, seed: int, drift: float = 0.0) -> list[dict]:
    """Deterministic price path with a gentle wave so trend signals appear; time steps are hourly."""
    price, out = 100.0 * (1 + seed / 10), []
    for i in range(hours):
        move = drift + 0.004 * math.sin(i / 20 + seed) + 0.002 * math.cos(i / 7 + seed * 3)
        close = price * (1 + move)
        out.append({"time": i * 3_600_000, "open": price, "high": max(price, close) * 1.004,
                    "low": min(price, close) * 0.996, "close": close, "volume": 100.0})
        price = close
    return out


def test_backtest_produces_consistent_metrics():
    history = {"AAAUSDT": synthetic("AAAUSDT", 1200, 1), "BBBUSDT": synthetic("BBBUSDT", 1200, 2)}
    s = Settings(offline=True, symbols=list(history), starting_balance=10_000)
    r = Backtest(s, history).run()
    assert r["days"] == round((1200 - WARMUP - 1) / 24)
    assert r["trades"] > 0 and sum(r["exits"].values()) == r["trades"]
    assert r["decisions"]["BUY"] + r["decisions"]["SELL"] + r["decisions"]["HOLD"] == 2 * (1200 - WARMUP)
    assert r["final_balance"] == pytest.approx(10_000 * (1 + r["return_pct"] / 100), abs=1.0)  # both are rounded
    assert 0 <= r["max_drawdown_pct"] < 100 and set(r["buy_and_hold_pct"]) == {"AAA", "BBB"}
    if r["win_rate_pct"] is not None and r["trades"]:
        assert 0 <= r["win_rate_pct"] <= 100


def test_backtest_respects_risk_limits_and_thresholds():
    history = {"AAAUSDT": synthetic("AAAUSDT", 1000, 3), "BBBUSDT": synthetic("BBBUSDT", 1000, 4),
               "CCCUSDT": synthetic("CCCUSDT", 1000, 5)}
    strict = Settings(offline=True, symbols=list(history), max_open_positions=1, max_position_pct=0.05)
    loose = Settings(offline=True, symbols=list(history), max_open_positions=3, max_position_pct=0.10)
    r_strict, r_loose = Backtest(strict, history).run(), Backtest(loose, history).run()
    assert r_strict["max_drawdown_pct"] <= r_loose["max_drawdown_pct"] + 1e-9  # smaller bets, smaller swings

    old = dmod.BUY_THRESHOLD
    try:
        dmod.BUY_THRESHOLD = 0.99  # unreachable: the rules never buy
        assert Backtest(loose, history).run()["trades"] == 0
    finally:
        dmod.BUY_THRESHOLD = old


def test_backtest_is_deterministic():
    history = {"AAAUSDT": synthetic("AAAUSDT", 800, 7)}
    s = Settings(offline=True, symbols=["AAAUSDT"])
    assert Backtest(s, history).run() == Backtest(s, history).run()


def test_keep_curve_changes_nothing():
    """1.9.0: the strategy library's opt-in equity curve leaves the replay's own result exactly as it was."""
    history = {"AAAUSDT": synthetic("AAAUSDT", 900, 1), "BBBUSDT": synthetic("BBBUSDT", 900, 2)}
    s = Settings(offline=True, symbols=list(history), starting_balance=10_000)
    plain = Backtest(s, history).run()
    kept = Backtest(s, history, keep_curve=True).run()
    curve, closed = kept.pop("curve"), kept.pop("closed")
    assert kept == plain and "curve" not in plain
    assert len(curve) == 900 - WARMUP and len(closed) == plain["trades"]      # one point per replayed hour
    assert curve[-1][1] == pytest.approx(plain["final_balance"], abs=0.01)
    assert all(x[3] in ("stop-loss", "take-profit", "signal") for x in closed)
    assert sum(x[4] for x in closed) == pytest.approx(plain["fees_paid"], abs=0.01)
    empty = Backtest(s, {k: v[:100] for k, v in history.items()}, keep_curve=True).run()
    assert empty["curve"] == [] and empty["closed"] == []
