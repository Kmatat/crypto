"""The event study (trader/agents/events.py, trader/events_report.py): headlines with publication times, bursts,
news velocity, the macro calendar, the normal-day baseline and the jump rule, the placebo window, the settle step
with its request budget, the owner's texts, and the guarantee that none of it changes a trade."""
import datetime as dt
import json
import math
import time
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from trader.__main__ import HELP_TEXT, cycle_line, help_text
from trader.agents import events as ev
from trader.agents import news as news_module
from trader.agents.events import EventsAgent, classify, headline_sentiment, load_calendar
from trader.agents.news import NewsAgent
from trader.binance_client import BinanceError
from trader.config import Settings
from trader.events_report import (EVENTS_TREND, category_sentence, category_table, event_sentence, events_line, events_md,
                                  events_text, verdict)
from trader.journal import Journal
from trader.notes import Notes
from trader.notify import trade_messages
from trader.orchestrator import Orchestrator

MIN = ev.MIN_MS
DAY = ev.DAY_MS
FIXTURES = Path(__file__).parent / "fixtures" / "events_windows.json"


def ms(text: str) -> int:
    return ev.ms_of(text)


def when(text: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms(text) / 1000, dt.timezone.utc)


def has_tz() -> bool:
    return ev._new_york() is not None


# ---- helpers: candles, a fake price API, a calendar file, an agent ------------------------------------------
def window(t_ms, levels=None, n=600, noise=0.0, volume=None, price=100.0):
    """`n` one-minute candles from t-360 min. The close of the candle opening at t + (m-1) min (known at t + m) is
    price x (1 + level %), where the level is the last {minute: pct} of `levels` at or before m (minutes before the
    event are negative); `noise` adds a small wiggle; `volume(m)` sets the volume (default 1)."""
    marks = sorted((levels or {}).items())
    out = []
    for i in range(n):
        m = i - 359
        level = 0.0
        for mm, lv in marks:
            if m >= mm:
                level = lv
        close = price * (1 + level / 100) * (1 + noise / 100 * math.sin(i * 0.7))
        out.append({"time": t_ms - 360 * MIN + i * MIN, "open": close, "high": close * 1.0001, "low": close * 0.9999,
                    "close": close, "volume": volume(m) if volume else 1.0})
    return out


def baseline_series(t_ms, step_min=15, n=1000, move=0.5):
    """`n` candles of `step_min` minutes ending at t (as the baseline download): flat at 100, except that every day at
    t's New York clock time the price climbs `move` % over the next two hours and comes back."""
    step = step_min * MIN
    t_al = t_ms - t_ms % step
    start = t_al - n * step
    closes = {start + i * step: 100.0 for i in range(n)}
    points = 120 // step_min
    for d in range(1, 40):
        s = ev.same_clock_days_ago(t_ms, d)
        s -= s % step
        for i in range(points):
            if s + i * step in closes:
                closes[s + i * step] = 100 * (1 + move * (i + 1) / points / 100)
    return [{"time": t, "open": c, "high": c, "low": c, "close": c, "volume": 10.0} for t, c in sorted(closes.items())]


class FakeClient:
    """klines() like the public API: 1-minute windows from window() with the jumps given per (symbol, event ms),
    baseline series from baseline_series(); counts every call; can fail for a symbol or return too few candles."""

    def __init__(self, jumps=None, fail=(), short=None, move=0.5, noise=0.0, refuse=()):
        self.calls, self.jumps, self.fail, self.refuse = [], dict(jumps or {}), set(fail), set(refuse)
        self.short, self.move, self.noise = dict(short or {}), move, noise

    def klines(self, symbol, interval="1h", limit=200, start_ms=None):
        self.calls.append((symbol, interval, limit, start_ms))
        if symbol in self.fail:
            raise BinanceError("HTTP 429 too many requests")
        if symbol in self.refuse:          # what the public API answers for a coin name it does not know
            raise BinanceError("HTTP 400: Invalid symbol.")
        if interval == "1m":
            t = start_ms + ev.PRE_MIN * MIN
            return window(t, self.jumps.get((symbol, t)), n=self.short.get(symbol, limit), noise=self.noise)
        step_min = int(interval[:-1])
        return baseline_series(start_ms + limit * step_min * MIN, step_min, limit, self.move)


class Clock:
    def __init__(self, text):
        self.t = ms(text) / 1000

    def __call__(self):
        return self.t


def write_calendar(path, events=(), holidays=(), raw=None):
    path.write_text(raw if raw is not None else json.dumps({"version": 1, "events": list(events),
                                                            "holidays": list(holidays)}))
    return path


def make_agent(tmp_path, client=None, events=(), holidays=(), budget=60, clock="2026-10-31T12:00:00Z", **settings):
    s = Settings(offline=True, events_daily_requests=budget, **settings)
    j = Journal(":memory:", 10_000)
    shipped = write_calendar(tmp_path / "shipped.json", events, holidays)
    a = EventsAgent(j, client, s, shipped_calendar=shipped, owner_calendar=tmp_path / "owner.json", now_fn=Clock(clock))
    a.refresh_calendar(force=True)
    return a


def add_event(a, ts, kind="macro", symbol="BTCUSDT", n=1, label="US CPI, September 2026 data", category="macro:cpi"):
    cur = a.j.db.execute("INSERT INTO news_events (kind, ts_utc, symbol, label, category, n_headlines) VALUES (?,?,?,?,?,?)",
                         (kind, ev.iso(ms(ts)), symbol, label, category, n if kind == "headline" else None))
    a.j.db.commit()
    return cur.lastrowid


def row(a, event_id):
    return dict(a.j.db.execute("SELECT * FROM news_events WHERE id=?", (event_id,)).fetchone())


def flat_baseline(b=0.5, b60=0.0):
    return {"b": {ev.IMPULSE_MIN: b}, "days": 6, "volume": 10.0, "step": 15}, {"b": {ev.QUIET_MIN: b60}, "days": 6}


T = ms("2026-10-14T12:30:00Z")   # a Wednesday, 08:30 New York


# ---- 1-5: the jump rule on synthetic candles ------------------------------------------------------------------
def test_impulse_flags_a_jump_and_not_a_quiet_window():
    m = ev.measure(window(T, {1: 2.0}), T, *flat_baseline(0.5))
    assert m["complete"] and m["impulse"] and m["propagation_min"] == 1 and m["direction"] == 1
    assert m["threshold_pct"] == pytest.approx(1.5) and m["magnitude_pct"] == pytest.approx(2.0)
    quiet = ev.measure(window(T, {1: 0.4}), T, *flat_baseline(0.5))
    assert quiet["complete"] and quiet["impulse"] is False and quiet["direction"] is None
    assert quiet["magnitude_pct"] == pytest.approx(0.4) and quiet["peak_ts"] == ev.iso(T + MIN)   # filled anyway


def test_threshold_scales_with_the_normal_day_baseline_and_the_floor_binds_on_quiet_days():
    assert ev.threshold(1.0) == pytest.approx(3.0) and ev.threshold(0.05) == ev.FLOOR_PCT == 0.3
    assert ev.K_WEEKDAY == 3.0 and ev.K_WEEKEND == 4.5
    saturday = ms("2026-10-17T12:30:00Z")
    assert ev.k_for(T) == 3.0 and ev.k_for(saturday) == 4.5
    # 0.25% on a day whose normal move is 0.05%: below the floor, no jump; 2% is 4x a 0.5% normal day: a jump on a
    # weekday, not at a weekend (4.5 x 0.5 = 2.25%)
    assert ev.measure(window(T, {1: 0.25}), T, *flat_baseline(0.05))["impulse"] is False
    assert ev.measure(window(saturday, {1: 2.0}), saturday, *flat_baseline(0.5))["impulse"] is False
    assert ev.measure(window(saturday, {1: 2.4}), saturday, *flat_baseline(0.5))["impulse"] is True


def test_peak_retrace_and_volume_exhaustion_are_post_mortem_facts():
    candles = window(T, {1: 0.5, 5: 1.0, 10: 2.0, 50: 1.2}, volume=lambda m: 0.5 if 11 <= m <= 25 else 1.0)
    m = ev.measure(candles, T, *flat_baseline(0.5))
    assert m["impulse"] and m["propagation_min"] == 10 and m["peak_min"] == 10
    assert m["peak_ts"] == ev.iso(T + 10 * MIN)
    assert m["correction_delay_min"] == 40 and m["retrace_ts"] == ev.iso(T + 50 * MIN)   # 2% -> 1.2% gives back > 38.2%
    assert m["volume_exhausted"] is True
    busy = ev.measure(window(T, {10: 2.0}), T, *flat_baseline(0.5))
    assert busy["volume_exhausted"] is False and busy["correction_delay_min"] is None   # never gave back
    late = ev.measure(window(T, {120: 2.0}), T, *flat_baseline(0.5))
    assert late["peak_min"] == 120
    text = event_sentence({**late, "kind": "macro", "symbol": "BTCUSDT", "label": "US CPI", "ts_utc": ev.iso(T),
                           "impulse": 1, "category": "macro:cpi", "placebo_ts": None})
    assert "still moving at the end of the 2-hour window" in text and "had not given back 38%" in text


def test_price_moved_first_is_detected_in_the_hour_before():
    m = ev.measure(window(T, {-20: 1.5}), T, *flat_baseline(0.5))
    assert m["moved_first"] is True and m["impulse"] is False
    assert ev.measure(window(T, {1: 2.0}), T, *flat_baseline(0.5))["moved_first"] is False
    # the hour before is judged against the normal days' hour before, at its own clock time
    assert ev.measure(window(T, {-20: 1.5}), T, flat_baseline(0.5)[0], {"b": {ev.QUIET_MIN: 0.6}})["moved_first"] is False
    assert ev.measure(window(T, {-20: 1.5}), T, flat_baseline(0.5)[0], None)["moved_first"] is None


def test_direction_is_the_sign_of_the_largest_excursion_not_the_sentiment(tmp_path):
    m = ev.measure(window(T, {1: -2.0}), T, *flat_baseline(0.5))
    assert m["impulse"] and m["direction"] == -1 and m["magnitude_pct"] == pytest.approx(-2.0)
    a = make_agent(tmp_path)
    seed(a.j, kind="headline", category="hack-security", sentiment=0.8, direction=-1, magnitude_pct=-2.0)
    agg = category_table(a.j)[0]
    assert agg["class"] == "headline/hack-security" and agg["agree"] == (0, 1)


def test_measure_never_raises_and_names_what_is_missing():
    assert ev.measure([], T, *flat_baseline())["note"] == "incomplete candles (0 of 600)"
    assert ev.measure([{"x": 1}, "junk", None], T, *flat_baseline())["complete"] is False
    few = ev.measure(window(T, {1: 2.0}), T, {"b": {}, "days": 2}, None)
    assert few["complete"] is False and few["note"] == "too few normal days for a baseline (2 of 3 needed)"


# ---- the normal-day baseline: time of day, its own days, the classes it skips ---------------------------------
def six_hour_baseline(candles, t_ms):
    """The study's rejected rule: the median largest 2-hour move starting in the 6 hours before the event."""
    by = {c["time"]: c["close"] for c in candles}
    vals = []
    for s0 in range(-359, -119):
        ref = by[t_ms + (s0 - 1) * MIN]
        vals.append(max(abs(by[t_ms + (s0 + i) * MIN] / ref - 1) * 100 for i in range(120)))
    return sorted(vals)[len(vals) // 2]


def test_the_baseline_is_time_of_day_aware_a_normal_busy_morning_is_no_jump():
    # every day: a quiet night, then a 1% move in the two hours after 08:30 New York (the stock-market open)
    series = baseline_series(T, move=1.0)
    candles = window(T, {30: 0.5, 60: 1.0}, noise=0.02)          # today: exactly that normal morning
    base, before = ev.baselines(series, T, set())
    assert base["b"][120] == pytest.approx(1.0) and base["days"] == 7     # the weekdays of 10.4 days
    assert ev.measure(candles, T, base, before)["impulse"] is False
    p0 = candles[359]["close"]
    old = ev.impulse(candles, T, p0, six_hour_baseline(candles, T), k=2.0)
    assert old["impulse"] is True        # the quiet night as the yardstick calls a normal morning a jump
    assert ev.measure(window(T, {1: 3.5}), T, base, before)["impulse"] is True   # 3.5x the usual morning is one


def test_the_baseline_uses_earlier_normal_days_of_the_same_kind_only():
    series = baseline_series(T)
    skip = {"2026-10-13", "2026-10-09"}                          # a release day and a holiday
    base = ev.normal_day_baseline(series, T, skip, horizons=(120,))
    assert base["days"] == 5 and all(d < "2026-10-14" for d in base["dates"])   # 7 weekdays in 10.4 days, 2 skipped
    assert not set(base["dates"]) & skip
    assert all(dt.date.fromisoformat(d).weekday() < 5 for d in base["dates"])
    placebo = ms("2026-10-12T12:30:00Z")
    own = ev.normal_day_baseline(series, placebo, skip, horizons=(120,))
    assert "2026-10-12" not in own["dates"] and all(d < "2026-10-12" for d in own["dates"])   # never its own day
    saturday = ms("2026-10-17T12:30:00Z")
    weekend = ev.normal_day_baseline(baseline_series(saturday, 30), saturday, set(), horizons=(120,))
    assert weekend["step"] == 30 and weekend["days"] >= 5
    assert all(dt.date.fromisoformat(d).weekday() >= 5 for d in weekend["dates"])
    short = ev.normal_day_baseline(series[-150:], T, set(), horizons=(120,))
    assert short["b"] == {} and short["days"] < 3


@pytest.mark.skipif(not FIXTURES.exists(), reason="test fixture missing")
def test_real_release_windows_from_the_macro_study():
    """Two real windows of the 63-release study (Binance 1-minute and 15-minute candles), judged exactly as the desk
    judges them: the release flags a jump, its matched normal morning does not, although the rejected 'preceding 6
    hours' baseline calls that normal morning a jump too."""
    data = json.loads(FIXTURES.read_text())
    skip = {"2026-08-07", "2026-08-12", "2026-09-04", "2026-09-07", "2026-09-11"}   # listed releases, Labor Day
    expected = {"CPI_20260911": (3.27, 80, 93, 34, -1.38), "NFP_20260904": (-3.77, 2, 17, None, 1.77)}
    for w in data["windows"]:
        def candles(kind):
            start = w[kind + "_1m_start_ms"]
            return [{"time": start + i * MIN, "open": r[2], "high": r[0], "low": r[1], "close": r[2], "volume": r[3]}
                    for i, r in enumerate(w[kind + "_1m"])]
        start15 = w["baseline_15m_start_ms"]
        series = [{"time": start15 + i * 15 * MIN, "close": r[0], "volume": r[1]} for i, r in enumerate(w["baseline_15m"])]
        t, tp = ms(w["event_utc"]), ms(w["placebo_utc"])
        assert ev.baseline_start_ms(t) == start15
        m = ev.measure(candles("event"), t, *ev.baselines(series, t, skip))
        p = ev.measure(candles("placebo"), tp, *ev.baselines(series, tp, skip))
        mag, prop, peak, retrace, pmag = expected[w["id"]]
        assert m["impulse"] and m["magnitude_pct"] == pytest.approx(mag, abs=0.01)
        assert (m["propagation_min"], m["peak_min"], m["correction_delay_min"]) == (prop, peak, retrace)
        assert m["direction"] == (1 if mag > 0 else -1) and m["moved_first"] is False
        assert p["complete"] and p["impulse"] is False and p["magnitude_pct"] == pytest.approx(pmag, abs=0.01)
        assert p["baseline_days"] >= 4 and m["volume_ratio"] > 2
        pc = candles("placebo")
        assert ev.impulse(pc, tp, pc[359]["close"], six_hour_baseline(pc, tp), k=2.0)["impulse"] is True


# ---- 6-8, 23-24: headlines, bursts, velocity, the feeds -------------------------------------------------------
def rss(items):
    body = "".join(f"<item><title>{escape(t)}</title><link>{escape(link)}</link>"
                   + (f"<pubDate>{p}</pubDate>" if p else "") + "</item>" for t, link, p in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


class FakeFeed:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body


def fake_feeds(monkeypatch, bodies):
    calls = []

    def get(url, **kw):
        calls.append(url)
        return FakeFeed(bodies[news_module.FEEDS.index(url)])
    monkeypatch.setattr(news_module.requests, "get", get)
    return calls


def item(title, ts, link=None, source="coindesk.com"):
    return {"title": title, "link": link if link is not None else "https://x.example/" + title.replace(" ", "-"),
            "ts": ev.iso(ms(ts)), "source": source}


def test_headlines_are_stored_once_per_coin_with_publication_time_source_and_category(monkeypatch, tmp_path):
    fake_feeds(monkeypatch, [rss([("Bitcoin and Ether slide after exchange hack", "https://coindesk.com/a",
                                   "Sat, 31 Oct 2026 12:49:47 +0000"),
                                  ("Fed minutes due", "https://coindesk.com/b", "Sat, 31 Oct 2026 11:00:00 +0000")]),
                             rss([]), rss([])])
    news = NewsAgent(offline=False)
    items = news.fetch_items()
    assert items[0] == {"title": "Bitcoin and Ether slide after exchange hack", "link": "https://coindesk.com/a",
                        "ts": "2026-10-31T12:49:47+00:00", "source": "coindesk.com"}
    a = make_agent(tmp_path, clock="2026-10-31T13:00:00Z")
    fetched = ms("2026-10-31T13:00:00Z") / 1000
    assert a.ingest(items, ["BTC", "ETH", "SOL"], {"BTC": {"score": -0.6}}, fetched) == 3
    rows = [dict(r) for r in a.j.db.execute("SELECT * FROM headlines ORDER BY id")]
    assert [(r["coins"], r["category"], r["source"]) for r in rows] == [
        ("BTC", "hack-security", "coindesk.com"), ("ETH", "hack-security", "coindesk.com"), ("", "macro", "coindesk.com")]
    assert rows[0]["url_hash"] == rows[1]["url_hash"] and rows[2]["burst_id"] is None
    assert rows[0]["ts_utc"] == "2026-10-31T12:49:47+00:00" and rows[0]["sentiment"] < 0
    btc = dict(a.j.db.execute("SELECT * FROM news_events WHERE symbol='BTCUSDT'").fetchone())
    assert btc["kind"] == "headline" and btc["ts_utc"] == "2026-10-31T12:49:00+00:00" and btc["sentiment"] == -0.6
    assert a.ingest(items, ["BTC", "ETH", "SOL"], None, fetched + 60) == 0          # nothing twice
    assert classify("Hacker drains bridge as price falls") == "hack-security"       # a hack before price talk
    assert classify("SEC sues exchange") == "regulation" and classify("Nice weather") == "other"
    assert headline_sentiment("Bitcoin surges to record high") > 0


def test_bursts_are_anchored_to_the_first_headline_and_velocity_uses_the_7_day_median(tmp_path):
    a = make_agent(tmp_path)
    fetched = ms("2026-10-31T11:00:00Z") / 1000
    burst = [item(f"Bitcoin story {i}", f"2026-10-31T10:{mm}:00Z") for i, mm in enumerate(("00", "20", "29"))]
    a.ingest(burst + [item("Bitcoin story late", "2026-10-31T10:31:00Z")], ["BTC"], None, fetched)
    rows = [dict(r) for r in a.j.db.execute("SELECT * FROM news_events ORDER BY ts_utc")]
    assert [(r["ts_utc"][11:16], r["n_headlines"]) for r in rows] == [("10:00", 3), ("10:31", 1)]
    # an older straggler moves the start earlier (feeds deliver out of order)
    a.ingest([item("Bitcoin story early", "2026-10-31T09:59:00Z")], ["BTC"], None, fetched + 60)
    first = dict(a.j.db.execute("SELECT * FROM news_events ORDER BY ts_utc LIMIT 1").fetchone())
    assert first["ts_utc"][11:16] == "09:59" and first["n_headlines"] == 3 + 1 and first["label"] == "Bitcoin story early"
    # velocity: headlines in the hour before / (median daily count of the previous 7 days / 24), floor 1 a day
    b = make_agent(tmp_path)
    for d in range(1, 8):
        for k in range(48 if d <= 4 else 12):
            b.j.db.execute("INSERT INTO headlines (ts_utc, coins, title, url_hash) VALUES (?,?,?,?)",
                           (ev.iso(ms("2026-10-31T00:00:00Z") - d * DAY + k * 60_000), "BTC", "t", f"h{d}-{k}"))
    assert b.velocity("BTC", ms("2026-10-31T10:00:00Z"), extra=2) == pytest.approx(2 / (48 / 24))
    assert b.velocity("SOL", ms("2026-10-31T10:00:00Z"), extra=1) == pytest.approx(24.0)   # floor: one a day


def test_ingest_time_sanity_and_burst_span(tmp_path):
    a = make_agent(tmp_path)
    fetched_ms = ms("2026-10-31T12:00:00Z")
    a.ingest([item("Bitcoin future story", "2026-10-31T12:10:00Z"),
              {"title": "Bitcoin no date", "link": "l1", "ts": None, "source": "x"},
              {"title": "Bitcoin bad date", "link": "l2", "ts": "not a date", "source": "x"},
              item("Bitcoin old story", "2026-10-27T11:00:00Z")], ["BTC"], None, fetched_ms / 1000)
    by = {r["title"]: dict(r) for r in a.j.db.execute("SELECT * FROM headlines")}
    assert by["Bitcoin future story"]["ts_utc"] == ev.iso(fetched_ms)          # a future time is the fetch time
    assert by["Bitcoin no date"]["ts_utc"] == by["Bitcoin bad date"]["ts_utc"] == ev.iso(fetched_ms)
    assert by["Bitcoin old story"]["burst_id"] is None                          # 4 days old: stored, opens nothing
    assert news_module.publication_time("Sat, 31 Oct 2026 12:49:47") == "2026-10-31T12:49:47+00:00"   # naive = UTC
    assert news_module.publication_time("garbage") is None and news_module.publication_time(None) is None
    b = make_agent(tmp_path)
    b.ingest([item("Bitcoin a", "2026-10-31T10:00:00Z"), item("Bitcoin b", "2026-10-31T10:29:00Z")], ["BTC"], None,
             fetched_ms / 1000)
    b.ingest([item("Bitcoin straggler", "2026-10-31T09:35:00Z")], ["BTC"], None, fetched_ms / 1000 + 60)
    spans = [(r["ts_utc"][11:16], r["n_headlines"]) for r in b.j.db.execute("SELECT * FROM news_events ORDER BY ts_utc")]
    assert spans == [("09:35", 1), ("10:00", 2)]                                # it would stretch the burst to 54 min


def old_fetch_headlines(per_feed=25):
    """The 1.7.8 loop, kept as the reference the new code must reproduce."""
    out = []
    for url in news_module.FEEDS:
        r = news_module.requests.get(url, timeout=10)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(b"".join(r.iter_content(65536)))
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if title:
                out.append(title)
            if len(out) >= per_feed * len(news_module.FEEDS):
                break
    return list(dict.fromkeys(out))


def test_fetch_items_keeps_the_existing_cap_and_order(monkeypatch):
    bodies = [rss([(f"Feed {f} story {i}", f"https://f{f}.example/{i}", None) for i in range(n)])
              for f, n in ((1, 40), (2, 30), (3, 20))]
    bodies[1] = rss([("Feed 1 story 3", "https://dup.example", None)] + [(f"Feed 2 story {i}", f"https://f2/{i}", None)
                                                                        for i in range(29)])
    fake_feeds(monkeypatch, bodies)
    expected = old_fetch_headlines()
    news = NewsAgent(offline=False)
    titles = news.fetch_headlines()
    assert titles == expected and len(news.last_items) == 75 and len(titles) == 74   # the duplicate title once
    assert [i["title"] for i in news.last_items][:3] == ["Feed 1 story 0", "Feed 1 story 1", "Feed 1 story 2"]
    assert news.last_fetch > 0


def test_fetch_headlines_behaviour_is_unchanged_and_monkeypatching_it_prevents_any_fetch(monkeypatch, tmp_path):
    def no_network(*a, **kw):
        raise AssertionError("no feed may be fetched")
    monkeypatch.setattr(news_module.requests, "get", no_network)
    news = NewsAgent(offline=False)
    monkeypatch.setattr(news, "fetch_headlines", lambda: ["Bitcoin ETF sees record inflows"])
    out = news.run(["BTC"])
    assert out["BTC"]["score"] > 0 and news.last_items == [] and news.last_result is out
    a = make_agent(tmp_path)
    assert a.ingest(news.last_items, ["BTC"], news.last_result, news.last_fetch or None) == 0


# ---- 9-10: the calendar ---------------------------------------------------------------------------------------
def test_shipped_calendar_matches_the_eastern_wall_clock_and_names_its_sources():
    data = json.loads(ev.SHIPPED_CALENDAR.read_text())
    rows, problems, holidays = load_calendar(ev.SHIPPED_CALENDAR, None)
    assert problems == [] and len(rows) == len(data["events"]) >= 45 and len(holidays) == len(data["holidays"])
    assert {r["kind"] for r in rows} == {"cpi", "nfp", "fomc"}
    assert ms(rows[-1]["ts_utc"]) - ms(data["read_on"] + "T00:00:00Z") >= 365 * DAY
    assert "bls.gov/schedule/news_release/cpi.htm" in data["sources"]["cpi"]
    assert "bls.gov/schedule/news_release/empsit.htm" in data["sources"]["nfp"]
    assert "federalreserve.gov/monetarypolicy/fomccalendars.htm" in data["sources"]["fomc"]
    for e in data["events"]:
        d = when(e["ts_utc"])
        assert e["kind"] != "nfp" or d.weekday() == 4                       # payrolls on Fridays
        assert e["kind"] != "fomc" or d.weekday() == 2                      # FOMC statements on Wednesdays
        assert not set(data["holidays"]) & {d.date().isoformat()}
    if not has_tz():
        pytest.skip("no time-zone database on this machine: the ET -> UTC check needs it")
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    for e in data["events"]:
        local = dt.datetime.strptime(e["local"].removesuffix(" ET"), "%Y-%m-%d %H:%M").replace(tzinfo=ny)
        assert local.astimezone(dt.timezone.utc) == when(e["ts_utc"]), e
        assert local.strftime("%H:%M") == ("14:00" if e["kind"] == "fomc" else "08:30")
    # across the clock changes: 1 Nov 2026 (EDT ends), 14 Mar 2027 (starts), 7 Nov 2027 (ends)
    by = {e["ts_utc"][:10]: e["ts_utc"][11:16] for e in data["events"]}
    assert (by["2026-10-28"], by["2026-11-06"], by["2027-03-10"], by["2027-03-17"]) == ("18:00", "13:30", "13:30", "18:00")
    assert (by["2027-11-05"], by["2027-11-10"], by["2027-12-08"]) == ("12:30", "13:30", "19:00")


def test_calendar_merge_owner_file_and_scheduling_for_watched_coins(tmp_path):
    shipped = [{"ts_utc": "2026-10-14T12:30:00Z", "name": "US CPI, September 2026 data", "kind": "cpi"},
               {"ts_utc": "2026-10-02T12:30:00Z", "name": "US jobs", "kind": "nfp"},
               {"ts_utc": "2026-11-06T13:30:00Z", "name": "US jobs, October", "kind": "nfp"}]
    a = make_agent(tmp_path, FakeClient(), shipped)
    (tmp_path / "owner.json").write_text(json.dumps({"events": [
        {"ts_utc": "2026-10-15T12:30:00Z", "name": "US PPI", "kind": "ppi"},
        {"ts_utc": "2026-10-02T12:30:00Z", "name": "US jobs", "kind": "nfp", "remove": True}],
        "holidays": ["2026-10-12"]}))
    a.refresh_calendar(force=True)
    cal = [tuple(r) for r in a.j.db.execute("SELECT ts_utc, kind FROM macro_calendar ORDER BY ts_utc")]
    assert cal == [("2026-10-14T12:30:00+00:00", "cpi"), ("2026-10-15T12:30:00+00:00", "ppi"),
                   ("2026-11-06T13:30:00+00:00", "nfp")] and "2026-10-12" in a.holidays
    now = when("2026-10-20T00:00:00Z")
    assert a.schedule_macro(now, ["BTCUSDT", "ETHUSDT"]) == 4                  # two releases x two coins, none ahead
    assert a.schedule_macro(now, ["BTCUSDT", "ETHUSDT"]) == 0
    cats = {r[0] for r in a.j.db.execute("SELECT category FROM news_events")}
    assert cats == {"macro:cpi", "macro:ppi"}
    # a coin that joins the watchlist later is measured from the next release on, not back-filled
    assert a.schedule_macro(when("2026-11-07T00:00:00Z"), ["BTCUSDT", "ETHUSDT", "SOLUSDT"]) == 3
    assert a.j.db.execute("SELECT COUNT(*) FROM news_events WHERE symbol='SOLUSDT'").fetchone()[0] == 1
    # a malformed owner file: reported once in the diary, ignored, the shipped rows stay
    (tmp_path / "owner.json").write_text("{not json")
    a.refresh_calendar(force=True)
    a.refresh_calendar(force=True)
    errors = [e for e in a.j.events() if e["kind"] == "error"]
    assert len(errors) == 1 and "cannot be read" in errors[0]["text"]
    assert a.j.db.execute("SELECT COUNT(*) FROM macro_calendar WHERE kind='cpi'").fetchone()[0] == 1
    # switched off for months and back on: still no more than the last 60 days
    a.j.set("events_macro_to", "2025-01-01T00:00:00+00:00")
    a.j.db.execute("INSERT INTO macro_calendar (ts_utc, name, kind) VALUES ('2025-06-01T12:30:00+00:00', 'old', 'cpi')")
    # 2 Oct is back (the broken owner file no longer removes it), 14 Oct, 15 Oct, 6 Nov; not 1 Jun 2025
    assert a.schedule_macro(when("2026-11-08T00:00:00Z"), ["XRPUSDT"]) == 4


@pytest.mark.parametrize("raw", [None, "{broken", "[1, 2]", '{"events": 5}', '{"events": [{"ts_utc": "2026-10-14 12:30"},'
                                                                                 ' "x", {"name": "a"}], "holidays": "no"}'])
def test_a_missing_or_malformed_shipped_calendar_never_stops_the_desk(tmp_path, raw):
    client = FakeClient(jumps={("BTCUSDT", ms("2026-10-31T06:00:00Z")): {1: 2.0}})
    a = make_agent(tmp_path, client, clock="2026-10-31T12:00:00Z")
    if raw is None:
        a.shipped = tmp_path / "missing.json"
    else:
        write_calendar(a.shipped, raw=raw)
    a.refresh_calendar(force=True)
    add_event(a, "2026-10-31T06:00:00Z", kind="headline", n=2, label="Bitcoin story", category="other")
    out = a.step(when("2026-10-31T12:00:00Z"), ["BTCUSDT"], [], None, None)
    assert not [r for r in out if "error" in r] and out[0]["settled"] == 1   # headline events are still measured
    status = a.j.get("events_calendar")
    assert status["last_ts"] is None and (raw is None or status["problems"])
    assert "no release calendar loaded" in events_text(a.j, a.s)


# ---- 11-16, 22: the settle step ------------------------------------------------------------------------------
def test_settle_takes_complete_events_only_in_priority_order_within_the_per_check_cap(tmp_path):
    rel, b3, b1 = "2026-10-14T12:30:00Z", "2026-10-14T15:00:00Z", "2026-10-14T13:05:00Z"
    client = FakeClient(jumps={("BTCUSDT", ms(rel)): {1: 2.0}, ("ETHUSDT", ms(b3)): {3: -2.5}})
    a = make_agent(tmp_path, client)
    one = add_event(a, b1, kind="headline", symbol="SOLUSDT", n=1, label="Solana", category="other")
    three = add_event(a, b3, kind="headline", symbol="ETHUSDT", n=3, label="Ether", category="price-talk")
    release = add_event(a, rel)
    late = add_event(a, "2026-10-14T18:00:00Z", kind="headline", n=5, label="Bitcoin", category="other")
    now = when("2026-10-14T21:30:00Z")             # 15:00 + 6.5 h: complete; 18:00 + 3.5 h: not yet
    out = a.settle(now)
    assert out == [{"symbol": "EVENTS", "stage": "events", "settled": 2, "requests": 6, "used": 6, "budget": 60}]
    assert [c[1] for c in client.calls] == ["1m", "15m", "1m"] * 2
    r, b = row(a, release), row(a, three)
    nearest = "2026-10-13" if release % 2 == 0 else "2026-10-12"      # odd ids take the second-nearest free day
    assert r["settled_at"] and r["impulse"] == 1 and r["direction"] == 1 and r["placebo_ts"] == nearest + "T12:30:00+00:00"
    assert r["placebo_impulse"] == 0 and r["baseline_pct"] == pytest.approx(0.5) and r["baseline_days"] >= 3
    assert b["settled_at"] and b["impulse"] == 1 and b["direction"] == -1 and b["propagation_min"] == 3
    assert row(a, one)["settled_at"] is None and row(a, late)["settled_at"] is None
    assert cycle_line(out[0]) == "EVENTS    measured 2 event(s) with 6 requests (6/60 today)"
    assert trade_messages(out) == []                                    # a measurement sends no message
    a.settle(now)
    assert row(a, one)["settled_at"] and row(a, late)["settled_at"] is None


def test_placebo_is_same_hour_nearest_free_weekday_before_with_the_exclusions(tmp_path):
    a = make_agent(tmp_path, events=[{"ts_utc": "2026-10-02T12:30:00Z", "name": "US jobs", "kind": "nfp"},
                                     {"ts_utc": "2026-09-29T12:30:00Z", "name": "US JOLTS", "kind": "jolts"}],
                   holidays=["2026-09-28"])
    now = when("2026-10-02T17:00:00Z")
    friday = add_event(a, "2026-10-02T12:30:00Z")
    odd = friday if friday % 2 else add_event(a, "2026-10-02T12:30:00Z", symbol="ETHUSDT")
    even = add_event(a, "2026-10-02T12:30:00Z", symbol="SOLUSDT") if odd == friday else friday
    # Thursday 1 Oct is the jobless-claims slot, Tue 29 Sep has a listed release, Mon 28 Sep a holiday, weekends are
    # another class, nothing after the event is complete yet
    assert ev.iso(a.placebo_time(row(a, even), now)) == "2026-09-30T12:30:00+00:00"
    assert ev.iso(a.placebo_time(row(a, odd), now)) == "2026-09-25T12:30:00+00:00"
    # for a release a same-coin headline burst on the candidate day does not disqualify it; another release row does
    add_event(a, "2026-09-30T12:00:00Z", kind="headline", n=2, label="Bitcoin", category="other")
    assert ev.iso(a.placebo_time(row(a, even), now)) == "2026-09-30T12:30:00+00:00"
    add_event(a, "2026-09-30T13:30:00Z", kind="macro", symbol="XRPUSDT", label="owner release", category="macro:x")
    assert ev.iso(a.placebo_time(row(a, even), now)) == "2026-09-25T12:30:00+00:00"
    # for a headline event a same-coin event within 2 hours does
    h = add_event(a, "2026-10-02T16:00:00Z", kind="headline", symbol="BNBUSDT", n=1, label="BNB", category="other")
    add_event(a, "2026-10-01T15:00:00Z", kind="headline", symbol="BNBUSDT", n=1, label="BNB", category="other")
    later = when("2026-10-02T21:00:00Z")
    got = ev.iso(a.placebo_time(row(a, h), later))
    assert got != "2026-10-01T16:00:00+00:00" and got.endswith("16:00:00+00:00")
    # nothing free within 7 days: None, and the record says so
    b = make_agent(tmp_path, FakeClient())
    lonely = add_event(b, "2026-10-14T12:30:00Z")
    for d in range(1, 8):           # another release row (an owner-added one, another coin) on every day before
        add_event(b, ev.iso(ev.same_clock_days_ago(ms("2026-10-14T12:30:00Z"), d) + 30 * MIN), symbol="XRPUSDT",
                  label="owner release", category="macro:x")
    b.settle_one(b.j.db.execute("SELECT * FROM news_events WHERE id=?", (lonely,)).fetchone(), when("2026-10-14T17:00:00Z"))
    r = row(b, lonely)
    assert r["placebo_ts"] is None and r["note"] == "no free placebo day within 7 days" and r["impulse"] is not None
    # only days after are free once they are complete: never Thursday 15 Oct (claims), the nearest weekdays after
    assert b.placebo_time(row(b, lonely), when("2026-10-15T18:00:00Z")) is None
    assert ev.iso(b.placebo_time(row(b, lonely), when("2026-10-30T00:00:00Z"))) in ("2026-10-16T12:30:00+00:00",
                                                                                  "2026-10-19T12:30:00+00:00")
    # back-filled long after with both sides complete: the days before still come first
    c = make_agent(tmp_path)
    back = add_event(c, "2026-10-14T12:30:00Z")
    assert ev.iso(c.placebo_time(row(c, back), when("2026-10-30T00:00:00Z"))) in ("2026-10-13T12:30:00+00:00",
                                                                                "2026-10-12T12:30:00+00:00")


@pytest.mark.skipif(not has_tz(), reason="no time-zone database on this machine")
def test_placebo_and_baseline_keep_the_new_york_clock_across_a_summer_time_switch(tmp_path):
    a = make_agent(tmp_path)
    now = when("2026-11-03T18:00:00Z")
    first = add_event(a, "2026-11-03T13:30:00Z")             # Tuesday 08:30 New York, the first week of winter time
    second = add_event(a, "2026-11-03T13:30:00Z", symbol="ETHUSDT")
    even, odd = (first, second) if first % 2 == 0 else (second, first)
    assert ev.iso(a.placebo_time(row(a, even), now)) == "2026-11-02T13:30:00+00:00"
    assert ev.iso(a.placebo_time(row(a, odd), now)) == "2026-10-30T12:30:00+00:00"   # Friday 08:30 EDT = 12:30 UTC
    assert ev.same_clock_days_ago(ms("2026-11-03T13:30:00Z"), 4) == ms("2026-10-30T12:30:00Z")
    assert ev.new_york_hour(ms("2026-11-03T13:30:00Z")) == ev.new_york_hour(ms("2026-10-30T12:30:00Z")) == 8


def test_daily_budget_stops_the_step_and_reports_it(tmp_path):
    a = make_agent(tmp_path, FakeClient(), budget=6)
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        add_event(a, "2026-10-14T12:30:00Z", symbol=sym)
    now = when("2026-10-14T18:00:00Z")
    assert a.settle(now)[0]["settled"] == 2 and a.budget(now) == (6, 6)
    assert a.settle(now) == [] and a.budget(now) == (6, 6)
    assert a.j.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NULL").fetchone()[0] == 1
    a.j.set("events_budget", {"day": dt.datetime.now(dt.timezone.utc).date().isoformat(), "used": 6})
    assert "used up; 1 event complete and queued: measuring resumes at 00:00 UTC" in events_text(a.j, a.s)
    tomorrow = when("2026-10-15T00:05:00Z")
    assert a.budget(tomorrow) == (0, 6) and a.settle(tomorrow)[0]["settled"] == 1


def test_a_failed_download_ends_the_step_counts_the_request_and_pauses_30_minutes(tmp_path):
    client = FakeClient(fail=["ETHUSDT"])
    a = make_agent(tmp_path, client, clock="2026-10-14T18:00:00Z")
    eth = add_event(a, "2026-10-14T12:30:00Z", symbol="ETHUSDT")
    btc = add_event(a, "2026-10-14T12:31:00Z", symbol="BTCUSDT")
    now = when("2026-10-14T18:00:00Z")
    out = a.settle(now)
    assert out == [{"symbol": "EVENTS", "stage": "events",
                    "error": "could not download prices for ETHUSDT: HTTP 429 too many requests"}]
    assert len(client.calls) == 1 and a.budget(now)[0] == 1 and row(a, eth)["settled_at"] is None
    assert row(a, btc)["settled_at"] is None                    # no further requests this check
    assert a.settle(now) == [] and len(client.calls) == 1       # paused
    msgs = trade_messages(out)
    assert msgs[0][0] == "error:EVENTS" and msgs[0][1].startswith("⚠️ Event study: could not download prices for ETHUSDT")
    a.now_fn.t += 31 * 60
    client.fail.clear()
    assert a.settle(now)[0]["settled"] == 2 and row(a, eth)["settled_at"]


def test_a_broken_record_is_written_off_and_never_blocks_the_queue(tmp_path):
    a = make_agent(tmp_path, FakeClient())
    broken = add_event(a, "2026-10-14T12:30:00Z")
    a.j.db.execute("UPDATE news_events SET ts_utc='2026-10-14T12:30:00+00:00x' WHERE id=?", (broken,))
    good = add_event(a, "2026-10-14T12:31:00Z", symbol="ETHUSDT")
    out = a.settle(when("2026-10-14T18:00:00Z"))
    assert out[0]["settled"] == 1 and "could not measure an event of BTCUSDT" in out[1]["error"]
    assert row(a, broken)["note"].startswith("could not be measured") and row(a, broken)["settled_at"]
    assert row(a, good)["impulse"] is not None and a.settle(when("2026-10-14T18:00:00Z")) == []


def test_incomplete_candles_settle_with_a_note_and_no_placebo_request(tmp_path):
    client = FakeClient(short={"BTCUSDT": 400})
    a = make_agent(tmp_path, client)
    e = add_event(a, "2026-10-14T12:30:00Z")
    a.settle(when("2026-10-14T18:00:00Z"))
    r = row(a, e)
    assert len(client.calls) == 1 and r["settled_at"] and r["note"] == "incomplete candles (400 of 600)"
    assert r["impulse"] is None and r["placebo_ts"] is None
    assert "not measured (incomplete candles (400 of 600))" in event_sentence(r)


def test_prune_drops_old_rows_and_budget_starved_headline_events(tmp_path):
    a = make_agent(tmp_path)
    now = when("2026-10-31T12:00:00Z")
    a.j.db.execute("INSERT INTO headlines (ts_utc, coins, title, url_hash) VALUES (?,?,?,?)",
                   (ev.iso(ms("2026-04-01T00:00:00Z")), "BTC", "old", "h-old"))
    a.j.db.execute("INSERT INTO headlines (ts_utc, coins, title, url_hash) VALUES (?,?,?,?)",
                   (ev.iso(ms("2026-10-30T00:00:00Z")), "BTC", "new", "h-new"))
    starved = add_event(a, "2026-10-23T10:00:00Z", kind="headline", n=1, label="b", category="other")
    waiting_release = add_event(a, "2026-10-23T12:30:00Z")
    fresh = add_event(a, "2026-10-30T10:00:00Z", kind="headline", n=1, label="b", category="other")
    ancient = add_event(a, "2025-09-01T12:30:00Z")
    a.j.db.execute("UPDATE news_events SET settled_at='2025-09-01T17:00:00+00:00' WHERE id=?", (ancient,))
    a.prune(now)
    ids = {r[0] for r in a.j.db.execute("SELECT id FROM news_events")}
    assert ids == {waiting_release, fresh} and starved not in ids
    assert [r[0] for r in a.j.db.execute("SELECT title FROM headlines")] == ["new"]
    assert a.j.get("events_dropped") == 1 and a.j.get("events_pruned_day") == "2026-10-31"
    add_event(a, "2026-10-01T10:00:00Z", kind="headline", n=1, label="b", category="other")
    a.prune(now)                                                 # once a day
    assert a.j.get("events_dropped") == 1


def test_overlap_notes_cover_the_noise_window_and_release_windows(tmp_path):
    a = make_agent(tmp_path, FakeClient(), events=[{"ts_utc": "2026-10-14T12:30:00Z", "name": "US CPI", "kind": "cpi"}])
    earlier = add_event(a, "2026-10-14T07:30:00Z", kind="headline", n=1, label="Bitcoin early", category="other")
    inside = add_event(a, "2026-10-14T12:49:00Z", kind="headline", n=2, label="Bitcoin drops as CPI comes in hot",
                       category="macro")
    a.j.db.execute("UPDATE news_events SET settled_at='x' WHERE id=?", (earlier,))
    a.settle_one(a.j.db.execute("SELECT * FROM news_events WHERE id=?", (inside,)).fetchone(),
                 when("2026-10-14T18:00:00Z"))
    note = row(a, inside)["note"]
    assert "overlaps another BTC event 319 min earlier" in note and "within the US CPI window" in note
    r = row(a, inside)
    assert r["impulse"] is not None                               # measured and shown ...
    a.j.db.execute("UPDATE news_events SET impulse=0, settled_at='2026-10-14T18:00:00+00:00' WHERE id=?", (earlier,))
    seed(a.j, kind="headline", category="macro", ts_utc="2026-10-10T10:00:00+00:00")
    agg = [x for x in category_table(a.j) if x["class"] == "headline/macro"][0]
    assert agg["n"] == 1 and agg["inside"] == 1                   # ... but kept out of the headline class
    assert "1 burst within 4 hours of a release, not counted" in events_text(a.j, a.s)


# ---- 17-18: the owner's texts --------------------------------------------------------------------------------
def seed(j, **kw):
    r = {"kind": "macro", "ts_utc": "2026-10-14T12:30:00+00:00", "symbol": "BTCUSDT", "label": "US CPI, September 2026 data",
         "category": "macro:cpi", "impulse": 1, "direction": -1, "propagation_min": 1, "magnitude_pct": -2.27,
         "peak_ts": "2026-10-14T14:21:00+00:00", "correction_delay_min": None, "volume_exhausted": 0,
         "placebo_ts": "2026-10-13T12:30:00+00:00", "placebo_impulse": 0, "placebo_magnitude_pct": 0.49,
         "settled_at": "2026-10-14T16:40:00+00:00", "baseline_pct": 0.6, "baseline_days": 6, "moved_first": 0,
         "volume_ratio": 4.2}
    r.update(kw)
    j.db.execute(f"INSERT INTO news_events ({','.join(r)}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
    j.db.commit()


def test_events_text_sentences_and_verdict_rules(tmp_path):
    a = make_agent(tmp_path)
    j, s = a.j, a.s
    assert "Nothing measured yet" in events_text(j, s)
    seed(j)
    seed(j, kind="headline", symbol="ETHUSDT", label="Ether falls after hack https://evil.example/x \x07", category="hack-security",
         impulse=0, magnitude_pct=-0.41, peak_ts="2026-10-14T13:30:00+00:00", moved_first=1, sentiment=-0.6, n_headlines=2,
         velocity=4.0, placebo_ts=None, placebo_impulse=None, placebo_magnitude_pct=None, ts_utc="2026-10-14T12:49:00+00:00")
    text = events_text(j, s)
    assert "BTC fell 2.27% within 111 min (the jump registered after 1 min); had not given back 38%" in text
    assert "Same hour on Tue 13 Oct (a normal day): +0.49%, no jump." in text
    assert "A jump at that hour needed 1.80% (3 x 0.60%; 0.60% is the normal days' usual largest 2-hour move on " \
           "15-minute closes)." in text and "4.2x its normal level" in text
    assert "On a normal day at that hour it moves" not in text   # a 15-minute yardstick is not what a normal day moves
    assert "ETH: no jump; largest move -0.41% in 2 h. Price moved first (a jump in the hour before publication)" in text
    assert "No normal day free to compare within a week." in text and "fast news" in text
    assert "[link removed]" in text and "\x07" not in text and "https://" not in text
    assert "The desk's news reading at the time: -0.6 (bearish)" in text
    btc = events_text(j, s, "btc")
    assert "BTC fell" in btc and "ETH:" not in btc
    assert "Usage" in events_text(j, s, "xyz") and "Usage" in events_text(j, s, "a;b")
    assert "US CPI" in events_text(j, s, "macro") and "jumps (*) in 1 of 1" in events_text(j, s, "macro")
    assert "no verdict yet (1 of 30 events)" in text
    agg = {"n": 30, "jumps": 27, "n_p": 30, "p_jumps": 6, "jump_rate": 0.9, "placebo_rate": 0.2, "propagation_min": 1,
           "peak_min": 93}
    assert verdict(agg).startswith("Prices jump more around these events than at the same hour on a normal day (90% vs 20%)")
    assert "sees them only afterwards; the extreme came after a median 93 min" in verdict(agg)
    close = {**agg, "jumps": 5, "p_jumps": 4, "jump_rate": 5 / 30, "placebo_rate": 4 / 30, "propagation_min": 25}
    assert verdict(close).startswith("no clear difference from a normal hour") and "had at least one look" in verdict(close)
    assert verdict({**agg, "n": 12}) == "no verdict yet (12 of 30 events)"
    seed(j, symbol="SOLUSDT", placebo_ts=None, placebo_impulse=None, placebo_magnitude_pct=None)
    assert "2 coin-events from 1 release (1 with a normal-day match)" in events_text(j, s)
    long_label = "Bitcoin " + "x" * 160
    for i in range(6):
        seed(j, kind="headline", label=long_label, category="other", ts_utc=f"2026-10-2{i}T10:00:00+00:00", n_headlines=1)
    many = events_text(j, s)
    assert len(many) < 4000 and "Requests today: 0 of 60" in many and "EVENTS.md" in many
    assert "The event study is switched off" in events_text(j, Settings(events_daily_requests=0))
    assert events_text(j, Settings(strategy="trend")) == EVENTS_TREND


def test_daily_report_line_and_events_md(tmp_path):
    a = make_agent(tmp_path)
    assert events_line(a.j, "2026-10-15") == ""
    seed(a.j)
    seed(a.j, symbol="ETHUSDT", magnitude_pct=-3.54, placebo_impulse=1)
    line = events_line(a.j, "2026-10-15")
    assert line.startswith("📰 Events: 2 measured yesterday (US CPI Wed 14 Oct 12:30 UTC: BTC -2.3%, ETH -3.5% within 2 h; jumps "
                           "in 2 of 2, on the matched normal days 1 of 2)") and "2 on record · /events" in line
    assert "nothing new measured yesterday" in events_line(a.j, "2026-10-20")
    b = make_agent(tmp_path)
    seed(b.j, kind="headline", category="other", impulse=0)
    assert "1 headline burst measured yesterday, jumps in 0" in events_line(b.j, "2026-10-15")
    notes = Notes(a.j, tmp_path / "n", a.s)
    assert notes.refresh() is not None
    md = (tmp_path / "n" / "EVENTS.md").read_text()
    assert "## By category" in md and "K, floor" in md and "3 on weekdays, 4.5 at weekends" in md
    assert "| macro:cpi | 2 |" in md and "## Releases (newest first)" in md and "9.5%" in md
    assert "EVENTS.md" in (tmp_path / "n" / "README.md").read_text()
    assert "daily trend rule does not run it" in events_md(a.j, Settings(strategy="trend"))


# ---- 19-21: nothing changes a trade; offline and trend modes; the setting --------------------------------------
# wall-clock times and the paper broker's random order ids differ between any two runs; everything else must match
TRADING_TABLES = {"decisions": "ts", "orders": "ts, order_id", "positions": "opened_at, protection",
                  "closed_trades": "opened_at, closed_at"}


def trading_record(bot):
    out = {}
    for table, skip in TRADING_TABLES.items():
        cols = [r[1] for r in bot.journal.db.execute(f"PRAGMA table_info({table})")
                if r[1] not in {c.strip() for c in skip.split(",")}]
        out[table] = [tuple(r) for r in bot.journal.db.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY rowid")]
    return out


def test_the_event_study_changes_no_decision_order_or_pace(tmp_path):
    desks = []
    for budget in (60, 0):
        s = Settings(offline=True, events_daily_requests=budget)
        bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
        bot.events.client, bot.events.owner = FakeClient(jumps={}), None
        now = time.time()
        bot.news.last_items = [item("Bitcoin rallies", ev.iso(int((now - 6 * 3600) * 1000))),
                               item("Ether upgrade ships", ev.iso(int((now - 5 * 3600) * 1000)))]
        bot.news.last_fetch = now
        paces = []
        for _ in range(40):
            summary = bot.run_cycle()
            summary += bot.events_step()
            paces.append(dict(bot.pace))
        desks.append((bot, paces))
    (on, on_pace), (off, off_pace) = desks
    assert trading_record(on) == trading_record(off) and on_pace == off_pace
    assert on.journal.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NOT NULL").fetchone()[0] >= 2
    assert off.journal.db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0] == 0
    assert off.journal.db.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 0 and off.events.client.calls == []


def test_offline_and_trend_modes(tmp_path):
    s = Settings(offline=True)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.events.owner = None
    assert bot.events.client is None
    bot.run_cycle()
    assert bot.events_step() == []
    assert bot.journal.db.execute("SELECT COUNT(*) FROM news_events").fetchone()[0] == 0   # nothing network-bound
    # the daily trend rule never settles (or schedules, or downloads) anything, even with complete events on record
    t = Settings(offline=True, strategy="trend", symbols=["BTCUSDT"])
    trend_bot = Orchestrator(t, Journal(":memory:", t.starting_balance))
    trend_bot.events.client, trend_bot.events.owner = FakeClient(), None
    trend_bot.journal.db.execute("INSERT INTO news_events (kind, ts_utc, symbol, label, category) VALUES "
                                 "('macro', '2026-10-14T12:30:00+00:00', 'BTCUSDT', 'US CPI', 'macro:cpi')")
    for _ in range(3):
        trend_bot.run_cycle()
        assert trend_bot.events_step() == []
    assert trend_bot.events.client.calls == []
    assert trend_bot.journal.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NOT NULL").fetchone()[0] == 0
    assert trend_bot.events.step(when("2026-10-31T00:00:00Z"), ["BTCUSDT"], [], None, None) == []
    assert trend_bot.events.client.calls == []
    assert "/events" in HELP_TEXT and "/events" not in help_text(t) and "/events" in help_text(s)


def test_settings_key_and_problems(monkeypatch):
    assert Settings().events_daily_requests == 60
    monkeypatch.setenv("EVENTS_DAILY_REQUESTS", "-1")
    s = Settings.load()
    assert s.events_daily_requests == -1 and any("EVENTS_DAILY_REQUESTS" in p for p in s.problems())
    assert EventsAgent(Journal(":memory:", 1), None, s).enabled is False
    monkeypatch.setenv("EVENTS_DAILY_REQUESTS", "0")
    off = Settings.load()
    a = EventsAgent(Journal(":memory:", 1), FakeClient(), off, owner_calendar=Path("/nonexistent/x.json"))
    assert a.step(when("2026-10-31T00:00:00Z"), ["BTCUSDT"], [item("Bitcoin x", "2026-10-30T10:00:00Z")], None,
                  time.time()) == []
    assert a.j.db.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 0 and a.client.calls == []
    monkeypatch.setenv("EVENTS_DAILY_REQUESTS", "90")
    assert Settings.load().events_daily_requests == 90
    assert "EVENTS_DAILY_REQUESTS=" in (Path(__file__).parent.parent / ".env.example").read_text()


def test_a_release_measured_for_every_coin_writes_one_diary_line(tmp_path):
    rel = "2026-10-14T12:30:00Z"
    a = make_agent(tmp_path, FakeClient(jumps={("BTCUSDT", ms(rel)): {1: -1.0, 30: -2.3}}),
                   events=[{"ts_utc": rel, "name": "US CPI, September 2026 data", "kind": "cpi"}])
    a.schedule_macro(when("2026-10-14T18:00:00Z"), ["BTCUSDT", "ETHUSDT"])
    a.settle(when("2026-10-14T18:00:00Z"))
    lines = [e["text"] for e in a.j.events() if e["kind"] == "events" and "measured for" in e["text"]]
    assert lines == ["US CPI, September 2026 data (Wed 14 Oct 12:30 UTC) measured for 2 coins: a jump in 1 (BTC -2.3% "
                     "within 30 min); on the matched normal days 0 of 2 jumped"]


def test_one_settle_step_is_cheap():
    """The cost the README states: one coin-event (two 600-candle windows, one 1,000-candle baseline) measures in
    well under a second of plain Python, and the step keeps nothing but the record."""
    series, candles, placebo = baseline_series(T), window(T, {1: 2.0}, noise=0.02), window(T - DAY, noise=0.02)
    started = time.perf_counter()
    for _ in range(5):
        ev.measure(candles, T, *ev.baselines(series, T, set()))
        ev.measure(placebo, T - DAY, *ev.baselines(series, T - DAY, set()))
    assert (time.perf_counter() - started) / 5 < 1.0


# ---- review fixes of 1.8.1: each test names the finding it guards against -------------------------------------
def fetch_row(a, event_id):
    return a.j.db.execute("SELECT * FROM news_events WHERE id=?", (event_id,)).fetchone()


def shipped_agent(tmp_path, client, clock):
    """An agent on the real shipped calendar (releases, US market holidays)."""
    s = Settings(offline=True, events_daily_requests=60)
    a = EventsAgent(Journal(":memory:", 10_000), client, s, shipped_calendar=ev.SHIPPED_CALENDAR,
                    owner_calendar=tmp_path / "owner.json", now_fn=Clock(clock))
    a.refresh_calendar(force=True)
    return a


def test_an_unreadable_publication_date_never_costs_the_news_agent_its_headlines(monkeypatch):
    """correctness:0 - the year 9999 parses, but cannot be converted to UTC (OverflowError): that one item loses its
    time, the news agent reads exactly the titles 1.8.0 read, and its keyword scores and cache work as before."""
    bad = "Fri, 31 Dec 9999 23:59:59 -0100"
    for odd in (bad, "Mon, 1 Jan 0001 00:00:00 +0100", "Thu, 99 Oct 2026 12:00:00 +0000", "Sat, 31 Oct 2026 25:61:61",
                "\x00", "31 Oct 2026", "Sat, 31 Oct 2026 12:00:00 +9999"):
        out = news_module.publication_time(odd)
        assert out is None or isinstance(out, str)
    assert news_module.publication_time(bad) is None
    fake_feeds(monkeypatch, [rss([("Bitcoin ETF sees record inflows", "https://a.example/1", "Sat, 31 Oct 2026 12:00:00 +0000"),
                                  ("Bitcoin rally in the far future", "https://a.example/2", bad)]),
                             rss([("Ether upgrade ships", "https://b.example/1", "Sat, 31 Oct 2026 11:00:00 +0000")]), rss([])])
    expected = old_fetch_headlines()
    news = NewsAgent(offline=False)
    assert news.fetch_headlines() == expected == ["Bitcoin ETF sees record inflows", "Bitcoin rally in the far future",
                                                 "Ether upgrade ships"]
    assert [i["ts"] for i in news.last_items] == ["2026-10-31T12:00:00+00:00", None, "2026-10-31T11:00:00+00:00"]
    out = news.run(["BTC", "ETH"])
    assert out["BTC"]["score"] > 0 and out["_mood"] == "unknown (keyword mode)" and news._cache is not None


def test_a_coin_binance_refuses_is_written_off_at_once_and_a_failure_that_never_clears_after_a_day(tmp_path):
    """correctness:1, owner:1 - HTTP 400 'Invalid symbol.' (a coin name the public API does not know) gets the same
    answer every time: the record is written off with the reason after one request, without a pause, and the next
    event is measured in the same check. A temporary failure that never clears blocks the queue for a day at most."""
    client = FakeClient(refuse=["OLDUSDT"])
    a = make_agent(tmp_path, client, clock="2026-10-14T18:00:00Z")
    old = add_event(a, "2026-10-14T12:30:00Z", symbol="OLDUSDT")
    btc = add_event(a, "2026-10-14T12:31:00Z", symbol="BTCUSDT")
    now = when("2026-10-14T18:00:00Z")
    out = a.settle(now)
    assert out[0] == {"symbol": "EVENTS", "stage": "events", "settled": 1, "requests": 4, "used": 4, "budget": 60}
    assert out[1]["error"] == ("prices not available for OLDUSDT (HTTP 400: Invalid symbol.): that event is recorded as "
                               "not measured; check the coin names in SYMBOLS")
    r = row(a, old)
    assert r["settled_at"] and r["impulse"] is None and r["note"] == "prices not available for OLDUSDT: HTTP 400: Invalid symbol."
    assert "not measured (prices not available for OLDUSDT" in event_sentence(r)
    assert row(a, btc)["impulse"] is not None and a._retry_at == 0
    assert [c[0] for c in client.calls].count("OLDUSDT") == 1
    diary = [e["text"] for e in a.j.events() if e["kind"] == "events" and "measured for" in e["text"]]
    assert diary and diary[0].endswith("1 could not be measured")         # the release still gets its diary line
    # a rate limit that never clears: tried every 31 minutes, given up after a day, then the next event is measured
    c2 = FakeClient(fail=["ETHUSDT"])
    b = make_agent(tmp_path, c2, clock="2026-10-14T18:00:00Z")
    eth = add_event(b, "2026-10-14T12:30:00Z", symbol="ETHUSDT")
    sol = add_event(b, "2026-10-14T12:31:00Z", symbol="SOLUSDT")
    t = when("2026-10-14T18:00:00Z")
    b.settle(t)
    assert b.j.get("events_retry_at") == "2026-10-14T18:30:00+00:00" and b.j.get("events_failing") == {
        str(eth): ["2026-10-14T18:00:00+00:00", 1]}
    for i in range(1, 60):
        b.now_fn.t += 31 * 60
        b.settle(t + dt.timedelta(minutes=31 * i))
        if row(b, sol)["settled_at"]:
            break
    tries = [c[0] for c in c2.calls].count("ETHUSDT")
    assert row(b, eth)["note"] == "prices could not be downloaded for 24 hours: HTTP 429 too many requests"
    assert row(b, sol)["impulse"] is not None and 45 <= tries <= 48 and b.j.get("events_failing") == {}


def test_a_burst_in_the_hours_before_a_release_is_kept_out_of_the_headline_classes(tmp_path):
    """correctness:2 - a burst opened 25 minutes before a listed CPI has the release's move in its own window: it is
    measured and shown, but counted as inside the release window, also when a post-release headline joined it."""
    cpi = "2026-10-14T12:30:00Z"
    client = FakeClient(jumps={("BTCUSDT", ms("2026-10-14T12:05:00Z")): {26: 2.0},
                               ("ETHUSDT", ms("2026-10-14T12:10:00Z")): {21: 2.0}})
    a = make_agent(tmp_path, client, events=[{"ts_utc": cpi, "name": "US CPI", "kind": "cpi"}])
    ahead = add_event(a, "2026-10-14T12:05:00Z", kind="headline", n=1, label="Bitcoin steady ahead of US CPI inflation data",
                      category="macro")
    a.ingest([item("Ether traders brace for CPI inflation print", "2026-10-14T12:10:00Z"),
              item("Ether jumps as CPI comes in cooler than expected", "2026-10-14T12:34:00Z")], ["ETH"], None,
             ms("2026-10-14T13:00:00Z") / 1000)
    joined = a.j.db.execute("SELECT id, n_headlines, ts_utc FROM news_events WHERE symbol='ETHUSDT'").fetchone()
    assert joined["n_headlines"] == 2 and joined["ts_utc"] == "2026-10-14T12:10:00+00:00"
    for event_id in (ahead, joined["id"]):
        a.settle_one(fetch_row(a, event_id), when("2026-10-14T18:00:00Z"))
    r = row(a, ahead)
    assert r["impulse"] == 1 and r["propagation_min"] == 26                 # the CPI minute, inside the burst's window
    assert "within the US CPI window (the release came 25 min after the first headline)" in (r["note"] or "")
    assert "within the US CPI window (the release came 20 min after the first headline)" in (row(a, joined["id"])["note"] or "")
    seed(a.j, kind="headline", category="macro", ts_utc="2026-10-10T10:00:00+00:00", impulse=0)
    agg = [x for x in category_table(a.j) if x["class"] == "headline/macro"][0]
    assert agg["n"] == 1 and agg["jumps"] == 0 and agg["inside"] == 2


def test_a_placebo_day_the_download_cannot_judge_is_never_chosen_or_shown_as_a_normal_move(tmp_path):
    """correctness:3, owner:0 - the CPI of Tue 10 Nov 2026 on the shipped calendar: for every other coin the
    second-nearest free day is Wed 4 Nov, which has only 2 normal days before it in the event's download. It is
    skipped before any download, so Mon 9 Nov is used; a day that still cannot be measured is never shown as +0.00%."""
    cpi, wed, mon = ms("2026-11-10T13:30:00Z"), ms("2026-11-04T13:30:00Z"), ms("2026-11-09T13:30:00Z")
    client = FakeClient(jumps={(sym, wed): {1: 2.0} for sym in ("BTCUSDT", "ETHUSDT")}
                        | {(sym, mon): {1: 0.4} for sym in ("BTCUSDT", "ETHUSDT")})
    a = shipped_agent(tmp_path, client, "2026-11-10T18:00:00Z")
    a.schedule_macro(when("2026-11-10T18:00:00Z"), ["BTCUSDT", "ETHUSDT"])
    ids = [r[0] for r in a.j.db.execute("SELECT id FROM news_events WHERE ts_utc=?", (ev.iso(cpi),))]
    odd = [i for i in ids if i % 2 == 1][0]
    assert ev.iso(a.placebo_time(fetch_row(a, odd), when("2026-11-10T18:00:00Z"))) == "2026-11-04T13:30:00+00:00"
    calls = len(client.calls)
    a.settle_one(fetch_row(a, odd), when("2026-11-10T18:00:00Z"))
    r = row(a, odd)
    assert len(client.calls) - calls == 3 and r["placebo_ts"] == "2026-11-09T13:30:00+00:00" and r["note"] is None
    assert r["placebo_impulse"] == 0 and r["placebo_magnitude_pct"] == pytest.approx(0.4)
    text = event_sentence(r)
    assert "Same hour on Mon 9 Nov (a normal day): +0.40%, no jump." in text and "Wed 4 Nov" not in text
    # a headline burst whose only free day is a week back: no third download, and the record says why
    b = make_agent(tmp_path, client)
    burst = add_event(b, "2026-10-14T16:00:00Z", kind="headline", n=2, label="Bitcoin story", category="other")
    for day in ("2026-10-13", "2026-10-12", "2026-10-09", "2026-10-08"):
        add_event(b, day + "T16:30:00Z", kind="headline", n=1, label="Bitcoin other", category="other")
    calls = len(client.calls)
    b.settle_one(fetch_row(b, burst), when("2026-10-14T21:00:00Z"))
    r = row(b, burst)
    assert len(client.calls) - calls == 2 and r["placebo_ts"] is None and r["impulse"] is not None
    assert r["note"].startswith("no placebo day within 7 days could be judged")
    assert "the free days within a week have too few normal days before them to be judged" in event_sentence(r)
    # a matched day that was downloaded but could not be measured (a gap in Binance's candles): no figure shown
    c = make_agent(tmp_path)
    seed(c.j, placebo_impulse=None, placebo_magnitude_pct=None, note="placebo window: incomplete candles (400 of 600)")
    text = event_sentence(_rows_of(c)[0])
    assert "Same hour on Tue 13 Oct (a normal day): could not be measured (incomplete candles (400 of 600))." in text
    assert "+0.00%" not in text and "no jump" not in text


def _rows_of(a):
    return [dict(r) for r in a.j.db.execute("SELECT * FROM news_events ORDER BY id")]


def test_a_headline_29_minutes_30_seconds_after_the_first_joins_its_burst_in_either_order(tmp_path):
    """correctness:4 - bursts are stored at their first headline's minute; the search must start from the new
    headline's minute too, or a headline at 10:30:20 would open a second event beside the one opened at 10:00:50."""
    pair = [item("Bitcoin first", "2026-10-31T10:00:50Z"), item("Bitcoin second", "2026-10-31T10:30:20Z")]
    for order in ((0, 1), (1, 0)):
        a = make_agent(tmp_path)
        for i, k in enumerate(order):
            a.ingest([pair[k]], ["BTC"], None, ms("2026-10-31T11:00:00Z") / 1000 + i)
        assert [(r["ts_utc"][11:16], r["n_headlines"]) for r in a.j.db.execute("SELECT * FROM news_events")] == [("10:00", 2)]
    b = make_agent(tmp_path)
    b.ingest([pair[0]], ["BTC"], None, ms("2026-10-31T11:00:00Z") / 1000)
    b.ingest([item("Bitcoin late", "2026-10-31T10:31:00Z")], ["BTC"], None, ms("2026-10-31T11:00:00Z") / 1000 + 1)
    assert [r["n_headlines"] for r in b.j.db.execute("SELECT * FROM news_events ORDER BY ts_utc")] == [1, 1]  # 30 min 10 s


def test_claudes_review_digest_leaves_the_event_study_out(tmp_path):
    """correctness:5 - the weekly review's summary reaches the decision prompt through the playbook, so the study's
    diary lines (release lines, calendar warnings) never go into the digest Claude reads; other diary lines do."""
    rel = "2026-10-14T12:30:00Z"
    a = make_agent(tmp_path, FakeClient(jumps={("BTCUSDT", ms(rel)): {1: -2.3}}),
                   events=[{"ts_utc": rel, "name": "US CPI, September 2026 data", "kind": "cpi"}])
    a.schedule_macro(when("2026-10-14T18:00:00Z"), ["BTCUSDT"])
    a.settle(when("2026-10-14T18:00:00Z"))
    a.j.event("lab", "weekly lab: the champion stays")
    assert any(e["kind"] == "events" and "measured for" in e["text"] for e in a.j.events())
    digest = Notes(a.j, tmp_path / "n", a.s).digest()
    assert "lab: weekly lab: the champion stays" in digest
    assert "measured for" not in digest and "US CPI" not in digest


def test_the_hour_before_is_judged_on_the_events_own_normal_days_also_just_after_midnight():
    """correctness:6 - for a Monday 00:30 UTC event the hour before is Sunday evening; it is compared with the hour
    before 00:30 on the same normal weekdays the 2-hour baseline uses (with the weekday K), never with weekend hours,
    and a Saturday 00:30 event's with weekend days only."""
    monday = ms("2026-10-19T00:30:00Z")
    main, before = ev.baselines(baseline_series(monday), monday, set())
    assert before["dates"] == main["dates"] and len(main["dates"]) >= 3
    assert all(dt.date.fromisoformat(d).weekday() < 5 for d in before["dates"])
    saturday = ms("2026-10-24T00:30:00Z")
    main, before = ev.baselines(baseline_series(saturday, 30), saturday, set(), 30)
    assert before["dates"] == main["dates"] and all(dt.date.fromisoformat(d).weekday() >= 5 for d in before["dates"])
    main, before = ev.baselines(baseline_series(T), T, set())               # at 08:30 New York nothing changes
    assert before["dates"] == main["dates"] and before["b"][ev.QUIET_MIN] == 0.0 and len(before["dates"]) == 7


def test_without_a_time_zone_database_the_claims_thursday_is_still_skipped_in_winter(monkeypatch, tmp_path):
    """correctness:7 - with no tz database the New York hour follows the US summer-time rule (UTC-4 from the second
    Sunday of March, UTC-5 from the first Sunday of November), so a winter 08:30 release never gets a Thursday."""
    real = ev._new_york()
    monkeypatch.setattr(ev, "_new_york", lambda: None)
    assert ev.new_york_hour(ms("2026-12-04T13:30:00Z")) == 8 and ev.new_york_hour(ms("2026-10-02T12:30:00Z")) == 8
    assert [ev.new_york_hour(ms(t)) for t in ("2026-03-08T06:59:00Z", "2026-03-08T07:00:00Z", "2026-11-01T05:59:00Z",
                                                "2026-11-01T06:00:00Z")] == [1, 3, 1, 1]
    if real is not None:   # every hour of two years agrees with the time-zone database
        start = ms("2026-01-01T00:00:00Z")
        for h in range(0, 2 * 365 * 24):
            t = start + h * 3_600_000
            assert ev.new_york_hour(t) == dt.datetime.fromtimestamp(t / 1000, real).hour, ev.iso(t)
    a = make_agent(tmp_path)
    jobs = [add_event(a, "2026-12-04T13:30:00Z", symbol=s) for s in ("BTCUSDT", "ETHUSDT")]   # Friday 08:30 EST
    days = {when(ev.iso(a.placebo_time(row(a, i), when("2026-12-04T18:00:00Z")))).weekday() for i in jobs}
    assert days == {1, 2}                       # Tue 1 and Wed 2 Dec, never Thu 3 Dec (weekly jobless claims)


def test_the_jump_bar_is_shown_as_the_rules_yardstick_not_as_what_a_normal_day_moves(tmp_path):
    """owner:2 - the baseline comes from 15-minute closes (30 at weekends), which read lower than the 1-minute moves
    printed next to it: the sentence names it as the bar a jump had to clear, never as 'what a normal day moves'."""
    a = make_agent(tmp_path)
    seed(a.j)
    seed(a.j, ts_utc="2026-10-17T12:30:00+00:00", symbol="ETHUSDT")                 # a Saturday
    seed(a.j, ts_utc="2026-10-15T12:30:00+00:00", symbol="SOLUSDT", baseline_pct=0.05)
    texts = {r["symbol"]: event_sentence(r) for r in _rows_of(a)}
    assert ("A jump at that hour needed 1.80% (3 x 0.60%; 0.60% is the normal days' usual largest 2-hour move on "
            "15-minute closes).") in texts["BTCUSDT"]
    assert "needed 2.70% (4.5 x 0.60%; 0.60% is the normal days' usual largest 2-hour move on 30-minute closes)" in texts["ETHUSDT"]
    assert "needed 0.30% (the 0.3% floor, above 3 x 0.05%;" in texts["SOLUSDT"]
    assert not [t for t in texts.values() if "it moves" in t]
    md = events_md(a.j, a.s)
    assert "rule baseline (15-min closes)" in md and "is the rule's yardstick, not what a normal day moves" in md


def test_back_filled_releases_carry_their_dates_in_the_daily_line_and_the_diary(tmp_path):
    """owner:3 - two CPI releases back-filled at an install and measured the same day: the daily line names the
    newest with its date and how many releases the coin-events come from, and each diary line has its own date."""
    cal = [{"ts_utc": "2026-09-11T12:30:00Z", "name": "US CPI, August 2026 data", "kind": "cpi"},
           {"ts_utc": "2026-10-14T12:30:00Z", "name": "US CPI, September 2026 data", "kind": "cpi"}]
    a = make_agent(tmp_path, FakeClient(), cal, clock="2026-10-30T10:00:00Z")
    now = when("2026-10-30T10:00:00Z")
    assert a.schedule_macro(now, ["BTCUSDT"]) == 2
    a.settle(now)
    diary = [e["text"] for e in a.j.events() if e["kind"] == "events" and "measured for" in e["text"]]
    assert [d.split(": a jump")[0] for d in diary] == [
        "US CPI, August 2026 data (Fri 11 Sep 12:30 UTC) measured for 1 coin",
        "US CPI, September 2026 data (Wed 14 Oct 12:30 UTC) measured for 1 coin"]
    line = events_line(a.j, "2026-10-31")
    assert line.startswith("📰 Events: 2 measured yesterday (2 coin-events of 2 releases, the newest US CPI Wed 14 Oct "
                           "12:30 UTC: BTC +0.0% within 2 h; jumps in 0 of 1")


def test_moved_first_give_back_and_volume_stand_next_to_the_matched_normal_days(tmp_path):
    """owner:4 - the matched normal day's own flags are stored when it is measured, and every share in the class
    line stands next to the normal-day share (or, for 'moved first', the calibration's 1 normal hour in 10)."""
    rel = "2026-10-14T12:30:00Z"
    client = FakeClient(jumps={("BTCUSDT", ms(rel)): {1: 2.0}})
    a = make_agent(tmp_path, client)
    e = add_event(a, rel)
    nearest = "2026-10-13" if e % 2 == 0 else "2026-10-12"
    client.jumps[("BTCUSDT", ms(nearest + "T12:30:00Z"))] = {-20: 1.5, 1: 4.0, 40: 2.0}   # moved first, jump, give-back
    a.settle_one(fetch_row(a, e), when("2026-10-14T18:00:00Z"))
    r = row(a, e)
    assert r["placebo_ts"].startswith(nearest) and r["placebo_impulse"] == 1
    assert (r["placebo_moved_first"], r["placebo_retraced"], r["placebo_volume_exhausted"]) == (1, 1, 0)
    j = make_agent(tmp_path).j
    for i in range(40):
        seed(j, kind="headline", category="hack-security", ts_utc=f"2026-09-{1 + i % 28:02d}T{i % 24:02d}:00:00+00:00",
             impulse=int(i < 4), moved_first=int(i % 10 == 0), placebo_moved_first=int(i % 10 == 5),
             placebo_impulse=int(i == 9), placebo_retraced=1 if i == 9 else None, correction_delay_min=12 if i < 2 else None)
    for i in range(3):
        seed(j, kind="headline", category="regulation", ts_utc=f"2026-09-0{i + 1}T10:00:00+00:00", moved_first=0)
    seed(j, category="macro:fomc", label="FOMC statement", ts_utc="2026-10-28T18:00:00+00:00", volume_exhausted=1,
         placebo_impulse=1, placebo_volume_exhausted=0, placebo_retraced=1)
    lines = {x["class"]: category_sentence(x) for x in category_table(j)}
    hack = lines["headline/hack-security"]
    assert "price moved first in 4 of 40 (normal hours: 4 of 40)" in hack
    assert "2 of 4 jumps gave back 38% inside the window, a median 12 min after the extreme (jumps on the matched " \
           "normal days: 1 of 1)" in hack
    assert ("price moved first in 0 of 3 (about 1 normal hour in 10 also shows this: 9% in the calibration)"
            in lines["headline/regulation"])
    assert "(no jump on the matched normal days to compare yet)" in lines["headline/regulation"] or \
        "gave back" not in lines["headline/regulation"]
    fomc = lines["macro:fomc"]
    assert ("volume dried up after 1 of 1 jumps (jumps on the matched normal days: 0 of 1); usual for a 14:00 New York "
            "release: the US session closes inside the window") in fomc
    assert "1 coin-event from 1 release" in fomc
    md = events_md(j, Settings(offline=True))
    assert "| 4 of 40 (normal hours: 4 of 40) |" in md


def test_calendar_warnings_and_the_rarer_verdict_and_text_branches(tmp_path):
    """owner:6 - branches no test reached: each calendar warning is written once (also after another refresh), the
    'jump less' and 'no normal-day matches' verdicts, and the FOMC volume caveat on one event."""
    a = make_agent(tmp_path, events=[{"ts_utc": "2026-11-30T13:30:00Z", "name": "US CPI", "kind": "cpi"}])
    a.refresh_calendar(force=True)
    warned = [e["text"] for e in a.j.events() if e["text"].startswith("Release calendar:")]
    assert warned == ["Release calendar: the release calendar runs out on 2026-11-30: a code update refreshes it, or "
                      "add dates to data/macro_calendar.json"]
    b = make_agent(tmp_path, events=[{"ts_utc": "2026-10-02T12:30:00Z", "name": "US jobs", "kind": "nfp"},
                                     {"ts_utc": "2026-11-10T13:30:00Z", "name": "US CPI", "kind": "cpi"},
                                     {"ts_utc": "2027-06-10T12:30:00Z", "name": "US CPI", "kind": "cpi"}])
    b.refresh_calendar(force=True)
    warned = [e["text"] for e in b.j.events() if e["text"].startswith("Release calendar:")]
    assert warned == ["Release calendar: no US jobs report date in the next 70 days although the calendar runs further: "
                      "check the official schedule and add it to data/macro_calendar.json"]
    agg = {"n": 40, "jumps": 2, "n_p": 40, "p_jumps": 16, "jump_rate": 0.05, "placebo_rate": 0.4, "propagation_min": None,
           "peak_min": None}
    assert verdict(agg) == "Prices jump less around these events than at the same hour on a normal day (5% vs 40%)"
    assert verdict({**agg, "n": 30, "n_p": 0}) == "no verdict: no normal-day matches to compare with"
    seed(a.j, category="macro:fomc", label="FOMC statement", ts_utc="2026-10-28T18:00:00+00:00", volume_exhausted=1)
    text = event_sentence(_rows_of(a)[0])
    assert ("trading volume fell back to its pre-event level (usual for a 14:00 New York release: the US session closes "
            "inside the window)") in text


def test_a_budget_below_one_measured_event_is_reported_and_switches_the_study_off(tmp_path):
    """owner:7 - with EVENTS_DAILY_REQUESTS of 1 or 2 nothing could ever be measured (one event takes 3 downloads):
    the settings check says so, the study is off, and /events never promises 'measured tomorrow'."""
    for n in (1, 2):
        s = Settings(offline=True, events_daily_requests=n)
        assert [p for p in s.problems() if "EVENTS" in p] == [
            f"EVENTS_DAILY_REQUESTS={n} is below one measured event (3 downloads), so nothing could ever be measured: "
            "the event study is off; use 0 (off) or at least 3"]
        a = EventsAgent(Journal(":memory:", 1), FakeClient(), s, owner_calendar=tmp_path / "none.json")
        assert a.enabled is False
        assert a.step(when("2026-10-31T00:00:00Z"), ["BTCUSDT"], [item("Bitcoin x", "2026-10-30T10:00:00Z")], None,
                      time.time()) == [] and a.client.calls == []
        text = events_text(a.j, s)
        assert "below one measured event" in text and "measured tomorrow" not in text
    ok = Settings(offline=True, events_daily_requests=3)
    assert not [p for p in ok.problems() if "EVENTS" in p] and EventsAgent(Journal(":memory:", 1), None, ok).enabled


def test_the_footer_tells_complete_queued_events_from_open_windows(tmp_path):
    """owner:8 - releases back-filled weeks ago are complete and wait for the per-check limit, not for their window;
    a pause after a failed download is named with its end."""
    a = make_agent(tmp_path)
    now_ms = int(time.time() * 1000)
    for d in (20, 21):
        add_event(a, ev.iso(now_ms - d * DAY))
    add_event(a, ev.iso(now_ms - 60 * MIN), kind="headline", n=1, label="Bitcoin", category="other")
    text = events_text(a.j, a.s)
    assert "2 events complete and queued for measurement (2 per check, up to 20 a day)" in text
    assert "1 event waits for its window to complete" in text and "3 events wait" not in text
    a.j.set("events_retry_at", ev.iso(now_ms + 20 * MIN))
    assert f"downloads paused after a failure until {ev.iso(now_ms + 20 * MIN)[11:16]} UTC" in events_text(a.j, a.s)
    assert "events waiting for their window: 1 · complete, queued for measurement: 2" in events_md(a.j, a.s)


def test_a_pause_after_a_failed_download_survives_a_restart(tmp_path):
    """The /events footer reads the pause from the journal; a restarted agent must honour the same pause."""
    agent = make_agent(tmp_path)
    now = agent.now_fn()
    agent.j.set("events_retry_at", ev.iso(int((now + 900) * 1000)))
    again = EventsAgent(agent.j, agent.client, agent.s, now_fn=agent.now_fn)
    assert abs(again._retry_at - (now + 900)) < 1
    agent.j.set("events_retry_at", "not a time")
    assert EventsAgent(agent.j, agent.client, agent.s, now_fn=agent.now_fn)._retry_at == 0.0
