"""Exchange-side stop-loss / take-profit (OCO) protection."""
import sqlite3

import pytest

from trader.agents.execution import STOP_SLIPPAGE, ExecutionAgent, PaperBroker, TestnetBroker
from trader.binance_client import BinanceClient, BinanceError
from trader.config import Settings
from trader.journal import Journal
from trader.notify import trade_messages
from trader.orchestrator import Orchestrator
from trader.report import status_text

FILTERS = {"step_size": "0.00001000", "min_qty": 0.00001, "min_notional": 5.0, "tick_size": "0.01000000",
           "oco_allowed": True, "order_types": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"]}


# ---------------------------------------------------------------- request shape
def test_oco_sell_request_matches_binance_docs(monkeypatch):
    c = BinanceClient("https://x", "k", "s")
    c._filters["BTCUSDT"] = FILTERS
    sent = {}
    monkeypatch.setattr(c, "_request", lambda m, path, params=None, signed=False: sent.update(
        method=m, path=path, params=params, signed=signed) or {"orderListId": 1, "orders": []})
    c.oco_sell("BTCUSDT", 0.0123456, take_profit=70000.004, stop_loss=61999.996, list_client_id="agent-abc")
    p = sent["params"]
    assert (sent["method"], sent["path"], sent["signed"]) == ("POST", "/api/v3/orderList/oco", True)
    assert p["side"] == "SELL" and p["quantity"] == "0.01234" and p["listClientOrderId"] == "agent-abc"
    assert p["aboveType"] == "LIMIT_MAKER" and p["abovePrice"] == "70000.00" and p["aboveClientOrderId"] == "agent-abc-tp"
    assert p["belowType"] == "STOP_LOSS" and p["belowStopPrice"] == "62000.00" and p["belowClientOrderId"] == "agent-abc-sl"
    assert "belowPrice" not in p
    c.oco_sell("BTCUSDT", 0.01, 70000, 62000, "agent-x", stop_type="STOP_LOSS_LIMIT", stop_limit=61380)
    assert sent["params"]["belowPrice"] == "61380.00" and sent["params"]["belowTimeInForce"] == "GTC"


# ---------------------------------------------------------------- testnet broker
class FakeClient:
    def __init__(self):
        self.free = {"BTC": 0.02, "USDT": 1000}
        self.orders = {}          # orderId -> order record
        self.trades = {}          # orderId -> fills
        self.oco_calls, self.cancelled = [], []
        self.cancel_error = None
        self.filters = dict(FILTERS)

    def symbol_filters(self, s):
        return self.filters

    def balances(self):
        return dict(self.free)

    def price(self, s):
        return 65_000.0

    def oco_sell(self, symbol, qty, take, stop, list_id, stop_type="STOP_LOSS", stop_limit=None):
        self.oco_calls.append((symbol, qty, take, stop, list_id, stop_type))
        self.orders[11] = {"orderId": 11, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"}
        self.orders[12] = {"orderId": 12, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"}
        return {"orderListId": 7, "orders": [{"clientOrderId": f"{list_id}-tp", "orderId": 11},
                                            {"clientOrderId": f"{list_id}-sl", "orderId": 12}]}

    def get_order_by_id(self, symbol, order_id):
        if order_id not in self.orders:
            raise BinanceError("HTTP 400: Order does not exist.")
        return self.orders[order_id]

    def my_trades(self, symbol, order_id):
        return self.trades.get(order_id, [])

    def cancel_order_list(self, symbol, list_id):
        if self.cancel_error:
            raise BinanceError(self.cancel_error)
        self.cancelled.append(list_id)
        for o in self.orders.values():
            o["status"] = "CANCELED"
        return {"listOrderStatus": "ALL_DONE"}


def test_protect_places_oco_for_the_free_balance_and_maps_leg_ids():
    fc = FakeClient()
    prot = TestnetBroker(fc).protect("BTCUSDT", qty=0.0249, stop=62000, take=70000)
    assert fc.oco_calls[0][1:4] == (0.02, 70000, 62000)  # capped at the 0.02 BTC we actually hold
    assert fc.oco_calls[0][5] == "STOP_LOSS"
    assert prot["kind"] == "exchange" and prot["tp_order_id"] == 11 and prot["sl_order_id"] == 12
    assert prot["list_id"] == 7 and prot["list_client_id"] == fc.oco_calls[0][4]


def test_protect_refuses_tiny_positions_and_symbols_without_oco():
    fc = FakeClient()
    fc.free["BTC"] = 0.00005  # worth about $3, under the $5 exchange minimum
    with pytest.raises(BinanceError, match="too small"):
        TestnetBroker(fc).protect("BTCUSDT", 0.00005, 62000, 70000)
    fc = FakeClient()
    fc.filters = {**FILTERS, "oco_allowed": False}
    with pytest.raises(BinanceError, match="OCO"):
        TestnetBroker(fc).protect("BTCUSDT", 0.02, 62000, 70000)
    fc = FakeClient()
    fc.filters = {**FILTERS, "order_types": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"]}
    TestnetBroker(fc).protect("BTCUSDT", 0.02, 62000, 70000)
    assert fc.oco_calls[0][5] == "STOP_LOSS_LIMIT"  # falls back when plain STOP_LOSS is not offered


def test_protection_status_reports_each_state():
    fc = FakeClient()
    b = TestnetBroker(fc)
    prot = b.protect("BTCUSDT", 0.02, 62000, 70000)
    assert b.protection_status("BTCUSDT", prot)["state"] == "protected"

    # the stop-loss fired: Binance filled it and expired the take-profit; fee paid in USDT
    fc.orders[12].update(status="FILLED", executedQty="0.02", cummulativeQuoteQty="1239.00")
    fc.orders[11]["status"] = "EXPIRED"
    fc.trades[12] = [{"price": "61950", "qty": "0.02", "commission": "1.239", "commissionAsset": "USDT"}]
    st = b.protection_status("BTCUSDT", prot)
    assert st["state"] == "exited" and st["reason"] == "stop-loss"
    assert st["fill"]["qty"] == 0.02 and st["fill"]["avg_price"] == 61950
    assert st["fill"]["quote"] == pytest.approx(1239.0 - 1.239) and st["fill"]["fee_quote"] == pytest.approx(1.239)

    fc.orders[12].update(status="EXPIRED", executedQty="0", cummulativeQuoteQty="0")
    fc.orders[11].update(status="PARTIALLY_FILLED", executedQty="0.01", cummulativeQuoteQty="700")
    assert b.protection_status("BTCUSDT", prot)["state"] == "executing"

    fc.orders[11].update(status="CANCELED", executedQty="0", cummulativeQuoteQty="0")
    assert b.protection_status("BTCUSDT", prot)["state"] == "gone"

    del fc.orders[11]  # wiped by a testnet reset
    assert b.protection_status("BTCUSDT", prot)["state"] == "gone"


def test_unprotect_cancels_or_reports_an_exit_that_already_happened():
    fc = FakeClient()
    b = TestnetBroker(fc)
    prot = b.protect("BTCUSDT", 0.02, 62000, 70000)
    assert b.unprotect("BTCUSDT", prot) == {"state": "cancelled"} and fc.cancelled == [prot["list_client_id"]]

    fc.cancel_error = "HTTP 400: Unknown order sent."  # already done: the take-profit filled meanwhile
    fc.orders[11].update(status="FILLED", executedQty="0.02", cummulativeQuoteQty="1400")
    fc.orders[12]["status"] = "EXPIRED"
    st = b.unprotect("BTCUSDT", prot)
    assert st["state"] == "exited" and st["reason"] == "take-profit"

    fc.orders[11].update(status="NEW", executedQty="0", cummulativeQuoteQty="0")
    fc.orders[12]["status"] = "NEW"
    with pytest.raises(BinanceError, match="could not cancel"):
        b.unprotect("BTCUSDT", prot)


# ---------------------------------------------------------------- paper emulation
def test_paper_broker_emulates_exchange_stops_from_the_price_path():
    rng = {"value": None}
    pb = PaperBroker(lambda s: 100.0, range_fn=lambda s, since: rng["value"])
    prot = pb.protect("BTCUSDT", 1.0, stop=95, take=110)
    assert prot["kind"] == "emulated"
    prot["since_ms"] = 0  # pretend it was placed long ago so the test's small timestamps move it forward
    assert pb.protection_status("BTCUSDT", prot)["state"] == "protected"  # no price data yet

    rng["value"] = (96, 104, 1_000)
    st = pb.protection_status("BTCUSDT", prot)
    assert st["state"] == "protected" and st["protection"]["since_ms"] == 1_000  # window moves forward

    rng["value"] = (94.5, 104, 2_000)  # dipped through the stop between checks
    st = pb.protection_status("BTCUSDT", prot)
    assert st["state"] == "exited" and st["reason"] == "stop-loss"
    assert st["fill"]["avg_price"] == pytest.approx(95 * (1 - STOP_SLIPPAGE))

    rng["value"] = (99, 111, 3_000)
    st = pb.protection_status("BTCUSDT", prot)
    assert st["reason"] == "take-profit" and st["fill"]["avg_price"] == 110
    assert pb.unprotect("BTCUSDT", prot) == {"state": "cancelled"}


# ---------------------------------------------------------------- orchestrator lifecycle
class FakeBroker:
    def __init__(self, price_fn):
        self.price_fn = price_fn
        self.calls = []
        self.fail_protect = False
        self.next_status = {"state": "protected"}
        self.next_unprotect = {"state": "cancelled"}

    def _fill(self, qty, price):
        return {"status": "FILLED", "order_id": "f1", "qty": qty, "avg_price": price, "quote": qty * price,
                "fee_quote": 0}

    def buy(self, sym, quote):
        self.calls.append("buy")
        return self._fill(quote / self.price_fn(sym), self.price_fn(sym))

    def sell(self, sym, qty):
        self.calls.append("sell")
        return self._fill(qty, self.price_fn(sym))

    def protect(self, sym, qty, stop, take):
        self.calls.append("protect")
        if self.fail_protect:
            raise BinanceError("Filter failure: PERCENT_PRICE_BY_SIDE")
        return {"kind": "exchange", "list_client_id": "L", "list_id": 1, "tp_order_id": 2, "sl_order_id": 3,
                "qty": qty, "stop": stop, "take": take}

    def protection_status(self, sym, prot):
        self.calls.append("status")
        return {"protection": prot, **self.next_status}

    def unprotect(self, sym, prot):
        self.calls.append("unprotect")
        return {"protection": prot, **self.next_unprotect}


@pytest.fixture
def bot():
    s = Settings(offline=True, symbols=["BTCUSDT"], trail_atr=0)  # these tests are about the order flow, not trailing
    b = Orchestrator(s, Journal(":memory:", s.starting_balance))
    b.broker = FakeBroker(lambda sym: b.last_price[sym])
    b.execution = ExecutionAgent(b.broker)
    b.decide = {"action": "HOLD"}
    b.decision.run = lambda sym, reports, holding, use_ai=True, threshold=None: {**b.decide, "confidence": 0.9, "reasoning": "r",
                                                    "source": "rules", "combined_score": 0.5}
    return b


def exit_fill(qty, price):
    return {"status": "FILLED", "order_id": "x1", "qty": qty, "avg_price": price, "quote": qty * price, "fee_quote": 0}


def test_buy_is_protected_at_once_and_exchange_exit_is_recorded(bot):
    bot.decide = {"action": "BUY"}
    out = bot.run_cycle()
    assert out[0]["protected"] == "exchange" and bot.broker.calls == ["buy", "protect"]
    pos = bot.journal.positions()["BTCUSDT"]
    assert pos["protected"] == "exchange" and pos["protection"]["sl_order_id"] == 3

    bot.decide = {"action": "HOLD"}
    bot.broker.next_status = {"state": "exited", "reason": "stop-loss", "fill": exit_fill(pos["qty"], pos["stop_loss"])}
    out = bot.run_cycle()
    assert out[0]["action"] == "SELL" and out[0]["reason"] == "stop-loss (exchange)"
    assert out[0]["trade"]["exit_reason"] == "stop-loss (exchange)" and out[0]["trade"]["pnl"] < 0
    assert bot.journal.positions() == {} and "sell" not in bot.broker.calls
    assert bot.journal.cash == pytest.approx(10_000 - pos["cost"] + pos["qty"] * pos["stop_loss"])


def test_lost_protection_is_placed_again_and_partial_exit_keeps_the_rest_protected(bot):
    bot.decide = {"action": "BUY"}
    bot.run_cycle()
    bot.decide = {"action": "HOLD"}
    bot.broker.next_status = {"state": "gone", "detail": "sl CANCELED, tp CANCELED"}
    bot.run_cycle()
    assert bot.broker.calls.count("protect") == 2 and bot.journal.positions()["BTCUSDT"]["protected"] == "exchange"

    pos = bot.journal.positions()["BTCUSDT"]
    bot.broker.next_status = {"state": "exited", "reason": "take-profit", "fill": exit_fill(pos["qty"] / 2, pos["take_profit"])}
    bot.run_cycle()
    left = bot.journal.positions()["BTCUSDT"]
    assert left["qty"] == pytest.approx(pos["qty"] / 2)
    assert bot.broker.calls.count("protect") == 3 and left["protected"] == "exchange"  # remainder re-protected


def test_signal_sell_cancels_the_exchange_orders_first(bot):
    bot.decide = {"action": "BUY"}
    bot.run_cycle()
    bot.broker.calls.clear()
    bot.decide = {"action": "SELL"}
    out = bot.run_cycle()
    assert bot.broker.calls == ["status", "unprotect", "sell"] and out[0]["trade"]["exit_reason"] == "signal"

    bot.decide = {"action": "BUY"}
    bot.run_cycle()
    pos = bot.journal.positions()["BTCUSDT"]
    bot.broker.calls.clear()
    bot.decide = {"action": "SELL"}
    bot.broker.next_unprotect = {"state": "exited", "reason": "take-profit", "fill": exit_fill(pos["qty"], pos["take_profit"])}
    out = bot.run_cycle()
    assert "sell" not in bot.broker.calls and out[0]["trade"]["exit_reason"] == "take-profit (exchange)"


def test_failed_protection_falls_back_to_desk_checks_and_faster_pace(bot):
    bot.broker.fail_protect = True
    bot.decide = {"action": "BUY"}
    out = bot.run_cycle()
    assert "PERCENT_PRICE" in out[0]["protect_error"] and "protected" not in out[0]
    pos = bot.journal.positions()["BTCUSDT"]
    assert pos["protected"] == "none" and pos["protection"] is None and "BTCUSDT" in bot.protect_errors

    atr = 4.0
    ctx = {"BTCUSDT": {"price": pos["stop_loss"] + 0.25 * atr, "technical": {"atr": atr}}}
    assert bot._next_pace(None, ctx)["source"] == "risk"           # unprotected and near the stop: hurry
    bot.journal.set_protection("BTCUSDT", {"kind": "exchange"}, "exchange")
    assert bot._next_pace(None, ctx)["source"] == "settings"       # the exchange watches it: normal pace

    bot.broker.fail_protect = False
    bot.decide = {"action": "HOLD"}
    bot.journal.set_protection("BTCUSDT", None, "none")
    bot.run_cycle()
    assert bot.journal.positions()["BTCUSDT"]["protected"] == "exchange"  # retried and succeeded


def test_offline_simulation_uses_emulated_exchange_orders():
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    b = Orchestrator(s, Journal(":memory:", s.starting_balance))
    reasons = set()
    for _ in range(300):
        for r in b.run_cycle():
            if t := r.get("trade"):
                reasons.add(t["exit_reason"])
    for p in b.journal.positions().values():
        assert p["protected"] == "emulated"
    # a trailing stop moved above the price after a quick fall is refused, as Binance refuses it, and the desk sells at
    # the market price ("stop-loss") instead of a simulated fill at the higher stop (1.7.1)
    assert reasons and all(r.endswith("(emulated)") or r in ("signal", "stop-loss", "take-profit") for r in reasons)
    assert any(r.endswith("(emulated)") for r in reasons)


# ---------------------------------------------------------------- journal migration, messages, status
def test_old_journal_files_gain_the_protection_columns(tmp_path):
    path = tmp_path / "journal.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE positions (symbol TEXT PRIMARY KEY, qty REAL, entry_price REAL, cost REAL, "
               "stop_loss REAL, take_profit REAL, opened_at TEXT)")
    db.execute("INSERT INTO positions VALUES ('BTCUSDT', 0.01, 60000, 600, 58000, 63000, '2026-10-01T00:00:00')")
    db.commit()
    db.close()
    j = Journal(str(path), 10_000)
    pos = j.positions()["BTCUSDT"]
    assert pos["protected"] == "none" and pos["protection"] is None
    j.set_protection("BTCUSDT", {"kind": "exchange", "list_id": 5}, "exchange")
    assert j.positions()["BTCUSDT"]["protection"]["list_id"] == 5


def test_messages_and_status_show_protection():
    msgs = trade_messages([
        {"symbol": "BTCUSDT", "protect_error": "Filter failure: PERCENT_PRICE_BY_SIDE"},
        {"symbol": "ETHUSDT", "action": "BUY", "confidence": 0.8, "reasoning": "r", "stop_loss": 2000.0,
         "take_profit": 2600.0, "protected": "exchange",
         "order": {"ok": True, "qty": 0.5, "avg_price": 2300.0, "quote": 1150.0}},
        {"symbol": "SOLUSDT", "action": "SELL", "reason": "stop-loss (exchange)",
         "order": {"ok": True, "qty": 10, "avg_price": 95.0, "quote": 950.0}, "trade": {"pnl": -50.0, "pnl_pct": -5.0}},
    ])
    assert msgs[0][0] == "protect:BTCUSDT" and "every 5 minutes" in msgs[0][1]
    assert "🛡️ both placed on Binance" in msgs[1][1]
    assert "fired on Binance" in msgs[2][1] and "Loss -50.00" in msgs[2][1]

    j = Journal(":memory:", 10_000)
    j.open_position("BTCUSDT", {"qty": 0.01, "avg_price": 60000, "quote": 600}, 58000, 63000)
    assert "desk-side checks only" in status_text(j, {"BTCUSDT": 61000}, ["BTCUSDT"])
    j.set_protection("BTCUSDT", {"kind": "exchange"}, "exchange")
    assert "live on Binance" in status_text(j, {"BTCUSDT": 61000}, ["BTCUSDT"])


def test_a_paper_exit_is_recorded_at_the_minute_it_happened():
    """1.7.5: the minute candles tell when the simulated stop filled (the trend rule's re-entry counts from it)."""
    minutes = [{"time": 60_000 * i, "low": 100 - i, "high": 101} for i in range(10)]
    pb = PaperBroker(lambda s: 100.0, range_fn=lambda s, since: (min(r["low"] for r in minutes), 101, 540_000, minutes))
    prot = pb.protect("BTCUSDT", 1.0, stop=95, take=110)
    prot["since_ms"] = 0
    st = pb.protection_status("BTCUSDT", prot)
    assert st["reason"] == "stop-loss" and st["fill"]["time_ms"] == 300_000   # the first minute whose low reached 95
