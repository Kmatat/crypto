"""Orchestrator: runs one trading cycle across all agents.

Each cycle:
  0. Market Scout  -> every few hours, refresh which coins to watch (AI web research)
  1. Market Data   -> latest candles for every watched coin (real Binance market prices)
  2. Risk exits    -> sell immediately if stop-loss / take-profit was hit
  3. News (+ pace), Forex, Technical, Prediction agents -> independent reports
  4. Decision agent -> BUY / SELL / HOLD with confidence and reasoning
  5. Risk Manager  -> approve or veto; buys are ranked so the strongest signals
                      get the limited position slots first
  6. Execution     -> place the order, confirm it FILLED, record it
  7. Pace          -> when to check again: normally 15 min, the AI may speed up
                      or slow down, and positions close to an exit force 5 min

Analysis always uses real market data. In testnet mode the orders go to the
Binance Spot Testnet, whose prices can drift from the real market; each order
records the real price next to the fill price so the report can show the gap.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

from .agents.decision import DecisionAgent
from .agents.execution import ExecutionAgent, PaperBroker, TestnetBroker
from .agents.forex import ForexAgent
from .agents.market import MarketDataAgent
from .agents.news import NewsAgent
from .agents.prediction import PredictionAgent
from .agents.risk import RiskManagerAgent
from .agents.scout import MarketScoutAgent
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
        self.llm = ClaudeJSON(s.claude_model, s.anthropic_key, s.ai_daily_budget_usd) if s.use_claude else None

        public = None if s.offline else BinanceClient(PUBLIC_BASE_URL)
        testnet = BinanceClient(TESTNET_BASE_URL, s.api_key, s.api_secret) \
            if s.mode == "testnet" and not s.offline else None
        self.market = MarketDataAgent(public, s.interval, offline=s.offline)
        self.scout = MarketScoutAgent(public, testnet, self.llm, s) if public and s.auto_coins else None
        self.technical = TechnicalAnalysisAgent()
        self.prediction = PredictionAgent()
        self.news = NewsAgent(self.llm, offline=s.offline, cache_seconds=s.news_refresh_minutes * 60)
        self.forex = ForexAgent(offline=s.offline)
        self.decision = DecisionAgent(self.llm)
        self.risk = RiskManagerAgent(s)
        self.last_price: dict[str, float] = {}
        broker = TestnetBroker(testnet) if testnet else PaperBroker(lambda sym: self.last_price[sym])
        self.execution = ExecutionAgent(broker)
        self.pace = {"minutes": s.cycle_minutes, "reason": "normal pace", "source": "settings"}
        self.scout_update: dict | None = None  # set when the watchlist changed this cycle
        self._scout_retry_at = 0.0

    # ---- portfolio helpers ----
    def equity(self) -> float:
        pos = self.journal.positions()
        return self.journal.cash + sum(p["qty"] * self.last_price.get(sym, p["entry_price"]) for sym, p in pos.items())

    def portfolio(self) -> dict:
        eq = self.equity()
        return {"cash": self.journal.cash, "equity": eq, "open_positions": len(self.journal.positions()),
                "day_start_equity": self.journal.day_start_equity(eq)}

    # ---- watchlist ----
    def watchlist(self) -> dict:
        wl = self.journal.get("watchlist")
        if not self.scout or not wl:
            return {"symbols": list(self.s.symbols), "reasons": {c: "core coin (always watched)" for c in self.s.symbols},
                    "source": "settings"}
        return wl

    def maybe_scout(self) -> None:
        """Refresh the watchlist when it is older than SCOUT_HOURS (kept across restarts)."""
        self.scout_update = None
        if not self.scout or time.time() < self._scout_retry_at:
            return
        old = self.journal.get("watchlist")
        same_settings = old and set(old.get("core", [])) == set(self.s.symbols) and old.get("max_coins") == self.s.max_coins
        if same_settings:
            age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(old["ts"])).total_seconds() / 3600
            if age_h < self.s.scout_hours:
                return
        try:
            new = self.scout.run(core=list(self.s.symbols), held=list(self.journal.positions()))
        except Exception:
            self._scout_retry_at = time.time() + 30 * 60  # don't retry every cycle
            raise
        new.update(core=list(self.s.symbols), max_coins=self.s.max_coins)
        self.journal.set("watchlist", new)
        before = set(old["symbols"]) if old else set(self.s.symbols)
        self.scout_update = {**new, "added": [s for s in new["symbols"] if s not in before],
                             "removed": [s for s in before if s not in new["symbols"]]}

    # ---- the cycle ----
    def run_cycle(self) -> list[dict]:
        summary: list[dict] = []
        try:
            self.maybe_scout()
        except Exception as e:  # keep trading the current watchlist
            log.exception("Market scout failed")
            summary.append({"symbol": "SCOUT", "error": f"could not refresh the coin list: {e}"})
        wl = self.watchlist()
        symbols = list(dict.fromkeys(wl["symbols"] + list(self.journal.positions())))

        # 1-2. prices, analysis, and immediate exits
        ctx: dict[str, dict] = {}
        for sym in symbols:
            try:
                candles = self.market.candles(sym)
                price = candles[-1]["close"]
                self.last_price[sym] = price
                position = self.journal.positions().get(sym)
                technical = self.technical.run(candles)
                if position and (why := self.risk.exit_signal(position, price)):
                    summary.append(self._sell(sym, position, why))
                    continue
                ctx[sym] = {"price": price, "technical": technical, "prediction": self.prediction.run(candles),
                            "closes": [c["close"] for c in candles]}
            except Exception as e:  # one bad coin must not stop the others
                log.exception("Market data failed for %s", sym)
                summary.append({"symbol": sym, "error": str(e)})

        # 3. news (with a market snapshot, so the AI can set the pace) and forex
        positions = self.journal.positions()
        snapshot = {sym.removesuffix("USDT"): self._snapshot(c, sym in positions) for sym, c in ctx.items()}
        try:
            news = self.news.run(list(snapshot), snapshot) if snapshot else {"_mood": "no market data", "_pace": None}
        except Exception:
            log.exception("News agent failed - treating news as neutral")
            news = {"_mood": "news agent error", "_pace": None}
        try:
            forex = self.forex.run()
        except Exception:
            log.exception("Forex agent failed - treating macro as neutral")
            forex = ForexAgent._neutral("forex agent error")

        # 4. decisions; sells right away, buys collected and ranked
        buys = []
        for sym, c in ctx.items():
            try:
                coin = sym.removesuffix("USDT")
                reports = {
                    "technical": c["technical"],
                    "prediction": c["prediction"],
                    "news": news.get(coin, {"score": 0, "summary": "no data"}) | {"market_mood": news.get("_mood")},
                    "forex": forex,
                    "watchlist": {"why_watched": wl.get("reasons", {}).get(sym, "")},
                }
                position = positions.get(sym)
                decision = self.decision.run(sym, reports, holding=position is not None)
                if decision["action"] == "BUY":
                    buys.append((sym, c, reports, decision))
                    continue
                verdict = self.risk.review(decision, sym, c["price"], c["technical"]["atr"], self.portfolio())
                self.journal.log_decision(sym, decision, verdict, reports)
                result = self._result(sym, c, decision, verdict)
                if verdict["approved"] and decision["action"] == "SELL":
                    result.update(self._sell(sym, position, "signal"))
                summary.append(result)
            except Exception as e:
                log.exception("Decision failed for %s", sym)
                summary.append({"symbol": sym, "error": str(e)})

        # 5-6. buys: strongest signals first, while position slots and cash last
        buys.sort(key=lambda b: (b[3]["confidence"], b[3]["combined_score"]), reverse=True)
        for sym, c, reports, decision in buys:
            try:
                verdict = self.risk.review(decision, sym, c["price"], c["technical"]["atr"], self.portfolio())
                self.journal.log_decision(sym, decision, verdict, reports)
                result = self._result(sym, c, decision, verdict)
                if verdict["approved"]:
                    fill = self.execution.execute("BUY", sym, quote_amount=verdict["quote_amount"])
                    fill["market_price"] = c["price"]
                    self.journal.log_order(sym, "BUY", fill, "signal")
                    if fill["ok"]:
                        self.journal.open_position(sym, fill, verdict["stop_loss"], verdict["take_profit"])
                    result.update(order=fill, stop_loss=verdict["stop_loss"], take_profit=verdict["take_profit"])
                summary.append(result)
            except Exception as e:
                log.exception("Buy failed for %s", sym)
                summary.append({"symbol": sym, "error": str(e)})

        # 7. when to check again
        self.pace = self._next_pace(news.get("_pace"), ctx)
        self.journal.log_equity(self.equity())
        return summary

    @staticmethod
    def _result(sym: str, c: dict, decision: dict, verdict: dict) -> dict:
        return {"symbol": sym, "price": c["price"], "action": decision["action"], "confidence": decision["confidence"],
                "score": decision["combined_score"], "approved": verdict["approved"], "risk": verdict["reason"],
                "reasoning": decision["reasoning"], "source": decision.get("source")}

    @staticmethod
    def _snapshot(c: dict, holding: bool) -> dict:
        closes = c["closes"]
        return {"change_1h_pct": (closes[-1] / closes[-2] - 1) * 100,
                "change_24h_pct": (closes[-1] / closes[-25] - 1) * 100 if len(closes) > 25 else 0.0,
                "volatility_pct": c["technical"]["atr"] / c["price"] * 100, "holding": holding}

    def _next_pace(self, ai: dict | None, ctx: dict) -> dict:
        s = self.s
        if ai and ai.get("minutes"):
            pace = {"minutes": max(s.min_check_minutes, min(s.max_check_minutes, int(ai["minutes"]))),
                    "reason": ai.get("reason", ""), "source": "AI"}
        else:
            pace = {"minutes": s.cycle_minutes, "reason": "normal pace", "source": "settings"}
        near = []
        for sym, p in self.journal.positions().items():
            c = ctx.get(sym)
            if c and c["technical"]["atr"] > 0:
                gap = min(c["price"] - p["stop_loss"], p["take_profit"] - c["price"]) / c["technical"]["atr"]
                if gap < 0.5:  # within half a typical move of its stop-loss or target
                    near.append(sym)
        if near and pace["minutes"] > s.min_check_minutes:
            pace = {"minutes": s.min_check_minutes, "source": "risk",
                    "reason": f"{', '.join(near)} close to its stop-loss or target"}
        return pace

    def _sell(self, sym: str, position: dict, reason: str) -> dict:
        fill = self.execution.execute("SELL", sym, qty=position["qty"])
        fill["market_price"] = self.last_price[sym]
        self.journal.log_order(sym, "SELL", fill, reason)
        out = {"symbol": sym, "action": "SELL", "reason": reason, "order": fill}
        if fill["ok"]:
            out["trade"] = self.journal.close_position(sym, fill, reason)
        return out
