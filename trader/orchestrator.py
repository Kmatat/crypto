"""Orchestrator: runs one trading cycle across all agents.

Each cycle:
  0. Market Scout  -> every few hours, refresh which coins to watch (AI web research)
  1. Market Data   -> latest candles for every watched coin (real Binance market prices)
  2. Protection    -> for each position: did the exchange-side stop-loss / take-profit
                      (OCO pair) fire since the last check? Record the exit. Positions
                      without exchange protection get the desk-side exit check and a
                      new attempt to protect them
  3. News (+ pace), Forex, Technical, Prediction agents -> independent reports
  4. Decision agent -> BUY / SELL / HOLD with confidence and reasoning
  5. Risk Manager  -> approve or veto; buys are ranked so the strongest signals
                      get the limited position slots first
  6. Execution     -> place the order, confirm it FILLED, record it, then place the
                      stop-loss + take-profit pair on the exchange right away
  7. Pace          -> when to check again: normally 15 min, the AI may speed up or
                      slow down; an UNPROTECTED position close to an exit forces 5 min

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
from .agents.mentor import MentorAgent
from .agents.news import NewsAgent
from .agents.prediction import PredictionAgent
from .agents.risk import RiskManagerAgent
from .agents.scout import MarketScoutAgent
from .agents.technical import TechnicalAnalysisAgent
from .binance_client import BinanceClient
from .config import PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .journal import Journal
from .llm import ClaudeJSON
from .notes import Notes, default_dir
from .strategy import StrategyCard, summary_line

log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self, s: Settings, journal: Journal | None = None):
        self.base = s                       # the owner's settings; the champion strategy card is applied on top
        self.s = s
        self.journal = journal or Journal(s.db_path, s.starting_balance)
        self.notes = Notes(self.journal, default_dir(s, self.journal.path), s)  # the Markdown notebook (data/notes)
        self.card: StrategyCard | None = None
        self.llm = ClaudeJSON(s.claude_model, s.anthropic_key, s.ai_daily_budget_usd, store=self.journal) \
            if s.use_claude else None
        if self.llm and s.ai_budget_pct_month:
            # e.g. 0.25% of $10,000 a month = $25/month = about $0.83 a day: the AI bill stays a small fraction
            # of any plausible edge, so a cheap model is the only sensible choice at this account size
            self.llm.pct_daily_budget = s.starting_balance * s.ai_budget_pct_month / 100 / 30

        public = None if s.offline else BinanceClient(PUBLIC_BASE_URL)
        testnet = BinanceClient(TESTNET_BASE_URL, s.api_key, s.api_secret) \
            if s.mode == "testnet" and not s.offline else None
        self.testnet = testnet
        self.market = MarketDataAgent(public, s.interval, offline=s.offline)
        self.scout = MarketScoutAgent(public, testnet, self.llm, s) if public and s.auto_coins else None
        self.technical = TechnicalAnalysisAgent()
        self.prediction = PredictionAgent()
        self.news = NewsAgent(self.llm, offline=s.offline, cache_seconds=s.news_refresh_minutes * 60)
        self.forex = ForexAgent(offline=s.offline)
        self.decision = DecisionAgent(self.llm)
        self.risk = RiskManagerAgent(s)
        self.mentor = MentorAgent(s.mentor_checks, s.min_reward_risk, s.cooldown_hours) if s.mentor_checks else None
        self.last_price: dict[str, float] = {}
        broker = TestnetBroker(testnet) if testnet else \
            PaperBroker(lambda sym: self.last_price[sym], range_fn=self.market.range_since)
        self.execution = ExecutionAgent(broker)
        self.protect_errors: dict[str, str] = {}  # symbol -> last reason protection could not be placed
        self.pace = {"minutes": s.cycle_minutes, "reason": "normal pace", "source": "settings"}
        self.scout_update: dict | None = None  # set when the watchlist changed this cycle
        self._scout_retry_at = 0.0

    # ---- portfolio helpers ----
    def equity(self) -> float:
        pos = self.journal.positions()
        return self.journal.cash + sum(p["qty"] * self.last_price.get(sym, p["entry_price"]) for sym, p in pos.items())

    def portfolio(self) -> dict:
        eq = self.equity()
        start = float(self.journal.get("starting_balance") or eq)
        tripped = self.journal.get("brake_tripped_at")
        if tripped and eq >= start * (1 - self.s.max_drawdown_pct / 2):
            self.journal.set("brake_tripped_at", None)  # recovered half the loss: back to normal
            tripped = None
        days = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(tripped)).total_seconds() / 86400 \
            if tripped else None
        return {"cash": self.journal.cash, "equity": eq, "open_positions": len(self.journal.positions()),
                "day_start_equity": self.journal.day_start_equity(eq), "starting_balance": start,
                "brake_tripped_days": days}

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
        if self.scout_update["added"] or self.scout_update["removed"]:
            self.journal.event("watchlist", "Coin list changed: added " + (", ".join(self.scout_update["added"]) or "none")
                               + "; removed " + (", ".join(self.scout_update["removed"]) or "none")
                               + f". Now watching {', '.join(new['symbols'])}.")

    # ---- strategy card ----
    def apply_champion(self) -> StrategyCard:
        """Run whatever strategy card is champion (set by the lab, the owner's /approve, or a rollback)."""
        state = self.journal.get("strategy_champion")
        if state:
            card = StrategyCard.from_dict(state["card"])
        else:
            card = StrategyCard.from_settings(self.base)
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            self.journal.set("strategy_champion", {"card": card.to_dict(), "since": now, "by": "owner settings",
                                                   "previous": None})
            hist = self.journal.get("strategy_history") or []   # same record the lab writes, so the lineage is complete
            hist.append({"ts": now, "event": "promote", "id": card.id, "name": card.name, "by": "owner settings",
                         "note": "first run"})
            self.journal.set("strategy_history", hist[-100:])
            self.journal.event("strategy", f"First champion recorded from the owner's settings: {summary_line(card)}")
        if self.card is None or card.id != self.card.id:
            self.s = card.settings(self.base)
            card.apply_decision_rules()
            self.risk = RiskManagerAgent(self.s)
            self.mentor = MentorAgent(self.s.mentor_checks, self.s.min_reward_risk, self.s.cooldown_hours) \
                if self.s.mentor_checks else None
            self.market.interval = self.s.interval
            if self.card is not None:
                log.info("Strategy switched to %s [%s]", card.name, card.id)
                self.journal.event("strategy", f"The desk now trades {card.name} [{card.id}]: "
                                               f"{card.describe_diff(self.card)}")
            self.card = card
        return card

    # ---- the cycle ----
    def run_cycle(self) -> list[dict]:
        summary: list[dict] = []
        self.apply_champion()
        try:
            self.maybe_scout()
        except Exception as e:  # keep trading the current watchlist
            log.exception("Market scout failed")
            summary.append({"symbol": "SCOUT", "error": f"could not refresh the coin list: {e}"})
        wl = self.watchlist()
        symbols = list(dict.fromkeys(wl["symbols"] + list(self.journal.positions())))
        summary += self.reconcile()

        # 1-2. prices, analysis, exchange-side exits, desk-side backup exits
        ctx: dict[str, dict] = {}
        for sym in symbols:
            try:
                candles = self.market.candles(sym)
                price = candles[-1]["close"]          # the live price: stops, trailing, sizing, the journal
                self.last_price[sym] = price
                position = self.journal.positions().get(sym)
                closed = self.market.closed(candles)  # the decision looks only at finished candles (as the backtester)
                technical = self.technical.run(closed)
                technical["price"] = price
                if position:
                    position, exit_result = self._sync_protection(sym, position)
                    if exit_result:
                        summary.append(exit_result)
                    if position is None:
                        continue
                    position, exit_result = self._trail(sym, position, price, technical["atr"])
                    if exit_result:
                        summary.append(exit_result)
                    if position is None:
                        continue
                    if not self._is_protected(position):
                        if why := self.risk.exit_signal(position, price):  # desk-side backup
                            summary.append(self._sell(sym, position, why))
                            continue
                        if err := self._protect(sym, position):
                            summary.append({"symbol": sym, "protect_error": err})
                ctx[sym] = {"price": price, "technical": technical, "prediction": self.prediction.run(closed),
                            "closes": [c["close"] for c in closed], "candles": closed}
            except Exception as e:  # one bad coin must not stop the others
                log.exception("Market data failed for %s", sym)
                summary.append({"symbol": sym, "error": str(e)})

        if ctx and not self.journal.get("benchmark_start"):  # first cycle: remember where the market was
            prices = {sym: c["price"] for sym, c in ctx.items() if sym in self.s.symbols}
            if "BTCUSDT" not in prices and self.market.client is not None:
                try:
                    prices["BTCUSDT"] = self.market.client.price("BTCUSDT")
                except Exception as e:
                    log.warning("Could not record the Bitcoin benchmark price: %s", e)
            self.journal.set("benchmark_start", {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                 "prices": prices})

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
                review = self._mentor_review(sym, c) if self.mentor and position is None else None
                if review:
                    reports["mentor"] = {"pass": review["pass"], "checklist": MentorAgent.summary(review)}
                    c["mentor"] = review
                # Claude costs money: skip it when a buy is doomed anyway (checklist failed, no free slot, limits)
                slots_full = position is None and len(positions) >= self.s.max_open_positions
                use_ai = not (position is None and ((review and not review["pass"]) or slots_full))
                decision = self.decision.run(sym, reports, holding=position is not None, use_ai=use_ai)
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
                if verdict.get("trip"):
                    self.journal.set("brake_tripped_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
                    self.journal.event("brake", f"Overall loss brake tripped: {verdict['reason']}. No new buys for "
                                                f"{self.s.brake_pause_days} days, then half size until half the loss is back.")
                review = c.get("mentor")
                if verdict["approved"] and review and not review["pass"]:
                    names = ", ".join(review["failed"])
                    verdict = {"approved": False, "reason": f"master traders' checklist: {names} not met"}
                self.journal.log_decision(sym, decision, verdict, reports)
                result = self._result(sym, c, decision, verdict)
                if review:
                    result["mentor"] = MentorAgent.summary(review)
                if verdict["approved"]:
                    fill = self.execution.execute("BUY", sym, quote_amount=verdict["quote_amount"])
                    fill["market_price"] = c["price"]
                    self.journal.log_order(sym, "BUY", fill, "signal")
                    if not fill["ok"]:
                        self.journal.event("order", f"BUY {sym} failed: {fill.get('error')}")
                    if fill["ok"]:
                        self.journal.open_position(sym, fill, verdict["stop_loss"], verdict["take_profit"])
                        self.journal.event("trade", f"BUY {sym}: {fill['qty']:.6g} at {fill['avg_price']:,.6g} = "
                                                    f"{fill['quote']:,.2f} USDT · stop {verdict['stop_loss']:,.6g} · target "
                                                    f"{verdict['take_profit']:,.6g} · confidence {decision['confidence']:.0%} "
                                                    f"({decision.get('source') or 'rules'}): {str(decision['reasoning'])[:240]}")
                        position = self.journal.positions()[sym]
                        if err := self._protect(sym, position):
                            result["protect_error"] = err
                            if why := self.risk.exit_signal(position, self.last_price[sym]):
                                result.update(self._sell(sym, position, why))  # already past an exit level
                        else:
                            result["protected"] = self.journal.positions()[sym]["protected"]
                    result.update(order=fill, stop_loss=verdict["stop_loss"], take_profit=verdict["take_profit"])
                summary.append(result)
            except Exception as e:
                log.exception("Buy failed for %s", sym)
                summary.append({"symbol": sym, "error": str(e)})

        # 7. when to check again
        self.pace = self._next_pace(news.get("_pace"), ctx)
        self.journal.log_equity(self.equity())
        self.notes.refresh(self.last_price, scorecard=self.journal.get("scorecard_text") or "")
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
            if self._is_protected(p):
                continue  # the exchange watches this one every second; no need to hurry
            near.append(sym)  # no exchange orders: the desk is the only safety net, so check often
        if near and pace["minutes"] > s.min_check_minutes:
            pace = {"minutes": s.min_check_minutes, "source": "risk",
                    "reason": f"{', '.join(near)} held without exchange protection"}
        return pace

    def reconcile(self) -> list[dict]:
        """Testnet only: do we still hold the coins the journal says we hold? Binance resets the demo
        exchange now and then, and a position whose coins vanished would otherwise fail to sell forever."""
        if not self.testnet or not self.journal.positions():
            return []
        try:
            held = self.testnet.balances_total()
        except Exception as e:
            log.warning("Could not read exchange balances: %s", e)
            return []
        out = []
        for sym, p in self.journal.positions().items():
            step = self.execution.step_size(sym)
            if step and p["qty"] < step:  # dust left by an earlier version: unsellable, unprotectable, blocks a slot
                self.journal.write_off(sym)
                self.journal.event("trade", f"Wrote off {p['qty']:.8g} {sym.removesuffix('USDT')} of dust (below the "
                                            f"exchange's step size {step:g}, bought for {p['cost']:,.2f} USDT): it could "
                                            "neither be sold nor protected and was blocking the coin and a position slot.")
                out.append({"symbol": sym, "action": "DUST", "reason": f"wrote off {p['qty']:.8g} below step {step:g}"})
                continue
            have = held.get(sym.removesuffix("USDT"), 0.0)
            if have < p["qty"] * 0.99:
                out.append({"symbol": sym, "error": f"the journal says we hold {p['qty']:.6g} but the exchange only "
                                                    f"has {have:.6g}. Binance probably reset the demo account. "
                                                    f"Run `desk reset --force` to start the demo fresh."})
        return out

    # ---- master traders' checklist and trailing stops ----
    def _mentor_review(self, sym: str, c: dict) -> dict | None:
        try:
            daily = self.market.daily_closes(sym)
            btc_daily = daily if sym == "BTCUSDT" else self.market.daily_closes("BTCUSDT")
        except Exception as e:
            log.warning("Daily history unavailable for %s: %s", sym, e)
            daily, btc_daily = [], None
        atr = c["technical"]["atr"]
        stop, take = c["price"] - self.s.stop_loss_atr * atr, c["price"] + self.s.take_profit_atr * atr
        return self.mentor.review(sym, c["candles"], daily, c["price"], stop, take, atr, btc_daily,
                                  self.journal.last_stop_loss_ms(sym), int(time.time() * 1000))

    def _trail(self, sym: str, position: dict, price: float, atr: float) -> tuple[dict | None, dict | None]:
        """Raise the stop-loss as the price makes new highs; re-place the exchange orders when it moves.
        Returns (position or None if it turned out to be closed, exit summary entry or None)."""
        high = max(position.get("high_water") or position["entry_price"], price)
        new_stop = self.risk.trailing_stop(position, high, atr)
        if new_stop is None:
            if high != position.get("high_water"):
                self.journal.set_high_water(sym, high)
            return self.journal.positions()[sym], None
        if position.get("protection"):
            status = self.execution.unprotect(sym, position["protection"])
            if status["state"] == "exited":  # it filled while we were about to move the stop
                exit_result = self._record_exchange_exit(sym, status)
                return self.journal.positions().get(sym), exit_result
            if status["state"] == "error":
                return position, None  # keep the old orders; try again next cycle
            self.journal.set_protection(sym, None, "none")
        self.journal.update_stop(sym, new_stop, high)
        log.info("%s: trailing stop raised to %.6g (high %.6g)", sym, new_stop, high)
        self._protect(sym, self.journal.positions()[sym])
        return self.journal.positions()[sym], None

    # ---- protection (stop-loss / take-profit orders living on the exchange) ----
    @staticmethod
    def _is_protected(position: dict) -> bool:
        return position.get("protected") in ("exchange", "emulated")

    def _protect(self, sym: str, position: dict) -> str | None:
        """Place the OCO pair for a position. Returns an error text when it could not be placed."""
        if not self.execution.can_protect:
            return None
        res = self.execution.protect(sym, position["qty"], position["stop_loss"], position["take_profit"])
        if res["ok"]:
            self.journal.set_protection(sym, res["protection"], res["protection"]["kind"])
            self.protect_errors.pop(sym, None)
            return None
        self.journal.set_protection(sym, None, "none")
        if self.protect_errors.get(sym) != res["error"]:
            self.journal.event("protection", f"Could not place the stop-loss / take-profit for {sym} on the exchange: "
                                             f"{res['error']}. The desk watches it itself every few minutes meanwhile.")
        self.protect_errors[sym] = res["error"]
        return res["error"]

    def _sync_protection(self, sym: str, position: dict) -> tuple[dict | None, dict | None]:
        """Ask the exchange what happened to the protective orders since the last check.
        Returns (position or None when it was closed, summary entry for an exit or None)."""
        prot = position.get("protection")
        if not prot:
            return position, None
        status = self.execution.protection_status(sym, prot)
        state = status["state"]
        if state == "exited":  # a partial fill leaves the rest open (and unprotected) for the caller to handle
            exit_result = self._record_exchange_exit(sym, status)
            return self.journal.positions().get(sym), exit_result
        if state == "protected":
            if status["protection"] != prot:
                self.journal.set_protection(sym, status["protection"], prot["kind"])
            return self.journal.positions()[sym], None
        if state == "executing":  # one leg is part-filled, the other is gone: desk watches the rest
            self.journal.set_protection(sym, prot, "partial")
        elif state == "gone":  # cancelled or wiped (Binance resets the testnet now and then): re-protect
            log.warning("Protection for %s disappeared (%s) - placing it again", sym, status.get("detail"))
            if hasattr(self.execution.broker, "cancel_quietly"):
                self.execution.broker.cancel_quietly(sym, prot)  # make sure nothing stays locked
            self.journal.set_protection(sym, None, "none")
        # "unknown" (network problem): keep the record, try again next cycle
        return self.journal.positions()[sym], None

    def _record_exchange_exit(self, sym: str, status: dict) -> dict:
        fill = {"ok": True, **status["fill"], "market_price": self.last_price.get(sym)}
        kind = "exchange" if status["protection"]["kind"] == "exchange" else "emulated"
        reason = f"{status['reason']} ({kind})"
        self.journal.log_order(sym, "SELL", fill, reason)
        trade = self.journal.close_position(sym, fill, reason, step=self.execution.step_size(sym))
        self._exit_event(sym, fill, trade)
        if sym in self.journal.positions():  # part of the position is left: protect it again
            self.journal.set_protection(sym, None, "none")
        return {"symbol": sym, "action": "SELL", "reason": reason, "order": fill, "trade": trade}

    def _exit_event(self, sym: str, fill: dict, trade: dict) -> None:
        hours = (dt.datetime.fromisoformat(trade["closed_at"]) - dt.datetime.fromisoformat(trade["opened_at"])).total_seconds() / 3600
        self.journal.event("trade", f"SELL {sym}: {fill['qty']:.6g} at {fill['avg_price']:,.6g} = {fill['quote']:,.2f} USDT · "
                                    f"{trade['exit_reason']} · {'profit' if trade['pnl'] > 0 else 'loss'} "
                                    f"{trade['pnl']:+,.2f} USDT ({trade['pnl_pct']:+.2f}%) after {hours:.1f} h")

    def _sell(self, sym: str, position: dict, reason: str) -> dict:
        if position.get("protection"):  # free the coins first: open orders lock them
            status = self.execution.unprotect(sym, position["protection"])
            if status["state"] == "exited":
                return self._record_exchange_exit(sym, status)
            if status["state"] == "error":
                fill = {"ok": False, "error": status["detail"], "market_price": self.last_price[sym]}
                self.journal.log_order(sym, "SELL", fill, reason)
                self.journal.event("order", f"SELL {sym} failed ({reason}): could not cancel the protective orders: "
                                            f"{status['detail']}")
                return {"symbol": sym, "action": "SELL", "reason": reason, "order": fill}
            self.journal.set_protection(sym, None, "none")
            position = self.journal.positions()[sym]
        fill = self.execution.execute("SELL", sym, qty=position["qty"])
        fill["market_price"] = self.last_price[sym]
        self.journal.log_order(sym, "SELL", fill, reason)
        out = {"symbol": sym, "action": "SELL", "reason": reason, "order": fill}
        if fill["ok"]:
            out["trade"] = self.journal.close_position(sym, fill, reason, step=self.execution.step_size(sym))
            self._exit_event(sym, fill, out["trade"])
            if sym in self.journal.positions():
                self._protect(sym, self.journal.positions()[sym])  # partial sale: protect the remainder
        elif self.journal.positions().get(sym):
            self.journal.event("order", f"SELL {sym} failed ({reason}): {fill.get('error')}")
            self._protect(sym, self.journal.positions()[sym])  # sale failed: put the protection back
        return out
