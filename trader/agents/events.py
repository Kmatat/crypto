"""Event Synthesis agent: what prices actually did around news and scheduled releases.

It keeps every headline with its publication time, groups headlines about a coin into bursts (30 minutes from
the first), and for each burst and each scheduled US release (CPI, FOMC statement, jobs report) measures, after
the fact, what the coin's price did: the largest move within two hours and whether it counts as a jump, how long
the jump took to register, the extreme, the 38% give-back, whether trading volume dried up, and whether the price
had already moved in the hour before. Every event is compared with the same hour on a normal day (a "placebo"
window). A headline's time is its publication time, which often trails the real trigger: that is why the hour
before publication is checked ("price moved first").

This is a measurement, not a forecast, and nothing in it feeds a trade: no threshold, no pace, no risk rule and
no AI prompt reads these records. The owner reads them with /events, in one line of the daily report and in the
notebook page EVENTS.md. It runs with the hourly agents only; the daily trend rule never runs it.

How a jump is judged (the "normal-day baseline"). The move after an event is compared with what the same coin
usually does at the same clock hour: for each of the last 10 normal days of the same kind (weekdays for a weekday
event, weekend days for a weekend event; US market holidays and days with a listed release are skipped) the largest
move of the 15-minute closes (30-minute for a weekend event) in the two hours starting at the same New York clock
time is measured, and the median of those days is the baseline. A jump is a move of the one-minute closes within
two hours of the event larger than K times that baseline (and at least FLOOR_PCT). The baseline is time-of-day
aware on purpose: a baseline taken from the hours before an 08:30 New York release is the quiet overnight session,
while the two hours after it contain the US stock-market open, so such a baseline called 45% of normal mornings
"jumps" in the study that preceded this release (plan 1.8, macro study). The "moved first" hour is judged the same
way against the normal days' hour before. The placebo window is judged against ITS OWN preceding normal days, so its
own day is never part of the figure it is compared with. K was set on real prices so that about one normal window
in ten counts as a jump (README, "What prices do around news and scheduled releases").

Costs: three downloads per measured coin-event (600 one-minute candles around the event, 1,000 fifteen- or
thirty-minute candles for the baseline, 600 one-minute candles for the placebo window), at most SETTLE_PER_CHECK
coin-events per check, under a daily budget (EVENTS_DAILY_REQUESTS). The candles are never stored: only the derived
record.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sqlite3
import statistics
import time
from collections import Counter
from pathlib import Path

import requests

from ..binance_client import BinanceClient, BinanceError
from ..journal import Journal
from ..notify import clean
from .news import NEGATIVE, POSITIVE, NewsAgent, hits

log = logging.getLogger(__name__)

MIN_MS = 60_000
DAY_MS = 86_400_000
# the windows around an event, in minutes: noise window [-360, -60), the "moved first" hour [-60, 0), the owner's
# pre-event window [-30, 0), the impulse window [0, 120), the correction window [peak, 240)
PRE_MIN, QUIET_MIN, PRE_VOLUME_MIN, IMPULSE_MIN, POST_MIN = 360, 60, 30, 120, 240
# the impulse rule: a move of the 1-minute closes within 2 hours of more than K times the normal-day baseline, and at
# least FLOOR_PCT. K is set per kind of day from placebo windows of real prices (build notes: plan 1.8,
# build_1_8_1/calibration_final.txt): on weekdays K = 3.0 flags 9.5% of the 189 normal windows of the 63-release study
# (08:30 and 14:00 New York) and 7.5% of every weekday hour of 62 days; weekend prices sit still for hours and then
# move at once, so a weekend window needs a larger K for the same rate: 4.5 flags 8.4% of the weekend hours
K_WEEKDAY, K_WEEKEND, FLOOR_PCT = 3.0, 4.5, 0.3
K_IMPULSE = K_WEEKDAY                          # the weekday K (the release hours are weekdays)
RETRACE, EXHAUST_MINUTES = 0.382, 10          # "retrace": the give-back fraction (a parameter, no claim beyond the number)
# the normal-day baseline: 1,000 coarser candles ending at the event (15-minute ones on weekdays: 10.4 days back,
# 30-minute ones for a weekend event: 20.8 days, so that enough weekend days exist), up to 10 normal days of the
# same weekday class (Mon-Fri or Sat-Sun), at least 3 (usually 6 or 7 on weekdays, 5 on weekends)
BASELINE_STEP_MIN, WEEKEND_STEP_MIN, BASELINE_CANDLES, BASELINE_DAYS, MIN_BASELINE_DAYS = 15, 30, 1000, 10, 3
BASELINE_LOOKBACK_DAYS = 25                   # how far back the same-clock-hour search may walk (the series bounds it)
BURST_MINUTES, PLACEBO_DAYS, PLACEBO_CLEAR_MIN = 30, 7, 120
SETTLE_PER_CHECK, REQUESTS_PER_EVENT = 2, 3
WAIT_DAYS, KEEP_DAYS, KEEP_EVENT_DAYS, BACKFILL_DAYS = 7, 180, 400, 60
MAX_HEADLINE_AGE_DAYS = 3                     # an item published longer ago opens no event (feeds re-publish old pieces)
CLAIMS_WEEKDAY = 3                            # Thursday: weekly jobless claims at 08:30 New York, never a placebo day then
RETRY_MINUTES = 30                            # after a failed download the step pauses this long
GIVE_UP_HOURS, GIVE_UP_TRIES = 24, 6          # a record whose download keeps failing this long (and this often)
                                              # is written off, so it cannot hold up the queue for ever
MIN_WINDOW_CANDLES = 540                      # of the 600 one-minute candles around an event
SHIPPED_CALENDAR = Path(__file__).resolve().parent.parent / "macro_calendar.json"
RELEASE_NAMES = {"cpi": "US CPI", "nfp": "US jobs report", "fomc": "FOMC statement"}

# headline categories: the first group with a matching word wins (a hack is a hack even when it also talks about
# the price). Words match at the start of a word, like the news agent's keyword lists.
CATEGORIES: list[tuple[str, list[str]]] = [
    ("hack-security", ["hack", "exploit", "breach", "drained", "stolen", "vulnerability", "scam", "fraud", "rug pull"]),
    ("regulation", ["sec", "cftc", "regulator", "regulation", "lawsuit", "court", "charges", "fine", "ban", "license",
                    "mica", "senate", "congress", "bill", "law", "treasury"]),
    ("etf-flows", ["etf", "inflow", "outflow", "blackrock", "fidelity", "grayscale", "fund"]),
    ("macro", ["fed", "fomc", "rate cut", "rate hike", "inflation", "cpi", "jobs report", "payrolls", "tariff",
               "yields", "dollar", "powell"]),
    ("tech-upgrade", ["upgrade", "mainnet", "hard fork", "testnet", "protocol", "launch", "rollup", "fork"]),
    ("exchange-listing", ["lists", "listing", "delist", "binance", "coinbase", "kraken", "exchange", "trading pair"]),
    ("adoption-corporate", ["partnership", "adopt", "treasury buys", "acquires", "payment", "integration", "holdings",
                            "strategy", "reserve"]),
    ("price-talk", ["price", "rally", "surge", "plunge", "drops", "falls", "bull", "bear", "analyst", "prediction",
                    "support", "resistance", "liquidation", "all-time high"]),
]
_CATEGORY_PATTERNS = [(name, [re.compile(r"\b" + re.escape(w) + (r"\b" if w in ("sec", "ban", "fine", "law", "bill", "fund",
                                                                                 "fed", "fork", "lists") else ""))
                              for w in words]) for name, words in CATEGORIES]


# ---- time helpers --------------------------------------------------------------------------------------------
def _new_york():
    """The New York time zone, or None when the machine has no time-zone database (then day arithmetic stays in UTC
    and the New York clock hour follows the US summer-time rule written out in _new_york_offset_hours)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - no tz database: the caller falls back to plain UTC arithmetic
        return None


def same_clock_days_ago(t_ms: int, days: int) -> int:
    """The same New York wall-clock time `days` days earlier (negative: later). Across a summer-time switch the UTC
    hour changes by one, so '08:30 New York' stays '08:30 New York'; without a tz database it is the same UTC time."""
    tz = _new_york()
    if tz is None:
        return t_ms - days * DAY_MS
    local = dt.datetime.fromtimestamp(t_ms / 1000, tz)
    earlier = local - dt.timedelta(days=days)   # wall-clock arithmetic: the offset is recomputed for the new day
    return int(earlier.timestamp() * 1000)


def _new_york_offset_hours(t_ms: int) -> int:
    """How many hours New York is behind UTC at a UTC time, by the US rule, for machines without a tz database:
    4 in summer time (from 07:00 UTC on the second Sunday of March to 06:00 UTC on the first Sunday of November,
    both 02:00 New York), 5 otherwise."""
    utc = dt.datetime.fromtimestamp(t_ms / 1000, dt.timezone.utc)

    def sunday(month: int, first_day: int) -> dt.datetime:   # the first Sunday on or after that day of the month
        d = dt.date(utc.year, month, first_day)
        d += dt.timedelta(days=(6 - d.weekday()) % 7)
        return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
    starts = sunday(3, 8) + dt.timedelta(hours=7)     # the second Sunday of March
    ends = sunday(11, 1) + dt.timedelta(hours=6)      # the first Sunday of November
    return 4 if starts <= utc < ends else 5


def new_york_hour(t_ms: int) -> int:
    """The New York clock hour of a UTC time (UTC hour minus 4 in summer time, minus 5 in winter, when there is no
    tz database)."""
    tz = _new_york()
    if tz is None:
        return (dt.datetime.fromtimestamp(t_ms / 1000, dt.timezone.utc).hour - _new_york_offset_hours(t_ms)) % 24
    return dt.datetime.fromtimestamp(t_ms / 1000, tz).hour


def minute_floor(ms: int) -> int:
    return ms - ms % MIN_MS


def iso(ms: int) -> str:
    """The journal's ISO form (seconds, +00:00) of a time in milliseconds."""
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def ms_of(text: str) -> int:
    """Milliseconds of an ISO time ('...Z' or '...+00:00'; a time without a zone is taken as UTC)."""
    d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def utc_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def is_weekend(ms: int) -> bool:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).weekday() >= 5


def window_start_ms(t_ms: int) -> int:
    return t_ms - PRE_MIN * MIN_MS


def baseline_step_min(t_ms: int) -> int:
    """The candle size of the baseline series: 15 minutes on weekdays, 30 for a weekend event (see the constants)."""
    return WEEKEND_STEP_MIN if is_weekend(t_ms) else BASELINE_STEP_MIN


def baseline_start_ms(t_ms: int) -> int:
    """Where the baseline series starts: 1,000 candles end at the event minute (aligned down to the candle size)."""
    step = baseline_step_min(t_ms) * MIN_MS
    return t_ms - t_ms % step - BASELINE_CANDLES * step


def is_complete(t_ms: int, now_ms: int) -> bool:
    """True once the last candle of the correction window has closed (two extra minutes for Binance to publish it)."""
    return now_ms >= t_ms + (POST_MIN + 2) * MIN_MS


# ---- headline helpers ------------------------------------------------------------------------------------------
def classify(title: str) -> str:
    """The headline's category: the first group of CATEGORIES with a matching word, else 'other'."""
    low = title.lower()
    for name, patterns in _CATEGORY_PATTERNS:
        if any(p.search(low) for p in patterns):
            return name
    return "other"


def headline_sentiment(title: str) -> float:
    """The keyword tone of one headline, -1..+1 (the news agent's word lists, two words = full score)."""
    return max(-1.0, min(1.0, (hits(title, POSITIVE) - hits(title, NEGATIVE)) / 2))


def url_hash(link: str, title: str) -> str:
    """A short fingerprint of a headline's link (or of its title when the feed gives no link)."""
    key = link.strip() if link and link.strip() else "title:" + title.strip()
    return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:16]


# ---- pure measurement over one-minute candles ------------------------------------------------------------------
def excursions(candles: list[dict], t_ms: int, ref_close: float, horizon_min: int) -> list[tuple[int, float]]:
    """(minute, move in % of ref_close) for the closes of the candles opening at t_ms .. t_ms + horizon - 1 minute.
    Minute m is the close of the candle that opens at t_ms + (m - 1) minutes, known at t_ms + m minutes."""
    by_time = {c["time"]: c["close"] for c in candles}
    out = []
    for m in range(1, horizon_min + 1):
        close = by_time.get(t_ms + (m - 1) * MIN_MS)
        if close is not None and ref_close:
            out.append((m, (close / ref_close - 1) * 100))
    return out


def k_for(t_ms: int) -> float:
    """The K of the jump rule for a window starting at t_ms: K_WEEKEND on Saturday and Sunday (UTC), else K_WEEKDAY."""
    return K_WEEKEND if is_weekend(t_ms) else K_WEEKDAY


def threshold(baseline_pct: float, k: float = K_IMPULSE, floor_pct: float = FLOOR_PCT) -> float:
    """The move a jump has to exceed: K times the normal-day baseline, and never less than the floor."""
    return max(k * baseline_pct, floor_pct)


def impulse(candles: list[dict], t_ms: int, p0: float, baseline_pct: float, horizon_min: int = IMPULSE_MIN,
            k: float = K_IMPULSE, floor_pct: float = FLOOR_PCT) -> dict:
    """The impulse test on the window starting at t_ms, against the pre-event close p0.
    {"impulse": bool, "propagation_min": the first minute whose close was beyond the threshold (None without a jump),
    "magnitude_pct": the largest move within the window, signed, "peak_min": its minute, "direction": -1 | 0 | 1 (the
    sign of the largest move; only meaningful with a jump)}. The direction comes from the price, never from a headline."""
    exc = excursions(candles, t_ms, p0, horizon_min)
    thr = threshold(baseline_pct, k, floor_pct)
    peak_min, magnitude = 0, 0.0
    propagation = None
    for m, e in exc:
        if abs(e) > abs(magnitude):
            peak_min, magnitude = m, e
        if propagation is None and abs(e) >= thr:
            propagation = m
    return {"impulse": propagation is not None, "propagation_min": propagation, "magnitude_pct": magnitude,
            "peak_min": peak_min, "direction": (magnitude > 0) - (magnitude < 0), "threshold_pct": thr}


def noise_level(candles: list[dict], t_ms: int, p0: float) -> float | None:
    """The mean 1-minute true range over the noise window [-360, -60) minutes, in % of the pre-event close: the
    coin's ordinary minute-to-minute movement before anything happened (kept for the record; the jump test uses
    the normal-day baseline). None when fewer than 240 of the 300 candles exist."""
    by_time = {c["time"]: c for c in candles}
    trs = []
    for m in range(-PRE_MIN, -QUIET_MIN):
        c, prev = by_time.get(t_ms + m * MIN_MS), by_time.get(t_ms + (m - 1) * MIN_MS)
        if c is None:
            continue
        tr = c["high"] - c["low"]
        if prev is not None:
            tr = max(tr, abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"]))
        trs.append(tr)
    if len(trs) < 240 or not p0:
        return None
    return sum(trs) / len(trs) / p0 * 100


def correction(candles: list[dict], t_ms: int, p0: float, peak_min: int, magnitude_pct: float,
               pre_volume_median: float, retrace: float = RETRACE) -> dict:
    """After the peak, inside the window ending at t_ms + POST_MIN: the first close that gives back at least
    `retrace` of the move ({"retrace_min": minutes from the peak, or None}), and whether ten consecutive minutes
    traded less than the pre-event median volume ({"volume_exhausted": bool}). Post-mortem facts: computed only once
    the whole window exists."""
    by_time = {c["time"]: c for c in candles}
    peak_close = by_time.get(t_ms + (peak_min - 1) * MIN_MS, {}).get("close")
    if peak_close is None or not magnitude_pct:
        return {"retrace_min": None, "volume_exhausted": False}
    move = peak_close - p0
    target = peak_close - retrace * move
    retrace_min, quiet, exhausted = None, 0, False
    for m in range(peak_min + 1, POST_MIN + 1):
        c = by_time.get(t_ms + (m - 1) * MIN_MS)
        if c is None:
            quiet = 0
            continue
        if retrace_min is None and ((move > 0 and c["close"] <= target) or (move < 0 and c["close"] >= target)):
            retrace_min = m - peak_min
        quiet = quiet + 1 if c["volume"] < pre_volume_median else 0
        if quiet >= EXHAUST_MINUTES:
            exhausted = True
    return {"retrace_min": retrace_min, "volume_exhausted": exhausted}


def normal_day_baseline(series: list[dict], t_ms: int, skip_dates: set[str] | None = None,
                        step_min: int | None = None, horizons: tuple[int, ...] = (QUIET_MIN, IMPULSE_MIN),
                        max_days: int = BASELINE_DAYS, min_days: int = MIN_BASELINE_DAYS, offset_min: int = 0) -> dict:
    """What the coin usually does at this clock hour, from a series of coarser candles (15-minute; 30-minute for a
    weekend event) covering the days before t_ms. For each of the last `max_days` days of the same weekday class
    (Mon-Fri or Sat-Sun) whose date is not in `skip_dates` (listed releases, US market holidays), the largest move of
    the closes within `h` minutes of the same New York clock time is measured, from the close just before; the
    baseline for horizon h is the median of those. `offset_min` shifts the measured window on each of those days
    (-60: the hour before the clock time, for "moved first"); the days themselves are always chosen by t_ms, so the
    hour before a Monday 00:30 UTC event is compared with the hour before 00:30 on other weekdays, never with
    weekend hours. Returns {"b": {h: baseline %}, "volume": the median volume of the first candle of the window on
    those days, "step": the candle minutes, "days": how many days it rests on, "dates": their UTC dates}; "b" is
    empty when fewer than `min_days` days qualify."""
    step_min = step_min or baseline_step_min(t_ms)
    step = step_min * MIN_MS
    by_time = {c["time"]: c["close"] for c in series}
    volumes = {c["time"]: c.get("volume", 0.0) for c in series}
    first = min(by_time) if by_time else None
    weekend, skip = is_weekend(t_ms), skip_dates or set()
    samples: dict[int, list[float]] = {h: [] for h in horizons}
    first_volumes: list[float] = []
    dates: list[str] = []
    points = max(horizons) // step_min
    for d in range(1, BASELINE_LOOKBACK_DAYS + 1):
        day = same_clock_days_ago(t_ms, d)
        day -= day % step
        start = day + offset_min * MIN_MS
        start -= start % step
        if first is None or start - step < first:
            break
        if is_weekend(day) != weekend or utc_date(day) in skip:
            continue
        ref = by_time.get(start - step)
        closes = [by_time.get(start + i * step) for i in range(points)]
        if ref is None or not ref or any(c is None for c in closes):
            continue
        running, biggest = 0.0, {}
        for i, close in enumerate(closes, start=1):
            running = max(running, abs(close / ref - 1) * 100)
            biggest[i * step_min] = running
        for h in horizons:
            samples[h].append(biggest[h])
        first_volumes.append(volumes.get(start, 0.0))
        dates.append(utc_date(day))
        if len(dates) >= max_days:
            break
    out = {"b": {}, "volume": None, "step": step_min, "days": len(dates), "dates": dates}
    if len(dates) >= min_days:
        out["b"] = {h: statistics.median(samples[h]) for h in horizons}
        out["volume"] = statistics.median(first_volumes)
    return out


def measure(candles: list[dict], t_ms: int, baseline: dict, hour_before: dict | None = None,
            k: float | None = None) -> dict:
    """Everything the record needs from the 600 one-minute candles of one window. `baseline` is the normal-day
    baseline at the event's clock time (horizon 120 min), `hour_before` the one at the clock time an hour earlier
    (horizon 60 min) for the "moved first" check; `k` defaults to the K of the window's kind of day. Never raises;
    "complete" is False, with a "note", when the candles are missing or too few or the baseline rests on too few
    days. The direction is the sign of the largest move; the peak, the retrace and the volume flag are post-mortem
    facts, computed only because the whole window exists."""
    out: dict = {"complete": False, "note": None, "baseline_pct": None, "baseline_days": baseline.get("days", 0),
                 "baseline_atr": None, "volume_ratio": None, "moved_first": None, "impulse": None, "direction": None,
                 "propagation_min": None, "magnitude_pct": None, "peak_min": None, "peak_ts": None, "retrace_ts": None,
                 "correction_delay_min": None, "volume_exhausted": None, "threshold_pct": None}
    by_time = {c["time"]: c for c in candles if isinstance(c, dict) and "time" in c}
    inside = [t for t in by_time if window_start_ms(t_ms) <= t < t_ms + POST_MIN * MIN_MS]
    p0 = by_time.get(t_ms - MIN_MS, {}).get("close")
    if len(inside) < MIN_WINDOW_CANDLES or not p0:
        out["note"] = f"incomplete candles ({len(inside)} of {PRE_MIN + POST_MIN})"
        return out
    if not baseline.get("b") or IMPULSE_MIN not in baseline["b"]:
        out["note"] = f"too few normal days for a baseline ({baseline.get('days', 0)} of {MIN_BASELINE_DAYS} needed)"
        return out
    k = k_for(t_ms) if k is None else k
    b120 = baseline["b"][IMPULSE_MIN]
    out["baseline_pct"] = b120
    out["baseline_atr"] = noise_level(candles, t_ms, p0)
    if baseline.get("volume"):   # the first 15 (weekend: 30) minutes' volume against the normal days' at that hour
        step = baseline.get("step") or BASELINE_STEP_MIN
        traded = sum(by_time[t]["volume"] for t in by_time if t_ms <= t < t_ms + step * MIN_MS)
        out["volume_ratio"] = round(traded / baseline["volume"], 2)
    q0 = by_time.get(t_ms - (QUIET_MIN + 1) * MIN_MS, {}).get("close")
    b60 = (hour_before or {}).get("b", {}).get(QUIET_MIN)
    if q0 and b60 is not None:   # a flat hour on every normal day is a baseline of 0: the floor then decides
        out["moved_first"] = impulse(candles, t_ms - QUIET_MIN * MIN_MS, q0, b60, QUIET_MIN, k=k)["impulse"]
    imp = impulse(candles, t_ms, p0, b120, k=k)
    out.update({"impulse": imp["impulse"], "magnitude_pct": imp["magnitude_pct"], "peak_min": imp["peak_min"],
                "peak_ts": iso(t_ms + imp["peak_min"] * MIN_MS) if imp["peak_min"] else None,
                "threshold_pct": imp["threshold_pct"], "complete": True})
    if imp["impulse"]:
        out["direction"], out["propagation_min"] = imp["direction"], imp["propagation_min"]
        pre = [by_time[t]["volume"] for t in by_time if t_ms - PRE_VOLUME_MIN * MIN_MS <= t < t_ms]
        corr = correction(candles, t_ms, p0, imp["peak_min"], imp["magnitude_pct"],
                          statistics.median(pre) if pre else 0.0)
        out["correction_delay_min"] = corr["retrace_min"]
        if corr["retrace_min"] is not None:
            out["retrace_ts"] = iso(t_ms + (imp["peak_min"] + corr["retrace_min"]) * MIN_MS)
        out["volume_exhausted"] = corr["volume_exhausted"]
    return out


def baselines(series: list[dict], t_ms: int, skip_dates: set[str], step_min: int | None = None) -> tuple[dict, dict]:
    """The two normal-day baselines one window needs, from one baseline download: at the window's clock time (the
    largest 2-hour move) and the hour before it on the same normal days (the largest 1-hour move, for "moved first")."""
    step_min = step_min or baseline_step_min(t_ms)
    main = normal_day_baseline(series, t_ms, skip_dates, step_min, horizons=(IMPULSE_MIN,))
    # the same normal days, an hour earlier: the day class, the skipped dates and K all follow the window itself
    before = normal_day_baseline(series, t_ms, skip_dates, step_min, horizons=(QUIET_MIN,), offset_min=-QUIET_MIN)
    return main, before


# ---- the macro calendar ----------------------------------------------------------------------------------------
def load_calendar(shipped: Path, owner: Path | None) -> tuple[list[dict], list[str], set[str]]:
    """Rows {ts_utc, name, kind, remove} from the shipped file plus the owner's file, (rows, problems, holidays).
    A row needs an ISO UTC time ending in Z or +00:00, a non-empty name and kind; bad rows are skipped and named in
    `problems`. The owner's file may add rows, remove shipped ones ("remove": true) and add holidays."""
    rows, problems, holidays = [], [], set()
    for path, who in ((shipped, "shipped calendar"), (owner, "data/macro_calendar.json")):
        if path is None or not Path(path).exists():
            continue
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            problems.append(f"{who}: cannot be read ({e})")
            continue
        events, days = data.get("events") or [], data.get("holidays") or []
        if not isinstance(events, list) or not isinstance(days, list):
            problems.append(f"{who}: 'events' and 'holidays' must be lists; the file is ignored")
            continue
        for i, row in enumerate(events):
            try:
                ts = str(row["ts_utc"]).strip()
                if not (ts.endswith("Z") or ts.endswith("+00:00")):
                    raise ValueError("time must end in Z or +00:00")
                ts_iso = iso(ms_of(ts))
                name, kind = str(row["name"]).strip(), str(row["kind"]).strip().lower()
                if not name or not kind:
                    raise ValueError("name and kind must not be empty")
                rows.append({"ts_utc": ts_iso, "name": name, "kind": kind, "remove": bool(row.get("remove"))})
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                problems.append(f"{who}: event {i + 1} skipped ({e})")
        for h in days:
            try:
                holidays.add(dt.date.fromisoformat(str(h)).isoformat())
            except ValueError:
                problems.append(f"{who}: holiday {h!r} is not a date")
    return rows, problems, holidays


def release_name(kind: str, name: str = "") -> str:
    """A short name for a release kind ('US CPI'), or the calendar's own name for an owner-added kind."""
    return RELEASE_NAMES.get(kind) or name or f"release ({kind})"


def when_text(t_ms: int) -> str:
    """'Wed 14 Oct 12:30 UTC': a time with its day, for the diary."""
    d = dt.datetime.fromtimestamp(t_ms / 1000, dt.timezone.utc)
    return f"{d:%a} {d.day} {d:%b} {d:%H:%M} UTC"


def usable_budget(s) -> int:
    """The requests a day the study may spend: EVENTS_DAILY_REQUESTS, or 0 (the study is off) when that is below one
    measured coin-event (REQUESTS_PER_EVENT downloads), because then nothing could ever be measured."""
    n = int(getattr(s, "events_daily_requests", 0) or 0)
    return n if n >= REQUESTS_PER_EVENT else 0


def refused(e: Exception) -> bool:
    """True when Binance refused this one request (HTTP 400, such as -1121 'Invalid symbol.': a coin name it does not
    know). Asking again would get the same answer. Rate limits (418, 429), server trouble (5xx), blocks (403, 451) and
    network failures are temporary or hit every coin alike, so they are not refusals."""
    return isinstance(e, BinanceError) and str(e).startswith("HTTP 400")


# ---- the agent -------------------------------------------------------------------------------------------------
class EventsAgent:
    """Records events and measures their windows once complete. Reads the news agent's items and the public price
    API; writes only its own tables (headlines, macro_calendar, news_events) and its events_* state keys."""
    name = "events"

    def __init__(self, journal: Journal, client: BinanceClient | None, s, shipped_calendar: Path | None = None,
                 owner_calendar: Path | None = None, now_fn=time.time):
        self.j = journal
        self.client = client
        self.s = s
        self.shipped = Path(shipped_calendar) if shipped_calendar else SHIPPED_CALENDAR
        if owner_calendar is None and getattr(s, "db_path", ":memory:") != ":memory:":
            owner_calendar = Path(s.db_path).with_name("macro_calendar.json")
        self.owner = Path(owner_calendar) if owner_calendar else None
        self.now_fn = now_fn
        self.holidays: set[str] = set()
        self._calendar_at = 0.0
        self._calendar_mtimes: tuple = ()
        self._retry_at = 0.0
        try:   # a pause after a failed download outlives a restart, so /events and the agent agree on it
            saved = self.j.get("events_retry_at")
            self._retry_at = ms_of(saved) / 1000 if saved else 0.0
        except (TypeError, ValueError, AttributeError):
            self._retry_at = 0.0
        self._ingested_at = 0.0

    @property
    def enabled(self) -> bool:
        return usable_budget(self.s) > 0

    # ---- the step the loop calls ----
    def step(self, now: dt.datetime, symbols: list[str], items: list[dict] | None, news_result: dict | None,
             fetched_at: float | None) -> list[dict]:
        """One check's worth of work, each part guarded so one failure does not stop the next: the calendar, new
        release rows, new headlines, the measurement of complete windows (under the budget), the daily prune.
        Returns console entries (symbol EVENTS, stage events); an exception becomes one error entry."""
        if not self.enabled or getattr(self.s, "trend", False):   # the daily trend rule never runs the study
            return []
        out: list[dict] = []
        parts = [("calendar", lambda: self.refresh_calendar()),
                 ("releases", lambda: self.schedule_macro(now, symbols) if self.client else 0),
                 ("headlines", lambda: self.ingest(items or [], [s.removesuffix("USDT") for s in symbols], news_result,
                                                   fetched_at) if fetched_at and fetched_at > self._ingested_at else 0),
                 ("prune", lambda: self.prune(now))]
        for what, fn in parts:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - a measurement must never stop the desk
                log.exception("Event study: %s failed", what)
                out.append({"symbol": "EVENTS", "stage": "events", "error": f"{what}: {e}"})
        try:
            out += self.settle(now)
        except Exception as e:  # noqa: BLE001
            log.exception("Event study: settle failed")
            out.append({"symbol": "EVENTS", "stage": "events", "error": f"could not measure events: {e}"})
        return out

    # ---- calendar ----
    def refresh_calendar(self, force: bool = False) -> None:
        """Load the shipped calendar and the owner's additions into the macro_calendar table, at most once an hour
        or when a file changed. A malformed owner file is reported once in the diary and ignored."""
        mtimes = tuple(p.stat().st_mtime if p and p.exists() else None for p in (self.shipped, self.owner))
        if not force and self._calendar_at and time.time() - self._calendar_at < 3600 and mtimes == self._calendar_mtimes:
            return
        rows, problems, holidays = load_calendar(self.shipped, self.owner)
        self.holidays = holidays
        for r in rows:
            if r["remove"]:
                self.j.db.execute("DELETE FROM macro_calendar WHERE ts_utc=? AND name=?", (r["ts_utc"], r["name"]))
            else:
                self.j.db.execute("INSERT OR IGNORE INTO macro_calendar (ts_utc, name, kind) VALUES (?,?,?)",
                                  (r["ts_utc"], r["name"], r["kind"]))
        self.j.db.commit()
        last = self.j.db.execute("SELECT MAX(ts_utc) FROM macro_calendar").fetchone()[0]
        status = {"path": str(self.shipped), "owner": str(self.owner) if self.owner else None, "rows": len(rows),
                  "problems": problems, "last_ts": last, "loaded_at": iso(int(time.time() * 1000))}
        self.j.set("events_calendar", status)
        if problems and self.j.get("events_calendar_problem") != problems[0]:
            self.j.set("events_calendar_problem", problems[0])
            self.j.event("error", "Release calendar: " + "; ".join(problems)[:300])
        self._warn_calendar(last)
        self._calendar_at, self._calendar_mtimes = time.time(), mtimes

    def _warn_calendar(self, last: str | None) -> None:
        """One diary line when the calendar runs out within 60 days, or a release kind that had dates has none in the
        next 70 days (the FOMC's December-January gap is 49 days, so 45 would warn every December)."""
        if not last:
            return
        now_ms = int(self.now_fn() * 1000)
        warnings = []
        if ms_of(last) - now_ms < 60 * DAY_MS:
            warnings.append(f"the release calendar runs out on {last[:10]}: a code update "
                            f"refreshes it, or add dates to data/macro_calendar.json")
        for (kind,) in self.j.db.execute("SELECT DISTINCT kind FROM macro_calendar"):
            ahead = self.j.db.execute("SELECT COUNT(*) FROM macro_calendar WHERE kind=? AND ts_utc > ? AND ts_utc <= ?",
                                      (kind, iso(now_ms), iso(now_ms + 70 * DAY_MS))).fetchone()[0]
            if not ahead and ms_of(last) - now_ms >= 70 * DAY_MS:
                warnings.append(f"no {release_name(kind)} date in the next 70 days although the calendar runs further: "
                                f"check the official schedule and add it to data/macro_calendar.json")
        text = "; ".join(warnings)
        if text and self.j.get("events_calendar_warned") != text:
            self.j.set("events_calendar_warned", text)
            self.j.event("events", "Release calendar: " + text)

    def skip_dates(self) -> set[str]:
        """Dates that are not 'normal days' for the baseline: every listed release day and US market holiday."""
        dates = {r[0][:10] for r in self.j.db.execute("SELECT ts_utc FROM macro_calendar")}
        return dates | self.holidays

    def schedule_macro(self, now: dt.datetime, symbols: list[str]) -> int:
        """One news_events row per watched coin for every listed release that happened since the last call (the
        last BACKFILL_DAYS days at a fresh install). Returns how many rows were created. A coin that joins the
        watchlist later is measured from the next release on, not back-filled."""
        now_ms = int(now.timestamp() * 1000)
        # never further back than BACKFILL_DAYS, also after the study was switched off for a while
        since = max(self.j.get("events_macro_to") or "", iso(now_ms - BACKFILL_DAYS * DAY_MS))
        created = 0
        for r in self.j.db.execute("SELECT ts_utc, name, kind FROM macro_calendar WHERE ts_utc > ? AND ts_utc <= ? "
                                   "ORDER BY ts_utc", (since, iso(now_ms))).fetchall():
            for sym in symbols:
                exists = self.j.db.execute("SELECT 1 FROM news_events WHERE kind='macro' AND ts_utc=? AND symbol=?",
                                           (r["ts_utc"], sym)).fetchone()
                if exists:
                    continue
                self.j.db.execute("INSERT INTO news_events (kind, ts_utc, symbol, label, category) VALUES (?,?,?,?,?)",
                                  ("macro", r["ts_utc"], sym, r["name"], f"macro:{r['kind']}"))
                created += 1
        self.j.db.commit()
        self.j.set("events_macro_to", iso(now_ms))
        return created

    # ---- headlines ----
    def ingest(self, items: list[dict], coins: list[str], news_result: dict | None, fetched_at: float | None) -> int:
        """Store the feeds' items once per (headline, coin) with publication time, source, tone and category, and
        attach each to its coin's burst. `coins` are bare names (BTC); `news_result` is the news agent's result for
        this batch (its per-coin score becomes the burst's sentiment). Returns how many rows were new."""
        if not self.enabled:
            return 0
        fetched_ms = int((fetched_at or self.now_fn()) * 1000)
        self._ingested_at = fetched_at or self.now_fn()
        new = 0
        for item in items:
            title = clean(str(item.get("title") or ""), 400).strip()
            if not title:
                continue
            h = url_hash(str(item.get("link") or ""), title)
            ts = fetched_ms
            if item.get("ts"):
                try:
                    ts = min(ms_of(str(item["ts"])), fetched_ms)   # a time in the future is a feed error: now
                except ValueError:
                    ts = fetched_ms
            too_old = fetched_ms - ts > MAX_HEADLINE_AGE_DAYS * DAY_MS
            matched = [c for c in coins if NewsAgent.mentions(title, c)]
            category, tone, source = classify(title), headline_sentiment(title), str(item.get("source") or "")
            for coin in matched or [""]:
                exists = self.j.db.execute("SELECT 1 FROM headlines WHERE url_hash=? AND coins=?", (h, coin)).fetchone()
                if exists:
                    continue
                burst = None if (too_old or not coin) else self._burst_for(coin, ts, title, category, news_result)
                self.j.db.execute("INSERT OR IGNORE INTO headlines (ts_utc, source, title, url_hash, coins, sentiment, "
                                  "category, burst_id) VALUES (?,?,?,?,?,?,?,?)",
                                  (iso(ts), source, title, h, coin, tone, category, burst))
                new += 1
        self.j.db.commit()
        return new

    def _burst_for(self, coin: str, ts_ms: int, title: str, category: str, news_result: dict | None) -> int:
        """The open burst of the coin this headline belongs to (within 30 minutes, the burst spanning at most 30
        minutes afterwards), or a new event row. Returns the event id."""
        sym = coin + "USDT"
        # a burst's stored time is its first headline's minute (seconds dropped), so the search starts from this
        # headline's minute too; the span check below, on the exact times, decides
        lo, hi = iso(minute_floor(ts_ms) - BURST_MINUTES * MIN_MS), iso(ts_ms + BURST_MINUTES * MIN_MS)
        for row in self.j.db.execute("SELECT id, ts_utc FROM news_events WHERE symbol=? AND kind='headline' AND settled_at "
                                     "IS NULL AND ts_utc BETWEEN ? AND ? ORDER BY ABS(julianday(ts_utc) - julianday(?))",
                                     (sym, lo, hi, iso(ts_ms))).fetchall():
            span = self.j.db.execute("SELECT MIN(ts_utc), MAX(ts_utc) FROM headlines WHERE burst_id=?", (row["id"],)).fetchone()
            first = min(ms_of(span[0]), ts_ms) if span[0] else ts_ms
            last = max(ms_of(span[1]), ts_ms) if span[1] else ts_ms
            if last - first <= BURST_MINUTES * MIN_MS:
                self._join_burst(row["id"], coin, ts_ms, title, category)
                return row["id"]
        score = (news_result or {}).get(coin, {}).get("score") if isinstance(news_result, dict) else None
        cur = self.j.db.execute("INSERT INTO news_events (kind, ts_utc, symbol, label, category, sentiment, n_headlines, "
                                "velocity) VALUES ('headline', ?, ?, ?, ?, ?, 1, ?)",
                                (iso(minute_floor(ts_ms)), sym, clean(title, 160), category, score,
                                 self.velocity(coin, ts_ms, extra=1)))
        return cur.lastrowid

    def _join_burst(self, event_id: int, coin: str, ts_ms: int, title: str, category: str) -> None:
        """Add a headline to a burst: one more headline, the start moved earlier if this one is older (feeds deliver
        out of order), the category recomputed as the burst's most common, the velocity at the newest headline."""
        row = self.j.db.execute("SELECT ts_utc, label FROM news_events WHERE id=?", (event_id,)).fetchone()
        stored = self.j.db.execute("SELECT ts_utc, category FROM headlines WHERE burst_id=? ORDER BY ts_utc, id",
                                   (event_id,)).fetchall()
        cats = [c for _, c in sorted([(ms_of(r["ts_utc"]), r["category"]) for r in stored] + [(ts_ms, category)],
                                       key=lambda x: x[0])]
        most = Counter(cats).most_common()
        top = [c for c, n in most if n == most[0][1]]
        first_cat = cats[0] if cats[0] in top else top[0]          # ties go to the earliest headline's category
        start = min(ms_of(row["ts_utc"]), minute_floor(ts_ms))
        label = clean(title, 160) if minute_floor(ts_ms) < ms_of(row["ts_utc"]) else row["label"]
        newest = max([ms_of(r["ts_utc"]) for r in stored] + [ts_ms])
        self.j.db.execute("UPDATE news_events SET n_headlines = n_headlines + 1, ts_utc=?, label=?, category=?, velocity=? "
                          "WHERE id=?", (iso(start), label, first_cat, self.velocity(coin, newest, extra=1), event_id))

    def velocity(self, coin: str, ts_ms: int, extra: int = 0) -> float:
        """News velocity: headlines about the coin in the 60 minutes up to `ts_ms` (plus `extra` not yet stored),
        divided by the coin's usual hourly rate (the median of its daily counts over the previous 7 days, at least
        one a day, divided by 24). 1.0 = the usual rate; a rarely-covered coin reads high on a single headline,
        which the texts allow for."""
        hour = self.j.db.execute("SELECT COUNT(*) FROM headlines WHERE coins=? AND ts_utc > ? AND ts_utc <= ?",
                                 (coin, iso(ts_ms - 60 * MIN_MS), iso(ts_ms))).fetchone()[0] + extra
        counts = []
        for d in range(1, 8):
            day_start = utc_date(ts_ms - d * DAY_MS)
            n = self.j.db.execute("SELECT COUNT(*) FROM headlines WHERE coins=? AND substr(ts_utc, 1, 10)=?",
                                  (coin, day_start)).fetchone()[0]
            counts.append(n)
        usual_per_hour = max(statistics.median(counts), 1) / 24
        return round(hour / usual_per_hour, 2)

    # ---- the budget ----
    def budget(self, now: dt.datetime) -> tuple[int, int]:
        """(requests used today, requests allowed a day); the count restarts at 00:00 UTC."""
        rec = self.j.get("events_budget") or {}
        used = int(rec.get("used", 0)) if rec.get("day") == now.date().isoformat() else 0
        return used, max(0, int(getattr(self.s, "events_daily_requests", 0)))

    def _spend(self, now: dt.datetime) -> None:
        used, _ = self.budget(now)
        self.j.set("events_budget", {"day": now.date().isoformat(), "used": used + 1})

    def _fetch(self, now: dt.datetime, symbol: str, interval: str, limit: int, start_ms: int) -> list[dict]:
        """One candle download, counted against the budget before it is made (a failed request counts too)."""
        self._spend(now)
        rows = self.client.klines(symbol, interval, limit=limit, start_ms=start_ms)
        if not isinstance(rows, list):
            raise BinanceError(f"unexpected answer for {symbol} {interval} candles")
        return rows

    # ---- settling ----
    def settle(self, now: dt.datetime) -> list[dict]:
        """Measure up to SETTLE_PER_CHECK complete coin-events, releases first, then the bursts with the most
        headlines, then the oldest; three requests each, within the daily budget. Returns console entries: one
        summary line when something was measured, one error entry when a download failed (then the step pauses
        RETRY_MINUTES and the event is tried again) or a record could not be measured (it is written off with the
        reason, so one broken record never blocks the others). A download Binance refuses (HTTP 400, such as a coin
        name it does not know) is not temporary: that record is written off at once and the next one is measured;
        so is a record whose download has kept failing for GIVE_UP_HOURS."""
        if not self.enabled or self.client is None or self.now_fn() < self._retry_at:
            return []
        now_ms = int(now.timestamp() * 1000)
        rows = self.j.db.execute(
            "SELECT * FROM news_events WHERE settled_at IS NULL AND ts_utc <= ? ORDER BY (kind='macro') DESC, "
            "n_headlines DESC, ts_utc ASC LIMIT ?", (iso(now_ms - (POST_MIN + 2) * MIN_MS), SETTLE_PER_CHECK)).fetchall()
        settled, spent, out = 0, 0, []
        failing = self.j.get("events_failing") or {}   # {row id: [its first failed try, tries]} while it keeps failing
        for row in rows:
            used, allowed = self.budget(now)
            if used + REQUESTS_PER_EVENT > allowed:
                break
            try:
                result = self.settle_one(row, now)
            except (BinanceError, requests.RequestException) as e:
                spent += self.budget(now)[0] - used
                first, tries = failing.get(str(row["id"])) or [iso(now_ms), 0]
                failing[str(row["id"])] = [first, tries + 1]
                if refused(e) or (tries + 1 >= GIVE_UP_TRIES and now_ms - ms_of(first) >= GIVE_UP_HOURS * 3600_000):
                    # Binance refuses this coin (a name it does not know), or the download has failed for a whole
                    # day: the record is written off with the reason, so one coin can never hold up the queue
                    why = (f"prices not available for {row['symbol']}" if refused(e)
                           else f"prices could not be downloaded for {GIVE_UP_HOURS} hours")
                    self._write_off(row, now_ms, f"{why}: {clean(str(e), 200)}")
                    failing.pop(str(row["id"]), None)
                    out.append({"symbol": "EVENTS", "stage": "events",
                                "error": f"{why} ({clean(str(e), 200)}): that event is recorded as not measured"
                                         + ("; check the coin names in SYMBOLS" if refused(e) else "")})
                    continue
                # a temporary failure (network, rate limit, Binance busy): counted, pause, the same event next time
                self._retry_at = self.now_fn() + RETRY_MINUTES * 60
                self.j.set("events_retry_at", iso(int(self._retry_at * 1000)))
                out.append({"symbol": "EVENTS", "stage": "events",
                            "error": f"could not download prices for {row['symbol']}: {clean(str(e), 200)}"})
                break
            except Exception as e:  # noqa: BLE001 - a broken record: written off with the reason, the queue moves on
                log.exception("Event study: could not measure event %s", row["id"])
                self._write_off(row, now_ms, f"could not be measured: {clean(str(e), 200)}")
                failing.pop(str(row["id"]), None)
                out.append({"symbol": "EVENTS", "stage": "events",
                            "error": f"could not measure an event of {row['symbol']}: {clean(str(e), 200)}"})
                spent += self.budget(now)[0] - used
                continue
            failing.pop(str(row["id"]), None)
            settled += 1
            spent += result["requests"]
        if failing != (self.j.get("events_failing") or {}):
            self.j.set("events_failing", failing)
        if settled:
            used, allowed = self.budget(now)
            out.insert(0, {"symbol": "EVENTS", "stage": "events", "settled": settled, "requests": spent, "used": used,
                           "budget": allowed})
        return out

    def _write_off(self, row: sqlite3.Row, now_ms: int, note: str) -> None:
        """Close a record that cannot be measured: settled now, nothing measured, the reason in the note. A release
        whose last coin this was gets its diary line."""
        self.j.db.execute("UPDATE news_events SET settled_at=?, note=? WHERE id=?", (iso(now_ms), note, row["id"]))
        self.j.db.commit()
        if row["kind"] == "macro":
            try:
                self._release_diary(row)
            except Exception:  # noqa: BLE001 - a broken record may not even have a readable time: no diary line then
                log.exception("Event study: no diary line for event %s", row["id"])

    def settle_one(self, row: sqlite3.Row, now: dt.datetime) -> dict:
        """Measure one coin-event: the event window, the normal-day baseline, the placebo window; write the record.
        Returns {"requests": n, "measured": bool}. Raises on a failed download (the caller counts and retries)."""
        t_ms, symbol = ms_of(row["ts_utc"]), row["symbol"]
        requests_made = 0
        candles = self._fetch(now, symbol, "1m", PRE_MIN + POST_MIN, window_start_ms(t_ms))
        requests_made += 1
        quick = measure(candles, t_ms, {"b": {}, "days": 0})
        if quick["note"] and quick["note"].startswith("incomplete"):
            self._write(row, quick, None, None, [quick["note"]], now)
            return {"requests": requests_made, "measured": False}
        step_min = baseline_step_min(t_ms)
        series = self._fetch(now, symbol, f"{step_min}m", BASELINE_CANDLES, baseline_start_ms(t_ms))
        requests_made += 1
        skip = self.skip_dates()
        m = measure(candles, t_ms, *baselines(series, t_ms, skip, step_min))
        notes = [m["note"]] if m["note"] else []
        placebo_ts, placebo = None, None
        if m["complete"]:
            # only a day the same download can judge (enough normal days before it) is worth the third request
            placebo_ts = self.placebo_time(row, now, lambda c: bool(baselines(series, c, skip, step_min)[0]["b"]))
            if placebo_ts is None and self.placebo_time(row, now) is not None:
                notes.append(f"no placebo day within {PLACEBO_DAYS} days could be judged: the free days have too few "
                             f"normal days before them in the download")
            elif placebo_ts is None:
                notes.append(f"no free placebo day within {PLACEBO_DAYS} days")
            else:
                pc = self._fetch(now, symbol, "1m", PRE_MIN + POST_MIN, window_start_ms(placebo_ts))
                requests_made += 1
                # the placebo is judged against its own preceding normal days (from the same download): its own day
                # is never part of the figure it is compared with, exactly as for the event
                placebo = measure(pc, placebo_ts, *baselines(series, placebo_ts, skip, step_min))
                if not placebo["complete"]:
                    notes.append("placebo window: " + str(placebo["note"]))
                    placebo = None
            notes += self._overlap_notes(row, t_ms)
        self._write(row, m, placebo_ts, placebo, notes, now)
        if row["kind"] == "macro":
            self._release_diary(row)
        return {"requests": requests_made, "measured": m["complete"]}

    def _write(self, row: sqlite3.Row, m: dict, placebo_ts: int | None, placebo: dict | None, notes: list[str],
               now: dt.datetime) -> None:
        """Store the record. For the matched normal day the same facts are kept as for the event (its jump and move,
        whether its price moved first, and for its jump the give-back and the volume flag), so every share the texts
        show can stand next to the normal-day share."""
        def flag(v):
            return None if v is None else int(bool(v))
        p = placebo or {}
        p_jump = bool(p.get("impulse"))
        self.j.db.execute(
            "UPDATE news_events SET moved_first=?, baseline_atr=?, baseline_pct=?, baseline_days=?, volume_ratio=?, impulse=?, "
            "direction=?, propagation_min=?, magnitude_pct=?, peak_ts=?, retrace_ts=?, correction_delay_min=?, "
            "volume_exhausted=?, placebo_ts=?, placebo_impulse=?, placebo_magnitude_pct=?, placebo_moved_first=?, "
            "placebo_retraced=?, placebo_volume_exhausted=?, settled_at=?, note=? WHERE id=?",
            (flag(m["moved_first"]), m["baseline_atr"], m["baseline_pct"],
             m["baseline_days"], m["volume_ratio"], flag(m["impulse"]), m["direction"],
             m["propagation_min"],
             m["magnitude_pct"], m["peak_ts"], m["retrace_ts"], m["correction_delay_min"],
             flag(m["volume_exhausted"]),
             iso(placebo_ts) if placebo_ts else None, None if placebo is None else int(placebo["impulse"]),
             None if placebo is None else placebo["magnitude_pct"], flag(p.get("moved_first")),
             int(p.get("correction_delay_min") is not None) if p_jump else None,
             flag(p.get("volume_exhausted")) if p_jump else None, iso(int(now.timestamp() * 1000)),
             "; ".join(notes) if notes else None, row["id"]))
        self.j.db.commit()

    def _overlap_notes(self, row: sqlite3.Row, t_ms: int) -> list[str]:
        """Other events whose windows touch this one: another event of the coin from 6 hours before (its move would
        sit in the noise window) to 4 hours after; for a headline burst, a listed release less than 4 hours before or
        after it, so that the two 4-hour windows overlap: a burst in the hours before a release ("Bitcoin steady
        ahead of CPI") has the release's move in its own window. Such a burst is measured and shown, but kept out of
        the headline aggregates (its note says "within the ... window"), so a release is not counted twice."""
        notes = []
        coin = row["symbol"].removesuffix("USDT")
        for other in self.j.db.execute("SELECT ts_utc FROM news_events WHERE symbol=? AND id<>? AND ts_utc > ? AND ts_utc < ? "
                                       "ORDER BY ABS(julianday(ts_utc) - julianday(?)) LIMIT 1",
                                       (row["symbol"], row["id"], iso(t_ms - PRE_MIN * MIN_MS), iso(t_ms + POST_MIN * MIN_MS),
                                        row["ts_utc"])):
            gap = (ms_of(other[0]) - t_ms) // MIN_MS
            if gap:
                notes.append(f"overlaps another {coin} event {abs(gap)} min {'later' if gap > 0 else 'earlier'}")
        if row["kind"] == "headline":
            for rel in self.j.db.execute("SELECT ts_utc, name, kind FROM macro_calendar WHERE ts_utc > ? AND ts_utc < ? "
                                         "ORDER BY ts_utc LIMIT 1",
                                         (iso(t_ms - POST_MIN * MIN_MS), iso(t_ms + POST_MIN * MIN_MS))):
                after = (ms_of(rel["ts_utc"]) - t_ms) // MIN_MS
                notes.append(f"within the {release_name(rel['kind'], rel['name'])} window"
                             + (f" (the release came {after} min after the first headline)" if after > 0 else ""))
        return notes

    def placebo_time(self, row: sqlite3.Row, now: dt.datetime, judgeable=None) -> int | None:
        """The matched normal window: the same New York clock time on the nearest free day before the event (the
        second-nearest for odd row ids, so a class's placebos are not all 'the day before'); days after only when no
        day before is free. A day is free when it is the same weekday class, not a US market holiday, not a Thursday
        for an 08:00-09:00 New York event (weekly jobless claims), complete, and clear of other events: for a release,
        no listed release and no other release row within 2 hours (headlines do not disqualify a normal day); for a
        headline burst, no event of the coin and no listed release within 2 hours. `judgeable(day)`, when given, must
        also say yes (settle_one: the day has enough normal days before it to be judged), so a day that could not be
        measured is never chosen or downloaded. None when no day qualifies."""
        t_ms, now_ms = ms_of(row["ts_utc"]), int(now.timestamp() * 1000)
        weekend, claims_slot = is_weekend(t_ms), new_york_hour(t_ms) == 8
        accepted: list[int] = []
        for side in (1, -1):
            for d in range(1, PLACEBO_DAYS + 1):
                c = same_clock_days_ago(t_ms, side * d)
                c_dt = dt.datetime.fromtimestamp(c / 1000, dt.timezone.utc)
                if is_weekend(c) != weekend or utc_date(c) in self.holidays or not is_complete(c, now_ms):
                    continue
                if claims_slot and c_dt.weekday() == CLAIMS_WEEKDAY:
                    continue
                if not self._free(row, c):
                    continue
                if judgeable is not None and not judgeable(c):
                    continue
                accepted.append(c)
                if len(accepted) >= 2:
                    break
            if accepted:
                break
        if not accepted:
            return None
        return accepted[1] if (row["id"] % 2 == 1 and len(accepted) > 1) else accepted[0]

    def _free(self, row: sqlite3.Row, c_ms: int) -> bool:
        lo, hi = iso(c_ms - PLACEBO_CLEAR_MIN * MIN_MS), iso(c_ms + PLACEBO_CLEAR_MIN * MIN_MS)
        if self.j.db.execute("SELECT 1 FROM macro_calendar WHERE ts_utc > ? AND ts_utc < ?", (lo, hi)).fetchone():
            return False
        if row["kind"] == "macro":
            hit = self.j.db.execute("SELECT 1 FROM news_events WHERE kind='macro' AND ts_utc > ? AND ts_utc < ?",
                                    (lo, hi)).fetchone()
        else:
            hit = self.j.db.execute("SELECT 1 FROM news_events WHERE symbol=? AND ts_utc > ? AND ts_utc < ?",
                                    (row["symbol"], lo, hi)).fetchone()
        return hit is None

    def _release_diary(self, row: sqlite3.Row) -> None:
        """One diary line when the last coin of a release is measured."""
        rows = self.j.db.execute("SELECT * FROM news_events WHERE kind='macro' AND ts_utc=? ORDER BY symbol",
                                 (row["ts_utc"],)).fetchall()
        if any(r["settled_at"] is None for r in rows):
            return
        measured = [r for r in rows if r["impulse"] is not None]
        jumps = [r for r in measured if r["impulse"]]
        placebo = [r for r in measured if r["placebo_impulse"] is not None]
        parts = [f"{r['symbol'].removesuffix('USDT')} {r['magnitude_pct']:+.1f}% within "
                 f"{(ms_of(r['peak_ts']) - ms_of(r['ts_utc'])) // MIN_MS if r['peak_ts'] else '?'} min" for r in jumps[:3]]
        # the release's own calendar name ('US CPI, September 2026 data') and its date: a release measured days later
        # (the back-fill after an install or an outage) must not read as today's
        name = clean(str(row["label"] or release_name((row["category"] or "").removeprefix("macro:"))), 120)
        text = (f"{name} ({when_text(ms_of(row['ts_utc']))}) measured for {len(rows)} coin{'s' if len(rows) != 1 else ''}: "
                f"a jump in {len(jumps)}" + (f" ({', '.join(parts)}{', ...' if len(jumps) > 3 else ''})" if parts else ""))
        if placebo:
            text += f"; on the matched normal days {sum(bool(r['placebo_impulse']) for r in placebo)} of {len(placebo)} jumped"
        if len(measured) < len(rows):
            text += f"; {len(rows) - len(measured)} could not be measured"
        self.j.event("events", text)

    # ---- housekeeping ----
    def prune(self, now: dt.datetime) -> None:
        """Once a day: forget headlines older than KEEP_DAYS, settled events older than KEEP_EVENT_DAYS, and headline
        events still unmeasured after WAIT_DAYS (the budget never reached them; counted in events_dropped). Release
        rows wait as long as it takes."""
        today = now.date().isoformat()
        if self.j.get("events_pruned_day") == today:
            return
        now_ms = int(now.timestamp() * 1000)
        self.j.db.execute("DELETE FROM headlines WHERE ts_utc < ?", (iso(now_ms - KEEP_DAYS * DAY_MS),))
        self.j.db.execute("DELETE FROM news_events WHERE settled_at IS NOT NULL AND ts_utc < ?",
                          (iso(now_ms - KEEP_EVENT_DAYS * DAY_MS),))
        cur = self.j.db.execute("DELETE FROM news_events WHERE settled_at IS NULL AND kind='headline' AND ts_utc < ?",
                                (iso(now_ms - WAIT_DAYS * DAY_MS),))
        if cur.rowcount:
            self.j.set("events_dropped", int(self.j.get("events_dropped") or 0) + cur.rowcount)
        self.j.db.commit()
        failing = self.j.get("events_failing") or {}
        if failing:   # forget failed tries of records that are gone or measured
            open_ids = {str(r[0]) for r in self.j.db.execute("SELECT id FROM news_events WHERE settled_at IS NULL")}
            self.j.set("events_failing", {k: v for k, v in failing.items() if k in open_ids})
        self.j.set("events_pruned_day", today)
