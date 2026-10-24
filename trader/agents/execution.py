"""Execution agent: turns an approved decision into an actual order and confirms it filled.

Two brokers share one interface:
  PaperBroker   - fills locally at the current market price (simulation mode)
  TestnetBroker - sends real orders to the Binance Spot Testnet (fake money)

Protection: right after a buy, the stop-loss and take-profit are placed ON THE
EXCHANGE as a one-cancels-the-other (OCO) pair, so a crash between two checks
is caught by Binance within seconds instead of by the desk minutes later.
The paper broker emulates this with 1-minute price data.

"Successful trade" here means the order was accepted AND confirmed FILLED by the
exchange, with the filled quantity and average price recorded. Whether the trade
made money is tracked separately by the performance report.
"""
from __future__ import annotations

import logging
import time
import uuid

import requests

from ..binance_client import BinanceClient, BinanceError, round_step

log = logging.getLogger(__name__)
PAPER_FEE = 0.001        # Binance standard spot fee 0.1%
STOP_SLIPPAGE = 0.002    # a stop-loss that fires in a falling market fills a little below the stop price
STOP_LIMIT_GAP = 0.01    # for STOP_LOSS_LIMIT (symbols without plain STOP_LOSS): limit 1% under the stop
FINAL = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PaperBroker:
    """Simulated exchange. `range_fn(symbol, since_ms)` -> (low, high, last_ms) of prices since a moment,
    used to emulate stop/take-profit orders that the exchange would have filled between checks."""

    def __init__(self, price_fn, range_fn=None):
        self.price_fn = price_fn
        self.range_fn = range_fn

    def buy(self, symbol: str, quote_amount: float) -> dict:
        price = self.price_fn(symbol)
        qty = quote_amount * (1 - PAPER_FEE) / price
        return {"status": "FILLED", "order_id": f"paper-{uuid.uuid4().hex[:10]}", "qty": qty,
                "avg_price": price, "quote": quote_amount, "fee_quote": quote_amount * PAPER_FEE}

    def sell(self, symbol: str, qty: float) -> dict:
        return self._sell_at(qty, self.price_fn(symbol))

    @staticmethod
    def _sell_at(qty: float, price: float) -> dict:
        gross = qty * price
        return {"status": "FILLED", "order_id": f"paper-{uuid.uuid4().hex[:10]}", "qty": qty,
                "avg_price": price, "quote": gross * (1 - PAPER_FEE), "fee_quote": gross * PAPER_FEE}

    # ---- protection ----
    def protect(self, symbol: str, qty: float, stop: float, take: float) -> dict:
        return {"kind": "emulated", "qty": qty, "stop": stop, "take": take, "since_ms": _now_ms(),
                "placed_at": _now_ms()}

    def protection_status(self, symbol: str, protection: dict) -> dict:
        rng = self.range_fn(symbol, protection["since_ms"]) if self.range_fn else None
        if not rng:
            return {"state": "protected", "protection": protection}
        low, high, last_ms = rng
        if low <= protection["stop"]:      # a stop-loss market order fills just under the stop in a fall
            fill = self._sell_at(protection["qty"], protection["stop"] * (1 - STOP_SLIPPAGE))
            return {"state": "exited", "reason": "stop-loss", "fill": fill, "protection": protection}
        if high >= protection["take"]:     # a take-profit limit order fills exactly at its price
            fill = self._sell_at(protection["qty"], protection["take"])
            return {"state": "exited", "reason": "take-profit", "fill": fill, "protection": protection}
        return {"state": "protected", "protection": {**protection, "since_ms": max(protection["since_ms"], last_ms)}}

    def unprotect(self, symbol: str, protection: dict) -> dict:
        return {"state": "cancelled"}


class TestnetBroker:
    __test__ = False  # not a pytest test class

    def __init__(self, client: BinanceClient):
        self.client = client

    def buy(self, symbol: str, quote_amount: float) -> dict:
        f = self.client.symbol_filters(symbol)
        if quote_amount < f["min_notional"]:
            raise BinanceError(f"order ${quote_amount:.2f} below exchange minimum ${f['min_notional']}")
        qty = round_step(quote_amount / self.client.price(symbol), f["step_size"])
        return self._place(symbol, "BUY", qty)

    def sell(self, symbol: str, qty: float) -> dict:
        base = symbol.removesuffix("USDT")
        free = self.client.balances().get(base, 0.0)
        qty = round_step(min(qty, free), self.client.symbol_filters(symbol)["step_size"])
        if qty <= 0:
            raise BinanceError(f"no {base} balance on testnet to sell")
        return self._place(symbol, "SELL", qty)

    def _place(self, symbol: str, side: str, qty: float) -> dict:
        client_id = f"agent-{uuid.uuid4().hex[:20]}"
        try:
            resp = self.client.market_order(symbol, side, qty, client_id)
        except requests.RequestException:
            # Network dropped mid-request: the order may or may not exist. Ask the
            # exchange by our own ID instead of blindly re-sending (avoids double orders).
            time.sleep(2)
            resp = self.client.get_order(symbol, client_id)
        if resp.get("status") in (None, "NEW", "PARTIALLY_FILLED"):  # still working: wait for the final state
            resp = self._wait_done(symbol, client_id)
        return self._fill_from_order(symbol, side, resp)

    def _fill_from_order(self, symbol: str, side: str, resp: dict, fills: list[dict] | None = None) -> dict:
        exec_qty = float(resp["executedQty"])
        if exec_qty <= 0:
            raise BinanceError(f"order ended as {resp['status']} with nothing filled")
        quote = float(resp["cummulativeQuoteQty"])
        avg_price = quote / exec_qty
        base = symbol.removesuffix("USDT")
        fee_quote = 0.0
        for x in fills if fills is not None else resp.get("fills", []):
            c = float(x["commission"])
            if x["commissionAsset"] == "USDT":
                fee_quote += c
                if side == "SELL":
                    quote -= c
            elif x["commissionAsset"] == base:
                fee_quote += c * float(x["price"])
                if side == "BUY":
                    exec_qty -= c
            # fees paid in another coin (e.g. BNB discount) don't change our USDT or coin amounts
        # A MARKET order on the thin testnet book can fill partly and expire; keep what we actually got.
        return {"status": "FILLED", "partial": resp["status"] != "FILLED", "order_id": str(resp["orderId"]),
                "qty": exec_qty, "avg_price": avg_price, "quote": quote, "fee_quote": fee_quote}

    def _wait_done(self, symbol: str, client_id: str, attempts: int = 5) -> dict:
        for _ in range(attempts):
            time.sleep(1)
            o = self.client.get_order(symbol, client_id)
            if o["status"] in FINAL:
                break
        return o

    # ---- protection: OCO pair on the exchange ----
    def protect(self, symbol: str, qty: float, stop: float, take: float) -> dict:
        f = self.client.symbol_filters(symbol)
        if not f["oco_allowed"]:
            raise BinanceError(f"{symbol} does not support OCO orders on this exchange")
        base = symbol.removesuffix("USDT")
        free = self.client.balances().get(base, 0.0)
        qty = round_step(min(qty, free), f["step_size"])
        if qty < f["min_qty"] or qty * stop < f["min_notional"]:
            raise BinanceError(f"position too small to protect on the exchange ({qty} {base})")
        stop_type = "STOP_LOSS" if "STOP_LOSS" in f["order_types"] else "STOP_LOSS_LIMIT"
        list_id = f"agent-{uuid.uuid4().hex[:16]}"
        try:
            resp = self.client.oco_sell(symbol, qty, take, stop, list_id, stop_type, stop * (1 - STOP_LIMIT_GAP))
        except requests.RequestException:
            time.sleep(2)  # network dropped: find out whether the pair exists before trying again
            resp = self.client.order_list(list_id)
        ids = {o["clientOrderId"]: o["orderId"] for o in resp["orders"]}
        return {"kind": "exchange", "list_client_id": list_id, "list_id": resp["orderListId"],
                "tp_order_id": ids.get(f"{list_id}-tp"), "sl_order_id": ids.get(f"{list_id}-sl"),
                "qty": qty, "stop": stop, "take": take, "stop_type": stop_type, "placed_at": _now_ms()}

    def protection_status(self, symbol: str, protection: dict) -> dict:
        """protected | executing (a leg is part-filled) | exited (a leg filled -> fill, reason) | gone."""
        legs = {}
        for leg in ("sl", "tp"):
            try:
                legs[leg] = self.client.get_order_by_id(symbol, protection[f"{leg}_order_id"])
            except BinanceError as e:
                if "does not exist" in str(e).lower() or "-2013" in str(e):
                    return {"state": "gone", "protection": protection, "detail": "order no longer exists"}
                raise
        for leg, o in legs.items():
            if o["status"] == "FILLED":
                fill = self._fill_from_order(symbol, "SELL", o, self._fills(symbol, o))
                return {"state": "exited", "reason": "stop-loss" if leg == "sl" else "take-profit",
                        "fill": fill, "protection": protection}
        if any(o["status"] == "PARTIALLY_FILLED" for o in legs.values()):
            return {"state": "executing", "protection": protection}
        if all(o["status"] in ("NEW", "PENDING_NEW") for o in legs.values()):
            return {"state": "protected", "protection": protection}
        detail = ", ".join(f"{leg} {o['status']}" for leg, o in legs.items())
        return {"state": "gone", "protection": protection, "detail": detail}

    def _fills(self, symbol: str, order: dict) -> list[dict]:
        """Fee details of an order that filled while we were not watching (not in the order record)."""
        try:
            return [{"price": t["price"], "qty": t["qty"], "commission": t["commission"],
                     "commissionAsset": t["commissionAsset"]} for t in self.client.my_trades(symbol, order["orderId"])]
        except (BinanceError, requests.RequestException) as e:
            log.warning("Could not read fees for order %s: %s - assuming %.1f%%", order["orderId"], e, PAPER_FEE * 100)
            return [{"price": "0", "qty": order["executedQty"], "commissionAsset": "USDT",
                     "commission": str(float(order["cummulativeQuoteQty"]) * PAPER_FEE)}]

    def unprotect(self, symbol: str, protection: dict) -> dict:
        """Cancel the pair so the coins are free to sell. If a leg already filled, report that exit instead."""
        try:
            self.client.cancel_order_list(symbol, protection["list_client_id"])
        except BinanceError as e:
            status = self.protection_status(symbol, protection)
            if status["state"] in ("exited", "gone"):
                return status
            raise BinanceError(f"could not cancel the exchange stop orders: {e}")
        return {"state": "cancelled"}


class ExecutionAgent:
    name = "execution"

    def __init__(self, broker):
        self.broker = broker

    def execute(self, side: str, symbol: str, *, quote_amount: float = 0, qty: float = 0) -> dict:
        try:
            fill = self.broker.buy(symbol, quote_amount) if side == "BUY" else self.broker.sell(symbol, qty)
            if fill["status"] != "FILLED" or fill["qty"] <= 0:
                return {"ok": False, "error": f"not filled ({fill['status']})"}
            return {"ok": True, **fill}
        except (BinanceError, requests.RequestException) as e:
            log.error("%s %s failed: %s", side, symbol, e)
            return {"ok": False, "error": str(e)}

    @property
    def can_protect(self) -> bool:
        return hasattr(self.broker, "protect")

    def protect(self, symbol: str, qty: float, stop: float, take: float) -> dict:
        """Returns {"ok": True, "protection": {...}} or {"ok": False, "error": "..."}."""
        try:
            return {"ok": True, "protection": self.broker.protect(symbol, qty, stop, take)}
        except (BinanceError, requests.RequestException, KeyError) as e:
            log.warning("Could not protect %s on the exchange: %s", symbol, e)
            return {"ok": False, "error": str(e)}

    def protection_status(self, symbol: str, protection: dict) -> dict:
        try:
            return self.broker.protection_status(symbol, protection)
        except (BinanceError, requests.RequestException) as e:
            log.warning("Could not check protection of %s: %s", symbol, e)
            return {"state": "unknown", "protection": protection, "detail": str(e)}

    def unprotect(self, symbol: str, protection: dict) -> dict:
        try:
            return self.broker.unprotect(symbol, protection)
        except (BinanceError, requests.RequestException) as e:
            log.error("Could not cancel protection of %s: %s", symbol, e)
            return {"state": "error", "detail": str(e)}
