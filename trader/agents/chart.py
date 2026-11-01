"""Chart Events agent: the market picture per coin from the candles the desk already has.

Three timeframes - the desk's candles (hourly by default), 4-hour bars built from them, and the daily closes - each
get a regime (chop / trending / impulsive), the structural events of the latest finished bars (average crossovers,
breakouts and breakdowns, unusually big candles, new highs with weaker momentum or thinner volume) and the nearest
support and resistance. Everything is computed from finished candles only, with a fixed number of candles, so the
replay (trader/backtest.py) and the live desk see exactly the same picture. No network, no AI.

The regime is Kaufman's efficiency ratio: how far the price went over the last 20 bars divided by the length of the
path it took. 1.0 is a straight line, near 0 is going back and forth. A pure random walk averages about 0.22, so below
0.30 is "chop" (going nowhere); at or above 0.60 with a move of at least 3 ATR it is "impulsive" (a fast move);
everything in between is "trending" (a steady direction).

Nothing here places an order: the report is one more analyst report for the Portfolio Manager, the owner reads it with
/chart, and the optional `regime` checklist rule (trader/agents/mentor.py) is the only place it can refuse a buy.
"""
from __future__ import annotations

import datetime as dt

from .mentor import fmt
from .technical import ema

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
FOUR_H_MS = 4 * HOUR_MS
TRIM = 192            # base candles used: live has 199 finished ones, the replay 200 - the last 192 are the same on both sides
ER_BARS = 20          # the efficiency ratio looks at the last 20 bars of each timeframe
CHOP_ER = 0.30        # below: chop (a pure random walk scores about 0.22 on average)
IMPULSIVE_ER = 0.60   # at or above, together with IMPULSIVE_MOVE_ATR: impulsive
IMPULSIVE_MOVE_ATR = 3.0   # ... and the 20-bar net move is at least 3 ATR (a tiny but straight drift is not an impulse)
SWING_HOURS = {"short": 48, "4h": 120}  # support/resistance and "new high" windows in HOURS: 2 days on a 1h/2h base chart, 5 days on 4h
RECENT_BARS = {"short": 6, "4h": 2}     # how many finished bars back the events are evaluated (the journal dedupes repeats)
# a 4h base (CANDLE_INTERVAL=4h or a 4h strategy card) has one frame and uses the "4h" entries: 120 h = 30 bars, 2 recent bars
RSI_PERIOD = 14       # rsi_series / atr_series period; a divergence needs the previous peak at index >= RSI_PERIOD
IMPULSE_ATR = 2.5     # a candle whose range is above 2.5 x the ATR of the 14 bars before it
RSI_GAP = 5.0         # bearish divergence: RSI at the new high at least 5 points below the RSI at the previous peak
MIN_PEAK_GAP = 3      # the previous peak must be at least 3 bars before the new high (a real swing, not the neighbour)
VOLUME_RATIO = 0.7    # exhaustion: 3-bar average volume at the new high below 70% of that around the previous peak
KINDS = ("cross_up", "cross_down", "breakout", "breakdown", "impulse_up", "impulse_down", "rsi_divergence",
         "volume_exhaustion")
PRIORITY = ("breakdown", "breakout", "cross_down", "cross_up", "impulse_down", "impulse_up", "rsi_divergence",
            "volume_exhaustion")           # which event of one bar counts as "the" event (for latest_event)
NOTABLE = ("cross_up", "cross_down", "breakout", "breakdown")   # what reaches the diary (4h and daily only)
REGIME_WORDS = {"chop": "going nowhere", "trending": "a steady direction", "impulsive": "a fast move"}


# ---- helpers (pure functions, no state) ----
def interval_ms(interval: str) -> int:
    """'1h' -> 3_600_000, '2h', '4h' (the desk's CANDLE_INTERVAL / a strategy card's interval); '15m', '1d' too."""
    text = str(interval).strip().lower()
    try:
        number = int(text[:-1])
    except ValueError:
        return HOUR_MS
    unit = {"m": 60_000, "h": HOUR_MS, "d": DAY_MS}.get(text[-1:], HOUR_MS)
    return max(1, number) * unit


def daily_missing_reason(checklist_on: bool, held: bool, btc_rule: bool = False) -> str:
    """Why a coin has no daily frame, in words: the desk downloads daily prices only where the checklist does.
    `btc_rule`: the coin is Bitcoin and the checklist's `btc` rule is on; that rule reads Bitcoin's daily prices for the
    alts it judges (orchestrator._btc_rule_closes, at most once an hour), held or not, so a held Bitcoin misses its
    daily frame at a check where that copy has expired or was not made yet (before the check's own read), and at every
    check while the desk holds all the coins it watches (no alt is judged, so nothing reads Bitcoin's daily prices)."""
    if not checklist_on:
        return "not available (the checklist is off, so no daily prices are downloaded)"
    if held and btc_rule:
        return ("not refreshed at this check (while the btc rule is on, the desk reads Bitcoin's daily prices at most "
                "once an hour, at checks where it judges a coin it does not hold)")
    if held:
        return "not refreshed while the coin is held (the desk downloads no extra prices for it)"
    return "no daily prices yet"


def aggregate(candles: list[dict], step_ms: int, candle_step: int | None = None) -> list[dict]:
    """Bigger bars from smaller finished candles, aligned to UTC multiples of `step_ms` (a 4h bar opens at 00:00,
    04:00, ... UTC). A bar is kept only when the window holds both its first and its last candle (open time == bar
    start, and open time == bar start + step - candle step), so a partial first bar and the still-forming last bar
    are dropped. open = first open, high = max, low = min, close = last close, volume = sum, time = bar start.
    (Exchange candles open on the hour; the offline synthetic ones carry the start-up second, so the test is "within
    the first candle step" of the bar start, which is exact for aligned candles.)"""
    if len(candles) < 2:
        return []
    if candle_step is None:   # callers that know their candle size pass it: a missing hour must not change the step
        candle_step = candles[1]["time"] - candles[0]["time"]
    if candle_step <= 0 or step_ms % candle_step:
        return []
    out: list[dict] = []
    cur: dict | None = None
    for c in candles:
        start = c["time"] - c["time"] % step_ms
        if cur is None or cur["time"] != start:
            if cur is not None:
                out.append(cur)
            cur = {"time": start, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
                   "volume": c["volume"], "_first": c["time"], "_last": c["time"]}
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c["volume"]
            cur["_last"] = c["time"]
    if cur is not None:
        out.append(cur)
    whole = []
    for b in out:
        if b["_first"] < b["time"] + candle_step and b["_last"] >= b["time"] + step_ms - candle_step:
            whole.append({k: v for k, v in b.items() if not k.startswith("_")})
    return whole


def rsi_series(closes: list[float], period: int = RSI_PERIOD) -> list[float]:
    """RSI at every bar, Wilder smoothing exactly as technical.rsi (the last value equals rsi(closes)).
    Bars before `period` get 50.0 (neutral)."""
    n = len(closes)
    out = [50.0] * n
    if n < 2:
        return out
    avg_g = avg_l = 0.0
    for i in range(1, min(period, n - 1) + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            avg_g += d
        elif d < 0:
            avg_l += -d
    avg_g /= period
    avg_l /= period
    out[min(period, n - 1)] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr_series(candles: list[dict], period: int = RSI_PERIOD) -> list[float | None]:
    """ATR at every bar: the simple mean of the last `period` true ranges up to and including bar i, as
    technical.atr computes its one value (the last value equals atr(candles)). None before enough bars."""
    n = len(candles)
    out: list[float | None] = [None] * n
    trs = [0.0] * n
    for i in range(1, n):
        prev, c = candles[i - 1], candles[i]
        trs[i] = max(c["high"] - c["low"], abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"]))
    total = 0.0
    for i in range(1, n):
        total += trs[i]
        if i > period:
            total -= trs[i - period]
        if i >= period:
            out[i] = total / period
    return out


def efficiency_ratio(closes: list[float], n: int = ER_BARS) -> float | None:
    """Kaufman's efficiency ratio of the last n bars: |close[-1] - close[-1-n]| / sum(|close[k] - close[k-1]|).
    1.0 = a straight line, near 0 = going nowhere. None with fewer than n + 1 closes; 0.0 when the path length is 0
    (a perfectly flat window counts as chop, never a division error)."""
    if len(closes) < n + 1:
        return None
    last = len(closes) - 1
    path = sum(abs(closes[k] - closes[k - 1]) for k in range(last - n + 1, last + 1))
    return abs(closes[last] - closes[last - n]) / path if path else 0.0


def regime(closes: list[float], atr: float | None, n: int = ER_BARS) -> dict | None:
    """{"label": "chop" | "trending" | "impulsive", "direction": +1 | -1 | 0, "er": 0.41}; None when unknown."""
    er = efficiency_ratio(closes, n)
    if er is None:
        return None
    net = closes[-1] - closes[-1 - n]
    if er < CHOP_ER:
        label = "chop"
    elif er >= IMPULSIVE_ER and atr and abs(net) >= IMPULSIVE_MOVE_ATR * atr:
        label = "impulsive"
    else:
        label = "trending"
    return {"label": label, "direction": 1 if net > 0 else -1 if net < 0 else 0, "er": round(er, 3)}


def finished_daily(rows: list[dict], last_candle_time_ms: int) -> list[float]:
    """Closes of the UTC days finished before the day of the last finished base candle (the replay's list exactly:
    the still-forming day and the day the candle belongs to are left out)."""
    return [r["close"] for r in rows if r["time"] + DAY_MS <= last_candle_time_ms]


def _window_word(hours: int | None) -> str:
    if not hours:
        return "recent"
    return f"{hours // 24}-day" if hours % 24 == 0 else f"{hours}-hour"


def phrase(event: dict) -> str:
    """Plain language for one event, e.g. 'broke above the 5-day high at $81.20'."""
    kind, d = event["kind"], event.get("details") or {}
    w = _window_word(d.get("swing_hours"))
    if kind in ("cross_up", "cross_down"):
        if event.get("timeframe") == "1d":
            return ("50-day average crossed above the 200-day (golden cross)" if kind == "cross_up"
                    else "50-day average crossed below the 200-day (death cross)")
        return "12/26 averages crossed " + ("up" if kind == "cross_up" else "down")
    if kind == "breakout":
        return f"broke above the {w} high at ${fmt(d['level'])}" if "level" in d else f"broke above the {w} high"
    if kind == "breakdown":
        return f"broke below the {w} low at ${fmt(d['level'])}" if "level" in d else f"broke below the {w} low"
    if kind in ("impulse_up", "impulse_down"):
        way = "up" if kind == "impulse_up" else "down"
        return f"a big {way} candle ({d['ratio']:.1f}x the usual range)" if "ratio" in d else f"a big {way} candle"
    if kind == "rsi_divergence":
        return "new high with weaker momentum (RSI divergence)"
    if kind == "volume_exhaustion":
        return "new high on thinner volume"
    return kind.replace("_", " ")


def regime_text(frame: dict | None) -> str:
    """'trending up', 'impulsive down' or 'chop' for one frame ('unknown' when it has no regime yet)."""
    if not frame or not frame.get("regime"):
        return "unknown"
    if frame["regime"] == "chop":
        return "chop"
    return f"{frame['regime']} {'up' if frame.get('direction', 0) > 0 else 'down'}"


def frame_name(label: str) -> str:
    """The owner's word for a frame: '4h', '1h' (or '2h'), 'daily'."""
    return "daily" if label == "1d" else label


class ChartEventsAgent:
    name = "chart"

    def __init__(self, interval: str = "1h"):
        """`interval` is the desk's candle size (Settings.interval / a card's interval: 1h, 2h or 4h). The 4-hour
        frame is built only when the base is shorter than 4 hours."""
        self.interval = interval
        self.step = interval_ms(interval)

    def run(self, candles: list[dict], daily: list[float] | None = None) -> dict:
        """`candles`: finished base candles, oldest first (the last TRIM are used). `daily`: closes of finished UTC
        days (finished_daily() live, DailySeries in the replay), or None when unavailable. Never asks the network and
        never raises on short input: with too few candles every frame is None."""
        bars = list(candles[-TRIM:])
        frames: dict[str, dict | None] = {}
        if self.step >= FOUR_H_MS:
            hours, recent = SWING_HOURS["4h"], RECENT_BARS["4h"]
        else:
            hours, recent = SWING_HOURS["short"], RECENT_BARS["short"]
        frames[self.interval] = self._frame(self.interval, bars, hours * HOUR_MS // self.step, recent, hours)
        if self.step < FOUR_H_MS:
            frames["4h"] = self._frame("4h", aggregate(bars, FOUR_H_MS, self.step), SWING_HOURS["4h"] * HOUR_MS // FOUR_H_MS,
                                       RECENT_BARS["4h"], SWING_HOURS["4h"])
        frames["1d"] = self._daily(daily, bars[-1]["time"]) if daily and bars else None
        steps = {self.interval: self.step, "4h": FOUR_H_MS, "1d": DAY_MS}
        events = [e for f in frames.values() if f for e in f["events"]]
        # newest first: by the time the bar ended, the longer frame first on a tie, then the kind's priority
        events.sort(key=lambda e: (-(e["bar_time"] + steps[e["timeframe"]]), -steps[e["timeframe"]],
                                   PRIORITY.index(e["kind"])))
        latest = next((e for e in events if e["timeframe"] != "1d"), None)
        report = {"interval": self.interval, "timeframes": frames, "events": events, "latest_event": latest}
        report["summary"] = self.summary(report)
        return report

    def _frame(self, label: str, bars: list[dict], swing: int, recent: int, swing_hours: int) -> dict | None:
        """One timeframe's picture (None with fewer than ER_BARS + 1 bars). `swing` is the window in bars, `swing_hours`
        the same window in hours (for the texts: "2-day", "5-day")."""
        n = len(bars)
        if n < ER_BARS + 1:
            return None
        closes = [b["close"] for b in bars]
        ema12, ema26 = ema(closes, 12), ema(closes, 26)
        rsi, atr = rsi_series(closes), atr_series(bars)
        reg = regime(closes, atr[-1])
        events = []
        for i in range(max(0, n - recent), n):
            for e in self._events_at(bars, i, closes, ema12, ema26, rsi, atr, swing):
                e["timeframe"] = label
                e["details"]["swing_hours"] = swing_hours
                events.append(e)
        window = bars[-swing:]
        return {"label": label, "bars": n, "bar_time": bars[-1]["time"], "price": closes[-1],
                "regime": reg["label"] if reg else None, "direction": reg["direction"] if reg else 0,
                "er": reg["er"] if reg else None, "ema12": ema12[-1], "ema26": ema26[-1], "rsi": rsi[-1],
                "atr": atr[-1], "support": min(b["low"] for b in window), "resistance": max(b["high"] for b in window),
                "swing_bars": swing, "swing_hours": swing_hours, "events": events}

    @staticmethod
    def _events_at(bars: list[dict], i: int, closes: list[float], ema12: list[float], ema26: list[float],
                   rsi: list[float], atr: list[float | None], swing: int) -> list[dict]:
        """The events on bar i (series known up to i), in KINDS order. `swing` = N, the frame's window in bars."""
        out: list[dict] = []
        b, price = bars[i], closes[i]

        def add(kind: str, **details) -> None:
            out.append({"kind": kind, "bar_time": b["time"], "price": price, "details": details})

        if i >= 1:
            if ema12[i] > ema26[i] and ema12[i - 1] <= ema26[i - 1]:
                add("cross_up", ema12=ema12[i], ema26=ema26[i])
            elif ema12[i] < ema26[i] and ema12[i - 1] >= ema26[i - 1]:
                add("cross_down", ema12=ema12[i], ema26=ema26[i])
        if i >= swing:
            before = bars[i - swing:i]                    # the N bars before this one: its own high is not the level
            high, low = max(x["high"] for x in before), min(x["low"] for x in before)
            if price > high:
                add("breakout", level=high)
            elif price < low:
                add("breakdown", level=low)
        if i >= 1 and atr[i - 1]:                          # the ATR of the bars before, so the big bar is not its own yardstick
            rng = b["high"] - b["low"]
            if rng > IMPULSE_ATR * atr[i - 1]:
                add("impulse_up" if b["close"] >= b["open"] else "impulse_down", range=rng, atr=atr[i - 1],
                    ratio=rng / atr[i - 1])
        if i >= swing and price > max(closes[i - swing:i]):   # a new N-bar closing high
            window = closes[i - swing:i - MIN_PEAK_GAP + 1]  # the previous peak, at least MIN_PEAK_GAP bars back
            p = i - swing + max(range(len(window)), key=window.__getitem__)
            if p >= RSI_PERIOD and rsi[i] < rsi[p] - RSI_GAP:   # before its period the RSI series is a placeholder
                add("rsi_divergence", rsi=rsi[i], rsi_prev=rsi[p], prev_bar_time=bars[p]["time"])
            now_v = [x["volume"] for x in bars[i - 2:i + 1]]
            prev_v = [x["volume"] for x in bars[max(p - 1, 0):p + 2]]   # clamp: a negative index would wrap around
            vol_now, vol_prev = sum(now_v) / len(now_v), sum(prev_v) / len(prev_v)
            if vol_now < VOLUME_RATIO * vol_prev:
                add("volume_exhaustion", volume=vol_now, volume_prev=vol_prev)
        return out

    @staticmethod
    def _daily(daily: list[float], last_candle_time: int) -> dict | None:
        """Daily regime and the 50/200-day crossover on finished days only. The closes list has no highs and lows, so
        there are no daily breakouts; the ATR stand-in is the mean absolute daily change of the last 14 days. The
        frame carries no count of days: live has at most 259, the replay its whole history, and every number here
        uses at most the last 201, so the frame is identical on both sides."""
        n = len(daily)
        if n < ER_BARS + 1:
            return None
        day_start = last_candle_time - last_candle_time % DAY_MS
        bar_time = day_start - DAY_MS                      # the last finished day, as its 00:00 UTC in ms
        moves = [abs(daily[k] - daily[k - 1]) for k in range(n - RSI_PERIOD, n)]
        reg = regime(daily, sum(moves) / len(moves))
        sma50 = sum(daily[-50:]) / 50 if n >= 50 else None
        sma200 = sum(daily[-200:]) / 200 if n >= 200 else None
        events = []
        if n >= 201:
            prev50, prev200 = sum(daily[-51:-1]) / 50, sum(daily[-201:-1]) / 200
            kind = ("cross_up" if sma50 > sma200 and prev50 <= prev200 else
                    "cross_down" if sma50 < sma200 and prev50 >= prev200 else None)
            if kind:
                events.append({"kind": kind, "timeframe": "1d", "bar_time": bar_time, "price": daily[-1],
                               "details": {"sma50": sma50, "sma200": sma200}})
        return {"label": "1d", "bar_time": bar_time, "price": daily[-1], "regime": reg["label"] if reg else None,
                "direction": reg["direction"] if reg else 0, "er": reg["er"] if reg else None,
                "sma50": sma50, "sma200": sma200, "events": events}

    # ---- compact forms and texts ----
    @staticmethod
    def frames_in_order(report: dict) -> list[tuple[str, dict | None]]:
        """(label, frame) pairs in the order the owner reads them: 4h, the base frame, daily."""
        frames, base = report["timeframes"], report["interval"]
        order = ([("4h", frames.get("4h"))] if "4h" in frames and base != "4h" else []) + [(base, frames.get(base))]
        return order + [("1d", frames.get("1d"))]

    @staticmethod
    def brief(report: dict) -> dict:
        """The compact form for the Portfolio Manager and the decisions table (under ~400 bytes): one regime word per
        frame, the newest events in plain language, the main levels and the owner's line."""
        out: dict = {}
        for label, f in report["timeframes"].items():
            if f:
                out[label] = regime_text(f)
        out["events"] = [f"{e['timeframe']}: {phrase(e)}" for e in report["events"][:3]]
        main = report["timeframes"].get("4h") or report["timeframes"].get(report["interval"])
        if main:
            out["support"], out["resistance"] = main["support"], main["resistance"]
        out["summary"] = report.get("summary") or ChartEventsAgent.summary(report)
        return out

    @staticmethod
    def tag(report: dict) -> str:
        """'4h trending up · 1h chop' for /status."""
        parts = []
        for label, f in ChartEventsAgent.frames_in_order(report):
            if label == "1d":
                continue
            parts.append(f"{label} {regime_text(f)}" if f else f"no {label} bars yet")
        return " · ".join(parts)

    @staticmethod
    def summary(report: dict, daily_missing: str = "no daily prices yet") -> str:
        """The owner's line: '4h: chop · 1h: trending up, broke above the 2-day high at $3,522.50 · daily: trending
        up'. Each frame shows its regime and the events of its newest bar that has any (at most two). `daily_missing`
        is what the daily part says when the desk has no daily prices for this coin."""
        parts = []
        for label, f in ChartEventsAgent.frames_in_order(report):
            name = frame_name(label)
            if f is None:
                parts.append(f"{name}: {daily_missing}" if label == "1d" else f"{name}: no bars yet")
                continue
            text = f"{name}: {regime_text(f)}"
            if f["events"]:
                newest = max(e["bar_time"] for e in f["events"])
                shown = sorted((e for e in f["events"] if e["bar_time"] == newest),
                               key=lambda e: PRIORITY.index(e["kind"]))[:2]
                text += ", " + ", ".join(phrase(e) for e in shown)
            parts.append(text)
        if not any(f for _, f in ChartEventsAgent.frames_in_order(report)):
            return "not enough finished candles yet"
        return " · ".join(parts)


def bar_clock(ms: int, with_date: bool = False) -> str:
    """'16:00' (or '31 Oct 16:00') UTC for a bar's open time in ms."""
    t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return t.strftime("%d %b %H:%M") if with_date else t.strftime("%H:%M")
