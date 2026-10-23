from types import SimpleNamespace

import pytest

from trader.agents.decision import DecisionAgent
from trader.agents.execution import ExecutionAgent, PaperBroker, TestnetBroker
from trader.agents.news import NewsAgent
from trader.agents.prediction import PredictionAgent
from trader.agents.risk import RiskManagerAgent
from trader.agents.technical import TechnicalAnalysisAgent, rsi
from trader.binance_client import BinanceClient, BinanceError, format_qty, round_step
from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator
from trader.report import performance


def candles_from(closes):
    return [{"time": i, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1} for i, c in enumerate(closes)]


def test_rsi_extremes():
    assert rsi([float(i) for i in range(1, 60)]) == 100.0
    assert rsi([float(i) for i in range(60, 1, -1)]) < 5


def test_technical_and_prediction_direction():
    up = candles_from([100 * 1.01 ** i for i in range(100)])
    down = candles_from([100 * 0.99 ** i for i in range(100)])
    assert PredictionAgent().run(up)["prob_up"] > 0.5 > PredictionAgent().run(down)["prob_up"]
    assert TechnicalAnalysisAgent().run(up)["atr"] > 0


def test_step_rounding_never_rounds_up():
    assert round_step(0.123456789, "0.00001000") == pytest.approx(0.12345)
    assert format_qty(1.999999, "0.01000000") == "1.99"


def test_signature_matches_binance_docs_example():
    c = BinanceClient("x", "k", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1,
              "price": 0.1, "recvWindow": 5000, "timestamp": 1499827319559}
    assert c.sign(params).endswith("c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")


def test_news_keyword_sentiment():
    heads = ["Bitcoin price surges to record high", "Ethereum hack drains $50M", "Solana partnership announced"]
    r = NewsAgent.keyword_sentiment(heads, ["BTC", "ETH", "SOL"])
    assert r["BTC"]["score"] > 0 and r["ETH"]["score"] < 0 and r["SOL"]["score"] > 0


def reports(score):
    return {k: {"score": score} for k in ("technical", "prediction", "news", "forex")}


def test_decision_respects_spot_rules():
    d = DecisionAgent()
    assert d.run("BTCUSDT", reports(0.8), holding=False)["action"] == "BUY"
    assert d.run("BTCUSDT", reports(0.8), holding=True)["action"] == "HOLD"   # already long
    assert d.run("BTCUSDT", reports(-0.8), holding=False)["action"] == "HOLD"  # can't short
    assert d.run("BTCUSDT", reports(-0.8), holding=True)["action"] == "SELL"


def test_decision_uses_claude_and_falls_back():
    claude = SimpleNamespace(ask=lambda *a, **k: {"action": "BUY", "confidence": 0.9, "reasoning": "x"})
    assert DecisionAgent(claude).run("BTCUSDT", reports(0), holding=False)["source"] == "claude"
    broken = SimpleNamespace(ask=lambda *a, **k: None)
    assert DecisionAgent(broken).run("BTCUSDT", reports(0), holding=False)["source"] == "rules"


PORT = {"cash": 10_000, "equity": 10_000, "open_positions": 0, "day_start_equity": 10_000}


def test_risk_sizing_and_vetoes():
    r = RiskManagerAgent(Settings())
    buy = {"action": "BUY", "confidence": 0.8}
    v = r.review(buy, "BTCUSDT", 100, 2, PORT)
    assert v["approved"] and v["quote_amount"] == 1000 and v["stop_loss"] == 96 and v["take_profit"] == 106
    assert not r.review({"action": "BUY", "confidence": 0.5}, "BTCUSDT", 100, 2, PORT)["approved"]
    assert not r.review(buy, "BTCUSDT", 100, 2, PORT | {"open_positions": 3})["approved"]
    assert not r.review(buy, "BTCUSDT", 100, 2, PORT | {"equity": 9_600})["approved"]  # -4% day
    assert r.review({"action": "SELL", "confidence": 0.1}, "BTCUSDT", 100, 2, PORT)["approved"]
    assert r.exit_signal({"stop_loss": 96, "take_profit": 106}, 95) == "stop-loss"


def test_paper_round_trip_pnl():
    j = Journal(":memory:", 10_000)
    prices = {"BTCUSDT": 100.0}
    ex = ExecutionAgent(PaperBroker(lambda s: prices[s]))
    buy = ex.execute("BUY", "BTCUSDT", quote_amount=1000)
    j.open_position("BTCUSDT", buy, 90, 120)
    j.log_order("BTCUSDT", "BUY", buy, "signal")
    prices["BTCUSDT"] = 110.0
    sell = ex.execute("SELL", "BTCUSDT", qty=buy["qty"])
    j.log_order("BTCUSDT", "SELL", sell, "signal")
    trade = j.close_position("BTCUSDT", sell, "signal")
    assert trade["pnl"] == pytest.approx(1000 * 0.999 * 1.1 * 0.999 - 1000)
    p = performance(j)
    assert p["execution_success_rate_pct"] == 100.0 and p["win_rate_pct"] == 100.0


class FakeTestnet:
    def __init__(self):
        self.sent = []

    def symbol_filters(self, s):
        return {"step_size": "0.00001000", "min_qty": 0.00001, "min_notional": 5}

    def price(self, s):
        return 50_000.0

    def balances(self):
        return {"BTC": 0.01, "USDT": 1000}

    def market_order(self, symbol, side, qty, cid):
        self.sent.append((side, qty))
        return {"status": "FILLED", "orderId": 1, "executedQty": str(qty), "cummulativeQuoteQty": str(qty * 50_000),
                "fills": [{"price": "50000", "qty": str(qty), "commission": str(qty * 0.001), "commissionAsset": "BTC"}]}


def test_testnet_broker_rounds_and_accounts_for_fees():
    fake = FakeTestnet()
    res = ExecutionAgent(TestnetBroker(fake)).execute("BUY", "BTCUSDT", quote_amount=1000)
    assert res["ok"] and fake.sent[0] == ("BUY", 0.02)
    assert res["qty"] == pytest.approx(0.02 * 0.999)
    # selling more than we hold is capped at the actual free balance
    ExecutionAgent(TestnetBroker(fake)).execute("SELL", "BTCUSDT", qty=5)
    assert fake.sent[1] == ("SELL", 0.01)


def test_execution_reports_failures_without_crashing():
    class Bad:
        def buy(self, *a):
            raise BinanceError("Account has insufficient balance")
    res = ExecutionAgent(Bad()).execute("BUY", "BTCUSDT", quote_amount=100)
    assert res == {"ok": False, "error": "Account has insufficient balance"}


def test_live_mode_is_blocked(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(SystemExit):
        Settings.load()


def test_offline_orchestrator_end_to_end():
    s = Settings(offline=True)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    for _ in range(150):
        out = bot.run_cycle()
        assert all("error" not in r for r in out)
    p = performance(bot.journal, bot.last_price)
    assert p["orders_sent"] > 0 and p["execution_success_rate_pct"] == 100.0
    assert bot.journal.cash >= 0
