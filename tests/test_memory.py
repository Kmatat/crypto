"""1.9.1: the post-mortem & memory agent (trader/agents/memory.py, trader/postmortem_report.py): the context at entry,
the causes of every closed trade (winners too), the statistics, the walk-forward memory in the replay, the live hooks,
the seed and the backfill, the scorecard, the texts, and the guarantee that nothing changes unless the owner adds
`memory` to MENTOR_CHECKS."""
import dataclasses
import datetime as dt
import inspect
import json
import random
import re
import time
from types import SimpleNamespace

import pytest

from trader import setup_wizard
from trader import __main__ as main_mod
from trader.__main__ import HELP_TEXT, help_text
from trader.agents import decision as dmod
from trader.agents import memory
from trader.agents import mentor as mentor_module
from trader.agents.chart import ChartEventsAgent
from trader.agents.events import iso, load_calendar, ms_of
from trader.agents.memory import (MIN_POOL, MIN_SIMILAR, MemoryBook, binom_tail, breakout_at_entry, breakout_failed,
                                  bucket_key, btc_side, causes, chance_text, decision_time, describe_bucket,
                                  entry_features, fisher_greater, frame_side, hour_block, judged_families, normal_dip,
                                  release_near, sidak)
from trader.agents.mentor import ALL_CHECKS, MentorAgent
from trader.agents.research import PROPOSAL_SCHEMA, ResearchAgent
from trader.backtest import Backtest, DailySeries
from trader.config import ENV_KEYS, Settings
from trader.journal import Journal
from trader.notes import Notes
from trader.orchestrator import Orchestrator
from trader.postmortem_report import (checklist_detail, lessons_causes, memory_daily_line, postmortem_md,
                                      postmortem_text, replay_memory_text, seed_refusal)
from trader.scorecard import checklist_scorecard, scorecard_text
from trader.strategy import StrategyCard
from tests.test_backtest import synthetic
from tests.test_chart import bars, cycle_series, desk, flat, force_buy

H = 3_600_000
DAY = 86_400_000
MIN = 60_000
KEY = "v1|4h:chop|btc:above|h:12-18|rel:no"
FORBIDDEN = re.compile(r"\b(will|predicts?|safe|avoids losses|expect to win)\b", re.I)


def fixed_releases(*times, start=0, end=400 * DAY):
    return [(t, "US CPI") for t in sorted(times)], (start, end)


def rows(bucket, n, losses, t0=0, step=H, opened=None):
    return [{"bucket": bucket, "closed_ms": t0 + k * step, "loss": k < losses, "opened_ms": opened or 0}
            for k in range(n)]


def verdict_brute(rows_, bucket, t, rule="test"):
    """The memory's verdict worked out from scratch (test 6c)."""
    win = [r for r in rows_ if t - memory.MEMORY_DAYS * DAY < r["closed_ms"] <= t]
    cell = [r for r in win if r["bucket"] == bucket]
    n, losses = len(cell), sum(r["loss"] for r in cell)
    pool, pool_losses = len(win), sum(r["loss"] for r in win)
    counts = {}
    for r in win:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    m = sum(1 for v in counts.values() if v >= MIN_SIMILAR)
    v = {"ok": None, "rule": rule, "bucket": bucket, "n": n, "losses": losses, "pool": pool, "pool_losses": pool_losses,
         "base": pool_losses / pool if pool else None, "p": None, "level": None, "groups": m}
    if rule == "owner60":
        if n >= MIN_SIMILAR:
            v["ok"] = not (losses / n > 0.60)
    elif pool >= MIN_POOL and n >= MIN_SIMILAR:
        v["p"] = binom_tail(losses, n, v["base"])
        v["level"] = sidak(0.05, m)
        v["ok"] = v["p"] > v["level"]
    v["detail"] = checklist_detail(v)
    return v


def chart_report(latest=None, base="trending up", four="chop"):
    def frame(words, label):
        if words == "chop":
            return {"label": label, "regime": "chop", "direction": 1, "rsi": 55.0}
        reg, way = words.split()
        return {"label": label, "regime": reg, "direction": 1 if way == "up" else -1, "rsi": 55.0}
    return {"interval": "1h", "timeframes": {"1h": frame(base, "1h"), "4h": frame(four, "4h"), "1d": None},
            "events": [], "latest_event": latest}


# ---- 1. features ----
def test_decision_time_blocks_sides_and_buckets():
    assert decision_time(bars(flat(10, 100)), H) == 10 * H
    assert hour_block(6 * H) == "06-12" and hour_block(5 * H) == "00-06" and hour_block(24 * H - MIN) == "18-24"
    assert hour_block(decision_time(bars(flat(6, 100)), H)) == "06-12"        # a candle opening 05:00 closes 06:00
    assert hour_block(decision_time(bars(flat(5, 100)), H)) == "00-06"
    assert frame_side(None) == frame_side({"regime": None}) == "unknown"
    assert frame_side({"regime": "chop", "direction": 1}) == "chop"
    assert frame_side({"regime": "trending", "direction": 1}) == frame_side({"regime": "impulsive", "direction": 1}) == "up"
    assert frame_side({"regime": "trending", "direction": -1}) == frame_side({"regime": "impulsive", "direction": -1}) == "down"
    up, down = [100 + i for i in range(220)], [400 - i for i in range(220)]
    assert btc_side("BTCUSDT", up, None) == "above" and btc_side("BTCUSDT", down, up) == "below"   # its own closes
    assert btc_side("ETHUSDT", down, up) == "above" and btc_side("ETHUSDT", up, down) == "below"
    assert btc_side("ETHUSDT", up, up[:150]) == btc_side("ETHUSDT", up, None) == "unknown"
    assert bucket_key("chop", "above", "12-18", "no") == KEY
    assert describe_bucket(KEY) == ("4-hour chart in chop, Bitcoin above its 200-day average, decided 12:00-18:00 UTC, "
                                    "no US release within 2 hours")
    assert memory.parse_bucket(KEY) == {"4h": "chop", "btc": "above", "h": "12-18", "rel": "no"}
    assert describe_bucket(bucket_key("up", "below", "00-06", "yes")).startswith("4-hour chart rising, Bitcoin below")


# ---- 2. releases ----
def test_release_near_window_and_cuts():
    r = 100 * H
    rel = fixed_releases(r)
    assert release_near(*rel, r - 120 * MIN)["near"] == "yes"     # the release 120 min after the decision
    assert release_near(*rel, r - 121 * MIN)["near"] == "no"
    assert release_near(*rel, r + 120 * MIN)["near"] == "yes"     # 120 min before
    assert release_near(*rel, r + 121 * MIN)["near"] == "no"
    out = release_near(*rel, r - 121 * MIN)
    assert out == {"near": "no", "to_min": 121, "since_min": None, "name": "US CPI"}
    assert release_near(*rel, r + 3 * H)["since_min"] == 180 and release_near(*rel, r + 3 * H)["to_min"] is None
    assert release_near(*rel, r - 7 * DAY)["to_min"] == 7 * 24 * 60
    assert release_near(*rel, r - 7 * DAY - MIN)["to_min"] is None and release_near(*rel, r - 7 * DAY - MIN)["name"] is None
    assert release_near(rel[0], (0, 50 * H), r)["near"] == "unknown"   # outside the calendar's coverage
    assert release_near(rel[0], None, r)["near"] == "unknown"


def _calendar(path, events, **extra):
    path.write_text(json.dumps({"version": 1, "events": events, **extra}))
    return path


def test_release_times_merges_the_three_files(tmp_path):
    hist = _calendar(tmp_path / "hist.json", [
        {"ts_utc": "2024-11-01T12:30:00Z", "kind": "nfp", "name": "jobs Nov"},
        {"ts_utc": "2025-01-15T13:30:00Z", "kind": "cpi", "name": "cpi Jan"}], covers_from="2024-11-01")
    shipped = _calendar(tmp_path / "shipped.json", [
        {"ts_utc": "2025-01-15T13:30:00Z", "kind": "cpi", "name": "CPI again"},          # the same time and kind
        {"ts_utc": "2026-08-07T12:30:00Z", "kind": "nfp", "name": "jobs Aug"},
        {"ts_utc": "2026-08-12T12:30:00Z", "kind": "cpi", "name": "cpi Aug"}])
    owner = _calendar(tmp_path / "owner.json", [
        {"ts_utc": "2026-08-12T12:30:00Z", "kind": "cpi", "name": "cpi Aug", "remove": True},
        {"ts_utc": "2026-09-01T12:00:00Z", "kind": "pce", "name": "US PCE"}])
    rels, cov = memory.release_times(owner, shipped, hist)
    assert [iso(ms) for ms, _ in rels] == ["2024-11-01T12:30:00+00:00", "2025-01-15T13:30:00+00:00",
                                           "2026-08-07T12:30:00+00:00", "2026-09-01T12:00:00+00:00"]
    assert [n for _, n in rels] == ["US jobs report", "US CPI", "US jobs report", "US PCE"]
    assert cov == (ms_of("2024-11-01T00:00:00Z"), ms_of("2026-09-02T00:00:00Z"))
    assert memory.release_times(owner, shipped, hist) is memory.release_times(owner, shipped, hist)   # cached
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    rels2, cov2 = memory.release_times(broken, shipped, hist)
    assert len(rels2) == 4 and "US PCE" not in [n for _, n in rels2] and cov2[0] == cov[0]
    shipped_only, cov3 = memory.release_times(None, shipped, tmp_path / "missing.json")
    assert len(shipped_only) == 3 and cov3[0] == ms_of("2025-01-15T00:00:00Z")


# ---- 3. the normal 2-hour dip ----
def dip_candles(n, dips, close=100.0):
    return [{"time": i * H, "open": close, "high": close, "low": close - dips.get(i, 0.0), "close": close, "volume": 1.0}
            for i in range(n)]


def test_normal_dip_value_index_rule_and_windows():
    # 168 starts; a 1-unit dip at candle j makes the starts j-2 and j-1 see a 1% fall. 9 such candles: 18 starts of
    # 1% and 150 of 0%: sorted, index floor(0.9 x 167) = 150 is the first 1% -> 1%. With 8: 16 starts -> 0%.
    nine = {10 * k: 1.0 for k in range(1, 10)}
    assert normal_dip(dip_candles(170, nine), H) == pytest.approx(0.01)
    eight = {10 * k: 1.0 for k in range(1, 9)}
    assert normal_dip(dip_candles(170, eight), H) == 0.0
    last = {**eight, 169: 1.0}            # the newest candle is in exactly one window (the last start's)
    assert normal_dip(dip_candles(170, last), H) == 0.0
    assert normal_dip(dip_candles(169, nine), H) is None
    older = {i: 50.0 for i in range(30)}   # 30 older candles with huge dips: outside the 170 used
    long = dip_candles(200, {**older, **{30 + k: v for k, v in nine.items()}})
    assert normal_dip(long, H) == normal_dip(long[-199:], H) == normal_dip(long[-170:], H) == pytest.approx(0.01)
    two = [{**c, "time": c["time"] * 2} for c in dip_candles(85, {})]
    assert normal_dip(two[:84], 2 * H) is None and normal_dip(two, 2 * H) == 0.0      # 84 starts, 1-candle windows
    four = [{**c, "time": c["time"] * 4} for c in dip_candles(43, {5: 2.0})]
    assert normal_dip(four[:42], 4 * H) is None and normal_dip(four, 4 * H) == 0.0    # 42 starts


# ---- 4. breakouts ----
def test_breakout_at_entry_and_failed_breakouts():
    def latest(frame, kind="breakout"):
        return {"latest_event": {"kind": kind, "timeframe": frame, "bar_time": 0, "details": {"level": 105.0}}}
    assert breakout_at_entry(latest("1h"), "1h") == {"level": 105.0, "frame": "1h", "hours": 6}
    assert breakout_at_entry(latest("4h"), "1h")["hours"] == 8
    assert breakout_at_entry(latest("2h"), "2h")["hours"] == 12
    assert breakout_at_entry(latest("4h"), "4h")["hours"] == 8
    assert breakout_at_entry(latest("1h", "cross_up"), "1h") is None and breakout_at_entry(None, "1h") is None
    closes = [110.0] * 20
    c = bars(closes)
    assert breakout_failed(c[:16], 10 * H, 105.0, 6) is False          # the window's last candle (15:00) present
    assert breakout_failed(c[:15], 10 * H, 105.0, 6) is None           # not complete yet
    below = bars(closes[:12] + [104.0] + closes[13:])
    assert breakout_failed(below[:13], 10 * H, 105.0, 6) is True        # a close below inside the window
    after = bars(closes[:16] + [104.0] + closes[17:])
    assert breakout_failed(after, 10 * H, 105.0, 6) is False            # after the window: ignored
    assert breakout_failed(c[11:], 10 * H, 105.0, 6) is None            # the window's start is not in hand


# ---- 5. the causes, each at its threshold ----
OPEN, CLOSE = "1970-01-05T10:00:00+00:00", "1970-01-05T16:00:00+00:00"


def trade(pnl=-10.0, sales=1, opened=OPEN, closed=CLOSE):
    return {"symbol": "ETHUSDT", "opened_at": opened, "closed_at": closed, "pnl": pnl, "pnl_pct": pnl / 10,
            "cost": 1000.0, "exit_reasons": ["stop-loss"], "sales": sales}


def ctx(**kw):
    base = {"strategy": "hourly", "regime_base": "trending up", "regime_4h": "trending up", "latest_event": None,
            "breakout_level": None, "breakout_hours": None, "breakout_failed": None, "stop_pct": 0.02,
            "normal_dip": 0.01, "signal_price": 100.0, "fill_price": 100.0, "decision_price": 100.0, "atr_pct": 0.01,
            "market_price": 100.0, "quote_planned": 1000.0, "quote_filled": 1000.0, "backfilled": 0}
    return {**base, **kw}


def kinds_of(tags):
    return {t["kind"] for t in tags}


def test_each_cause_fires_and_does_not_fire_at_its_threshold():
    rel = ([], (0, 400 * DAY))
    def k(c=None, sales=(), releases=rel, jumps=(), source="live", t=None):
        return kinds_of(causes(t or trade(), c if c is not None else ctx(), list(sales), releases[0], list(jumps), 0,
                               source))
    assert k() == set()                                                                           # no tag
    sale = {"price": 99.59, "market_price": 100.0, "reason": "stop-loss (exchange)"}
    assert k(sales=[sale]) == {"stop_slippage"}                                                   # 0.41%
    assert k(sales=[{**sale, "price": 99.61}]) == set()                                           # 0.39%
    assert k(sales=[{**sale, "reason": "stop-loss (emulated)"}]) == {"stop_slippage"}
    assert k(sales=[{**sale, "reason": "stop-loss", "price": 90.0}]) == set()     # a desk-side stop: not judged
    assert k(sales=[{**sale, "market_price": None}]) == set()                     # no reference: not judged
    assert k(ctx(fill_price=100.26, market_price=100.26)) == {"entry_gap"}                         # 0.26 ATR
    assert k(ctx(fill_price=100.24, market_price=100.24)) == set()                                 # 0.24 ATR
    # a fill equal to the live price at the check, but 0.3 ATR above the finished candle's close: tagged
    assert k(ctx(fill_price=100.3, decision_price=100.3, market_price=100.3)) == {"entry_gap"}
    assert k(ctx(signal_price=100.51, fill_price=100.51, market_price=100.0)) == {"demo_gap"}      # 0.51%
    assert k(ctx(signal_price=100.49, fill_price=100.49, market_price=100.0)) == set()             # 0.49%
    assert k(ctx(quote_filled=970.0)) == {"partial"} and k(ctx(quote_filled=990.0)) == set()
    assert k(t=trade(sales=2)) == {"partial"}
    opened = ms_of(OPEN)
    assert k(releases=fixed_releases(opened - 119 * MIN)) == {"release"}
    assert k(releases=fixed_releases(opened - 121 * MIN)) == set()
    assert k(releases=fixed_releases(ms_of(CLOSE) + MIN)) == set()
    text = causes(trade(), ctx(), [], fixed_releases(opened + 10 * MIN)[0], [], 0, "live")[0]["text"]
    assert text == "US release during the trade: US CPI at 10:10 UTC, 10 min after the buy"
    jump = {"kind": "headline", "ts_utc": "1970-01-05T14:05:00+00:00", "label": "SEC sues", "magnitude_pct": -1.4}
    assert k(jumps=[jump]) == {"jump"} and k(jumps=[{**jump, "ts_utc": "1970-01-05T17:00:00+00:00"}]) == set()
    assert "measured jump -1.4% within 2 h (event study)" in causes(trade(), ctx(), [], [], [jump], 0, "live")[0]["text"]
    assert k(ctx(regime_base="chop")) == {"chop"} and k(ctx(regime_4h="chop")) == {"chop"}
    fb = ctx(latest_event="1h breakout", breakout_level=105.0, breakout_hours=6)
    assert k({**fb, "breakout_failed": 1}) == {"failed_breakout"}
    assert k({**fb, "breakout_failed": 0}) == set() and k(fb) == set()
    assert "failed_breakout" not in judged_families(trade(), fb, "live", rel[1])           # not known yet
    assert "failed_breakout" in judged_families(trade(), {**fb, "breakout_failed": 0}, "live", rel[1])
    assert k(ctx(stop_pct=0.01, normal_dip=0.01)) == {"noise"}                                # at equality: fires
    assert k(ctx(stop_pct=0.0101, normal_dip=0.01)) == set()
    # a winner with a failed breakout is tagged: the rule reads the price path, not the outcome
    assert k({**fb, "breakout_failed": 1}, t=trade(pnl=25.0)) == {"failed_breakout"}
    # replay rows: execution and headline jumps are not judged
    bad = ctx(fill_price=110.0, market_price=100.0, quote_filled=10.0)
    assert k(bad, sales=[sale], jumps=[jump], source="replay") == set()
    assert judged_families(trade(), ctx(), "replay", rel[1]) == ["news", "chop", "failed_breakout", "noise"]
    # the daily trend rule's rows: the chart families are not judged
    trend_ctx = ctx(strategy="trend", regime_base="chop", stop_pct=0.0, normal_dip=0.5)
    assert k(trend_ctx) == set() and judged_families(trade(), trend_ctx, "live", rel[1]) == ["execution", "news"]
    assert judged_families(trade(), None, "live", rel[1]) == ["execution", "news"]          # no entry record
    assert judged_families(trade(), ctx(), "live", None) == ["execution", "chop", "failed_breakout", "noise"]


# ---- 6. statistics and the book ----
def test_statistics_are_exact():
    assert binom_tail(9, 10, 0.5) == 0.0107421875          # R: binom.test(9, 10, alternative = "greater") 0.01074
    assert binom_tail(0, 10, 0.3) == 1.0 and binom_tail(11, 10, 0.3) == 0.0
    assert binom_tail(1100, 1200, 0.9) == pytest.approx(sum(
        __import__("math").comb(1200, i) * 0.9 ** i * 0.1 ** (1200 - i) for i in range(1100, 1201)), rel=1e-9)
    assert sidak(0.05, 5) == pytest.approx(0.010206, abs=1e-6) and sidak(0.05, 8) == pytest.approx(0.006391, abs=1e-6)
    assert fisher_greater(3, 1, 1, 3) == pytest.approx(17 / 70)             # Fisher's tea-tasting table: 0.2428...
    assert fisher_greater(0, 5, 0, 5) == 1.0
    assert chance_text(0.0022) == "about 1 in 450" and chance_text(1 / 1176) == "about 1 in 1,200"
    assert chance_text(0.6) == "more than 1 in 2" and chance_text(1 / 7) == "about 1 in 7"


def test_the_book_judges_against_the_base_rate():
    # base rate = the pool's share of losers
    pool = rows("a", 20, 13) + rows("b", 20, 13, t0=50 * H)
    v = MemoryBook(pool).verdict("a", 100 * H)
    assert v["pool"] == 40 and v["base"] == 26 / 40 and v["n"] == 20 and v["losses"] == 13
    # below 30 trades in memory, or below 10 similar: unknown, never a refusal
    v = MemoryBook(rows(KEY, 29, 29)).verdict(KEY, 100 * H)
    assert v["ok"] is None and v["detail"] == "only 29 trades in memory (needs 30) - it does not judge yet"
    v = MemoryBook(rows(KEY, 9, 9) + rows("x", 30, 10, t0=10 * H)).verdict(KEY, 100 * H)
    assert v["ok"] is None and v["detail"].startswith("9 similar past trades (4-hour chart in chop") \
        and v["detail"].endswith("- it judges from 10")
    # 19 of 20 at a base of 65% with one group: refused
    others = []
    for c in range(9):                                  # 80 more trades in 9 small cells (under 10 each): one group
        others += rows(f"o{c}", 9 if c < 8 else 8, [6, 5, 5, 5, 5, 5, 5, 5, 5][c], t0=(30 + c * 10) * H)
    v = MemoryBook(rows(KEY, 20, 19) + others).verdict(KEY, 200 * H)
    assert v["base"] == pytest.approx(0.65) and v["groups"] == 1 and v["ok"] is False
    assert v["p"] == pytest.approx(binom_tail(19, 20, 0.65)) and v["level"] == pytest.approx(0.05)
    # 17 of 20 with eight groups: the Šidák limit (0.64%) is not reached; with one group (5%) it would be
    eight = rows(KEY, 20, 17)
    for c in range(7):
        eight += rows(f"g{c}", 10, [6, 6, 6, 6, 6, 6, 5][c], t0=(30 + c * 10) * H)
    v = MemoryBook(eight).verdict(KEY, 200 * H)
    assert v["groups"] == 8 and v["ok"] is True and v["level"] < v["p"] < 0.05
    one = rows(KEY, 20, 17)
    for c in range(8):
        one += rows(f"s{c}", 9 if c < 7 else 7, 5, t0=(30 + c * 10) * H)
    v1 = MemoryBook(one).verdict(KEY, 200 * H)
    assert v1["groups"] == 1 and v1["ok"] is False
    # a row closed at the decision counts, one closed a millisecond later does not; 365 days back it has left
    book = MemoryBook([{"bucket": KEY, "closed_ms": 1000 * H, "loss": True}])
    assert book.verdict(KEY, 1000 * H)["n"] == 1
    assert MemoryBook([{"bucket": KEY, "closed_ms": 1000 * H + 1, "loss": True}]).verdict(KEY, 1000 * H)["n"] == 0
    old = MemoryBook([{"bucket": KEY, "closed_ms": 0, "loss": True}, {"bucket": KEY, "closed_ms": DAY, "loss": True}])
    assert old.verdict(KEY, 366 * DAY)["n"] == 0 and old.verdict(KEY, 365 * DAY)["n"] == 1   # 0 is exactly 365 days back


def test_incremental_book_equals_brute_force():
    rng = random.Random(7)
    buckets = [KEY, "v1|4h:up|btc:above|h:00-06|rel:no", "v1|4h:chop|btc:above|h:18-24|rel:no", "u"]
    for case in range(200):
        n = rng.randint(0, 120)
        pool = sorted(({"bucket": rng.choice(buckets), "closed_ms": rng.randint(0, 900) * DAY,
                        "loss": rng.random() < 0.68} for _ in range(n)), key=lambda r: r["closed_ms"])
        rule = "owner60" if case % 5 == 0 else "test"
        book = MemoryBook(pool, rule)
        grow = MemoryBook([], rule)                       # the replay's way: rows added as they close
        added = 0
        for t in sorted(rng.randint(0, 1000) * DAY for _ in range(50)):
            while added < len(pool) and pool[added]["closed_ms"] <= t:
                grow.add(pool[added])
                added += 1
            b = rng.choice(buckets)
            want = verdict_brute(pool, b, t, rule)
            assert book.verdict(b, t) == want and grow.verdict(b, t) == want


def test_tags_and_outcome_details_never_change_a_verdict():
    rng = random.Random(3)
    plain = [{"bucket": rng.choice([KEY, "x"]), "closed_ms": i * DAY, "loss": rng.random() < 0.7} for i in range(200)]
    noisy = [{**r, "breakout_failed": rng.choice([0, 1, None]), "tags": ["noise"], "pnl": -5} for r in plain]
    a, b = MemoryBook(plain), MemoryBook(noisy)
    for t in range(0, 220 * DAY, 7 * DAY):
        assert a.verdict(KEY, t) == b.verdict(KEY, t)
    src = inspect.getsource(MemoryBook)
    assert "breakout_failed" not in src and "tags" not in src and "pnl" not in src


def test_the_owner60_rule_is_replay_only_and_has_no_pool_minimum():
    assert MemoryBook(rows(KEY, 10, 7), "owner60").verdict(KEY, 100 * H)["ok"] is False      # 70% > 60%
    assert MemoryBook(rows(KEY, 10, 6), "owner60").verdict(KEY, 100 * H)["ok"] is True       # 60% is not more
    assert MemoryBook(rows(KEY, 10, 7)).verdict(KEY, 100 * H)["ok"] is None   # the default rule: too few in memory
    with pytest.raises(ValueError):
        MemoryBook([], "sixty")
    assert "owner60" not in inspect.getsource(Orchestrator)          # the live desk only ever builds the default rule


# ---- 7. constants and settings ----
def test_constants_are_fixed_and_no_setting_exists(monkeypatch):
    assert (memory.MIN_SIMILAR, memory.MIN_POOL, memory.FAMILY_ALPHA, memory.MEMORY_DAYS) == (10, 30, 0.05, 365)
    assert (memory.HOUR_BLOCK, memory.RELEASE_NEAR_MIN, memory.NOISE_HOURS, memory.NOISE_DAYS) == (6, 120, 2, 7)
    assert (memory.NOISE_QUANTILE, memory.ENTRY_GAP_ATR, memory.DEMO_GAP_LIMIT, memory.PARTIAL_SHARE) == (0.9, 0.25, 0.005, 0.98)
    assert memory.STOP_SLIP_LIMIT == pytest.approx(0.004) and memory.OWNER_RULE_LOSS_SHARE == 0.60
    assert (memory.CAUSE_MIN_LOSERS, memory.CAUSE_MIN_WINNERS, memory.SEED_DAYS) == (20, 10, 365)
    assert not any("memory" in f.name for f in dataclasses.fields(Settings))
    assert not any("MEMORY" in k or "POSTMORTEM" in k for k in ENV_KEYS)
    monkeypatch.setenv("MENTOR_CHECKS", "trend200,memory")
    assert Settings.load().mentor_checks == ("trend200", "memory")
    assert "memory" not in Settings().mentor_checks and "memory" not in setup_wizard.DEFAULTS["MENTOR_CHECKS"]
    assert "memory" not in StrategyCard("x").checks and "memory" in ALL_CHECKS
    assert StrategyCard(name="x", checks=("memory",)).violations() == []
    enum = PROPOSAL_SCHEMA["properties"]["proposals"]["items"]["properties"]["checks"]["items"]["enum"]
    assert "memory" not in enum and "regime" in enum
    assert any("memory" in title for title, _ in setup_wizard.LAYOUT)
    assert mentor_module.SOURCES["memory"].startswith("the desk's own record")


# ---- 8. the checklist rule ----
def test_mentor_memory_rule_and_its_lines():
    args = ("ETHUSDT", bars(flat(60, 100)), [], 100, 98, 104, 1, None, None, 0)
    r = MentorAgent(("reward",)).review(*args, memory={"ok": False, "detail": "x"})
    assert "memory" not in r["checks"] and r["pass"]
    r = MentorAgent(("memory",)).review(*args)
    assert r["pass"] and r["checks"]["memory"]["ok"] is None and r["checks"]["memory"]["detail"] == "no memory verdict"
    assert MentorAgent(("memory",)).review(*args, memory={"ok": None, "detail": "too few"})["pass"]
    r = MentorAgent(("memory", "reward")).review(*args, memory={"ok": False, "detail": "refused"})
    assert not r["pass"] and r["failed"] == ["memory"]
    desc = describe_bucket(KEY)
    v = {"ok": True, "rule": "test", "bucket": KEY, "n": 12, "losses": 8, "pool": 312, "pool_losses": 215, "base": 0.69,
         "p": 0.6, "level": 1 / 176, "groups": 9}
    assert checklist_detail(v) == f"8 of 12 similar past trades lost (67%; {desc}) against 69% of all 312 in memory - nothing unusual"
    v.update(n=20, losses=17, p=1 / 11)
    assert checklist_detail(v) == (f"17 of 20 similar past trades lost (85%; {desc}) against 69% of all 312 in memory - "
                                   "chance does that about 1 in 11, the limit is 1 in 176")
    v.update(ok=False, losses=19, pool=280, base=0.65, groups=7, p=1 / 470, level=1 / 137)
    line = checklist_detail(v)
    assert line == (f"19 of 20 similar past trades lost (95%; {desc}) against 65% of all 280 in memory (7 groups judged) - "
                    "chance does that only about 1 in 470 (limit 1 in 137): buy refused")
    assert MentorAgent.summary({"checks": {"memory": {"ok": False, "detail": line, "source": "s"}}}).startswith("✗ memory: 19 of 20")
    assert checklist_detail({"ok": None, "rule": "test", "bucket": KEY, "n": 3, "losses": 3, "pool": 22}) == \
        "only 22 trades in memory (needs 30) - it does not judge yet"
    assert checklist_detail({"ok": None, "rule": "test", "bucket": KEY, "n": 7, "losses": 3, "pool": 40}) == \
        f"7 similar past trades ({desc}) - it judges from 10"


# ---- 9. the live cycle ----
def test_a_buy_writes_one_context_row_with_every_field():
    series = {"BTCUSDT": cycle_series()}
    bot = desk(series)
    force_buy(bot)
    bot.run_cycle()
    pos = bot.journal.positions()["BTCUSDT"]
    ctxs = bot.journal.trade_contexts("live")
    assert len(ctxs) == 1
    row = ctxs[0]
    assert row["opened_at"] == pos["opened_at"] and row["symbol"] == "BTCUSDT" and row["source"] == "live"
    assert row["decision_ms"] == series["BTCUSDT"][-1]["time"] + H and row["signal_price"] == series["BTCUSDT"][-1]["close"]
    assert row["strategy"] == "hourly" and row["card_id"] == bot.card.id and row["features_version"] == 1
    assert row["bucket"].startswith("v1|4h:") and row["regime_4h"] and row["regime_base"] and row["backfilled"] == 0
    assert row["latest_event"] == "4h breakout" and row["breakout_level"] and row["breakout_hours"] == 8
    for col in ("score", "technical", "prediction", "news", "forex", "rsi", "atr_pct", "stop_pct",
                "normal_dip", "release_near", "hour_utc", "weekday", "decision_price", "fill_price", "market_price",
                "quote_planned", "quote_filled", "created_at"):
        assert row[col] is not None, col
    assert row["fill_price"] == pos["entry_price"] and row["quote_filled"] == pos["cost"]
    assert bot.journal.add_trade_context({**{k: v for k, v in row.items() if k != "id"}}) is None   # one row per buy
    with pytest.raises(ValueError):
        bot.journal.add_trade_context({"source": "live", "nonsense": 1})


def test_a_failing_context_write_still_opens_and_protects(monkeypatch, caplog):
    bot = desk({"BTCUSDT": cycle_series()})
    force_buy(bot)
    monkeypatch.setattr(bot.journal, "add_trade_context", lambda row: 1 / 0)
    bot.run_cycle()
    assert bot.journal.positions()["BTCUSDT"]["protected"] == "exchange" and "protect" in bot.broker.calls
    assert bot.journal.trade_contexts("live") == [] and "Recording the entry record" in caplog.text


def test_the_trend_step_writes_a_trend_row():
    from tests.test_trend import candles as daily_candles, trend_desk, wavy
    bot, j = trend_desk({"AAAUSDT": daily_candles(wavy(130, 100, 0.02))}, positions=1)
    bot.run_cycle()
    rows_ = j.trade_contexts("live")
    assert len(rows_) == 1 and rows_[0]["strategy"] == "trend" and rows_[0]["card_id"] == "trend"
    assert rows_[0]["bucket"] is None and rows_[0]["opened_at"] == j.positions()["AAAUSDT"]["opened_at"]
    assert rows_[0]["decision_ms"] % DAY == 0 and rows_[0]["stop_pct"] == pytest.approx(0.25)


def _seeded_refusal(bot, bucket, decision_ms):
    """20 losing replay trades of the champion's card in `bucket` and 20 others (6 lost): 26 of 40 = 65% base."""
    seed = rows(bucket, 20, 20, t0=H) + rows("v1|other", 20, 6, t0=H)
    out = [{"source": "replay", "symbol": f"S{k}USDT", "opened_at": iso(r["closed_ms"] - H), "strategy": "hourly",
            "card_id": bot.card.id, "features_version": 1, "bucket": r["bucket"], "closed_at": iso(r["closed_ms"]),
            "pnl": -1.0 if r["loss"] else 1.0} for k, r in enumerate(seed)]
    bot.journal.replace_seed(out, {"card_id": bot.card.id, "trades": 40})
    assert all(r["closed_ms"] <= decision_ms for r in seed)


def test_memory_on_refuses_a_cell_far_worse_than_usual(monkeypatch):
    series = {"BTCUSDT": cycle_series()}
    probe = desk(series, checks=("memory",))
    force_buy(probe)
    probe.run_cycle()                                    # an empty memory refuses nothing ...
    assert "BTCUSDT" in probe.journal.positions()
    bucket = probe.journal.trade_contexts("live")[0]["bucket"]
    bot = desk(series, checks=("memory",))
    asked = []

    def decide(sym, reports, holding, use_ai=True, threshold=None):
        asked.append(use_ai)
        return {"action": "BUY", "confidence": 0.9, "reasoning": "r", "source": "rules", "combined_score": 0.5}
    bot.decision.run = decide
    bot.apply_champion()
    _seeded_refusal(bot, bucket, series["BTCUSDT"][-1]["time"] + H)
    out = bot.run_cycle()                                # ... the same buy with a record far worse than usual is refused
    assert bot.journal.positions() == {} and asked == [False]          # the AI is not consulted for a refused buy
    row = bot.journal.db.execute("SELECT risk_reason, reports FROM decisions WHERE symbol='BTCUSDT'").fetchone()
    assert row["risk_reason"] == "master traders' checklist: memory not met"
    assert "✗ memory: 20 of 20 similar past trades lost" in json.loads(row["reports"])["mentor"]["checklist"]
    assert any("memory not met" in str(r.get("risk")) for r in out)
    # a broken memory gives "unknown" and the buy goes ahead
    broken = desk(series, checks=("memory",))
    force_buy(broken)
    broken.apply_champion()
    _seeded_refusal(broken, bucket, series["BTCUSDT"][-1]["time"] + H)
    monkeypatch.setattr(memory, "entry_features", lambda *a, **k: 1 / 0)
    broken.run_cycle()
    assert "BTCUSDT" in broken.journal.positions()
    rep = json.loads(broken.journal.db.execute("SELECT reports FROM decisions").fetchone()["reports"])
    assert "? memory: memory unavailable this check (see the log)" in rep["mentor"]["checklist"]


# ---- 10. live and replay agree ----
def test_memory_check_live_and_replay_agree():
    history = synthetic("BTCUSDT", 1500, 4, drift=0.0003)
    ds = DailySeries(history)
    a = ChartEventsAgent("1h")
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=("memory",))
    rel = memory.release_times(None)
    pool = [r for r in rows(KEY, 20, 20, t0=400 * H) + rows("v1|other", 20, 6, t0=400 * H)]

    def replay_side(i):
        c = history[i - 199: i + 1]
        price = c[-1]["close"]
        from trader.agents.technical import TechnicalAnalysisAgent
        atr = TechnicalAnalysisAgent().run(c)["atr"]
        chart = a.run(c, ds.completed[: ds.before[i]])
        f = entry_features("BTCUSDT", c, price, atr, price - s.stop_loss_atr * atr, chart, ds.at(i, price),
                           ds.at(i, price), *rel, "1h")
        return f, MemoryBook(pool).verdict(f["bucket"], f["decision_ms"])

    def live_side(i, bucket):
        bot = desk({"BTCUSDT": history[i - 198: i + 1]}, checks=("memory",))   # 199 finished candles, as live
        bot.market.daily = {"BTCUSDT": ds.completed[: ds.before[i]]}
        force_buy(bot)
        bot.apply_champion()
        seed = [r for r in rows(bucket, 20, 20, t0=400 * H) + rows("v1|other", 20, 6, t0=400 * H)]
        bot.journal.replace_seed([{"source": "replay", "symbol": f"S{k}USDT", "opened_at": iso(r["closed_ms"] - H),
                                   "strategy": "hourly", "card_id": bot.card.id, "features_version": 1,
                                   "bucket": r["bucket"], "closed_at": iso(r["closed_ms"]),
                                   "pnl": -1.0 if r["loss"] else 1.0} for k, r in enumerate(seed)], {})
        seen = {}
        real = bot.mentor.review

        def spy(*args, **kw):
            seen["memory"] = kw.get("memory")
            return real(*args, **kw)
        bot.mentor.review = spy
        bot.run_cycle()
        return bot, seen

    boundary = next(i for i in range(700, 1500) if (history[i]["time"] + H) % DAY == 0)   # the first candle of a day
    for i in (700, 911, boundary):                       # the pool refuses the buy's own cell: a refusal on both sides
        f, _ = replay_side(i)
        pool = rows(f["bucket"], 20, 20, t0=400 * H) + rows("v1|other", 20, 6, t0=400 * H)
        v = MemoryBook(pool).verdict(f["bucket"], f["decision_ms"])
        bot, seen = live_side(i, f["bucket"])
        assert seen["memory"] == v and v["ok"] is False
        assert bot.journal.positions() == {}
        assert bot.journal.db.execute("SELECT risk_reason FROM decisions").fetchone()[0] == \
            "master traders' checklist: memory not met"
    f, _ = replay_side(800)                              # a pass: the pool's refused cell is not the buy's cell
    v = MemoryBook(rows("v1|elsewhere", 20, 20, t0=400 * H) + rows("v1|other", 20, 6, t0=400 * H)).verdict(
        f["bucket"], f["decision_ms"])
    bot, seen = live_side(800, "v1|elsewhere")
    assert seen["memory"] == v and v["ok"] is None and "BTCUSDT" in bot.journal.positions()
    # the features themselves: the live desk's c["entry"] equals the replay's (199 and 200 candles)
    i = 911
    f, _ = replay_side(i)
    bot = desk({"BTCUSDT": history[i - 198: i + 1]}, checks=("memory",))
    bot.market.daily = {"BTCUSDT": ds.completed[: ds.before[i]]}
    force_buy(bot)
    captured = {}
    real_features = memory.entry_features

    def spy_features(*args, **kw):
        captured["f"] = real_features(*args, **kw)
        return captured["f"]
    memory.entry_features, saved = spy_features, memory.entry_features
    try:
        bot.run_cycle()
    finally:
        memory.entry_features = saved
    assert captured["f"] == f
    row = bot.journal.trade_contexts("live")[0]
    assert row["bucket"] == f["bucket"] and row["normal_dip"] == f["normal_dip"] and row["stop_pct"] == f["stop_pct"]


# ---- 11. no look-ahead ----
def test_future_prices_never_change_the_features_or_the_verdicts(monkeypatch):
    history = synthetic("BTCUSDT", 1600, 4, drift=0.0003)
    cut = 1100
    rng = random.Random(1)
    future = [dict(c, close=c["close"] * (1 + rng.uniform(-0.2, 0.2)), low=c["low"] * 0.7) for c in history[cut + 1:]]
    perturbed = history[: cut + 1] + future
    rel = memory.release_times(None)
    a = ChartEventsAgent("1h")

    def at(hist, i):
        ds = DailySeries(hist)
        c = hist[i - 199: i + 1]
        return entry_features("BTCUSDT", c, c[-1]["close"], 1.0, c[-1]["close"] - 2, a.run(c, ds.completed[: ds.before[i]]),
                              ds.at(i, c[-1]["close"]), None, *rel, "1h")
    for i in (700, 900, cut):
        assert at(history, i) == at(perturbed, i)
    # (c) the replay: flip the outcome of every trade closing after T; every verdict up to T is unchanged
    hist3 = {s: synthetic(s, 2400, k, drift=0.0002) for k, s in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT"))}
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", 0.05)
    s = Settings(offline=True, symbols=list(hist3), mentor_checks=("memory",))
    T = hist3["AAAUSDT"][1500]["time"]

    def trace(flip):
        seen = []
        real_verdict = MemoryBook.verdict

        def spy(self, bucket, decision_ms):
            v = real_verdict(self, bucket, decision_ms)
            seen.append((decision_ms, bucket, v["n"], v["losses"], v["pool"], v["pool_losses"], v["ok"]))
            return v
        real_close = Backtest._close

        def flipping(positions, trades, sym, px, reason, t, step=0):
            out = real_close(positions, trades, sym, px, reason, t, step=step)
            if flip and trades[-1]["closed_ms"] > T:
                trades[-1]["loss"] = not trades[-1]["loss"]
            return out
        with monkeypatch.context() as m:
            m.setattr(MemoryBook, "verdict", spy)
            m.setattr(Backtest, "_close", staticmethod(flipping))
            Backtest(s, hist3, releases=rel).run()
        return seen
    plain, flipped = trace(False), trace(True)
    assert len(plain) > 50
    assert [x for x in plain if x[0] <= T] == [x for x in flipped if x[0] <= T]
    assert [x for x in plain if x[0] > T] != [x for x in flipped if x[0] > T]     # the test has power
    # (d) a trade closing during candle t is visible to the decision at t + one candle, not earlier
    r = Backtest(Settings(offline=True, symbols=list(hist3), mentor_checks=("memory",)), hist3, record=True,
                 releases=rel).run()
    rec = r["trade_records"][0]
    assert rec["closed_ms"] == rec["t"] + H
    book = MemoryBook([rec])
    assert book.verdict(rec["bucket"], rec["t"] + H)["pool"] == 1
    assert MemoryBook([rec]).verdict(rec["bucket"], rec["t"])["pool"] == 0


# ---- 12. the seed ----
def _seed_fetch(calls):
    def fetch(sym, days_back):
        calls.append((sym, days_back))
        return synthetic(sym, 2400, 1 + len(sym) % 5, drift=0.0002)
    return fetch


def test_the_seed_replays_the_card_without_its_memory_rule(monkeypatch):
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", 0.05)
    base = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"])
    now = 2400 * H
    out = {}
    for name, checks in (("with", ("reward", "memory")), ("without", ("reward",))):
        j = Journal(":memory:", 10_000)
        card = StrategyCard(name=name, checks=checks, buy_threshold=0.05)
        calls = []
        meta = memory.run_seed(base, card, j, None, ["BTCUSDT", "ETHUSDT"], now, days=60, fetch=_seed_fetch(calls))
        assert meta["error"] is None and meta["card_id"] == card.id and meta["trades"] > 0 and calls
        rows_ = j.trade_contexts("replay")
        assert rows_ and all(r["card_id"] == card.id and r["closed_at"] and r["exit_reason"] and r["pnl"] is not None
                             for r in rows_)
        out[name] = [(r["symbol"], r["opened_at"], r["closed_at"], round(r["pnl"], 9), r["bucket"]) for r in rows_]
        assert j.get("memory_seed")["trades"] == len(rows_)
        out[name + "_card"] = card.id
        out[name + "_j"] = j
    assert out["with"] == out["without"]                  # the memory rule is never applied inside its own seed
    j = out["with_j"]
    assert memory.pool_rows(j, out["with_card"], []) and memory.pool_rows(j, "another-card", []) == []
    # a failing insert keeps the old seed
    before = j.trade_contexts("replay")
    with pytest.raises(ValueError):
        j.replace_seed([{"source": "replay", "symbol": "X", "opened_at": "x", "bogus": 1}], {"card_id": "new"})
    assert j.trade_contexts("replay") == before and j.get("memory_seed")["card_id"] == out["with_card"]
    # a failed download records the reason and keeps the rows
    meta = memory.run_seed(base, StrategyCard(name="x"), j, None, ["BTCUSDT"], now, days=60,
                           fetch=lambda sym, d: (_ for _ in ()).throw(ConnectionError("no network")))
    assert "no network" in meta["error"] and j.trade_contexts("replay") == before


def test_the_automatic_seed_runs_only_when_it_is_due(monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod, "run_memory_seed", lambda *a, **k: calls.append(a) or {})
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    j = Journal(":memory:", 10_000)
    cards = {"c1": StrategyCard(name="one", checks=("reward", "memory")),
             "c2": StrategyCard(name="two", checks=("reward", "memory"), cooldown_hours=12.0)}

    def bot_with(checks, client=object(), card="c1"):
        return SimpleNamespace(s=SimpleNamespace(mentor_checks=checks), journal=j, card=cards[card],
                               market=SimpleNamespace(client=client))
    online = SimpleNamespace(trend=False, offline=False)
    main_mod.maybe_memory_seed(bot_with(("reward",)), None, online, None)                      # memory off
    main_mod.maybe_memory_seed(bot_with(("memory",)), None, SimpleNamespace(trend=True, offline=False), None)
    main_mod.maybe_memory_seed(bot_with(("memory",)), None, SimpleNamespace(trend=False, offline=True), None)
    main_mod.maybe_memory_seed(bot_with(("memory",), client=None), None, online, None)
    assert calls == []
    main_mod.maybe_memory_seed(bot_with(("memory",)), None, online, None)
    assert len(calls) == 1
    j.set("memory_seed", {"card_id": cards["c1"].id, "tried_day": today, "trades": 5, "error": None})
    main_mod.maybe_memory_seed(bot_with(("memory",), card="c2"), None, online, None)          # tried today
    assert len(calls) == 1
    j.set("memory_seed", {"card_id": cards["c1"].id, "tried_day": "2000-01-01", "trades": 5, "error": None})
    main_mod.maybe_memory_seed(bot_with(("memory",)), None, online, None)                     # the same card: done
    assert len(calls) == 1
    main_mod.maybe_memory_seed(bot_with(("memory",), card="c2"), None, online, None)          # a new card: again
    assert len(calls) == 2


# ---- 13, 28. the backfill ----
def _closed_trade(j, sym, reports, source="rules", entry=100.0, exit_=95.0, decision=True):
    if decision:
        j.log_decision(sym, {"action": "BUY", "confidence": 0.9, "source": source, "combined_score": 0.4,
                             "reasoning": "r"}, {"approved": True, "reason": "ok"}, reports)
    fill = {"ok": True, "qty": 1.0, "avg_price": entry, "quote": entry, "market_price": entry * 0.999}
    j.log_order(sym, "BUY", fill, "signal")
    j.open_position(sym, fill, entry * 0.9, entry * 1.2)
    sell = {"ok": True, "qty": 1.0, "avg_price": exit_, "quote": exit_, "market_price": exit_}
    j.log_order(sym, "SELL", sell, "stop-loss")
    return j.close_position(sym, sell, "stop-loss")


def test_the_backfill_rebuilds_entry_records_from_stored_decisions(monkeypatch):
    from trader.agents.market import MarketDataAgent
    for name in ("candles", "daily_rows", "daily_closes", "daily_candles", "price"):
        monkeypatch.setattr(MarketDataAgent, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    j = Journal(":memory:", 10_000)
    reports = {"technical": {"score": 0.3, "price": 100.0, "atr": 2.0, "rsi": 61.0}, "prediction": {"score": 0.2},
               "news": {"score": 0.1, "source": "keywords"}, "forex": {"score": 0.0},
               "chart": {"4h": "chop", "1h": "trending up", "1d": "trending up", "summary": "x"},
               "mentor": {"pass": True, "checklist": "✓ trend200: x [P]\n✓ btc: Bitcoin 90,000 vs 80,000 [D]"}}
    _closed_trade(j, "ETHUSDT", reports)
    _closed_trade(j, "SOLUSDT", reports, decision=False)                           # no entry decision: no row
    _closed_trade(j, "ADAUSDT", {"trend": {"close": 1}}, source="trend")
    btc = dict(reports, mentor={"pass": True, "checklist": "✗ trend200: below [P]\n✓ btc: this is Bitcoin itself [D]"})
    _closed_trade(j, "BTCUSDT", btc, entry=60_000.0, exit_=66_000.0)
    s = Settings(offline=True, symbols=["BTCUSDT"])
    Orchestrator(s, j)                                         # the first start of 1.9.1 back-fills once
    got = {r["symbol"]: r for r in j.trade_contexts("live")}
    assert set(got) == {"ETHUSDT", "ADAUSDT", "BTCUSDT"} and j.get("memory_backfilled")
    eth = got["ETHUSDT"]
    assert (eth["regime_4h"], eth["regime_base"], eth["btc_above"], eth["backfilled"]) == ("chop", "trending up", 1, 1)
    assert eth["bucket"].startswith("v1|4h:chop|btc:above|h:") and eth["fill_price"] == 100.0
    assert eth["atr_pct"] == pytest.approx(0.02) and eth["rsi"] == 61.0 and eth["news_source"] == "keywords"
    assert eth["stop_pct"] is None and eth["normal_dip"] is None and eth["breakout_level"] is None
    assert eth["decision_ms"] % H == 0 and eth["signal_price"] is None
    assert got["ADAUSDT"]["strategy"] == "trend" and got["ADAUSDT"]["bucket"] is None
    assert got["BTCUSDT"]["btc_above"] == 0                    # Bitcoin's side comes from its trend200 line
    assert memory.backfill(j, *memory.release_times(None)) == 0          # idempotent
    # the entry-gap kind is not judged for a back-filled row (no finished-candle close was stored)
    c = dict(got["BTCUSDT"], fill_price=70_000.0, market_price=70_000.0)
    assert "entry_gap" not in kinds_of(causes(trade(), c, [], [], [], 0, "live"))
    assert "failed_breakout" not in judged_families(trade(), c, "live", (0, 10 ** 15))


# ---- 14, 27. outcomes and the pool ----
def test_outcomes_partial_sales_open_positions_and_trend_eras():
    j = Journal(":memory:", 10_000)
    j.open_position("ETHUSDT", {"qty": 1.0, "avg_price": 100.0, "quote": 100.0}, 90.0, 120.0)
    j.close_position("ETHUSDT", {"qty": 0.4, "avg_price": 110.0, "quote": 44.0}, "take-profit")
    assert j.closed_positions() == []                          # partly sold: not finished
    j.close_position("ETHUSDT", {"qty": 0.6, "avg_price": 95.0, "quote": 57.0}, "stop-loss")
    (p,) = j.closed_positions()
    assert p["sales"] == 2 and p["pnl"] == pytest.approx(1.0) and not p["loss"] and p["exit_reasons"] == ["take-profit", "stop-loss"]
    opened = p["opened_at"]
    j.add_trade_context({"source": "live", "symbol": "ETHUSDT", "opened_at": opened, "strategy": "hourly",
                         "features_version": 1, "bucket": KEY, "card_id": "c"})
    assert [r["loss"] for r in memory.pool_rows(j, "c", [])] == [False]
    assert memory.pool_rows(j, "c", [(opened, None)]) == []    # a trade in a daily-trend-rule era is left out


def test_the_pool_is_split_strictly_at_the_live_start():
    j = Journal(":memory:", 10_000)
    t = lambda h: iso(1_750_000_000_000 + h * H)   # noqa: E731
    for sym, opened, bucket in (("AAAUSDT", t(0), "v1|4h:unknown|btc:above|h:00-06|rel:no"),   # back-filled, no chart
                                ("BBBUSDT", t(100), KEY)):                                     # the live start
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, cost, pnl, pnl_pct, exit_reason) "
                     "VALUES (?,?,?,?,?,?,?)", (sym, opened, t(101 if sym == "BBBUSDT" else 1), 100, -1, -1, "stop-loss"))
        j.add_trade_context({"source": "live", "symbol": sym, "opened_at": opened, "strategy": "hourly",
                             "features_version": 1, "bucket": bucket, "backfilled": int(sym == "AAAUSDT")})
    j.db.commit()
    seed = [{"source": "replay", "symbol": "XUSDT", "opened_at": t(h), "strategy": "hourly", "card_id": "c",
             "features_version": 1, "bucket": KEY, "closed_at": t(h + 1), "pnl": -1.0} for h in (50, 99, 100, 150)]
    j.replace_seed(seed, {"card_id": "c"})
    assert memory.live_start(j) == t(100)
    got = memory.pool_rows(j, "c", [])
    assert sorted((r["source"], r["symbol"], iso(r["opened_ms"])) for r in got) == [
        ("live", "BBBUSDT", t(100)), ("replay", "XUSDT", t(50)), ("replay", "XUSDT", t(99))]


# ---- 15. the scorecard of the live refusals ----
def test_memory_refusals_are_judged_on_later_prices():
    j = Journal(":memory:", 10_000)
    s = Settings(offline=True)
    reports = {"technical": {"price": 100.0, "atr": 1.0}}
    reason = "master traders' checklist: memory not met"
    for sym, ts, why in (("ETHUSDT", "2026-01-01T00:00:00+00:00", reason),
                         ("ETHUSDT", "2026-01-01T01:00:00+00:00", reason),        # while the first is open: skipped
                         ("SOLUSDT", "2026-01-01T00:00:00+00:00", "master traders' checklist: trend200, memory not met"),
                         ("BTCUSDT", "2026-01-02T00:00:00+00:00", reason)):
        j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, risk_approved, risk_reason, reports) VALUES "
                     "(?,?,?,?,?,?,?)", (ts, sym, "BUY", 0.9, 0, why, json.dumps(reports)))
    j.db.commit()
    asked = []

    def falling(sym, since_ms):
        asked.append(sym)
        return [{"time": since_ms + k * H, "open": 100, "high": 100.2, "low": 100 - k * 0.5, "close": 100 - k * 0.5}
                for k in range(130)]
    card = checklist_scorecard(j, s, falling)
    assert card["cases"] == 2 and card["losers"] == 2 and card["saved_usd"] > 0 and card["cost_usd"] == 0
    assert sorted(asked) == ["BTCUSDT", "ETHUSDT"] and card["verdict"] == "not enough refused buys judged yet (need 30)"
    again = checklist_scorecard(j, s, lambda sym, since_ms: (_ for _ in ()).throw(AssertionError("cached")))
    assert again == card
    text = scorecard_text({"cases": 0, "saved_usd": 0, "cost_usd": 0, "net_usd": 0, "verdict": "v"},
                          {"judged": 0, "hit_rate_pct": None, "note": "n"}, 0.0, memory=card)
    assert "🧠 Memory rule refusals judged: 2 · would have lost 2 (100%" in text and "need 30" in text
    plain = scorecard_text({"cases": 0, "saved_usd": 0, "cost_usd": 0, "net_usd": 0, "verdict": "v"},
                           {"judged": 0, "hit_rate_pct": None, "note": "n"}, 0.0,
                           memory=checklist_scorecard(Journal(":memory:", 1), s, falling))
    assert "Memory rule" not in plain


# ---- 16-18, 24, 25. the texts, the lessons, the digest, the notebook, the AI's inputs ----
def populated(checks=("trend200", "btc", "cooldown", "reward")):
    """A journal with closed trades, entry records and a replay seed, and its settings."""
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=checks)
    j = Journal(":memory:", 10_000)
    card = StrategyCard.from_settings(s)
    j.set("strategy_champion", {"card": card.to_dict(), "since": "2026-01-01", "by": "owner settings", "previous": None})
    rng = random.Random(5)
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
    for k in range(40):
        sym = ("BTCUSDT", "ETHUSDT")[k % 2]
        opened = (base + dt.timedelta(hours=10 * k)).isoformat(timespec="seconds")
        closed = (base + dt.timedelta(hours=10 * k + 4)).isoformat(timespec="seconds")
        loss = rng.random() < 0.65
        pnl = -5.0 if loss else 12.0
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, "
                     "proceeds, pnl, pnl_pct, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sym, opened, closed, 100, 100 + pnl / 10, 10, 1000, 1000 + pnl, pnl, pnl / 10,
                      "stop-loss (exchange)" if loss else "take-profit (exchange)"))
        j.db.execute("INSERT INTO orders (ts, symbol, side, ok, avg_price, quote, reason, market_price) VALUES "
                     "(?,?,?,?,?,?,?,?)", (closed, sym, "SELL", 1, 99.3 if loss else 101.2, 1000 + pnl,
                                           "stop-loss (exchange)" if loss else "take-profit (exchange)",
                                           100.0 if loss else 101.2))
        j.add_trade_context({"source": "live", "symbol": sym, "opened_at": opened, "strategy": "hourly",
                             "card_id": card.id, "features_version": 1, "bucket": KEY if k % 3 else "v1|other",
                             "regime_base": "chop" if k % 2 else "trending up", "regime_4h": "trending up",
                             "latest_event": "1h breakout" if k % 4 == 0 else None, "breakout_level": 101.0,
                             "breakout_hours": 6, "breakout_failed": int(loss) if k % 4 == 0 else None,
                             "stop_pct": 0.02, "normal_dip": 0.025 if k % 5 == 0 else 0.01, "signal_price": 100.0,
                             "fill_price": 100.0, "decision_price": 100.0, "atr_pct": 0.01, "market_price": 100.0,
                             "quote_planned": 1000.0, "quote_filled": 1000.0, "decision_ms": ms_of(opened)})
    j.db.commit()
    seed = [{"source": "replay", "symbol": "ETHUSDT", "opened_at": iso(ms_of(base.isoformat()) - (300 - h) * H),
             "strategy": "hourly", "card_id": card.id, "features_version": 1, "bucket": KEY, "regime_base": "chop",
             "regime_4h": "chop", "closed_at": iso(ms_of(base.isoformat()) - (299 - h) * H), "pnl": -1.0 if h % 3 else 2.0,
             "pnl_pct": -0.1 if h % 3 else 0.2, "exit_reason": "stop-loss", "quote_filled": 1000.0, "stop_pct": 0.02,
             "normal_dip": 0.01} for h in range(0, 240, 6)]
    j.replace_seed(seed, {"card_id": card.id, "ts": iso(ms_of(base.isoformat())), "days": 365,
                          "coins": ["BTCUSDT", "ETHUSDT"], "until": base.isoformat(timespec="seconds"),
                          "trades": len(seed), "losers": sum(1 for r in seed if r["pnl"] <= 0), "error": None})
    j.event("memory", "Memory filled: 40 replay trades")
    return j, s


def test_postmortem_texts():
    s = Settings(offline=True, symbols=["BTCUSDT"])
    empty = Journal(":memory:", 10_000)
    assert "No closed trades yet: the post-mortem starts with the first sale." in postmortem_text(empty, s, "")
    j, s = populated()
    text = postmortem_text(j, s, "")
    assert text.startswith("🔍 Post-mortem of the last 5 closed trades (what the record shows, not a forecast)")
    assert text.count("\n• BTC ") + text.count("\n• ETH ") == 5 and "Causes over 40 closed trades" in text
    assert "execution: the stop sold 0.7% below its price (the replay assumes 0.2%)" in text
    assert "Replay of the strategy in force (365 days before" in text and "Memory rule: off. To try it" in text
    assert "desk backtest --days 730 --checks trend200,btc,cooldown,reward,memory" in text
    assert (postmortem_text(j, s, "20").count("\n• BTC ") + postmortem_text(j, s, "20").count("\n• ETH ")) == 20
    assert postmortem_text(j, s, "100") == postmortem_text(j, s, "20")
    assert postmortem_text(j, s, "abc").startswith("Usage")
    mem_text = postmortem_text(j, s, "memory")
    assert mem_text.startswith("🧠 Memory rule: off") and "In memory now: 80 closed trades of the last 365 days" in mem_text
    assert "40 live" in mem_text and "40 replay of the strategy in force" in mem_text and "Groups with 10 or more" in mem_text
    assert "Refusals at checks so far: 0" in mem_text and "not a forecast" in mem_text
    assert "Seed: 40 replay trades" in mem_text
    assert seed_refusal(Settings(offline=True)) == "The replay needs Binance's public prices, which offline mode does not read."
    assert seed_refusal(Settings(offline=False, strategy="trend")).startswith("The memory rule belongs to the hourly agents")
    assert seed_refusal(Settings(offline=False)) is None
    # trend-mode trades and trades without an entry record
    j2 = Journal(":memory:", 10_000)
    _closed_trade(j2, "ETHUSDT", {})
    j2.add_trade_context({"source": "live", "symbol": "ETHUSDT", "opened_at": j2.closed_positions()[0]["opened_at"],
                          "strategy": "trend", "card_id": "trend"})
    _closed_trade(j2, "SOLUSDT", {})
    t2 = postmortem_text(j2, Settings(offline=True, strategy="trend"), "")
    assert "chart causes not judged: the daily trend rule reads no hourly chart" in t2
    assert "(no entry record: bought before entry records were kept; only fills and US releases are judged)" in t2
    assert "Memory rule" not in t2
    page = postmortem_md(j, s)
    for heading in ("# Post-mortem", "## How a trade is tagged", "## Live trades: losers against winners",
                    "## Replay trades (labelled replay)", "## The memory rule", "## The last 50 closed trades"):
        assert heading in page
    assert "/postmortem [n]" in HELP_TEXT and "/postmortem memory" in HELP_TEXT
    assert "/postmortem [n]" in help_text(Settings(offline=True, strategy="trend"))
    on_j, on_s = populated(("trend200", "memory"))
    on_text = postmortem_text(on_j, on_s, "memory")
    assert on_text.startswith("🧠 Memory rule: on (MENTOR_CHECKS has memory)")
    from trader.postmortem_report import health_parts
    status, detail = health_parts(j, s, "2000")
    assert status == "idle" and detail.startswith("40 trades with an entry record, 40 replay trades; memory rule off; "
                                                  "seed ")      # no entry record written in the period
    j.add_trade_context({"source": "live", "symbol": "XUSDT", "opened_at": "x", "created_at": "2026-01-01"})
    assert health_parts(j, s, "2000")[0] == "working"
    assert health_parts(Journal(":memory:", 1), s, "2000") == ("idle", "0 trades with an entry record, 0 replay trades; "
                                                                       "memory rule off; no seed")
    assert health_parts(Journal(":memory:", 1), on_s, "2000")[1].endswith("it does not judge yet (fewer than 30 trades)")
    for t in (text, mem_text, t2, page, on_text, postmortem_text(on_j, on_s, ""), memory_daily_line(on_j, "2000"),
              replay_memory_text({"rule": "test", "refused": 3, "whatif": {"n": 2, "losers": 1, "mean_pct": -0.1},
                                  "made": {"n": 9, "losers": 6, "mean_pct": 0.1},
                                  "end": {"pool": 40, "groups": 2, "refusing": [KEY]}})):
        assert not FORBIDDEN.search(t), FORBIDDEN.search(t)


def test_the_daily_line_appears_only_with_memory_on():
    sent = []
    tg = SimpleNamespace(enabled=True, send=lambda text, **k: sent.append(text) or True)
    for checks in (("reward",), ("reward", "memory")):
        s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=checks, daily_report_hour_utc=0)
        bot = Orchestrator(s, Journal(":memory:", 10_000))
        bot.apply_champion()
        main_mod.maybe_daily_report(bot, tg, s)
    assert len(sent) == 2 and "🧠 Memory rule" not in sent[0]
    assert "🧠 Memory rule: refused nothing in the last 24 h; 0 trades in memory (it judges from 30)." in sent[1]


def lessons_181(journal, days=30, exclude=None):
    """research.lessons as 1.9.0 left it (the body before the post-mortem's keys), for test 17."""
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows_ = [dict(r) for r in journal.db.execute("SELECT * FROM closed_trades WHERE closed_at >= ?", (start,))]
    rows_ = [r for r in rows_ if not str(r["exit_reason"]).startswith("trend:")
             and not any(a <= r["opened_at"] and (b is None or r["opened_at"] < b) for a, b in exclude or [])]
    losers = [r for r in rows_ if r["pnl"] <= 0]
    quick = 0
    for r in losers:
        hours = (dt.datetime.fromisoformat(r["closed_at"]) - dt.datetime.fromisoformat(r["opened_at"])).total_seconds() / 3600
        if hours <= 2 and r["exit_reason"].startswith("stop-loss"):
            quick += 1
    reasons = {}
    for r in rows_:
        key = r["exit_reason"].split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    out = {"days": days, "trades": len(rows_), "losers": len(losers), "noise_stops": quick, "exits": reasons,
           "avg_loss_pct": round(sum(r["pnl_pct"] for r in losers) / len(losers), 2) if losers else None,
           "avg_win_pct": round(sum(r["pnl_pct"] for r in rows_ if r["pnl"] > 0) / max(1, len(rows_) - len(losers)), 2)}
    notes = []
    if rows_ and quick / max(1, len(losers)) > 0.4:
        notes.append("many stop-losses hit within two hours of entry: stops may be too tight, or entries too eager")
    if rows_ and len(losers) / len(rows_) > 0.7:
        notes.append("more than 70% losers: the entry filter is letting weak signals through")
    if not rows_:
        notes.append("no closed trades in the period")
    out["notes"] = notes
    return out


def test_the_lessons_gain_the_cause_mix_and_keep_their_notes():
    from trader.agents.head import HeadTraderAgent
    j, s = populated()
    new = ResearchAgent.lessons(j)
    old = lessons_181(j)
    assert {k: v for k, v in new.items() if k not in ("causes", "cause_notes")} == old and new["notes"] == old["notes"]
    assert new["causes"]["trades"] == 40 and set(new["causes"]["families"]) == set(memory.CAUSES)
    mix, notes = lessons_causes(j, 30, None)
    assert mix == new["causes"] and notes == new["cause_notes"]
    j.set("lessons", new)
    with_causes = HeadTraderAgent(s, j, None).playbook(private=True)
    j.set("lessons", old)
    assert HeadTraderAgent(s, j, None).playbook(private=True) == with_causes        # the playbook reads only notes


def test_the_digest_shows_the_post_mortem_only_with_memory_on(tmp_path):
    off_j, off_s = populated()
    on_j, on_s = populated(("trend200", "memory"))
    off = Notes(off_j, tmp_path / "off", off_s).digest()
    on = Notes(on_j, tmp_path / "on", on_s).digest()
    assert "# Post-mortem (losers against winners" not in off and "Memory filled" not in off
    assert "# Post-mortem (losers against winners" in on and "memory: Memory filled" in on


def test_a_failing_post_mortem_page_never_stops_the_notebook(tmp_path, monkeypatch):
    import trader.postmortem_report as pm
    j, s = populated()
    monkeypatch.setattr(pm, "_postmortem_page", lambda *a: 1 / 0)
    notes = Notes(j, tmp_path / "n", s)
    written = notes.refresh()
    names = {p.name for p in written}
    assert {"README.md", "STRATEGY.md", "TRADES.md", "DIARY.md", "PERFORMANCE.md", "CHART.md", "EVENTS.md",
            "LIBRARY.md", "POSTMORTEM.md"} <= names and any(p.parent.name == "strategies" for p in written)
    assert (tmp_path / "n" / "POSTMORTEM.md").read_text().startswith(
        "# Post-mortem\n\nThe post-mortem could not be built this check: division by zero")
    assert "POSTMORTEM.md" in (tmp_path / "n" / "README.md").read_text()


class FakeLLM:
    def __init__(self, checks):
        self.prompts, self.checks = [], checks

    def research(self, *a, **k):
        return {"text": "notes"}

    def ask(self, system, prompt, schema, effort="medium", caller="other"):
        self.prompts.append(prompt)
        return {"proposals": [{"name": "p", "rationale": "r", "source": "s", "buy_threshold": 0.3, "stop_loss_atr": 2.0,
                               "take_profit_atr": 4.0, "trail_atr": 2.0, "checks": list(self.checks),
                               "cooldown_hours": 24, "min_reward_risk": 2.0, "risk_per_trade_pct": 0.01,
                               "max_position_pct": 0.1, "max_open_positions": 3, "interval": "1h",
                               "coin_thresholds": []}], "ideas_needing_code": []}


def test_the_ai_inputs_are_unchanged_by_default():
    j, s = populated()
    new, old = ResearchAgent.lessons(j), lessons_181(j)
    champion = StrategyCard(name="c", checks=("reward", "trend200"))
    a, b = FakeLLM(["reward"]), FakeLLM(["reward"])
    ResearchAgent(a)._claude_proposals(champion, new, 1)
    ResearchAgent(b)._claude_proposals(champion, old, 1)
    assert a.prompts == b.prompts and "causes" not in a.prompts[0]
    on = FakeLLM(["reward"])
    cards = ResearchAgent(on)._claude_proposals(StrategyCard(name="m", checks=("reward", "memory")), new, 1)
    assert "\"causes\"" in on.prompts[0] and cards[0].checks == ("memory", "reward")   # the owner's rule is kept
    sneaky = FakeLLM(["reward", "memory"])
    cards = ResearchAgent(sneaky)._claude_proposals(champion, old, 1)
    assert "memory" not in cards[0].checks                                        # and never added by the AI


# ---- 19. nothing changes by default ----
TRADING = {"decisions": "symbol, action, confidence, source, combined_score, risk_approved, risk_reason, rules_action",
           "orders": "symbol, side, ok, qty, avg_price, quote, fee, reason", "positions": "symbol, qty, entry_price, "
           "cost, stop_loss, take_profit, protected, high_water",
           "closed_trades": "symbol, entry_price, exit_price, qty, cost, proceeds, pnl, pnl_pct, exit_reason",
           "equity": "equity, cash"}


def run_offline(checks_count=40, journal=None, patch=None):
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    if patch:
        patch()
    bot = Orchestrator(s, journal or Journal(":memory:", s.starting_balance))
    paces = []
    for _ in range(checks_count):
        bot.run_cycle()
        paces.append(bot.pace["minutes"])
    j = bot.journal
    return {t: [tuple(r) for r in j.db.execute(f"SELECT {cols} FROM {t} ORDER BY rowid")] for t, cols in TRADING.items()} \
        | {"pace": paces}, j


def test_the_new_hooks_change_no_trade(monkeypatch):
    with_hooks, j = run_offline()
    assert with_hooks["orders"] and j.trade_contexts("live")        # the offline desk trades and records contexts

    def off():
        monkeypatch.setattr(Orchestrator, "_track_breakouts", lambda self, sym, closed: None)
        monkeypatch.setattr(Orchestrator, "_record_context", lambda self, *a: None)
        monkeypatch.setattr(memory, "backfill", lambda *a: 0)
    without, j2 = run_offline(patch=off)
    assert j2.trade_contexts("live") == [] and with_hooks == without


def test_desks_with_and_without_memory_records_trade_identically():
    """Memory records in the journal (entry records, a replay seed, its state) change nothing while MENTOR_CHECKS lacks
    memory: two offline desks run the same checks and make exactly the same decisions, orders and trades."""
    plain, _ = run_offline()
    j = Journal(":memory:", Settings(offline=True).starting_balance)
    for k in range(60):
        j.add_trade_context({"source": "live", "symbol": ("BTCUSDT", "ETHUSDT", "SOLUSDT")[k % 3],
                             "opened_at": iso(1_700_000_000_000 + k * H), "strategy": "hourly", "card_id": "x",
                             "features_version": 1, "bucket": KEY, "breakout_level": 1.0, "breakout_hours": 6,
                             "decision_ms": 1_700_000_000_000 + k * H})
    j.replace_seed([{"source": "replay", "symbol": "BTCUSDT", "opened_at": iso(1_690_000_000_000 + k * H),
                     "strategy": "hourly", "card_id": StrategyCard.from_settings(Settings(offline=True)).id,
                     "features_version": 1, "bucket": KEY, "closed_at": iso(1_690_000_000_000 + (k + 1) * H),
                     "pnl": -1.0} for k in range(200)], {"card_id": "x", "trades": 200, "error": None})
    j.set("memory_backfilled", "2026-01-01T00:00:00+00:00")
    seeded, _ = run_offline(journal=j)
    assert plain == seeded and plain["orders"]


def test_the_replay_without_memory_is_exactly_as_before():
    """Golden values from the 1.9.0 code, before this change (the same synthetic history)."""
    history = {"AAAUSDT": synthetic("AAAUSDT", 1500, 1, drift=0.0002), "BBBUSDT": synthetic("BBBUSDT", 1500, 2, drift=0.0001)}
    s = Settings(offline=True, symbols=list(history), starting_balance=10_000,
                 mentor_checks=("trend200", "btc", "cooldown", "reward"))
    assert Backtest(s, history).run() == {
        'avg_hold_hours': 16.2, 'avg_loss': -7.53, 'avg_win': 42.04, 'basket_hold_pct': 20.8,
        'buy_and_hold_pct': {'AAA': 19.7, 'BBB': 21.9}, 'days': 54, 'decisions': {'BUY': 62, 'HOLD': 2538, 'SELL': 0},
        'exits': {'signal': 0, 'stop-loss': 16, 'take-profit': 45}, 'fees_paid': 136.72, 'fees_pct_of_start': 1.37,
        'final_balance': 12173.97, 'max_drawdown_pct': 0.4, 'mentor_vetoes': {}, 'profit_factor': 36.97,
        'return_pct': 21.74, 'start_balance': 10000, 'trades': 61, 'trades_per_week': 7.9, 'win_rate_pct': 86.9}
    s2 = dataclasses.replace(s, mentor_checks=("trend200", "cooldown", "reward", "regime"))
    assert Backtest(s2, history).run() == {
        'avg_hold_hours': 16.9, 'avg_loss': -13.63, 'avg_win': 35.14, 'basket_hold_pct': 20.8,
        'buy_and_hold_pct': {'AAA': 19.7, 'BBB': 21.9}, 'days': 54, 'decisions': {'BUY': 331, 'HOLD': 2269, 'SELL': 0},
        'exits': {'signal': 0, 'stop-loss': 18, 'take-profit': 21}, 'fees_paid': 82.02, 'fees_pct_of_start': 0.82,
        'final_balance': 10839.4, 'max_drawdown_pct': 0.44, 'mentor_vetoes': {'regime': 291}, 'profit_factor': 6.56,
        'return_pct': 8.39, 'start_balance': 10000, 'trades': 39, 'trades_per_week': 5.0, 'win_rate_pct': 71.8}
    r = Backtest(s, history).run()
    assert "trade_records" not in r and "memory" not in r


# ---- 20. cost ----
def test_the_memory_is_cheap():
    history = synthetic("BTCUSDT", 400, 4, drift=0.0003)
    c = history[-200:]
    chart = ChartEventsAgent("1h").run(c, None)
    rel = memory.release_times(None)
    best = min(_timed(lambda: entry_features("BTCUSDT", c, 100.0, 1.0, 98.0, chart, [], None, *rel, "1h"))
               for _ in range(5))
    assert best < 0.002
    rng = random.Random(2)
    pool = sorted(({"bucket": rng.choice([KEY, "a", "b"]), "closed_ms": rng.randint(0, 300) * DAY,
                    "loss": rng.random() < 0.7} for _ in range(1000)), key=lambda r: r["closed_ms"])
    book = MemoryBook(pool)
    book.verdict(KEY, 300 * DAY)
    assert min(_timed(lambda: book.verdict(KEY, 300 * DAY)) for _ in range(5)) < 0.001
    j = Journal(":memory:", 10_000)
    j.replace_seed([{"source": "replay", "symbol": f"C{k % 50}USDT", "opened_at": iso(k * H), "strategy": "hourly",
                     "card_id": "c", "features_version": 1, "bucket": KEY, "closed_at": iso(k * H + H), "pnl": -1.0}
                    for k in range(1000)], {})
    assert min(_timed(lambda: memory.pool_rows(j, "c", [])) for _ in range(3)) < 0.05


def _timed(fn):
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


# ---- 21. the history of releases ----
def test_the_release_history_file():
    rows_, problems, _ = load_calendar(memory.HISTORY_CALENDAR, None)
    data = json.loads(memory.HISTORY_CALENDAR.read_text())
    assert problems == [] and len(rows_) == len(data["events"]) == 55 and data["covers_from"] == "2024-11-01"
    assert {e["kind"] for e in data["events"]} == {"cpi", "nfp", "fomc"}
    assert sum(e["kind"] == "fomc" for e in data["events"]) == 15
    assert all(e["ts_utc"].endswith("Z") and e["ts_utc"] < "2026-08-07" for e in data["events"])
    from trader.agents import events as ev
    if ev._new_york() is None:
        pytest.skip("no time-zone database on this machine: the ET -> UTC check needs it")
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    for e in data["events"]:
        local = dt.datetime.strptime(e["local"].removesuffix(" ET"), "%Y-%m-%d %H:%M").replace(tzinfo=ny)
        assert local.astimezone(dt.timezone.utc) == dt.datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00")), e
        assert local.strftime("%H:%M") == ("14:00" if e["kind"] == "fomc" else "08:30")
    rels, cov = memory.release_times(None)
    assert cov[0] == ms_of("2024-11-01T00:00:00Z") and len(rels) >= 55 + 45 - 1


# ---- 22. the replay's command line ----
def test_backtest_command_line_memory_options(monkeypatch, capsys):
    import trader.backtest as bt
    hist = {s: synthetic(s, 2000, k, drift=0.0002) for k, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
    monkeypatch.setattr(bt, "fetch_history", lambda client, sym, days, interval="1h": hist[sym])
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", dmod.BUY_THRESHOLD)    # main() sets the thresholds: restored after
    monkeypatch.setattr(dmod, "SELL_THRESHOLD", dmod.SELL_THRESHOLD)
    seen = []
    real = bt.Backtest

    class Spy(real):
        def __init__(self, s, *a, **k):
            seen.append((s.mentor_checks, k.get("memory_rule")))
            super().__init__(s, *a, **k)
    monkeypatch.setattr(bt, "Backtest", Spy)
    assert bt.main(["--days", "60", "--checks", "reward,memory", "--memory-rule", "owner60"]) == 0
    out = capsys.readouterr().out
    assert seen[-1] == (("reward", "memory"), "owner60")
    assert "Memory rule as first planned (more than 60% losers among 10 or more similar trades):" in out
    assert "a measurement on past prices, not a forecast." in out
    assert bt.main(["--days", "60", "--checks", "memory"]) == 0
    assert "Memory rule (similar past trades lost clearly more often than usual):" in capsys.readouterr().out
    assert bt.main(["--days", "60", "--checks", "all"]) == 0
    assert seen[-1][0] == ("trend200", "alignment", "overextended", "btc", "reward", "cooldown", "regime")
    assert "Memory rule" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        bt.main(["--memory-rule", "sixty"])


def test_the_postmortem_command_line(monkeypatch, capsys, tmp_path):
    import trader.backtest as bt
    import trader.postmortem_report as pm
    hist = {s: synthetic(s, 2000, k, drift=0.0002) for k, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))}
    monkeypatch.setattr(bt, "fetch_history", lambda client, sym, days, interval="1h": hist[sym])
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", 0.05)
    assert pm.main(["--replay", "--days", "60", "--checks", "reward"]) == 0
    out = capsys.readouterr().out
    assert "Post-mortem of the replay's" in out and "failed breakout:" in out and "not judged for these trades" in out
    monkeypatch.setenv("JOURNAL_DB", str(tmp_path / "j.db"))
    assert pm.main(["--trades", "3"]) == 0
    assert "No closed trades yet" in capsys.readouterr().out


# ---- 23. no buy is lost to bookkeeping ----
def test_no_buy_is_lost_to_bookkeeping(monkeypatch, caplog):
    from tests.test_trend import candles as daily_candles, trend_desk, wavy
    monkeypatch.setattr(memory, "trend_features", lambda *a, **k: 1 / 0)
    bot, j = trend_desk({"AAAUSDT": daily_candles(wavy(130, 100, 0.02))}, positions=1)
    bot.run_cycle()
    assert "AAAUSDT" in j.positions() and j.positions()["AAAUSDT"]["protected"] == "emulated"
    assert j.trade_contexts("live") == [] and "Building the trend entry record" in caplog.text
    monkeypatch.undo()
    # entry_features failing inside the checklist: "? memory" and the other rules still decide
    ok = desk({"BTCUSDT": cycle_series()}, checks=("reward", "memory"))
    force_buy(ok)
    with monkeypatch.context() as m:
        m.setattr(memory, "entry_features", lambda *a, **k: 1 / 0)
        ok.run_cycle()
    assert "BTCUSDT" in ok.journal.positions()
    # the context write failing after an immediate sale in _buy changes no order
    orders = []
    for broken in (False, True):
        b = desk({"BTCUSDT": cycle_series()})
        force_buy(b)
        b.broker.fail_protect = True
        b.apply_champion()                 # the first cycle rebuilds the risk manager from the champion card
        b.risk.exit_signal = lambda position, price: "stop-loss"     # already past an exit level: sold at once
        if broken:
            monkeypatch.setattr(b.journal, "add_trade_context", lambda row: 1 / 0)
        b.run_cycle()
        orders.append([tuple(r) for r in b.journal.db.execute("SELECT symbol, side, ok, qty, avg_price, reason FROM orders")])
    assert orders[0] == orders[1] and [o[1] for o in orders[0]] == ["BUY", "SELL"]


# ---- 26. the replay judges memory only after the other rules ----
def test_the_replay_judges_memory_only_for_buys_the_other_rules_let_through(monkeypatch):
    hist = {s: synthetic(s, 2000, k, drift=0.0002) for k, s in enumerate(("AAAUSDT", "BBBUSDT"))}
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", 0.05)
    rel = memory.release_times(None)

    def refuse(bucket):
        return "h:12-18" in bucket

    def forced(self, bucket, decision_ms):
        return {"ok": False if refuse(bucket) else None, "detail": "forced"}
    calls, passes = [], []
    real_features, real_review = memory.entry_features, MentorAgent.review

    def counting_features(*a, **k):
        calls.append(1)
        return real_features(*a, **k)

    def counting_review(self, *a, **k):
        r = real_review(self, *a, **k)
        passes.append(r["pass"])
        return r
    s = Settings(offline=True, symbols=list(hist), mentor_checks=("reward", "cooldown", "memory"))
    with monkeypatch.context() as m:
        m.setattr(memory, "entry_features", counting_features)
        m.setattr(MentorAgent, "review", counting_review)
        m.setattr(MemoryBook, "verdict", forced)
        lazy = Backtest(s, hist, releases=rel).run()
    assert calls and len(calls) == sum(passes) and len(passes) > len(calls)
    assert lazy["mentor_vetoes"].get("memory")
    # the same trades when memory is judged inside the checklist for every candidate
    a = ChartEventsAgent("1h")

    def eager_review(self, symbol, candles, daily, price, stop, take, atr, btc_daily, last_stop_ms, now_ms, chart=None,
                     memory=None):
        f = real_features(symbol, candles, price, atr, stop, a.run(candles, daily[:-1]), daily, btc_daily, *rel, "1h")
        verdict = {"ok": False if refuse(f["bucket"]) else None, "detail": "forced"}
        return real_review(self, symbol, candles, daily, price, stop, take, atr, btc_daily, last_stop_ms, now_ms,
                           chart=chart, memory=verdict)
    with monkeypatch.context() as m:
        m.setattr(MentorAgent, "review", eager_review)
        s2 = dataclasses.replace(s, mentor_checks=("reward", "cooldown", "memory"))
        bt = Backtest(s2, hist, releases=rel)
        bt.use_memory = False            # memory judged by the checklist only
        eager = bt.run()
    keys = ("trades", "return_pct", "exits", "final_balance", "win_rate_pct")
    assert {k: lazy[k] for k in keys} == {k: eager[k] for k in keys}


# ---- 29. review fixes ----
def _replay_entry(sym, hist, ds, i, btc_hist, btc_ds, rel, s):
    """The replay's entry features of `sym` at candle i (the 200-candle window, Bitcoin's daily list as the replay
    builds it)."""
    from trader.agents.technical import TechnicalAnalysisAgent
    c = hist[i - 199: i + 1]
    price = c[-1]["close"]
    atr = TechnicalAnalysisAgent().run(c)["atr"]
    chart = ChartEventsAgent("1h").run(c, ds.completed[: ds.before[i]])
    return entry_features(sym, c, price, atr, price - s.stop_loss_atr * atr, chart, ds.at(i, price),
                          btc_ds.at(i, btc_hist[i]["close"]), *rel, "1h")


def test_an_alt_knows_bitcoins_side_while_bitcoin_is_held_as_in_the_replay(monkeypatch):
    """While Bitcoin is held its daily rows come only from a cache that empties an hour after the read; the memory's
    group and the entry record of an alt must still know Bitcoin's side, as the replay always does. With the `btc`
    rule on, the checklist reads Bitcoin's history itself again (as 1.7 did) and the records reuse that list; without
    it the records read their own copy, which the checklist and the chart reader never see."""
    btc_hist = synthetic("BTCUSDT", 5300, 1, drift=-0.0002)        # more than 200 finished days
    eth_hist = synthetic("ETHUSDT", 5300, 2, drift=0.0003)
    btc_ds, eth_ds = DailySeries(btc_hist), DailySeries(eth_hist)
    rel = memory.release_times(None)
    i = 5100
    for checks in (("memory",), ("trend200", "cooldown", "reward"), ("btc", "memory")):
        s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=checks)
        f = _replay_entry("ETHUSDT", eth_hist, eth_ds, i, btc_hist, btc_ds, rel, s)
        assert f["btc"] == "below"                                  # the replay knows Bitcoin's side
        bot = desk({"BTCUSDT": btc_hist[i - 198: i + 1], "ETHUSDT": eth_hist[i - 198: i + 1]}, checks=checks)
        bot.market.daily = {"BTCUSDT": btc_ds.completed[: btc_ds.before[i]],
                            "ETHUSDT": eth_ds.completed[: eth_ds.before[i]]}
        px = btc_hist[i]["close"]
        bot.journal.open_position("BTCUSDT", {"qty": 0.01, "avg_price": px, "quote": px * 0.01}, px * 0.5, px * 2)
        bot.decision.run = lambda sym, reports, holding, use_ai=True, threshold=None: (
            {"action": "BUY", "confidence": 0.9, "reasoning": "r", "source": "rules", "combined_score": 0.5}
            if sym == "ETHUSDT" else {"action": "HOLD", "confidence": 0.0, "reasoning": "r", "source": "rules",
                                      "combined_score": 0.0})
        bot.apply_champion()                                        # the checklist the check will use
        captured, given = {}, {}
        real_features, real_review = memory.entry_features, bot.mentor.review

        def spy_features(sym, *a, **k):
            captured[sym] = real_features(sym, *a, **k)
            return captured[sym]

        def spy_review(sym, candles, daily, price, stop, take, atr, btc_daily, *a, **k):
            given[sym] = btc_daily
            return real_review(sym, candles, daily, price, stop, take, atr, btc_daily, *a, **k)
        bot.mentor.review = spy_review
        with monkeypatch.context() as m:
            m.setattr(memory, "entry_features", spy_features)
            bot.run_cycle()
        if "memory" in checks:
            assert captured["ETHUSDT"] == f                         # live and replay put the buy in the same group
        assert bot.market.downloads.count("BTCUSDT") == 1           # one download, cached for an hour
        if "btc" in checks:   # Bitcoin below its 200-day average: the restored rule refuses the alt, as the replay
            assert given["ETHUSDT"] == bot.market.daily_closes("BTCUSDT") and "ETHUSDT" not in bot.journal.positions()
            assert bot.market.aside_downloads == []                 # the memory reused the checklist's list
            continue
        assert "ETHUSDT" in bot.journal.positions(), checks
        row = [r for r in bot.journal.trade_contexts("live") if r["symbol"] == "ETHUSDT"][0]
        assert row["bucket"] == f["bucket"] and row["btc_above"] == 0, checks
        assert given["ETHUSDT"] == []                               # without the `btc` rule: as in 1.9.0
        assert bot.market.aside_downloads == ["BTCUSDT"] and "BTCUSDT" not in bot.market._fresh   # its own copy


def test_a_replay_sale_on_a_signal_reaches_the_memory_at_the_next_decision_as_live(monkeypatch):
    """The live desk sells on a signal after that check's buys were judged (the book is built before), so a buy at the
    same check does not count the sale; the replay must not either, and both count it from the next decision."""
    seen = []
    real_verdict = MemoryBook.verdict

    def spy(self, bucket, decision_ms):
        v = real_verdict(self, bucket, decision_ms)
        seen.append((decision_ms, v["pool"]))
        return v
    monkeypatch.setattr(MemoryBook, "verdict", spy)
    # the replay: A bought at candle 600 and sold on a signal at 602; B bought at 602, A again at 603
    hist = {sym: synthetic(sym, 1200, k, drift=0.0002) for k, sym in enumerate(("AAAUSDT", "BBBUSDT"))}
    times = [c["time"] for c in hist["AAAUSDT"]]
    t_buy, t_sell, t_next = times[600], times[602], times[603]
    s = Settings(offline=True, symbols=list(hist), mentor_checks=("memory",))
    bt = Backtest(s, hist, releases=memory.release_times(None))
    clock = {"t": None}

    def decide(sym, reports, holding=False):
        t = clock["t"]
        buy = {"action": "BUY", "confidence": 0.9, "combined_score": 0.5}
        if sym == "AAAUSDT" and t in (t_buy, t_next) and not holding:
            return buy
        if sym == "AAAUSDT" and t == t_sell and holding:
            return {"action": "SELL", "confidence": 0.9, "combined_score": -0.5}
        if sym == "BBBUSDT" and t == t_sell and not holding:
            return buy
        return {"action": "HOLD", "confidence": 0.0, "combined_score": 0.0}
    bt.decision.run = decide
    real_tech = bt.technical.run

    def tech(c):
        clock["t"] = c[-1]["time"]
        return real_tech(c)
    bt.technical.run = tech
    r = Backtest.run(bt)
    assert r["exits"]["signal"] == 1
    pools = dict(seen)
    assert pools[t_sell + H] == 0 and pools[t_next + H] == 1      # B does not see A's sale; the next decision does
    # the trade record itself is unchanged: closed at the end of the sale's candle
    rec = Backtest(s, hist, releases=memory.release_times(None), record=True)
    rec.decision.run, rec.technical.run = decide, tech
    sale = [x for x in rec.run()["trade_records"] if x["reason"] == "signal"]
    assert len(sale) == 1 and sale[0]["closed_ms"] == t_sell + H and sale[0]["t"] == t_sell
    # live: A sold on a signal and B bought at the same check - B's memory does not count the sale; one candle later
    # (the next check) it does
    seen.clear()
    now_h = int(time.time() * 1000) // H
    closes = [c["close"] for c in cycle_series()]
    start = now_h - len(closes)                    # the newest finished candle closes at the top of this hour
    series = {"AAAUSDT": bars(closes, start_hour=start), "BBBUSDT": bars([x * 0.5 for x in closes], start_hour=start)}
    bot = desk(series, checks=("memory",))
    plan = {"AAAUSDT": "BUY", "BBBUSDT": "HOLD"}

    def live_decide(sym, reports, holding, use_ai=True, threshold=None):
        act = plan[sym]
        return {"action": act, "confidence": 0.9, "reasoning": "r", "source": "rules",
                "combined_score": {"BUY": 0.5, "SELL": -0.5}.get(act, 0.0)}
    bot.decision.run = live_decide
    bot.run_cycle()                                # check 1: A bought
    assert "AAAUSDT" in bot.journal.positions() and bot.journal.trade_contexts("live")
    seen.clear()
    plan.update(AAAUSDT="SELL", BBBUSDT="BUY")
    bot.run_cycle()                                # check 2: A sold on the signal, B bought
    assert "AAAUSDT" not in bot.journal.positions() and "BBBUSDT" in bot.journal.positions()
    assert [p for _, p in seen] == [0]
    seen.clear()
    bot.market.series = {sym: bars([c["close"] for c in v[1:]] + [v[-1]["close"]], start_hour=start + 1)
                         for sym, v in series.items()}
    plan.update(AAAUSDT="BUY", BBBUSDT="HOLD")
    bot.run_cycle()                                # check 3, one candle later: the sale is counted
    assert [p for _, p in seen] == [1]


def test_the_memory_rule_follows_mentor_checks_behind_a_lab_or_approved_champion():
    """OWNER_ONLY rules are the owner's: a champion the lab or /approve put in force gets the owner's choice of the
    memory rule, both ways, with its other checks kept and without a repeated replacement. Only the card changes: who
    put it in force, since when and the card a rollback returns to are kept, and the lineage calls it a settings
    change, not a promotion."""
    from trader.postmortem_report import memory_on
    base = ("trend200", "btc", "cooldown", "reward")
    s_on = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=base + ("memory",))
    s_off = dataclasses.replace(s_on, mentor_checks=base)
    for by, first, then in (("auto-promote", s_on, s_off), ("owner", s_off, s_on)):
        j = Journal(":memory:", 10_000)
        owner_card = Orchestrator(first, j).apply_champion()
        lab_card = dataclasses.replace(owner_card, name="lab challenger", cooldown_hours=12.0)
        j.set("strategy_champion", {"card": lab_card.to_dict(), "since": "2026-10-01T00:00:00+00:00", "by": by,
                                    "previous": owner_card.to_dict()})
        assert ("memory" in Orchestrator(first, j).apply_champion().checks) == ("memory" in first.mentor_checks)
        bot = Orchestrator(then, j)                # the owner changed MENTOR_CHECKS; the desk restarts
        card = bot.apply_champion()
        want = "memory" in then.mentor_checks
        assert ("memory" in card.checks) == want and ("memory" in bot.mentor.checks) == want
        assert card.name == "lab challenger" and card.cooldown_hours == 12.0 and set(card.checks) - {"memory"} == set(base)
        state = j.get("strategy_champion")
        assert state["by"] == by and ("memory" in state["card"]["checks"]) == want and memory_on(j, then) == want
        assert state["previous"]["id"] == owner_card.id and state["since"] == "2026-10-01T00:00:00+00:00"
        assert postmortem_text(j, then, "memory").startswith("🧠 Memory rule: " + ("on" if want else "off"))
        hist = len(j.get("strategy_history"))
        assert bot.apply_champion().id == card.id and Orchestrator(then, j).apply_champion().id == card.id
        assert len(j.get("strategy_history")) == hist                         # once, not at every check
        last = j.get("strategy_history")[-1]
        assert (last["event"], last["id"], last["by"]) == ("settings", card.id, "owner settings")
        assert last["note"] == "the owner's settings changed: the memory rule is now " + ("on" if want else "off")


def test_a_position_open_at_the_upgrade_gets_its_entry_record():
    j = Journal(":memory:", 10_000)
    reports = {"technical": {"score": 0.3, "price": 100.0, "atr": 2.0, "rsi": 61.0}, "prediction": {"score": 0.2},
               "news": {"score": 0.1, "source": "keywords"}, "forex": {"score": 0.0},
               "chart": {"4h": "chop", "1h": "trending up", "1d": "trending up", "summary": "x"},
               "mentor": {"pass": True, "checklist": "✓ btc: Bitcoin 90,000 vs 80,000 [D]"}}
    j.log_decision("ETHUSDT", {"action": "BUY", "confidence": 0.9, "source": "rules", "combined_score": 0.4,
                               "reasoning": "r"}, {"approved": True, "reason": "ok"}, reports)
    fill = {"ok": True, "qty": 1.0, "avg_price": 100.0, "quote": 100.0, "market_price": 99.9}
    j.log_order("ETHUSDT", "BUY", fill, "signal")
    j.open_position("ETHUSDT", fill, 90.0, 120.0)            # bought under 1.9.0, still open at the upgrade
    s = Settings(offline=True, symbols=["ETHUSDT"])
    Orchestrator(s, j)                                        # the first start of 1.9.1
    (row,) = j.trade_contexts("live")
    assert row["backfilled"] == 1 and row["regime_4h"] == "chop" and row["opened_at"] == j.positions()["ETHUSDT"]["opened_at"]
    assert memory.backfill(j, *memory.release_times(None)) == 0          # idempotent
    sell = {"ok": True, "qty": 1.0, "avg_price": 95.0, "quote": 95.0, "market_price": 95.0}
    j.log_order("ETHUSDT", "SELL", sell, "stop-loss (exchange)")
    j.close_position("ETHUSDT", sell, "stop-loss (exchange)")   # sold under 1.9.1
    assert len(j.trade_contexts("live")) == 1
    text = postmortem_text(j, s, "")
    assert "chop at entry" in text and "(entry record rebuilt from the stored decision" in text
    assert "no entry record" not in text


def test_the_backfill_floors_the_decision_to_the_cards_candle():
    """A 2h or 4h card decides on its own finished candles: the backfilled decision time is the close of the newest
    one, as live (decision_time), not the hour."""
    briefs = {"1h": {"1h": "trending up", "4h": "chop", "1d": "chop"},
              "2h": {"2h": "trending up", "4h": "chop", "1d": "chop"},
              "4h": {"4h": "chop", "1d": "chop"}, "none": {"summary": "no chart report"}}
    decided = ms_of("2026-09-10T07:20:00+00:00")
    want = {"1h": "07:00", "2h": "06:00", "4h": "04:00", "none": "07:00"}
    for card, chart in briefs.items():
        j = Journal(":memory:", 10_000)
        _closed_trade(j, "ETHUSDT", {"technical": {"price": 100.0, "atr": 2.0}, "chart": chart})
        j.db.execute("UPDATE decisions SET ts=?", (iso(decided),))
        j.db.execute("UPDATE orders SET ts=?", (iso(decided + MIN),))
        j.db.execute("UPDATE closed_trades SET opened_at=?, closed_at=?", (iso(decided + MIN), iso(decided + 5 * H)))
        j.db.commit()
        assert memory.backfill(j, [], None) == 1
        (row,) = j.trade_contexts("live")
        assert iso(row["decision_ms"])[11:16] == want[card], card
        if card != "none":
            step = memory.interval_ms(card)
            newest = decided - decided % step - step                    # the newest finished candle's open time
            assert row["decision_ms"] == decision_time([{"time": newest}], step)
            assert row["bucket"].split("|")[3] == "h:" + hour_block(decision_time([{"time": newest}], step))
    assert hour_block(ms_of("2026-09-10T04:00:00+00:00")) == "00-06"


def test_the_trend_desks_texts_give_its_own_loss_rate_and_no_seed_offer():
    j = Journal(":memory:", 10_000)
    _closed_trade(j, "ETHUSDT", {})
    trend = Settings(offline=True, strategy="trend")
    hourly = Settings(offline=True)
    t_text, t_page = postmortem_text(j, trend, ""), postmortem_md(j, trend)
    h_text, h_page = postmortem_text(j, hourly, ""), postmortem_md(j, hourly)
    for t in (t_text, t_page):
        assert "loses most of its trades by design: it wins 20-30%" in t and "two trades in three" not in t
    assert "| no tag | none of the rules applies | an ordinary loss: the daily trend rule loses most" in t_page
    assert "No seed. The memory rule belongs to the hourly agents; the daily trend rule has no checklist." in t_page
    assert "/postmortem seed" not in t_page
    for t in (h_text, h_page):
        assert "two trades in three" in t and "20-30%" not in t
    assert "No seed: the replay of the strategy in force is made only when the memory rule is on" in h_page


def test_the_seed_line_promises_an_automatic_refresh_only_with_the_rule_on():
    from trader.postmortem_report import seed_status_text
    for checks, on in ((("trend200", "btc", "cooldown", "reward"), False), (("trend200", "memory"), True)):
        j, s = populated(checks)
        seed = j.get("memory_seed")
        j.set("memory_seed", dict(seed, card_id="0ld0ld00"))          # a seed of an earlier strategy
        for t in (postmortem_text(j, s, "memory"), seed_status_text(j, s), postmortem_md(j, s)):
            assert "an earlier strategy than the one in force" in t
            assert ("refreshed when the strategy changes" in t) == on
            assert ("with the memory rule off it is not refreshed automatically: only with /postmortem seed." in t) \
                == (not on)


# ---- the recheck of the review fixes: Bitcoin's history, the lab's rollback, the trend desk's seed texts ----
CHECKS4 = ("trend200", "btc", "cooldown", "reward")
BUY = {"action": "BUY", "confidence": 0.9, "reasoning": "r", "source": "rules", "combined_score": 0.5}
HOLD = {"action": "HOLD", "confidence": 0.0, "reasoning": "r", "source": "rules", "combined_score": 0.0}


class DailyClient:
    """Binance's public data behind the real MarketDataAgent: fixed hourly candles, daily rows as Binance serves them
    (the finished days, then the forming day at the newest candle's close) and a log of every daily download;
    `fail`: coins whose daily download raises (a timeout)."""
    def __init__(self, hourly, daily, fail=()):
        self.hourly, self.daily, self.fail = hourly, daily, set(fail)
        self.daily_calls: list[str] = []
        self.log: list[str] | None = None      # set by a test to share one log with the broker

    def klines(self, symbol, interval="1h", limit=200, start_ms=None):
        if interval == "1d":
            self.daily_calls.append(symbol)
            if self.log is not None:
                self.log.append(f"daily {symbol}")
            if symbol in self.fail:
                raise RuntimeError("read timed out")
            closes, last = self.daily.get(symbol) or [], self.hourly[symbol][-1]
            day = last["time"] - last["time"] % DAY
            rows = [{"time": day - (len(closes) - k) * DAY, "open": c, "high": c, "low": c, "close": c, "volume": 1.0}
                    for k, c in enumerate(closes)]
            return rows + [{"time": day, "open": last["close"], "high": last["close"], "low": last["close"],
                            "close": last["close"], "volume": 1.0}]
        return self.hourly[symbol][-limit:]

    def price(self, symbol):
        return self.hourly[symbol][-1]["close"]


def online_desk(monkeypatch, daily, checks, coins=("BTCUSDT", "ETHUSDT"), held=("BTCUSDT",), fail=()):
    """A desk on the real MarketDataAgent (its one-hour daily cache, on a clock the test moves) with `held` bought."""
    from trader.agents import market as market_module
    from trader.agents.market import MarketDataAgent
    hourly = {"BTCUSDT": bars([100.0 + 0.01 * i for i in range(200)]),
              "ETHUSDT": bars([50.0 + 0.01 * i for i in range(200)]),
              "SOLUSDT": bars([20.0 + 0.01 * i for i in range(200)])}
    bot = desk({c: hourly[c] for c in coins}, checks=checks)
    client = DailyClient(hourly, daily, fail)
    bot.market = MarketDataAgent(client, "1h", offline=False)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(market_module, "time", SimpleNamespace(time=lambda: clock[0]))
    for sym in held:
        px = hourly[sym][-1]["close"]
        bot.journal.open_position(sym, {"qty": 0.01, "avg_price": px, "quote": px * 0.01}, px * 0.5, px * 4)
    return bot, client, clock


def deciding(plan, seen=None):
    def decide(sym, reports, holding, use_ai=True, threshold=None):
        if seen is not None:
            seen.append((sym, json.dumps(reports, sort_keys=True, default=str)))
        return dict(BUY if sym in plan and not holding else HOLD)
    return decide


def checklist_of(j, sym):
    row = j.db.execute("SELECT reports FROM decisions WHERE symbol=? ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
    return json.loads(row["reports"])["mentor"]["checklist"]


def test_the_btc_rule_reads_bitcoin_again_while_the_desk_holds_it(monkeypatch):
    """Fix: from 1.8.0 the `btc` rule let alt buys through unchecked while the desk held Bitcoin (a held coin's daily
    rows come only from a copy that expires an hour after the last read, so the rule read '?'). The checklist reads
    Bitcoin's history itself again, as 1.7 did: an alt is refused while Bitcoin is below its 200-day average and
    bought above it, with one download an hour at most."""
    for btc, line in (([200.0] * 259, "✗ btc: Bitcoin 101.99 vs its 200-day average 199.51"),
                      ([50.0] * 259, "✓ btc: Bitcoin 101.99 vs its 200-day average 50.26")):
        bot, client, clock = online_desk(monkeypatch, {"BTCUSDT": btc, "ETHUSDT": [10.0] * 259}, CHECKS4)
        bot.decision.run = deciding({"ETHUSDT"})
        out = bot.run_cycle()
        eth = next(r for r in out if r.get("symbol") == "ETHUSDT")
        assert line in eth["mentor"] and "✓ trend200" in eth["mentor"]
        refused = line.startswith("✗")
        assert ("ETHUSDT" in bot.journal.positions()) == (not refused)
        assert client.daily_calls.count("BTCUSDT") == 1
        if not refused:
            continue
        assert eth["risk"] == "master traders' checklist: btc not met"
        for minutes in (15, 30, 45):                  # within the hour: the rule still applies, no new download
            clock[0] += 15 * 60
            bot.run_cycle()
            assert line in checklist_of(bot.journal, "ETHUSDT") and "ETHUSDT" not in bot.journal.positions()
            assert bot.charts["BTCUSDT"]["timeframes"].get("1d") is not None   # the side effect the CHANGELOG names
        assert client.daily_calls.count("BTCUSDT") == 1
        clock[0] += 16 * 60                           # 61 minutes after the read: the copy has expired
        bot.run_cycle()
        assert line in checklist_of(bot.journal, "ETHUSDT") and client.daily_calls.count("BTCUSDT") == 2


def test_a_failed_bitcoin_read_is_not_tried_again_at_the_same_check(monkeypatch):
    """After a failed read of Bitcoin's daily prices (the chart step's, or the checklist's while Bitcoin is held) no
    coin's checklist or record tries again at that check: the `btc` rule reads '?', the coin's own rules are still
    judged, the buy is not lost, and the next check tries once more."""
    daily = {"ETHUSDT": [10.0] * 259, "SOLUSDT": [10.0] * 259}
    for held in (("BTCUSDT",), ()):
        bot, client, clock = online_desk(monkeypatch, daily, CHECKS4, coins=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                                         held=held, fail={"BTCUSDT"})
        bot.decision.run = deciding({"ETHUSDT"})
        bot.run_cycle()
        assert client.daily_calls.count("BTCUSDT") == 1, held
        for sym in ("ETHUSDT", "SOLUSDT"):
            text = checklist_of(bot.journal, sym)
            assert "? btc: Bitcoin history unavailable" in text and "✓ trend200" in text, (held, sym)
        assert "ETHUSDT" in bot.journal.positions()   # unknown never blocks
        (row,) = [r for r in bot.journal.trade_contexts("live") if r["symbol"] == "ETHUSDT"]
        assert row["btc_above"] is None
        clock[0] += 15 * 60
        bot.run_cycle()
        assert client.daily_calls.count("BTCUSDT") == 2, held


def test_the_records_bitcoin_read_changes_no_trade_and_no_ai_input(monkeypatch):
    """With the memory rule off (and no `btc` rule to read Bitcoin), the entry record of an alt bought while the desk
    holds Bitcoin reads Bitcoin's history into a copy of its own: the next check, 15 minutes later, trades and shows
    every AI exactly what a desk without entry records does (the held Bitcoin's chart gets no daily frame from it)."""
    daily = {"BTCUSDT": [200.0] * 259, "ETHUSDT": [10.0] * 259, "SOLUSDT": [10.0] * 259}
    for checks in ((), ("trend200", "cooldown", "reward")):
        runs = []
        for records in (True, False):
            with monkeypatch.context() as m:
                bot, client, clock = online_desk(m, daily, checks, coins=("BTCUSDT", "ETHUSDT", "SOLUSDT"))
                if not records:
                    m.setattr(Orchestrator, "_entry_context", lambda self, *a: None)
                    m.setattr(Orchestrator, "_record_context", lambda self, *a: None)
                seen, plan, news = [], {"ETHUSDT"}, []
                real_news = bot.news.run
                bot.news.run = lambda coins, snapshot: news.append(json.dumps(snapshot, sort_keys=True)) or \
                    real_news(coins, snapshot)
                bot.decision.run = deciding(plan, seen)
                bot.run_cycle()                       # check 1: ETH bought
                plan.clear()
                plan.add("SOLUSDT")
                clock[0] += 15 * 60
                bot.run_cycle()                       # check 2: SOL bought
                runs.append({"seen": seen, "news": news, "held": sorted(bot.journal.positions()),
                             "btc chart": bot.charts["BTCUSDT"]["summary"],
                             "btc 1d": client.daily_calls.count("BTCUSDT"), "shared": "BTCUSDT" in bot.market._daily,
                             "rows": bot.journal.trade_contexts("live")})
        with_records, without = runs
        for key in ("seen", "news", "held", "btc chart"):
            assert with_records[key] == without[key], (checks, key)
        assert with_records["held"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert (with_records["btc 1d"], without["btc 1d"]) == (1, 0) and not with_records["shared"]
        assert [r["btc_above"] for r in with_records["rows"]] == [0, 0]     # the records still know Bitcoin's side


def test_no_download_sits_between_the_risk_approval_and_the_order(monkeypatch):
    """The entry record (and its Bitcoin read) is built after the buy's exchange orders; a failing read there loses
    neither the buy nor its protection."""
    daily = {"BTCUSDT": [200.0] * 259, "ETHUSDT": [10.0] * 259}
    for fail in ((), ("BTCUSDT",)):
        bot, client, _ = online_desk(monkeypatch, daily, ("trend200", "cooldown", "reward"), fail=fail)
        client.log = bot.broker.calls
        bot.decision.run = deciding({"ETHUSDT"})
        bot.run_cycle()
        log = bot.broker.calls                        # the held Bitcoin's own protection comes first
        b = log.index("buy")
        assert "daily BTCUSDT" not in log[:b] and log.index("protect", b) < log.index("daily BTCUSDT"), log
        assert bot.journal.positions()["ETHUSDT"]["protected"]
        (row,) = [r for r in bot.journal.trade_contexts("live") if r["symbol"] == "ETHUSDT"]
        assert row["btc_above"] == (None if fail else 0)


def test_the_records_own_daily_copy(monkeypatch):
    """MarketDataAgent.daily_rows_aside: a cache of its own (the checklist's and the chart reader's stays untouched),
    one download an hour at most, `download=False` reads only that copy, offline none."""
    from trader.agents import market as market_module
    from trader.agents.market import MarketDataAgent
    clock = [1_800_000_000.0]
    monkeypatch.setattr(market_module, "time", SimpleNamespace(time=lambda: clock[0]))
    client = DailyClient({"BTCUSDT": bars([100.0] * 10)}, {"BTCUSDT": [90.0] * 5})
    m = MarketDataAgent(client, "1h", offline=False)
    assert m.daily_rows_aside("BTCUSDT", download=False) == [] and client.daily_calls == []
    rows = m.daily_rows_aside("BTCUSDT")
    assert len(rows) == 6 and client.daily_calls == ["BTCUSDT"]
    assert m._daily == {} and m.daily_cached("BTCUSDT") == []          # the shared copy is not filled
    clock[0] += 3599
    assert m.daily_rows_aside("BTCUSDT") == rows and m.daily_rows_aside("BTCUSDT", download=False) == rows
    assert client.daily_calls == ["BTCUSDT"]
    clock[0] += 2
    assert m.daily_rows_aside("BTCUSDT", download=False) == [] and client.daily_calls == ["BTCUSDT"]
    assert m.daily_rows_aside("BTCUSDT") == rows and client.daily_calls == ["BTCUSDT"] * 2
    assert m.daily_rows("BTCUSDT") == rows and len(client.daily_calls) == 3    # and the shared one keeps its own
    assert MarketDataAgent(client, "1h", offline=True).daily_rows_aside("BTCUSDT") == []
    assert len(client.daily_calls) == 3


def test_the_labs_rollback_survives_the_owners_memory_choice():
    """The lab promotes a card (its way back: the owner's card); the owner then adds (or removes) `memory` and
    restarts; the card's live trades turn bad: the rollback returns to the owner's card, with the owner's current
    choice of the memory rule, and the rollback watch then stops as after any rollback. The switch of the memory rule
    is a 'settings' event in the lineage, not a promotion."""
    from trader.health import strategy_changes, strategy_changes_text
    from trader.lab import ROLLBACK_MIN_TRADES, Lab
    from trader.notes import card_periods, champion_at, lineage_text
    s_off = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=CHECKS4)
    s_on = dataclasses.replace(s_off, mentor_checks=CHECKS4 + ("memory",))
    for first, then in ((s_off, s_on), (s_on, s_off)):
        want = "memory" in then.mentor_checks
        j = Journal(":memory:", 10_000)
        owner_card = Orchestrator(first, j).apply_champion()
        lab_card = dataclasses.replace(owner_card, name="lab challenger", cooldown_hours=12.0)
        Lab(first, j).set_champion(lab_card, by="auto-promote", note="passed the exam and the forward test")
        since = j.get("strategy_champion")["since"]
        card = Orchestrator(then, j).apply_champion()          # the owner changed MENTOR_CHECKS; the desk restarts
        assert card.name == "lab challenger" and ("memory" in card.checks) == want
        state = j.get("strategy_champion")
        assert (state["by"], state["since"], state["previous"]["id"]) == ("auto-promote", since, owner_card.id)
        for _ in range(ROLLBACK_MIN_TRADES):                    # the lab card's live trades turn clearly bad
            j.open_position("BTCUSDT", {"ok": True, "qty": 1.0, "avg_price": 100.0, "quote": 100.0}, 90.0, 120.0)
            j.close_position("BTCUSDT", {"ok": True, "qty": 1.0, "avg_price": 95.0, "quote": 95.0},
                             "stop-loss (exchange)")
        assert Lab(then, j).watch_live().startswith("profit factor 0.00")
        back = Orchestrator(then, j).apply_champion()
        assert back.name == owner_card.name and back.cooldown_hours == owner_card.cooldown_hours
        assert set(back.checks) == set(then.mentor_checks)      # the owner's card with the owner's memory choice
        assert j.get("strategy_champion")["by"] == "rollback" and Lab(then, j).watch_live() is None
        hist = j.get("strategy_history")
        synced = [h for h in hist if h["event"] == "settings"]
        # the rollback itself puts the owner's choice in force (Lab.set_champion): no second 'settings' event after it
        assert [h["id"] for h in synced] == [card.id] and {h["by"] for h in synced} == {"owner settings"}
        assert [(h["event"], h["by"]) for h in hist if h["id"] == back.id][-2:] == [("promote", "rollback"),
                                                                                    ("rollback", "lab")]
        assert champion_at(hist, "9999")["id"] == back.id
        assert [p[1] for p in card_periods(hist, card.id)] == [next(h["ts"] for h in hist[hist.index(synced[0]) + 1:]
                                                                   if h["event"] == "promote")]
        changes = strategy_changes(j, "2000-01-01")
        assert all(h["event"] == "promote" for h in changes["promoted"]) and changes["settings"] == synced
        text = strategy_changes_text(changes, 7)
        assert ("⚙️" in text and "stays in force" in text and "the memory rule is now " + ("on" if want else "off")
                in text)
        assert f"settings lab challenger [{card.id}] by owner settings" in lineage_text(j)


def test_the_trend_desk_never_offers_a_seed():
    """/postmortem seed refuses on a trend desk, so no text offers it there; the hourly desk's texts still do."""
    from trader.postmortem_report import SEED_TREND, seed_status_text
    j = Journal(":memory:", 10_000)
    _closed_trade(j, "ETHUSDT", {})
    trend, hourly = Settings(offline=True, strategy="trend"), Settings(offline=True)
    texts = [seed_status_text(j, trend), postmortem_text(j, trend, "seed-status"), postmortem_text(j, trend, "x"),
             postmortem_text(j, trend, ""), postmortem_text(j, trend, "memory"), postmortem_md(j, trend)]
    j.set("memory_seed", {"trades": 12, "ts": "2026-10-01T00:00:00+00:00", "card_id": "0ld0ld00", "days": 365,
                          "coins": ["BTCUSDT"], "until": "2026-10-01T00:00:00+00:00", "error": None})
    texts += [seed_status_text(j, trend), postmortem_text(j, trend, "seed status"), postmortem_md(j, trend)]
    for t in texts:
        assert "/postmortem seed" not in t, t
    assert seed_status_text(j, trend) == "🧠 No replay seed: the daily trend rule needs none. " + SEED_TREND
    assert postmortem_text(j, trend, "x") == "Usage: /postmortem, /postmortem 10 (at most 20) or /postmortem memory"
    assert postmortem_text(j, hourly, "x") == ("Usage: /postmortem, /postmortem 10 (at most 20), /postmortem memory or "
                                               "/postmortem seed")
    assert "/postmortem seed" in seed_status_text(j, hourly)
