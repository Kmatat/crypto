"""The strategy library (release 1.9.0): published trading rules, tested honestly on years of real prices.

  python -m trader library                 # on any computer with the project: prints the table, writes LIBRARY.md
  python -m trader library --no-hourly     # without the desk's hourly reference (less memory, a third of the time)
  desk library                             # on the server: the same, saved to the journal (for /notes, /library last)

What it is: seven well-known, published, long-only rule families, each written here from its published description
(the source is cited next to it), plus the desk's own two strategies as references, against three yardsticks
(holding the same 8 coins, Bitcoin alone, cash). Every one is judged by the same strict protocol, fixed in
trader/library_stats.py before any of them was run on real prices: settings chosen on the first 70% of the years,
judged once on the last 30%, a walk-forward (2 years to choose, the next 6 months to test), and a luck adjustment
that counts every variant tried. A rule gets PASS only if it beats both cash and holding the coins on a
risk-adjusted basis, with enough trades, and survives the luck adjustment.

What it is not: it trades nothing. No setting, prompt, threshold or risk rule reads its result; a rule that passes is
a candidate for a later release the owner would approve, never switched on here. Nothing is downloaded but Binance's
public prices; no code is ever downloaded or run.

How every library rule trades (one simulator for all, so they compare fairly):
  * decisions on a finished bar's close, orders at the next bar's open (0.1% fee, 0.05% slippage);
  * a stop is checked on each bar's low and fills at the stop, or at the open when the bar opened below it, less 0.2%;
  * one slot per coin, each buy at most 10% of the account (the desk's position cap), never more than the cash;
  * long only, no leverage, no adding to a position; cash earns nothing;
  * every window starts flat with 10,000 USDT, and what is still open at its end is valued at what selling it would
    have brought (those protocol closes do not count as the rule's own trades).
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import math
import os
import resource
import statistics
import sys
import time
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import library_stats as st
from . import trend
from .agents import technical
from .agents.execution import PAPER_FEE, STOP_SLIPPAGE
from .backtest import Backtest
from .binance_client import BinanceClient
from .config import PUBLIC_BASE_URL, Settings
from .library_data import DAY_MS, INTERVAL_MS, Series, cache_dir, universe

COINS = trend.TESTED_COINS             # the universe: the 8 coins the desk's research tested
WARMUP_DAYS = 210                      # a coin joins 210 days after its first Binance candle (200-day averages exist)
BALANCE = 10_000.0
MAX_POSITION_PCT = 0.10                # the desk's position cap (Settings.max_position_pct default)
SLOT_SHARE = min(MAX_POSITION_PCT, 1 / len(COINS))   # 10%: one slot per coin, as the trend rule
RISK_PER_TRADE = 0.01                  # the desk's Turtle sizing: a stop costs at most 1% of equity
VOL_TARGET = 0.5                       # trend.TESTED[1]: the desk's volatility target, for the momentum family
FEE, MARKET_SLIPPAGE, STOP_FILL_SLIPPAGE = PAPER_FEE, trend.SLIPPAGE, STOP_SLIPPAGE
HOURLY_MIN_FREE_MB = 400               # the hourly reference (about 300 MB) runs only with this much free memory
HOURLY_DAYS = 1095                     # the hourly reference replays the last 3 years
NAN = float("nan")


# ---- indicators: every value i uses bars <= i only (nan until it exists) ----
def _blank(n: int) -> array:
    return array("d", [NAN]) * n


def sma(x, n: int) -> array:
    """Rolling average of the last n values (a 1-day average is the value itself)."""
    if n == 1:
        return array("d", x)
    out, total = _blank(len(x)), 0.0
    for i, v in enumerate(x):
        total += v
        if i >= n:
            total -= x[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def _prior(x, n: int, better) -> array:
    """The best of the n bars BEFORE bar i (bar i itself excluded), with a monotonic queue."""
    out, q = _blank(len(x)), deque()
    for i in range(len(x)):
        if i >= n:
            while q[0] < i - n:
                q.popleft()
            out[i] = x[q[0]]
        while q and not better(x[q[-1]], x[i]):
            q.pop()
        q.append(i)
    return out


def prior_max(high, n: int) -> array:
    """The highest high of the n bars before bar i (the Donchian channel's top)."""
    return _prior(high, n, lambda kept, new: kept > new)


def prior_min(low, n: int) -> array:
    """The lowest low of the n bars before bar i (the Donchian channel's bottom)."""
    return _prior(low, n, lambda kept, new: kept < new)


def turtle_n(s: Series, n: int = 20) -> array:
    """The Turtles' N (Faith 2003): the average true range, N_i = (19 x N_(i-1) + TR_i) / 20, started with the plain
    average of the first 20 true ranges."""
    out, total, value = _blank(len(s)), 0.0, NAN
    for i in range(1, len(s)):
        h, l, c = s.high[i], s.low[i], s.close[i - 1]
        tr = max(h - l, abs(h - c), abs(l - c))
        if i <= n:
            total += tr
            if i == n:
                value = total / n
        else:
            value = ((n - 1) * value + tr) / n
        if i >= n:
            out[i] = value
    return out


def rsi(close, n: int) -> array:
    """RSI with the desk's own arithmetic (technical.rsi on the closes up to bar i): simple average of the first n
    changes, then Wilder's smoothing."""
    out, g, l = _blank(len(close)), 0.0, 0.0
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        if i <= n:
            if d > 0:
                g += d
            elif d < 0:
                l += -d
            if i == n:
                g /= n
                l /= n
        else:
            up = d if d > 0 else 0
            down = -d if d < 0 else 0
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + down) / n
        if i >= n:
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def ema(x, n: int) -> array:
    """The desk's exponential average (technical.ema), over the whole series."""
    return array("d", technical.ema(list(x), n)) if len(x) else array("d")


def bands(close, n: int, k: float) -> tuple[array, array]:
    """Bollinger's lower and middle bands per bar (technical.bollinger: population standard deviation)."""
    lower, mid = _blank(len(close)), _blank(len(close))
    for i in range(n - 1, len(close)):
        lo, m, _ = technical.bollinger(close[i - n + 1:i + 1], n, k)
        lower[i], mid[i] = lo, m
    return lower, mid


def vol30(close, i: int) -> float | None:
    """The yearly swing at bar i: standard deviation of the last 30 daily log moves x sqrt(365), as trend.signal."""
    if i < trend.VOL_DAYS:
        return None
    moves = [math.log(close[j] / close[j - 1]) for j in range(i - trend.VOL_DAYS + 1, i + 1)]
    return statistics.stdev(moves) * math.sqrt(365)


def _ok(*values) -> bool:
    return all(v == v for v in values)        # nan != nan: "not yet defined" means no decision


# ---- the families ----
@dataclass(frozen=True)
class Family:
    key: str            # "donchian"
    name: str           # "Donchian channel breakout (the Turtles' systems 1 and 2)"
    short: str          # a short name for tables
    cite: str           # the source in a few words, for the phone
    timeframe: str      # "1d" or "4h"
    kind: str           # "state" (long while a condition holds) | "event" (an entry event, an exit event) | "intraday"
    source: str         # author, title, year
    url: str
    rule: str           # one plain-language sentence for the owner
    grid: tuple         # pre-declared settings; the source's headline setting first
    prepare: Callable   # (Series, params) -> {column: array, "_p": params}
    enter: Callable     # (Series, columns, i) -> None or {"stop": price or None, "share": fraction of equity}
    leave: Callable     # (Series, columns, i) -> sell at the next open?
    trigger: Callable | None = None   # intraday: the buy level for bar i (its open and bar i-1 only)


def _slot(stop=None) -> dict:
    return {"stop": stop, "share": SLOT_SHARE}


def _donchian_prepare(s, p):
    return {"hi": prior_max(s.high, p["entry"]), "lo": prior_min(s.low, p["exit"]), "n": turtle_n(s, 20), "_p": p}


def _donchian_enter(s, c, i):
    hi, n, px = c["hi"][i], c["n"][i], s.close[i]
    if not _ok(hi, n) or n <= 0 or not px > hi:
        return None
    stop = px - 2 * n                       # 2 N below the price the decision was made at, then kept
    return {"stop": stop, "share": min(SLOT_SHARE, RISK_PER_TRADE / ((px - stop) / px))}


def _donchian_leave(s, c, i):
    lo = c["lo"][i]
    return _ok(lo) and s.close[i] < lo


def _tsmom_prepare(s, p):
    n, ret = p["lookback"], _blank(len(s))
    for i in range(n, len(s)):
        ret[i] = s.close[i] / s.close[i - n] - 1
    return {"ret": ret, "_p": p}


def _tsmom_enter(s, c, i):
    r = c["ret"][i]
    if not _ok(r) or not r > 0:
        return None
    vol = vol30(s.close, i)
    if vol is None:
        return None
    return {"stop": None, "share": trend.share({"vol": vol}, len(COINS), VOL_TARGET, MAX_POSITION_PCT)}


def _tsmom_leave(s, c, i):
    r = c["ret"][i]
    return _ok(r) and r <= 0


def _sma_prepare(s, p):
    return {"fast": sma(s.close, p["fast"]), "slow": sma(s.close, p["slow"]), "_p": p}


def _sma_enter(s, c, i):
    f, w = c["fast"][i], c["slow"][i]
    return _slot() if _ok(f, w) and f > w else None


def _sma_leave(s, c, i):
    f, w = c["fast"][i], c["slow"][i]
    return _ok(f, w) and f <= w


def _rsi2_prepare(s, p):
    return {"trend": sma(s.close, 200), "rsi": rsi(s.close, 2), "exit": sma(s.close, 5), "_p": p}


def _rsi2_enter(s, c, i):
    t, r = c["trend"][i], c["rsi"][i]
    return _slot() if _ok(t, r) and s.close[i] > t and r < c["_p"]["below"] else None


def _rsi2_leave(s, c, i):
    e = c["exit"][i]
    return _ok(e) and s.close[i] > e


def _bollinger_prepare(s, p):
    lower, mid = bands(s.close, p["period"], p["width"])
    return {"trend": sma(s.close, 200), "lower": lower, "mid": mid, "_p": p}


def _bollinger_enter(s, c, i):
    t, lo = c["trend"][i], c["lower"][i]
    return _slot() if _ok(t, lo) and s.close[i] > t and s.close[i] < lo else None


def _bollinger_leave(s, c, i):
    m = c["mid"][i]
    return _ok(m) and s.close[i] > m


def _macd_prepare(s, p):
    fast, slow = ema(s.close, p["fast"]), ema(s.close, p["slow"])
    line = array("d", (a - b for a, b in zip(fast, slow)))
    return {"line": line, "signal": ema(line, p["signal"]), "_p": p}


def _macd_on(c, i) -> bool:
    line, sig = c["line"][i], c["signal"][i]
    return line > sig and (line > 0 or not c["_p"]["zero"])


def _macd_enter(s, c, i):
    return _slot() if _macd_on(c, i) else None


def _macd_leave(s, c, i):
    return not _macd_on(c, i)


def _williams_prepare(s, p):
    return {"_p": p}


def _williams_trigger(s, c, i):
    if i < 1:
        return None
    return s.open[i] + c["_p"]["k"] * (s.high[i - 1] - s.low[i - 1])


DONCHIAN = Family(
    "donchian", "Donchian channel breakout (the Turtles' systems 1 and 2)", "Donchian / Turtle", "Turtles, Faith 2003",
    "1d", "event",
    "Curtis Faith, The Original Turtle Trading Rules (2003); Way of the Turtle (McGraw-Hill, 2007)",
    "https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf",
    "Buy after a close above the highest high of the last 20 (or 55) days, with a stop 2 N (the Turtles' average "
    "daily range) below; sell after a close below the lowest low of the last 10 (or 20) days.",
    ({"entry": 20, "exit": 10}, {"entry": 55, "exit": 20}),
    _donchian_prepare, _donchian_enter, _donchian_leave)
TSMOM = Family(
    "tsmom", "Time-series momentum", "Time-series momentum", "Moskowitz, Ooi, Pedersen 2012", "1d", "state",
    "Moskowitz, Ooi, Pedersen, Time Series Momentum, Journal of Financial Economics 104(2), 2012; for crypto: "
    "Liu, Tsyvinski, Risks and Returns of Cryptocurrency, Review of Financial Studies 34(6), 2021",
    "https://doi.org/10.1016/j.jfineco.2011.11.003",
    "Hold a coin while its price is higher than it was 4 weeks (or 2 weeks, 3 or 6 months) ago, otherwise cash; "
    "wild coins get a smaller share (sized by their 30-day swing, as the paper scales by volatility).",
    ({"lookback": 28}, {"lookback": 14}, {"lookback": 91}, {"lookback": 182}),
    _tsmom_prepare, _tsmom_enter, _tsmom_leave)
SMA_CROSS = Family(
    "sma_cross", "Moving-average crossover", "Moving averages", "Brock, Lakonishok, LeBaron 1992", "1d", "state",
    "Brock, Lakonishok, LeBaron, Simple Technical Trading Rules and the Stochastic Properties of Stock Returns, "
    "Journal of Finance 47(5), 1992; for crypto: Corbet et al., Finance Research Letters 31, 2019",
    "https://doi.org/10.1111/j.1540-6261.1992.tb04681.x",
    "Hold a coin while its short daily average is above its long one (the paper's five pairs: 1/50, 1/150, 5/150, "
    "1/200 and 2/200 days; a 1-day average is the close itself), otherwise cash.",
    ({"fast": 1, "slow": 50}, {"fast": 1, "slow": 150}, {"fast": 5, "slow": 150}, {"fast": 1, "slow": 200},
     {"fast": 2, "slow": 200}),
    _sma_prepare, _sma_enter, _sma_leave)
RSI2 = Family(
    "rsi2", "Connors' RSI(2) mean reversion", "RSI(2) dips", "Connors & Alvarez 2008", "1d", "event",
    "Larry Connors, Cesar Alvarez, Short Term Trading Strategies That Work (TradingMarkets, 2008); rules as "
    "summarised by StockCharts ChartSchool",
    "https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2",
    "Buy a dip: the 2-day RSI closes below 10 (or 5) while the price is above its 200-day average; sell after a "
    "close above the 5-day average. No stop (the source has none).",
    ({"below": 10}, {"below": 5}),
    _rsi2_prepare, _rsi2_enter, _rsi2_leave)
BOLLINGER = Family(
    "bollinger", "Bollinger-band mean reversion in an uptrend", "Bollinger bands", "Bollinger 2001", "1d", "event",
    "John Bollinger, Bollinger on Bollinger Bands (McGraw-Hill, 2001), and his rules page",
    "https://www.bollingerbands.com/bollinger-band-rules",
    "Buy after a close below the lower band (20 days, 2 deviations; or his 10/1.9 and 50/2.1) while the price is "
    "above its 200-day average; sell after a close above the middle band. Bollinger warns that a lower-band tag is "
    "not a buy signal by itself: this is the textbook use he cautions against, kept as a control.",
    ({"period": 20, "width": 2.0}, {"period": 10, "width": 1.9}, {"period": 50, "width": 2.1}),
    _bollinger_prepare, _bollinger_enter, _bollinger_leave)
MACD_4H = Family(
    "macd_4h", "MACD (Appel) on 4-hour bars", "MACD 4-hour", "Appel 2005", "4h", "state",
    "Gerald Appel, Technical Analysis: Power Tools for Active Investors (FT Prentice Hall, 2005); settings as "
    "described by StockCharts ChartSchool",
    "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/"
    "macd-moving-average-convergence-divergence-oscillator",
    "On 4-hour bars, hold while the MACD line (12/26, or the more sensitive 5/35) is above its signal line (9, or 5), "
    "in two variants also only while the line is above zero; otherwise cash.",
    ({"fast": 12, "slow": 26, "signal": 9, "zero": False}, {"fast": 5, "slow": 35, "signal": 5, "zero": False},
     {"fast": 12, "slow": 26, "signal": 9, "zero": True}, {"fast": 5, "slow": 35, "signal": 5, "zero": True}),
    _macd_prepare, _macd_enter, _macd_leave)
WILLIAMS = Family(
    "williams", "Larry Williams' volatility breakout", "Williams breakout", "Williams 1999", "1d", "intraday",
    "Larry Williams, Long-Term Secrets to Short-Term Trading (Wiley, 1999; 2nd edition 2011); k values from "
    "secondary descriptions (not the book)",
    "https://www.wiley.com/en-us/Long+Term+Secrets+to+Short+Term+Trading%2C+2nd+Edition-p-9780470915738",
    "Buy during the day when the price rises above the day's open plus half (or 0.75, or all) of yesterday's range, "
    "and sell at the day's close: never held overnight.",
    ({"k": 0.5}, {"k": 0.75}, {"k": 1.0}),
    _williams_prepare, lambda s, c, i: None, lambda s, c, i: False, _williams_trigger)

FAMILIES = (DONCHIAN, TSMOM, SMA_CROSS, RSI2, BOLLINGER, MACD_4H, WILLIAMS)
REFERENCES = ("desk-hourly", "desk-trend")
REFERENCE_NAMES = {"desk-trend": "Desk, daily trend rule", "desk-hourly": "Desk, hourly rules"}


# ---- the simulator ----
def joins(daily: dict[str, Series]) -> dict[str, int]:
    """The day each coin joins the test: WARMUP_DAYS after its first daily candle (00:00 UTC, in ms)."""
    return {sym: (s.time[0] // DAY_MS + WARMUP_DAYS) * DAY_MS for sym, s in daily.items() if len(s)}


def _order(symbols) -> list[str]:
    rank = {c: k for k, c in enumerate(COINS)}
    return sorted(symbols, key=lambda x: (rank.get(x, len(rank)), x))


def _cut_columns(cols: dict, n: int) -> dict:
    return {k: (v[:n] if isinstance(v, array) else v) for k, v in cols.items()}


def _days(start_ms: int, end_ms: int) -> range:
    return range(start_ms, end_ms + 1, DAY_MS)


def simulate(family: Family, params: dict, bars: dict[str, Series], prepared: dict[str, dict],
             joins: dict[str, int], start_ms: int, end_ms: int) -> dict:
    """One account trading `family` with `params` on the days start_ms..end_ms (both included, 00:00 UTC), flat with
    10,000 USDT at the start, everything still open valued at its liquidation value at the end. Returns the Run:
    {"curve": [(day, equity at the day's close)], "base", "trades", "invested", "orders"}."""
    step = INTERVAL_MS[family.timeframe]
    last_bar = end_ms + DAY_MS - step
    coins = [c for c in _order(bars) if c in joins and joins[c] <= end_ms and c in prepared]
    intraday = family.kind == "intraday"
    cash = BALANCE
    pos: dict[str, dict] = {}
    pending: dict[str, tuple] = {}
    trades: list[dict] = []
    curve: list[tuple[int, float]] = []
    invested: list[float] = []
    orders: list[tuple] = []
    marks: dict[str, float] = {}
    ptr = {c: bisect.bisect_left(bars[c].time, start_ms) for c in coins}
    ends = {c: bars[c].upto(last_bar) for c in coins}

    def buy(sym, px, quote, stop, t):
        nonlocal cash
        quote = min(quote, cash)
        if quote <= 1e-9:
            return False
        cash -= quote
        pos[sym] = {"qty": quote * (1 - FEE) / px, "cost": quote, "stop": stop, "opened": t}
        return True

    def sell(sym, px, reason, t):
        nonlocal cash
        p = pos.pop(sym)
        proceeds = p["qty"] * px * (1 - FEE)
        cash += proceeds
        trades.append({"symbol": sym, "entry_ms": p["opened"], "exit_ms": t, "pnl": proceeds - p["cost"],
                       "pnl_pct": (proceeds / p["cost"] - 1) * 100, "reason": reason,
                       "fees": p["cost"] * FEE + p["qty"] * px * FEE})

    def decide(sym, i, equity):
        """At the close of bar i: an order for the next open (if that bar is in the window and the coin has joined)."""
        s = bars[sym]
        if i + 1 >= ends[sym] or s.time[i + 1] < joins[sym]:
            return
        if sym in pos:
            if family.leave(s, prepared[sym], i):
                pending[sym] = ("sell",)
                orders.append((s.time[i], sym, "sell", 0.0, None))
        elif not intraday:
            d = family.enter(s, prepared[sym], i)
            if d:
                quote = min(d["share"], SLOT_SHARE) * equity      # never more than the desk's position cap
                pending[sym] = ("buy", quote, d["stop"])
                orders.append((s.time[i], sym, "buy", quote, d["stop"]))

    for sym in coins:                       # the first decisions: at the close of the bar before the window
        if ptr[sym] >= 1:
            decide(sym, ptr[sym] - 1, BALANCE)
    equity_close = BALANCE
    t = start_ms
    while t <= last_bar:
        active = []
        for sym in coins:
            s, p = bars[sym], ptr[sym]
            while p < ends[sym] and s.time[p] < t:
                p += 1
            if p < ends[sym] and s.time[p] == t:
                active.append((sym, p))
                p += 1
            ptr[sym] = p
        if active:
            for sym, i in active:           # 1. the open: sells first, then buys
                if pending.get(sym, ("",))[0] == "sell" and sym in pos:
                    sell(sym, bars[sym].open[i] * (1 - MARKET_SLIPPAGE), "signal", t)
            for sym, i in active:
                order = pending.pop(sym, None)
                if order and order[0] == "buy" and sym not in pos:
                    buy(sym, bars[sym].open[i] * (1 + MARKET_SLIPPAGE), order[1], order[2], t)
            day_trades = []
            if intraday:                    # 2. a stop-buy at the day's level (known at the open)
                for sym, i in active:
                    if sym in pos or t < joins[sym]:
                        continue
                    s = bars[sym]
                    level = family.trigger(s, prepared[sym], i)
                    if level is not None and s.high[i] >= level:
                        quote = SLOT_SHARE * equity_close
                        orders.append((t, sym, "buy", quote, None))
                        if buy(sym, level * (1 + STOP_FILL_SLIPPAGE), quote, None, t):
                            day_trades.append((sym, i))
            for sym, i in active:           # 3. stops, on the bar's low
                p = pos.get(sym)
                if p is not None and p["stop"] is not None and bars[sym].low[i] <= p["stop"]:
                    s = bars[sym]
                    sell(sym, min(s.open[i], p["stop"]) * (1 - STOP_FILL_SLIPPAGE), "stop-loss", t)
            for sym, i in day_trades:       # 4. the intraday rule sells at the close
                if sym in pos:
                    sell(sym, bars[sym].close[i] * (1 - MARKET_SLIPPAGE), "close", t)
            for sym, i in active:           # 5. the close
                marks[sym] = bars[sym].close[i]
            equity_close = cash + sum(p["qty"] * marks[sym] for sym, p in pos.items())
            for sym, i in active:           # 6. decisions for the next open
                decide(sym, i, equity_close)
        if (t + step) % DAY_MS == 0:        # the day's last bar: one equity point a day
            value = sum(p["qty"] * marks[sym] for sym, p in pos.items())
            equity = cash + value
            curve.append((t + step - DAY_MS, equity))
            invested.append(value / equity if equity > 0 else 0.0)
        t += step
    for sym in list(pos):                   # the window's end: valued at what selling would bring
        sell(sym, marks[sym] * (1 - MARKET_SLIPPAGE), "end of window", end_ms)
    if curve:
        curve[-1] = (curve[-1][0], cash)
    return {"curve": curve, "base": BALANCE, "trades": trades, "invested": invested, "orders": orders}


def cash_run(start_ms: int, end_ms: int) -> dict:
    """Doing nothing: 10,000 USDT every day."""
    days = list(_days(start_ms, end_ms))
    return {"curve": [(d, BALANCE) for d in days], "base": BALANCE, "trades": [], "invested": [0.0] * len(days)}


def hold(daily: dict[str, Series], joins: dict[str, int], start_ms: int, end_ms: int,
         coins: tuple[str, ...] | None = None) -> dict:
    """Buy and hold: an equal slice (BALANCE / number of coins) of each coin at the window's first open, or at the
    coin's join day when it joins later (until then the slice waits in cash); never rebalanced; same costs."""
    coins = tuple(coins or COINS)
    each = BALANCE / len(coins)
    cash, pos, marks, curve, invested, trades = BALANCE, {}, {}, [], [], []
    held = [c for c in coins if c in daily and c in joins]
    for d in _days(start_ms, end_ms):
        for sym in held:
            s = daily[sym]
            i = s.upto(d) - 1
            if i < 0 or s.time[i] != d:
                continue
            if sym not in pos and d >= joins[sym]:
                quote = min(each, cash)
                px = s.open[i] * (1 + MARKET_SLIPPAGE)
                pos[sym] = {"qty": quote * (1 - FEE) / px, "cost": quote, "opened": d}
                cash -= quote
            marks[sym] = s.close[i]
        value = sum(p["qty"] * marks[sym] for sym, p in pos.items())
        curve.append((d, cash + value))
        invested.append(value / (cash + value) if cash + value > 0 else 0.0)
    for sym, p in pos.items():
        px = marks[sym] * (1 - MARKET_SLIPPAGE)
        proceeds = p["qty"] * px * (1 - FEE)
        cash += proceeds
        trades.append({"symbol": sym, "entry_ms": p["opened"], "exit_ms": end_ms, "pnl": proceeds - p["cost"],
                       "pnl_pct": (proceeds / p["cost"] - 1) * 100, "reason": "end of window",
                       "fees": p["cost"] * FEE + p["qty"] * px * FEE})
    if curve:
        curve[-1] = (curve[-1][0], cash)
    return {"curve": curve, "base": BALANCE, "trades": trades, "invested": invested}


# ---- the desk's own two strategies (references: their settings were chosen on some of these years) ----
def _closed(rows) -> list[dict]:
    return [{"symbol": "", "entry_ms": None, "exit_ms": t, "pnl": pnl, "pnl_pct": pct, "reason": reason, "fees": fees}
            for t, pnl, pct, reason, fees in rows]


def desk_trend(daily: dict[str, Series], joins: dict[str, int], start_ms: int, end_ms: int) -> dict:
    """The daily trend rule (trader/trend.py) with its tested defaults, one continuous replay under the desk's Risk
    Manager and its overall loss brake. Each coin's candles start trend.window(50) days before its join day, so its
    first signal is on the close of the day before it joins and its first buy at the join day's open."""
    s = Settings(offline=True, strategy="trend", symbols=list(_order(daily)), max_open_positions=len(COINS),
                 starting_balance=BALANCE)
    need = trend.window(trend.params(s)[0])
    history = {}
    for sym in _order(daily):
        if sym not in joins:
            continue
        series = daily[sym]
        first = series.upto(joins[sym] - need * DAY_MS - 1)
        last = series.upto(end_ms)
        history[sym] = series.dicts(first)[: last - first]
    r = trend.backtest(s, history, start_ms=start_ms - DAY_MS, end_ms=end_ms, keep_curve=True)
    return {"curve": r["curve"], "base": BALANCE, "trades": _closed(r["closed"]), "invested": None}


def desk_hourly(hourly: dict[str, Series], start_ms: int, end_ms: int) -> dict:
    """The hourly agents' rule core (trader/backtest.py) with the tested default card: 3 slots, 10% cap, 1% risk,
    the checklist, trailing stops; news and forex neutral and no Claude, as in every replay. Its last hour is the
    period's last finished day; the equity is sampled at each UTC day's last hour."""
    from .agents.decision import DecisionAgent
    from .strategy import StrategyCard
    s = Settings(offline=True, symbols=list(_order(hourly)), starting_balance=BALANCE)
    hist = {sym: hourly[sym].dicts() for sym in _order(hourly)}
    r = Backtest(s, hist, decision=DecisionAgent.for_card(StrategyCard("defaults")), days=HOURLY_DAYS,
                 btc_history=hist.get("BTCUSDT"), end_ms=end_ms + 23 * 3_600_000, keep_curve=True).run()
    hist.clear()
    days: dict[int, float] = {}
    for t, e in r.get("curve", []):
        days[t // DAY_MS * DAY_MS] = e
    return {"curve": sorted(days.items()), "base": BALANCE, "trades": _closed(r.get("closed", [])), "invested": None}


# ---- the protocol ----
def _choice_rows(family: Family, bars, prepared: list[dict], joins, a: int, b: int) -> list[dict]:
    """Every grid setting simulated on the choice window a..b, with prices and indicators cut at b's end: the choice
    sees nothing that happened later."""
    cut = {sym: s.cut(b + DAY_MS - 1) for sym, s in bars.items()}
    rows = []
    for k, params in enumerate(family.grid):
        cols = {sym: _cut_columns(prepared[k][sym], len(cut[sym])) for sym in cut}
        run = simulate(family, params, cut, cols, joins, a, b)
        r = st.daily_returns(run, a, b)
        half = len(r) // 2
        m = st.metrics(run, a, b)
        rows.append({"index": k, "params": params, "sharpe": m["sharpe"], "sharpe_daily": m["sharpe_daily"],
                     "halves": [st.annual(st.sharpe(r[:half])), st.annual(st.sharpe(r[half:]))],
                     "trades": m["rule_trades"], "return_pct": m["return_pct"],
                     "max_drawdown_pct": m["max_drawdown_pct"], "eligible": False})
    return rows


def _public_row(row: dict) -> dict:
    return {k: row[k] for k in ("params", "sharpe", "halves", "trades", "return_pct", "max_drawdown_pct", "eligible")}


def run_family(family: Family, bars: dict[str, Series], daily: dict[str, Series], joins: dict[str, int], P: dict,
               variance: float | None = None, should_stop=None) -> dict:
    """The whole protocol for one family: in-sample rows for every setting, the choice, the holdout judged once, the
    walk-forward (a choice per segment, then its test), stitched into one record. `daily` is unused by the families
    themselves (their own bars are `bars`); it is accepted for the references' sake and kept for symmetry."""
    prepared = [{sym: family.prepare(s, params) for sym, s in bars.items()} for params in family.grid]
    a, b = P["in_sample"]
    rows = _choice_rows(family, bars, prepared, joins, a, b)
    in_days = (b - a) // DAY_MS + 1
    chosen = st.choose(rows, in_days / st.DAYS_PER_YEAR)
    holdout = None
    if chosen is not None:
        ha, hb = P["holdout"]
        holdout = st.metrics(simulate(family, chosen["params"], bars, prepared[chosen["index"]], joins, ha, hb), ha, hb)
    seg_runs, choices = [], []
    for ca, cb, ta, tb in P["segments"]:
        if should_stop and should_stop():
            raise KeyboardInterrupt("stopped")
        seg_rows = _choice_rows(family, bars, prepared, joins, ca, cb)
        pick = st.choose(seg_rows, ((cb - ca) // DAY_MS + 1) / st.DAYS_PER_YEAR)
        if pick is None:
            seg_runs.append(cash_run(ta, tb))
        else:
            seg_runs.append(simulate(family, pick["params"], bars, prepared[pick["index"]], joins, ta, tb))
        choices.append({"segment": [ta, tb], "params": None if pick is None else pick["params"]})
    walk = None
    if seg_runs:
        walk = st.metrics(st.stitch(seg_runs, BALANCE), P["segments"][0][2], P["segments"][-1][3])
    prepared.clear()
    entry = {"key": family.key, "kind": "family", "name": family.name, "short": family.short, "cite": family.cite,
             "timeframe": family.timeframe, "style": family.kind, "source": family.source, "url": family.url,
             "rule": family.rule,
             "grid": list(family.grid), "in_sample_rows": [_public_row(r) for r in rows],
             "in_sample_sharpes": [r["sharpe_daily"] for r in rows], "min_trades": st.min_trades(in_days),
             "chosen": None if chosen is None else {"params": chosen["params"], "index": chosen["index"]},
             "holdout": holdout, "walk_forward": walk, "choices": choices}
    if variance is not None and walk is not None:
        entry["dsr"] = st.dsr_of(walk, st.trials(), variance)
        entry["luck_in_100"] = st.luck_in_100(entry["dsr"])
    return entry


def _day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def mem_available_mb() -> float | None:
    """Free memory (MemAvailable in /proc/meminfo) in MB; None where the system does not say (then it runs)."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def usage() -> tuple[float, float]:
    """(CPU seconds used so far, peak memory in MB) of this process."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    peak = r.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    return r.ru_utime + r.ru_stime, peak


def _yardsticks(daily, J, P) -> dict:
    out = {}
    for key, make in (("hold", lambda a, b: hold(daily, J, a, b)),
                      ("btc", lambda a, b: hold(daily, J, a, b, ("BTCUSDT",))),
                      ("cash", cash_run)):
        seg = {"in_sample": st.metrics(make(*P["in_sample"]), *P["in_sample"]),
               "holdout": st.metrics(make(*P["holdout"]), *P["holdout"])}
        if P["segments"]:
            a, b = P["segments"][0][2], P["segments"][-1][3]
            runs = [make(ta, tb) for _, _, ta, tb in P["segments"]]
            # The verdicts' bar (pre-declared): restarted with every segment like the rules, so for holding it is an
            # equal-weight basket re-split every 6 months. "walk_forward_once": bought once over the same dates, for
            # information only (no verdict reads it).
            seg["walk_forward"] = st.metrics(st.stitch(runs, BALANCE), a, b)
            if key != "cash":
                seg["walk_forward_once"] = st.metrics(make(a, b), a, b)
        out[key] = seg
    return out


def _reference(key: str, run: dict, window: tuple[int, int], P: dict, variance: float) -> dict:
    a, b = window
    full = st.metrics(run, a, b)
    ha, hb = max(P["holdout"][0], a), P["holdout"][1]
    entry = {"key": key, "kind": "reference", "name": REFERENCE_NAMES[key], "window": [_day(a), _day(b)],
             "full": full, "holdout": st.metrics(run, ha, hb), "trials": st.DESK_TRIALS[key],
             "verdict": "REFERENCE"}
    if P["in_sample"][1] >= a:
        entry["in_sample"] = st.metrics(run, max(P["in_sample"][0], a), P["in_sample"][1])
    entry["dsr"] = st.dsr_of(full, st.DESK_TRIALS[key], variance)
    entry["luck_in_100"] = st.luck_in_100(entry["dsr"])
    return entry


def run_library(data: dict, hourly_skip: str | None, progress=None, free_mb=None, families=FAMILIES) -> dict:
    """The whole protocol, in order (cheap first, the heaviest last, memory freed in between):
    yardsticks; every family (choice, holdout, walk-forward); the luck spread V and the verdicts; the daily trend
    reference; then, if memory allows, the hourly reference. Returns the report (no per-day series in it)."""
    from .library_report import CAVEATS, skipped_text
    say = progress or (lambda msg: None)
    started = time.monotonic()
    summary = data.get("meta") or meta(data)
    daily = {sym: data["1d"][sym] for sym in _order(data["1d"])}
    J = {sym: d for sym, d in joins(daily).items() if d <= daily[sym].time[-1]}
    last_day = min(daily[sym].time[-1] for sym in J)
    P = st.periods(min(J.values()), last_day)
    say(f"Period {_day(P['start'])} to {_day(P['end'])} ({P['days']} days): choose on {_day(P['in_sample'][0])} to "
        f"{_day(P['in_sample'][1])}, judge on {_day(P['holdout'][0])} to {_day(P['holdout'][1])}, "
        f"{len(P['segments'])} walk-forward segments")
    yard = _yardsticks(daily, J, P)
    entries = []
    for family in families:
        clock = time.monotonic()
        bars = daily if family.timeframe == "1d" else {s: data[family.timeframe][s] for s in _order(data[family.timeframe])
                                                        if s in J}
        entries.append(run_family(family, bars, daily, J, P))
        say(f"{family.name}: {len(family.grid)} settings, in-sample, holdout, {len(P['segments'])} walk-forward "
            f"segments ... {time.monotonic() - clock:.1f} s")
    V = st.trial_variance([x for e in entries for x in e.pop("in_sample_sharpes")])
    N = st.trials()
    bench = {"holdout": yard["hold"]["holdout"], "walk_forward": yard["hold"].get("walk_forward")}
    for e in entries:
        e["dsr"] = st.dsr_of(e["walk_forward"], N, V) if e["walk_forward"] else 0.0
        e["luck_in_100"] = st.luck_in_100(e["dsr"])
        e["verdict"], e["reasons"] = st.verdict(e, bench, N, V)
    clock = time.monotonic()
    entries.append(_reference("desk-trend", desk_trend(daily, J, P["start"], P["end"]), (P["start"], P["end"]), P, V))
    say(f"Desk, daily trend rule (reference) ... {time.monotonic() - clock:.1f} s")
    data["4h"] = {}                # the 4-hour candles are freed (columns hold no cycles: freed at once)
    free = (free_mb or mem_available_mb)()
    hourly = data.get("1h") or {}
    window = (P["end"] - (HOURLY_DAYS - 1) * DAY_MS, P["end"])
    skip = None
    if hourly_skip:
        skip = skipped_text(hourly_skip)
    elif free is not None and free < HOURLY_MIN_FREE_MB:
        skip = skipped_text("memory", free)
    elif not hourly or any(sym not in hourly for sym in J):
        skip = skipped_text("prices")
    if skip:
        entries.append({"key": "desk-hourly", "kind": "reference", "name": REFERENCE_NAMES["desk-hourly"],
                        "verdict": "SKIPPED", "note": skip, "trials": st.DESK_TRIALS["desk-hourly"],
                        "window": [_day(window[0]), _day(window[1])]})
        say(f"Desk, hourly rules (reference): {skip}")
    else:
        clock = time.monotonic()
        run = desk_hourly({sym: hourly[sym] for sym in _order(hourly) if sym in J}, *window)
        entries.append(_reference("desk-hourly", run, window, P, V))
        say(f"Desk, hourly rules (reference) ... {time.monotonic() - clock:.1f} s")
    data["1h"] = {}
    cpu, peak = usage()
    return {
        "protocol": st.PROTOCOL_VERSION, "trials": N, "trials_before": st.TRIALS_BEFORE,
        "version": _version(), "started_at": "", "finished_at": "",
        "seconds": round(time.monotonic() - started, 1), "cpu_seconds": round(cpu, 1), "peak_mb": round(peak),
        "requests": int(data.get("requests", 0)),
        "universe": sorted(({"symbol": sym, "first": _day(daily[sym].time[0]), "joins": _day(J[sym])} for sym in J),
                           key=lambda u: (u["joins"], _order([u["symbol"]]))),
        "data": summary,
        "period": {"start": _day(P["start"]), "end": _day(P["end"]), "days": P["days"]},
        "in_sample": [_day(x) for x in P["in_sample"]], "holdout": [_day(x) for x in P["holdout"]],
        "segments": [[_day(x) for x in seg] for seg in P["segments"]],
        "trial_variance": V, "luck_bar": st.annual(st.expected_max_sharpe(N, V)),
        "costs": {"fee": FEE, "market_slippage": MARKET_SLIPPAGE, "stop_slippage": STOP_FILL_SLIPPAGE},
        "pass_history": {},
        "yardsticks": yard,
        "entries": [_dates(e) for e in entries],
        "caveats": list(CAVEATS),
    }


def _dates(entry: dict) -> dict:
    """Segment times as dates, for the report."""
    for c in entry.get("choices", []):
        c["segment"] = [_day(x) for x in c["segment"]]
    return entry


def _version() -> str:
    try:
        from .updater import version_string
        return version_string()
    except Exception:  # noqa: BLE001 - a version label must never stop a run
        from . import __version__
        return __version__


def meta(data: dict) -> dict:
    """What was downloaded (per candle size and coin: first and last candle, count), noted before the series are
    freed."""
    return {iv: {sym: {"first": _day(s.time[0]), "last": _day(s.time[-1]), "candles": len(s)}
                 for sym, s in (data.get(iv) or {}).items() if len(s)} for iv in ("1d", "4h", "1h")}


# ---- python -m trader library ----
BUSY_EXIT = 3


def _low_priority() -> None:
    """The lowest CPU priority, and the first process the system stops if memory runs out (the desk comes first)."""
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError:
            pass
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("1000")
    except OSError:
        pass


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save(s: Settings, report: dict) -> list[dict]:
    """Store the result in the journal (never creating one: a new journal would start a new account)."""
    from .journal import Journal
    from .library_report import diary_line
    j = Journal(s.db_path, s.starting_balance)
    try:
        runs = list(j.get("library_runs") or [])
        best = max((e for e in report["entries"] if e["kind"] == "family" and e.get("walk_forward")),
                   key=lambda e: e["walk_forward"]["sharpe_daily"], default=None)
        runs.append({"ts": report["finished_at"], "protocol": report["protocol"], "end": report["period"]["end"],
                     "passed": [e["key"] for e in report["entries"] if e["verdict"] == "PASS"],
                     "best": None if best is None else {"key": best["key"], "wf_sharpe": best["walk_forward"]["sharpe"],
                                                        "luck_in_100": best["luck_in_100"]}})
        runs = runs[-20:]
        mine = [r for r in runs if r.get("protocol") == report["protocol"]]
        report["pass_history"] = {e["key"]: [sum(1 for r in mine if e["key"] in r.get("passed", [])), len(mine)]
                                  for e in report["entries"] if e["kind"] == "family"}
        j.set("library_runs", runs)
        j.set("library_report", report)
        j.event("library", diary_line(report))
        return runs
    finally:
        j.db.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="trader library", description="Published trading rules and the desk's own, "
                                 "tested on years of real prices under one fixed protocol. It trades nothing.")
    ap.add_argument("--no-hourly", action="store_true", help="leave out the desk's hourly reference (saves about 150 MB "
                    "of memory, two thirds of the time and the hourly downloads)")
    ap.add_argument("--refresh", action="store_true", help="download every price again instead of using the cache")
    ap.add_argument("--save", action="store_true", help="store the result in the journal (for /notes and /library last)")
    ap.add_argument("--quiet", action="store_true", help="no progress lines")
    ap.add_argument("--low-priority", action="store_true", help="lowest CPU priority; first to stop if memory runs out")
    ap.add_argument("--report", default=None, help="where to write the JSON report (default data/library/report.json)")
    args = ap.parse_args(argv)
    if args.low_priority:
        _low_priority()
    import fcntl
    from .library_report import library_page, table_text
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    say = (lambda msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
    s = Settings.load()
    directory = cache_dir(s)
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / "run.lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Another library run is in progress.", file=sys.stderr)
            return BUSY_EXIT
        first = not (directory / "candles").exists()
        say(f"Downloading prices: {len(COINS)} coins x 1d, 4h" + ("" if args.no_hourly else ", 1h")
            + (" (first run: about 430 requests, 2-4 minutes) ..." if first else " (only what is new) ..."))
        client = BinanceClient(PUBLIC_BASE_URL)
        data, used = universe(client, directory, COINS, refresh=args.refresh, hourly=not args.no_hourly, progress=say)
        data["requests"], data["meta"] = used, meta(data)
        say(f"{used} price requests")
        report = run_library(data, "--no-hourly" if args.no_hourly else None, say)
        data.clear()
        report["started_at"] = started
        report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        runs = []
        if args.save and Path(s.db_path).exists():
            try:
                runs = _save(s, report)
            except Exception as e:  # noqa: BLE001 - a busy or damaged journal must not throw away a finished run
                report["not_saved"] = str(e)[:200]
                print(f"The result is written to files, but the journal could not be updated ({e}): /library last "
                      "and the notebook still show the previous run.", file=sys.stderr)
        elif args.save:
            say("No journal yet (the desk has not run on this computer): the result is written to files only.")
        target = Path(args.report) if args.report else directory / "report.json"
        _write_atomic(target, json.dumps(report, indent=1, default=str))
        _write_atomic(directory / "LIBRARY.md", library_page(report, runs).rstrip() + "\n")
        if not args.quiet:
            print(table_text(report))
            print(f"\nReport: {target}\nNotebook page: {directory / 'LIBRARY.md'}")
        return 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - one clear last line for the owner (the Telegram waiter quotes it)
        import traceback
        traceback.print_exc()
        print(f"Strategy library failed: {e}", file=sys.stderr)
        return 1
    finally:
        lock.close()
