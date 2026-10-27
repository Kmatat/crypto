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

from . import free_ai, trend
from .agents.decision import NEWS_VETO, DecisionAgent
from .agents.execution import ExecutionAgent, PaperBroker, TestnetBroker
from .agents.forex import ForexAgent
from .agents.head import HeadTraderAgent
from .agents.market import MarketDataAgent
from .agents.mentor import MentorAgent
from .agents.news import NewsAgent
from .agents.prediction import PredictionAgent
from .agents.risk import RiskManagerAgent
from .agents.scout import MarketScoutAgent
from .agents.technical import TechnicalAnalysisAgent
from .ai_router import AIRouter, effective_order
from .binance_client import BinanceClient
from .config import AI_BUDGET_CEILING_USD, PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .journal import Journal
from .llm import ClaudeJSON
from .notes import Notes, default_dir
from .strategy import StrategyCard, summary_line

log = logging.getLogger("orchestrator")
TREND_TRIES = 4   # checks in a row the daily trend step is tried when a coin's prices or a sale fail
DEMO_GAP = 0.03   # the demo exchange's price may differ this much from the real market before a buy is refused


class Orchestrator:
    def __init__(self, s: Settings, journal: Journal | None = None):
        self.base = s                       # the owner's settings; the champion strategy card is applied on top
        self.s = s
        self.journal = journal or Journal(s.db_path, s.starting_balance)
        self.notes = Notes(self.journal, default_dir(s, self.journal.path), s)  # the Markdown notebook (data/notes)
        self.card: StrategyCard | None = None
        self.llm = ClaudeJSON(s.claude_model, s.anthropic_key, s.ai_daily_budget_usd, store=self.journal) \
            if s.use_claude else None
        if self.llm and s.ai_pct_month():
            # only when the owner sets AI_BUDGET_PCT_MONTH (demo money has none): e.g. 0.25% of $10,000 a month =
            # $25/month = about $0.83 a day
            self.llm.pct_daily_budget = s.starting_balance * s.ai_pct_month() / 100 / 30
        if self.llm and (override := self.journal.get("ai_budget_override")):
            # the owner's /aibudget, kept across restarts (never above the hard ceiling, whatever the stored value)
            self.llm.daily_budget = min(float(override["usd_per_day"]), AI_BUDGET_CEILING_USD)
            self.llm.pct_daily_budget = 0.0

        # the free AI answers the everyday questions within its daily allowance; the router sends each question to
        # the free AI or Claude in the owner's order (AI_ORDER, or /ai on Telegram), the free rules after both
        self.free_ai = free_ai.from_settings(s, store=self.journal)
        self.ai = AIRouter(self.llm, self.free_ai, effective_order(s, self.journal)) if self.free_ai else self.llm

        public = None if s.offline else BinanceClient(PUBLIC_BASE_URL)
        testnet = BinanceClient(TESTNET_BASE_URL, s.api_key, s.api_secret) \
            if s.mode == "testnet" and not s.offline else None
        self.testnet = testnet
        self.market = MarketDataAgent(public, s.interval, offline=s.offline)
        # the daily trend strategy trades the owner's coins on a measured rule: no scout, head trader or AI decisions
        self.scout = MarketScoutAgent(public, testnet, self.ai, s) if public and s.auto_coins and not s.trend else None
        self.technical = TechnicalAnalysisAgent()
        self.prediction = PredictionAgent()
        self.news = NewsAgent(self.ai, offline=s.offline, cache_seconds=s.news_refresh_minutes * 60)
        self.forex = ForexAgent(offline=s.offline)
        self.decision = DecisionAgent(self.ai)
        self.head = HeadTraderAgent(s, self.journal, self.ai) if s.head_trader and not s.trend else None
        self.risk = RiskManagerAgent(s)
        self.mentor = MentorAgent(s.mentor_checks, s.min_reward_risk, s.cooldown_hours) if s.mentor_checks else None
        self.last_price: dict[str, float] = {}
        broker = TestnetBroker(testnet) if testnet else \
            PaperBroker(lambda sym: self.last_price[sym], range_fn=self.market.range_since)
        self.execution = ExecutionAgent(broker)
        self.protect_errors: dict[str, str] = {}  # symbol -> last reason protection could not be placed
        self.pace = {"minutes": s.cycle_minutes, "reason": "normal pace", "source": "settings"}
        self.scout_update: dict | None = None  # set when the watchlist changed this cycle
        self.trend_report: dict | None = None  # set when the daily trend decisions ran this cycle (Telegram summary)
        self._trend_logged: set = set()        # (day, coin, action, reason) already recorded: retries add no duplicates
        self.champion_note: str | None = None    # set when the strategy in force was replaced at start (see apply_champion)
        self._scout_retry_at = 0.0

    # ---- portfolio helpers ----
    def equity(self) -> float:
        pos = self.journal.positions()
        return self.journal.cash + sum(p["qty"] * self.last_price.get(sym, p["entry_price"]) for sym, p in pos.items())

    def apply_budget_change(self) -> list[str]:
        """The owner changed STARTING_BALANCE_USDT after the record began. The record keeps the budget it started with
        (sizes, loss brakes and reports all read it), so a new value would otherwise never take effect. It is applied
        like a deposit or a withdrawal: the difference goes to the cash, the trades and the result so far are kept.
        On the Binance demo account it is applied only if the demo wallet can pay for it, because Binance refuses
        orders the wallet cannot cover."""
        j, new = self.journal, float(self.base.starting_balance)
        old = float(j.get("starting_balance") or new)
        if abs(new - old) < 0.005:
            j.set("budget_problem", None)
            return []
        cash, delta = j.cash, new - old
        new_cash = cash + delta
        result = self.equity() - old   # what the desk made or lost so far, kept as it is
        if new_cash < 0:
            invested = sum(p["cost"] for p in j.positions().values())
            text = (f"STARTING_BALANCE_USDT is ${new:,.2f}, less than what the record has already used "
                    f"(${old - cash:,.2f}: ${invested:,.2f} in open positions, the rest losses so far): the desk keeps "
                    f"trading with ${old:,.2f}. Choose a larger budget, or start a fresh record with "
                    f"`desk reset --force`.")
            j.set("budget_problem", text)
            return [f"🛑 {text}"]
        if self.testnet is not None:
            try:
                usdt = float(self.testnet.balances().get("USDT", 0.0))
            except Exception as e:  # noqa: BLE001 - no wallet reading: do not apply what cannot be checked
                log.warning("Budget change: could not read the demo wallet: %s", e)
                return [f"⚠️ STARTING_BALANCE_USDT changed to ${new:,.0f}, but the Binance demo wallet could not be read to "
                        f"check it; the desk keeps ${old:,.0f} and tries again at the next start."]
            if new_cash > usdt + 1:
                text = (f"you set STARTING_BALANCE_USDT to ${new:,.0f}, but the Binance demo wallet holds only "
                        f"{usdt:,.2f} USDT, and Binance refuses orders the wallet cannot pay for. The desk keeps trading "
                        f"with its record of ${old:,.2f} (cash ${cash:,.2f}). For a ${new:,.0f} budget, switch to paper "
                        f"trading (real prices, simulated orders): run `desk setup`, choose 2) Paper trading and type "
                        f"{new:.0f}. Or keep the demo account with a budget of at most {max(0.0, usdt - (cash - old) - 1):,.0f}.")
                j.set("budget_problem", text)
                j.event("error", "Budget not changed: " + text)
                return [f"🛑 Budget not changed: {text}"]
        j.set("cash", round(new_cash, 8))
        j.set("starting_balance", new)
        day = j.get("day_start")
        if day:   # the day's loss limit compares with the day's first equity: move it by the same amount
            j.set("day_start", {**day, "equity": day["equity"] + delta})
        when = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        j.set("budget_changes", (j.get("budget_changes") or []) + [
            {"ts": when, "from": old, "to": new, "cash_before": round(cash, 2), "cash_after": round(new_cash, 2)}])
        j.set("budget_problem", None)
        j.event("owner", f"Budget changed from ${old:,.2f} to ${new:,.2f} (STARTING_BALANCE_USDT): cash ${cash:,.2f} -> "
                         f"${new_cash:,.2f}; the result so far ({result:+,.2f} USDT) and every trade are kept")
        return [f"💰 Budget changed from ${old:,.2f} to ${new:,.2f} as set in the settings. The desk's cash is now "
                f"${new_cash:,.2f}; the trades and the result so far ({result:+,.2f} USDT) are kept, and position sizes, "
                f"loss brakes and reports use ${new:,.0f} from now on. (For a completely fresh record instead: "
                f"`desk reset --force` on the server.)"]

    def note_strategy_mode(self) -> list[str]:
        """Remember which strategy the record runs. When the owner switches into the daily trend rule, its own record
        starts afresh: its goals, its re-entry rule after a stop and its first daily decisions count from now."""
        j, mode = self.journal, ("trend" if self.base.trend else "hourly")
        was = j.get("strategy_mode")
        if was is None:   # records from before 1.7.0 never stored it: any non-trend decision means the hourly agents
            older = j.db.execute("SELECT 1 FROM decisions WHERE source IS NULL OR source != 'trend' LIMIT 1").fetchone()
            was = "hourly" if older else mode
        j.set("strategy_mode", mode)
        if was == mode:
            return []
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        j.set("strategy_mode_since", now)
        hist = j.get("strategy_history") or []   # the lineage shows the switch; the hourly card's period ends or resumes
        if mode == "trend":
            for key in ("trend_day", "trend_tries", "trend_start", "trend_positions"):
                j.set(key, None)
            stop = trend.params(self.base)[2]
            hist.append({"ts": now, "event": "promote", "id": "trend", "name": "daily trend rule", "by": "owner settings",
                         "note": "STRATEGY=trend; the hourly strategy card is kept for a switch back"})
            j.set("strategy_history", hist[-100:])
            j.event("strategy", "The owner switched the desk to the daily trend rule (STRATEGY=trend)")
            return [f"🔁 Strategy switched to the daily trend rule. Its first decisions are made at this check, then once a "
                    f"day after 00:00 UTC. Coins you hold that are in SYMBOLS stay, with a {stop:.0%} emergency stop, while "
                    f"they close above their average; coins not in SYMBOLS are sold. Its goals count from today; "
                    f"/trendtest shows the rule on past prices."]
        if state := j.get("strategy_champion"):
            # the hourly card's live record (and its rollback watch) restarts now, not with the trend rule's trades
            j.set("strategy_champion", {**state, "since": now})
            card = state.get("card") or {}
            hist.append({"ts": now, "event": "promote", "id": card.get("id"), "name": card.get("name"), "by": "owner settings",
                         "note": "STRATEGY=hourly again: the hourly card resumes"})
            j.set("strategy_history", hist[-100:])
        items = j.get("strategy_challengers") or []
        for e in items:   # a forward test passed before the trend period is stale: the next weekly lab re-runs it
            if e.get("status") == "ready":
                e["status"] = "lab_passed"
        if items:
            j.set("strategy_challengers", items)
        j.event("strategy", "The owner switched the desk back to the hourly agents (STRATEGY=hourly)")
        return ["🔁 Strategy switched back to the hourly agents. From this check the hourly rules manage every position: "
                "the trailing stop moves up to 2 ATR below the recent high, so positions the trend rule bought will "
                "mostly be sold soon."]

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
                               + f". Now watching {', '.join(new['symbols'])}.",
                               {"added": self.scout_update["added"], "removed": self.scout_update["removed"]})

    # ---- strategy card ----
    def apply_champion(self) -> StrategyCard | None:
        """Run whatever strategy card is champion (set by the lab, the owner's /approve, or a rollback). The daily trend
        strategy runs no card: a card's position limit and ATR exits belong to the hourly strategy."""
        if self.base.trend:
            self.s = self.base
            return None
        state = self.journal.get("strategy_champion")
        if state:
            card = StrategyCard.from_dict(state["card"])
            owner = StrategyCard.from_settings(self.base)
            if state.get("by") == "owner settings" and card.id != owner.id:
                # the champion is simply "the owner's settings" and those changed (desk setup, or an old default
                # corrected at load): follow them. Champions chosen by the lab or by /approve are left alone.
                self._replace_champion(state, owner, "owner settings", f"the owner's settings changed: {owner.describe_diff(card)}")
                card = owner
            elif (problems := card.violations()) and owner.id != card.id:   # never re-promote the same card each check
                self._replace_champion(state, owner, "owner settings", f"the strategy in force could not trade ({problems[0]})")
                card = owner
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

    def _replace_champion(self, state: dict, card: StrategyCard, by: str, why: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.journal.set("strategy_champion", {"card": card.to_dict(), "since": now, "by": by,
                                               "previous": state.get("card")})
        hist = self.journal.get("strategy_history") or []
        hist.append({"ts": now, "event": "promote", "id": card.id, "name": card.name, "by": by, "note": why})
        self.journal.set("strategy_history", hist[-100:])
        self.journal.event("strategy", f"Strategy in force is now {summary_line(card)} because {why}")
        self.champion_note = why

    # ---- the cycle ----
    def run_cycle(self) -> list[dict]:
        if self.base.trend:
            return self._trend_cycle()
        summary: list[dict] = []
        self.apply_champion()
        try:
            self.maybe_scout()
        except Exception as e:  # keep trading the current watchlist
            log.exception("Market scout failed")
            summary.append({"symbol": "SCOUT", "stage": "scout", "error": f"could not refresh the coin list: {e}"})
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
                    # the trailing stop follows finished hourly closes, exactly as the backtest and the laboratory
                    # measured it; the exchange's stop order still fires on any intra-hour drop
                    position, exit_result = self._trail(sym, position, self._trail_price(position, closed),
                                                        technical["atr"])
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
                summary.append({"symbol": sym, "stage": "market data", "error": str(e)})

        if ctx:
            self._remember_benchmark({sym: c["price"] for sym, c in ctx.items() if sym in self.s.symbols}, allow_empty=True)

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

        # 4. decisions. First every coin's reports, its threshold for this check and whether a Claude call could
        #    change anything; then the head trader decides who may consult Claude; then sells right away, buys
        #    collected and ranked
        if self.head:
            # Claude's review of the private notebook stays out of what a free AI reads
            self.decision.playbook = self.head.playbook(private=self.free_ai is None)
        closes = {sym: c["closes"] for sym, c in ctx.items()}
        pending = []
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
                score = self.decision.score(reports)
                base_thr = self.decision.threshold_for(sym)
                news_score = float(reports["news"].get("score", 0) or 0)
                thr, why = self.head.threshold(sym, base_thr, closes, positions, news_score,
                                               news_source=reports["news"].get("source")) if self.head \
                    else (base_thr, "coin threshold")
                reports["head"] = {"threshold": thr, "why": why}
                # Claude costs money: a buy the checklist or the position limit refuses anyway is not worth a call
                slots_full = position is None and len(positions) >= self.s.max_open_positions
                news_veto = position is None and news_score <= NEWS_VETO \
                    and reports["news"].get("source") != "keywords"   # refused before any call is made
                eligible = position is not None or not ((review and not review["pass"]) or slots_full or news_veto)
                pending.append({"symbol": sym, "ctx": c, "reports": reports, "position": position, "score": score,
                                "threshold": thr, "holding": position is not None, "eligible": eligible,
                                "fresh": self.decision.has_fresh_view(sym, score, position is not None)})
            except Exception as e:
                log.exception("Analysis failed for %s", sym)
                summary.append({"symbol": sym, "stage": "analysis", "error": str(e)})
        allowed = self.head.plan_ai(pending, pace_minutes=self.pace["minutes"]) if self.head \
            else {p["symbol"] for p in pending if p["eligible"]}
        buys = []
        for p in pending:
            sym, c, reports, position = p["symbol"], p["ctx"], p["reports"], p["position"]
            try:
                decision = self.decision.run(sym, reports, holding=position is not None, use_ai=sym in allowed,
                                             threshold=p["threshold"])
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
                summary.append({"symbol": sym, "stage": "decision", "error": str(e)})

        # 5-6. buys: strongest signals first, while position slots and cash last
        buys.sort(key=lambda b: (b[3]["confidence"], b[3]["combined_score"]), reverse=True)
        for sym, c, reports, decision in buys:
            try:
                verdict = self.risk.review(decision, sym, c["price"], c["technical"]["atr"], self.portfolio())
                self._note_brake(verdict)
                review = c.get("mentor")
                if verdict["approved"] and review and not review["pass"]:
                    names = ", ".join(review["failed"])
                    verdict = {"approved": False, "reason": f"master traders' checklist: {names} not met"}
                self.journal.log_decision(sym, decision, verdict, reports)
                result = self._result(sym, c, decision, verdict)
                if review:
                    result["mentor"] = MentorAgent.summary(review)
                if verdict["approved"]:
                    result.update(self._buy(sym, decision, verdict))
                summary.append(result)
            except Exception as e:
                log.exception("Buy failed for %s", sym)
                summary.append({"symbol": sym, "stage": "buy", "error": str(e)})

        # 7. when to check again
        self.pace = self._next_pace(news.get("_pace"), ctx)
        self.journal.log_equity(self.equity())
        self.notes.refresh(self.last_price, scorecard=self.journal.get("scorecard_text") or "")
        return summary

    # ---- the daily trend strategy (STRATEGY=trend, trader/trend.py) ----
    def _trend_cycle(self) -> list[dict]:
        """Every check: the holdings check, exchange exits, the take-profit moved up as prices rise, and the desk's own
        safety net for a position without exchange orders. Once a day, at the first check after the daily close
        (00:00 UTC): the rule's sells and buys on the day that just finished."""
        self.s = self.base
        self.trend_report = None
        summary = self.reconcile()
        coins = list(self.s.symbols)
        for sym in dict.fromkeys(coins + list(self.journal.positions())):
            try:
                price = self.market.price(sym)
                self.last_price[sym] = price
                position = self.journal.positions().get(sym)
                if position is None:
                    continue
                position, exit_result = self._sync_protection(sym, position)
                if exit_result:
                    summary.append(exit_result)
                if position is None:
                    continue
                if self._managed(sym, position):   # positions of the hourly strategy are adopted at the daily step first
                    position, exit_result = self._raise_take(sym, position, price)
                    if exit_result:
                        summary.append(exit_result)
                    if position is None:
                        continue
                if not self._is_protected(position):
                    if why := self.risk.trend_exit(position, price):  # desk-side backup: the emergency stop only
                        summary.append(self._sell(sym, position, why))
                        continue
                    if err := self._protect(sym, position):
                        summary.append({"symbol": sym, "protect_error": err})
            except Exception as e:  # one bad coin must not stop the others
                log.exception("Market data failed for %s", sym)
                summary.append({"symbol": sym, "stage": "market data", "error": str(e)})
        self._remember_benchmark({sym: self.last_price[sym] for sym in coins if sym in self.last_price})
        day = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()  # the last finished day
        if self.journal.get("trend_day") != day:
            entries, complete = self._trend_step(coins)
            summary += entries
            tries = self.journal.get("trend_tries") or {}
            if tries.get("day") != day:
                tries = {"day": day, "n": 0, "first": time.time()}
            tries = {**tries, "n": tries["n"] + 1}
            # give up after TREND_TRIES tries and at least an hour (checks come every 5 minutes while a position is
            # unprotected, so tries alone could end in a quarter of an hour)
            if complete or (tries["n"] >= TREND_TRIES and time.time() - tries.get("first", 0) >= 3600):
                if not complete:
                    text = (f"the decisions for {day} stayed incomplete after {TREND_TRIES} tries (see the errors before "
                            "this one); the coins concerned wait until tomorrow")
                    self.journal.event("error", f"Daily trend decisions: {text}")
                    summary.append({"symbol": "TREND", "stage": "trend", "error": text})
                self.journal.set("trend_day", day)
                if self.trend_report is not None:
                    self.trend_report["final"] = True
            self.journal.set("trend_tries", tries)
            if self.trend_report is not None:
                self.trend_report.update(day=day, complete=complete, tries=tries["n"])
        self.pace = self._next_pace(None, {})
        self.journal.log_equity(self.equity())
        self.notes.refresh(self.last_price, scorecard=self.journal.get("scorecard_text") or "")
        return summary

    def _demo_price_problem(self, sym: str, price: float) -> str | None:
        """On the Binance demo exchange a coin may be missing or priced far from the real market. The desk values its
        coins at real prices, so a demo fill at half the real price would show twice the money: such buys are refused
        (the market scout filters the same way)."""
        if self.testnet is None:
            return None
        try:
            demo = float(self.testnet.price(sym))
        except Exception as e:  # noqa: BLE001 - not listed there, or no answer: no buy this time
            return f"the Binance demo exchange gives no price for it ({e})"
        if not demo or abs(demo / price - 1) > DEMO_GAP:
            return (f"the Binance demo exchange prices it at {demo:,.6g}, {demo / price - 1:+.1%} from the real market "
                    f"({price:,.6g}); paper trading has no such limit")
        return None

    def _raise_take(self, sym: str, position: dict, price: float) -> tuple[dict | None, dict | None]:
        """The rule has no profit target, but Binance's protective pair needs one and refuses a sell more than twice
        the price: once the price is within 30% of the take-profit, move it up to 1.9x the price (stop unchanged)."""
        if price < position["take_profit"] / trend.RAISE_AT:
            return position, None
        take = price * trend.TAKE_MULT
        old_stop, old_take = position["stop_loss"], position["take_profit"]
        log.info("%s: take-profit moved up to %.6g (price %.6g)", sym, take, price)
        moved, exit_result = self._move_exits(sym, position, old_stop, take)
        if moved is None or exit_result is not None:
            return moved, exit_result
        if self.execution.can_protect and not self._is_protected(moved):
            # Binance refused the new pair: put the old one back rather than leave the coin without a stop
            error = self.protect_errors.get(sym, "refused")
            self.journal.update_exits(sym, old_stop, old_take)
            self._protect(sym, self.journal.positions()[sym])
            self.journal.event("protection", f"{sym}: the take-profit order could not be raised ({error}); the previous "
                                             "pair was put back")
            return self.journal.positions().get(sym), None
        placed = moved["take_profit"]   # what Binance accepted (it may cap a take-profit below 1.9x)
        if placed <= old_take * 1.0001:
            return moved, None
        self.journal.event("protection", f"{sym}: take-profit order raised to {placed:,.6g} (price {price:,.6g}, "
                                         f"{price / position['entry_price'] - 1:+.0%} since the buy) so the trend is not "
                                         f"cut short; the emergency stop stays at {old_stop:,.6g}")
        return moved, {"symbol": sym, "action": "RAISE", "price": price, "entry_price": position["entry_price"],
                       "take_profit": placed, "stop_loss": old_stop}

    def _managed(self, sym: str, position: dict) -> bool:
        """Is this position under the daily trend rule (bought by it, or adopted from the hourly strategy)?"""
        return (self.journal.get("trend_positions") or {}).get(sym) == position.get("opened_at")

    def _trend_step(self, coins: list[str]) -> tuple[list[dict], bool]:
        """The rule's decisions on the day that just finished: sells first, then buys (strongest 60-day gain first).
        Returns (summary entries, complete). Not complete when a coin's daily prices or a sale failed: the next check
        tries again (the step can run twice safely: it only sells what it holds and buys what it does not)."""
        sma_days, vol_target, stop_pct = trend.params(self.s)
        now = dt.datetime.now(dt.timezone.utc)
        if not (start := self.journal.get("trend_start")):
            # the trend rule's own record starts here: its goals, return and benchmark count from this moment
            prices = {sym: self.last_price[sym] for sym in coins if sym in self.last_price}
            if "BTCUSDT" not in prices:   # the reports compare with Bitcoin too
                try:
                    prices["BTCUSDT"] = self.market.price("BTCUSDT")
                except Exception as e:  # noqa: BLE001 - a benchmark, never a reason to stop
                    log.warning("Could not record the Bitcoin benchmark price: %s", e)
            start = {"ts": now.isoformat(timespec="seconds"), "equity": round(self.equity(), 2), "prices": prices}
            self.journal.set("trend_start", start)
            self.journal.event("strategy", f"The desk now trades the daily trend rule on {', '.join(c.removesuffix('USDT') for c in coins)}: "
                                           f"hold a coin while its daily close is above its {sma_days}-day average, emergency "
                                           f"stop {stop_pct:.0%} below the entry, decisions once a day after 00:00 UTC.")
        since_ms = int(dt.datetime.fromisoformat(start["ts"]).timestamp() * 1000)
        now_ms = int(now.timestamp() * 1000)
        out, complete, sigs, days = [], True, {}, {}
        for sym in coins:
            try:
                days[sym] = trend.finished(self.market.daily_candles(sym, trend.history_days(sma_days) + 1), now_ms)
                sigs[sym] = trend.signal([c["close"] for c in days[sym]], sma_days)
            except Exception as e:  # noqa: BLE001 - this coin waits; the step runs again at the next check
                log.warning("Daily prices unavailable for %s: %s", sym, e)
                complete = False
                out.append({"symbol": sym, "stage": "trend", "error": f"daily prices unavailable: {e}"})

        day = (now.date() - dt.timedelta(days=1)).isoformat()
        report = {"coins": {}, "adopted": [], "slots": self.s.max_open_positions}
        self.trend_report = report

        def decide(sym: str, action: str, why: str, verdict: dict) -> dict:
            g = sigs.get(sym)
            decision = {"action": action, "confidence": 1.0, "source": "trend", "rules_action": action,
                        "combined_score": round(g["close"] / g["sma"] - 1, 4) if g else 0.0,
                        "reasoning": f"{why} · {trend.describe(sym, g, sma_days)}" if sym in sigs else why}
            key = (day, sym, action, verdict.get("reason"))
            if key not in self._trend_logged:   # a retry of the day's step records only what is new
                self._trend_logged = {k for k in self._trend_logged if k[0] == day} | {key}
                self.journal.log_decision(sym, decision, verdict, {"trend": g})
            report["coins"][sym] = {"action": action, "why": why, "approved": verdict.get("approved"),
                                    "risk": verdict.get("reason"),
                                    "gap": round(g["close"] / g["sma"] - 1, 4) if g else None}
            return decision

        # 1. sells: a coin that closed at or below its average, and coins the owner no longer lists
        for sym, p in self.journal.positions().items():
            g = sigs.get(sym)
            if sym not in coins:
                why = "not one of the coins in SYMBOLS any more"
            elif g is not None and not g["on"]:
                why = f"closed at or below its {sma_days}-day average"
            else:
                continue
            if sym not in self.last_price:   # never sell blind: tried again at the next check
                complete = False
                out.append({"symbol": sym, "stage": "trend", "error": f"not sold ({why}): no live price this check"})
                continue
            try:
                decision = decide(sym, "SELL", why, {"approved": True, "reason": "closing position"})
                result = self._result(sym, {"price": self.last_price[sym]}, decision,
                                      {"approved": True, "reason": "closing position"})
                result.update(self._sell(sym, p, f"trend: {why}"))
            except Exception as e:  # one coin must not stop the day's other decisions
                log.exception("Trend sale failed for %s", sym)
                complete = False
                out.append({"symbol": sym, "stage": "trend", "error": f"sale failed: {e}"})
                continue
            if not (result.get("order") or {}).get("ok", True) or sym in self.journal.positions():
                complete = False   # failed, or sold only in part on a thin order book: tried again
            out.append(result)

        # 2. positions the hourly strategy opened on a coin the rule holds: the rule's emergency stop, no profit cap
        managed = self.journal.get("trend_positions") or {}
        positions = self.journal.positions()
        managed = {s: o for s, o in managed.items() if s in positions and positions[s]["opened_at"] == o}
        for sym, p in positions.items():
            if managed.get(sym) == p["opened_at"] or sym not in coins:   # an unsold leftover is retried, not adopted
                continue
            stop = p["entry_price"] * (1 - stop_pct)
            take = max(p["entry_price"], self.last_price.get(sym, 0.0)) * trend.TAKE_MULT
            moved, exit_result = self._move_exits(sym, p, stop, take)
            if exit_result:
                out.append(exit_result)
            moved = self.journal.positions().get(sym)
            if moved is None:
                continue
            converted = abs(moved["stop_loss"] - stop) <= 1e-9 * stop and moved["take_profit"] > moved["entry_price"]
            if not converted or (self.execution.can_protect and not self._is_protected(moved)
                                 and moved["stop_loss"] < self.last_price.get(sym, 0.0)):
                # the old orders could not be cancelled, or the new pair was refused: tried again at the next check
                if converted:
                    self.journal.update_exits(sym, p["stop_loss"], p["take_profit"])
                    self._protect(sym, self.journal.positions()[sym])
                complete = False
                out.append({"symbol": sym, "stage": "trend", "error": "could not move its exits to the trend rule's "
                                                                       f"yet: {self.protect_errors.get(sym, 'exchange busy')}"})
                continue
            managed[sym] = moved["opened_at"]
            self.journal.event("strategy", f"{sym}: the position opened by the hourly strategy now follows the trend "
                                           f"rule (emergency stop {stop:,.6g}, held while the coin stays above its "
                                           f"{sma_days}-day average)")
            out.append({"symbol": sym, "action": "ADOPT", "old_stop": p["stop_loss"], "stop_loss": stop,
                        "take_profit": moved["take_profit"], "entry_price": p["entry_price"], "sma_days": sma_days})
            report["adopted"].append(sym)

        # 3. buys: coins above their average that the desk does not hold, strongest 60-day gain first
        held = self.journal.positions()
        wanted = []
        for sym in coins:
            if sym not in sigs:
                continue
            g = sigs[sym]
            if sym in held:
                decide(sym, "HOLD", "holds: still above its average", {"approved": False, "reason": "holding"})
                continue
            if g is None or not g["on"]:
                decide(sym, "HOLD", "stays out" if g else "waits for more daily history",
                       {"approved": False, "reason": "no buy signal"})
                continue
            stop_ms = self.journal.last_stop_loss_ms(sym)
            if stop_ms is not None and stop_ms >= since_ms and not trend.armed(days[sym], sma_days, stop_ms):
                decide(sym, "HOLD", "waits: its emergency stop fired; it is bought again once it has closed at or "
                                    "below its average and risen above it again", {"approved": False, "reason": "re-entry rule"})
                continue
            wanted.append(sym)
        for sym in sorted(wanted, key=lambda x: -sigs[x]["momentum"]):
            try:
                price = self.last_price.get(sym)
                if price is None:
                    raise RuntimeError("no live price this check")
                if why := self._demo_price_problem(sym, price):
                    decide(sym, "HOLD", f"not bought: {why}", {"approved": False, "reason": "demo exchange price"})
                    continue
                share = trend.share(sigs[sym], len(coins), vol_target, self.s.max_position_pct)
                verdict = self.risk.review_trend(sym, price, share, self.portfolio())
                self._note_brake(verdict)
                decision = decide(sym, "BUY", f"buys {share:.1%} of the account", verdict)
                result = self._result(sym, {"price": price}, decision, verdict)
                if verdict["approved"]:
                    result.update(self._buy(sym, decision, verdict))
                    if sym in self.journal.positions():
                        managed[sym] = self.journal.positions()[sym]["opened_at"]
                out.append(result)
            except Exception as e:
                log.exception("Trend buy failed for %s", sym)
                out.append({"symbol": sym, "stage": "buy", "error": str(e)})
        self.journal.set("trend_positions", managed)
        return out, complete

    @staticmethod
    def _result(sym: str, c: dict, decision: dict, verdict: dict) -> dict:
        return {"symbol": sym, "price": c["price"], "action": decision["action"], "confidence": decision["confidence"],
                "score": decision["combined_score"], "approved": verdict["approved"], "risk": verdict["reason"],
                "reasoning": decision["reasoning"], "source": decision.get("source")}

    def _buy(self, sym: str, decision: dict, verdict: dict) -> dict:
        """Place an approved buy, record it and put its stop-loss / take-profit on the exchange at once.
        Returns the fields for this check's summary entry."""
        out: dict = {}
        trend_buy = decision.get("source") == "trend"
        real = self._real_price(sym, self.last_price[sym])   # the real market just before the order
        fill = self.execution.execute("BUY", sym, quote_amount=verdict["quote_amount"])
        fill["market_price"] = real
        self.journal.log_order(sym, "BUY", fill, "trend" if trend_buy else "signal")
        if not fill["ok"]:
            self.journal.event("order", f"BUY {sym} failed: {fill.get('error')}")
        if fill["ok"]:
            self.journal.open_position(sym, fill, verdict["stop_loss"], verdict["take_profit"])
            exits = (f"emergency stop {verdict['stop_loss']:,.6g} · take-profit order {verdict['take_profit']:,.6g} (moved up "
                     f"with the price) · daily trend rule") if trend_buy else \
                (f"stop {verdict['stop_loss']:,.6g} · target {verdict['take_profit']:,.6g} · confidence "
                 f"{decision['confidence']:.0%} ({decision.get('source') or 'rules'})")
            self.journal.event("trade", f"BUY {sym}: {fill['qty']:.6g} at {fill['avg_price']:,.6g} = "
                                        f"{fill['quote']:,.2f} USDT · {exits}: {str(decision['reasoning'])[:240]}")
            position = self.journal.positions()[sym]
            if err := self._protect(sym, position):
                out["protect_error"] = err
                check = self.risk.trend_exit if trend_buy else self.risk.exit_signal
                if why := check(position, self.last_price[sym]):
                    out.update(self._sell(sym, position, why))  # already past an exit level
            else:
                out["protected"] = self.journal.positions()[sym]["protected"]
        out.update(order=fill, stop_loss=verdict["stop_loss"], take_profit=verdict["take_profit"])
        return out

    def _note_brake(self, verdict: dict) -> None:
        if verdict.get("trip"):
            self.journal.set("brake_tripped_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
            self.journal.event("brake", f"Overall loss brake tripped: {verdict['reason']}. No new buys for "
                                        f"{self.s.brake_pause_days} days, then half size until half the loss is back.")

    def _remember_benchmark(self, prices: dict[str, float], allow_empty: bool = False) -> None:
        """First check: remember where the market was, so the reports can compare with just holding."""
        if self.journal.get("benchmark_start") or not (prices or allow_empty):
            return
        prices = dict(prices)
        if "BTCUSDT" not in prices and self.market.client is not None:
            try:
                prices["BTCUSDT"] = self.market.client.price("BTCUSDT")
            except Exception as e:
                log.warning("Could not record the Bitcoin benchmark price: %s", e)
        self.journal.set("benchmark_start", {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                             "prices": prices})

    @staticmethod
    def _snapshot(c: dict, holding: bool) -> dict:
        closes = c["closes"]
        return {"change_1h_pct": (closes[-1] / closes[-2] - 1) * 100,
                "change_24h_pct": (closes[-1] / closes[-25] - 1) * 100 if len(closes) > 25 else 0.0,
                "volatility_pct": c["technical"]["atr"] / c["price"] * 100, "holding": holding}

    def _next_pace(self, ai: dict | None, ctx: dict) -> dict:
        s = self.s
        if ai and ai.get("minutes"):
            minutes = max(s.min_check_minutes, min(s.max_check_minutes, int(ai["minutes"])))
            if str(ai.get("by", "")).startswith("ai:"):
                # a free model may slow the checks down but not speed them up: faster checks spend its daily
                # allowance, and its reading of the news is not yet measured
                minutes = max(minutes, s.cycle_minutes)
            pace = {"minutes": minutes, "reason": ai.get("reason", ""),
                    "source": "AI" if not ai.get("by") or ai["by"] == "claude" else f"AI ({ai['by'][3:]})"}
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
                out += self._book_unrecorded_sells(sym, p)   # a protective order that filled in parts, unseen
                p = self.journal.positions().get(sym)
                if p is None or have >= p["qty"] * 0.99:
                    continue
                out.append({"symbol": sym, "stage": "exchange", "error":
                            f"the journal says we hold {p['qty']:.6g} but the exchange only has {have:.6g}, and the "
                            "account's recent sales do not explain the difference. "
                            + ("Binance probably reset the demo account. Run `desk reset --force` to start the demo "
                               "fresh." if have <= 0 else "The desk keeps checking; if this stays for a day, send /health.")})
        return out

    def _booked_sells(self, sym: str, order_id: str) -> tuple[float, float]:
        row = self.journal.db.execute("SELECT COALESCE(SUM(qty), 0), COALESCE(SUM(quote), 0) FROM orders WHERE symbol=? "
                                      "AND side='SELL' AND ok=1 AND order_id=?", (sym, str(order_id))).fetchone()
        return float(row[0]), float(row[1])

    def _book_unrecorded_sells(self, sym: str, position: dict) -> list[dict]:
        """Record the coins the exchange sold for us that the journal never saw: the filled part of a protective
        order that was then cancelled or replaced, or that is still part-filled. Without this the journal keeps
        coins that no longer exist (a position that can never be sold, blocking a slot) and misses their money."""
        broker = self.execution.broker
        if not hasattr(broker, "sells_since") or not position:
            return []
        try:
            opened = int(dt.datetime.fromisoformat(position["opened_at"]).timestamp() * 1000)
            sells = broker.sells_since(sym, opened)
        except Exception as e:  # noqa: BLE001 - try again at the next check
            log.warning("Could not read the account's sales of %s: %s", sym, e)
            return []
        out, step = [], self.execution.step_size(sym)
        # an order placed before the buy that opened this position is not this position's (Binance numbers a pair's
        # orders in sequence): e.g. a protective order an earlier record left live after `desk reset --force`
        row = self.journal.db.execute("SELECT order_id FROM orders WHERE symbol=? AND side='BUY' AND ok=1 "
                                      "ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
        first = int(row[0]) if row and str(row[0]).isdigit() else None
        # the position's own protective orders give the sale its real reason and the price it was set at
        prot = position.get("protection") or {}
        legs = {str(prot.get("sl_order_id")): ("stop-loss", prot.get("stop")),
                str(prot.get("tp_order_id")): ("take-profit", prot.get("take"))}
        for order_id, s in sells.items():
            p = self.journal.positions().get(sym)
            if p is None:
                break
            if first is not None and str(order_id).isdigit() and int(order_id) < first:
                continue
            booked_qty, booked_quote = self._booked_sells(sym, order_id)
            qty = min(s["qty"] - booked_qty, p["qty"])
            if qty < max((step or 0) / 2, p["qty"] * 1e-6):   # nothing left of it (exchange amounts are whole steps)
                continue
            share = qty / s["qty"] if s["qty"] else 1.0
            quote = max(0.0, s["quote"] - booked_quote) * qty / (s["qty"] - booked_qty) if booked_qty \
                else s["quote"] * share
            leg = legs.get(str(order_id))
            fill = {"ok": True, "status": "FILLED", "order_id": str(order_id), "qty": qty,
                    "avg_price": s.get("gross", s["quote"]) / s["qty"], "quote": quote,
                    "fee_quote": s["fee_quote"] * share, "market_price": leg[1] if leg else None}
            reason = f"{leg[0]} (exchange)" if leg else \
                "sold on the exchange (part of a protective order, found in the account's sales)"
            self.journal.log_order(sym, "SELL", fill, reason)
            trade = self.journal.close_position(sym, fill, reason, step=step)
            self._exit_event(sym, fill, trade)
            out.append({"symbol": sym, "action": "SELL", "reason": reason, "order": fill, "trade": trade})
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

    def _trail_price(self, position: dict, closed: list[dict]) -> float:
        """The highest finished-candle close since the position was opened (the entry price before the first one)."""
        from .backtest import INTERVAL_MS
        step = INTERVAL_MS.get(self.market.interval, 3_600_000)
        try:
            opened = dt.datetime.fromisoformat(position["opened_at"]).timestamp() * 1000
        except (KeyError, TypeError, ValueError):
            opened = 0
        closes = [c["close"] for c in closed if c["time"] + step > opened]
        return max(closes or [position["entry_price"]])

    def _trail(self, sym: str, position: dict, price: float, atr: float) -> tuple[dict | None, dict | None]:
        """Raise the stop-loss as the price makes new highs; re-place the exchange orders when it moves.
        Returns (position or None if it turned out to be closed, exit summary entry or None)."""
        high = max(position.get("high_water") or position["entry_price"], price)
        new_stop = self.risk.trailing_stop(position, high, atr)
        if new_stop is None:
            if high != position.get("high_water"):
                self.journal.set_high_water(sym, high)
            return self.journal.positions()[sym], None
        log.info("%s: trailing stop raised to %.6g (high %.6g)", sym, new_stop, high)
        return self._move_exits(sym, position, new_stop, position["take_profit"], high)

    def _move_exits(self, sym: str, position: dict, stop: float, take: float,
                    high: float | None = None) -> tuple[dict | None, dict | None]:
        """Move a position's stop-loss / take-profit: cancel the exchange pair, record what it sold meanwhile, place
        a new pair at the new prices. Returns (position or None if it turned out to be closed, exit entry or None)."""
        if position.get("protection"):
            status = self.execution.unprotect(sym, position["protection"])
            if status["state"] == "exited":  # it filled while we were about to move it
                exit_result = self._record_exchange_exit(sym, status)
                return self.journal.positions().get(sym), exit_result
            if status["state"] == "error":
                return position, None  # keep the old orders; try again next cycle
            self.journal.set_protection(sym, None, "none")
            booked = self._book_unrecorded_sells(sym, position)
            if sym not in self.journal.positions():
                return None, (booked[0] if booked else None)
        self.journal.update_exits(sym, stop, take)
        if high is not None:
            self.journal.set_high_water(sym, high)
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
        strict = self.base.trend   # the trend rule's far stop and take-profit: only what Binance accepts that far out
        res = self.execution.protect(sym, position["qty"], position["stop_loss"], position["take_profit"], strict=strict)
        if res["ok"]:
            self.journal.set_protection(sym, res["protection"], res["protection"]["kind"])
            self.protect_errors.pop(sym, None)
            placed = res["protection"].get("take")
            if strict and placed and abs(placed - position["take_profit"]) > 1e-9 * placed:
                self.journal.update_exits(sym, position["stop_loss"], placed)   # Binance's cap: the order that rests
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
        if state == "executing":
            # one leg filled in part and the other was cancelled by the exchange: the coins left have no stop-loss on
            # the exchange any more. Cancel what is left, record the part that sold, and let this check protect the
            # rest again with a fresh pair (or sell it at once if the price is already below the stop)
            res = self.execution.unprotect(sym, prot)
            if res["state"] == "exited":             # the rest filled meanwhile
                return self.journal.positions().get(sym), self._record_exchange_exit(sym, res)
            if res["state"] == "error":              # the rest still works and locks the coins: keep it, retry
                return position, None
            booked = self._book_unrecorded_sells(sym, position)
            if sym in self.journal.positions():
                self.journal.set_protection(sym, None, "none")
            return self.journal.positions().get(sym), (booked[0] if booked else None)
        elif state == "gone":  # cancelled or wiped (Binance resets the testnet now and then): re-protect
            log.warning("Protection for %s disappeared (%s) - placing it again", sym, status.get("detail"))
            if hasattr(self.execution.broker, "cancel_quietly"):
                self.execution.broker.cancel_quietly(sym, prot)  # make sure nothing stays locked
            # a stop that filled in part and then expired on the thin demo book: record that part now, while its
            # order is still known as the stop-loss (the re-entry and cooldown rules look for stop-losses)
            booked = self._book_unrecorded_sells(sym, position)
            if sym not in self.journal.positions():
                return None, (booked[0] if booked else None)
            self.journal.set_protection(sym, None, "none")
            if booked:
                return self.journal.positions()[sym], booked[0]
        # "unknown" (network problem): keep the record, try again next cycle
        return self.journal.positions()[sym], None

    def _real_price(self, sym: str, fallback: float) -> float:
        """The real market price now (Binance's public data), to judge a demo fill against; the last known price when
        it cannot be read."""
        client = self.market.client
        if client is None:
            return fallback
        try:
            return float(client.price(sym))
        except Exception:  # noqa: BLE001 - a measurement, never a reason to skip an order
            return fallback

    def _record_exchange_exit(self, sym: str, status: dict) -> dict | None:
        # the exchange filled a stop-loss or a take-profit at some moment since the last check: judge the fill against
        # the price the order was set at, not against the price now (that would count the market's later moves)
        prot = status.get("protection") or {}
        intended = prot.get("stop") if status.get("reason") == "stop-loss" else prot.get("take")
        fill = {"ok": True, **status["fill"], "market_price": intended or self.last_price.get(sym)}
        booked_qty, booked_quote = self._booked_sells(sym, fill.get("order_id", ""))
        if booked_qty > 0:   # part of this order was already recorded when it was found part-filled
            whole = fill["qty"]
            fill["qty"] = max(0.0, whole - booked_qty)
            fill["fee_quote"] = fill.get("fee_quote", 0.0) * (fill["qty"] / whole if whole else 0.0)
            fill["quote"] = max(0.0, fill["quote"] - booked_quote)
            if fill["qty"] < (self.execution.step_size(sym) or 0) / 2:   # all of it was already recorded
                if sym in self.journal.positions():
                    self.journal.set_protection(sym, None, "none")
                return None
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
                return self._record_exchange_exit(sym, status) or {"symbol": sym, "action": "SELL", "reason": reason}
            if status["state"] == "error":
                fill = {"ok": False, "error": status["detail"], "market_price": self.last_price[sym]}
                self.journal.log_order(sym, "SELL", fill, reason)
                self.journal.event("order", f"SELL {sym} failed ({reason}): could not cancel the protective orders: "
                                            f"{status['detail']}")
                return {"symbol": sym, "action": "SELL", "reason": reason, "order": fill}
            self.journal.set_protection(sym, None, "none")
            booked = self._book_unrecorded_sells(sym, position)
            if sym not in self.journal.positions():
                return booked[0] if booked else {"symbol": sym, "action": "SELL", "reason": reason}
            position = self.journal.positions()[sym]
        real = self._real_price(sym, self.last_price[sym])
        fill = self.execution.execute("SELL", sym, qty=position["qty"])
        fill["market_price"] = real
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
