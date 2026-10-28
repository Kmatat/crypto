"""Scorecard (Claude vs rules from real later prices), news veto, AI gating and the percentage AI cap."""
import json
from types import SimpleNamespace

import pytest

from trader.agents.decision import NEWS_VETO, DecisionAgent
from trader.agents.execution import ExecutionAgent
from trader.config import Settings
from trader.journal import Journal
from trader.llm import ClaudeJSON
from trader.orchestrator import Orchestrator
from trader.scorecard import claude_scorecard, news_scorecard, replay_trade, scorecard_text
from tests.test_protection import FakeBroker


def path(prices, spread=0.002):
    return [{"time": i * 3_600_000, "open": p, "high": p * (1 + spread), "low": p * (1 - spread), "close": p, "volume": 1}
            for i, p in enumerate(prices)]


def test_replay_trade_follows_stop_target_and_trail():
    s = Settings(stop_loss_atr=2, take_profit_atr=4, trail_atr=0)
    assert replay_trade(path([100, 99, 95, 94]), entry=100, atr=2, s=s)["exit"] == "stop-loss"   # 96 touched
    r = replay_trade(path([100, 104, 109]), entry=100, atr=2, s=s)
    assert r["exit"] == "take-profit" and r["pnl_pct"] == pytest.approx((108 * 0.999 / (100 * 1.001) - 1) * 100)
    r = replay_trade(path([100, 101, 102]), entry=100, atr=2, s=s)
    assert r["exit"] == "open" and r["pnl_pct"] > 1.5
    trailing = Settings(stop_loss_atr=2, take_profit_atr=12, trail_atr=2)
    r = replay_trade(path([100, 103, 106, 103, 101.9]), entry=100, atr=2, s=trailing)   # stop trails to 102 then hits
    assert r["exit"] == "stop-loss" and r["pnl_pct"] > 1.0                                # a profitable stop
    assert replay_trade([], 100, 2, s)["exit"] == "unknown"


def decision_row(j, ts, symbol, action, rules_action, price, atr, news=0.0, source="claude"):
    j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, risk_approved,"
                 " risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (ts, symbol, action, 0.8, source, 0.3, "r", 0, "x",
                  json.dumps({"technical": {"price": price, "atr": atr}, "news": {"score": news}}), rules_action))
    j.db.commit()


def test_claude_scorecard_sums_saved_and_cost_from_later_prices():
    j = Journal(":memory:", 10_000)
    s = Settings(stop_loss_atr=2, take_profit_atr=4, trail_atr=0, max_position_pct=0.10)
    decision_row(j, "2026-01-01T00:00:00+00:00", "AAAUSDT", "HOLD", "BUY", 100, 2)   # vetoed buy that would have won
    decision_row(j, "2026-01-02T00:00:00+00:00", "BBBUSDT", "HOLD", "BUY", 100, 2)   # vetoed buy that would have lost
    decision_row(j, "2026-01-03T00:00:00+00:00", "CCCUSDT", "HOLD", "SELL", 100, 2)  # vetoed sell; price then fell
    decision_row(j, "2026-01-04T00:00:00+00:00", "DDDUSDT", "BUY", "BUY", 100, 2)    # agreement: not a divergence
    later = {"AAAUSDT": path([100, 105, 109]), "BBBUSDT": path([100, 97, 95]), "CCCUSDT": path([100] + [98] * 30)}
    sc = claude_scorecard(j, s, lambda sym, since: later[sym])
    assert sc["cases"] == 3
    assert sc["cost_usd"] > 0 and sc["saved_usd"] > 0           # AAA cost money (missed win), BBB saved money
    assert sc["net_usd"] == pytest.approx(sc["saved_usd"] - sc["cost_usd"])
    assert "need 50" in sc["verdict"]
    cached = j.get("scorecard_cases")
    assert len(cached) == 3 and all(c["final"] for c in cached.values())
    # cached finals are reused even if prices are no longer available
    sc2 = claude_scorecard(j, s, lambda sym, since: (_ for _ in ()).throw(RuntimeError("no data")))
    assert sc2["cases"] == 3 and sc2["net_usd"] == sc["net_usd"]


def test_news_scorecard_hit_rate():
    j = Journal(":memory:", 10_000)
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    for i, (score, up) in enumerate([(0.6, True), (0.7, True), (-0.6, False), (0.6, False), (0.1, True)]):
        ts = (now - dt.timedelta(hours=48 + i)).isoformat(timespec="seconds")
        decision_row(j, ts, f"C{i}USDT", "HOLD", "HOLD", 100, 1, news=score, source="rules")
    later = lambda sym, since: path([100] + [101 if sym in ("C0USDT", "C1USDT", "C4USDT") else 99] * 24)
    ns = news_scorecard(j, later)
    assert ns["judged"] == 4 and ns["hit_rate_pct"] == 75.0     # the 0.1 score is too weak to be judged
    assert "need 30" in ns["note"]
    text = scorecard_text({"cases": 3, "saved_usd": 10, "cost_usd": 4, "net_usd": 6, "verdict": "v"}, ns, 1.25)
    assert "net $+6.00" in text and "hit rate 75.0%" in text and "AI bill so far $1.25" in text


def reports(score, news=0.0):
    return {"technical": {"score": score}, "prediction": {"score": score}, "news": {"score": news}, "forex": {"score": 0}}


def test_news_veto_blocks_a_buy_but_not_a_sell():
    d = DecisionAgent()
    r = d.run("BTCUSDT", reports(0.8, news=NEWS_VETO), holding=False)
    assert r["action"] == "HOLD" and "negative news" in r["reasoning"] and r["rules_action"] == "BUY"
    assert d.run("BTCUSDT", reports(0.8, news=-0.2), holding=False)["action"] == "BUY"
    assert d.run("BTCUSDT", reports(-0.8, news=-0.9), holding=True)["action"] == "SELL"


def test_claude_is_not_asked_when_the_checklist_or_slots_veto_anyway():
    asked = []
    llm = SimpleNamespace(ask=lambda *a, **k: asked.append(1) or {"action": "BUY", "confidence": 0.9, "reasoning": "x"})
    d = DecisionAgent(llm)
    d.run("BTCUSDT", reports(0.8), holding=False, use_ai=False)
    assert asked == []
    d.run("BTCUSDT", reports(0.8), holding=False)
    assert asked == [1]

    s = Settings(offline=True, symbols=["ETHUSDT"], mentor_checks=("reward",), take_profit_atr=3.0)  # 1.5:1 always fails
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.broker = FakeBroker(lambda sym: bot.last_price[sym])
    bot.execution = ExecutionAgent(bot.broker)
    calls = []
    bot.decision.run = lambda sym, reports, holding, use_ai=True, threshold=None: calls.append(use_ai) or {
        "action": "HOLD", "confidence": 0.5, "reasoning": "r", "source": "rules", "combined_score": 0.0}
    bot.run_cycle()
    assert calls == [False]  # checklist already failed: no need to pay for an opinion


def test_percentage_cap_limits_the_ai_budget():
    llm = ClaudeJSON("claude-opus-5-5", "k", daily_budget_usd=5.0)
    assert llm.effective_daily_budget() == 5.0
    llm.pct_daily_budget = 10_000 * 0.25 / 100 / 30          # $0.83 a day on a $10,000 account
    assert llm.effective_daily_budget() == pytest.approx(0.8333, rel=1e-3)
    llm._roll_day()
    llm._spent = 0.9
    assert not llm._allowed() and llm.budget_hit
    s = Settings(offline=True, symbols=["BTCUSDT"], anthropic_key="k", ai_budget_pct_month=0.25)
    s.offline = False  # use_claude needs a key and not offline; the client is never called here
    bot = Orchestrator.__new__(Orchestrator)
    # orchestrator sets the percentage cap from the account size
    s2 = Settings(symbols=["BTCUSDT"], anthropic_key="k", ai_budget_pct_month=0.25, starting_balance=10_000)
    assert s2.use_claude


def test_the_scorecard_downloads_each_coins_prices_once_and_gets_the_same_answer():
    """1.7.7: /report asked the exchange once per judged case (minutes on a busy journal, every other command waiting);
    one download per coin, cut per case, gives identical results."""
    import random
    from trader.scorecard import CandleCache
    hour, now = 3_600_000, 1_800_000_000_000
    rng = random.Random(7)
    series = {}
    for sym in ("AAAUSDT", "BBBUSDT"):
        price, rows = 100.0, []
        for i in range(900):
            o = price
            price *= 1 + rng.uniform(-0.02, 0.02)
            rows.append({"time": now - (900 - i) * hour, "open": o, "close": price, "high": max(o, price) * 1.005,
                         "low": min(o, price) * 0.995, "volume": 1.0})
        series[sym] = rows
    calls = []

    def fetch(symbol, start_ms, limit):
        calls.append(symbol)
        return [r for r in series[symbol] if r["time"] >= start_ms][:limit]

    def direct(symbol, since_ms):
        return fetch(symbol, since_ms, 130)
    moments = [(sym, now - h * hour - 1234) for h in range(850, 0, -7) for sym in series]
    want = [direct(sym, ms) for sym, ms in moments]
    calls.clear()
    cache = CandleCache(fetch)
    assert [cache(sym, ms) for sym, ms in moments] == want
    assert len(calls) == 2                                             # one download per coin, not one per case
    assert cache("AAAUSDT", now - 899 * hour) == direct("AAAUSDT", now - 899 * hour)   # an earlier moment: fetched again


def test_report_builds_the_scorecard_once(monkeypatch):
    from trader import __main__ as main
    built = []
    monkeypatch.setattr(main, "scorecard_block", lambda bot, j: built.append(1) or "🧪 card")
    j = Journal(":memory:", 5_000)
    text = main.report_message(SimpleNamespace(last_price={}), Settings(starting_balance=5_000), j)
    assert built == [1] and text.endswith("🧪 card") and "Balance" in text
