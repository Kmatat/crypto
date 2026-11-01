"""The chart reader (trader/agents/chart.py): regimes, events, levels, the journal table, the cycle, the `regime`
checklist rule live and in the replay, and the owner's texts."""
import json
import math
import re
import time

import pytest

from trader import setup_wizard
from trader.__main__ import HELP_TEXT, help_text
from trader.agents import decision as dmod
from trader.agents import mentor as mentor_module
from trader.agents.chart import (DAY_MS, FOUR_H_MS, HOUR_MS, KINDS, RECENT_BARS, ChartEventsAgent, aggregate,
                                 atr_series, efficiency_ratio, finished_daily, phrase, regime, rsi_series)
from trader.agents.execution import ExecutionAgent
from trader.agents.mentor import ALL_CHECKS, MentorAgent
from trader.agents.technical import atr, ema, rsi
from trader.backtest import Backtest, DailySeries
from trader.config import Settings
from trader.journal import Journal
from trader.notes import Notes
from trader.notify import CHART_TREND, NO_CHART_YET, chart_daily_line, chart_text
from trader.orchestrator import Orchestrator
from trader.report import status_text
from trader.strategy import StrategyCard
from tests.test_backtest import synthetic
from tests.test_protection import FakeBroker

H = HOUR_MS


# ---- helpers ----
def bars(closes, start_hour=0, volume=100.0, spread=0.004, volumes=None):
    """Hourly candles from a list of closes: each opens at the previous close, highs and lows a little around them."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"time": (start_hour + i) * H, "open": prev, "high": max(prev, c) * (1 + spread),
                    "low": min(prev, c) * (1 - spread), "close": c, "volume": volumes[i] if volumes else volume})
        prev = c
    return out


def flat(n, price):
    return [float(price)] * n


def trend(n, start, step):
    return [start + i * step for i in range(n)]


def wavy(n, amp=0.5):
    return [100 + amp * math.sin(i / 3) for i in range(n)]


def kinds(candles, swing=48):
    """The event kinds on the last candle, as the agent evaluates them."""
    closes = [c["close"] for c in candles]
    events = ChartEventsAgent._events_at(candles, len(candles) - 1, closes, ema(closes, 12), ema(closes, 26),
                                         rsi_series(closes), atr_series(candles), swing)
    return {e["kind"]: e["details"] for e in events}


class FakeMarket:
    """A market that serves fixed finished candles and daily rows, and records every daily download."""
    interval = "1h"
    client = None

    def __init__(self, series: dict[str, list[dict]], daily: dict[str, list[float]] | None = None):
        self.series = series
        self.daily = daily or {}
        self.downloads: list[str] = []     # every daily download, in order (the request budget)
        self._fresh: set[str] = set()      # coins whose daily rows are "less than an hour old" (the real cache)
        self.aside_downloads: list[str] = []   # the downloads into the records' own copy (daily_rows_aside)
        self._aside_fresh: set[str] = set()    # that copy: never read by daily_rows / daily_cached

    def candles(self, sym, limit=200):
        return self.series[sym][-limit:]

    def closed(self, candles):
        return candles

    def _rows(self, sym):
        """Daily rows as Binance serves them: the finished days end the day before the newest candle's day, and the
        forming day (that candle's day) comes last with the current price as its close."""
        closes = self.daily.get(sym) or []
        last = self.series[sym][-1]
        day = last["time"] - last["time"] % DAY_MS
        rows = [{"time": day - (len(closes) - k) * DAY_MS, "close": c} for k, c in enumerate(closes)]
        rows.append({"time": day, "close": last["close"]})
        return rows

    def daily_rows(self, sym, limit=260):
        if sym not in self._fresh:
            self.downloads.append(sym)
            self._fresh.add(sym)
        return self._rows(sym)

    def daily_cached(self, sym):
        return self._rows(sym) if sym in self._fresh else []

    def daily_closes(self, sym, limit=260):
        return [r["close"] for r in self.daily_rows(sym, limit)]

    def daily_rows_aside(self, sym, limit=260, download=True):
        if sym not in self._aside_fresh:
            if not download:
                return []
            self.downloads.append(sym)
            self.aside_downloads.append(sym)
            self._aside_fresh.add(sym)
        return self._rows(sym)

    def price(self, sym):
        return self.series[sym][-1]["close"]

    def range_since(self, sym, since_ms):
        return None


def desk(series, daily=None, checks=(), **settings):
    s = Settings(offline=True, symbols=list(series), mentor_checks=tuple(checks), **settings)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.market = FakeMarket(series, daily)
    bot.broker = FakeBroker(lambda sym: bot.last_price[sym])
    bot.execution = ExecutionAgent(bot.broker)
    return bot


def cycle_series():
    """200 aligned hourly candles: a gentle drift down, then four strong up candles, so the newest finished hourly
    bar AND the newest finished 4-hour bar (196:00-199:00) both close above their recent highs."""
    return bars(trend(196, 100.0, -0.01) + [100.5, 101.0, 101.5, 102.0])


def force_buy(bot):
    bot.decision.run = lambda sym, reports, holding, use_ai=True, threshold=None: {
        "action": "BUY", "confidence": 0.9, "reasoning": "r", "source": "rules", "combined_score": 0.5}


# ---- 1-4: bars, series, regimes ----
def test_four_hour_bars_align_to_utc_boundaries_and_drop_partials():
    c = bars([100 + math.sin(i / 5) for i in range(200)], start_hour=1)
    b = aggregate(c, FOUR_H_MS)
    assert len(b) == 49 and b[0]["time"] == 4 * H and b[-1]["time"] == 196 * H   # 01-03 partial dropped, 197-200 too
    for bar in b:
        four = c[bar["time"] // H - 1: bar["time"] // H + 3]
        assert [x["time"] for x in four] == [bar["time"] + k * H for k in range(4)]
        assert bar["open"] == four[0]["open"] and bar["close"] == four[-1]["close"]
        assert bar["high"] == max(x["high"] for x in four) and bar["low"] == min(x["low"] for x in four)
        assert bar["volume"] == pytest.approx(sum(x["volume"] for x in four))
    assert len(aggregate(bars(flat(192, 100), start_hour=0), FOUR_H_MS)) == 48
    assert len(aggregate(bars(flat(192, 100), start_hour=1), FOUR_H_MS)) == 47
    a = ChartEventsAgent("1h")
    for short in (c[:10], []):
        r = a.run(short)
        assert r["timeframes"] == {"1h": None, "4h": None, "1d": None} and r["events"] == []
        assert r["latest_event"] is None and r["summary"] == "not enough finished candles yet"
    four_h = aggregate(bars([100 + math.sin(i / 9) for i in range(2000)]), FOUR_H_MS)
    r4 = ChartEventsAgent("4h").run(four_h)
    assert set(r4["timeframes"]) == {"4h", "1d"} and r4["timeframes"]["4h"]["swing_bars"] == 30
    two_h = aggregate(bars([100 + math.sin(i / 9) for i in range(400)]), 2 * H)
    r2 = ChartEventsAgent("2h").run(two_h)
    assert r2["timeframes"]["2h"]["swing_bars"] == 24 and r2["timeframes"]["2h"]["swing_hours"] == 48


def test_two_hour_base_builds_4h_bars_too():
    two_h = aggregate(bars([100 + math.sin(i / 9) for i in range(400)]), 2 * H)
    assert len(two_h) == 200 and two_h[1]["time"] - two_h[0]["time"] == 2 * H
    r = ChartEventsAgent("2h").run(two_h)
    assert r["interval"] == "2h" and set(r["timeframes"]) == {"2h", "4h", "1d"}
    assert r["timeframes"]["4h"]["bars"] == 96                     # 192 two-hour candles = 96 whole 4h bars
    assert r["timeframes"]["4h"]["swing_bars"] == 30 and r["timeframes"]["4h"]["swing_hours"] == 120
    assert "4h" not in {k for k, v in ChartEventsAgent("4h").run(aggregate(two_h, FOUR_H_MS))["timeframes"].items()
                        if k != "4h"}   # a 4h base has the 4h frame as its only (base) frame


def test_rsi_and_atr_series_match_the_technical_agent():
    c = bars([100 + 3 * math.sin(i / 4) + 0.7 * math.cos(i / 1.3) for i in range(120)])
    closes = [x["close"] for x in c]
    assert rsi_series(closes)[-1] == pytest.approx(rsi(closes), abs=1e-9)
    assert atr_series(c)[-1] == pytest.approx(atr(c), abs=1e-9)
    assert rsi_series(closes)[:14] == [50.0] * 14 and atr_series(c)[:14] == [None] * 14
    assert rsi_series(closes[:30])[-1] == pytest.approx(rsi(closes[:30]), abs=1e-9)


def test_regime_labels_and_direction():
    a = ChartEventsAgent("1h")
    f = a.run(bars(trend(60, 100.0, 1.0)))["timeframes"]["1h"]
    assert f["regime"] == "impulsive" and f["direction"] == 1 and f["er"] == 1.0
    f = a.run(bars([100 + (i % 2) for i in range(60)]))["timeframes"]["1h"]            # saw-tooth: back and forth
    assert f["regime"] == "chop" and f["er"] == 0.0
    f = a.run(bars([100 + i * 0.3 + 1.5 * math.sin(i / 2) for i in range(60)]))["timeframes"]["1h"]
    assert f["regime"] == "trending" and f["direction"] == 1 and 0.30 <= f["er"] < 0.60
    f = a.run(bars(trend(60, 100.0, 0.01)))["timeframes"]["1h"]   # straight, but 20 bars move 0.2 against an ATR ~0.8
    assert f["regime"] == "trending" and f["er"] == 1.0
    down = a.run(bars(trend(60, 200.0, -1.0)))["timeframes"]["1h"]
    assert down["regime"] == "impulsive" and down["direction"] == -1
    assert regime(flat(20, 100), 1.0) is None and efficiency_ratio(flat(20, 100)) is None
    assert efficiency_ratio(flat(30, 100)) == 0.0 and regime(flat(30, 100), 0.0)["label"] == "chop"


# ---- 5-9: each event kind fires exactly when it should ----
def test_ema_cross_fires_only_on_the_crossing_bar():
    c = bars(trend(100, 200.0, -0.5) + trend(100, 150.0, 0.8))
    closes = [x["close"] for x in c]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    crosses = [(i, e["kind"]) for i in range(1, len(c))
               for e in ChartEventsAgent._events_at(c, i, closes, e12, e26, rsi_series(closes), atr_series(c), 48)
               if e["kind"].startswith("cross")]
    ups = [i for i, k in crosses if k == "cross_up"]
    assert len(ups) == 1
    i = ups[0]
    assert e12[i] > e26[i] and e12[i - 1] <= e26[i - 1] and all(e12[k] > e26[k] for k in range(i, len(c)))
    assert [k for _, k in crosses if k == "cross_down"] == ["cross_down"]   # once, when the fall began


def test_breakout_and_breakdown_use_the_previous_window():
    k = kinds(bars(flat(60, 100) + [101.0]))
    assert "breakout" in k and k["breakout"]["level"] == pytest.approx(100.4) and "breakdown" not in k
    k = kinds(bars(flat(60, 100) + [101.0, 101.0]))     # the window now holds the breakout bar's high
    assert "breakout" not in k and "breakdown" not in k
    k = kinds(bars(flat(60, 100) + [99.0]))
    assert "breakdown" in k and k["breakdown"]["level"] == pytest.approx(99.6) and "breakout" not in k
    k = kinds(bars(flat(60, 100) + [100.4]))              # a close exactly at the level is not a break
    assert "breakout" not in k and "breakdown" not in k
    r = ChartEventsAgent("1h").run(bars(flat(60, 100) + [101.0]))
    assert phrase(next(e for e in r["events"] if e["kind"] == "breakout")) == "broke above the 2-day high at $100.40"


def test_impulsive_candle_threshold():
    base = bars(flat(60, 100))                                        # every bar's range is 0.8, so the ATR is 0.8
    big_up = base + [{"time": 60 * H, "open": 100.0, "high": 101.7, "low": 99.3, "close": 100.2, "volume": 100.0}]
    k = kinds(big_up)
    assert "impulse_up" in k and k["impulse_up"]["ratio"] == pytest.approx(3.0) and "impulse_down" not in k
    big_down = base + [{"time": 60 * H, "open": 100.0, "high": 101.7, "low": 99.3, "close": 99.8, "volume": 100.0}]
    assert "impulse_down" in kinds(big_down) and "impulse_up" not in kinds(big_down)
    small = base + [{"time": 60 * H, "open": 100.0, "high": 100.8, "low": 99.2, "close": 100.2, "volume": 100.0}]
    assert not {"impulse_up", "impulse_down"} & set(kinds(small))
    assert phrase({"kind": "impulse_up", "details": {"ratio": 3.0}}) == "a big up candle (3.0x the usual range)"


def test_rsi_divergence_needs_a_new_high_with_lower_rsi():
    base = wavy(60)
    sharp_peak = base + [base[-1] + k for k in range(1, 6)]                     # 5 fast bars up: strong momentum
    dip = sharp_peak + [sharp_peak[-1] - 0.5 * k for k in range(1, 9)]
    slow_new_high = dip + [dip[-1] + 0.25 * k for k in range(1, 19)]           # a higher high, reached slowly
    k = kinds(bars(slow_new_high))
    assert "rsi_divergence" in k and k["rsi_divergence"]["rsi"] < k["rsi_divergence"]["rsi_prev"] - 5
    assert k["rsi_divergence"]["prev_bar_time"] == 64 * H                      # the first peak's bar
    choppy = list(base)                                                        # a first peak reached in fits and starts
    for _ in range(5):
        choppy += [choppy[-1] + 1.0, choppy[-1] + 0.6]
    dip2 = choppy + [choppy[-1] - 0.5 * k for k in range(1, 9)]
    clean_new_high = dip2 + [dip2[-1] + 0.75 * k for k in range(1, 9)]        # ... then a clean climb: momentum not weaker
    assert clean_new_high[-1] > max(choppy) and "rsi_divergence" not in kinds(bars(clean_new_high))
    assert "rsi_divergence" not in kinds(bars(trend(80, 100.0, 0.5)))          # every bar a new high: no swing to compare


def test_volume_exhaustion_needs_thinner_volume_at_the_new_high():
    shape = flat(60, 100) + [102.0, 101.5, 101.0, 100.8] + [101.5, 102.2, 102.5]
    thin = [100.0] * (len(shape) - 3) + [50.0] * 3
    k = kinds(bars(shape, volumes=thin))
    assert "volume_exhaustion" in k and k["volume_exhaustion"]["volume"] == pytest.approx(50.0)
    assert k["volume_exhaustion"]["volume_prev"] == pytest.approx(100.0)
    assert "volume_exhaustion" not in kinds(bars(shape))          # equal volumes
    assert phrase({"kind": "volume_exhaustion", "details": {}}) == "new high on thinner volume"


# ---- 10-13: the daily frame, the report, latest event, the live/replay window ----
def test_daily_frame_golden_and_death_cross_and_regime():
    rising = flat(200, 100) + trend(60, 101.0, 1.0)
    sma = lambda v, n: sum(v[-n:]) / n   # noqa: E731
    k = next(k for k in range(200, 260) if sma(rising[:k + 1], 50) > sma(rising[:k + 1], 200)
             and not sma(rising[:k], 50) > sma(rising[:k], 200))
    t = 3000 * DAY_MS + 7 * H                                       # a candle at 07:00 on day 3000
    f = ChartEventsAgent._daily(rising[:k + 1], t)
    assert [e["kind"] for e in f["events"]] == ["cross_up"] and f["events"][0]["bar_time"] == 2999 * DAY_MS
    assert f["events"][0]["price"] == rising[k] and f["label"] == "1d" and "bars" not in f
    assert f["sma50"] > f["sma200"] and f["regime"] in ("trending", "impulsive") and f["direction"] == 1
    assert ChartEventsAgent._daily(rising[:k + 2], t)["events"] == []            # no flip the day after
    falling = flat(200, 100) + trend(60, 99.0, -1.0)
    assert [e["kind"] for e in ChartEventsAgent._daily(falling[:k + 1], t)["events"]] == ["cross_down"]
    assert phrase(ChartEventsAgent._daily(falling[:k + 1], t)["events"][0]).endswith("(death cross)")
    short = ChartEventsAgent._daily(rising[:150], t)
    assert short["events"] == [] and short["sma200"] is None and short["regime"] is not None
    assert ChartEventsAgent._daily([], t) is None
    a = ChartEventsAgent("1h")
    assert a.run(bars(flat(60, 100)), None)["timeframes"]["1d"] is None
    assert a.run(bars(flat(60, 100)), [])["timeframes"]["1d"] is None
    rows = [{"time": d * DAY_MS, "close": 100.0 + d} for d in range(5)]
    assert finished_daily(rows, 3 * DAY_MS + 5 * H) == [100.0, 101.0, 102.0]    # days 0-2 before day 3's candle
    assert finished_daily(rows, 3 * DAY_MS) == [100.0, 101.0, 102.0]           # the day's first candle: the same


def test_report_shape_brief_and_summary_text():
    r = ChartEventsAgent("1h").run(cycle_series(), flat(200, 100) + trend(60, 101.0, 1.0))
    assert set(r) == {"interval", "timeframes", "events", "latest_event", "summary"}
    f = r["timeframes"]["1h"]
    assert set(f) >= {"label", "bars", "bar_time", "price", "regime", "direction", "er", "ema12", "ema26", "rsi",
                      "atr", "support", "resistance", "swing_bars", "swing_hours", "events"}
    assert f["bars"] == 192 and f["swing_bars"] == 48 and r["timeframes"]["4h"]["swing_bars"] == 30
    assert f["resistance"] == pytest.approx(102.0 * 1.004) and f["support"] <= f["price"] <= f["resistance"]
    b = ChartEventsAgent.brief(r)
    assert len(json.dumps(b)) < 500 and set(b) == {"1h", "4h", "1d", "events", "support", "resistance", "summary"}
    assert b["4h"].startswith("trending") and len(b["events"]) <= 3 and b["events"][0].startswith("4h: ")
    assert b["support"] == r["timeframes"]["4h"]["support"]
    assert re.fullmatch(r"4h: .+ · 1h: .+ · daily: .+", r["summary"]) and "broke above the 5-day high" in r["summary"]
    assert ChartEventsAgent.tag(r).startswith("4h trending up · 1h ")
    for kind in KINDS:
        text = phrase({"kind": kind, "timeframe": "1h", "details": {"level": 81.2, "ratio": 2.7, "swing_hours": 48}})
        assert text and "_" not in text
    assert phrase({"kind": "breakout", "details": {"level": 81.2, "swing_hours": 120}}) == "broke above the 5-day high at $81.20"
    none = ChartEventsAgent("1h").run(bars(flat(30, 100)))
    assert none["summary"] == "4h: no bars yet · 1h: chop · daily: no daily prices yet"
    assert ChartEventsAgent.summary(none, daily_missing="not refreshed while the coin is held") \
        .endswith("daily: not refreshed while the coin is held")
    assert ChartEventsAgent.tag(none) == "no 4h bars yet · 1h chop"


def test_latest_event_priority_and_recent_bar_scan():
    a = ChartEventsAgent("1h")
    r = a.run(bars(flat(60, 100) + [101.0, 101.0, 99.0]))     # a breakout on bar -3, a breakdown + big candle on bar -1
    frame_kinds = {(e["kind"], e["bar_time"] // H) for e in r["timeframes"]["1h"]["events"]}
    assert {("breakout", 60), ("breakdown", 62), ("impulse_down", 62)} <= frame_kinds
    assert r["latest_event"]["kind"] == "breakdown" and r["latest_event"]["bar_time"] == 62 * H
    assert r["events"][0]["kind"] == "breakdown" and r["events"][1]["kind"] == "impulse_down"   # newest first, priority
    old = a.run(bars(flat(60, 100) + [101.0] + flat(RECENT_BARS["short"], 101)))      # the breakout is bar -7
    assert all(e["kind"] != "breakout" for e in old["timeframes"]["1h"]["events"])
    assert old["latest_event"] is None or old["latest_event"]["kind"] != "breakout"


def test_trim_makes_199_and_200_candle_windows_identical():
    series = synthetic("AAAUSDT", 400, 3, drift=0.0004)
    a = ChartEventsAgent("1h")
    daily = DailySeries(series).completed
    live, replay = a.run(series[-199:], daily), a.run(series[-200:], daily)
    assert live == replay and a.run(series[-199:], daily) == live
    assert live["timeframes"]["1h"]["bars"] == 192
    assert a.run(series[-250:], daily) == live                      # any longer window, the same picture


# ---- 14: the journal table ----
def test_journal_chart_events_dedupe_counts_and_prune():
    j = Journal(":memory:", 10_000)
    events = [{"timeframe": "1h", "kind": "breakout", "bar_time": 10 * H, "price": 101.0, "details": {"level": 100.4}},
              {"timeframe": "4h", "kind": "cross_up", "bar_time": 8 * H, "price": 101.0, "details": {"ema12": 1, "ema26": 0}}]
    assert [e["kind"] for e in j.add_chart_events("ETHUSDT", events)] == ["breakout", "cross_up"]
    assert j.add_chart_events("ETHUSDT", events) == []
    assert j.add_chart_events("BTCUSDT", events[:1])[0]["kind"] == "breakout"    # another coin, the same bar: new
    assert j.db.execute("SELECT COUNT(*) FROM chart_events").fetchone()[0] == 3
    rows = j.chart_events("ETHUSDT", limit=10)
    assert [r["kind"] for r in rows] == ["breakout", "cross_up"] and rows[0]["details"] == {"level": 100.4}
    assert len(j.chart_events(limit=2)) == 2 and len(j.chart_events()) == 3
    counts = j.chart_event_counts("2000-01-01T00:00:00+00:00")
    assert counts == {"1h": {"breakout": 2}, "4h": {"cross_up": 1}}
    assert j.chart_event_counts("2999-01-01T00:00:00+00:00") == {}
    j.db.execute("INSERT INTO chart_events (ts, symbol, timeframe, kind, bar_time, price, details) VALUES "
                 "('2020-01-01T00:00:00+00:00', 'OLDUSDT', '1h', 'breakdown', 1, 1.0, '{}')")
    assert j.prune_chart_events(90) == 1 and j.prune_chart_events(90) == 0
    assert j.db.execute("SELECT COUNT(*) FROM chart_events").fetchone()[0] == 3


# ---- 15-16: the cycle ----
def test_hourly_cycle_writes_events_and_notable_ones_to_the_diary():
    series = {"BTCUSDT": cycle_series(), "ETHUSDT": cycle_series()}
    daily = {sym: flat(200, 100) + trend(60, 101.0, 1.0) for sym in series}
    bot = desk(series, daily, checks=("reward",))
    bot.journal.open_position("ETHUSDT", {"qty": 1.0, "avg_price": 100.0, "quote": 100.0}, 95.0, 110.0)
    bot.market._fresh.add("ETHUSDT")        # its daily rows were read earlier in the hour (the real cache)
    bot.run_cycle()
    rows = bot.journal.chart_events("BTCUSDT", limit=50)
    assert {(r["timeframe"], r["kind"]) for r in rows} >= {("4h", "breakout"), ("1h", "breakout")}
    diary = [e for e in bot.journal.events() if e["kind"] == "chart"]
    assert diary and all(" 4h: " in e["text"] for e in diary) and any("broke above the 5-day high" in e["text"] for e in diary)
    assert all(" 1h: " not in e["text"] for e in diary) and all(e["data"]["kind"] in ("breakout", "cross_up") for e in diary)
    assert bot.charts["BTCUSDT"]["timeframes"]["4h"]["events"] and bot.charts_at
    latest = bot.journal.get("chart_latest")
    assert "broke above" in latest["BTCUSDT"]["summary"] and latest["BTCUSDT"]["interval"] == "1h"
    row = bot.journal.db.execute("SELECT reports FROM decisions WHERE symbol='BTCUSDT' ORDER BY id DESC").fetchone()
    assert json.loads(row["reports"])["chart"]["summary"] == bot.charts["BTCUSDT"]["summary"]
    # the request budget: the unheld coin's daily rows once (the checklist's own download), never the held coin's
    assert bot.market.downloads == ["BTCUSDT"]
    assert bot.charts["BTCUSDT"]["timeframes"]["1d"] is not None            # built from that download
    assert bot.charts["ETHUSDT"]["timeframes"]["1d"] is not None            # built from the cache, no download
    n_rows, n_diary = bot.journal.db.execute("SELECT COUNT(*) FROM chart_events").fetchone()[0], len(diary)
    bot.run_cycle()                                                        # the same candles: nothing new
    assert bot.journal.db.execute("SELECT COUNT(*) FROM chart_events").fetchone()[0] == n_rows
    assert len([e for e in bot.journal.events() if e["kind"] == "chart"]) == n_diary
    assert bot.market.downloads == ["BTCUSDT"]
    off = desk(series, daily, checks=())
    off.run_cycle()
    assert off.market.downloads == [] and off.charts["BTCUSDT"]["timeframes"]["1d"] is None
    assert off.charts["BTCUSDT"]["timeframes"]["4h"]["regime"] is not None


def test_trend_mode_never_runs_the_chart_agent():
    s = Settings(offline=True, strategy="trend", symbols=["BTCUSDT"])
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.run_cycle()
    assert bot.journal.db.execute("SELECT COUNT(*) FROM chart_events").fetchone()[0] == 0
    assert bot.charts == {} and bot.journal.get("chart_latest") is None and bot.charts_at is None


# ---- 17-19: the regime rule live, in the replay and in the settings ----
def test_regime_check_live_and_replay_agree():
    history = synthetic("BTCUSDT", 1500, 4, drift=0.0003)
    ds, a, m = DailySeries(history), ChartEventsAgent("1h"), MentorAgent(("regime",))

    def replay_side(i):
        chart = a.run(history[i - 199: i + 1], ds.completed[: ds.before[i]])
        review = m.review("BTCUSDT", history[i - 199: i + 1], [], history[i]["close"], 1, 2, 1, None, None, 0, chart=chart)
        return chart, review

    verdicts = {i: replay_side(i)[1]["pass"] for i in range(600, 1500, 7)}   # from day 25: the daily frame exists
    vetoed = [i for i, ok in verdicts.items() if not ok]
    passed = [i for i, ok in verdicts.items() if ok]
    assert vetoed and passed
    boundary = next(i for i in range(600, 1500) if i % 24 == 0)              # the first candle of a UTC day

    def live_side(i):
        bot = desk({"BTCUSDT": history[i - 198: i + 1]}, checks=("regime",))   # 199 finished candles, as live
        bot.market.daily = {"BTCUSDT": ds.completed[: ds.before[i]]}
        force_buy(bot)
        bot.apply_champion()               # the first cycle rebuilds the mentor from the champion card: spy after that
        seen = {}
        real = bot.mentor.review

        def spy(*args, **kw):
            seen["chart"] = kw.get("chart")
            seen["review"] = real(*args, **kw)
            return seen["review"]
        bot.mentor.review = spy
        bot.run_cycle()
        return bot, seen

    for i in (vetoed[0], passed[0], boundary):
        chart_r, review_r = replay_side(i)
        bot, seen = live_side(i)
        assert seen["chart"] == chart_r and bot.charts["BTCUSDT"] == chart_r
        assert seen["review"] == review_r and seen["review"]["pass"] == verdicts.get(i, review_r["pass"])
        assert chart_r["timeframes"]["1d"] is not None and len(ds.completed[: ds.before[i]]) >= 21
        if not review_r["pass"]:
            assert bot.journal.positions() == {} and "regime" in review_r["failed"]
        else:
            assert "BTCUSDT" in bot.journal.positions()


def test_backtest_with_regime_check_vetoes_chop_buys(monkeypatch):
    saw = bars([100 * (1.01 if i % 2 else 0.99) for i in range(1500)])          # +1% / -1%: going nowhere
    history = {"AAAUSDT": saw}
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", -1.0)                             # every hour is a buy signal
    calls = []
    real = MentorAgent.review

    def counting(self, *a, **kw):
        calls.append(kw.get("chart"))
        return real(self, *a, **kw)
    monkeypatch.setattr(MentorAgent, "review", counting)
    bt = Backtest(Settings(offline=True, symbols=["AAAUSDT"], mentor_checks=("regime",)), history)
    assert bt.chart is not None
    r = bt.run()
    assert r["trades"] == 0 and r["decisions"]["BUY"] > 0 and calls
    assert r["mentor_vetoes"] == {"regime": len(calls)} and all(c is not None for c in calls)
    plain = Backtest(Settings(offline=True, symbols=["AAAUSDT"], mentor_checks=()), history)
    assert plain.chart is None and plain.run()["trades"] > 0
    reward = Backtest(Settings(offline=True, symbols=["AAAUSDT"], mentor_checks=("reward",)), history)
    assert reward.chart is None and "regime" not in reward.run()["mentor_vetoes"]
    assert Settings(mentor_checks=("regime",)).mentor_checks == ("regime",)


def test_regime_is_not_a_default_and_is_selectable(monkeypatch):
    assert "regime" not in Settings().mentor_checks and "regime" in ALL_CHECKS
    assert "regime" not in setup_wizard.DEFAULTS["MENTOR_CHECKS"] and "regime" not in StrategyCard("x").checks
    assert any("regime" in title for title, _ in setup_wizard.LAYOUT)
    monkeypatch.setenv("MENTOR_CHECKS", "trend200,regime")
    assert Settings.load().mentor_checks == ("trend200", "regime")
    assert StrategyCard(name="x", checks=("regime",)).violations() == []
    assert any("regim" in p for p in StrategyCard(name="x", checks=("regim",)).violations())
    assert mentor_module.SOURCES["regime"].startswith("Dow theory")
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=("regime",))
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    assert "regime" in bot.mentor.checks
    r = MentorAgent(("regime",)).review("ETHUSDT", bars(flat(60, 100)), [], 100, 99, 102, 1, None, None, 0)
    assert r["pass"] and r["checks"]["regime"]["ok"] is None and "no chart report" in r["checks"]["regime"]["detail"]


# ---- 20-22: the owner's texts, the notebook, the cost ----
def test_chart_command_status_tag_daily_line_and_help():
    series = {"BTCUSDT": cycle_series(), "ETHUSDT": cycle_series()}
    bot = desk(series, {sym: flat(200, 100) + trend(60, 101.0, 1.0) for sym in series}, checks=("reward",))
    j = bot.journal
    assert chart_text(bot, j) == NO_CHART_YET
    bot.journal.open_position("ETHUSDT", {"qty": 1.0, "avg_price": 100.0, "quote": 100.0}, 95.0, 110.0)
    bot.run_cycle()
    text = chart_text(bot, j)
    assert text.startswith("📊 Chart picture (finished candles, latest check") and "BTC $102.00 - 4h: " in text
    assert text.index("ETH ") < text.index("BTC ")                                   # held coins first
    assert "support $" in text and "on the 4-hour chart" in text and "Chop = going nowhere" in text
    assert "daily: not refreshed while the coin is held" in text                     # ETH: no download for it
    one = chart_text(bot, j, "btc")
    assert one.startswith("📊 BTC chart") and "4-hour chart: trending up, straightness 0." in one
    assert "4h bars up to 08:00 UTC" in one and "hourly chart: " in one and "daily chart: " in one   # bar 196-199 ends 200:00
    assert "Recent events:" in one and "broke above the 5-day high" in one and "50-day $" in one
    for t in (text, one):
        assert not re.search(r"\bER\b", t)
    assert chart_text(bot, j, "XRP") == "XRP is not on the watchlist (see /watchlist)."
    bot.charts = {}                                                                  # right after a restart
    fallback = chart_text(bot, j)
    assert fallback.startswith("📊 Chart picture as of") and "BTC - 4h: trending up" in fallback
    trend_bot = Orchestrator(Settings(offline=True, strategy="trend", symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    assert chart_text(trend_bot, trend_bot.journal) == CHART_TREND
    off = desk(series, checks=())
    off.run_cycle()
    assert "daily: not available (the checklist is off" in chart_text(off, off.journal)
    bot.run_cycle()
    tags = {sym: ChartEventsAgent.tag(r) for sym, r in bot.charts.items()}
    with_tags = status_text(j, bot.last_price, list(series), trend=False, tags=tags)
    assert "• BTCUSDT HOLD" in with_tags and " UTC · chart: 4h trending up · 1h impulsive up: " in with_tags
    plain = status_text(j, bot.last_price, list(series))
    assert "chart:" not in plain and plain.startswith("📌 Status")
    line = chart_daily_line(j, "2000-01-01T00:00:00+00:00")
    assert line.startswith("📊 Chart events, last 24 h: ") and "4-hour chart: " in line and "breakout" in line
    assert "hourly chart: " in line and "daily chart: none" in line
    assert chart_daily_line(j, "2999-01-01T00:00:00+00:00") == ""
    assert "/chart [coin]" in HELP_TEXT and "/chart" in help_text(Settings())
    assert "/chart" not in help_text(Settings(strategy="trend"))


def test_notebook_chart_page(tmp_path):
    series = {"BTCUSDT": cycle_series()}
    bot = desk(series, {"BTCUSDT": flat(200, 100) + trend(60, 101.0, 1.0)}, checks=("reward",),
               notes_dir=str(tmp_path / "notes"))
    bot.run_cycle()
    page = (tmp_path / "notes" / "CHART.md").read_text()
    assert page.startswith("# Chart picture") and "| BTC | impulsive up | trending up |" in page
    assert "## Events, last 14 days" in page
    assert "BTC 4h breakout: broke above the 5-day high" in page and "## Events per kind, last 7 days" in page
    assert "| CHART.md |" in (tmp_path / "notes" / "README.md").read_text()
    assert "# Chart picture" in bot.notes.digest(bot.last_price)
    empty = Notes(Journal(":memory:", 10_000), tmp_path / "empty", Settings(offline=True))
    assert "Nothing recorded yet" in empty.chart_md() and "none recorded" in empty.chart_md()
    trend_notes = Notes(Journal(":memory:", 10_000), tmp_path / "trend", Settings(offline=True, strategy="trend"))
    assert "daily trend rule, which does not use the chart reader" in trend_notes.chart_md()


def test_chart_agent_is_cheap():
    series = synthetic("BTCUSDT", 200, 9, drift=0.0003)
    daily = [100 + i * 0.1 + math.sin(i / 9) for i in range(260)]
    a = ChartEventsAgent("1h")
    best = min(_timed(a, series, daily) for _ in range(5))
    assert best < 0.050, f"{best * 1000:.1f} ms per coin"


def _timed(a, series, daily):
    t0 = time.perf_counter()
    a.run(series, daily)
    return time.perf_counter() - t0


# ---- fixes from the 1.8.0 review ----
def test_a_crossover_is_recorded_once_per_direction():
    """The 4-hour averages are computed on a sliding window, so a marginal cross can land on a neighbouring bar from
    one check to the next: the same direction is never recorded twice, and never behind a later cross."""
    j = Journal(":memory:", 10_000)
    ev = lambda kind, bar: {"timeframe": "4h", "kind": kind, "bar_time": bar * FOUR_H_MS, "price": 100.0, "details": {}}
    assert len(j.add_chart_events("AAAUSDT", [ev("cross_up", 100)])) == 1
    assert j.add_chart_events("AAAUSDT", [ev("cross_up", 101)]) == []          # the same cross, one bar later
    assert len(j.add_chart_events("AAAUSDT", [ev("cross_down", 103)])) == 1
    assert j.add_chart_events("AAAUSDT", [ev("cross_up", 102)]) == []          # earlier than the newest recorded cross
    assert len(j.add_chart_events("AAAUSDT", [ev("breakout", 103), ev("breakout", 103)])) == 1
    assert len(j.add_chart_events("BBBUSDT", [ev("cross_up", 101)])) == 1      # another coin is independent
    assert sorted(e["kind"] for e in j.chart_events("AAAUSDT")) == ["breakout", "cross_down", "cross_up"]


def test_four_hour_bars_use_the_known_candle_step():
    """A missing hour at the start of the window must not change the candle size the bars are built from (it let the
    still-forming 4-hour bar through)."""
    c = bars(flat(201, 100), start_hour=2)                                # the newest candle is 3 h into a 4h bar
    gap = c[:1] + c[2:]                                                    # the second hour is missing
    whole = aggregate(c, FOUR_H_MS, H)
    assert aggregate(gap, FOUR_H_MS, H)[-1]["time"] == whole[-1]["time"]  # the forming bar stays out
    assert aggregate(gap, FOUR_H_MS)[-1]["time"] > whole[-1]["time"]      # the inferred step (2 h) lets it in


def test_regime_is_unknown_without_a_usable_frame():
    short = ChartEventsAgent("1h").run(bars(flat(20, 100)))
    review = MentorAgent(("regime",)).review("AAAUSDT", bars(flat(20, 100)), [], 100.0, 95.0, 110.0, 1.0, None, None,
                                              int(time.time() * 1000), chart=short)
    assert review["pass"] and review["checks"]["regime"]["ok"] is None
    assert "no chart frames yet" in review["checks"]["regime"]["detail"]


def test_a_failed_daily_download_is_tried_once_per_coin_per_check():
    from trader.binance_client import BinanceError

    class Failing(FakeMarket):
        def daily_rows(self, sym, limit=260):
            self.downloads.append(sym)
            raise BinanceError("HTTP 429")

        def daily_closes(self, sym, limit=260):
            return [r["close"] for r in self.daily_rows(sym, limit)]
    bot = desk({"BTCUSDT": cycle_series(), "ETHUSDT": cycle_series()}, checks=("trend200",))
    bot.market = Failing(bot.market.series)
    bot.run_cycle()
    assert bot.market.downloads == ["BTCUSDT", "ETHUSDT"]                 # 1.8.0 made two attempts per coin
    assert set(bot.charts) == {"BTCUSDT", "ETHUSDT"}                       # the picture is drawn without the daily part
    assert "no daily prices yet" in bot.charts["BTCUSDT"]["summary"]


def test_a_coin_whose_data_failed_has_no_stale_picture():
    series = {"BTCUSDT": cycle_series(), "ETHUSDT": cycle_series()}
    bot = desk(series)
    j = bot.journal
    bot.run_cycle()
    assert set(bot.charts) == {"BTCUSDT", "ETHUSDT"}
    good = bot.market.candles

    def flaky(sym, limit=200):
        if sym == "ETHUSDT":
            raise RuntimeError("timeout")
        return good(sym, limit)
    bot.market.candles = flaky
    bot.run_cycle()
    assert set(bot.charts) == {"BTCUSDT"}
    text = chart_text(bot, j, "eth")
    assert "no picture from the latest check" in text and "not on the watchlist" not in text
    assert "not on the watchlist" in chart_text(bot, j, "doge")
    assert "ETH" not in chart_text(bot, j, "")                             # the all-coins view shows no stale line


def test_the_all_coins_chart_has_room_for_twenty_coins():
    series = {f"C{i:02d}USDT": cycle_series() for i in range(20)}
    bot = desk(series, daily={s: flat(259, 100) for s in series}, checks=("trend200",))
    bot.run_cycle()
    text = chart_text(bot, bot.journal, "")
    assert len(text) > 4000 and text.rstrip().endswith("with its recent events.")   # nothing cut; the command splits it
    assert all(f"C{i:02d} " in text for i in range(20))


def test_help_and_checklist_describe_chop_the_same_way():
    assert "chop (going nowhere)" in HELP_TEXT and "calm" not in HELP_TEXT.split("/chart")[1].split("\n")[0]
    saw = bars([100 + (i % 2) * 3 for i in range(200)])                   # a sawtooth: chop on every frame
    r = ChartEventsAgent("1h").run(saw)
    review = MentorAgent(("regime",)).review("AAAUSDT", saw, [], 100.0, 95.0, 110.0, 1.0, None, None,
                                              int(time.time() * 1000), chart=r)
    detail = review["checks"]["regime"]["detail"]
    assert "1h chop" in detail and "chop up" not in detail and "chop down" not in detail
