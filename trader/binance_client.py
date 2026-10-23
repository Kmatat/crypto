"""Minimal Binance Spot REST client (public market data + signed testnet orders)."""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import requests


class BinanceError(RuntimeError):
    pass


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
        try:
            data = resp.json()
        except ValueError:
            raise BinanceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200 or (isinstance(data, dict) and data.get("code") not in (None, 200)):
            msg = data.get("msg") if isinstance(data, dict) else data
            raise BinanceError(f"HTTP {resp.status_code}: {msg}")
        return data

    # ---- market data ---------------------------------------------------
    def klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
        rows = self._request("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        return [
            {"time": r[0], "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
             "close": float(r[4]), "volume": float(r[5])}
            for r in rows
        ]

    def price(self, symbol: str) -> float:
        return float(self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def symbol_filters(self, symbol: str) -> dict:
        if symbol not in self._filters:
            info = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
            f = {x["filterType"]: x for x in info["symbols"][0]["filters"]}
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[symbol] = {
                "step_size": f.get("LOT_SIZE", {}).get("stepSize", "0.00000100"),
                "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", 0)),
                "min_notional": float(notional.get("minNotional", 5)),
            }
        return self._filters[symbol]

    # ---- account / orders (signed) -------------------------------------
    def balances(self) -> dict[str, float]:
        acct = self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in acct["balances"] if float(b["free"]) > 0}

    def market_order(self, symbol: str, side: str, quantity: float, client_id: str) -> dict:
        return self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": format_qty(quantity, self.symbol_filters(symbol)["step_size"]),
            "newClientOrderId": client_id, "newOrderRespType": "FULL",
        }, signed=True)

    def get_order(self, symbol: str, client_id: str) -> dict:
        return self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)


def round_step(qty: float, step_size: str) -> float:
    """Round a quantity DOWN to the exchange's allowed step (e.g. 0.00001 BTC)."""
    step = Decimal(step_size)
    return float((Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step)


def format_qty(qty: float, step_size: str) -> str:
    step = Decimal(step_size).normalize()
    places = max(0, -step.as_tuple().exponent)
    return f"{round_step(qty, step_size):.{places}f}"
