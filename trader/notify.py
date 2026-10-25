"""Telegram alerts: trade notifications, failures, a daily report, and phone commands.

Only the owner's chat (TELEGRAM_CHAT_ID) gets messages or answers; anyone else
who finds the bot is ignored. Messages are plain text (no markdown) so AI
reasoning text can never break the formatting.
"""
from __future__ import annotations

import inspect
import logging
import re
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

    def redact(self, text) -> str:
        """Network errors quote the request URL, which contains the bot token: never let it reach the logs."""
        return str(text).replace(self.token, "<bot-token>") if self.token else str(text)

    def call(self, method: str, timeout: float = 15, **params) -> dict:
        try:
            r = requests.post(API.format(token=self.token, method=method), json=params, timeout=timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"Telegram unreachable: {self.redact(e)}") from None
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
            log.warning("Telegram send failed: %s", self.redact(e))
            return False

    def send_long(self, text: str) -> bool:
        """A long text (a review, a report) as several messages, split on line breaks."""
        ok, chunk, room = True, "", MAX_LEN - 100
        for line in text.splitlines(keepends=True):
            while len(line) > room:
                if chunk:
                    ok &= self.send(chunk)
                    chunk = ""
                ok &= self.send(line[:room])
                line = line[room:]
            if len(chunk) + len(line) > room:
                ok &= self.send(chunk)
                chunk = ""
            chunk += line
        if chunk.strip():
            ok &= self.send(chunk)
        return ok

    def send_once(self, key: str, text: str, every_hours: float = 6) -> bool:
        """Send, but not more than once per `every_hours` for the same key (avoids alert floods)."""
        if time.time() - self._last_sent.get(key, 0) < every_hours * 3600:
            return False
        self._last_sent[key] = time.time()
        return self.send(text)

    # ---- setup helpers ----
    def bot_username(self) -> str:
        return self.call("getMe")["username"]

    def latest_chat_id(self, code: str) -> str | None:
        """Chat id of whoever sent the pairing `code` to the bot (used once, during setup).
        Matching on a code, not on "the latest message", stops a stranger who finds the bot
        at the wrong moment from becoming its owner."""
        updates = self.call("getUpdates", timeout=15)
        for upd in reversed(updates):
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") == "private" and (msg.get("text") or "").strip() == code:
                return str(chat["id"])
        return None

    def send_document(self, path, caption: str = "") -> bool:
        """Upload a file (used for the weekly journal backup)."""
        if not self.enabled:
            return False
        try:
            with open(path, "rb") as fh:
                r = requests.post(API.format(token=self.token, method="sendDocument"),
                                  data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                  files={"document": fh}, timeout=120)
            if not r.json().get("ok"):
                raise RuntimeError(r.json().get("description", f"HTTP {r.status_code}"))
            return True
        except (requests.RequestException, ValueError, RuntimeError, OSError) as e:
            log.warning("Telegram upload failed: %s", self.redact(e))
            return False

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
            log.warning("Telegram listener start failed: %s", self.redact(e))
        while True:
            try:
                offset = self.handle(self._get_updates(offset), handlers, offset)
            except Exception as e:
                log.warning("Telegram polling error: %s", self.redact(e))
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
                arg = " ".join(words[1:])
                reply = handler(arg) if inspect.signature(handler).parameters else handler()
            except Exception as e:
                log.exception("Telegram command %s failed", cmd)
                reply = f"Sorry, that command failed: {e}"
            self.send(reply)
        return offset

    def _get_updates(self, offset: int | None) -> list[dict]:
        # Long polling: Telegram holds the request up to 50s until a message arrives.
        try:
            r = requests.post(API.format(token=self.token, method="getUpdates"),
                              json={"offset": offset, "timeout": 50, "allowed_updates": ["message"]}, timeout=65)
        except requests.RequestException as e:
            raise RuntimeError(f"Telegram unreachable: {self.redact(e)}") from None
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
        return data["result"]


def money(v: float) -> str:
    return f"${v:,.2f}"


_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text, limit: int = 400) -> str:
    """AI-written text (reasons, summaries) is built from headlines and web pages we do not control.
    Before it reaches the owner's phone: no links (phishing), no control characters, bounded length."""
    t = _CTRL.sub("", _URL.sub("[link removed]", str(text or "")))
    return t if len(t) <= limit else t[:limit - 1] + "…"


def trade_messages(summary: list[dict]) -> list[tuple[str, str]]:
    """Turn one cycle's results into (dedupe_key, message) alerts. Empty key = always send."""
    out = []
    for r in summary:
        sym = r["symbol"]
        if "error" in r:
            what = "Market scout" if sym == "SCOUT" else sym
            out.append((f"error:{sym}", f"⚠️ {what}: {r['error']}\n(The desk keeps running with the coins it has "
                                        f"and retries. This alert repeats at most every 6 hours.)"))
            continue
        if err := r.get("protect_error"):
            out.append((f"protect:{sym}", f"🛡️ Could not place the stop-loss / take-profit for {sym} on Binance: {err}\n"
                                          f"The desk checks this coin every 5 minutes itself meanwhile and keeps "
                                          f"retrying. (Repeats at most every 6 hours.)"))
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
            shield = {"exchange": " · 🛡️ both placed on Binance", "emulated": " · 🛡️ simulated exchange orders"}.get(
                r.get("protected"), "")
            mentor = f"\nMaster traders' checklist:\n{r['mentor']}" if r.get("mentor") else ""
            out.append(("", f"🟢 BUY {sym}\n{o['qty']:.6g} at {money(o['avg_price'])} = {money(o['quote'])}\n"
                            f"Stop-loss {money(r['stop_loss'])} · Take-profit {money(r['take_profit'])}{shield}\n"
                            f"Confidence {r['confidence']:.0%}. Why: {clean(r['reasoning'])}{mentor}"))
        else:
            t = r.get("trade") or {}
            result = f"{'✅ Profit' if t.get('pnl', 0) > 0 else '🔻 Loss'} {t.get('pnl', 0):+,.2f} USDT " \
                     f"({t.get('pnl_pct', 0):+.2f}%)" if t else ""
            why = {"stop-loss": "stop-loss hit (limits the loss)", "take-profit": "take-profit target reached",
                   "signal": "the agents' signal turned negative",
                   "stop-loss (exchange)": "stop-loss order fired on Binance (instant protection)",
                   "take-profit (exchange)": "take-profit order filled on Binance",
                   "stop-loss (emulated)": "stop-loss hit between checks (simulated exchange order)",
                   "take-profit (emulated)": "take-profit hit between checks (simulated exchange order)",
                   }.get(r.get("reason"), r.get("reason", ""))
            out.append(("", f"🔴 SELL {sym}\n{o['qty']:.6g} at {money(o['avg_price'])} = {money(o['quote'])}\n"
                            f"Reason: {why}\n{result}"))
    return out


def _coin(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def scout_message(u: dict) -> str:
    """Alert when the Market Scout changes the watchlist."""
    how = "AI scout with web research" if u.get("source") == "claude" else "market scan"
    lines = [f"🔭 Watchlist updated ({how})"]
    if u.get("summary"):
        lines.append(clean(u["summary"]))
    lines += [f"➕ {_coin(s)}: {clean(u['reasons'].get(s, ''), 200)}" for s in u["added"]]
    lines += [f"➖ {_coin(s)} (no longer watched)" for s in u["removed"]]
    if u.get("avoid"):
        lines.append("Avoiding: " + "; ".join(f"{_coin(k)} ({clean(v, 120)})" for k, v in list(u["avoid"].items())[:3]))
    lines.append("Now watching: " + ", ".join(_coin(s) for s in u["symbols"]))
    return "\n".join(lines)


def pace_message(pace: dict) -> str:
    who = {"AI": "the AI", "risk": "the risk rules", "settings": "the normal setting"}.get(pace["source"], pace["source"])
    return f"⏱️ Now checking every {pace['minutes']} min (set by {who}): {clean(pace['reason'], 200)}"
