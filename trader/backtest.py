"""Backtest: replay the desk's rule-based decision logic over real historical hourly prices.

  python -m trader backtest --days 180
  python -m trader backtest --days 365 --symbols BTC,ETH,SOL,BNB,XRP --sweep

What it replays exactly as live: the Technical and Forecaster agents, the rule-based Portfolio
Manager (weighted score and thresholds), the Risk Manager (position size, stop-loss / take-profit,
daily and overall loss limits), fees, and exchange-side exits emulated from each hour's high/low.
What it cannot replay: news and forex (no history) are neutral, and Claude's judgement is absent.
So this measures the free, rule-based core - the floor the AI has to beat.
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import sys
import time

from .agents.decision import DecisionAgent
from .agents.execution import PAPER_FEE, STOP_SLIPPAGE
from .agents.mentor import ALL_CHECKS, MentorAgent
from .agents.prediction import PredictionAgent
from .agents.risk import RiskManagerAgent
from .agents.technical import TechnicalAnalysisAgent
from .binance_client import BinanceClient
from .config import PUBLIC_BASE_URL, Settings

WARMUP = 200          # candles the indicators need before the first decision
EXTRA_DAYS = 210
CACHE_MAX = 400_000   # (coin, hour) indicator entries kept for the laboratory; about 100 MB at most      # history before the test window, so 200-day averages exist from day one


INTERVAL_MS = {"1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}


RETRY_WAITS = (2, 5, 10)  # seconds between attempts at one page of public candles (a few hundred pages per lab run)


def _klines_page(client: BinanceClient, symbol: str, interval: str, start: int, sleep=time.sleep) -> list[dict]:
    """One page of public candles, retried on a network error, a rate limit or a server error: one hiccup in the
    few hundred requests of a lab run must not cancel the week's study or exam. Reading prices is safe to repeat."""
    import requests
    from .binance_client import BinanceError
    for wait in (*RETRY_WAITS, None):
        try:
            return client.klines(symbol, interval, limit=1000, start_ms=start)
        except (requests.RequestException, BinanceError) as e:
            transient = isinstance(e, requests.RequestException) or str(e).startswith(("HTTP 5", "HTTP 429", "HTTP 418"))
            if wait is None or not transient:
                raise
            sleep(wait)
    return []


def fetch_history(client: BinanceClient, symbol: str, days: int, interval: str = "1h") -> list[dict]:
    start = int((time.time() - (days + EXTRA_DAYS) * 24 * 3600) * 1000)
    out: list[dict] = []
    while True:
        rows = _klines_page(client, symbol, interval, start)
        if not rows:
            break
        out += rows
        if len(rows) < 1000:
            break
        start = rows[-1]["time"] + INTERVAL_MS[interval]
    return out[:-1]  # drop the still-forming current candle


class DailySeries:
    """Daily closes available at each hourly candle, precomputed so each step is O(1)."""

    def __init__(self, hourly: list[dict]):
        self.completed: list[float] = []   # closes of fully completed UTC days
        self.before: list[int] = []        # for candle i: number of completed days before its day
        last_day, last_close = None, None
        for c in hourly:
            day = dt.datetime.fromtimestamp(c["time"] / 1000, dt.timezone.utc).date()
            if day != last_day and last_day is not None:
                self.completed.append(last_close)
            last_day, last_close = day, c["close"]
            self.before.append(len(self.completed))

    def at(self, i: int, close: float) -> list[float]:
        return self.completed[: self.before[i]] + [close]


class Backtest:
    def __init__(self, s: Settings, history: dict[str, list[dict]], decision: DecisionAgent | None = None,
                 days: int | None = None, btc_history: list[dict] | None = None, end_ms: int | None = None,
                 analysis_cache: dict | None = None):
        self.s = s
        self.history = history
        # (symbol, candle time) -> (technical report, prediction report); a caller testing several windows or
        # strategy cards on the same history passes one dict so the indicators are computed once
        self._cache = analysis_cache if analysis_cache is not None else {}
        self.days = days
        self.end_ms = end_ms  # evaluate a window that ended in the past (out-of-sample checks)
        self.decision = decision or DecisionAgent()
        self.risk = RiskManagerAgent(s)
        self.technical, self.prediction = TechnicalAnalysisAgent(), PredictionAgent()
        self.mentor = MentorAgent(s.mentor_checks, s.min_reward_risk, s.cooldown_hours) if s.mentor_checks else None
        self.daily = {sym: DailySeries(h) for sym, h in history.items()}
        self.btc = btc_history if btc_history is not None else history.get("BTCUSDT")
        self.btc_daily = DailySeries(self.btc) if self.btc else None
        self.btc_index = {c["time"]: i for i, c in enumerate(self.btc)} if self.btc else {}

    def run(self) -> dict:
        s = self.s
        times = sorted(set.intersection(*(set(c["time"] for c in h) for h in self.history.values())))
        by_time = {sym: {c["time"]: i for i, c in enumerate(h)} for sym, h in self.history.items()}
        if self.end_ms:
            times = [t for t in times if t <= self.end_ms]
        if self.days and times:  # trade only the last `days`; earlier history feeds the long averages
            times = [t for t in times if t >= times[-1] - self.days * 86_400_000]
        step_ms = times[1] - times[0] if len(times) > 1 else 3_600_000
        cash, positions, trades, curve = s.starting_balance, {}, [], []
        day, day_start = None, s.starting_balance
        decisions = {"BUY": 0, "SELL": 0, "HOLD": 0}
        vetoes: dict[str, int] = {}
        last_stop: dict[str, int] = {}
        brake_tripped_at: int | None = None
        for t in times[WARMUP:] if not self.days else times:
            date = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).date()
            idx = {sym: by_time[sym][t] for sym in self.history}
            if any(i < WARMUP - 1 for i in idx.values()):
                continue
            # the live desk analyses the latest 200 candles: use exactly that window
            candles = {sym: self.history[sym][idx[sym] - WARMUP + 1: idx[sym] + 1] for sym in self.history}
            price = {sym: c[-1]["close"] for sym, c in candles.items()}

            # exchange-side exits during this hour (stop first: the conservative assumption)
            for sym, p in list(positions.items()):
                c = candles[sym][-1]
                if c["low"] <= p["stop"]:
                    cash += self._close(positions, trades, sym, p["stop"] * (1 - STOP_SLIPPAGE), "stop-loss", t)
                    last_stop[sym] = t
                elif c["high"] >= p["take"]:
                    cash += self._close(positions, trades, sym, p["take"], "take-profit", t)
            # indicators depend only on the candles, not on the strategy: computed once per (coin, hour) and shared
            # across the laboratory's windows and cards through `analysis_cache`
            analysis = {}
            for sym, c in candles.items():
                key = (sym, t)
                a = self._cache.get(key)
                if a is None:
                    tech = self.technical.run(c)
                    a = (tech["score"], tech["atr"], self.prediction.run(c)["score"])  # all the rule core needs
                    if len(self._cache) < CACHE_MAX:
                        self._cache[key] = a
                analysis[sym] = a
            # trailing stops follow new highs (Seykota / Turtles)
            for sym, p in positions.items():
                p["high"] = max(p["high"], price[sym])
                atr = analysis[sym][1]
                new_stop = self.risk.trailing_stop({"stop_loss": p["stop"]}, p["high"], atr)
                if new_stop:
                    p["stop"] = new_stop

            equity = cash + sum(p["qty"] * price[sym] for sym, p in positions.items())
            if date != day:
                day, day_start = date, equity
            if brake_tripped_at is not None and equity >= s.starting_balance * (1 - s.max_drawdown_pct / 2):
                brake_tripped_at = None  # recovered half the loss: back to normal (same rule as the live desk)
            portfolio = {"cash": cash, "equity": equity, "open_positions": len(positions),
                         "day_start_equity": day_start, "starting_balance": s.starting_balance,
                         "brake_tripped_days": None if brake_tripped_at is None else (t - brake_tripped_at) / 86_400_000}

            buys = []
            for sym, c in candles.items():
                tech_score, atr, pred_score = analysis[sym]
                reports = {"technical": {"score": tech_score, "atr": atr}, "prediction": {"score": pred_score},
                           "news": {"score": 0.0}, "forex": {"score": 0.0}}
                d = self.decision.run(sym, reports, holding=sym in positions)
                decisions[d["action"]] += 1
                if d["action"] == "SELL":
                    verdict = self.risk.review(d, sym, price[sym], atr, portfolio)
                    if verdict["approved"]:
                        cash += self._close(positions, trades, sym, price[sym], "signal", t)
                elif d["action"] == "BUY":
                    buys.append((d["confidence"], d["combined_score"], sym, atr, d))
            for _, _, sym, atr, d in sorted(buys, reverse=True):
                portfolio.update(cash=cash, open_positions=len(positions),
                                 equity=cash + sum(p["qty"] * price[x] for x, p in positions.items()))
                verdict = self.risk.review(d, sym, price[sym], atr, portfolio)
                if verdict.get("trip"):
                    brake_tripped_at = t
                if not verdict["approved"]:
                    continue
                if self.mentor:
                    btc_daily = (self.btc_daily.at(self.btc_index[t], self.btc[self.btc_index[t]]["close"])
                                 if self.btc_daily and t in self.btc_index else None)
                    review = self.mentor.review(sym, candles[sym], self.daily[sym].at(idx[sym], price[sym]), price[sym],
                                                verdict["stop_loss"], verdict["take_profit"], atr, btc_daily,
                                                last_stop.get(sym), t)
                    if not review["pass"]:
                        for name in review["failed"]:
                            vetoes[name] = vetoes.get(name, 0) + 1
                        continue
                quote = verdict["quote_amount"]
                positions[sym] = {"qty": quote * (1 - PAPER_FEE) / price[sym], "cost": quote, "entry": price[sym],
                                  "stop": verdict["stop_loss"], "take": verdict["take_profit"], "opened": t,
                                  "high": price[sym]}
                cash -= quote
            curve.append((t, cash + sum(p["qty"] * price[sym] for sym, p in positions.items())))

        # value anything still open at the last price
        final = curve[-1][1] if curve else s.starting_balance
        traded = [t for t, _ in curve]
        if not traded:
            return {"days": 0, "start_balance": s.starting_balance, "final_balance": s.starting_balance, "return_pct": 0.0,
                    "buy_and_hold_pct": {}, "basket_hold_pct": None, "trades": 0, "trades_per_week": 0.0,
                    "fees_paid": 0.0, "fees_pct_of_start": 0.0, "win_rate_pct": None, "avg_win": None, "avg_loss": None,
                    "profit_factor": None, "max_drawdown_pct": 0.0, "avg_hold_hours": None,
                    "exits": {"stop-loss": 0, "take-profit": 0, "signal": 0}, "decisions": decisions, "mentor_vetoes": {}}
        result = self._metrics(trades, curve, final, decisions, traded)
        result["mentor_vetoes"] = vetoes
        return result

    @staticmethod
    def _close(positions, trades, sym, px, reason, t) -> float:
        p = positions.pop(sym)
        proceeds = p["qty"] * px * (1 - PAPER_FEE)
        trades.append({"symbol": sym, "pnl": proceeds - p["cost"], "pnl_pct": (proceeds / p["cost"] - 1) * 100,
                       "reason": reason, "hours": (t - p["opened"]) / 3_600_000,
                       "fees": p["cost"] * PAPER_FEE + p["qty"] * px * PAPER_FEE})
        return proceeds

    def _metrics(self, trades, curve, final, decisions, times) -> dict:
        s = self.s
        wins = [x for x in trades if x["pnl"] > 0]
        losses = [x for x in trades if x["pnl"] <= 0]
        peak, max_dd = s.starting_balance, 0.0
        for _, e in curve:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak)
        days = max(1, (times[-1] - times[0]) / 86_400_000) if times else 1
        gross_win, gross_loss = sum(x["pnl"] for x in wins), -sum(x["pnl"] for x in losses)
        fees = sum(x["fees"] for x in trades)
        first = {sym: next(c["close"] for c in h if c["time"] == times[0]) for sym, h in self.history.items()} if times else {}
        last = {sym: h[-1]["close"] for sym, h in self.history.items()}
        hold = {sym: (last[sym] / first[sym] - 1) * 100 for sym in first}
        return {
            "days": round(days), "start_balance": s.starting_balance, "final_balance": round(final, 2),
            "return_pct": round((final / s.starting_balance - 1) * 100, 2),
            "buy_and_hold_pct": {sym.removesuffix("USDT"): round(v, 1) for sym, v in hold.items()},
            "basket_hold_pct": round(sum(hold.values()) / len(hold), 1) if hold else None,
            "trades": len(trades), "trades_per_week": round(len(trades) / days * 7, 1),
            "fees_paid": round(fees, 2), "fees_pct_of_start": round(fees / s.starting_balance * 100, 2),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else None,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "avg_hold_hours": round(sum(x["hours"] for x in trades) / len(trades), 1) if trades else None,
            "exits": {r: sum(1 for x in trades if x["reason"] == r) for r in ("stop-loss", "take-profit", "signal")},
            "decisions": decisions,
        }


def print_result(r: dict) -> None:
    print(f"\n=== {r['days']} days, ${r['start_balance']:,.0f} start ===")
    print(f"Strategy return     {r['return_pct']:+.2f}%   (final ${r['final_balance']:,.2f})")
    basket = "n/a" if r["basket_hold_pct"] is None else f"{r['basket_hold_pct']:+.1f}%"
    print(f"Buy-and-hold        basket {basket}   " +
          "  ".join(f"{k} {v:+.1f}%" for k, v in r["buy_and_hold_pct"].items()))
    print(f"Trades              {r['trades']} ({r['trades_per_week']}/week), avg hold {r['avg_hold_hours']} h, "
          f"exits {r['exits']}")
    print(f"Fees paid           ${r['fees_paid']:,.2f} ({r['fees_pct_of_start']}% of the starting balance)")
    print(f"Win rate            {r['win_rate_pct']}%   avg win ${r['avg_win']}  avg loss ${r['avg_loss']}  "
          f"profit factor {r['profit_factor']}")
    print(f"Max drawdown        {r['max_drawdown_pct']}%")
    print(f"Decisions           {r['decisions']}")


def parse_coin_buy(ap: argparse.ArgumentParser, text: str) -> dict[str, float]:
    """'BTC=0.4,ETH=0.3' -> {'BTCUSDT': 0.4, 'ETHUSDT': 0.3}; anything else is a usage error, not a traceback."""
    out: dict[str, float] = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        try:
            coin, value = item.split("=")
            threshold = float(value)
        except ValueError:
            ap.error(f"--coin-buy expects COIN=VALUE[,COIN=VALUE], e.g. BTC=0.4,ETH=0.3 (got {item!r})")
        coin = coin.strip().upper()
        out[coin + ("" if coin.endswith("USDT") else "USDT")] = threshold
    return out


def _symbols(text: str) -> list[str]:
    return [x.strip().upper() + ("" if x.strip().upper().endswith("USDT") else "USDT") for x in text.split(",") if x.strip()]


def trend_main(args) -> int:
    """`backtest --strategy trend`: the daily trend rule over the last --days days, under the desk's limits."""
    from . import trend
    symbols = _symbols(args.symbols) if args.symbols else list(trend.TESTED_COINS)
    args.days = args.days or 1095   # 1-2 trades a week: a short window says little
    live = Settings()
    s = Settings(offline=True, symbols=symbols, starting_balance=args.balance, strategy="trend",
                 max_position_pct=args.position or live.max_position_pct,
                 max_open_positions=args.max_positions or len(symbols),
                 trend_sma_days=args.sma or live.trend_sma_days,
                 trend_vol_target=args.vol_target or live.trend_vol_target,
                 trend_stop_pct=args.trend_stop or live.trend_stop_pct)
    sma_days = trend.params(s)[0]
    client = BinanceClient(PUBLIC_BASE_URL)
    print(f"Downloading {args.days} (+{trend.window(sma_days)} for the averages) days of daily prices for "
          f"{', '.join(symbols)} ...", file=sys.stderr)
    now_ms = int(time.time() * 1000)
    history = {sym: trend.fetch_daily(client, sym, args.days + trend.window(sma_days) + 2, now_ms) for sym in symbols}
    r = trend.backtest(s, history, start_ms=now_ms - args.days * trend.DAY_MS)
    print(f"Daily trend rule: {sma_days}-day average, volatility target {trend.params(s)[1]:g}, emergency stop "
          f"{trend.params(s)[2]:.0%}, up to {s.max_open_positions} positions of at most {s.max_position_pct:.0%} "
          "(the tested defaults and these flags, not your settings file; /trendtest on Telegram uses yours)")
    print_result(r)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="trader backtest")
    ap.add_argument("--days", type=int, default=None, help="days to replay (default 180; 1095 with --strategy trend)")
    ap.add_argument("--symbols", default=None, help="coins, e.g. BTC,ETH,SOL (default BTC,ETH,SOL; with --strategy "
                                                     "trend the 8 tested coins)")
    ap.add_argument("--balance", type=float, default=10_000)
    ap.add_argument("--buy", type=float, help="buy threshold on the weighted score (default 0.25)")
    ap.add_argument("--sell", type=float, help="sell threshold (default -0.25)")
    ap.add_argument("--stop", type=float, help="stop-loss distance in ATR (default 2)")
    ap.add_argument("--take", type=float, help="take-profit distance in ATR (default 3)")
    ap.add_argument("--position", type=float, help="max position size as a fraction of equity (default 0.10)")
    ap.add_argument("--max-positions", type=int)
    ap.add_argument("--sweep", action="store_true", help="try a grid of thresholds and stop/take distances")
    ap.add_argument("--checks", default=None, help="master traders' checklist: 'all', 'none' or a comma list of "
                                                     + ",".join(ALL_CHECKS) + " (default: the live setting)")
    ap.add_argument("--rr", type=float, help="minimum reward-to-risk for the 'reward' check (default 2)")
    ap.add_argument("--cooldown", type=float, help="hours to wait after a stop-loss before re-buying (default 24)")
    ap.add_argument("--risk", type=float, help="risk per trade as %% of equity, Turtle sizing (e.g. 1); 0 = fixed size")
    ap.add_argument("--trail", type=float, help="trailing stop distance in ATR (e.g. 2); 0 = off")
    ap.add_argument("--interval", default="1h", choices=sorted(INTERVAL_MS), help="candle size (live: CANDLE_INTERVAL)")
    ap.add_argument("--coin-buy", default="", help="per-coin buy thresholds, e.g. BTC=0.4,ETH=0.3 (others use --buy)")
    ap.add_argument("--strategy", default="hourly", choices=("hourly", "trend"),
                    help="hourly: the agents' hourly rules (default); trend: the daily trend rule (STRATEGY=trend)")
    ap.add_argument("--sma", type=int, help="trend: days of the average (default 50)")
    ap.add_argument("--vol-target", type=float, help="trend: yearly swing a full share allows (default 0.5)")
    ap.add_argument("--trend-stop", type=float, help="trend: emergency stop below the entry (default 0.25)")
    args = ap.parse_args(argv)
    if args.strategy == "trend":
        return trend_main(args)
    args.days = args.days or 180
    coin_buy = parse_coin_buy(ap, args.coin_buy) if args.coin_buy else {}  # before any download: bad input fails fast

    from .agents import decision as dmod
    symbols = _symbols(args.symbols or "BTC,ETH,SOL")
    client = BinanceClient(PUBLIC_BASE_URL)
    print(f"Downloading {args.days} (+{EXTRA_DAYS} for long averages) days of hourly prices for "
          f"{', '.join(symbols)} ...", file=sys.stderr)
    history = {sym: fetch_history(client, sym, args.days, args.interval) for sym in symbols}
    btc = history.get("BTCUSDT") or fetch_history(client, "BTCUSDT", args.days, args.interval)
    live = Settings()  # the live desk's defaults: a flag-less backtest measures exactly what trades
    if args.checks is None:
        checks = live.mentor_checks
    elif args.checks == "all":
        checks = tuple(ALL_CHECKS)
    elif args.checks in ("", "none"):
        checks = ()
    else:
        checks = tuple(c.strip() for c in args.checks.split(","))

    def pick(flag, default):
        return default if flag is None else flag

    def settings(stop=None, take=None):
        return Settings(offline=True, symbols=symbols, starting_balance=args.balance, interval=args.interval,
                        stop_loss_atr=pick(stop, pick(args.stop, live.stop_loss_atr)),
                        take_profit_atr=pick(take, pick(args.take, live.take_profit_atr)),
                        max_position_pct=pick(args.position, live.max_position_pct),
                        max_open_positions=pick(args.max_positions, live.max_open_positions),
                        mentor_checks=checks, min_reward_risk=pick(args.rr, live.min_reward_risk),
                        cooldown_hours=pick(args.cooldown, live.cooldown_hours),
                        risk_per_trade_pct=pick(args.risk, live.risk_per_trade_pct * 100) / 100,
                        trail_atr=pick(args.trail, live.trail_atr))

    def run(s):
        return Backtest(s, history, days=args.days, btc_history=btc).run()

    if args.coin_buy:
        dmod.COIN_THRESHOLDS.clear()
        dmod.COIN_THRESHOLDS.update(coin_buy)
    if not args.sweep:
        dmod.BUY_THRESHOLD = args.buy or dmod.BUY_THRESHOLD
        dmod.SELL_THRESHOLD = args.sell or dmod.SELL_THRESHOLD
        r = run(settings())
        print_result(r)
        if r["mentor_vetoes"]:
            print(f"Checklist vetoes      {r['mentor_vetoes']}")
        return 0

    print(f"Sweep with checklist={','.join(checks) or 'none'} risk={pick(args.risk, live.risk_per_trade_pct * 100):g}% "
          f"trail={pick(args.trail, live.trail_atr):g} ATR (pass --checks none --risk 0 --trail 0 for the plain rules)")
    print(f"{'buy':>5} {'sell':>6} {'stop':>5} {'take':>5} | {'return':>8} {'trades':>6} {'win%':>5} {'PF':>5} {'maxDD':>6}")
    for buy, (stop, take) in itertools.product([0.15, 0.25, 0.35, 0.45], [(1.5, 3.0), (2.0, 3.0), (2.0, 4.0), (3.0, 6.0)]):
        dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD = buy, -buy
        r = run(settings(stop, take))
        print(f"{buy:>5.2f} {-buy:>6.2f} {stop:>5.1f} {take:>5.1f} | {r['return_pct']:>+7.2f}% {r['trades']:>6} "
              f"{r['win_rate_pct'] or 0:>5.1f} {r['profit_factor'] or 0:>5.2f} {r['max_drawdown_pct']:>6.2f}")
    print(f"basket buy-and-hold over the same window: {r['basket_hold_pct']:+.1f}%")
    return 0
