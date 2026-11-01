"""Post-mortem & memory agent: what the desk knew when it bought, why each closed trade won or lost, and - only when
the owner adds `memory` to MENTOR_CHECKS - whether similar past trades lost clearly more often than the desk's trades
usually do.

  * The context at entry: one trade_context row per buy (hourly agents and daily trend rule), from data the desk had
    at that moment: finished candles, the chart reader's report, daily closes, the listed US release times, the
    decision's own reports. Nothing later is ever written into it except the "failed breakout" result, which the
    post-mortem uses and the memory never reads.
  * The post-mortem of every closed trade, winners and losers alike, so a cause only counts when it is clearly more
    common among losers than among winners (with the counts).
  * The memory check: past closed trades in the same cell (4-hour regime side, Bitcoin's side of its 200-day average,
    6-hour block of the day, a listed release within 2 hours or not); a buy is refused only when the cell holds at
    least 10 trades and their loss rate is far above the base loss rate of all trades in memory (exact binomial test,
    Šidák-adjusted). The hourly rules lose about two trades in three by design, so "more than 60% lost" is normal.

A measurement and a guard, never a forecast. No new request per check, no AI. Nothing here places an order: with
`memory` absent from MENTOR_CHECKS (the default) nothing computed here reaches a decision.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import json
import logging
import math
import time
from collections import Counter
from pathlib import Path

from .chart import RECENT_BARS, interval_ms, regime_text
from .events import SHIPPED_CALENDAR, iso, load_calendar, ms_of, release_name
from .execution import STOP_SLIPPAGE
from .mentor import sma

log = logging.getLogger(__name__)

FEATURES_VERSION = 1          # in every bucket key: a later change of the cell rule never mixes with old rows
# ---- the context at entry ----
HOUR_BLOCK = 6                # hours per time-of-day block: 00-06, 06-12, 12-18, 18-24 UTC
RELEASE_NEAR_MIN = 120        # "a release within 2 hours" before or after the decision (the event study's 2-hour window)
RELEASE_FAR_DAYS = 7          # minutes to/since a release are stored only within 7 days (else NULL)
NOISE_HOURS = 2               # the "normal 2-hour dip" (Kase's two-bar range)
NOISE_DAYS = 7                # ... measured over the 7 days (168 hourly starts) before the decision
NOISE_QUANTILE = 0.90         # ... the dip reached in 1 of 10 ordinary 2-hour stretches
# ---- the post-mortem ----
STOP_SLIP_LIMIT = 2 * STOP_SLIPPAGE   # 0.4%: a stop exit filled more than twice the 0.2% the replay assumes
ENTRY_GAP_ATR = 0.25          # bought more than a quarter ATR above the signal price (the close the replay buys at; 1/8 of the 2-ATR stop)
DEMO_GAP_LIMIT = 0.005        # the demo exchange filled more than 0.5% away from the real market (the report's yardstick)
PARTIAL_SHARE = 0.98          # bought less than 98% of the planned amount
RELEASE_BEFORE_MIN = 120      # a release up to 2 h before the buy counts: the buy fell inside its measured 2-hour window
CAUSE_MIN_LOSERS, CAUSE_MIN_WINNERS = 20, 10   # below these counts the losers-vs-winners comparison says "too few"
CAUSE_FAMILY_ALPHA = 0.05     # Šidák over the 5 cause families: each comparison at 1 - 0.95**(1/5) = 1.02%
# ---- the memory check ----
MIN_SIMILAR = 10              # similar trades before the rule judges (the scope's value; the test itself needs 10-16)
MIN_POOL = 30                 # trades in memory before a base rate is trusted (±17 points at 30 trades, Wilson)
FAMILY_ALPHA = 0.05           # chance of any false refusal across the cells judged at one check (Šidák; at most 5.13%)
MEMORY_DAYS = 365             # only trades closed in the last 365 days count (a refused cell would otherwise never reopen)
OWNER_RULE_LOSS_SHARE = 0.60  # the owner's literal rule, replay only (--memory-rule owner60)
# ---- the seed ----
SEED_DAYS = 365               # the replay seed covers the memory window
CAUSES = ("execution", "news", "chop", "failed_breakout", "noise")   # the five families compared losers vs winners

HISTORY_CALENDAR = Path(__file__).resolve().parent.parent / "macro_history.json"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
MIN_MS = 60_000
RULES = ("test", "owner60")
CHART_FAMILIES = ("chop", "failed_breakout", "noise")
# the trade_context columns a row may carry (journal.py's SCHEMA)
CONTEXT_COLUMNS = (
    "source", "symbol", "opened_at", "decision_ms", "strategy", "card_id", "features_version", "bucket", "regime_base",
    "regime_4h", "regime_1d", "latest_event", "breakout_level", "breakout_hours", "breakout_failed", "score",
    "technical", "prediction", "news", "forex", "news_source", "rsi", "atr_pct", "stop_pct", "normal_dip",
    "btc_above", "release_near", "mins_to_release", "mins_since_release", "release_name", "hour_utc", "weekday",
    "decision_price", "signal_price", "fill_price", "market_price", "quote_planned", "quote_filled", "closed_at",
    "exit_price", "exit_reason", "pnl", "pnl_pct", "hours", "backfilled", "created_at")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---- the listed US releases (past and future) ----
_RELEASE_CACHE: dict[tuple, tuple] = {}


def _mtime(path: Path | None) -> float | None:
    try:
        return Path(path).stat().st_mtime if path else None
    except OSError:
        return None


def owner_calendar(db_path: str | None = None) -> Path | None:
    """The owner's data/macro_calendar.json next to the journal (the file the event study reads), or None for an
    in-memory journal."""
    if db_path is None:
        from ..config import Settings
        db_path = Settings().db_path
    return None if db_path == ":memory:" else Path(db_path).with_name("macro_calendar.json")


def release_times(owner: Path | None = None, shipped: Path = SHIPPED_CALENDAR,
                  history: Path = HISTORY_CALENDAR) -> tuple[list[tuple[int, str]], tuple[int, int] | None]:
    """((ms, name) sorted, (first_ms, last_ms) or None): every listed release of the history file, the shipped calendar
    and the owner's data/macro_calendar.json, read with events.load_calendar (a row of the owner's file with
    "remove": true deletes the same ts_utc and name). Duplicates (the same time and kind) count once. The coverage runs
    from the history file's covers_from to the end of the last listed date; a broken file is skipped and logged.
    Cached by the three files' modification times (the replay calls it once; the live desk once a check)."""
    key = (str(owner), _mtime(owner), str(shipped), _mtime(shipped), str(history), _mtime(history))
    if key in _RELEASE_CACHE:
        return _RELEASE_CACHE[key]
    rows, covers_from = [], None
    hist_rows, problems, _ = load_calendar(history, None)
    for p in problems:
        log.warning("Release history: %s", p)
    if hist_rows:
        try:
            covers_from = json.loads(Path(history).read_text(encoding="utf-8")).get("covers_from")
            covers_from = ms_of(str(covers_from) + "T00:00:00Z") if covers_from else None
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("Release history: no covers_from (%s)", e)
            covers_from = None
    rows += hist_rows
    cal_rows, problems, _ = load_calendar(shipped, owner)
    for p in problems:
        log.warning("Release calendar: %s", p)
    rows += cal_rows
    removed = {(r["ts_utc"], r["name"]) for r in rows if r["remove"]}
    seen, out = set(), []
    for r in rows:
        if r["remove"] or (r["ts_utc"], r["name"]) in removed:
            continue
        ms = ms_of(r["ts_utc"])
        if (ms, r["kind"]) in seen:
            continue
        seen.add((ms, r["kind"]))
        out.append((ms, release_name(r["kind"], r["name"])))
    out.sort()
    coverage = None
    if out:
        first = covers_from if covers_from is not None else out[0][0] - out[0][0] % DAY_MS
        last_day = out[-1][0] - out[-1][0] % DAY_MS
        coverage = (first, last_day + DAY_MS)
    result = (out, coverage)
    _RELEASE_CACHE.clear()   # one entry: the files rarely change, memory stays flat
    _RELEASE_CACHE[key] = result
    return result


def release_near(releases, coverage, decision_ms: int) -> dict:
    """{"near": "yes" | "no" | "unknown", "to_min": int | None, "since_min": int | None, "name": str | None}.
    near = "yes" when a listed release is at most RELEASE_NEAR_MIN minutes before or after the decision; "unknown"
    when the decision lies outside the calendar's coverage (no list of releases for that time exists). to_min /
    since_min: minutes to the next / since the last listed release when within RELEASE_FAR_DAYS, else None. name: the
    nearer of those two releases (None when neither is within RELEASE_FAR_DAYS)."""
    times = [r[0] for r in releases]
    i = bisect.bisect_left(times, decision_ms)
    nxt = releases[i] if i < len(releases) else None                       # at or after the decision
    j = bisect.bisect_right(times, decision_ms) - 1
    last = releases[j] if j >= 0 else None                                 # at or before the decision
    far = RELEASE_FAR_DAYS * DAY_MS
    to_min = (nxt[0] - decision_ms) // MIN_MS if nxt and nxt[0] - decision_ms <= far else None
    since_min = (decision_ms - last[0]) // MIN_MS if last and decision_ms - last[0] <= far else None
    if coverage is None or not (coverage[0] <= decision_ms < coverage[1]):
        near = "unknown"
    else:
        near = "yes" if ((nxt and nxt[0] - decision_ms <= RELEASE_NEAR_MIN * MIN_MS)
                         or (last and decision_ms - last[0] <= RELEASE_NEAR_MIN * MIN_MS)) else "no"
    name = None
    if to_min is not None and (since_min is None or to_min <= since_min):
        name = nxt[1]
    elif since_min is not None:
        name = last[1]
    return {"near": near, "to_min": to_min, "since_min": since_min, "name": name}


# ---- the context at entry (pure functions; live and replay call exactly these) ----
def decision_time(candles: list[dict], step_ms: int) -> int:
    """The close time of the newest finished candle the decision used (its open time + one candle)."""
    return int(candles[-1]["time"]) + int(step_ms)


def frame_side(frame: dict | None) -> str:
    """'chop', 'up' (trending or impulsive, rising), 'down' (trending or impulsive, falling) or 'unknown'."""
    return side_of_words(regime_text(frame))


def side_of_words(words: str | None) -> str:
    """The same sides from the chart reader's words ('chop', 'trending up', 'impulsive down', 'unknown')."""
    words = str(words or "unknown")
    if words == "chop":
        return "chop"
    if words.endswith(" up"):
        return "up"
    if words.endswith(" down"):
        return "down"
    return "unknown"


def btc_side(symbol: str, daily: list[float], btc_daily: list[float] | None) -> str:
    """'above' / 'below' / 'unknown': the last value against the 200-day simple average, exactly the comparison of the
    checklist's `btc` rule; Bitcoin itself is judged on its own daily closes."""
    series = daily if symbol.startswith("BTC") else btc_daily
    if not series:
        return "unknown"
    ma = sma(series, 200)
    if ma is None:
        return "unknown"
    return "above" if series[-1] > ma else "below"


def hour_block(decision_ms: int) -> str:
    """'00-06', '06-12', '12-18' or '18-24' (UTC) for the moment of the decision."""
    hour = dt.datetime.fromtimestamp(decision_ms / 1000, dt.timezone.utc).hour
    start = hour // HOUR_BLOCK * HOUR_BLOCK
    return f"{start:02d}-{start + HOUR_BLOCK:02d}"


def bucket_key(side_4h: str, btc: str, block: str, near: str) -> str:
    """'v1|4h:chop|btc:above|h:12-18|rel:no' - the cell of the memory (FEATURES_VERSION first)."""
    return f"v{FEATURES_VERSION}|4h:{side_4h}|btc:{btc}|h:{block}|rel:{near}"


def parse_bucket(bucket: str) -> dict:
    out = {}
    for part in str(bucket or "").split("|")[1:]:
        k, _, v = part.partition(":")
        out[k] = v
    return out


SIDE_WORDS = {"chop": "4-hour chart in chop", "up": "4-hour chart rising", "down": "4-hour chart falling",
              "unknown": "4-hour chart not known"}
BTC_WORDS = {"above": "Bitcoin above its 200-day average", "below": "Bitcoin below its 200-day average",
             "unknown": "Bitcoin's side of its 200-day average not known"}
REL_WORDS = {"yes": "a US release within 2 hours", "no": "no US release within 2 hours",
             "unknown": "US release times not known"}


def describe_bucket(bucket: str) -> str:
    """Plain words: '4-hour chart in chop, Bitcoin above its 200-day average, decided 12:00-18:00 UTC, no US release
    within 2 hours'."""
    p = parse_bucket(bucket)
    block = p.get("h", "")
    if "-" in block:
        a, b = block.split("-", 1)
        hours = f"decided {a}:00-{b}:00 UTC"
    else:
        hours = "time of day not known"
    return ", ".join([SIDE_WORDS.get(p.get("4h"), SIDE_WORDS["unknown"]), BTC_WORDS.get(p.get("btc"), BTC_WORDS["unknown"]),
                      hours, REL_WORDS.get(p.get("rel"), REL_WORDS["unknown"])])


def normal_dip(candles: list[dict], step_ms: int) -> float | None:
    """The coin's normal 2-hour dip before the decision: for each of the last NOISE_DAYS*24 hours of starts (in base
    candles: 168 on 1h, 84 on 2h, 42 on 4h; the starts end one window before the newest finished candle, so every
    window ends at or before it - no look-ahead), the largest fall from that candle's close to the lowest low of the
    next max(1, NOISE_HOURS h / step) candles, as a share of the close; returns the NOISE_QUANTILE value (sorted
    ascending, index floor(0.90 x (count - 1))). Uses only the last starts + window candles (170 on 1h), so the live
    window of 199 and the replay window of 200 give the same number. None with fewer candles."""
    if step_ms <= 0:
        return None
    window = max(1, NOISE_HOURS * HOUR_MS // step_ms)
    starts = NOISE_DAYS * 24 * HOUR_MS // step_ms
    if starts < 1 or len(candles) < starts + window:
        return None
    seg = candles[-(starts + window):]
    drops = []
    for k in range(starts):
        close = seg[k]["close"]
        if not close:
            continue
        low = min(c["low"] for c in seg[k + 1:k + 1 + window])
        drops.append((close - low) / close)
    if not drops:
        return None
    drops.sort()
    return drops[int(NOISE_QUANTILE * (len(drops) - 1))]


def breakout_at_entry(report: dict | None, base_interval: str) -> dict | None:
    """{"level", "frame", "hours"} when the chart report's latest_event is a breakout ('1h breakout' or '4h breakout'):
    level = details['level']; hours = RECENT_BARS of that frame x its bar length (6 h for a 1h breakout, 12 h on a 2h
    base, 8 h for a 4h breakout) - as long as the chart reader's own recent window. It is counted from the decision,
    so it can run a few hours past the moment the reader stops calling an older event recent."""
    latest = (report or {}).get("latest_event")
    if not latest or latest.get("kind") != "breakout":
        return None
    level = (latest.get("details") or {}).get("level")
    if level is None:
        return None
    frame = latest.get("timeframe")
    step = interval_ms(base_interval)
    if frame == "4h" or (frame == base_interval and step >= 4 * HOUR_MS):
        hours = RECENT_BARS["4h"] * 4
    else:
        hours = RECENT_BARS["short"] * step // HOUR_MS
    return {"level": float(level), "frame": frame, "hours": int(hours)}


def entry_features(symbol: str, candles: list[dict], price: float, atr: float, stop: float, report: dict | None,
                   daily: list[float], btc_daily: list[float] | None, releases, coverage, interval: str) -> dict:
    """Everything the context row and the memory need, from inputs known at the decision. `candles`: the finished
    candles the decision used (oldest first); `report`: the chart reader's report on them (None: regimes 'unknown');
    `daily` / `btc_daily`: the checklist's daily lists. Returns decision_ms, signal_price (candles[-1]['close'], the
    price the replay buys at), regime_base / regime_4h / regime_1d (chart.regime_text words), side_4h, btc, hour_utc,
    weekday, block, release (release_near), latest_event ('1h breakout' ...), breakout (breakout_at_entry), rsi (the
    base frame's rsi), atr_pct, stop_pct, normal_dip and bucket. Never raises on short input (missing parts become
    None / 'unknown')."""
    step = interval_ms(interval)
    frames = (report or {}).get("timeframes") or {}
    base = frames.get(interval)
    decision_ms = decision_time(candles, step) if candles else None
    out = {"decision_ms": decision_ms, "signal_price": candles[-1]["close"] if candles else None,
           "regime_base": regime_text(base), "regime_4h": regime_text(frames.get("4h")),
           "regime_1d": regime_text(frames.get("1d")), "side_4h": frame_side(frames.get("4h")),
           "btc": btc_side(symbol, daily or [], btc_daily)}
    if decision_ms is not None:
        when = dt.datetime.fromtimestamp(decision_ms / 1000, dt.timezone.utc)
        out.update(hour_utc=when.hour, weekday=when.weekday(), block=hour_block(decision_ms),
                   release=release_near(releases or [], coverage, decision_ms))
    else:
        out.update(hour_utc=None, weekday=None, block="unknown",
                   release={"near": "unknown", "to_min": None, "since_min": None, "name": None})
    latest = (report or {}).get("latest_event")
    out["latest_event"] = f"{latest['timeframe']} {latest['kind']}" if latest else None
    out["breakout"] = breakout_at_entry(report, interval)
    out["rsi"] = base.get("rsi") if base else None
    out["atr_pct"] = atr / price if price and atr else None
    out["stop_pct"] = (price - stop) / price if price and stop is not None else None
    out["normal_dip"] = normal_dip(candles or [], step)
    out["bucket"] = bucket_key(out["side_4h"], out["btc"], out["block"], out["release"]["near"])
    return out


def context_columns(f: dict) -> dict:
    """The trade_context columns of an entry_features() (or trend_features()) result."""
    rel = f.get("release") or {}
    brk = f.get("breakout") or {}
    btc = f.get("btc")
    return {"decision_ms": f.get("decision_ms"), "features_version": FEATURES_VERSION, "bucket": f.get("bucket"),
            "regime_base": f.get("regime_base"), "regime_4h": f.get("regime_4h"), "regime_1d": f.get("regime_1d"),
            "latest_event": f.get("latest_event"), "breakout_level": brk.get("level"),
            "breakout_hours": brk.get("hours"), "rsi": f.get("rsi"), "atr_pct": f.get("atr_pct"),
            "stop_pct": f.get("stop_pct"), "normal_dip": f.get("normal_dip"),
            "btc_above": 1 if btc == "above" else 0 if btc == "below" else None,
            "release_near": rel.get("near"), "mins_to_release": rel.get("to_min"),
            "mins_since_release": rel.get("since_min"), "release_name": rel.get("name"),
            "hour_utc": f.get("hour_utc"), "weekday": f.get("weekday"), "signal_price": f.get("signal_price")}


def trend_features(symbol: str, sig: dict | None, price: float, stop: float, now_ms: int, releases, coverage) -> dict:
    """The daily trend rule's context: decision_ms = 00:00 UTC of today (the close of the finished day), the rule's gap
    to its average, momentum and yearly swing (trend.signal), hour, weekday, release proximity, stop_pct. No chart, no
    bucket: trend trades never enter the hourly memory. Never raises (missing parts become None); the orchestrator
    still wraps the call, because it runs before the trend buy's order."""
    decision_ms = int(now_ms) - int(now_ms) % DAY_MS
    when = dt.datetime.fromtimestamp(decision_ms / 1000, dt.timezone.utc)
    sig = sig or {}
    try:
        gap = sig["close"] / sig["sma"] - 1 if sig.get("sma") else None
    except (TypeError, ZeroDivisionError, KeyError):
        gap = None
    return {"decision_ms": decision_ms, "signal_price": sig.get("close"), "regime_base": None, "regime_4h": None,
            "regime_1d": None, "side_4h": None, "btc": None, "hour_utc": when.hour, "weekday": when.weekday(),
            "block": hour_block(decision_ms), "release": release_near(releases or [], coverage, decision_ms),
            "latest_event": None, "breakout": None, "rsi": None, "atr_pct": sig.get("vol"),
            "stop_pct": (price - stop) / price if price and stop is not None else None, "normal_dip": None,
            "bucket": None, "score": gap, "technical": sig.get("momentum")}


def breakout_failed(candles: list[dict], decision_ms: int, level: float, hours: int,
                    step_ms: int | None = None) -> bool | None:
    """True when a finished candle opening in [decision_ms, decision_ms + hours) closed below `level`; False once the
    last candle of that window is present (or later candles exist) without such a close; None while the window is not
    complete (or its start is no longer in the candles given). The same function judges live candles (each check) and
    replay candles (at the seed). `step_ms`: the candle size (worked out from the candles when not given)."""
    if not candles or level is None or decision_ms is None or not hours:
        return None
    end = int(decision_ms) + int(hours) * HOUR_MS
    if step_ms is None:
        step_ms = candles[-1]["time"] - candles[-2]["time"] if len(candles) >= 2 else HOUR_MS
    times = [c["time"] for c in candles]
    i = bisect.bisect_left(times, decision_ms)
    for c in candles[i:]:
        if c["time"] >= end:
            break
        if c["close"] < level:
            return True
    if candles[0]["time"] > decision_ms:   # the start of the window is not in hand: cannot tell
        return None
    if candles[-1]["time"] + step_ms >= end:
        return False
    return None


# ---- statistics (pure Python, math.comb; no dependency) ----
def binom_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p): the exact sum."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    q = 1.0 - p
    if n <= 1000:
        return min(1.0, sum(math.comb(n, i) * p ** i * q ** (n - i) for i in range(k, n + 1)))
    lp, lq, lg = math.log(p), math.log(q), math.lgamma(n + 1)
    return min(1.0, sum(math.exp(lg - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
                        for i in range(k, n + 1)))


def sidak(alpha: float, m: int) -> float:
    """The level each of m comparisons is judged at so that the chance of any false alarm stays at alpha (Šidák)."""
    return 1 - (1 - alpha) ** (1 / max(1, m))


def fisher_greater(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact test of the table [[a, b], [c, d]] (rows: losers, winners; columns: tagged, not
    tagged): P(at least a of the a + c tagged trades are losers), with every margin fixed."""
    n_all, losers, tagged = a + b + c + d, a + b, a + c
    if tagged == 0 or n_all == 0:
        return 1.0
    total = math.comb(n_all, tagged)
    hits = sum(math.comb(losers, x) * math.comb(n_all - losers, tagged - x)
               for x in range(a, min(tagged, losers) + 1))
    return hits / total


def _two_figures(n: float) -> int:
    if n < 100:
        return int(round(n))
    digits = int(math.floor(math.log10(n))) - 1
    return int(round(n, -digits))


def chance_text(p: float | None) -> str:
    """'about 1 in 450' ('more than 1 in 2' for p > 0.5): how often chance alone gives such a result."""
    if p is None:
        return "not known"
    if p > 0.5:
        return "more than 1 in 2"
    if p < 1e-6:
        return "less than 1 in a million"
    return f"about 1 in {_two_figures(1 / p):,}"


def limit_text(level: float | None) -> str:
    """'1 in 176': a significance level as the owner reads it."""
    return "not known" if not level else f"1 in {int(round(1 / level)):,}"


# ---- the memory check ----
class MemoryBook:
    """The memory at one moment: rows (bucket, closed_ms, loss) of trades closed BEFORE the decision, within
    MEMORY_DAYS. Live: built once per check from the journal (pool_rows). Replay: grows as the replay's own trades
    close (add). verdict() is the one decision function both use."""

    def __init__(self, rows=(), rule: str = "test"):
        if rule not in RULES:
            raise ValueError(f"unknown memory rule {rule!r}")
        self.rows = sorted((dict(r) for r in rows), key=lambda r: r["closed_ms"])
        self.rule = rule
        self._reset()

    def _reset(self) -> None:
        self._hi = self._lo = 0                     # rows[_lo:_hi] are the counted window
        self._cells: Counter = Counter()
        self._cell_losses: Counter = Counter()
        self._n = self._losses = 0
        self._t: int | None = None

    def add(self, row: dict) -> None:
        """A trade that just closed (closed_ms not older than the last added; an older one is filed in order)."""
        row = {"bucket": row.get("bucket"), "closed_ms": int(row["closed_ms"]), "loss": bool(row["loss"])}
        if self.rows and row["closed_ms"] < self.rows[-1]["closed_ms"]:
            keys = [r["closed_ms"] for r in self.rows]
            self.rows.insert(bisect.bisect_right(keys, row["closed_ms"]), row)
            self._reset()
            return
        self.rows.append(row)

    def _advance(self, t: int) -> None:
        """Count rows with closed_ms <= t, forget rows with closed_ms <= t - window. A t earlier than the last one
        rebuilds from zero (never happens in a check or a replay)."""
        if self._t is not None and t < self._t:
            self._reset()
        rows, cut = self.rows, t - MEMORY_DAYS * DAY_MS
        while self._hi < len(rows) and rows[self._hi]["closed_ms"] <= t:
            r = rows[self._hi]
            self._cells[r["bucket"]] += 1
            self._cell_losses[r["bucket"]] += r["loss"]
            self._n += 1
            self._losses += r["loss"]
            self._hi += 1
        while self._lo < self._hi and rows[self._lo]["closed_ms"] <= cut:
            r = rows[self._lo]
            self._cells[r["bucket"]] -= 1
            self._cell_losses[r["bucket"]] -= r["loss"]
            if self._cells[r["bucket"]] <= 0:
                del self._cells[r["bucket"]]
                self._cell_losses.pop(r["bucket"], None)
            self._n -= 1
            self._losses -= r["loss"]
            self._lo += 1
        self._t = t

    def groups(self) -> list[str]:
        """The cells with at least MIN_SIMILAR trades in the current window."""
        return [b for b, n in self._cells.items() if n >= MIN_SIMILAR]

    def cells(self, decision_ms: int) -> dict[str, tuple[int, int]]:
        """{bucket: (trades, losers)} in the window at decision_ms."""
        self._advance(int(decision_ms))
        return {b: (n, self._cell_losses[b]) for b, n in self._cells.items()}

    def verdict(self, bucket: str, decision_ms: int) -> dict:
        """{"ok": True | False | None, "rule", "bucket", "n", "losses", "pool", "pool_losses", "base", "p", "level",
        "groups", "detail"}; ok None (too few trades to judge) never blocks."""
        self._advance(int(decision_ms))
        n, losses = self._cells.get(bucket, 0), self._cell_losses.get(bucket, 0)
        pool, pool_losses = self._n, self._losses
        m = len(self.groups())
        v = {"ok": None, "rule": self.rule, "bucket": bucket, "n": n, "losses": losses, "pool": pool,
             "pool_losses": pool_losses, "base": pool_losses / pool if pool else None, "p": None, "level": None,
             "groups": m}
        if self.rule == "owner60":
            if n >= MIN_SIMILAR:
                v["ok"] = not (losses / n > OWNER_RULE_LOSS_SHARE)
        elif pool >= MIN_POOL and n >= MIN_SIMILAR:
            v["p"] = binom_tail(losses, n, v["base"])
            v["level"] = sidak(FAMILY_ALPHA, m)
            v["ok"] = v["p"] > v["level"]
        from ..postmortem_report import checklist_detail   # the owner's words live with the other texts
        v["detail"] = checklist_detail(v)
        return v


def _ms(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return ms_of(str(text))
    except (TypeError, ValueError):
        return None


def _in(periods, ts: str | None) -> bool:
    return bool(ts) and any(a <= ts and (b is None or ts < b) for a, b in periods or [])


def live_start(journal) -> str | None:
    """The earliest opened_at of a live hourly context row whose bucket has a known 4-hour side: where the desk's own
    record of the memory's cells begins (a backfilled row without a chart picture does not count)."""
    row = journal.db.execute("SELECT MIN(opened_at) FROM trade_context WHERE source='live' AND strategy='hourly' "
                             "AND features_version=? AND bucket IS NOT NULL AND bucket NOT LIKE '%|4h:unknown|%'",
                             (FEATURES_VERSION,)).fetchone()
    return row[0] if row and row[0] else None


def strategy_ids(card) -> tuple[str, ...]:
    """The ids under which the memory counts a seed and live trades as the strategy in force: the card's own and the
    same card with the memory rule switched the other way (a card's id covers its checks). The seed replays the card
    without that rule anyway (a rule must not choose its own evidence: run_seed), so adding or removing `memory` keeps
    the seed and the desk's own record of that strategy."""
    if card is None:
        return ()
    without = card_checks_without_memory(card)
    other = dataclasses.replace(card, checks=without if "memory" in card.checks else without + ("memory",))
    return (card.id, other.id)


def pool_rows(journal, card_id, exclude: list[tuple[str, str | None]]) -> list[dict]:
    """The memory's rows from the journal, split strictly at the live start: live hourly trades fully closed
    (outcome from closed_trades) opened at or after it, plus the replay seed of the card in force opened before it;
    trades opened or closed inside an `exclude` period (the daily trend rule's eras, notes.card_periods(history,
    'trend')) are left out. Sorted by closed_ms. `card_id`: the card in force's id, or its ids (strategy_ids)."""
    ids = (card_id,) if isinstance(card_id, str) else tuple(card_id or ())
    start = live_start(journal)
    closed = {(p["symbol"], p["opened_at"]): p for p in journal.closed_positions()}
    out = []
    for r in journal.db.execute("SELECT symbol, opened_at, bucket, card_id FROM trade_context WHERE source='live' AND "
                                "strategy='hourly' AND features_version=? AND bucket IS NOT NULL", (FEATURES_VERSION,)):
        p = closed.get((r["symbol"], r["opened_at"]))
        if p is None or start is None or r["opened_at"] < start:
            continue
        if _in(exclude, r["opened_at"]) or _in(exclude, p["closed_at"]):
            continue
        out.append({"bucket": r["bucket"], "closed_ms": _ms(p["closed_at"]), "loss": bool(p["loss"]), "source": "live",
                    "opened_ms": _ms(r["opened_at"]), "symbol": r["symbol"], "card_id": r["card_id"]})
    if ids:
        marks = ",".join("?" * len(ids))
        for r in journal.db.execute("SELECT symbol, opened_at, closed_at, bucket, pnl, card_id FROM trade_context "
                                    f"WHERE source='replay' AND card_id IN ({marks}) AND features_version=? AND "
                                    "closed_at IS NOT NULL AND bucket IS NOT NULL", (*ids, FEATURES_VERSION)):
            if start is not None and r["opened_at"] >= start:
                continue
            out.append({"bucket": r["bucket"], "closed_ms": _ms(r["closed_at"]), "loss": (r["pnl"] or 0) <= 0,
                        "source": "replay", "opened_ms": _ms(r["opened_at"]), "symbol": r["symbol"],
                        "card_id": r["card_id"]})
    out = [r for r in out if r["closed_ms"] is not None]
    out.sort(key=lambda r: r["closed_ms"])
    return out


# ---- the post-mortem ----
def _fmt_px(v: float) -> str:
    return f"{v:,.2f}" if v >= 1 else f"{v:.6g}"


def _clock(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%H:%M")


def _span(minutes: float) -> str:
    minutes = int(round(abs(minutes)))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h" + (f" {m} min" if m else "")


def _sources_of(source: str, ctx: dict | None) -> str:
    """'live hourly', 'live trend', 'replay'."""
    if source == "replay":
        return "replay"
    return "live trend" if (ctx or {}).get("strategy") == "trend" else "live hourly"


def judged_families(trade: dict, ctx: dict | None, source: str, coverage) -> list[str]:
    """The cause families that can be judged for this trade (a family not judged is "not judged", never "no"):
    live hourly - all five; live trend - execution and news only; replay - news (releases only), chop, failed
    breakout, noise. News needs the calendar's coverage; the chart families need the entry record's chart picture."""
    kind = _sources_of(source, ctx)
    out = []
    if kind != "replay":
        out.append("execution")
    opened, closed = _ms(trade.get("opened_at")), _ms(trade.get("closed_at"))
    if coverage and opened is not None and closed is not None and \
            coverage[0] <= opened - RELEASE_BEFORE_MIN * MIN_MS and closed < coverage[1]:
        out.append("news")
    if kind == "live trend" or not ctx:
        return out
    if ctx.get("regime_base") not in (None, "unknown") or ctx.get("regime_4h") not in (None, "unknown"):
        out.append("chop")
        if not ctx.get("backfilled"):
            latest = ctx.get("latest_event") or ""
            if not latest.endswith("breakout") or ctx.get("breakout_failed") is not None:
                out.append("failed_breakout")
    if ctx.get("stop_pct") is not None and ctx.get("normal_dip") is not None:
        out.append("noise")
    return out


def causes(trade: dict, ctx: dict | None, sales: list[dict], releases, jumps: list[dict], pending: int,
           source: str) -> list[dict]:
    """The tags of one closed position: [{"family", "kind", "text"}]; [] = no tag. `trade`: the aggregate of its
    closed_trades rows (opened_at, closed_at, pnl, pnl_pct, cost, exit reasons, number of sales); `ctx`: its
    trade_context row or None; `sales`: its SELL orders (price, market_price, reason); `jumps`: settled news_events of
    the coin with impulse=1 in the window; `pending`: unsettled news_events in the window (shown by the texts, never a
    tag). Replay rows ('replay'): execution and headline jumps are not judged."""
    tags: list[dict] = []
    ctx = ctx or {}
    kind = _sources_of(source, ctx)
    opened, closed = _ms(trade.get("opened_at")), _ms(trade.get("closed_at"))

    def tag(family, k, text):
        tags.append({"family": family, "kind": k, "text": text})

    # ---- execution (live only) ----
    if kind != "replay":
        for sale in sales or []:
            reason, ref, px = str(sale.get("reason") or ""), sale.get("market_price"), sale.get("price")
            if reason in ("stop-loss (exchange)", "stop-loss (emulated)") and ref and px:
                slip = (ref - px) / ref
                if slip > STOP_SLIP_LIMIT:
                    tag("execution", "stop_slippage", f"execution: the stop sold {slip:.1%} below its price (the "
                                                      f"replay assumes {STOP_SLIPPAGE:.1%})")
        sig, fill, dec, atr_pct = ctx.get("signal_price"), ctx.get("fill_price"), ctx.get("decision_price"), \
            ctx.get("atr_pct")
        atr = atr_pct * dec if atr_pct and dec else None
        if kind == "live hourly" and sig and fill and atr and (fill - sig) / atr > ENTRY_GAP_ATR:
            tag("execution", "entry_gap", f"execution: bought {(fill - sig) / atr:.2f} ATR above the signal price "
                                          f"(${_fmt_px(fill)} against the finished candle's close ${_fmt_px(sig)}; "
                                          "the replay buys at that close)")
        market = ctx.get("market_price")
        if fill and market and abs(fill / market - 1) > DEMO_GAP_LIMIT:
            tag("execution", "demo_gap", f"execution: the demo exchange filled {abs(fill / market - 1):.1%} away from "
                                         "the real market")
        planned, got = ctx.get("quote_planned"), ctx.get("quote_filled")
        if planned and got is not None and got < PARTIAL_SHARE * planned:
            tag("execution", "partial", f"execution: bought {got / planned:.0%} of the planned amount")
        elif int(trade.get("sales") or 1) > 1:
            tag("execution", "partial", f"execution: sold in {int(trade['sales'])} parts")
    # ---- news and macro ----
    if opened is not None and closed is not None:
        lo = opened - RELEASE_BEFORE_MIN * MIN_MS
        for ms, name in releases or []:
            if lo <= ms <= closed:
                rel = (ms - opened) / MIN_MS
                where = f"{_span(rel)} after the buy" if rel >= 0 else f"{_span(rel)} before the buy"
                tag("news", "release", f"US release during the trade: {name} at {_clock(ms)} UTC, {where}")
            elif ms > closed:
                break
        if kind != "replay":
            for j in jumps or []:
                t = _ms(j.get("ts_utc"))
                if t is None or not (lo <= t <= closed):
                    continue
                mag = j.get("magnitude_pct")
                move = f"{mag:+.1f}%" if mag is not None else "a jump"
                what = (f"headline burst '{str(j.get('label') or '')[:60]}'" if j.get("kind") == "headline"
                        else str(j.get("label") or "a scheduled release"))
                tag("news", "jump", f"{what} at {_clock(t)} UTC: measured jump {move} within 2 h (event study)")
    # ---- structure and noise (hourly entries with a chart picture) ----
    if kind != "live trend" and ctx:
        chop = [f for f, w in (("hourly", ctx.get("regime_base")), ("4-hour", ctx.get("regime_4h"))) if w == "chop"]
        if chop:
            tag("chop", "chop", f"chop at entry: the {' and the '.join(chop)} chart{'s were' if len(chop) > 1 else ' was'} "
                                "going nowhere")
        latest = ctx.get("latest_event") or ""
        if latest.endswith("breakout") and ctx.get("breakout_failed") == 1 and ctx.get("breakout_level"):
            tag("failed_breakout", "failed_breakout",
                f"failed breakout: bought on the {latest.split()[0]} break above ${_fmt_px(ctx['breakout_level'])}, "
                f"closed back below it within {int(ctx.get('breakout_hours') or 0)} h of the buy")
        stop_pct, dip = ctx.get("stop_pct"), ctx.get("normal_dip")
        if stop_pct is not None and dip is not None and stop_pct <= dip:
            tag("noise", "noise", f"inside normal noise: the stop was {stop_pct:.1%} away; the coin dipped {dip:.1%} or "
                                  "more in 1 of 10 two-hour stretches of the week before")
    return tags


def _sales_of(orders: list[dict], rows: list[dict]) -> list[dict]:
    """The SELL order of each closed_trades row: same reason, at or after the buy, the nearest in time to the sale."""
    out = []
    for r in rows:
        ms = _ms(r["closed_at"])
        cands = [o for o in orders if o["reason"] == r["exit_reason"] and o["ts"] >= r["opened_at"]]
        if not cands or ms is None:
            out.append({"price": r.get("exit_price"), "market_price": None, "reason": r["exit_reason"]})
            continue
        best = min(cands, key=lambda o: abs((_ms(o["ts"]) or 0) - ms))
        out.append({"price": best["avg_price"], "market_price": best["market_price"], "reason": best["reason"]})
    return out


def postmortems(journal, releases, limit: int | None = None, source: str = "live",
                exclude=None, coverage=None) -> list[dict]:
    """Newest first: {"trade", "ctx", "tags", "loss", "judged": [families judged], "pending", "source"} for each fully
    closed position (source 'live') or each trade of the replay seed (source 'replay')."""
    items = []
    if source == "replay":
        rows = [dict(r) for r in journal.db.execute("SELECT * FROM trade_context WHERE source='replay' AND closed_at IS "
                                                    "NOT NULL ORDER BY closed_at DESC, id DESC")]
        for ctx in rows[:limit] if limit else rows:
            trade = {"symbol": ctx["symbol"], "opened_at": ctx["opened_at"], "closed_at": ctx["closed_at"],
                     "pnl": ctx["pnl"], "pnl_pct": ctx["pnl_pct"], "cost": ctx["quote_filled"],
                     "exit_reasons": [ctx["exit_reason"]], "sales": 1}
            tags = causes(trade, ctx, [], releases, [], 0, "replay")
            items.append({"trade": trade, "ctx": ctx, "tags": tags, "loss": (ctx["pnl"] or 0) <= 0,
                          "judged": judged_families(trade, ctx, "replay", coverage), "pending": 0, "source": "replay"})
        return items
    positions = journal.closed_positions()
    positions = [p for p in positions if not (_in(exclude, p["opened_at"]) or _in(exclude, p["closed_at"]))]
    positions.sort(key=lambda p: (p["closed_at"], p["opened_at"]), reverse=True)
    if limit:
        positions = positions[:limit]
    if not positions:
        return []
    ctxs = {(r["symbol"], r["opened_at"]): dict(r)
            for r in journal.db.execute("SELECT * FROM trade_context WHERE source='live'")}
    rows: dict[tuple, list[dict]] = {}
    for r in journal.db.execute("SELECT * FROM closed_trades ORDER BY id"):
        rows.setdefault((r["symbol"], r["opened_at"]), []).append(dict(r))
    symbols = sorted({p["symbol"] for p in positions})
    marks = ",".join("?" * len(symbols))
    orders: dict[str, list[dict]] = {}
    for o in journal.db.execute(f"SELECT ts, symbol, avg_price, market_price, reason FROM orders WHERE side='SELL' AND "
                                f"ok=1 AND symbol IN ({marks})", symbols):
        orders.setdefault(o["symbol"], []).append(dict(o))
    news: dict[str, list[dict]] = {}
    for e in journal.db.execute(f"SELECT kind, ts_utc, symbol, label, impulse, magnitude_pct, settled_at FROM "
                                f"news_events WHERE kind IN ('headline', 'macro') AND symbol IN ({marks})", symbols):
        news.setdefault(e["symbol"], []).append(dict(e))
    for p in positions:
        ctx = ctxs.get((p["symbol"], p["opened_at"]))
        sales = _sales_of(orders.get(p["symbol"], []), rows.get((p["symbol"], p["opened_at"]), []))
        opened, closed = _ms(p["opened_at"]), _ms(p["closed_at"])
        lo = (opened or 0) - RELEASE_BEFORE_MIN * MIN_MS
        window = [e for e in news.get(p["symbol"], []) if (t := _ms(e["ts_utc"])) is not None and lo <= t <= (closed or 0)]
        jumps = [e for e in window if e["settled_at"] and e["impulse"] == 1]
        pending = sum(1 for e in window if not e["settled_at"])
        tags = causes(p, ctx, sales, releases, jumps, pending, "live")
        items.append({"trade": p, "ctx": ctx, "tags": tags, "loss": bool(p["loss"]),
                      "judged": judged_families(p, ctx, "live", coverage), "pending": pending, "source": "live"})
    return items


def cause_mix(items: list[dict]) -> dict:
    """Per family: losers with it, winners with it, losers and winners judged, the one-sided Fisher p, the Šidák level
    and the verdict word ('too_few' | 'not_a_cause' | 'within_chance' | 'more_among_losers'); plus no-tag counts."""
    level = sidak(CAUSE_FAMILY_ALPHA, len(CAUSES))
    out: dict = {"families": {}, "level": level, "trades": len(items),
                 "losers": sum(1 for i in items if i["loss"]), "winners": sum(1 for i in items if not i["loss"])}
    for fam in CAUSES:
        lj = [i for i in items if i["loss"] and fam in i["judged"]]
        wj = [i for i in items if not i["loss"] and fam in i["judged"]]
        a = sum(1 for i in lj if any(t["family"] == fam for t in i["tags"]))
        c = sum(1 for i in wj if any(t["family"] == fam for t in i["tags"]))
        p = fisher_greater(a, len(lj) - a, c, len(wj) - c) if lj or wj else None
        share_l = a / len(lj) if lj else 0.0
        share_w = c / len(wj) if wj else 0.0
        if len(lj) < CAUSE_MIN_LOSERS or len(wj) < CAUSE_MIN_WINNERS:
            verdict = "too_few"
        elif share_l <= share_w:
            verdict = "not_a_cause"
        elif p is not None and p <= level:
            verdict = "more_among_losers"
        else:
            verdict = "within_chance"
        out["families"][fam] = {"losers": a, "losers_judged": len(lj), "winners": c, "winners_judged": len(wj),
                                "p": p, "verdict": verdict}
    out["losers_no_tag"] = sum(1 for i in items if i["loss"] and not i["tags"])
    out["winners_no_tag"] = sum(1 for i in items if not i["loss"] and not i["tags"])
    return out


# ---- seed and backfill ----
def seed_rows(records: list[dict], card_id: str, interval: str) -> list[dict]:
    """Replay trade records (Backtest(record=True)) -> trade_context rows labelled 'replay', outcome columns filled."""
    out, now = [], _now_iso()
    for r in records:
        f = r.get("ctx") or {}
        row = {"source": "replay", "symbol": r["symbol"], "opened_at": iso(f.get("decision_ms") or r["opened"]),
               "strategy": "hourly", "card_id": card_id, **context_columns(f),
               "breakout_failed": None if r.get("breakout_failed") is None else int(bool(r["breakout_failed"])),
               "score": r.get("score"), "technical": r.get("technical"), "prediction": r.get("prediction"),
               "news": 0.0, "forex": 0.0, "news_source": "replay (no news)", "decision_price": r.get("entry"),
               "fill_price": r.get("entry"), "market_price": None, "quote_planned": r.get("cost"),
               "quote_filled": r.get("cost"), "closed_at": iso(r["closed_ms"]), "exit_price": r.get("exit"),
               "exit_reason": r.get("reason"), "pnl": r.get("pnl"), "pnl_pct": r.get("pnl_pct"), "hours": r.get("hours"),
               "backfilled": 0, "created_at": now}
        out.append(row)
    return out


def card_checks_without_memory(card) -> tuple:
    return tuple(c for c in card.checks if c != "memory")


class SeedInterrupted(Exception):
    """The desk is stopping (an update or a reboot) during the seed's downloads: nothing is recorded (no failure, no
    'tried today'), so the seed runs again after the restart, as the weekly lab does after LabInterrupted."""


def seed_span(journal, now_ms: int, days: int = SEED_DAYS) -> tuple[int, int]:
    """(until_ms, replay days) of the seed: it ends at the live start (the desk's own record begins there; now when
    there is none yet) and covers only the part of the last `days` before it, so every replay trade counts in the
    memory when it is made (MemoryBook keeps MEMORY_DAYS) and the download stays `days` long however old the desk's
    record is. 0 days once that record covers the whole window: no replay is needed."""
    start = live_start(journal)
    until = min(int(now_ms), _ms(start)) if start else int(now_ms)
    age = max(0, math.ceil((now_ms - until) / DAY_MS))
    return until, max(0, days - age)


def run_seed(base, card, journal, client, coins: list[str], now_ms: int, days: int = SEED_DAYS,
             fetch=None, should_stop=None) -> dict:
    """Fill the memory: replay the strategy in force (`card` on the owner's settings `base`, with every checklist rule
    of the card except `memory` - a rule must not choose its own evidence) over the part of the last `days` before the
    live start (seed_span), on `coins`, and swap its closed trades in as 'replay' rows (journal.replace_seed).
    `fetch(symbol, days_back)` returns hourly candles (default: backtest.fetch_history on `client`). Returns the state
    record memory_seed; a failure keeps the previous seed and records the reason. When the desk's own record already
    covers the window, nothing is downloaded or replayed: the seed is recorded as not needed (0 trades). A stop of the
    desk during the downloads raises SeedInterrupted and records nothing."""
    from ..backtest import Backtest, fetch_history
    from .decision import DecisionAgent
    started = time.process_time()
    today = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).date().isoformat()
    previous = journal.get("memory_seed") or {}
    until, span = seed_span(journal, now_ms, days)
    s = dataclasses.replace(card.settings(base), mentor_checks=card_checks_without_memory(card))
    try:
        if span <= 0:   # the desk's own trades fill the whole window: a replay would add nothing the memory counts
            meta = {"card_id": card.id, "ts": _now_iso(), "days": 0, "coins": list(coins), "until": iso(until),
                    "trades": 0, "losers": 0, "cpu_s": 0.0, "error": None, "tried_day": today, "not_needed": True}
            journal.replace_seed([], meta)
            return meta
        back = span + max(0, math.ceil((now_ms - until) / DAY_MS))   # = days: from the window's start up to now
        fetch = fetch or (lambda sym, days_back: fetch_history(client, sym, days_back, s.interval))
        history = {}
        for sym in coins:
            if should_stop and should_stop():
                raise SeedInterrupted()
            history[sym] = fetch(sym, back)
        btc = history.get("BTCUSDT") or fetch("BTCUSDT", back)
        bt = Backtest(s, history, decision=DecisionAgent.for_card(card), days=span, end_ms=until, btc_history=btc,
                      record=True, releases=release_times(owner_calendar(getattr(base, "db_path", None))))
        result = bt.run()
        rows = seed_rows(result.get("trade_records") or [], card.id, s.interval)
        meta = {"card_id": card.id, "ts": _now_iso(), "days": span, "coins": list(coins), "until": iso(until),
                "trades": len(rows), "losers": sum(1 for r in rows if (r["pnl"] or 0) <= 0),
                "cpu_s": round(time.process_time() - started, 1), "error": None, "tried_day": today}
        journal.replace_seed(rows, meta)
        return meta
    except SeedInterrupted:
        log.info("Memory seed interrupted: the desk is stopping; it runs again after the restart")
        raise
    except Exception as e:  # noqa: BLE001 - the desk trades normally without a seed
        log.exception("Memory seed failed")
        meta = {**previous, "error": str(e)[:300], "tried_day": today, "ts_failed": _now_iso()}
        try:
            journal.set("memory_seed", meta)
        except Exception:  # noqa: BLE001
            log.exception("Recording the failed memory seed failed")
        return meta


def _checklist_side(symbol: str, checklist: str) -> int | None:
    """1 / 0 / None from a stored checklist summary: the `btc` line ('✓ btc' above, '✗ btc' below); for Bitcoin itself
    the `btc` line always passes, so its side comes from the `trend200` line."""
    rule = "trend200" if symbol.startswith("BTC") else "btc"
    for line in str(checklist or "").splitlines():
        line = line.strip()
        if line.startswith(f"✓ {rule}:"):
            return 1
        if line.startswith(f"✗ {rule}:"):
            return 0
    return None


def backfill(journal, releases, coverage) -> int:
    """Once, at the first start of 1.9.1: a 'live' context row (backfilled=1) for every position without one - closed,
    or still open at the upgrade (its post-mortem comes when it is sold) - built only from what the desk stored at its
    entry decision (a BUY with risk_approved=1 within one hour before opened_at): the scores, the news source, the
    technical price/ATR/RSI, the chart regime words when present, the checklist's Bitcoin line, the decision time
    floored to the candle size (the stored chart picture names the card's base frame: 1h, 2h, or only 4h for a 4-hour
    card; one hour without a picture), the BUY order's fill. No candle is read; the breakout, stop and dip fields stay
    empty ("not judged"). Returns the rows written. Idempotent."""
    from ..notes import champion_at
    have = {(r["symbol"], r["opened_at"]) for r in journal.db.execute("SELECT symbol, opened_at FROM trade_context "
                                                                      "WHERE source='live'")}
    hist = journal.get("strategy_history") or []
    written = 0
    for p in journal.closed_positions() + list(journal.positions().values()):
        if (p["symbol"], p["opened_at"]) in have:
            continue
        d = journal.db.execute("SELECT * FROM decisions WHERE symbol=? AND action='BUY' AND risk_approved=1 AND ts<=? "
                               "ORDER BY id DESC LIMIT 1", (p["symbol"], p["opened_at"])).fetchone()
        if not d:
            continue
        d_ms, o_ms = _ms(d["ts"]), _ms(p["opened_at"])
        if d_ms is None or o_ms is None or o_ms - d_ms > HOUR_MS:
            continue
        try:
            reports = json.loads(d["reports"] or "{}")
        except (TypeError, ValueError):
            reports = {}
        tech = reports.get("technical") or {}
        chart = reports.get("chart") if isinstance(reports.get("chart"), dict) else {}
        trend_mode = d["source"] == "trend"
        # the close of the newest finished candle at the decision, as live (decision_time): a 4-hour card decides on
        # the 4-hour candles, whose brief has no 1h/2h frame
        step = interval_ms("1h" if "1h" in chart else "2h" if "2h" in chart else "4h" if "4h" in chart else "1h")
        decision_ms = d_ms - d_ms % step
        when = dt.datetime.fromtimestamp(decision_ms / 1000, dt.timezone.utc)
        rel = release_near(releases or [], coverage, decision_ms)
        base_words = next((chart[k] for k in ("1h", "2h") if k in chart), None)
        regime_4h = chart.get("4h")
        btc = None if trend_mode else _checklist_side(p["symbol"], (reports.get("mentor") or {}).get("checklist"))
        price, atr = tech.get("price"), tech.get("atr")
        order = journal.db.execute("SELECT avg_price, market_price, quote, ts FROM orders WHERE symbol=? AND side='BUY' "
                                   "AND ok=1 AND ts<=? ORDER BY id DESC LIMIT 1", (p["symbol"], p["opened_at"])).fetchone()
        if order and (o_ms - (_ms(order["ts"]) or 0)) > HOUR_MS:
            order = None
        who = champion_at(hist, p["opened_at"])
        side = side_of_words(regime_4h) if regime_4h else "unknown"
        btc_word = "above" if btc == 1 else "below" if btc == 0 else "unknown"
        row = {"source": "live", "symbol": p["symbol"], "opened_at": p["opened_at"], "decision_ms": decision_ms,
               "strategy": "trend" if trend_mode else "hourly",
               "card_id": "trend" if trend_mode else (who or {}).get("id"), "features_version": FEATURES_VERSION,
               "bucket": None if trend_mode else bucket_key(side, btc_word, hour_block(decision_ms), rel["near"]),
               "regime_base": base_words, "regime_4h": regime_4h, "regime_1d": chart.get("1d"),
               "score": d["combined_score"], "technical": tech.get("score"),
               "prediction": (reports.get("prediction") or {}).get("score"),
               "news": (reports.get("news") or {}).get("score"), "forex": (reports.get("forex") or {}).get("score"),
               "news_source": (reports.get("news") or {}).get("source"), "rsi": tech.get("rsi"),
               "atr_pct": atr / price if atr and price else None, "btc_above": btc,
               "release_near": rel["near"], "mins_to_release": rel["to_min"], "mins_since_release": rel["since_min"],
               "release_name": rel["name"], "hour_utc": when.hour, "weekday": when.weekday(), "decision_price": price,
               "signal_price": None, "fill_price": order["avg_price"] if order else None,
               "market_price": order["market_price"] if order else None,
               "quote_filled": order["quote"] if order else None, "backfilled": 1, "created_at": _now_iso()}
        if journal.add_trade_context(row) is not None:
            written += 1
    return written
