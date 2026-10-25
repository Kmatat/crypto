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

Sizing (the Turtle Traders' "units", Richard Dennis) and trailing stops (Ed Seykota,
"cut losses, let profits run") live in the Risk Manager and the orchestrator; this
agent only reports them.
"""
from __future__ import annotations

import datetime as dt

from .technical import ema

ALL_CHECKS = ("trend200", "alignment", "overextended", "btc", "reward", "cooldown")
SOURCES = {
    "trend200": "Paul Tudor Jones", "alignment": "Mark Minervini", "overextended": "Minervini / Livermore",
    "btc": "Druckenmiller (don't fight the tape)", "reward": "Paul Tudor Jones", "cooldown": "Jesse Livermore",
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
               atr: float, btc_daily: list[float] | None, last_stop_ms: int | None, now_ms: int) -> dict:
        """Returns {"pass": bool, "checks": {name: {"ok", "detail", "source"}}, "failed": [names]}.
        A check that cannot be computed (not enough history) is reported as unknown and does not block."""
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
        failed = [n for n, c in out.items() if c["ok"] is False]
        return {"pass": not failed, "checks": out, "failed": failed}

    @staticmethod
    def summary(review: dict) -> str:
        """One line per check, for Telegram and the AI prompt."""
        marks = {True: "✓", False: "✗", None: "?"}
        return "\n".join(f"{marks[c['ok']]} {n}: {c['detail']} [{c['source']}]" for n, c in review["checks"].items())
