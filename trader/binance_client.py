"""Minimal Binance Spot REST client (public market data + signed testnet orders)."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import requests


class BinanceError(RuntimeError):
    pass


def scrub(text) -> str:
    """Error texts quote the request URL; drop the request signature so logs stay shareable.
    (The API key travels in a header and the secret never leaves this machine.)"""
    return re.sub(r"signature=[0-9a-fA-F]+", "signature=<redacted>", str(text))


class BinanceClient:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "", timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.session = requests.Session()
        self._filters: dict[str, dict] = {}

    # ---- low level -----------------------------------------------------
    def sign(self, params: dict) -> str:
        query = urlencode(params)
        sig = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        headers = {}
        try:
            if signed:
                if not (self.api_key and self.api_secret):
                    raise BinanceError("Testnet API key/secret missing - set them in .env")
                params["timestamp"] = int(time.time() * 1000)
                params.setdefault("recvWindow", 5000)
                headers["X-MBX-APIKEY"] = self.api_key
                url = f"{self.base_url}{path}?{self.sign(params)}"
                resp = self.session.request(method, url, headers=headers, timeout=self.timeout)
            else:
                resp = self.session.request(method, f"{self.base_url}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as e:  # keep the type (callers rely on it), drop the signature
            raise requests.RequestException(scrub(e)) from None
        try:
            data = resp.json()
        except ValueError:
            raise BinanceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200 or (isinstance(data, dict) and data.get("code") not in (None, 200)):
            msg = data.get("msg") if isinstance(data, dict) else data
            raise BinanceError(f"HTTP {resp.status_code}: {msg}")
        return data

    # ---- market data ---------------------------------------------------
    def klines(self, symbol: str, interval: str = "1h", limit: int = 200, start_ms: int | None = None) -> list[dict]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms:
            params["startTime"] = start_ms
        rows = self._request("GET", "/api/v3/klines", params)
        return [
            {"time": r[0], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
             "close": float(r[4]), "volume": float(r[5])}
            for r in rows
        ]

    def price(self, symbol: str) -> float:
        return float(self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def all_prices(self) -> dict[str, float]:
        return {r["symbol"]: float(r["price"]) for r in self._request("GET", "/api/v3/ticker/price")}

    def tickers_24h(self) -> list[dict]:
        """24-hour summary of every trading pair, in one request."""
        rows = self._request("GET", "/api/v3/ticker/24hr", {"type": "MINI"})
        return [{"symbol": r["symbol"], "price": float(r["lastPrice"]), "open": float(r["openPrice"]),
                 "high": float(r["highPrice"]), "low": float(r["lowPrice"]), "quote_volume": float(r["quoteVolume"])}
                for r in rows]

    def symbol_filters(self, symbol: str) -> dict:
        if symbol not in self._filters:
            info = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
            sym = info["symbols"][0]
            f = {x["filterType"]: x for x in sym["filters"]}
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            side = f.get("PERCENT_PRICE_BY_SIDE") or {}
            plain = f.get("PERCENT_PRICE") or {}
            self._filters[symbol] = {
                "step_size": f.get("LOT_SIZE", {}).get("stepSize", "0.00000100"),
                "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", 0)),
                "min_notional": float(notional.get("minNotional", 5)),
                "tick_size": f.get("PRICE_FILTER", {}).get("tickSize", "0.00000001"),
                "oco_allowed": bool(sym.get("ocoAllowed", False)),
                "order_types": list(sym.get("orderTypes", [])),
                # a sell limit must lie within these multiples of the exchange's recent average price (0 = no rule)
                "ask_up": float(side.get("askMultiplierUp") or plain.get("multiplierUp") or 0),
                "ask_down": float(side.get("askMultiplierDown") or plain.get("multiplierDown") or 0),
            }
        return self._filters[symbol]

    def avg_price(self, symbol: str) -> float:
        """The exchange's average price of the last few minutes, which its percent-price rules judge orders by."""
        return float(self._request("GET", "/api/v3/avgPrice", {"symbol": symbol})["price"])

    # ---- account / orders (signed) -------------------------------------
    def balances(self) -> dict[str, float]:
        acct = self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in acct["balances"] if float(b["free"]) > 0}

    def balances_total(self) -> dict[str, float]:
        """free + locked (coins reserved by our open protective orders still belong to us)."""
        acct = self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acct["balances"]
                if float(b["free"]) + float(b["locked"]) > 0}

    def market_order(self, symbol: str, side: str, quantity: float, client_id: str) -> dict:
        return self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": format_qty(quantity, self.symbol_filters(symbol)["step_size"]),
            "newClientOrderId": client_id, "newOrderRespType": "FULL",
        }, signed=True)

    def get_order(self, symbol: str, client_id: str) -> dict:
        return self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def get_order_by_id(self, symbol: str, order_id: int | str) -> dict:
        return self._request("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def my_trades(self, symbol: str, order_id: int | str | None = None, limit: int = 500) -> list[dict]:
        """The individual fills of one order (needed for exact fees on orders that fill later), or without an order
        id the account's most recent fills on the pair (to find sales the desk did not see happen)."""
        params = {"symbol": symbol, "orderId": order_id} if order_id is not None else {"symbol": symbol, "limit": limit}
        return self._request("GET", "/api/v3/myTrades", params, signed=True)

    # ---- protective orders: one-cancels-the-other (OCO) sell pair ---------
    def oco_sell(self, symbol: str, quantity: float, take_profit: float, stop_loss: float, list_client_id: str,
                 stop_type: str = "STOP_LOSS", stop_limit: float | None = None) -> dict:
        """Place a take-profit (limit) and a stop-loss on the exchange; when one fills the other is cancelled.

        Binance requires: take-profit price > last price > stop price. Fails fast with a
        clear BinanceError when the market has already moved past either level.
        """
        f = self.symbol_filters(symbol)
        params = {
            "symbol": symbol, "side": "SELL", "quantity": format_qty(quantity, f["step_size"]),
            "listClientOrderId": list_client_id, "newOrderRespType": "FULL",
            "aboveType": "LIMIT_MAKER", "abovePrice": format_price(take_profit, f["tick_size"]),
            "aboveClientOrderId": f"{list_client_id}-tp",
            "belowType": stop_type, "belowStopPrice": format_price(stop_loss, f["tick_size"]),
            "belowClientOrderId": f"{list_client_id}-sl",
        }
        if stop_type == "STOP_LOSS_LIMIT":
            params["belowPrice"] = format_price(stop_limit if stop_limit else stop_loss, f["tick_size"])
            params["belowTimeInForce"] = "GTC"
        return self._request("POST", "/api/v3/orderList/oco", params, signed=True)

    def order_list(self, list_client_id: str) -> dict:
        return self._request("GET", "/api/v3/orderList", {"origClientOrderId": list_client_id}, signed=True)

    def cancel_order_list(self, symbol: str, list_client_id: str) -> dict:
        return self._request("DELETE", "/api/v3/orderList", {"symbol": symbol, "listClientOrderId": list_client_id},
                             signed=True)


def round_step(qty: float, step_size: str) -> float:
    """Round a quantity DOWN to the exchange's allowed step (e.g. 0.00001 BTC)."""
    step = Decimal(step_size)
    return float((Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step)


def format_qty(qty: float, step_size: str) -> str:
    step = Decimal(step_size).normalize()
    places = max(0, -step.as_tuple().exponent)
    return f"{round_step(qty, step_size):.{places}f}"


def format_price(price: float, tick_size: str) -> str:
    """Round a price to the exchange's allowed tick (e.g. 0.01 USDT for BTC)."""
    tick = Decimal(tick_size).normalize()
    places = max(0, -tick.as_tuple().exponent)
    rounded = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return f"{rounded:.{places}f}"
