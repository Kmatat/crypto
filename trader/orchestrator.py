"""Orchestrator: runs one trading cycle across all agents.

For every coin, each cycle:
  1. Market Data   -> latest candles (real Binance market prices)
  2. Risk exits    -> sell immediately if stop-loss / take-profit was hit
  3. Technical, Prediction, News, Forex agents -> independent reports
  4. Decision agent -> BUY / SELL / HOLD with confidence and reasoning
  5. Risk Manager  -> approve or veto, set size + stop-loss + take-profit
  6. Execution     -> place the order, confirm it FILLED, record it

Analysis always uses real market data. In testnet mode the orders go to the
Binance Spot Testnet, whose prices can drift from the real market; each order
records the real price next to the fill price so the report can show the gap.
"""
from __future__ import annotations

import logging

from .agents.decision import DecisionAgent
from .agents.execution import ExecutionAgent, PaperBroker, TestnetBroker
from .agents.forex import ForexAgent
from .agents.market import MarketDataAgent
from .agents.news import NewsAgent
from .agents.prediction import PredictionAgent
from .agents.risk import RiskManagerAgent
from .agents.technical import TechnicalAnalysisAgent
from .binance_client import BinanceClient
from .config import PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .journal import Journal
from .llm import ClaudeJSON

log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self, s: Settings, journal: Journal | None = None):
        self.s = s
        self.journal = journal or Journal(s.db_path, s.starting_balance)
        llm = ClaudeJSON(s.claude_model, s.anthropic_key) if s.use_claude else None

        market_client = None if s.offline else BinanceClient(PUBLIC_BASE_URL)
        self.market = MarketDataAgent(market_client, s.interval, offline=s.offline)
        self.technical = TechnicalAnalysisAgent()
        self.prediction = PredictionAgent()
        self.news = NewsAgent(llm, offline=s.offline)
        self.forex = ForexAgent(offline=s.offline)
        self.decision = DecisionAgent(llm)
        self.risk = RiskManagerAgent(s)
        self.last_price: dict[str, float] = {}
        if s.mode == "testnet" and not s.offline:
            broker = TestnetBroker(BinanceClient(TESTNET_BASE_URL, s.api_key, s.api_secret))
        else:
            broker = PaperBroker(lambda sym: self.last_price[sym])
        self.execution = ExecutionAgent(broker)

    # ---- portfolio helpers ----
    def equity(self) -> float:
        pos = self.journal.positions()
        return self.journal.cash + sum(p["qty"] * self.last_price.get(sym, p["entry_price"]) for sym, p in pos.items())

    def portfolio(self) -> dict:
        eq = self.equity()
        return {"cash": self.journal.cash, "equity": eq, "open_positions": len(self.journal.positions()),
                "day_start_equity": self.journal.day_start_equity(eq)}

    # ---- the cycle ----
    def run_cycle(self) -> list[dict]:
        coins = [sym.removesuffix("USDT") for sym in self.s.symbols]
        try:
            news = self.news.run(coins)
        except Exception:
            log.exception("News agent failed - treating news as neutral")
            news = {"_mood": "news agent error"}
        try:
            forex = self.forex.run()
        except Exception:
            log.exception("Forex agent failed - treating macro as neutral")
            forex = ForexAgent._neutral("forex agent error")
        summary = []
        for sym in self.s.symbols:
            try:
                summary.append(self._run_symbol(sym, news, forex))
            except Exception as e:  # one bad coin must not stop the others
                log.exception("Cycle failed for %s", sym)
                summary.append({"symbol": sym, "error": str(e)})
        self.journal.log_equity(self.equity())
        return summary

    def _run_symbol(self, sym: str, news: dict, forex: dict) -> dict:
        candles = self.market.candles(sym)
        price = candles[-1]["close"]
        self.last_price[sym] = price
        position = self.journal.positions().get(sym)

        if position and (why := self.risk.exit_signal(position, price)):
            return self._sell(sym, position, why)

        coin = sym.removesuffix("USDT")
        reports = {
            "technical": self.technical.run(candles),
            "prediction": self.prediction.run(candles),
            "news": news.get(coin, {"score": 0, "summary": "no data"}) | {"market_mood": news.get("_mood")},
            "forex": forex,
        }
        decision = self.decision.run(sym, reports, holding=position is not None)
        verdict = self.risk.review(decision, sym, price, reports["technical"]["atr"], self.portfolio())
        self.journal.log_decision(sym, decision, verdict, reports)
        result = {"symbol": sym, "price": price, "action": decision["action"], "confidence": decision["confidence"],
                  "score": decision["combined_score"], "approved": verdict["approved"], "risk": verdict["reason"],
                  "reasoning": decision["reasoning"]}

        if verdict["approved"] and decision["action"] == "BUY":
            fill = self.execution.execute("BUY", sym, quote_amount=verdict["quote_amount"])
            fill["market_price"] = price
            self.journal.log_order(sym, "BUY", fill, "signal")
            if fill["ok"]:
                self.journal.open_position(sym, fill, verdict["stop_loss"], verdict["take_profit"])
            result.update(order=fill, stop_loss=verdict["stop_loss"], take_profit=verdict["take_profit"])
        elif verdict["approved"] and decision["action"] == "SELL":
            result.update(self._sell(sym, position, "signal"))
        return result

    def _sell(self, sym: str, position: dict, reason: str) -> dict:
        fill = self.execution.execute("SELL", sym, qty=position["qty"])
        fill["market_price"] = self.last_price[sym]
        self.journal.log_order(sym, "SELL", fill, reason)
        out = {"symbol": sym, "action": "SELL", "reason": reason, "order": fill}
        if fill["ok"]:
            out["trade"] = self.journal.close_position(sym, fill, reason)
        return out
