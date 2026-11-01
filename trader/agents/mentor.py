"""Mentor agent: the "master traders' checklist".

Every candidate BUY is checked against rules that well-known traders documented in
their books and interviews. Each check names its source so the owner (and Claude)
can see which principle a trade honours or breaks. Only principles that can be
computed from price data are included; nothing here copies anyone's live calls.

  trend200     Paul Tudor Jones: "Nothing good happens below the 200-day moving average."
  alignment    Mark Minervini's trend template: price above the 50- and 200-day averages,
               and the 50-day above the 200-day (the market is in a confirmed uptrend).
  overextended Minervini / Livermore: don't chase a coin that already ran far above its
               recent average; wait for a calmer entry.
  btc          Druckenmiller / "don't fight the tape": when Bitcoin itself is below its
               200-day average, the whole crypto market is in a downtrend; no new alt buys.
  reward       Paul Tudor Jones' reward-to-risk discipline: the target must be at least
               MIN_REWARD_RISK times the distance to the stop (default 2:1).
  cooldown     Jesse Livermore: never rush back into a coin that just stopped you out.
  regime       Dow theory / Wyckoff: no new buy while the hourly or 4-hour chart is in chop (going
               nowhere), or right after a breakdown below the recent low. Off by default; measure it
               first with the replay (python -m trader backtest --checks ...,regime).
  memory       The desk's own record: no new buy when similar past trades (same 4-hour chart side, Bitcoin side
               of its 200-day average, time of day, US release nearby) lost clearly more often than the desk's
               trades usually do. Off by default; measure it with the replay (... --checks ...,memory).

Sizing (the Turtle Traders' "units", Richard Dennis) and trailing stops (Ed Seykota,
"cut losses, let profits run") live in the Risk Manager and the orchestrator; this
agent only reports them.
"""
from __future__ import annotations

import datetime as dt

from .technical import ema

ALL_CHECKS = ("trend200", "alignment", "overextended", "btc", "reward", "cooldown", "regime", "memory")
OWNER_ONLY = ("memory",)   # rules only the owner's MENTOR_CHECKS adds: the research agent may neither propose nor drop them
SOURCES = {
    "trend200": "Paul Tudor Jones", "alignment": "Mark Minervini", "overextended": "Minervini / Livermore",
    "btc": "Druckenmiller (don't fight the tape)", "reward": "Paul Tudor Jones", "cooldown": "Jesse Livermore",
    "regime": "Dow theory / Wyckoff (trade with the trend, not inside the range)",
    "memory": "the desk's own record (exact binomial test against its base loss rate)",
}


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def fmt(price: float) -> str:
    """Readable prices for both Bitcoin ($80,936.01) and tiny coins ($0.00000712)."""
    return f"{price:,.2f}" if price >= 1 else f"{price:.6g}"


def daily_closes(hourly: list[dict]) -> list[float]:
    """Close of each UTC day, from hourly candles (used by the backtest; live uses real daily candles)."""
    days: dict[dt.date, float] = {}
    for c in hourly:
        days[dt.datetime.fromtimestamp(c["time"] / 1000, dt.timezone.utc).date()] = c["close"]
    return list(days.values())


class MentorAgent:
    name = "mentor"

    def __init__(self, checks=ALL_CHECKS, min_reward_risk: float = 2.0, cooldown_hours: float = 24,
                 overextension_atr: float = 3.0):
        self.checks = tuple(checks)
        self.min_reward_risk = min_reward_risk
        self.cooldown_hours = cooldown_hours
        self.overextension_atr = overextension_atr

    def review(self, symbol: str, candles: list[dict], daily: list[float], price: float, stop: float, take: float,
               atr: float, btc_daily: list[float] | None, last_stop_ms: int | None, now_ms: int,
               chart: dict | None = None, memory: dict | None = None) -> dict:
        """Returns {"pass": bool, "checks": {name: {"ok", "detail", "source"}}, "failed": [names]}.
        A check that cannot be computed (not enough history) is reported as unknown and does not block.
        `chart` is the Chart Events agent's report for the coin (trader/agents/chart.py), read only by the
        `regime` rule; `memory` is the memory's verdict on this buy (trader/agents/memory.py MemoryBook.verdict), read
        only by the `memory` rule; the other rules work without them."""
        out: dict[str, dict] = {}

        def add(name, ok, detail):
            if name in self.checks:
                out[name] = {"ok": ok, "detail": detail, "source": SOURCES[name]}

        ma200, ma50 = sma(daily, 200), sma(daily, 50)
        if ma200 is None:
            add("trend200", None, "not enough history for a 200-day average")
            add("alignment", None, "not enough history")
        else:
            add("trend200", price > ma200, f"price {fmt(price)} vs 200-day average {fmt(ma200)}")
            add("alignment", price > ma50 > ma200,
                f"price {fmt(price)}, 50-day {fmt(ma50)}, 200-day {fmt(ma200)}: "
                + ("confirmed uptrend" if price > ma50 > ma200 else "trend not aligned"))
        closes = [c["close"] for c in candles]
        ema20 = ema(closes, 20)[-1]
        stretch = (price - ema20) / atr if atr else 0.0
        add("overextended", stretch <= self.overextension_atr,
            f"price is {stretch:+.1f} ATR from its 20-hour average (limit {self.overextension_atr:g})")
        if symbol.startswith("BTC"):
            add("btc", True, "this is Bitcoin itself")
        elif btc_daily is None or sma(btc_daily, 200) is None:
            add("btc", None, "Bitcoin history unavailable")
        else:
            btc_ma = sma(btc_daily, 200)
            add("btc", btc_daily[-1] > btc_ma, f"Bitcoin {fmt(btc_daily[-1])} vs its 200-day average {fmt(btc_ma)}")
        rr = (take - price) / (price - stop) if price > stop else 0.0
        add("reward", rr >= self.min_reward_risk - 1e-9, f"reward-to-risk {rr:.1f}:1 (minimum {self.min_reward_risk:g}:1)")
        if last_stop_ms is None:
            add("cooldown", True, "no recent stop-loss on this coin")
        else:
            hours = (now_ms - last_stop_ms) / 3_600_000
            add("cooldown", hours >= self.cooldown_hours,
                f"last stop-loss on this coin {hours:.0f} h ago (wait {self.cooldown_hours:g} h)")
        if "regime" in self.checks:
            if not chart:
                add("regime", None, "no chart report")
            else:
                # the base frame (hourly) and the 4-hour frame; the daily frame is context only. The words are the
                # chart module's own (a local import: chart.py imports fmt from this module when it loads)
                from .chart import phrase, regime_text
                short = [f for f in chart["timeframes"].values() if f and f["label"] != "1d"]
                chop = [f["label"] for f in short if f.get("regime") == "chop"]
                latest = chart.get("latest_event")
                after_breakdown = bool(latest and latest["kind"] == "breakdown")
                picture = ", ".join(f"{f['label']} {regime_text(f)}" for f in short) or "no chart frames yet"
                detail = picture + (f"; latest event: {latest['timeframe']} {phrase(latest)}" if latest
                                    else "; no recent event")
                # no usable frame yet (a new listing): unknown, like the other rules with too little history
                add("regime", None if not short else (not chop and not after_breakdown), detail)
        if "memory" in self.checks:
            if not memory:
                add("memory", None, "no memory verdict")
            else:
                add("memory", memory["ok"], memory["detail"])   # ok None (too few trades) never blocks
        failed = [n for n, c in out.items() if c["ok"] is False]
        return {"pass": not failed, "checks": out, "failed": failed}

    @staticmethod
    def summary(review: dict) -> str:
        """One line per check, for Telegram and the AI prompt."""
        marks = {True: "✓", False: "✗", None: "?"}
        return "\n".join(f"{marks[c['ok']]} {n}: {c['detail']} [{c['source']}]" for n, c in review["checks"].items())
