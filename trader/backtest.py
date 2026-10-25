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
from .agents.prediction import PredictionAgent
from .agents.risk import RiskManagerAgent
from .agents.technical import TechnicalAnalysisAgent
from .binance_client import BinanceClient
from .config import PUBLIC_BASE_URL, Settings

WARMUP = 200  # candles the indicators need before the first decision


def fetch_history(client: BinanceClient, symbol: str, days: int) -> list[dict]:
    start = int((time.time() - (days * 24 + WARMUP) * 3600) * 1000)
    out: list[dict] = []
    while True:
        rows = client.klines(symbol, "1h", limit=1000, start_ms=start)
        if not rows:
            break
        out += rows
        if len(rows) < 1000:
            break
        start = rows[-1]["time"] + 3_600_000
    return out[:-1]  # drop the still-forming current hour


class Backtest:
    def __init__(self, s: Settings, history: dict[str, list[dict]], decision: DecisionAgent | None = None):
        self.s = s
        self.history = history
        self.decision = decision or DecisionAgent()
        self.risk = RiskManagerAgent(s)
        self.technical, self.prediction = TechnicalAnalysisAgent(), PredictionAgent()

    def run(self) -> dict:
        s = self.s
        times = sorted(set.intersection(*(set(c["time"] for c in h) for h in self.history.values())))
        by_time = {sym: {c["time"]: i for i, c in enumerate(h)} for sym, h in self.history.items()}
        cash, positions, trades, curve = s.starting_balance, {}, [], []
        day, day_start = None, s.starting_balance
        decisions = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for t in times[WARMUP:]:
            date = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).date()
            # the live desk analyses the latest 200 candles: use exactly that window
            candles = {sym: self.history[sym][max(0, by_time[sym][t] - WARMUP + 1): by_time[sym][t] + 1]
                       for sym in self.history}
            price = {sym: c[-1]["close"] for sym, c in candles.items()}

            # exchange-side exits during this hour (stop first: the conservative assumption)
            for sym, p in list(positions.items()):
                c = candles[sym][-1]
                if c["low"] <= p["stop"]:
                    cash += self._close(positions, trades, sym, p["stop"] * (1 - STOP_SLIPPAGE), "stop-loss", t)
                elif c["high"] >= p["take"]:
                    cash += self._close(positions, trades, sym, p["take"], "take-profit", t)

            equity = cash + sum(p["qty"] * price[sym] for sym, p in positions.items())
            if date != day:
                day, day_start = date, equity
            portfolio = {"cash": cash, "equity": equity, "open_positions": len(positions),
                         "day_start_equity": day_start, "starting_balance": s.starting_balance}

            buys = []
            for sym, c in candles.items():
                tech = self.technical.run(c)
                reports = {"technical": tech, "prediction": self.prediction.run(c),
                           "news": {"score": 0.0}, "forex": {"score": 0.0}}
                d = self.decision.run(sym, reports, holding=sym in positions)
                decisions[d["action"]] += 1
                if d["action"] == "SELL":
                    verdict = self.risk.review(d, sym, price[sym], tech["atr"], portfolio)
                    if verdict["approved"]:
                        cash += self._close(positions, trades, sym, price[sym], "signal", t)
                elif d["action"] == "BUY":
                    buys.append((d["confidence"], d["combined_score"], sym, tech["atr"], d))
            for _, _, sym, atr, d in sorted(buys, reverse=True):
                portfolio.update(cash=cash, open_positions=len(positions),
                                 equity=cash + sum(p["qty"] * price[x] for x, p in positions.items()))
                verdict = self.risk.review(d, sym, price[sym], atr, portfolio)
                if verdict["approved"]:
                    quote = verdict["quote_amount"]
                    positions[sym] = {"qty": quote * (1 - PAPER_FEE) / price[sym], "cost": quote, "entry": price[sym],
                                      "stop": verdict["stop_loss"], "take": verdict["take_profit"], "opened": t}
                    cash -= quote
            curve.append((t, cash + sum(p["qty"] * price[sym] for sym, p in positions.items())))

        # value anything still open at the last price
        final = curve[-1][1] if curve else s.starting_balance
        return self._metrics(trades, curve, final, decisions, times[WARMUP:])

    @staticmethod
    def _close(positions, trades, sym, px, reason, t) -> float:
        p = positions.pop(sym)
        proceeds = p["qty"] * px * (1 - PAPER_FEE)
        trades.append({"symbol": sym, "pnl": proceeds - p["cost"], "pnl_pct": (proceeds / p["cost"] - 1) * 100,
                       "reason": reason, "hours": (t - p["opened"]) / 3_600_000})
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
        first = {sym: next(c["close"] for c in h if c["time"] == times[0]) for sym, h in self.history.items()} if times else {}
        last = {sym: h[-1]["close"] for sym, h in self.history.items()}
        hold = {sym: (last[sym] / first[sym] - 1) * 100 for sym in first}
        return {
            "days": round(days), "start_balance": s.starting_balance, "final_balance": round(final, 2),
            "return_pct": round((final / s.starting_balance - 1) * 100, 2),
            "buy_and_hold_pct": {sym.removesuffix("USDT"): round(v, 1) for sym, v in hold.items()},
            "basket_hold_pct": round(sum(hold.values()) / len(hold), 1) if hold else None,
            "trades": len(trades), "trades_per_week": round(len(trades) / days * 7, 1),
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
    print(f"Buy-and-hold        basket {r['basket_hold_pct']:+.1f}%   " +
          "  ".join(f"{k} {v:+.1f}%" for k, v in r["buy_and_hold_pct"].items()))
    print(f"Trades              {r['trades']} ({r['trades_per_week']}/week), avg hold {r['avg_hold_hours']} h, "
          f"exits {r['exits']}")
    print(f"Win rate            {r['win_rate_pct']}%   avg win ${r['avg_win']}  avg loss ${r['avg_loss']}  "
          f"profit factor {r['profit_factor']}")
    print(f"Max drawdown        {r['max_drawdown_pct']}%")
    print(f"Decisions           {r['decisions']}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="trader backtest")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--symbols", default="BTC,ETH,SOL")
    ap.add_argument("--balance", type=float, default=10_000)
    ap.add_argument("--buy", type=float, help="buy threshold on the weighted score (default 0.25)")
    ap.add_argument("--sell", type=float, help="sell threshold (default -0.25)")
    ap.add_argument("--stop", type=float, help="stop-loss distance in ATR (default 2)")
    ap.add_argument("--take", type=float, help="take-profit distance in ATR (default 3)")
    ap.add_argument("--position", type=float, help="max position size as a fraction of equity (default 0.10)")
    ap.add_argument("--max-positions", type=int)
    ap.add_argument("--sweep", action="store_true", help="try a grid of thresholds and stop/take distances")
    args = ap.parse_args(argv)

    from .agents import decision as dmod
    symbols = [x.strip().upper() + ("" if x.strip().upper().endswith("USDT") else "USDT") for x in args.symbols.split(",")]
    client = BinanceClient(PUBLIC_BASE_URL)
    print(f"Downloading {args.days} days of hourly prices for {', '.join(symbols)} ...", file=sys.stderr)
    history = {sym: fetch_history(client, sym, args.days) for sym in symbols}

    def settings(stop=None, take=None):
        return Settings(offline=True, symbols=symbols, starting_balance=args.balance,
                        stop_loss_atr=stop or args.stop or 2.0, take_profit_atr=take or args.take or 3.0,
                        max_position_pct=args.position or 0.10, max_open_positions=args.max_positions or 3)

    if not args.sweep:
        dmod.BUY_THRESHOLD = args.buy or dmod.BUY_THRESHOLD
        dmod.SELL_THRESHOLD = args.sell or dmod.SELL_THRESHOLD
        print_result(Backtest(settings(), history).run())
        return 0

    print(f"{'buy':>5} {'sell':>6} {'stop':>5} {'take':>5} | {'return':>8} {'trades':>6} {'win%':>5} {'PF':>5} {'maxDD':>6}")
    for buy, (stop, take) in itertools.product([0.15, 0.25, 0.35, 0.45], [(1.5, 3.0), (2.0, 3.0), (2.0, 4.0), (3.0, 6.0)]):
        dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD = buy, -buy
        r = Backtest(settings(stop, take), history).run()
        print(f"{buy:>5.2f} {-buy:>6.2f} {stop:>5.1f} {take:>5.1f} | {r['return_pct']:>+7.2f}% {r['trades']:>6} "
              f"{r['win_rate_pct'] or 0:>5.1f} {r['profit_factor'] or 0:>5.2f} {r['max_drawdown_pct']:>6.2f}")
    print(f"basket buy-and-hold over the same window: {r['basket_hold_pct']:+.1f}%")
    return 0
