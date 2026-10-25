"""Defects found by the evidence review: forming-candle decisions, dust remainders, stock tokens, console output."""
import json
import time
from types import SimpleNamespace

import pytest

from trader.agents.execution import ExecutionAgent, TestnetBroker
from trader.agents.market import MarketDataAgent
from trader.agents.scout import is_stock_token
from trader.agents.technical import TechnicalAnalysisAgent
from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator
from tests.test_protection import FakeClient


class FakePublic:
    """200 flat candles, then a forming candle whose price has doubled."""
    def klines(self, symbol, interval, limit=200, start_ms=None):
        now = int(time.time() * 1000)
        out = []
        for i in range(limit):
            p = 100 + (i % 7) * 0.1
            out.append({"time": now - (limit - i) * 3_600_000, "open": p, "high": p + 0.2, "low": p - 0.2, "close": p, "volume": 1})
        out[-1].update(open=100.0, high=200.0, low=100.0, close=200.0)
        return out


def test_closed_candles_drop_the_forming_one_only_when_live():
    live = MarketDataAgent(SimpleNamespace(), "1h", offline=False)
    candles = [{"close": 1}, {"close": 2}, {"close": 3}]
    assert live.closed(candles) == candles[:2] and live.closed(candles[:1]) == candles[:1]
    assert MarketDataAgent(None, "1h", offline=True).closed(candles) == candles


def test_desk_decides_on_finished_candles_but_prices_live():
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=())
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.market = MarketDataAgent(FakePublic(), "1h", offline=False)
    bot.run_cycle()
    assert bot.last_price["BTCUSDT"] == 200.0                                   # live price for stops and sizing
    row = bot.journal.db.execute("SELECT reports FROM decisions WHERE symbol='BTCUSDT'").fetchone()
    tech = json.loads(row["reports"])["technical"]
    expected = TechnicalAnalysisAgent().run(FakePublic().klines("BTCUSDT", "1h")[:-1])
    assert tech["price"] == 200.0 and tech["rsi"] == expected["rsi"] and tech["atr"] == expected["atr"]
    assert tech["atr"] < 1  # the spike in the forming candle did not enter the indicators


def test_journal_keeps_real_partial_sales_but_closes_dust():
    j = Journal(":memory:", 10_000)
    j.open_position("ETHUSDT", {"qty": 1.0, "avg_price": 2000, "quote": 2000}, 1900, 2200)
    j.close_position("ETHUSDT", {"qty": 0.5, "avg_price": 2100, "quote": 1050}, "signal", step=0.0001)
    assert j.positions()["ETHUSDT"]["qty"] == pytest.approx(0.5)              # a real partial fill stays open
    t = j.close_position("ETHUSDT", {"qty": 0.49995, "avg_price": 2100, "quote": 1049.9}, "signal", step=0.0001)
    assert "ETHUSDT" not in j.positions() and t["cost"] == pytest.approx(1000)  # below one step: closed, cost included
    j.open_position("ETHUSDT", {"qty": 1.0, "avg_price": 2000, "quote": 2000}, 1900, 2200)
    j.close_position("ETHUSDT", {"qty": 0.99995, "avg_price": 2100, "quote": 2099}, "signal")  # no step known
    assert "ETHUSDT" not in j.positions()                                     # the old 99.9% rule still applies


def test_exchange_exit_closes_the_position_when_only_dust_remains():
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=(), trail_atr=0)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    fc = FakeClient()
    broker = TestnetBroker(fc)
    bot.execution = ExecutionAgent(broker)
    # the buy fee was charged in BTC, so the position is a little smaller than a whole number of steps
    bot.journal.open_position("BTCUSDT", {"qty": 0.00011988, "avg_price": 80_000, "quote": 9.59}, 78_000, 84_000)
    prot = broker.protect("BTCUSDT", 0.00011988, 78_000, 84_000)
    assert prot["qty"] == pytest.approx(0.00011)                              # the OCO sells the rounded amount
    bot.journal.set_protection("BTCUSDT", prot, "exchange")
    fc.orders[12].update(status="FILLED", executedQty="0.00011", cummulativeQuoteQty="8.58")
    fc.orders[11]["status"] = "EXPIRED"
    fc.trades[12] = [{"price": "78000", "qty": "0.00011", "commission": "0.00858", "commissionAsset": "USDT"}]
    bot.last_price["BTCUSDT"] = 78_000.0
    position, exit_result = bot._sync_protection("BTCUSDT", bot.journal.positions()["BTCUSDT"])
    assert position is None and "BTCUSDT" not in bot.journal.positions()     # no 0.00000988 BTC ghost position
    assert exit_result["trade"]["cost"] == pytest.approx(9.59) and exit_result["trade"]["qty"] == pytest.approx(0.00011)


def test_reconcile_writes_off_dust_left_by_an_earlier_version():
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=())
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.execution = ExecutionAgent(TestnetBroker(FakeClient()))
    bot.testnet = SimpleNamespace(balances_total=lambda: {"BTC": 0.000005, "USDT": 10_000})
    bot.journal.open_position("BTCUSDT", {"qty": 0.000005, "avg_price": 80_000, "quote": 0.4}, 78_000, 84_000)
    out = bot.reconcile()
    assert out[0]["action"] == "DUST" and "BTCUSDT" not in bot.journal.positions()
    last = bot.journal.events()[-1]
    assert last["kind"] == "trade" and "dust" in last["text"]
    assert ExecutionAgent(SimpleNamespace()).step_size("BTCUSDT") is None       # paper broker: no rounding


def test_stock_tokens_are_recognised_by_shape():
    assert all(is_stock_token(b) for b in ("TSLAB", "AAPLB", "SPYB", "PENGB", "USDEB"))
    assert not any(is_stock_token(b) for b in ("SHIB", "ARB", "BNB", "BTC", "PEPE"))


def test_console_printer_survives_protection_and_dust_entries(capsys):
    from trader.__main__ import print_cycle
    print_cycle(1, [{"symbol": "BTCUSDT", "protect_error": "OCO not allowed"},
                    {"symbol": "ETHUSDT", "action": "DUST", "reason": "wrote off 0.00001"}])
    out = capsys.readouterr().out
    assert "protection not placed: OCO not allowed" in out and "DUST" in out
