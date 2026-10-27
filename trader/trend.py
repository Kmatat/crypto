"""The daily trend strategy (STRATEGY=trend): the rule, and its replay over history.

Why it exists: the hourly signals lost money after fees in every test (profit factor 0.66-0.73 on two years of
hourly prices, about 200 variants all fragile). The one rule that held up in every check of the research - older
years never used to choose it, coins never used to choose it, and the last 180 days - is this slow daily trend
rule. README "The daily trend strategy" has the numbers.

Once a day, after the daily close (00:00 UTC), on finished daily candles only:
  * hold a coin while its close is above its 50-day average (TREND_SMA_DAYS); sell it after a close below;
  * each coin's share of the account is 1 / number of coins, made smaller when the coin swings more than
    TREND_VOL_TARGET a year (30-day volatility), and never more than MAX_POSITION_PCT;
  * when more coins qualify than there are free slots (MAX_OPEN_POSITIONS), the strongest 60-day gain goes first;
  * an emergency stop TREND_STOP_PCT below the entry lives on the exchange; after it fires the coin is bought again
    only once its signal has reset (a close at or below the average, then above it again).
There is no take-profit: the few long trends pay for the many small losses (the rule wins only 20-30% of its
trades), so a winner is never cut short. Binance's protective pair still needs a take-profit price and refuses a
sell limit more than twice the price, so the desk keeps it at 1.9x the price and moves it up whenever the price
comes within 30% of it (TAKE_MULT, RAISE_AT).
"""
from __future__ import annotations

import math
import statistics
import time

from .agents.execution import PAPER_FEE, STOP_SLIPPAGE
from .agents.risk import RiskManagerAgent

DAY_MS = 86_400_000
VOL_DAYS = 30          # volatility from the last 30 daily moves
MOMENTUM_DAYS = 60     # the ranking when slots are short: gain over 60 days
TAKE_MULT = 1.9        # the take-profit leg of the exchange pair, as a multiple of the price (Binance's limit is 2x)
RAISE_AT = 1.3         # move it up once the price is within 30% of it
SLIPPAGE = 0.0005      # a market order at the open pays a little more (buy) or gets a little less (sell)
TESTED = (50, 0.5, 0.25)   # TREND_SMA_DAYS, TREND_VOL_TARGET, TREND_STOP_PCT as tested
TESTED_COINS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT")


def params(s) -> tuple[int, float, float]:
    """(average days, volatility target, stop distance). Out-of-range settings fall back to the tested values
    (Settings.problems() reports them)."""
    if 10 <= s.trend_sma_days <= 400 and 0.05 <= s.trend_vol_target <= 2 and 0.05 <= s.trend_stop_pct <= 0.5:
        return int(s.trend_sma_days), float(s.trend_vol_target), float(s.trend_stop_pct)
    return TESTED


def window(sma_days: int) -> int:
    """Finished days one signal needs."""
    return max(sma_days, MOMENTUM_DAYS + 1, VOL_DAYS + 1)


def history_days(sma_days: int) -> int:
    """Daily candles the desk reads per coin: one signal, plus 60 days to see whether a stop's signal has reset."""
    return window(sma_days) + 60


def finished(candles: list[dict], now_ms: int | None = None) -> list[dict]:
    """Binance's last daily candle is the day still forming: decisions use finished days only."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return [c for c in candles if c["time"] + DAY_MS <= now_ms]


def signal(closes: list[float], sma_days: int) -> dict | None:
    """The coin's state on its last finished day, or None without enough history (a recent listing)."""
    if len(closes) < window(sma_days) or min(closes[-window(sma_days):]) <= 0:
        return None
    sma = sum(closes[-sma_days:]) / sma_days
    moves = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - VOL_DAYS, len(closes))]
    return {"close": closes[-1], "sma": sma, "on": closes[-1] > sma,
            "vol": statistics.stdev(moves) * math.sqrt(365),
            "momentum": closes[-1] / closes[-1 - MOMENTUM_DAYS] - 1}


def share(sig: dict, coins: int, vol_target: float, max_pct: float) -> float:
    """The coin's share of the account when it is bought."""
    calm = min(1.0, vol_target / sig["vol"]) if sig["vol"] > 0 else 1.0
    return min(max_pct, calm / max(1, coins))


def armed(candles: list[dict], sma_days: int, stop_ms: int | None) -> bool:
    """After an emergency stop the coin waits until its signal has reset: a finished day, from the stop's day on,
    that closed at or below its average. No stop, or a stop older than the history read: ready."""
    if stop_ms is None or len(candles) < sma_days or stop_ms < candles[sma_days - 1]["time"]:
        return True
    closes = [c["close"] for c in candles]
    for i in range(sma_days - 1, len(candles)):
        if candles[i]["time"] + DAY_MS > stop_ms and closes[i] <= sum(closes[i + 1 - sma_days:i + 1]) / sma_days:
            return True
    return False


def describe(sym: str, sig: dict | None, sma_days: int) -> str:
    """One line for the journal and the owner: why the rule holds, buys or stays out."""
    coin = sym.removesuffix("USDT")
    if sig is None:
        return f"{coin}: not enough daily history for the {sma_days}-day average yet"
    gap = (sig["close"] / sig["sma"] - 1) * 100
    side = "above" if sig["on"] else "at or below"
    return (f"{coin} closed at {sig['close']:,.6g}, {side} its {sma_days}-day average {sig['sma']:,.6g} ({gap:+.1f}%) · "
            f"swings {sig['vol']:.0%} a year · {MOMENTUM_DAYS}-day change {sig['momentum']:+.0%}")


# ---- history and replay ----
def fetch_daily(client, symbol: str, days: int, now_ms: int | None = None) -> list[dict]:
    """Finished daily candles for the last `days` days (public Binance data, pages of 1000)."""
    from .backtest import _klines_page
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    start, out = now_ms - days * DAY_MS, []
    while True:
        rows = _klines_page(client, symbol, "1d", start)
        if not rows:
            break
        out += rows
        if len(rows) < 1000:
            break
        start = rows[-1]["time"] + DAY_MS
    return finished(out, now_ms)


def backtest(s, history: dict[str, list[dict]], start_ms: int | None = None, end_ms: int | None = None) -> dict:
    """The rule as one account under the desk's own limits (the live Risk Manager decides every buy), on finished
    daily candles: decisions on a day's close, orders at the next day's open (0.1% fee + 0.05% slippage), the
    emergency stop checked on each day's low (filled at the stop, or at the open when the day opened below it, less
    0.2%), the overall loss brake as live. The daily loss limit cannot act live (the decision runs at the first check
    of the new day, when the day's result is still zero), so it is left out here too. `history` must start
    window(TREND_SMA_DAYS) days before `start_ms` for the signals to exist from the first day."""
    sma_days, vol_target, stop_pct = params(s)
    risk = RiskManagerAgent(s)
    coins = list(history)
    rows = {sym: history[sym] for sym in coins}
    index = {sym: {r["time"] // DAY_MS: i for i, r in enumerate(rows[sym])} for sym in coins}
    closes = {sym: [r["close"] for r in rows[sym]] for sym in coins}
    days = sorted({r["time"] // DAY_MS for sym in coins for r in rows[sym]})
    if start_ms is not None:
        days = [d for d in days if d >= start_ms // DAY_MS]
    if end_ms is not None:
        days = [d for d in days if d <= end_ms // DAY_MS]
    need, cache = window(sma_days), {}

    def sig(sym, d):
        i = index[sym].get(d)
        if i is None:
            return None
        if (sym, i) not in cache:
            cache[sym, i] = signal(closes[sym][max(0, i + 1 - need):i + 1], sma_days)
        return cache[sym, i]

    def last_close(sym, d):
        i = index[sym].get(d)
        return closes[sym][i] if i is not None else None

    start = float(s.starting_balance)
    cash, pos, trades, curve, ready, tripped, buys_refused = start, {}, [], [], {sym: True for sym in coins}, None, {}
    marks: dict[str, float] = {}

    def sell(sym, px, reason, d):
        nonlocal cash
        p = pos.pop(sym)
        proceeds = p["qty"] * px * (1 - PAPER_FEE)
        cash += proceeds
        trades.append({"symbol": sym, "pnl": proceeds - p["cost"], "pnl_pct": (proceeds / p["cost"] - 1) * 100,
                       "reason": reason, "hours": (d - p["opened"]) * 24.0,
                       "fees": p["cost"] * PAPER_FEE + p["qty"] * px * PAPER_FEE})

    for k in range(len(days) - 1):
        t, nxt = days[k], days[k + 1]
        for sym in list(pos):                      # the emergency stop during day t
            i = index[sym].get(t)
            if i is not None and rows[sym][i]["low"] <= pos[sym]["stop"]:
                sell(sym, min(rows[sym][i]["open"], pos[sym]["stop"]) * (1 - STOP_SLIPPAGE), "stop-loss", t)
                ready[sym] = False                 # bought again only after the signal resets
        for sym in coins:
            if (c := last_close(sym, t)) is not None:
                marks[sym] = c
        equity = cash + sum(p["qty"] * marks.get(sym, p["entry"]) for sym, p in pos.items())
        curve.append(equity)
        sigs = {sym: sig(sym, t) for sym in coins}
        for sym, g in sigs.items():
            if g is not None and not g["on"]:
                ready[sym] = True
        for sym in [x for x in pos if sigs.get(x) is not None and not sigs[x]["on"]]:
            if (j := index[sym].get(nxt)) is not None:
                sell(sym, rows[sym][j]["open"] * (1 - SLIPPAGE), "signal", nxt)
        equity = cash + sum(p["qty"] * marks.get(sym, p["entry"]) for sym, p in pos.items())
        if tripped is not None and equity >= start * (1 - s.max_drawdown_pct / 2):
            tripped = None                         # recovered half the loss (Orchestrator.portfolio)
        wanted = sorted((x for x in coins if x not in pos and ready[x] and sigs.get(x) is not None and sigs[x]["on"]
                         and index[x].get(nxt) is not None), key=lambda x: -sigs[x]["momentum"])
        for sym in wanted:
            px = rows[sym][index[sym][nxt]]["open"] * (1 + SLIPPAGE)
            folio = {"cash": cash, "equity": equity, "open_positions": len(pos), "day_start_equity": equity,
                     "starting_balance": start, "brake_tripped_days": None if tripped is None else nxt - tripped}
            verdict = risk.review_trend(sym, px, share(sigs[sym], len(coins), vol_target, s.max_position_pct), folio)
            if verdict.get("trip"):
                tripped = nxt
            if not verdict["approved"]:
                key = verdict.get("limit") or "cash or slots"
                buys_refused[key] = buys_refused.get(key, 0) + 1
                continue
            value = verdict["quote_amount"]
            cash -= value
            pos[sym] = {"qty": value * (1 - PAPER_FEE) / px, "cost": value, "entry": px, "stop": verdict["stop_loss"],
                        "opened": nxt}
    final = cash + sum(p["qty"] * marks.get(sym, p["entry"]) for sym, p in pos.items())
    if days:
        for sym in coins:
            if (c := last_close(sym, days[-1])) is not None:
                marks[sym] = c
        final = cash + sum(p["qty"] * marks.get(sym, p["entry"]) for sym, p in pos.items())
    curve.append(final)
    first = {sym: rows[sym][index[sym][days[0]]]["open"] for sym in coins if days and days[0] in index[sym]}
    hold = {sym: (marks[sym] / first[sym] - 1) * 100 for sym in first if sym in marks}
    return _metrics(start, final, trades, curve, len(days), hold, sorted(pos), buys_refused)


def _metrics(start, final, trades, curve, days, hold, holding, refused) -> dict:
    wins = [x for x in trades if x["pnl"] > 0]
    losses = [x for x in trades if x["pnl"] <= 0]
    peak, max_dd = start, 0.0
    for e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)
    gross_win, gross_loss = sum(x["pnl"] for x in wins), -sum(x["pnl"] for x in losses)
    fees = sum(x["fees"] for x in trades)
    days = max(1, days)
    return {
        "strategy": "trend", "days": days, "start_balance": start, "final_balance": round(final, 2),
        "return_pct": round((final / start - 1) * 100, 2),
        "buy_and_hold_pct": {sym.removesuffix("USDT"): round(v, 1) for sym, v in hold.items()},
        "basket_hold_pct": round(sum(hold.values()) / len(hold), 1) if hold else None,
        "trades": len(trades), "trades_per_week": round(len(trades) / days * 7, 1),
        "fees_paid": round(fees, 2), "fees_pct_of_start": round(fees / start * 100, 2),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_hold_hours": round(sum(x["hours"] for x in trades) / len(trades), 1) if trades else None,
        "exits": {r: sum(1 for x in trades if x["reason"] == r) for r in ("stop-loss", "signal")},
        "still_open": len(holding), "holding": holding, "decisions": {"buys refused": refused} if refused else {},
    }


def preview_settings(s):
    """The settings a replay for the owner uses: theirs when the desk trades the trend rule, else the tested setup
    (the 8 tested coins, a slot for each) with their position size and budget."""
    import dataclasses
    if s.trend:
        return s
    return dataclasses.replace(s, strategy="trend", symbols=list(TESTED_COINS), max_open_positions=len(TESTED_COINS))


def result_text(r: dict, s, days: int) -> str:
    """A replay result in a few lines for the owner's phone."""
    sma_days, vol_target, stop_pct = params(s)
    coins = " ".join(c.removesuffix("USDT") for c in s.symbols)
    hold = r.get("basket_hold_pct")
    return "\n".join([
        f"📈 Daily trend rule, replayed over the last {days} days on real Binance daily prices",
        f"Coins: {coins} · up to {s.max_open_positions} positions of at most {s.max_position_pct:.0%} · "
        f"{sma_days}-day average · emergency stop {stop_pct:.0%} · ${r['start_balance']:,.0f} start",
        f"Result {r['return_pct']:+.1f}% (just holding the same coins: " + (f"{hold:+.1f}%)" if hold is not None else "n/a)"),
        f"Worst drop {r['max_drawdown_pct']:.1f}% · {r['trades']} trades closed ({r['trades_per_week']:g} a week) · "
        f"winners {r['win_rate_pct'] if r['win_rate_pct'] is not None else 'n/a'}% · profit factor "
        f"{r['profit_factor'] if r['profit_factor'] is not None else 'n/a'} · still open {r['still_open']}",
        f"Fees ${r['fees_paid']:,.2f}. Same limits as the live desk (position cap, loss brakes); the past does not "
        "promise the future.",
    ])
