"""1.6.6: coins the exchange sold in parts (a protective order filling partly on the thin demo book) are recorded."""
import datetime as dt

from trader.agents.execution import ExecutionAgent, TestnetBroker
from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator

OPENED = dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.timezone.utc)
MS = int(OPENED.timestamp() * 1000)


class Exchange:
    """The few Binance calls the broker makes, with the account's fills on the pair."""
    def __init__(self, trades, held):
        self.trades, self.held, self.cancelled = trades, held, []

    def my_trades(self, symbol, order_id=None, limit=500):
        return [t for t in self.trades if order_id is None or str(t["orderId"]) == str(order_id)]

    def symbol_filters(self, symbol):
        return {"step_size": "0.01", "min_notional": 5.0, "oco_allowed": True}

    def balances_total(self):
        return dict(self.held)

    def balances(self):
        return dict(self.held)

    def cancel_order_list(self, symbol, list_id):
        self.cancelled.append(list_id)
        return {}


def fill(order_id, qty, price, minutes=60, fee_asset="USDT", buyer=False):
    return {"orderId": order_id, "qty": str(qty), "price": str(price), "quoteQty": str(qty * price),
            "commission": str(qty * price * 0.001), "commissionAsset": fee_asset, "time": MS + minutes * 60_000,
            "isBuyer": buyer}


def desk(trades, held, qty=71.6):
    """A desk on the demo account holding 71.6 UNI bought at 8.00, as the owner's journal does."""
    j = Journal(":memory:", 5000)
    bot = Orchestrator(Settings(offline=True, symbols=["UNIUSDT"], starting_balance=5000), j)
    ex = Exchange(trades, held)
    bot.execution = ExecutionAgent(TestnetBroker(ex))
    bot.testnet = ex
    j.open_position("UNIUSDT", {"qty": qty, "avg_price": 8.0, "quote": qty * 8.0}, 7.6, 8.8)
    j.db.execute("UPDATE positions SET opened_at=? WHERE symbol='UNIUSDT'", (OPENED.isoformat(),))
    j.set("cash", 5000 - qty * 8.0)
    j.db.commit()
    return bot, j, ex


def test_the_broker_adds_up_the_accounts_sales_per_order():
    ex = Exchange([fill(41, 6.0, 8.8), fill(41, 4.6, 8.8, minutes=61), fill(40, 71.6, 8.0, minutes=0, buyer=True),
                   fill(39, 5.0, 7.0, minutes=-600)], {})
    sells = TestnetBroker(ex).sells_since("UNIUSDT", MS)
    assert list(sells) == ["41"]                                   # the buy and the older sale are left out
    assert abs(sells["41"]["qty"] - 10.6) < 1e-9
    assert abs(sells["41"]["quote"] - 10.6 * 8.8 * 0.999) < 1e-6 and sells["41"]["fee_quote"] > 0


def test_reconcile_records_the_part_sold_instead_of_asking_for_a_reset():
    bot, j, ex = desk([fill(41, 10.6, 8.8)], {"UNI": 61.0})
    out = bot.reconcile()
    assert not any("error" in r for r in out), out
    assert out[0]["action"] == "SELL" and abs(j.positions()["UNIUSDT"]["qty"] - 61.0) < 1e-6
    assert abs(j.cash - (5000 - 71.6 * 8.0 + 10.6 * 8.8 * 0.999)) < 1e-6         # the money of the part sold is back
    assert bot.reconcile() == []                                                  # recorded once


def test_a_real_demo_reset_is_still_reported():
    bot, j, ex = desk([], {})
    out = bot.reconcile()
    assert "Binance probably reset the demo account" in out[0]["error"]
    bot, j, ex = desk([], {"UNI": 61.0})
    assert "if this stays for a day" in bot.reconcile()[0]["error"]


def test_an_exit_the_exchange_reports_later_counts_only_the_rest():
    bot, j, ex = desk([fill(41, 10.6, 8.8)], {"UNI": 61.0})
    bot.reconcile()                                            # 10.6 of order 41 recorded while it was part-filled
    status = {"state": "exited", "reason": "take-profit", "protection": {"kind": "exchange", "stop": 7.6, "take": 8.8},
              "fill": {"order_id": "41", "qty": 71.6, "avg_price": 8.8, "quote": 71.6 * 8.8 * 0.999,
                       "fee_quote": 71.6 * 8.8 * 0.001}}
    bot._record_exchange_exit("UNIUSDT", status)
    assert "UNIUSDT" not in j.positions()
    sold = j.db.execute("SELECT SUM(qty) FROM orders WHERE side='SELL' AND order_id='41'").fetchone()[0]
    assert abs(sold - 71.6) < 1e-6                             # 10.6 + 61, never 10.6 + 71.6


def test_a_part_filled_protective_order_is_replaced_and_its_sale_recorded():
    bot, j, ex = desk([fill(41, 10.6, 8.8)], {"UNI": 61.0})
    j.set_protection("UNIUSDT", {"kind": "exchange", "list_client_id": "L1", "stop": 7.6, "take": 8.8}, "exchange")
    bot.execution.broker.protection_status = lambda sym, prot: {"state": "executing", "protection": prot}
    position, result = bot._sync_protection("UNIUSDT", j.positions()["UNIUSDT"])
    assert ex.cancelled == ["L1"]                              # what was left of the order is cancelled
    assert result["action"] == "SELL" and abs(position["qty"] - 61.0) < 1e-6
    assert position["protection"] is None                      # this check protects the 61 again with a fresh pair


def test_a_sale_hidden_in_a_cancelled_order_is_found_when_the_desk_sells():
    bot, j, ex = desk([fill(41, 10.6, 8.8)], {"UNI": 61.0})
    j.set_protection("UNIUSDT", {"kind": "exchange", "list_client_id": "L1", "stop": 7.6, "take": 8.8}, "exchange")
    bot.execution.broker.unprotect = lambda sym, prot: {"state": "cancelled"}
    sold = []
    bot.execution.execute = lambda side, sym, qty=0, quote_amount=0: sold.append(qty) or {
        "ok": True, "status": "FILLED", "order_id": "50", "qty": qty, "avg_price": 8.1, "quote": qty * 8.1, "fee_quote": 0}
    bot.last_price["UNIUSDT"] = 8.1
    bot._sell("UNIUSDT", j.positions()["UNIUSDT"], "signal")
    assert abs(sold[0] - 61.0) < 1e-6 and "UNIUSDT" not in j.positions()    # sells what is really there, closes clean
