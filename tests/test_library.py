"""1.9.0: the strategy library: published rules, one simulator, one strict protocol, honest texts, and nothing that
reaches a trading decision. Synthetic prices only (fast enough for the small server's update job)."""
import ast
import datetime as dt
import fcntl
import gzip
import json
import math
import subprocess
import threading
import time
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader import __main__ as main
from trader import library as L
from trader import library_data as D
from trader import library_report as R
from trader import library_stats as st
from trader import trend
from trader.agents import technical
from trader.config import Settings
from trader.journal import Journal
from trader.library_data import Series
from trader.notes import Notes
from trader.orchestrator import Orchestrator

DAY = D.DAY_MS
END = (int(time.time() * 1000) // DAY - 2) * DAY      # a finished day, so the cache keeps every synthetic candle
ROOT = Path(__file__).resolve().parent.parent


# ---- synthetic prices ----
def bars(closes, start_day=0, spread=0.01, symbol="BTCUSDT", opens=None, lows=None, highs=None, t0=None):
    """A daily Series: each day opens at the previous close (or `opens`), high/low `spread` around the body."""
    t0 = END - (len(closes) - 1 + start_day) * DAY if t0 is None else t0
    rows = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else (closes[i - 1] if i else c)
        h = highs[i] if highs else max(o, c) * (1 + spread)
        lo = lows[i] if lows else min(o, c) * (1 - spread)
        rows.append((t0 + (start_day + i) * DAY, o, h, lo, c, 1.0))
    return Series.from_rows(symbol, "1d", rows)


def wave(n, drift=0.0015, swing=0.04, seed=0):
    """Deterministic up-and-down path with trends long and short."""
    return [100 * (1 + drift) ** i * (1 + swing * math.sin(i / 9 + seed) + 0.5 * swing * math.sin(i / 2.3 + 3 * seed)
                                       + 0.3 * swing * math.sin(i * 1.7 + seed)) for i in range(n)]


def four_hour(daily: Series) -> Series:
    """Six 4-hour bars per synthetic day, wandering from the day's open to its close."""
    rows = []
    for i in range(len(daily)):
        o, c = daily.open[i], daily.close[i]
        prev = o
        for k in range(6):
            x = o + (c - o) * (k + 1) / 6 + (c - o) * 0.3 * math.sin(i * 6 + k)
            rows.append((daily.time[i] + k * D.INTERVAL_MS["4h"], prev, max(prev, x) * 1.003, min(prev, x) * 0.997, x, 1.0))
            prev = x
    return Series.from_rows(daily.symbol, "4h", rows)


def dataset(n=600, coins=("BTCUSDT", "ETHUSDT", "SOLUSDT"), late=120, end=END, hourly=False):
    """{"1d": ..., "4h": ..., "1h": ...}: the first two coins from day 0, the third listed `late` days later."""
    out = {"1d": {}, "4h": {}, "1h": {}}
    for k, sym in enumerate(coins):
        length = n - (late if k == 2 else 0)
        d = bars(wave(length, seed=k, drift=0.001 + 0.0006 * k), symbol=sym, spread=0.02,
                 t0=end - (length - 1) * DAY)
        out["1d"][sym], out["4h"][sym] = d, four_hour(d)
    return out


@pytest.fixture
def small(monkeypatch):
    """Walk-forward 120/60/30 days and a 60-day warm-up, so a whole protocol runs in a fraction of a second."""
    monkeypatch.setattr(st, "WF_IN_DAYS", 120)
    monkeypatch.setattr(st, "WF_OUT_DAYS", 60)
    monkeypatch.setattr(st, "WF_MIN_LAST_DAYS", 30)
    monkeypatch.setattr(L, "WARMUP_DAYS", 60)


def family(**kw):
    """A test rule built on the library's Family."""
    base = dict(key="test", name="Test rule", short="Test", cite="test", timeframe="1d", kind="state", source="s",
                url="https://example.org", rule="r", grid=({},), prepare=lambda s, p: {"_p": p},
                enter=lambda s, c, i: None, leave=lambda s, c, i: False)
    base.update(kw)
    return L.Family(**base)


def run1(fam, series: dict, start, end, params=None, joins=None):
    params = params if params is not None else fam.grid[0]
    joins = joins or {sym: s.time[0] for sym, s in series.items()}
    prepared = {sym: fam.prepare(s, params) for sym, s in series.items()}
    return L.simulate(fam, params, series, prepared, joins, start, end)


# ---- 1-11: the families' signal rules ----
def donchian_path(gap=None):
    closes = [100.0] * 60 + [110.0] + [130.0] * 12 + [120.0] * 4
    opens = None
    if gap:
        day, open_, close = gap
        closes[day] = close
        opens = [closes[i - 1] if i else closes[0] for i in range(len(closes))]
        opens[day] = open_
    return bars(closes, spread=0.05, opens=opens)


def test_donchian_buys_a_close_above_the_channel_at_the_next_open_and_sells_below_the_exit_low():
    s = donchian_path()
    run = run1(L.DONCHIAN, {"BTCUSDT": s}, s.time[30], s.time[-1])
    first = run["trades"][0]
    assert first["entry_ms"] == s.time[61] and first["exit_ms"] == s.time[74] and first["reason"] == "signal"
    n60 = (19 * 10 + 20.5) / 20                      # the Turtles' N on the breakout day
    quote = 10_000 * L.RISK_PER_TRADE / (2 * n60 / 110)
    qty = quote * 0.999 / (110 * 1.0005)
    assert first["pnl"] == pytest.approx(qty * 120 * 0.9995 * 0.999 - quote, rel=1e-9)
    assert run["orders"][0][:3] == (s.time[60], "BTCUSDT", "buy") and run["orders"][0][4] == pytest.approx(110 - 2 * n60)


def test_donchian_stop_is_2n_below_and_fills_at_the_stop_or_a_lower_open_less_slippage():
    gapped = donchian_path(gap=(63, 70.0, 75.0))         # opens far below the stop
    run = run1(L.DONCHIAN, {"BTCUSDT": gapped}, gapped.time[30], gapped.time[-1])
    t = run["trades"][0]
    stop = 110 - 2 * (19 * 10 + 20.5) / 20
    assert t["reason"] == "stop-loss" and t["exit_ms"] == gapped.time[63]
    qty = run["orders"][0][3] * 0.999 / (110 * 1.0005)
    assert t["pnl"] == pytest.approx(qty * 70 * 0.998 * 0.999 - run["orders"][0][3], rel=1e-9)
    touched = donchian_path(gap=(63, 100.0, 85.0))        # opens above the stop, trades through it
    t = run1(L.DONCHIAN, {"BTCUSDT": touched}, touched.time[30], touched.time[-1])["trades"][0]
    assert t["reason"] == "stop-loss" and t["pnl"] == pytest.approx(qty * stop * 0.998 * 0.999 - run["orders"][0][3],
                                                                    rel=1e-9)


def test_donchian_sizes_one_percent_risk_capped_at_ten_percent():
    calm = bars([100.0] * 60 + [101.5], spread=0.001)
    c = L.DONCHIAN.prepare(calm, {"entry": 20, "exit": 10})
    assert L.DONCHIAN.enter(calm, c, 60)["share"] == L.SLOT_SHARE          # a tight stop: the 10% cap binds
    wild = donchian_path()
    c = L.DONCHIAN.prepare(wild, {"entry": 20, "exit": 10})
    d = L.DONCHIAN.enter(wild, c, 60)
    assert d["share"] == pytest.approx(0.01 / ((110 - d["stop"]) / 110)) and d["share"] < L.SLOT_SHARE
    assert L.DONCHIAN.enter(wild, c, 59) is None                            # no breakout, no entry


def test_tsmom_holds_only_while_the_lookback_return_is_positive_and_scales_down_wild_coins():
    s = bars(wave(300, swing=0.08))
    p = {"lookback": 28}
    c = L.TSMOM.prepare(s, p)
    for i in range(40, 300):
        up = s.close[i] / s.close[i - 28] - 1 > 0
        d = L.TSMOM.enter(s, c, i)
        assert (d is not None) == up and L.TSMOM.leave(s, c, i) == (not up)
        if d:
            assert d["share"] == trend.share({"vol": L.vol30(s.close, i)}, 8, 0.5, 0.10)
    assert L.TSMOM.enter(s, c, 20) is None and L.TSMOM.leave(s, c, 20) is False     # not defined yet: no decision
    assert trend.share({"vol": 0.5}, 8, 0.5, 0.10) == 0.10
    assert trend.share({"vol": 1.0}, 8, 0.5, 0.10) == 0.0625
    assert trend.share({"vol": 2.0}, 8, 0.5, 0.10) == 0.03125


def test_sma_cross_is_long_while_the_fast_average_is_above_the_slow():
    s = bars(wave(400))
    for p in L.SMA_CROSS.grid:
        c = L.SMA_CROSS.prepare(s, p)
        for i in range(p["slow"], 400, 7):
            fast = sum(s.close[i - p["fast"] + 1:i + 1]) / p["fast"]
            slow = sum(s.close[i - p["slow"] + 1:i + 1]) / p["slow"]
            assert (L.SMA_CROSS.enter(s, c, i) is not None) == (fast > slow)
            assert L.SMA_CROSS.leave(s, c, i) == (fast <= slow)
    c = L.SMA_CROSS.prepare(s, {"fast": 1, "slow": 50})
    assert list(c["fast"]) == list(s.close)                    # a 1-day average is the close itself


def test_rsi2_buys_a_dip_only_above_the_200_day_average_and_sells_above_the_5_day_average():
    up = [100 * 1.003 ** i for i in range(230)] + [100 * 1.003 ** 229 * 0.97, 100 * 1.003 ** 229 * 0.95]
    s = bars(up)
    c = L.RSI2.prepare(s, {"below": 10})
    assert L.RSI2.enter(s, c, 231) is not None and c["rsi"][231] < 10 and s.close[231] > c["trend"][231]
    assert L.RSI2.enter(s, c, 229) is None                     # no dip
    down = bars([100 * 0.997 ** i for i in range(230)] + [100 * 0.997 ** 229 * 0.95])
    cd = L.RSI2.prepare(down, {"below": 10})
    assert cd["rsi"][230] < 10 and L.RSI2.enter(down, cd, 230) is None    # a dip below the 200-day average: no buy
    assert L.RSI2.leave(s, c, 229) and not L.RSI2.leave(s, c, 231)      # sells above the 5-day average only


def test_bollinger_buys_below_the_lower_band_in_an_uptrend_and_exits_at_the_middle_band():
    up = [100 * 1.002 ** i * (1 + 0.01 * math.sin(i)) for i in range(230)]
    up += [up[-1] * 0.93, up[-1] * 1.02]
    s = bars(up)
    c = L.BOLLINGER.prepare(s, {"period": 20, "width": 2.0})
    lo, mid, _ = technical.bollinger(up[211:231], 20, 2.0)
    assert c["lower"][230] == pytest.approx(lo) and c["mid"][230] == pytest.approx(mid)
    assert L.BOLLINGER.enter(s, c, 230) is not None and L.BOLLINGER.enter(s, c, 229) is None
    assert L.BOLLINGER.leave(s, c, 231) == (up[231] > c["mid"][231])
    down = [100 * 0.998 ** i * (1 + 0.01 * math.sin(i)) for i in range(230)]
    down += [down[-1] * 0.9]
    sd = bars(down)
    cd = L.BOLLINGER.prepare(sd, {"period": 20, "width": 2.0})
    assert sd.close[230] < cd["lower"][230] and L.BOLLINGER.enter(sd, cd, 230) is None   # below the 200-day: no buy


def test_macd_4h_is_long_while_the_line_is_above_its_signal_and_the_zero_filter_needs_it_above_zero():
    s = four_hour(bars(wave(200, swing=0.06)))
    for p in L.MACD_4H.grid:
        c = L.MACD_4H.prepare(s, p)
        line = [a - b for a, b in zip(technical.ema(list(s.close), p["fast"]), technical.ema(list(s.close), p["slow"]))]
        sig = technical.ema(line, p["signal"])
        seen_zero_block = False
        for i in range(100, len(s), 5):
            on = line[i] > sig[i] and (line[i] > 0 or not p["zero"])
            assert (L.MACD_4H.enter(s, c, i) is not None) == on and L.MACD_4H.leave(s, c, i) == (not on)
            seen_zero_block |= p["zero"] and line[i] > sig[i] and line[i] <= 0
        assert seen_zero_block or not p["zero"]
    assert L.MACD_4H.timeframe == "4h"


def test_williams_buys_at_open_plus_k_range_and_sells_at_the_close():
    closes = [100.0] * 30
    highs = [101.0] * 30
    lows = [99.0] * 30
    opens = [100.0] * 30
    highs[20], closes[20] = 103.0, 102.0                       # level for day 20 = 100 + 0.5 x 2 = 101: filled
    s = bars(closes, opens=opens, highs=highs, lows=lows)
    run = run1(L.WILLIAMS, {"BTCUSDT": s}, s.time[10], s.time[-1], {"k": 0.5})
    assert len(run["trades"]) == 19      # every day the high reaches open + half a range, except after the wide day
    t = [x for x in run["trades"] if x["exit_ms"] == s.time[20]][0]
    quote = run["orders"][[o[0] for o in run["orders"]].index(s.time[20])][3]
    qty = quote * 0.999 / (101.0 * 1.002)
    assert t["reason"] == "close" and t["pnl"] == pytest.approx(qty * 102 * 0.9995 * 0.999 - quote, rel=1e-9)
    none = run1(L.WILLIAMS, {"BTCUSDT": s}, s.time[10], s.time[-1], {"k": 1.0})   # level 102 > high 101
    assert [x["exit_ms"] for x in none["trades"]] == [s.time[20]]                   # only the wide day
    assert len(run["orders"]) == len(run["trades"]) and all(o[0] == t["entry_ms"] for o, t in zip(run["orders"],
                                                                                                   run["trades"]))


def test_indicators_match_the_desk_s_own():
    close = array("d", wave(300, swing=0.07))
    r = L.rsi(close, 2)
    for i in range(2, 300):
        assert r[i] == pytest.approx(technical.rsi(list(close[:i + 1]), 2), abs=1e-12)
    assert math.isnan(r[1])
    assert list(L.ema(close, 12)) == technical.ema(list(close), 12)
    lower, mid = L.bands(close, 20, 2.0)
    for i in range(19, 300, 11):
        lo, m, _ = technical.bollinger(list(close[i - 19:i + 1]), 20, 2.0)
        assert lower[i] == pytest.approx(lo, rel=1e-12) and mid[i] == pytest.approx(m, rel=1e-12)
    s = bars(list(close), spread=0.02)
    n = L.turtle_n(s, 20)
    tr = [max(s.high[i] - s.low[i], abs(s.high[i] - s.close[i - 1]), abs(s.low[i] - s.close[i - 1])) for i in range(1, 300)]
    expect = sum(tr[:20]) / 20
    assert n[20] == pytest.approx(expect)
    for i in range(21, 300):
        expect = (19 * expect + tr[i - 1]) / 20
        assert n[i] == pytest.approx(expect)
    assert math.isnan(n[19])
    sm = L.sma(close, 50)
    assert sm[120] == pytest.approx(sum(close[71:121]) / 50, rel=1e-12) and math.isnan(sm[48])
    hi, lo = L.prior_max(s.high, 20), L.prior_min(s.low, 10)
    assert hi[100] == max(s.high[80:100]) and lo[100] == min(s.low[90:100]) and math.isnan(hi[19])


HEADLINE = {"donchian": {"entry": 20, "exit": 10}, "tsmom": {"lookback": 28}, "sma_cross": {"fast": 1, "slow": 50},
            "rsi2": {"below": 10}, "bollinger": {"period": 20, "width": 2.0},
            "macd_4h": {"fast": 12, "slow": 26, "signal": 9, "zero": False}, "williams": {"k": 0.5}}


def test_grids_and_sources_are_complete():
    assert len(L.FAMILIES) == 7 and sum(len(f.grid) for f in L.FAMILIES) == 23
    assert len({f.key for f in L.FAMILIES}) == 7
    for f in L.FAMILIES:
        assert f.source and f.url.startswith("https://") and f.rule.endswith(".") and len(f.rule) > 40 and f.cite
        assert f.grid[0] == HEADLINE[f.key] and f.timeframe in ("1d", "4h") and f.kind in ("state", "event", "intraday")


# ---- 12-16: the simulator ----
def scripted(buy_days=(), sell_days=(), share=0.1):
    """A rule that buys at the close of the given bar indexes and sells at others."""
    return family(enter=lambda s, c, i: {"stop": None, "share": share} if i in buy_days else None,
                  leave=lambda s, c, i: i in sell_days)


def test_one_round_trip_charges_fees_and_slippage_exactly():
    closes = [100.0] * 10 + [110.0] * 10
    s = bars(closes)
    run = run1(scripted(buy_days={4}, sell_days={11}), {"BTCUSDT": s}, s.time[2], s.time[-1])
    (t,) = run["trades"]
    qty = 1000 * 0.999 / (100 * 1.0005)
    proceeds = qty * 110 * 0.9995 * 0.999
    assert t["entry_ms"] == s.time[5] and t["exit_ms"] == s.time[12] and t["reason"] == "signal"
    assert t["pnl"] == pytest.approx(proceeds - 1000, rel=1e-12)
    assert t["fees"] == pytest.approx(1.0 + qty * 110 * 0.9995 * 0.001, rel=1e-12)
    assert run["curve"][-1][1] == pytest.approx(10_000 - 1000 + proceeds, rel=1e-12)


def test_one_slot_per_coin_and_never_more_than_ten_percent():
    coins = {sym: bars([100.0] * 40, symbol=sym) for sym in L.COINS}
    run = run1(family(enter=lambda s, c, i: {"stop": None, "share": 0.5}), coins, coins["BTCUSDT"].time[5],
               coins["BTCUSDT"].time[-1])
    buys = [o for o in run["orders"] if o[2] == "buy"]
    assert len({o[1] for o in buys}) == 8 and len(buys) == 8                 # one position per coin, no adding
    assert {o[3] for o in buys} == {1000.0}                                  # a 50% wish is cut to the 10% cap
    value = 8 * 1000 * 0.999 / 1.0005
    assert run["invested"][0] == pytest.approx(value / (2000 + value), rel=1e-9)
    assert max(run["invested"]) <= 0.8


def test_a_window_starts_flat_and_ends_at_liquidation_value():
    s = bars([100.0] * 10 + [120.0] * 10)
    run = run1(scripted(buy_days={4}), {"BTCUSDT": s}, s.time[5], s.time[-1])
    assert run["curve"][0][0] == s.time[5] and run["base"] == 10_000
    (t,) = run["trades"]
    qty = 1000 * 0.999 / (100 * 1.0005)
    assert t["reason"] == "end of window" and t["exit_ms"] == s.time[-1]
    assert run["curve"][-1][1] == pytest.approx(9_000 + qty * 120 * 0.9995 * 0.999, rel=1e-12)
    assert run["curve"][-2][1] == pytest.approx(9_000 + qty * 120, rel=1e-12)     # marked at the close before
    later = run1(scripted(buy_days={4}), {"BTCUSDT": s}, s.time[10], s.time[-1])
    assert later["trades"] == [] and later["curve"][0][1] == 10_000               # flat at every window's start


def test_a_coin_joins_after_its_warm_up(small):
    data = dataset(300)
    joins = L.joins(data["1d"])
    sol = data["1d"]["SOLUSDT"]
    assert joins["SOLUSDT"] == sol.time[0] + 60 * DAY and joins["BTCUSDT"] == data["1d"]["BTCUSDT"].time[0] + 60 * DAY
    always = family(enter=lambda s, c, i: {"stop": None, "share": 0.1})
    run = L.simulate(always, {}, data["1d"], {k: {} for k in data["1d"]}, joins, joins["BTCUSDT"], END)
    first = min(t["entry_ms"] for t in run["trades"] if t["symbol"] == "SOLUSDT")
    assert first == joins["SOLUSDT"]
    assert all(o[0] + DAY >= joins[o[1]] for o in run["orders"])           # decided the day before at the earliest


def test_yardsticks(small):
    data = dataset(300)
    joins = L.joins(data["1d"])
    a, b = joins["BTCUSDT"], END
    h = L.hold(data["1d"], joins, a, b)
    btc, eth, sol = (data["1d"][x] for x in ("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    i = btc.upto(a) - 1
    q_btc = 1250 * 0.999 / (btc.open[i] * 1.0005)
    j = eth.upto(a) - 1
    q_eth = 1250 * 0.999 / (eth.open[j] * 1.0005)
    assert h["curve"][0][1] == pytest.approx(10_000 - 2500 + q_btc * btc.close[i] + q_eth * eth.close[j], rel=1e-12)
    k = sol.upto(joins["SOLUSDT"]) - 1
    day = (joins["SOLUSDT"] - a) // DAY
    assert h["curve"][day - 1][1] == pytest.approx(10_000 - 2500 + q_btc * btc.close[i + day - 1]
                                                   + q_eth * eth.close[j + day - 1], rel=1e-12)
    q_sol = 1250 * 0.999 / (sol.open[k] * 1.0005)
    assert [t["symbol"] for t in h["trades"]] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert h["trades"][2]["pnl"] == pytest.approx(q_sol * sol.close[-1] * 0.9995 * 0.999 - 1250, rel=1e-9)
    alone = L.hold(data["1d"], joins, a, b, ("BTCUSDT",))
    assert alone["curve"][0][1] == pytest.approx(10_000 * 0.999 / (btc.open[i] * 1.0005) * btc.close[i], rel=1e-12)
    cash = L.cash_run(a, b)
    assert {e for _, e in cash["curve"]} == {10_000} and st.daily_returns(cash, a, b) == [0.0] * ((b - a) // DAY + 1)


# ---- 17-21: no look-ahead ----
def family_bars(fam, data):
    return data["1d"] if fam.timeframe == "1d" else data["4h"]


def same(a, b) -> bool:
    return len(a) == len(b) and all((x == y) or (x != x and y != y) for x, y in zip(a, b))


def test_indicators_are_trailing_prefix_invariant():
    data = dataset(700)
    for fam in L.FAMILIES:
        s = family_bars(fam, data)["BTCUSDT"]
        for p in fam.grid:
            full = fam.prepare(s, p)
            for day in (200, 400, 600):
                cut = s.cut(s.time[0] + day * DAY - 1)
                part = fam.prepare(cut, p)
                for key, col in full.items():
                    if isinstance(col, array):
                        assert same(part[key], col[:len(cut)]), (fam.key, p, key, day)


def scramble(s: Series, first: int) -> Series:
    f = [1.0] * first + [1 + 0.3 * math.sin(i) for i in range(first, len(s))]
    mul = lambda col: array("d", (x * k for x, k in zip(col, f)))   # noqa: E731
    return Series(s.symbol, s.interval, s.time, mul(s.open), mul(s.high), mul(s.low), mul(s.close), s.volume)


def looks_ahead(fam, params, data, joins, start, ks=(300, 450, 600)) -> bool:
    """True when anything known by the close of day k-1 changes after the prices from day k on are changed."""
    src = family_bars(fam, data)
    base = run1(fam, src, start, END, params, joins)
    day0 = data["1d"]["BTCUSDT"].time[0]
    for k in ks:
        tk = day0 + k * DAY
        other = {sym: scramble(s, s.upto(tk - 1)) for sym, s in src.items()}
        alt = run1(fam, other, start, END, params, joins)
        if [o for o in base["orders"] if o[0] < tk] != [o for o in alt["orders"] if o[0] < tk]:
            return True
        if [c for c in base["curve"] if c[0] < tk] != [c for c in alt["curve"] if c[0] < tk]:
            return True
        if [t for t in base["trades"] if t["exit_ms"] < tk] != [t for t in alt["trades"] if t["exit_ms"] < tk]:
            return True
    return False


def test_no_family_looks_ahead(small):
    data = dataset(700, late=0)
    joins = L.joins(data["1d"])
    start = data["1d"]["BTCUSDT"].time[250]
    for fam in L.FAMILIES:
        for p in fam.grid:
            assert not looks_ahead(fam, p, data, joins, start), (fam.key, p)
    # the test itself: a rule that peeks at tomorrow's close is caught
    peek = family(enter=lambda s, c, i: _peek(s, i, 1), leave=lambda s, c, i: bool(_peek(s, i, -1)))
    assert looks_ahead(peek, {}, data, joins, start)


def _peek(s, i, sign):
    if i + 1 >= len(s):
        return None
    return {"stop": None, "share": 0.1} if (s.close[i + 1] - s.close[i]) * sign > 0 else None


N_DAYS = 500                    # the shared small protocol run: 3 coins, 440 days judged, 5 walk-forward segments
_RUN = {}


def spied_run():
    """One small protocol run with library.simulate wrapped: every call's window, the newest candle it was given and
    whether every indicator column was cut to the same length. Shared by the protocol and text tests (the caller
    holds the `small` constants)."""
    if "report" not in _RUN:
        calls, real = [], L.simulate

        def spy(fam, params, bars_, prepared, joins, start_ms, end_ms):
            cols_ok = all(len(v) == len(bars_[sym]) for sym, cols in prepared.items() for v in cols.values()
                          if isinstance(v, array))
            calls.append({"family": fam.key, "params": params, "start": start_ms, "end": end_ms,
                          "newest": max(s.time[-1] for s in bars_.values()), "cols_ok": cols_ok})
            return real(fam, params, bars_, prepared, joins, start_ms, end_ms)
        L.simulate = spy
        try:
            _RUN["report"] = L.run_library(dataset(N_DAYS), "--no-hourly")
        finally:
            L.simulate = real
        _RUN["calls"] = calls
    return json.loads(json.dumps(_RUN["report"])), _RUN["calls"]


def p_of(data):
    daily = data["1d"]
    j = L.joins(daily)
    return st.periods(min(j.values()), min(s.time[-1] for s in daily.values()))


def test_the_choice_never_sees_the_holdout(small):
    P = p_of(dataset(N_DAYS))
    a, calls = spied_run()
    a_in, b_in = P["in_sample"]
    choice_calls = [c for c in calls if c["start"] == a_in and c["end"] == b_in]
    assert len(choice_calls) == 23
    for c in choice_calls:
        assert c["newest"] <= b_in + DAY - 1 and c["cols_ok"]
    # different prices in the judged period: the same in-sample rows, choice and V
    changed = dataset(N_DAYS)
    for key in ("1d", "4h"):
        for sym, s in changed[key].items():
            changed[key][sym] = scramble(s, s.upto(P["holdout"][0] - 1))
    b = L.run_library(changed, "--no-hourly")
    early = [k for k, seg in enumerate(P["segments"]) if seg[1] < P["holdout"][0]]
    assert len(early) >= 3
    for ea, eb in zip(a["entries"], b["entries"]):
        if ea["kind"] == "family":
            assert ea["in_sample_rows"] == eb["in_sample_rows"] and ea["chosen"] == eb["chosen"]
            assert [ea["choices"][k] for k in early] == [eb["choices"][k] for k in early]
    assert a["trial_variance"] == b["trial_variance"]
    assert a["entries"][0]["holdout"] != b["entries"][0]["holdout"]       # the judged period did change


def test_each_walk_forward_choice_sees_only_its_past(small):
    P = p_of(dataset(N_DAYS))
    _, calls = spied_run()
    for ca, cb, ta, tb in P["segments"]:
        seen = [c for c in calls if c["start"] == ca and c["end"] == cb]
        assert len(seen) == 23
        assert all(c["newest"] <= ta - 1 and c["cols_ok"] for c in seen)


def test_the_holdout_is_judged_once(small):
    P = p_of(dataset(N_DAYS))
    report, calls = spied_run()
    spy = SimpleNamespace(calls=calls)
    ha, hb = P["holdout"]
    for e in report["entries"]:
        if e["kind"] != "family":
            continue
        calls = [c for c in spy.calls if c["family"] == e["key"] and c["start"] == ha and c["end"] == hb]
        assert len(calls) == (1 if e["chosen"] else 0)
        if e["chosen"]:
            assert calls[0]["params"] == e["chosen"]["params"]


# ---- 22-33: the protocol and its statistics ----
def ms(text):
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def iso(x):
    return dt.datetime.fromtimestamp(x / 1000, dt.timezone.utc).date().isoformat()


def test_periods_split_70_30_and_walk_forward_segments():
    P = st.periods(ms("2018-03-15"), ms("2026-10-30"))
    assert P["days"] == 3152 and [iso(x) for x in P["in_sample"]] == ["2018-03-15", "2024-03-28"]
    assert [iso(x) for x in P["holdout"]] == ["2024-03-29", "2026-10-30"]
    segs = P["segments"]
    assert len(segs) == 13 and [iso(x) for x in segs[0]] == ["2018-03-15", "2020-03-13", "2020-03-14", "2020-09-11"]
    assert [iso(x) for x in segs[-1][2:]] == ["2026-03-07", "2026-10-30"]       # the 56-day remainder joined it
    assert sum((tb - ta) // DAY + 1 for _, _, ta, tb in segs) == 2422
    for prev, nxt in zip(segs, segs[1:]):
        assert nxt[2] == prev[3] + DAY and nxt[2] - nxt[0] == 730 * DAY and nxt[1] == nxt[2] - DAY
    alone = st.periods(ms("2018-03-15"), ms("2018-03-15") + (730 + 182 * 2 + 91 - 1) * DAY)["segments"]
    assert len(alone) == 3 and (alone[-1][3] - alone[-1][2]) // DAY + 1 == 91   # 91 days or more: its own segment


def row(params, halves, trades):
    return {"params": params, "halves": halves, "trades": trades}


def test_choice_takes_the_best_worse_half_needs_trades_and_breaks_ties_by_grid_order():
    rows = [row("a", [1.0, 0.2], 40), row("b", [0.5, 0.6], 40), row("c", [3.0, 3.0], 5), row("d", [0.6, 0.5], 40)]
    assert st.choose(rows, 2206 / 365)["params"] == "b"          # c has the best halves but too few trades
    assert [r["eligible"] for r in rows] == [True, True, False, True]
    assert st.min_trades(2206) == 31 and st.min_trades(730) == 10
    tie = [row("headline", [0.5, 0.5], 31), row("other", [0.5, 0.9], 31)]
    assert st.choose(tie, 2206 / 365)["params"] == "headline"
    assert st.choose([row("x", [9, 9], 30)], 2206 / 365) is None


def test_a_segment_without_an_eligible_setting_is_cash(small):
    data = dataset(N_DAYS)
    P = p_of(data)
    never = family(grid=({}, {"x": 1}))
    entry = L.run_family(never, data["1d"], data["1d"], L.joins(data["1d"]), P)
    assert entry["chosen"] is None and entry["holdout"] is None
    assert all(c["params"] is None for c in entry["choices"]) and len(entry["choices"]) == len(P["segments"])
    wf = entry["walk_forward"]
    assert wf["return_pct"] == 0 and wf["trades"] == 0 and wf["sharpe"] == 0
    verdict, reasons = st.verdict(entry, {}, 25, 0.0001)
    assert verdict == "FAIL" and reasons[0].startswith("too few trades in the first 70% at every setting")


def test_stitching_chains_segment_returns():
    a = {"curve": [(0, 11_000.0), (DAY, 9_900.0)], "base": 10_000.0, "trades": [{"pnl": 1}], "invested": [0.5, 0.5]}
    b = {"curve": [(2 * DAY, 12_000.0), (3 * DAY, 10_000.0)], "base": 10_000.0, "trades": [{"pnl": 2}],
         "invested": [1.0, 0.0]}
    s = st.stitch([a, b])
    assert s["curve"][-1][1] == pytest.approx(10_000 * 0.99 * 1.0)
    assert s["curve"][1][1] == pytest.approx(9_900) and s["curve"][2][1] == pytest.approx(9_900 * 1.2)
    m = st.metrics(s, 0, 3 * DAY)
    assert m["return_pct"] == pytest.approx((0.99 * 1.0 - 1) * 100, abs=0.01)
    peak = 9_900 * 1.2
    assert m["max_drawdown_pct"] == pytest.approx((peak - 9_900) / peak * 100, abs=0.01)   # across the segments
    assert len(s["trades"]) == 2 and s["invested"] == [0.5, 0.5, 1.0, 0.0] and m["invested_pct"] == 50.0


def test_sharpe_and_moments():
    r = [0.01, -0.02, 0.03, 0.0, 0.015, -0.005]
    mean = sum(r) / 6
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / 5)
    assert st.sharpe(r) == pytest.approx(mean / sd) and st.annual(1.0) == pytest.approx(math.sqrt(365))
    m2 = sum((x - mean) ** 2 for x in r) / 6
    skew, kurt = st.moments(r)
    assert skew == pytest.approx(sum((x - mean) ** 3 for x in r) / 6 / m2 ** 1.5)
    assert kurt == pytest.approx(sum((x - mean) ** 4 for x in r) / 6 / m2 ** 2)
    assert st.sharpe([0.01] * 10) == 0.0 and st.moments([0.01] * 10) == (0.0, 3.0) and st.moments([1, 2]) == (0.0, 3.0)
    zeros = [0.0] * 500
    assert st.dsr(zeros, 25, 0.0004) < 0.5


def test_dsr_reproduces_the_paper():
    V = 0.5 / 250                                     # the paper's annual variance 0.5, in daily terms
    sr = 2.5 / math.sqrt(250)
    assert st.expected_max_sharpe(100, V) == pytest.approx(0.1132, abs=1e-4)
    assert st.expected_max_sharpe(100, V) * math.sqrt(250) == pytest.approx(1.7894, abs=1e-4)
    assert st.psr(sr, st.expected_max_sharpe(100, V), 1250, -3, 10) == pytest.approx(0.9004, abs=1e-4)
    assert st.psr(sr, st.expected_max_sharpe(46, V), 1250, -3, 10) == pytest.approx(0.9505, abs=1e-4)
    assert st.psr(sr, st.expected_max_sharpe(88, V), 1250, 0, 3) == pytest.approx(0.9505, abs=1e-4)


def test_more_variants_mean_a_higher_luck_bar():
    r = [0.004 + 0.03 * math.sin(i * 1.3) for i in range(1000)]
    values = [st.dsr(r, n, 0.0004) for n in (2, 10, 25, 100, 1000)]
    assert values == sorted(values, reverse=True) and values[0] > values[-1]
    assert st.trials() == 25 == sum(len(f.grid) for f in L.FAMILIES) + len(L.REFERENCES) + st.TRIALS_BEFORE
    assert st.expected_max_sharpe(1, 0.0004) == 0.0
    assert st.psr(0.5, 0.0, 100, 10.0, 3.0) == 0.0                   # 1 - skew x SR + ... <= 0: no figure, no error
    assert st.psr(0.1, 0.0, 1, 0.0, 3.0) == 0.0


def test_luck_words():
    assert st.luck_words(0.82) == "about 18 times in 100" and st.luck_in_100(0.82) == 18
    assert st.luck_in_100(0.95) == 5 and st.luck_in_100(0.952) == 5      # a pass shows 5 or fewer
    assert st.luck_in_100(0.9451) == 6                                      # a fail never shows 5
    assert st.luck_words(0.997) == "under 1 in 100"


def metric(sharpe, ret=10.0, trades=60, dd=20.0, days=2400, skew=0.0, kurt=3.0):
    return {"sharpe_daily": sharpe / math.sqrt(365), "sharpe": sharpe, "return_pct": ret, "rule_trades": trades,
            "max_drawdown_pct": dd, "days": days, "skew": skew, "kurtosis": kurt}


def test_verdict_needs_every_condition():
    bench = {"holdout": metric(0.5, 20, 0, 50), "walk_forward": metric(2.5, 100, 0, 70)}
    good = {"chosen": {"params": {}}, "holdout": metric(1.5, 30, 40, 30), "walk_forward": metric(3.0, 200, 80, 40)}
    assert st.verdict(good, bench, 25, 0.0001) == ("PASS", [])
    broken = {
        "1": dict(good, chosen=None, holdout=None),
        "2": dict(good, holdout=metric(1.5, 30, 29, 30)),
        "3": dict(good, walk_forward=metric(3.0, 200, 49, 40)),
        "4": dict(good, holdout=metric(0.6, -1.0, 40, 30)),         # a positive Sharpe and still a loss
        "5": dict(good, holdout=metric(0.45, 5, 40, 30)),
        "6": dict(good, walk_forward=metric(-0.1, -5, 80, 40)),
        "7": dict(good, walk_forward=metric(2.4, 50, 80, 40)),
        "8": dict(good, walk_forward=metric(3.0, 200, 80, 75)),
        "9": dict(good, walk_forward=metric(3.0, 200, 80, 40, days=120)),
    }
    starts = {"1": "too few trades", "2": "only 29 rule trades in the judged period", "3": "only 49 rule trades in the "
              "walk-forward", "4": "did not beat cash in the judged period", "5": "did not beat holding the coins in "
              "the judged period", "6": "did not beat cash in the walk-forward", "7": "did not beat holding the coins "
              "in the walk-forward", "8": "fell deeper than holding the coins", "9": "a walk-forward record this good "
              "would turn up by luck"}
    for k, entry in broken.items():
        verdict, reasons = st.verdict(entry, bench, 25, 0.0001)
        assert verdict == "FAIL" and reasons[0].startswith(starts[k]), (k, reasons)
        if k not in ("1", "6"):
            assert len(reasons) == 1, (k, reasons)


def test_references_are_never_candidates(small):
    report = small_report()
    refs = [e for e in report["entries"] if e["kind"] == "reference"]
    assert {e["key"] for e in refs} == {"desk-trend", "desk-hourly"}
    assert {e["verdict"] for e in refs} <= {"REFERENCE", "SKIPPED"} and "reasons" not in refs[0]
    trend_ref = [e for e in refs if e["key"] == "desk-trend"][0]
    assert trend_ref["trials"] == 20 and 0 <= trend_ref["dsr"] <= 1


def test_the_protocol_is_the_pre_declared_one():
    """Changing any of these after a real run is a new protocol: bump PROTOCOL_VERSION and add the old trials() to
    TRIALS_BEFORE (trader/library_stats.py docstring)."""
    msg = "a changed protocol: bump PROTOCOL_VERSION and TRIALS_BEFORE"
    assert (st.PROTOCOL_VERSION, st.TRIALS_BEFORE, st.SPLIT) == (1, 0, 0.70), msg
    assert (st.WF_IN_DAYS, st.WF_OUT_DAYS, st.WF_MIN_LAST_DAYS) == (730, 182, 91), msg
    assert (st.MIN_TRADES_PER_YEAR, st.MIN_HOLDOUT_TRADES, st.MIN_WF_TRADES, st.LUCK_LEVEL) == (5, 30, 50, 0.95), msg
    assert st.DESK_TRIALS == {"desk-hourly": 200, "desk-trend": 20} and st.DAYS_PER_YEAR == 365, msg
    assert (L.WARMUP_DAYS, L.BALANCE, L.SLOT_SHARE, L.RISK_PER_TRADE, L.VOL_TARGET) == (210, 10_000, 0.10, 0.01, 0.5), msg
    assert (L.FEE, L.MARKET_SLIPPAGE, L.STOP_FILL_SLIPPAGE) == (0.001, 0.0005, 0.002), msg
    assert L.COINS == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"), msg
    grids = {f.key: f.grid for f in L.FAMILIES}
    assert grids == {
        "donchian": ({"entry": 20, "exit": 10}, {"entry": 55, "exit": 20}),
        "tsmom": ({"lookback": 28}, {"lookback": 14}, {"lookback": 91}, {"lookback": 182}),
        "sma_cross": ({"fast": 1, "slow": 50}, {"fast": 1, "slow": 150}, {"fast": 5, "slow": 150},
                      {"fast": 1, "slow": 200}, {"fast": 2, "slow": 200}),
        "rsi2": ({"below": 10}, {"below": 5}),
        "bollinger": ({"period": 20, "width": 2.0}, {"period": 10, "width": 1.9}, {"period": 50, "width": 2.1}),
        "macd_4h": ({"fast": 12, "slow": 26, "signal": 9, "zero": False}, {"fast": 5, "slow": 35, "signal": 5, "zero": False},
                    {"fast": 12, "slow": 26, "signal": 9, "zero": True}, {"fast": 5, "slow": 35, "signal": 5, "zero": True}),
        "williams": ({"k": 0.5}, {"k": 0.75}, {"k": 1.0}),
    }, msg
    assert {f.key: (f.timeframe, f.kind) for f in L.FAMILIES} == {
        "donchian": ("1d", "event"), "tsmom": ("1d", "state"), "sma_cross": ("1d", "state"), "rsi2": ("1d", "event"),
        "bollinger": ("1d", "event"), "macd_4h": ("4h", "state"), "williams": ("1d", "intraday")}, msg


def small_report():
    """The shared small run's report, with run times, for the text tests."""
    report, _ = spied_run()
    report.update(started_at="2026-10-31T10:00:00+00:00", finished_at="2026-10-31T10:31:00+00:00")
    return report


VOLATILE = ("started_at", "finished_at", "seconds", "cpu_seconds", "peak_mb", "version")


def test_the_library_is_deterministic(small):
    a, b = spied_run()[0], json.loads(json.dumps(L.run_library(dataset(N_DAYS), "--no-hourly")))
    for k in VOLATILE:
        a.pop(k), b.pop(k)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---- 34-36: the price cache ----
class FakeClient:
    """Serves synthetic candles in pages of 1,000, like Binance; records every start time it was asked for."""
    def __init__(self, data, forming=True):
        self.data, self.calls, self.forming = data, [], forming

    def klines(self, symbol, interval="1h", limit=200, start_ms=None):
        assert start_ms, "never ask without a start time (Binance would answer with the latest candles)"
        self.calls.append((symbol, interval, start_ms))
        s = self.data[interval][symbol]
        i = s.upto(start_ms - 1)
        rows = [{"time": s.time[k], "open": s.open[k], "high": s.high[k], "low": s.low[k], "close": s.close[k],
                 "volume": s.volume[k]} for k in range(i, min(len(s), i + limit))]
        if self.forming and i + limit >= len(s):        # Binance's last row: the candle still forming
            step = D.INTERVAL_MS[interval]
            rows.append({"time": s.time[-1] + step, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
        return rows[:limit]


def test_cache_round_trip_and_incremental_update(tmp_path):
    full = dataset(2500, coins=("BTCUSDT",))
    btc = full["1d"]["BTCUSDT"]
    early = {"1d": {"BTCUSDT": btc.cut(btc.time[2200])}}
    client = FakeClient(early)
    now = btc.time[2200] + DAY + 1
    s, n = D.update(client, "BTCUSDT", "1d", tmp_path, D.START_MS, now_ms=now)
    assert n == 3 and len(s) == 2201 and client.calls[0][2] == D.START_MS        # pages of 1,000 from 2017
    assert D.load(D.candle_path(tmp_path, "BTCUSDT", "1d")) == list(zip(s.time, s.open, s.high, s.low, s.close, s.volume))
    client = FakeClient(full)
    later = btc.time[-1] + DAY + 1
    s2, n2 = D.update(client, "BTCUSDT", "1d", tmp_path, D.START_MS, now_ms=later)
    assert n2 == 1 and client.calls == [("BTCUSDT", "1d", btc.time[2201])]        # only what is new
    assert list(s2.close) == list(btc.close) and list(s2.time) == list(btc.time)
    s3, n3 = D.update(FakeClient(full), "BTCUSDT", "1d", tmp_path, D.START_MS, now_ms=later)
    assert n3 == 0 and len(s3) == len(btc)                                       # nothing finished since: no request
    data, used = D.universe(FakeClient(dataset(400)), tmp_path / "u", ("BTCUSDT", "ETHUSDT"), now_ms=later,
                            hourly=False)
    assert used == 2 + 6 and set(data["1d"]) == {"BTCUSDT", "ETHUSDT"} and data["1h"] == {}   # 2,400 4h bars: 3 pages


def test_cache_drops_the_forming_candle_and_overlapping_rows(tmp_path):
    full = dataset(53, coins=("BTCUSDT",))
    btc = full["1d"]["BTCUSDT"]
    early = {"1d": {"BTCUSDT": btc.cut(btc.time[49])}}
    s, _ = D.update(FakeClient(early), "BTCUSDT", "1d", tmp_path, D.START_MS, now_ms=btc.time[49] + DAY + 1)
    assert len(s) == 50 and s.time[-1] == btc.time[49]                           # the forming row was dropped

    class Overlap(FakeClient):
        def klines(self, symbol, interval="1h", limit=200, start_ms=None):
            return super().klines(symbol, interval, limit, start_ms - 5 * DAY)      # an overlapping page
    s2, n = D.update(Overlap(full), "BTCUSDT", "1d", tmp_path, D.START_MS, now_ms=btc.time[-1] + DAY + 1)
    assert n == 1 and list(s2.time) == list(btc.time) and list(s2.close) == list(btc.close)
    assert all(b > a for a, b in zip(s2.time, s2.time[1:]))


def test_a_corrupt_cache_file_is_downloaded_again(tmp_path):
    full = dataset(30, coins=("BTCUSDT",))
    path = D.candle_path(tmp_path, "BTCUSDT", "1d")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a gzip file")
    said = []
    s, n = D.update(FakeClient(full), "BTCUSDT", "1d", tmp_path, D.START_MS,
                    now_ms=full["1d"]["BTCUSDT"].time[-1] + DAY + 1, progress=said.append)
    assert n == 1 and len(s) == 30 and said and "could not be read" in said[0]
    with gzip.open(path, "wt") as fh:                                             # times out of order
        fh.write("time,open,high,low,close,volume\n2,1,1,1,1,1\n1,1,1,1,1,1\n")
    assert D.load(path) is None


# ---- 39-40: the desk's own two strategies ----
def test_desk_references_use_their_own_replays_and_tested_defaults(small, monkeypatch):
    data = dataset(400, hourly=False)
    J = L.joins(data["1d"])
    seen = {}

    def fake_trend(s, history, start_ms=None, end_ms=None, keep_curve=False):
        seen["trend"] = (s, set(history), start_ms, end_ms, keep_curve)
        return {"curve": [], "closed": []}
    monkeypatch.setattr(trend, "backtest", fake_trend)
    L.desk_trend(data["1d"], J, J["BTCUSDT"], END)
    s, coins, start, end, keep = seen["trend"]
    assert coins == set(data["1d"]) and s.max_open_positions == 8 and keep and end == END and start == J["BTCUSDT"] - DAY
    assert trend.params(s) == trend.TESTED and s.strategy == "trend" and s.starting_balance == 10_000

    class FakeBacktest:
        def __init__(self, s, hist, decision=None, days=None, btc_history=None, end_ms=None, keep_curve=False):
            seen["hourly"] = (s, set(hist), days, end_ms, keep_curve, decision, btc_history is hist["BTCUSDT"])

        def run(self):
            return {"curve": [], "closed": []}
    monkeypatch.setattr(L, "Backtest", FakeBacktest)
    L.desk_hourly(data["1d"], END - 100 * DAY, END)
    s, coins, days, end, keep, decision, btc = seen["hourly"]
    assert s.max_open_positions == 3 and s.max_position_pct == 0.10 and s.risk_per_trade_pct == 0.01
    assert days == 1095 and end == END + 23 * 3_600_000 and keep and btc and decision.buy_threshold == 0.35
    assert s.mentor_checks == ("trend200", "btc", "cooldown", "reward") and s.trail_atr == 2.0


def test_a_later_coin_is_first_bought_by_the_trend_reference_on_its_join_day(small, monkeypatch):
    monkeypatch.setattr(L, "WARMUP_DAYS", 100)          # more than the trend rule's 61-day window, as in production
    rising = {sym: bars([100 * (1.004 + 0.001 * k) ** i for i in range(n)], symbol=sym, t0=END - (n - 1) * DAY)
              for k, (sym, n) in enumerate((("BTCUSDT", 300), ("ETHUSDT", 300), ("SOLUSDT", 200)))}
    J = L.joins(rising)
    from trader.agents.risk import RiskManagerAgent
    real, prices = RiskManagerAgent.review_trend, []

    def spy(self, symbol, price, share, folio):
        prices.append((symbol, price))
        return real(self, symbol, price, share, folio)
    monkeypatch.setattr(RiskManagerAgent, "review_trend", spy)
    run = L.desk_trend(rising, J, J["BTCUSDT"], END)
    sol = rising["SOLUSDT"]
    first = [p for sym, p in prices if sym == "SOLUSDT"][0]
    day = sol.time[[round(o * (1 + trend.SLIPPAGE), 9) for o in sol.open].index(round(first, 9))]
    assert day == J["SOLUSDT"]
    assert run["curve"][0][0] == J["BTCUSDT"] - DAY and run["base"] == 10_000


def test_memory_guard_skips_the_hourly_reference_with_a_message(small, monkeypatch):
    order = []
    real_sim, real_trend = L.simulate, L.desk_trend
    monkeypatch.setattr(L, "simulate", lambda *a: order.append("family") or real_sim(*a))
    monkeypatch.setattr(L, "desk_trend", lambda *a: order.append("trend") or real_trend(*a))
    monkeypatch.setattr(L, "desk_hourly", lambda *a: pytest.fail("the hourly reference must be skipped"))
    data = dataset(400)
    data["1h"] = {sym: s for sym, s in data["1d"].items()}          # present: only the memory guard can skip it
    report = L.run_library(data, None, free_mb=lambda: order.append("guard") or 250.0)
    assert order[-2:] == ["trend", "guard"] and "family" not in order[order.index("trend"):]
    hourly = [e for e in report["entries"] if e["key"] == "desk-hourly"][0]
    assert hourly["verdict"] == "SKIPPED" and "250 MB free" in hourly["note"] and "400 MB" in hourly["note"]
    assert data["4h"] == {} and data["1h"] == {}                       # freed
    assert "about 300 MB" in R.summary_text(report)


# ---- 41-46: texts and integration ----
LONG = ("a walk-forward record this good would turn up by luck about 100 times in 100 counting all 25 variants (a pass "
        "needs 5 or fewer)", "fell deeper than holding the coins (judged period 75% vs 55%; walk-forward 88% vs 71%)")


def test_summary_fits_one_message_and_says_it_is_a_measurement(small):
    report = small_report()
    for e in report["entries"]:
        if e["kind"] == "family":
            e["reasons"] = [LONG[len(e["key"]) % 2]] * 9          # the longest reasons the verdict writes
        if e["key"] == "desk-trend":
            e["dsr"] = 0.82
    text = R.summary_text(report)
    assert len(text) < 4000 and len(text.splitlines()) < 30
    report["entries"][0]["reasons"] = ["x" * 3000]                 # far too long: one shorter line per rule instead
    assert len(R.summary_text(report)) < 4000
    for words in ("trades nothing", "luck about", "candidate", "gain over cash", "Bitcoin alone", "cash 0%"):
        assert words in text, words
    for words in ("will rise", "predict", "chance it is luck", "not slowed"):
        assert words not in text.lower(), words
    assert "📏 Desk, daily trend rule (reference)" in text and "⏭️ Desk, hourly rules: skipped (--no-hourly)" in text
    passing = small_report()
    fam = [e for e in passing["entries"] if e["kind"] == "family"][0]
    fam.update(verdict="PASS", reasons=[], dsr=0.97)
    passing["pass_history"] = {fam["key"]: [1, 6]}
    text = R.summary_text(passing)
    assert "PASS, a candidate only" in text and "passed in 1 of the last 6 saved runs" in text
    assert "luck about 3 in 100" in text and "1 published rule passed" in text
    old = {"protocol": 2, "entries": [{"kind": "family", "name": "x"}]}
    assert R.summary_text(old).startswith("📚")                       # keys missing: a text, not an error
    assert R.summary_text({}).startswith("📚")
    table = R.table_text(small_report())
    assert "Hold the 3 coins" in table and "Donchian / Turtle" in table and "yardstick" in table


def test_library_md_lists_every_source_url_caveat_and_reproduction(small):
    j = Journal(":memory:", 10_000)
    assert R.library_md(j).startswith("# Strategy library\n\nNo library run yet")
    report = small_report()
    j.set("library_report", report)
    j.set("library_runs", [{"ts": "2026-10-31T10:31:00+00:00", "protocol": 1, "end": report["period"]["end"],
                            "passed": [], "best": {"key": "tsmom", "wf_sharpe": 0.9, "luck_in_100": 18}}])
    md = R.library_md(j)
    for f in L.FAMILIES:
        assert f.url in md and f.source in md and f.rule in md
    for c in R.CAVEATS:
        assert c in md
    for heading in ("## What was tested", "## How it was judged", "## Judged period", "## Walk-forward (",
                    "## Verdicts", "## In-sample choices", "## Walk-forward choices", "## The desk's own strategies",
                    "## Caveats", "## Data", "## Earlier runs", "## Reproduce"):
        assert heading in md, heading
    assert "python -m trader library" in md and "desk library" in md and "tsmom (Sharpe 0.90, luck 18 in 100)" in md
    assert "Bailey & Lopez de Prado 2014" in md and "It is not the chance that the rule is luck" in md


def test_help_texts_list_library_in_both_modes():
    assert "/library" in main.HELP_TEXT and "/library" in main.help_text(Settings(strategy="trend"))
    assert R.HELP_LINE in main.HELP_TEXT and "it trades nothing" in R.HELP_LINE
    assert len(main.HELP_TEXT) < 3500                                  # the start message stays one Telegram message
    assert "desk library" in (ROOT / "deploy" / "desk").read_text()
    desk = (ROOT / "deploy" / "desk").read_text()
    assert '"$PY" -m trader library --save --low-priority "$@"' in desk and "systemd-run" in desk


class FakeProc:
    """Stands in for the library's child process: wait() acts as the child would, then returns its exit code."""
    plan: list = []
    started: list = []

    def __init__(self, cmd, cwd=None, stdin=None, stdout=None, stderr=None):
        self.cmd, self.log, self.code = cmd, stdout, None
        self.act = FakeProc.plan.pop(0)
        self.gate = threading.Event()
        self.killed = False
        FakeProc.started.append(self)

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.act.get("timeout") and timeout is not None:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if self.killed:
            self.code = -9
            return self.code
        self.gate.wait(5)
        self.code = self.act["run"](self)
        return self.code

    def kill(self):
        self.killed = True


def wait_for(tg, needle, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        hits = [t for t, _ in tg.sent if needle in t]
        if hits:
            return hits[-1]
        time.sleep(0.02)
    return None


def test_library_command_runs_one_child_and_sends_the_summary(tmp_path, monkeypatch):
    from tests.test_trend_main import run_desk
    s, tg, bot = run_desk(tmp_path, monkeypatch, symbols=["BTCUSDT"])
    h = tg.handlers["/library"]
    assert h("") == R.OFFLINE_TEXT and h("last") == R.NO_RUN_TEXT
    s.offline, bot.market.client = False, object()
    monkeypatch.setattr(main.subprocess, "Popen", FakeProc)
    report_path = tmp_path / "library" / "report.json"
    sample = {"protocol": 1, "started_at": "", "finished_at": "2026-10-31T10:31:00+00:00", "entries": [],
              "period": {"start": "2018-03-15", "end": "2026-10-30"}}

    def ok(proc):
        report = dict(sample, started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        report_path.write_text(json.dumps(report))
        j = Journal(s.db_path, s.starting_balance)
        j.set("library_report", report)
        j.db.close()
        return 0

    def failed(proc):
        proc.log.write("Strategy library failed: boom\n")
        proc.log.flush()
        return 1

    def not_an_object(proc):
        report_path.write_text("[1, 2]")
        return 0
    FakeProc.plan = [{"run": ok}, {"run": lambda p: 3}, {"run": failed}, {"run": not_an_object}, {"timeout": True}]
    FakeProc.started = []
    first = h("")
    assert first.startswith("📚 Strategy library started") and "downloads about 50 MB" in first
    child = FakeProc.started[-1]
    assert child.cmd[1:] == ["-m", "trader", "library", "--save", "--low-priority", "--report", str(report_path)]
    assert h("").startswith("📚 The strategy library is already running") and len(FakeProc.started) == 1
    child.gate.set()
    assert wait_for(tg, "📚 Strategy library · protocol 1")
    assert h("last").startswith("📚 Strategy library · protocol 1")
    for expect in ("still going", "boom", "stopped with an error", "still running after 4 hours"):
        before = len(tg.sent)
        assert h("").startswith("📚 Strategy library started")
        FakeProc.started[-1].gate.set()
        end = time.time() + 5
        while len(tg.sent) == before and time.time() < end:
            time.sleep(0.02)
        assert expect in tg.sent[-1][0], (expect, tg.sent[-1][0])
    assert FakeProc.started[-1].killed and FakeProc.started[-1].code == -9      # stopped and reaped


def fake_universe(data_maker):
    def universe(client, directory, symbols, now_ms=None, refresh=False, hourly=True, progress=None):
        return data_maker(), 7
    return universe


def test_cli_saves_to_the_journal_and_writes_files(small, tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, offline=False: Settings(offline=True,
                                                                                          db_path=str(db))))
    monkeypatch.setattr(L, "BinanceClient", lambda url: FakeClient(dataset(400)))
    monkeypatch.setattr(L, "universe", fake_universe(lambda: dataset(400)))
    out = tmp_path / "r.json"
    assert L.main(["--save", "--no-hourly", "--quiet", "--report", str(out)]) == 0
    assert not db.exists() and json.loads(out.read_text())["protocol"] == 1       # no journal created
    assert (tmp_path / "library" / "LIBRARY.md").read_text().startswith("# Strategy library")
    Journal(str(db), 10_000).db.close()
    for _ in range(2):
        assert L.main(["--save", "--no-hourly", "--quiet", "--report", str(out)]) == 0
    j = Journal(str(db), 10_000)
    report, runs = j.get("library_report"), j.get("library_runs")
    assert report["requests"] == 7 and len(runs) == 2 and runs[0]["protocol"] == 1
    assert set(report["pass_history"]) == {f.key for f in L.FAMILIES}
    assert all(v[1] == 2 for v in report["pass_history"].values())
    assert [e["kind"] for e in j.events()].count("library") == 2
    assert json.loads(out.read_text())["pass_history"] == report["pass_history"]
    j.db.close()
    with open(tmp_path / "library" / "run.lock", "w") as fh:                   # another run holds the lock
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert L.main(["--no-hourly", "--quiet"]) == 3


def test_the_library_changes_no_decision_order_or_pace(small, tmp_path):
    from tests.test_events import trading_record
    desks = []
    for with_library in (True, False):
        s = Settings(offline=True)
        bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
        if with_library:
            bot.journal.set("library_report", small_report())
            bot.journal.set("library_runs", [{"ts": "x", "protocol": 1, "end": "2026-10-30", "passed": ["tsmom"],
                                              "best": None}])
            bot.journal.event("library", "Strategy library (protocol 1): 1 of 7 published rules passed")
        paces = []
        for _ in range(40):
            bot.run_cycle()
            paces.append(dict(bot.pace))
        desks.append((bot, paces))
    (on, on_pace), (off, off_pace) = desks
    assert trading_record(on) == trading_record(off) and on_pace == off_pace
    for k, (bot, _) in enumerate(desks):
        notes = Notes(bot.journal, tmp_path / f"n{k}", bot.base)
        assert "Strategy library" not in notes.digest() and "LIBRARY" not in notes.digest()
        assert notes.refresh() is not None and (tmp_path / f"n{k}" / "LIBRARY.md").exists()
        assert "| LIBRARY.md |" in (tmp_path / f"n{k}" / "README.md").read_text()
    assert "Strategy library: published trading rules" in (tmp_path / "n0" / "LIBRARY.md").read_text()
    assert "No library run yet" in (tmp_path / "n1" / "LIBRARY.md").read_text()


# ---- 47-49: added by the design review ----
def test_end_of_window_closes_do_not_count_toward_the_trade_minimums(small):
    coins = {sym: bars(wave(960, seed=k), symbol=sym) for k, sym in enumerate(L.COINS)}
    J = {sym: s.time[0] for sym, s in coins.items()}
    P = st.periods(coins["BTCUSDT"].time[0], coins["BTCUSDT"].time[-1])
    assert len(P["segments"]) >= 13
    always = family(enter=lambda s, c, i: {"stop": None, "share": 0.1})
    runs = [L.simulate(always, {}, coins, {k: {} for k in coins}, J, ta, tb) for _, _, ta, tb in P["segments"][:13]]
    for run, (_, _, ta, tb) in zip(runs, P["segments"]):
        assert {t["reason"] for t in run["trades"]} == {"end of window"} and len(run["trades"]) == 8
        assert run["curve"][-1][1] == pytest.approx(10_000 + sum(t["pnl"] for t in run["trades"]), rel=1e-9)
    wf = st.metrics(st.stitch(runs), P["segments"][0][2], P["segments"][12][3])
    assert wf["trades"] == 104 and wf["rule_trades"] == 0
    product = math.prod(r["curve"][-1][1] / 10_000 for r in runs)
    assert wf["return_pct"] == pytest.approx((product - 1) * 100, abs=0.01)
    ca, cb = P["segments"][0][:2]
    rows = L._choice_rows(always, coins, [{k: {} for k in coins}], J, ca, cb)
    assert rows[0]["trades"] == 0 and st.choose(rows, 120 / 365) is None
    ha, hb = P["holdout"]
    entry = {"chosen": {"params": {}}, "walk_forward": wf,
             "holdout": st.metrics(L.simulate(always, {}, coins, {k: {} for k in coins}, J, ha, hb), ha, hb)}
    _, reasons = st.verdict(entry, {}, 25, 0.0001)
    assert reasons[0].startswith("only 0 rule trades in the judged period")
    assert reasons[1].startswith("only 0 rule trades in the walk-forward")


def test_library_md_never_raises_and_the_notebook_keeps_its_other_files(tmp_path):
    s = Settings(offline=True)
    for damaged in ([1, 2], {"protocol": 1, "entries": [{"kind": "family"}]}, {"protocol": 2}):
        j = Journal(":memory:", 10_000)
        j.set("library_report", damaged)
        assert R.library_md(j).startswith("# Strategy library\n\nThe latest library result could not be shown")
        bot = Orchestrator(s, j)
        bot.run_cycle()
        d = tmp_path / str(len(str(damaged)))
        notes = Notes(j, d, s)
        assert notes.refresh() is not None
        assert {"STRATEGY.md", "EVENTS.md", "LIBRARY.md", "README.md"} <= {p.name for p in d.iterdir()}
        assert list((d / "strategies").glob("*.md"))


def test_no_trading_module_imports_the_library():
    banned = {"library", "library_data", "library_stats", "library_report"}
    files = [ROOT / "trader" / f for f in ("orchestrator.py", "config.py", "lab.py", "strategy.py", "calibration.py",
                                           "health.py", "scorecard.py", "report.py")]
    files += sorted((ROOT / "trader" / "agents").glob("*.py"))
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not any(n.split(".")[-1] in banned for n in names), (path.name, names)


def test_the_cli_is_wired_into_the_desk_command(monkeypatch):
    called = {}
    monkeypatch.setattr(L, "main", lambda argv: called.setdefault("argv", argv) and 0)
    monkeypatch.setattr(main.sys, "argv", ["trader", "library", "--no-hourly"])
    with pytest.raises(SystemExit):
        main.main()
    assert called["argv"] == ["--no-hourly"]


def test_the_hourly_reference_runs_on_the_desk_replay_and_is_sampled_daily(small):
    hourly = {}
    for k, sym in enumerate(("BTCUSDT", "ETHUSDT")):
        rows, p = [], 100.0 + k
        t0 = END - 30 * DAY
        for h in range(31 * 24):
            c = p * (1 + 0.003 * math.sin(h / 30 + k) + 0.0004)
            rows.append((t0 + h * 3_600_000, p, max(p, c) * 1.002, min(p, c) * 0.998, c, 1.0))
            p = c
        hourly[sym] = Series.from_rows(sym, "1h", rows)
    run = L.desk_hourly(hourly, END - 20 * DAY, END)
    days = [d for d, _ in run["curve"]]
    assert days == sorted(set(days)) and days[-1] == END and all(d % DAY == 0 for d in days)
    assert st.metrics(run, END - 20 * DAY, END)["days"] == 21 and run["base"] == 10_000


# ---- 1.9.0 review fixes ----
def test_walk_forward_holding_is_restarted_for_the_verdicts_and_bought_once_for_information(small):
    report = small_report()
    data = dataset(N_DAYS)
    daily = data["1d"]
    segs = p_of(data)["segments"]
    a, b = segs[0][2], segs[-1][3]
    once = st.metrics(L.hold(daily, L.joins(daily), a, b), a, b)
    y = report["yardsticks"]
    assert y["hold"]["walk_forward_once"]["return_pct"] == pytest.approx(once["return_pct"], rel=1e-12)
    assert y["hold"]["walk_forward_once"]["return_pct"] != pytest.approx(y["hold"]["walk_forward"]["return_pct"])
    assert "walk_forward_once" in y["btc"] and "walk_forward_once" not in y["cash"]
    fam = next(e for e in report["entries"] if e["kind"] == "family" and e.get("walk_forward"))
    bench = dict(y["hold"])
    for value in (-1e9, 1e9):                    # the bought-once figure never moves a verdict
        bench["walk_forward_once"] = dict(bench["walk_forward_once"], sharpe_daily=value, max_drawdown_pct=value)
        assert st.verdict(fam, bench, st.trials(), 0.0001) == st.verdict(fam, y["hold"], st.trials(), 0.0001)
    summary, table, md = R.summary_text(report), R.table_text(report), R.library_page(report, [])
    assert "holding re-split every 6 months (the bar the rules must beat)" in summary and "bought once" in summary
    assert "Hold (bought once)" in table and "Bitcoin (bought once)" in table
    assert "for information (no verdict uses it)" in table
    assert "Hold the coins, re-split every 6 months (the bar)" in md and "Hold the coins, bought once" in md
    assert "re-split equally every 6 months" in md and "(so does holding)" not in md


def test_an_intraday_rule_shows_intraday_instead_of_zero_invested(small):
    report = small_report()
    williams = next(e for e in report["entries"] if e["key"] == L.WILLIAMS.key)
    assert williams["style"] == "intraday"
    row = R._metric_row(williams["name"], williams["holdout"], True)
    assert row[R.METRIC_HEADERS.index("Invested")] == "intraday"
    assert R._metric_row("x", None, True)[R.METRIC_HEADERS.index("Invested")] == "n/a"
    md = R.library_page(report, [])
    lines = [x for x in md.splitlines()
             if x.startswith(f"| {williams['name']} |") and x.split(" | ")[1][:1] in "+-"]
    assert len(lines) == 2 and all("| intraday |" in x for x in lines)     # judged period and walk-forward


def test_the_caveats_name_the_crypto_studies_behind_two_families():
    text = " ".join(R.CAVEATS)
    assert "Most rules were published before crypto prices existed" in text and "Liu and Tsyvinski" in text
    assert "The rules were published before crypto prices existed" not in text


def test_a_busy_journal_still_writes_the_report_and_the_page(small, tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "journal.db"
    Journal(str(db), 10_000).db.close()
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls, offline=False: Settings(offline=True,
                                                                                          db_path=str(db))))
    monkeypatch.setattr(L, "BinanceClient", lambda url: FakeClient(dataset(400)))
    monkeypatch.setattr(L, "universe", fake_universe(lambda: dataset(400)))

    def busy(s, report):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(L, "_save", busy)
    out = tmp_path / "r.json"
    assert L.main(["--save", "--no-hourly", "--quiet", "--report", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["not_saved"] == "database is locked"
    assert (tmp_path / "library" / "LIBRARY.md").read_text().startswith("# Strategy library")
    assert "The journal could not be updated" in R.summary_text(report)


def test_a_run_the_server_stops_says_so_and_nohourly_is_passed_through(tmp_path, monkeypatch):
    from tests.test_trend_main import run_desk
    s, tg, bot = run_desk(tmp_path, monkeypatch, symbols=["BTCUSDT"])
    h = tg.handlers["/library"]
    s.offline, bot.market.client = False, object()
    monkeypatch.setattr(main.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(FakeProc, "pid", 4242, raising=False)
    niced = []
    monkeypatch.setattr(main.os, "setpriority", lambda which, pid, value: niced.append((which, pid, value)))

    def oom(proc):
        proc.log.write("Hourly reference (the desk's hourly rules, the last 3 years) ...\n")
        proc.log.flush()
        return -9
    FakeProc.plan = [{"run": oom}, {"run": lambda p: 1}]
    FakeProc.started = []
    assert h("").startswith("📚 Strategy library started")
    assert niced == [(main.os.PRIO_PROCESS, 4242, 19)]
    assert "--no-hourly" not in FakeProc.started[-1].cmd
    FakeProc.started[-1].gate.set()
    text = wait_for(tg, "The server stopped the strategy library")
    assert text and "(signal 9)" in text and "memory ran out" in text and "/library nohourly" in text
    assert "It was at: Hourly reference" in text
    assert h("nohourly").startswith("📚 Strategy library started")
    assert FakeProc.started[-1].cmd[-1] == "--no-hourly"
    FakeProc.started[-1].gate.set()
    assert wait_for(tg, "stopped with an error")
    assert "The price files finished so far are kept" in R.failure_text("")
    restart = R.killed_text(15, "")
    assert "restarting or updating" in restart and "memory" not in restart and "It was at" not in restart


def test_desk_library_runs_as_a_low_weight_background_service(tmp_path):
    app, tools, calls = tmp_path / "app", tmp_path / "bin", tmp_path / "calls.txt"
    (app / ".venv" / "bin").mkdir(parents=True)
    tools.mkdir()

    def tool(name, body):
        f = tools / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    tool("sudo", 'exec "$@"')
    tool("systemd-run", f'echo "systemd-run $*" >> {calls}')
    tool("systemctl", f'echo "systemctl $*" >> {calls}; exit 3')           # "inactive": the run has ended
    tool("journalctl", f'echo "journalctl $*" >> {calls}; exec sleep 30')
    desk = tmp_path / "desk"
    desk.write_text((ROOT / "deploy" / "desk").read_text().replace("__APP_DIR__", str(app))
                    .replace("__SERVICE__", "crypto-desk"))
    started = time.time()
    r = subprocess.run(["bash", str(desk), "library", "--no-hourly"], env={"PATH": f"{tools}:/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert time.time() - started < 20                                      # the progress view ends with the run
    log = calls.read_text()
    run = next(x for x in log.splitlines() if x.startswith("systemd-run "))
    for part in ("--collect", "--unit=crypto-desk-library", f"--working-directory={app}", "-p Nice=19",
                 "-p CPUWeight=1", "-p OOMScoreAdjust=1000"):
        assert part in run, part
    assert run.endswith(f"{app}/.venv/bin/python -m trader library --save --low-priority --no-hourly")
    assert "journalctl -u crypto-desk-library -f -o cat --since" in log
    assert "keeps running if you disconnect" in r.stdout
