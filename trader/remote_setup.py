"""Settings from the phone: /setup on Telegram.

The desk runs in a sandbox that cannot change its own settings file (.env): that is on purpose, so nothing that
reaches the desk from outside can rewrite its configuration. The phone setup therefore works like /update: the desk
asks the owner a few questions on Telegram, writes the answers as a request into its data folder, and the update job
(which may change the app folder) checks every value again, saves the previous file as data/.env.previous, writes the
new settings and restarts the desk. The update job reports the result on Telegram.

Only everyday settings can be changed this way (strategy, coins, risk level, budget, trading mode, timing). API keys
and tokens never go through Telegram: a Telegram chat is stored on Telegram's servers, and the desk's rule is that it
never asks for a key in Telegram. Keys are changed on the server with `desk setup`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

from .trend import TESTED_COINS

REQUEST_NAME = "settings-request.json"
BACKUP_NAME = ".env.previous"
SESSION_MINUTES = 15          # an unanswered phone setup ends by itself
HOURLY_COINS = "BTCUSDT,ETHUSDT,SOLUSDT"
TREND_COINS = ",".join(TESTED_COINS)
ALLOWED = ("STRATEGY", "SYMBOLS", "MAX_OPEN_POSITIONS", "AUTO_COINS", "TREND_VOL_TARGET", "STARTING_BALANCE_USDT",
           "TRADING_MODE", "CYCLE_MINUTES", "DAILY_REPORT_HOUR_UTC", "PULSE_HOURS")
COIN = re.compile(r"^[A-Z0-9]{2,20}USDT$")
NAMES = {"STRATEGY": "Strategy", "SYMBOLS": "Coins", "MAX_OPEN_POSITIONS": "Position slots", "AUTO_COINS": "Coin scout",
         "TREND_VOL_TARGET": "Risk level", "STARTING_BALANCE_USDT": "Budget", "TRADING_MODE": "Trading mode",
         "CYCLE_MINUTES": "Check interval", "DAILY_REPORT_HOUR_UTC": "Daily report hour", "PULSE_HOURS": "Pulse"}


def _number(value: str) -> float | None:
    try:
        v = float(str(value).replace(",", ".").replace("$", "").strip())
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def check_value(key: str, value: str, current: dict) -> str | None:
    """Why `value` cannot be saved for `key` (None when it can). The update job checks every value again."""
    if key not in ALLOWED:
        return f"{key} cannot be changed from the phone"
    value = str(value).strip()
    n = _number(value)
    if key == "STRATEGY":
        return None if value in ("hourly", "trend") else "must be hourly or trend"
    if key == "SYMBOLS":
        coins = value.split(",")
        if not 1 <= len(coins) <= 20 or not all(COIN.match(c) for c in coins) or len(set(coins)) != len(coins):
            return "must be 1 to 20 different coins such as BTCUSDT,ETHUSDT"
        return None
    if key == "AUTO_COINS":
        return None if value in ("true", "false") else "must be true or false"
    if key == "TRADING_MODE":
        if value not in ("simulation", "testnet"):
            return "must be simulation or testnet (real money is not supported)"
        if value == "testnet" and not (current.get("BINANCE_TESTNET_API_KEY") and current.get("BINANCE_TESTNET_API_SECRET")):
            return "the demo account needs Binance demo keys, which are set on the server with desk setup"
        return None
    limits = {"MAX_OPEN_POSITIONS": (1, 20, True), "TREND_VOL_TARGET": (0.05, 2.0, False),
              "STARTING_BALANCE_USDT": (10.0, 10_000_000.0, False), "CYCLE_MINUTES": (5, 60, True),
              "DAILY_REPORT_HOUR_UTC": (0, 23, True), "PULSE_HOURS": (0, 48, False)}
    low, high, whole = limits[key]
    if n is None or not low <= n <= high or (whole and n != int(n)):
        return f"must be {'a whole number' if whole else 'a number'} from {low:g} to {high:g}"
    return None


def write_request(data_dir: Path, changes: dict[str, str]) -> Path:
    """The desk's half: the owner's confirmed changes, for the update job to check and apply."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / REQUEST_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"requested_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "by": "telegram", "changes": changes}))
    os.replace(tmp, path)
    return path


def apply_request(env_path: Path, data_dir: Path) -> tuple[str, bool] | None:
    """The update job's half: check the request, save the current settings as data/.env.previous, write the new ones.
    Returns (message for the owner, applied) or None when nothing was asked. The request is always consumed."""
    from dotenv import dotenv_values
    from .setup_wizard import write_env
    path = data_dir / REQUEST_NAME
    if not path.exists():
        return None
    try:
        request = json.loads(path.read_text())
        changes = {str(k): str(v) for k, v in (request.get("changes") or {}).items()}
    except (OSError, ValueError, AttributeError):
        path.unlink(missing_ok=True)
        return "⚠️ The settings sent from the phone could not be read; nothing was changed. Send /setup again.", False
    path.unlink(missing_ok=True)
    current = {k: v for k, v in dotenv_values(env_path).items() if v is not None} if env_path.exists() else {}
    problems = [f"{NAMES.get(k, k)}: {why}" for k, v in changes.items() if (why := check_value(k, v, current))]
    if problems or not changes:
        return ("⚠️ Settings from the phone not applied: " + ("; ".join(problems) or "nothing to change")
                + ". Nothing was changed."), False
    if env_path.exists():
        backup = data_dir / BACKUP_NAME
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(env_path.read_text())
    write_env(env_path, {**normalized(current), **changes})
    lines = [f"• {NAMES.get(k, k)}: {describe(k, current.get(k))} → {describe(k, v)}" for k, v in changes.items()]
    return ("✅ Settings changed from your phone:\n" + "\n".join(lines) + "\nThe desk restarts now (it finishes its "
            "current market check first). The previous settings are kept on the server as data/.env.previous."), True


def normalized(values: dict[str, str]) -> dict[str, str]:
    """The same corrections `desk setup` makes before it writes the file (an old file's check interval and take-profit
    defaults, then the current file format), so a file written from the phone loads exactly like one from the wizard."""
    from .config import SETTINGS_VERSION, check_minutes, legacy_take_profit
    out = dict(values)
    if out.get("CYCLE_MINUTES"):
        out["CYCLE_MINUTES"] = str(check_minutes(out["CYCLE_MINUTES"], out.get("SETTINGS_VERSION")))
    take, _ = legacy_take_profit(out.get("TAKE_PROFIT_ATR"), out.get("SETTINGS_VERSION"), out.get("MIN_REWARD_RISK"))
    if take is not None:
        out["TAKE_PROFIT_ATR"] = f"{take:.1f}"
    out["SETTINGS_VERSION"] = SETTINGS_VERSION
    return out


def describe(key: str, value: str | None) -> str:
    """A setting the way the owner reads it."""
    if value in (None, ""):
        return "not set"
    if key == "STRATEGY":
        return "daily trend rule" if value == "trend" else "hourly agents"
    if key == "SYMBOLS":
        return " ".join(c.removesuffix("USDT") for c in value.split(","))
    if key == "TREND_VOL_TARGET":
        n = _number(value)
        return "Normal" if n == 0.5 else "Calmer" if n == 0.25 else f"your own ({value})"
    if key == "TRADING_MODE":
        return "Binance demo account" if value == "testnet" else "paper trading"
    if key == "AUTO_COINS":
        return "on" if value == "true" else "off"
    if key == "STARTING_BALANCE_USDT":
        n = _number(value)
        return f"${n:,.0f}" if n is not None else value
    if key == "CYCLE_MINUTES":
        return f"every {value} min"
    if key == "DAILY_REPORT_HOUR_UTC":
        return f"{value}:00 UTC"
    if key == "PULSE_HOURS":
        return "off" if _number(value) == 0 else f"every {value} h"
    return value


MENU = [("1", "Strategy"), ("2", "Coins"), ("3", "Risk level"), ("4", "Budget"), ("5", "Trading mode"),
        ("6", "Check interval"), ("7", "Daily report hour"), ("8", "Pulse"), ("9", "Apply changes"), ("0", "Cancel")]


class PhoneSetup:
    """One /setup conversation. `answer(text)` returns (reply, buttons); `changes` holds what the owner confirmed
    once `done`. `check_symbol(coin)` and `demo_warnings(coins)` reach Binance (tests replace them)."""

    def __init__(self, current: dict[str, str], check_symbol=None, demo_warnings=None, clock=time.time):
        self.current = {k: v for k, v in current.items() if v is not None}
        self.pending: dict[str, str] = {}
        self.state = "menu"
        self.done = self.cancelled = False
        self.check_symbol = check_symbol or (lambda coin: True)
        self.demo_warnings = demo_warnings or (lambda coins: {})
        self.clock = clock
        self.touched = clock()

    # ---- helpers ----
    def value(self, key: str, default: str = "") -> str:
        return self.pending.get(key, self.current.get(key, default))

    @property
    def expired(self) -> bool:
        return self.clock() - self.touched > SESSION_MINUTES * 60

    def _set(self, key: str, value: str) -> None:
        if self.current.get(key) == value:
            self.pending.pop(key, None)
        else:
            self.pending[key] = value

    def summary(self) -> str:
        trend = self.value("STRATEGY", "hourly") == "trend"
        parts = [f"strategy {describe('STRATEGY', self.value('STRATEGY', 'hourly'))}",
                 f"coins {describe('SYMBOLS', self.value('SYMBOLS', HOURLY_COINS))}"]
        if trend:
            parts.append(f"risk {describe('TREND_VOL_TARGET', self.value('TREND_VOL_TARGET', '0.5'))}")
        parts += [f"budget {describe('STARTING_BALANCE_USDT', self.value('STARTING_BALANCE_USDT', '10000'))}",
                  describe("TRADING_MODE", self.value("TRADING_MODE", "simulation")),
                  f"checks {describe('CYCLE_MINUTES', self.value('CYCLE_MINUTES', '15'))}"]
        return " · ".join(parts)

    def menu(self, note: str = "") -> tuple[str, list[list[str]]]:
        self.state = "menu"
        changed = ("\nNot applied yet: " + "; ".join(f"{NAMES[k]} → {describe(k, v)}" for k, v in self.pending.items())
                   if self.pending else "")
        text = ((note + "\n\n") if note else "") + f"⚙️ Settings now: {self.summary()}{changed}\n\nWhat do you want to change?"
        buttons = [[f"{n} {label}" for n, label in MENU[i:i + 3]] for i in range(0, len(MENU), 3)]
        return text, buttons

    def start(self) -> tuple[str, list[list[str]]]:
        return self.menu("⚙️ Phone setup. Send a number, or tap a button. API keys and tokens stay on the server "
                         "(desk setup); they are never asked for here.")

    # ---- the conversation ----
    def answer(self, text: str) -> tuple[str, list[list[str]] | None]:
        self.touched = self.clock()
        text = (text or "").strip()
        choice = text.split()[0].lower() if text else ""
        handler = getattr(self, f"_on_{self.state}")
        return handler(text, choice)

    def _on_menu(self, text: str, choice: str):
        if choice == "0":
            self.cancelled = True
            return "Phone setup cancelled; nothing was changed.", None
        if choice == "1":
            self.state = "strategy"
            return ("Strategy:\n1 Daily trend rule (recommended): holds a coin while its daily close is above its 50-day "
                    "average, decides once a day, no AI\n2 Hourly agents (the original; lost money after fees in every "
                    "replay)\n0 Back", [["1 Daily trend rule", "2 Hourly agents"], ["0 Back"]])
        if choice == "2":
            self.state = "coins"
            return ("Send the coins separated by commas, for example: BTC, ETH, SOL" +
                    (f"\nThe trend rule was tested on: {describe('SYMBOLS', TREND_COINS)}" if self.value("STRATEGY") == "trend"
                     else "") + "\n0 Back", [["0 Back"]])
        if choice == "3":
            if self.value("STRATEGY", "hourly") != "trend":
                return self.menu("The risk level belongs to the daily trend rule; choose it first (1 Strategy).")
            self.state = "risk"
            return ("Risk level:\n1 Normal: as tested, worst drop in the replays 28-40%\n2 Calmer: smaller positions in "
                    "wild markets, worst drop about 17-24%, about half to two thirds of the gain\n0 Back",
                    [["1 Normal", "2 Calmer"], ["0 Back"]])
        if choice == "4":
            self.state = "budget"
            return ("Send the budget in USDT, for example 5000. With the Binance demo account the desk applies it only if "
                    "the demo wallet holds that much (it tells you at the restart).\n0 Back", [["0 Back"]])
        if choice == "5":
            self.state = "mode"
            return ("Trading mode:\n1 Binance demo account (real orders, fake money; needs the demo keys on the server)\n"
                    "2 Paper trading (real prices, simulated orders)\n0 Back",
                    [["1 Demo account", "2 Paper trading"], ["0 Back"]])
        if choice == "6":
            self.state = "interval"
            return "Send the normal check interval in minutes (5-60), for example 15.\n0 Back", [["15", "30", "60"], ["0 Back"]]
        if choice == "7":
            self.state = "report"
            return "Send the hour (UTC, 0-23) for the daily report, for example 6.\n0 Back", [["6", "8"], ["0 Back"]]
        if choice == "8":
            self.state = "pulse"
            return "Send how often the short pulse message comes, in hours (0 = off), for example 6.\n0 Back", \
                [["6", "12", "0"], ["0 Back"]]
        if choice == "9":
            if not self.pending:
                return self.menu("Nothing has changed yet.")
            self.state = "confirm"
            lines = [f"• {NAMES[k]}: {describe(k, self.current.get(k))} → {describe(k, v)}" for k, v in self.pending.items()]
            return ("Apply these changes? The desk restarts within a few minutes (it finishes its current check first).\n"
                    + "\n".join(lines) + "\nSend YES to apply, or NO to go back.", [["YES", "NO"]])
        return self.menu("Please send a number from the menu.")

    def _on_strategy(self, text: str, choice: str):
        if choice == "0":
            return self.menu()
        was = self.value("STRATEGY", "hourly")
        if choice == "1":
            self._set("STRATEGY", "trend")
            self._set("AUTO_COINS", "false")   # the rule trades the owner's list; the scout's picks are untested
            if was != "trend":
                self.state = "trend_coins"
                return (f"Coins for the trend rule:\n1 The 8 tested coins ({describe('SYMBOLS', TREND_COINS)}), "
                        f"recommended\n2 Keep your coins ({describe('SYMBOLS', self.value('SYMBOLS', HOURLY_COINS))})",
                        [["1 The 8 tested coins", "2 Keep my coins"]])
            return self.menu("The daily trend rule stays in force.")
        if choice == "2":
            self._set("STRATEGY", "hourly")
            if was == "trend":   # back to the hourly agents' tested limits
                self._set("MAX_OPEN_POSITIONS", "3")
                self._set("AUTO_COINS", "true")
                if self.value("SYMBOLS") == TREND_COINS:
                    self._set("SYMBOLS", HOURLY_COINS)
            return self.menu("Strategy: hourly agents (3 positions, coin scout on).")
        return self._on_menu("1", "1")

    def _on_trend_coins(self, text: str, choice: str):
        if choice == "1":
            self._set("SYMBOLS", TREND_COINS)
        coins = self.value("SYMBOLS", HOURLY_COINS).split(",")
        self._set("MAX_OPEN_POSITIONS", str(len(coins)))
        self.state = "risk"
        note = self._demo_note(coins)
        return (f"Strategy: daily trend rule on {describe('SYMBOLS', ','.join(coins))}, one position slot per coin."
                + note + "\n\nRisk level:\n1 Normal: as tested, worst drop in the replays 28-40%\n2 Calmer: worst drop "
                "about 17-24%, about half to two thirds of the gain", [["1 Normal", "2 Calmer"]])

    def _on_coins(self, text: str, choice: str):
        if choice == "0":
            return self.menu()
        coins = [c.strip().upper() for c in re.split(r"[,\s]+", text) if c.strip()]
        coins = list(dict.fromkeys(c if c.endswith("USDT") else c + "USDT" for c in coins))
        bad = [c for c in coins if not COIN.match(c) or not self.check_symbol(c)]
        if not coins or bad or len(coins) > 20:
            return (f"Binance does not list: {', '.join(c.removesuffix('USDT') for c in bad) or '(nothing sent)'}. "
                    "Send names like BTC, ETH, SOL (at most 20), or 0 to go back.", [["0 Back"]])
        self._set("SYMBOLS", ",".join(coins))
        if self.value("STRATEGY", "hourly") == "trend":
            self._set("MAX_OPEN_POSITIONS", str(len(coins)))
        return self.menu(f"Coins: {describe('SYMBOLS', ','.join(coins))}." + self._demo_note(coins))

    def _on_risk(self, text: str, choice: str):
        if choice == "0":
            return self.menu()
        if choice in ("1", "2"):
            self._set("TREND_VOL_TARGET", "0.5" if choice == "1" else "0.25")
            return self.menu(f"Risk level: {'Normal' if choice == '1' else 'Calmer'}.")
        return "Send 1 (Normal) or 2 (Calmer).", [["1 Normal", "2 Calmer"]]

    def _number_state(self, key: str, text: str, choice: str, cast=str):
        if choice == "0":
            return self.menu()
        n = _number(text)
        value = cast(int(n)) if n is not None and cast is int and n == int(n) else (f"{n:g}" if n is not None else text)
        if (why := check_value(key, value, {**self.current, **self.pending})):
            return f"{NAMES[key]} {why}. Send it again, or 0 to go back.", [["0 Back"]]
        self._set(key, value)
        return self.menu(f"{NAMES[key]}: {describe(key, value)}.")

    def _on_budget(self, text: str, choice: str):
        return self._number_state("STARTING_BALANCE_USDT", text, choice)

    def _on_interval(self, text: str, choice: str):
        return self._number_state("CYCLE_MINUTES", text, choice, int)

    def _on_report(self, text: str, choice: str):
        return self._number_state("DAILY_REPORT_HOUR_UTC", text, choice, int)

    def _on_pulse(self, text: str, choice: str):
        return self._number_state("PULSE_HOURS", text, choice)

    def _on_mode(self, text: str, choice: str):
        if choice == "0":
            return self.menu()
        if choice not in ("1", "2"):
            return "Send 1 (demo account) or 2 (paper trading).", [["1 Demo account", "2 Paper trading"]]
        mode = "testnet" if choice == "1" else "simulation"
        if (why := check_value("TRADING_MODE", mode, self.current)):
            return self.menu(f"Not changed: {why}.")
        self._set("TRADING_MODE", mode)
        return self.menu(f"Trading mode: {describe('TRADING_MODE', mode)}.")

    def _on_confirm(self, text: str, choice: str):
        if choice in ("yes", "y"):
            self.done = True
            return "", None
        return self.menu("Not applied yet.")

    def _demo_note(self, coins: list[str]) -> str:
        if self.value("TRADING_MODE", "simulation") != "testnet":
            return ""
        try:
            problems = self.demo_warnings(coins)
        except Exception:  # noqa: BLE001 - only advice
            return ""
        if not problems:
            return ""
        return ("\n⚠️ On the Binance demo exchange: " + "; ".join(
            f"{s.removesuffix('USDT')} {why}" if s != "*" else why for s, why in problems.items())
            + ". The desk never buys such a coin there.")
