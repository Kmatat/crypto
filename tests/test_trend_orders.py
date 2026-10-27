"""1.7.1: the daily trend rule's orders on an exchange that enforces Binance's price rules."""
import itertools

import requests

from tests.test_trend import candles, trend_desk, wavy
from trader.agents.execution import ExecutionAgent, TestnetBroker
from trader.binance_client import BinanceError

UP = wavy(130, 100, 0.02)
PRICE = UP[-1]


class Exchange:
    """A demo exchange with Binance's rules for a protective pair: take-profit > price > stop, a sell limit within
    0.8x-2x of the average price, stop-limit legs priced like any limit."""
    def __init__(self, prices, order_types=("LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT")):
        self.prices, self.avg, self.order_types = dict(prices), dict(prices), list(order_types)
        self.coins, self.ocos, self.cancelled = {}, [], []
        self.refuse_oco = self.refuse_cancel = False
        self.ids = itertools.count(100)

    def symbol_filters(self, symbol):
        return {"step_size": "0.0001", "min_qty": 0.0, "min_notional": 5.0, "tick_size": "0.0001",
                "oco_allowed": True, "order_types": self.order_types, "ask_up": 2.0, "ask_down": 0.8}

    def price(self, symbol):
        return self.prices[symbol]

    def avg_price(self, symbol):
        return self.avg[symbol]

    def balances(self):
        return dict(self.coins)

    def balances_total(self):
        return dict(self.coins)

    def market_order(self, symbol, side, qty, client_id):
        coin = symbol.removesuffix("USDT")
        self.coins[coin] = self.coins.get(coin, 0.0) + (qty if side == "BUY" else -qty)
        quote = qty * self.prices[symbol]
        return {"status": "FILLED", "executedQty": str(qty), "cummulativeQuoteQty": str(quote), "orderId": next(self.ids),
                "fills": [{"commission": str(quote * 0.001), "commissionAsset": "USDT", "price": str(self.prices[symbol])}]}

    def oco_sell(self, symbol, qty, take, stop, list_id, stop_type, stop_limit):
        price, avg = self.prices[symbol], self.avg[symbol]
        if self.refuse_oco or not take > price > stop or take > 2.0 * avg or (
                stop_type == "STOP_LOSS_LIMIT" and stop_limit < 0.8 * avg):
            raise BinanceError("HTTP 400: Filter failure: PERCENT_PRICE_BY_SIDE")
        self.ocos.append({"symbol": symbol, "take": take, "stop": stop, "type": stop_type})
        return {"orderListId": next(self.ids), "orders": [{"clientOrderId": f"{list_id}-tp", "orderId": next(self.ids)},
                                                          {"clientOrderId": f"{list_id}-sl", "orderId": next(self.ids)}]}

    def get_order_by_id(self, symbol, order_id):
        return {"status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0", "orderId": order_id}

    def cancel_order_list(self, symbol, list_id):
        if self.refuse_cancel:
            raise requests.ConnectionError("timeout")
        self.cancelled.append(list_id)
        return {}

    def my_trades(self, symbol, order_id=None, limit=500):
        return []


def demo_desk(prices=None, **kw):
    series = {"AAAUSDT": candles(UP)}
    bot, j = trend_desk(series, **kw)
    ex = Exchange(prices or {"AAAUSDT": PRICE, "BTCUSDT": 50_000.0})
    bot.execution = ExecutionAgent(TestnetBroker(ex))
    bot.testnet = ex
    return bot, j, ex


def test_the_take_profit_placed_is_what_binance_accepts():
    bot, j, ex = demo_desk()
    ex.avg["AAAUSDT"] = PRICE * 0.9                 # the exchange's 5-minute average lags the price
    bot.run_cycle()
    p = j.positions()["AAAUSDT"]
    assert p["protected"] == "exchange" and ex.ocos[-1]["type"] == "STOP_LOSS"
    assert abs(p["take_profit"] - 2.0 * PRICE * 0.9 * 0.98) < 1e-6 < abs(p["take_profit"] - PRICE * 1.9)
    assert abs(p["stop_loss"] / p["entry_price"] - 0.75) < 1e-3


def test_a_coin_with_only_stop_limit_orders_is_watched_by_the_desk_without_doomed_orders():
    bot, j, ex = demo_desk()
    ex.order_types = ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"]
    out = bot.run_cycle()
    assert "AAAUSDT" in j.positions() and not ex.ocos
    assert any("only stop-limit orders" in str(r.get("protect_error")) for r in out)
    assert bot.pace["minutes"] == bot.s.min_check_minutes          # the desk watches the stop itself, often


def test_a_refused_raise_puts_the_previous_pair_back():
    bot, j, ex = demo_desk()
    bot.run_cycle()
    before = j.positions()["AAAUSDT"]
    bot.market.prices["AAAUSDT"] = ex.prices["AAAUSDT"] = ex.avg["AAAUSDT"] = before["take_profit"] / 1.25
    ex.refuse_oco = True                                             # the new pair is refused after the cancel
    out = bot.run_cycle()
    assert not [r for r in out if r.get("action") == "RAISE"]
    assert not any("take-profit order raised" in e["text"] for e in j.events())
    after = j.positions()["AAAUSDT"]
    assert after["take_profit"] == before["take_profit"] and after["stop_loss"] == before["stop_loss"]
    assert any("could not be raised" in e["text"] for e in j.events()) and len(ex.cancelled) == 1
    ex.refuse_oco = False
    out = bot.run_cycle()                                            # the next check raises it
    assert [r["action"] for r in out if r.get("action") == "RAISE"] == ["RAISE"]
    assert j.positions()["AAAUSDT"]["protected"] == "exchange"


def test_an_adoption_that_cannot_cancel_the_old_orders_is_tried_again():
    bot, j, ex = demo_desk()
    j.open_position("AAAUSDT", {"qty": 1.0, "avg_price": PRICE, "quote": PRICE}, PRICE * 0.97, PRICE * 1.06)
    ex.coins["AAA"] = 1.0
    j.set_protection("AAAUSDT", {"kind": "exchange", "list_client_id": "L1", "sl_order_id": 1, "tp_order_id": 2,
                                 "stop": PRICE * 0.97, "take": PRICE * 1.06}, "exchange")
    ex.refuse_cancel = True
    out = bot.run_cycle()
    assert not [r for r in out if r.get("action") == "ADOPT"] and "AAAUSDT" not in (j.get("trend_positions") or {})
    assert abs(j.positions()["AAAUSDT"]["stop_loss"] - PRICE * 0.97) < 1e-9 and j.get("trend_day") is None
    ex.refuse_cancel = False
    out = bot.run_cycle()
    assert [r["action"] for r in out if r.get("action") == "ADOPT"] == ["ADOPT"]
    assert abs(j.positions()["AAAUSDT"]["stop_loss"] / PRICE - 0.75) < 1e-9


def test_a_held_coin_without_a_price_does_not_stop_the_day():
    bot, j, ex = demo_desk()
    j.open_position("GONEUSDT", {"qty": 1.0, "avg_price": 2.0, "quote": 2.0}, 1.0, 3.0)   # an old pick, now delisted
    ex.coins["GONE"] = 1.0
    out = bot.run_cycle()
    assert "AAAUSDT" in j.positions() and "GONEUSDT" in j.positions()
    assert any("no live price" in str(r.get("error")) for r in out)


def test_the_hourly_strategy_asks_for_the_same_pair_as_before():
    from trader.config import Settings
    from trader.journal import Journal
    from trader.orchestrator import Orchestrator
    calls = []

    class Broker:
        def protect(self, symbol, qty, stop, take):          # the hourly signature, without `strict`
            calls.append((symbol, qty, stop, take))
            return {"kind": "exchange", "stop": stop, "take": take}
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    bot.execution = ExecutionAgent(Broker())
    bot.journal.open_position("BTCUSDT", {"qty": 0.1, "avg_price": 100.0, "quote": 10.0}, 95.0, 110.0)
    assert bot._protect("BTCUSDT", bot.journal.positions()["BTCUSDT"]) is None
    assert calls == [("BTCUSDT", 0.1, 95.0, 110.0)]
