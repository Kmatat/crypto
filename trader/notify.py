"""Telegram alerts: trade notifications, failures, a daily report, and phone commands.

Only the owner's chat (TELEGRAM_CHAT_ID) gets messages or answers; anyone else
who finds the bot is ignored. Messages are plain text (no markdown) so AI
reasoning text can never break the formatting.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # Telegram's limit is 4096 characters


class Telegram:
    def __init__(self, token: str, chat_id: str = ""):
        self.token = token
        self.chat_id = str(chat_id)
        self._last_sent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def call(self, method: str, timeout: float = 15, **params) -> dict:
        r = requests.post(API.format(token=self.token, method=method), json=params, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
        return data["result"]

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            self.call("sendMessage", chat_id=self.chat_id, text=text[:MAX_LEN], disable_web_page_preview=True)
            return True
        except (requests.RequestException, ValueError, RuntimeError) as e:
            log.warning("Telegram send failed: %s", e)
            return False

    def send_once(self, key: str, text: str, every_hours: float = 6) -> bool:
        """Send, but not more than once per `every_hours` for the same key (avoids alert floods)."""
        if time.time() - self._last_sent.get(key, 0) < every_hours * 3600:
            return False
        self._last_sent[key] = time.time()
        return self.send(text)

    # ---- setup helpers ----
    def bot_username(self) -> str:
        return self.call("getMe")["username"]

    def latest_chat_id(self) -> str | None:
        """Chat id of the most recent person who messaged the bot (used once, during setup)."""
        updates = self.call("getUpdates", timeout=15)
        for upd in reversed(updates):
            chat = (upd.get("message") or {}).get("chat")
            if chat:
                return str(chat["id"])
        return None

    # ---- commands ----
    def listen(self, handlers: dict[str, Callable[[], str]]) -> threading.Thread:
        """Answer /commands from the owner in a background thread."""
        t = threading.Thread(target=self._poll, args=(handlers,), daemon=True, name="telegram")
        t.start()
        return t

    def _poll(self, handlers: dict[str, Callable[[], str]]) -> None:
        offset = None
        try:  # skip commands sent while the desk was offline
            pending = self.call("getUpdates", offset=-1, timeout=15)
            offset = pending[-1]["update_id"] + 1 if pending else None
        except Exception as e:
            log.warning("Telegram listener start failed: %s", e)
        while True:
            try:
                offset = self.handle(self._get_updates(offset), handlers, offset)
            except Exception as e:
                log.warning("Telegram polling error: %s", e)
                time.sleep(15)

    def handle(self, updates: list[dict], handlers: dict[str, Callable[[], str]], offset: int | None) -> int | None:
        """Answer the owner's commands; returns the next update offset."""
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue  # not the owner
            words = (msg.get("text") or "").strip().split()
            cmd = words[0].split("@")[0].lower() if words else ""
            handler = handlers.get(cmd) or handlers["/help"]
            try:
                reply = handler()
            except Exception as e:
                log.exception("Telegram command %s failed", cmd)
                reply = f"Sorry, that command failed: {e}"
            self.send(reply)
        return offset

    def _get_updates(self, offset: int | None) -> list[dict]:
        # Long polling: Telegram holds the request up to 50s until a message arrives.
        r = requests.post(API.format(token=self.token, method="getUpdates"),
                          json={"offset": offset, "timeout": 50, "allowed_updates": ["message"]}, timeout=65)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
        return data["result"]


def money(v: float) -> str:
    return f"${v:,.2f}"


def trade_messages(summary: list[dict]) -> list[tuple[str, str]]:
    """Turn one cycle's results into (dedupe_key, message) alerts. Empty key = always send."""
    out = []
    for r in summary:
        sym = r["symbol"]
        if "error" in r:
            out.append((f"error:{sym}", f"⚠️ {sym}: the cycle failed - {r['error']}\n(The desk keeps running and "
                                        f"retries next cycle. This alert repeats at most every 6 hours.)"))
            continue
        o = r.get("order")
        if not o:
            continue
        if not o["ok"]:
            hint = ""
            if "API-key" in o["error"] or "Signature" in o["error"]:
                hint = "\nYour testnet keys may have been reset by Binance. On the server run: desk setup"
            elif "insufficient balance" in o["error"].lower():
                hint = "\nThe testnet account is short of funds (Binance resets testnet balances from time to time)."
            out.append((f"order:{sym}:{r['action']}", f"❌ {r['action']} {sym} FAILED: {o['error']}{hint}"))
        elif r["action"] == "BUY":
            out.append(("", f"🟢 BUY {sym}\n{o['qty']:.6g} at {money(o['avg_price'])} = {money(o['quote'])}\n"
                            f"Stop-loss {money(r['stop_loss'])} · Take-profit {money(r['take_profit'])}\n"
                            f"Confidence {r['confidence']:.0%}. Why: {r['reasoning']}"))
        else:
            t = r.get("trade") or {}
            result = f"{'✅ Profit' if t.get('pnl', 0) > 0 else '🔻 Loss'} {t.get('pnl', 0):+,.2f} USDT " \
                     f"({t.get('pnl_pct', 0):+.2f}%)" if t else ""
            why = {"stop-loss": "stop-loss hit (limits the loss)", "take-profit": "take-profit target reached",
                   "signal": "the agents' signal turned negative"}.get(r.get("reason"), r.get("reason", ""))
            out.append(("", f"🔴 SELL {sym}\n{o['qty']:.6g} at {money(o['avg_price'])} = {money(o['quote'])}\n"
                            f"Reason: {why}\n{result}"))
    return out
