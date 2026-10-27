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
RECENT_SECONDS = 900  # a command sent while the desk restarted is still answered if it is this recent


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

    def send(self, text: str, silent: bool = False) -> bool:
        """`silent`: delivered without a sound (the daily trend summary arrives at night for an owner in Europe or the
        Gulf)."""
        if not self.enabled:
            return False
        try:
            quiet = {"disable_notification": True} if silent else {}
            self.call("sendMessage", chat_id=self.chat_id, text=text[:MAX_LEN], disable_web_page_preview=True, **quiet)
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

    def catch_up(self, handlers: dict[str, Callable[[], str]]) -> int | None:
        """At start: commands sent while the desk was restarting are answered if recent; old ones are skipped
        (a week-old /update or /lab should not run now). Returns the offset to continue from."""
        offset = None
        try:
            pending = self.call("getUpdates", timeout=0)
            recent = [u for u in pending if time.time() - (u.get("message") or {}).get("date", 0) <= RECENT_SECONDS]
            offset = pending[-1]["update_id"] + 1 if pending else None
            if recent:
                self.handle(recent, handlers, offset)
        except Exception as e:
            log.warning("Telegram listener start failed: %s", self.redact(e))
        return offset

    def _poll(self, handlers: dict[str, Callable[[], str]]) -> None:
        offset = self.catch_up(handlers)
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


def price_text(v: float) -> str:
    """A coin price for the owner: cents from $1 up, four significant digits below (a $0.15 coin's stop is not $0.11)."""
    return money(v) if v >= 1 else f"${v:.4g}"


TREND_REENTRY = "The trend rule buys it again only after a daily close at or below its average and a new close above it."


def trade_messages(summary: list[dict], trend: bool = False) -> list[tuple[str, str]]:
    """Turn one cycle's results into (dedupe_key, message) alerts. Empty key = always send. `trend`: the desk trades
    the daily trend rule (a stop-loss exit then says when the coin is bought again). A result that cannot be written
    up is logged, and an order among them still gets a short alert: a message must never stop the desk."""
    out = []
    # a position the trend rule takes over may have its take-profit raised earlier in the same check, still with the
    # hourly stop: the takeover's message gives the final orders, so that raise is not announced separately
    adopted = {r.get("symbol") for r in summary if isinstance(r, dict) and r.get("action") == "ADOPT"}
    for r in summary:
        try:
            if r.get("action") == "RAISE" and r.get("symbol") in adopted:
                continue
            out += _messages(r, trend)
        except Exception:  # noqa: BLE001 - an unexpected row (a new kind, a missing field) must not crash the loop
            sym = r.get("symbol", "?") if isinstance(r, dict) else "?"
            log.exception("Could not write the alert for %s", sym)
            if isinstance(r, dict) and r.get("order"):   # the owner hears of every order, even without the details
                out.append(("", f"ℹ️ {r.get('action') or 'Order'} {sym}: see /status for the details "
                                "(this alert could not be written)"))
    return out


def _messages(r: dict, trend: bool) -> list[tuple[str, str]]:
    """The alerts for one result of a check (see trade_messages)."""
    out = []
    sym = r.get("symbol", "?")
    if "error" in r:
        if sym == "TREND":   # the day's trend decisions gave up: the next try is with tomorrow's decisions
            out.append((f"error:{sym}", f"⚠️ Daily trend decisions: {r['error']}\n(Open positions keep their emergency "
                                        f"stop. This alert repeats at most every 6 hours.)"))
            return out
        what = "Market scout" if sym == "SCOUT" else sym
        out.append((f"error:{sym}", f"⚠️ {what}: {r['error']}\n(The desk keeps running with the coins it has "
                                    f"and retries. This alert repeats at most every 6 hours.)"))
        return out
    if r.get("action") == "ADOPT":   # the daily trend rule takes over a position the hourly strategy bought
        below = 1 - r["stop_loss"] / r["entry_price"]
        out.append(("", f"🔁 {_coin(sym)}: the position bought by the hourly strategy now follows the daily trend rule: "
                        f"emergency stop moved from {price_text(r['old_stop'])} to {price_text(r['stop_loss'])} "
                        f"({below:.0%} below the entry), held while it closes above its {r.get('sma_days', 50)}-day "
                        "average"))
        return out
    if r.get("action") == "RAISE":   # the trend rule's take-profit order moved up as the price rose
        out.append(("", f"⬆️ {_coin(sym)} is {r['price'] / r['entry_price'] - 1:+.0%} since the buy: its take-profit "
                        f"order moved up to {price_text(r['take_profit'])} so the trend is not cut short (emergency "
                        f"stop unchanged at {price_text(r['stop_loss'])})"))
        return out
    if err := r.get("protect_error"):
        out.append((f"protect:{sym}", f"🛡️ Could not place the stop-loss / take-profit for {sym} on Binance: {err}\n"
                                      f"The desk checks this coin every 5 minutes itself meanwhile and keeps "
                                      f"retrying. (Repeats at most every 6 hours.)"))
    o = r.get("order")
    if not o:
        return out
    if not o.get("ok"):
        error = str(o.get("error") or "no reason given")
        hint = ""
        if "API-key" in error or "Signature" in error:
            hint = "\nYour testnet keys may have been reset by Binance. On the server run: desk setup"
        elif "insufficient balance" in error.lower():
            hint = "\nThe testnet account is short of funds (Binance resets testnet balances from time to time)."
        out.append((f"order:{sym}:{r.get('action')}", f"❌ {r.get('action')} {sym} FAILED: {error}{hint}"))
    elif r.get("action") == "BUY":
        shield = {"exchange": " · 🛡️ both placed on Binance", "emulated": " · 🛡️ simulated exchange orders"}.get(
            r.get("protected"), "")
        mentor = f"\nMaster traders' checklist:\n{r['mentor']}" if r.get("mentor") else ""
        if r.get("source") == "trend":   # the daily trend rule: an emergency stop, no profit target
            out.append(("", f"🟢 BUY {sym} (daily trend rule)\n{o['qty']:.6g} at {money(o['avg_price'])} = "
                            f"{money(o['quote'])}\nEmergency stop {money(r['stop_loss'])}{shield}; no profit target "
                            f"(the take-profit order moves up with the price)\nWhy: {clean(r['reasoning'])}"))
            return out
        out.append(("", f"🟢 BUY {sym}\n{o['qty']:.6g} at {money(o['avg_price'])} = {money(o['quote'])}\n"
                        f"Stop-loss {money(r['stop_loss'])} · Take-profit {money(r['take_profit'])}{shield}\n"
                        f"Confidence {r['confidence']:.0%}. Why: {clean(r['reasoning'])}{mentor}"))
    else:
        t = r.get("trade") or {}
        result = f"{'✅ Profit' if t.get('pnl', 0) > 0 else '🔻 Loss'} {t.get('pnl', 0):+,.2f} USDT " \
                 f"({t.get('pnl_pct', 0):+.2f}%)" if t else ""
        why = str({"stop-loss": "stop-loss hit (limits the loss)", "take-profit": "take-profit target reached",
                   "signal": "the agents' signal turned negative",
                   "stop-loss (exchange)": "stop-loss order fired on Binance (instant protection)",
                   "take-profit (exchange)": "take-profit order filled on Binance",
                   "stop-loss (emulated)": "stop-loss hit between checks (simulated exchange order)",
                   "take-profit (emulated)": "take-profit hit between checks (simulated exchange order)",
                   }.get(r.get("reason"), r.get("reason")) or "")
        if why.startswith("trend: "):
            why = "daily trend rule: " + why[len("trend: "):]
        text = f"🔴 SELL {sym}\n{o['qty']:.6g} at {money(o['avg_price'])} = {money(o['quote'])}\nReason: {why}\n{result}"
        if trend and str(r.get("reason") or "").startswith("stop-loss"):
            text += f"\n{TREND_REENTRY}"
        out.append(("", text))
    return out


def trend_lines(coins: dict[str, dict], sma_days: int, positions: dict | None = None, actions: bool = True) -> list[str]:
    """The daily trend rule's decisions in a few short lines: what it bought and sold (`actions`), what it holds with
    each close's distance to its average, what stays out, what waits after an emergency stop, and the buys it could
    not make. `coins` is a trend report's {coin: {action, why, approved, risk, gap}}; `positions` the desk's holdings
    now, so an order that failed is not shown as done."""
    def names(syms):
        return ", ".join(_coin(x) for x in syms)

    def gaps(syms):
        return ", ".join(f"{_coin(x)} {coins[x]['gap']:+.1%}" if coins[x].get("gap") is not None else _coin(x)
                         for x in syms)

    def held(sym):
        return positions is None or sym in positions
    bought, buy_failed, sold, sell_failed, holding, out, young, waiting, refused = ([] for _ in range(9))
    for sym, d in coins.items():
        action, risk = d.get("action"), str(d.get("risk") or "")
        if action == "BUY" and d.get("approved"):
            (bought if held(sym) else buy_failed).append(sym)
            if held(sym):
                holding.append(sym)
        elif action == "BUY":
            refused.append(f"{_coin(sym)} ({clean(risk or 'refused', 120)})")
        elif action == "SELL":
            (sell_failed if positions is not None and sym in positions else sold).append(sym)
        elif risk == "holding":
            if held(sym):
                holding.append(sym)
        elif risk == "no buy signal":
            (out if d.get("gap") is not None else young).append(sym)
        elif risk == "re-entry rule":
            waiting.append(sym)
        elif risk == "demo exchange price":
            refused.append(f"{_coin(sym)} (its price on the Binance demo exchange is far from the real market)")
        else:
            refused.append(f"{_coin(sym)} ({clean(risk or d.get('why') or 'not bought', 120)})")
    lines = []
    if actions and bought:
        lines.append(f"🟢 Bought: {names(bought)}")
    if actions and buy_failed:
        lines.append(f"❌ Buy order failed: {names(buy_failed)} (see the order message)")
    if actions and sold:
        lines.append("🔴 Sold: " + "; ".join(f"{_coin(x)} ({clean(coins[x].get('why') or 'sold', 120)})" for x in sold))
    if actions and sell_failed:
        lines.append(f"⚠️ Sale failed, still held: {names(sell_failed)} (see the order message)")
    if holding:
        lines.append(f"Holding, close vs its {sma_days}-day average: {gaps(holding)}")
    if out:
        lines.append(f"Out, closed at or below its average: {gaps(out)}")
    if young:
        lines.append(f"Not enough daily history yet: {names(young)}")
    if waiting:
        lines.append(f"Waiting after an emergency stop: {names(waiting)} (bought again after a close at or below its "
                     "average, then a close above)")
    if refused:
        lines.append("Not bought: " + "; ".join(refused))
    return lines


def trend_message(rep: dict, ctx=None) -> str:
    """The day's decisions of the daily trend rule in one short message, sent once a day after 00:00 UTC. `rep` is the
    desk's trend report (Orchestrator.trend_report); `ctx` the desk (its holdings now are then shown) or its Settings."""
    from .trend import TESTED, params
    s = getattr(ctx, "base", ctx)
    journal = getattr(ctx, "journal", None)
    positions = journal.positions() if journal is not None else None
    sma_days = params(s)[0] if s is not None else TESTED[0]
    coins = rep.get("coins") or {}
    lines = [f"📈 Daily trend decisions (closes of {rep.get('day') or 'yesterday'})"]
    lines += trend_lines(coins, sma_days, positions)
    if rep.get("adopted"):
        lines.append("🔁 Now following the rule: " + ", ".join(_coin(x) for x in rep["adopted"])
                     + " (bought by the hourly strategy)")
    missing = [x for x in (getattr(s, "symbols", None) or []) if x not in coins]
    if missing:
        lines.append("No decision (its prices could not be read): " + ", ".join(_coin(x) for x in missing))
    if positions is not None:
        held = len(positions)
    else:
        held = sum(1 for d in coins.values() if (d.get("action") == "HOLD" and d.get("risk") == "holding")
                   or (d.get("action") == "BUY" and d.get("approved")))
    lines.append(f"{held} of {rep.get('slots', '?')} slots used")
    if not rep.get("complete", True):
        lines.append(f"⚠️ Not complete after {rep.get('tries', '?')} tries (see the error messages): what could not be "
                     "done waits for tomorrow's decisions.")
    return "\n".join(lines)


def _coin(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def scout_message(u: dict) -> str:
    """Alert when the Market Scout changes the watchlist."""
    how = "AI scout with web research" if u.get("source") == "claude" and u.get("web", True) else \
        "free AI scout using Claude's web research" if str(u.get("source", "")).startswith("ai:") and u.get("web") else \
        "AI scout (market data only)" if str(u.get("source", "")).startswith(("ai:", "claude")) else "market scan"
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
