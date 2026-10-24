"""Execution agent: turns an approved decision into an actual order and confirms it filled.

Two brokers share one interface:
  PaperBroker   - fills locally at the current market price (simulation mode)
  TestnetBroker - sends real MARKET orders to the Binance Spot Testnet (fake money)

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
PAPER_FEE = 0.001  # Binance standard spot fee 0.1%


class PaperBroker:
    def __init__(self, price_fn):
        self.price_fn = price_fn

    def buy(self, symbol: str, quote_amount: float) -> dict:
        price = self.price_fn(symbol)
        qty = quote_amount * (1 - PAPER_FEE) / price
        return {"status": "FILLED", "order_id": f"paper-{uuid.uuid4().hex[:10]}", "qty": qty,
                "avg_price": price, "quote": quote_amount, "fee_quote": quote_amount * PAPER_FEE}

    def sell(self, symbol: str, qty: float) -> dict:
        price = self.price_fn(symbol)
        gross = qty * price
        return {"status": "FILLED", "order_id": f"paper-{uuid.uuid4().hex[:10]}", "qty": qty,
                "avg_price": price, "quote": gross * (1 - PAPER_FEE), "fee_quote": gross * PAPER_FEE}


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
        exec_qty = float(resp["executedQty"])
        if exec_qty <= 0:
            raise BinanceError(f"order ended as {resp['status']} with nothing filled")
        quote = float(resp["cummulativeQuoteQty"])
        avg_price = quote / exec_qty
        base = symbol.removesuffix("USDT")
        fee_quote = 0.0
        for x in resp.get("fills", []):
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
            if o["status"] in ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"):
                break
        return o


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
