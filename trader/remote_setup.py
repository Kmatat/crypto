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
import stat
import subprocess
import sys
import time
from pathlib import Path

from .trend import TESTED_COINS

REQUEST_NAME = "settings-request.json"
CLAIMED_NAME = "settings-request.processing"   # the update job's copy while it applies the request
BACKUP_NAME = ".env.previous"
SESSION_MINUTES = 15          # an unanswered phone setup ends by itself
MAX_AGE_HOURS = 1             # an older request is not applied: the settings may have changed on the server since
HOURLY_COINS = "BTCUSDT,ETHUSDT,SOLUSDT"
TREND_COINS = ",".join(TESTED_COINS)
ALLOWED = ("STRATEGY", "SYMBOLS", "MAX_OPEN_POSITIONS", "AUTO_COINS", "TREND_VOL_TARGET", "STARTING_BALANCE_USDT",
           "TRADING_MODE", "CYCLE_MINUTES", "DAILY_REPORT_HOUR_UTC", "PULSE_HOURS")
COIN = re.compile(r"[A-Z0-9]{2,20}USDT")         # used with fullmatch: nothing before or after, not even a newline
WHOLE = re.compile(r"\d{1,9}")                    # the exact forms the settings loader reads (int() and float())
DECIMAL = re.compile(r"\d{1,9}(\.\d{1,8})?")
LIMITS = {"MAX_OPEN_POSITIONS": (1, 20, True), "TREND_VOL_TARGET": (0.05, 2.0, False),
          "STARTING_BALANCE_USDT": (10.0, 10_000_000.0, False), "CYCLE_MINUTES": (5, 60, True),
          "DAILY_REPORT_HOUR_UTC": (0, 23, True), "PULSE_HOURS": (0, 48, False)}
TREND_KEYS = ("STRATEGY", "SYMBOLS", "MAX_OPEN_POSITIONS", "AUTO_COINS", "TREND_VOL_TARGET")   # what a strategy switch sets
NAMES = {"STRATEGY": "Strategy", "SYMBOLS": "Coins", "MAX_OPEN_POSITIONS": "Position slots", "AUTO_COINS": "Coin scout",
         "TREND_VOL_TARGET": "Risk level", "STARTING_BALANCE_USDT": "Budget", "TRADING_MODE": "Trading mode",
         "CYCLE_MINUTES": "Check interval", "DAILY_REPORT_HOUR_UTC": "Daily report hour", "PULSE_HOURS": "Pulse"}


def _number(value: str) -> float | None:
    """A number typed on the phone: '6', '6.5' or '6,5' (a decimal comma)."""
    try:
        v = float(str(value).replace(",", ".").strip())
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def _amount(value: str) -> float | None:
    """A budget typed on the phone: '5000', '$5,000', '10 000' or '2500.50'. A comma is read as a thousands separator
    only in that exact form (10,000); anything else with a comma is refused rather than guessed."""
    t = re.sub(r"(?i)usdt|\$|\s", "", str(value))
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", t):
        t = t.replace(",", "")
    elif "," in t:
        return None
    return _number(t)


def plain(n: float, places: int = 8) -> str:
    """A number the way the settings file stores it: no exponent, no thousands separator ('1234567', '0.25')."""
    return str(int(n)) if n == int(n) else f"{n:.{places}f}".rstrip("0").rstrip(".")


def _shown(n: float) -> str:
    return f"{n:,.0f}" if n == int(n) else f"{n:g}"


def check_value(key: str, value: str, current: dict) -> str | None:
    """Why `value` cannot be saved for `key` (None when it can). The update job checks every value again, strictly:
    only the exact text the settings loader reads is accepted (no spaces, signs, commas, exponents or newlines), so
    whatever passes here also loads when the desk starts."""
    if key not in ALLOWED:
        return f"{key} cannot be changed from the phone"
    value = str(value)
    if key == "STRATEGY":
        return None if value in ("hourly", "trend") else "must be hourly or trend"
    if key == "SYMBOLS":
        coins = value.split(",")
        if not 1 <= len(coins) <= 20 or not all(COIN.fullmatch(c) for c in coins) or len(set(coins)) != len(coins):
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
    low, high, whole = LIMITS[key]
    if not (WHOLE if whole else DECIMAL).fullmatch(value) or not low <= float(value) <= high:
        return f"must be {'a whole number' if whole else 'a number'} from {_shown(low)} to {_shown(high)}"
    return None


def pending_request(data_dir: Path) -> dict[str, str]:
    """Changes confirmed on the phone that the update job has not applied yet ({} when none)."""
    try:
        request = json.loads(read_small(data_dir / REQUEST_NAME) or "{}")
        changes = request.get("changes") if isinstance(request, dict) else None
        return {str(k): str(v) for k, v in changes.items()} if isinstance(changes, dict) else {}
    except ValueError:
        return {}


def write_request(data_dir: Path, changes: dict[str, str]) -> Path:
    """The desk's half: the owner's confirmed changes, for the update job to check and apply. Changes confirmed
    earlier and not applied yet are kept (the new ones win), so a second /setup never drops the first."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / REQUEST_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"requested_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "by": "telegram", "changes": {**pending_request(data_dir), **changes}}))
    os.replace(tmp, path)
    return path


def read_small(path: Path, limit: int = 65536) -> str | None:
    """A small file from the data folder, read without following a link and without waiting on anything that is not
    a plain file (the desk may write the data folder; the update job must not be steered or stalled through it).
    None when there is no file; "" when it is not a plain file or too large."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        return "" if os.path.lexists(path) else None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        data = os.read(fd, limit + 1)
        return data.decode("utf-8", "replace") if len(data) <= limit else ""
    finally:
        os.close(fd)


def private_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a whole file next to `path` and swap it in: a link or an old file at `path` is replaced, never written
    through, and the file has `mode` from its first byte."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fh.fileno(), mode)
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_problem(env_path: Path) -> str | None:
    """Why the settings file would stop the desk from starting (None when it loads), checked the way the desk loads
    it: in a fresh process that has none of this job's settings in its environment."""
    from .config import ENV_KEYS
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    code = ("import sys; from pathlib import Path; from trader import config; config.ENV_PATH = Path(sys.argv[1]); "
            "config.Settings.load()")
    try:
        p = subprocess.run([sys.executable, "-c", code, str(env_path)], cwd=Path(__file__).resolve().parent.parent,
                           env=env, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not be checked ({e})"
    if p.returncode == 0:
        return None
    lines = [ln for ln in (p.stderr or p.stdout).strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else f"exit code {p.returncode}")[-200:]


def apply_request(env_path: Path, data_dir: Path) -> tuple[str, bool] | None:
    """The update job's half: check the request, save the current settings as data/.env.previous, write the new ones.
    Returns (message for the owner, applied) or None when nothing was asked. The request is always consumed."""
    from dotenv import dotenv_values
    from .setup_wizard import write_env
    path, claimed = data_dir / REQUEST_NAME, data_dir / CLAIMED_NAME
    if os.path.lexists(path):   # claimed first: a request the desk writes from now on waits for the next run
        os.replace(path, claimed)
    elif not os.path.lexists(claimed):   # (a claimed copy left by a job that was stopped is still answered)
        return None
    try:
        request = json.loads(read_small(claimed) or "")
        changes = request["changes"]
        if not isinstance(changes, dict):
            raise ValueError("no changes")
        changes = {str(k): str(v) for k, v in changes.items()}
        asked = dt.datetime.fromisoformat(request["requested_at"])
        age = (dt.datetime.now(dt.timezone.utc) - asked).total_seconds() / 3600
    except (ValueError, KeyError, TypeError, AttributeError):
        return "⚠️ The settings sent from the phone could not be read; nothing was changed. Send /setup again.", False
    finally:
        claimed.unlink(missing_ok=True)
    if age > MAX_AGE_HOURS:
        return (f"⚠️ The settings sent from the phone on {asked:%d %b at %H:%M} UTC reached the update job only now, so they were "
                "not applied (the settings may have changed on the server since). Nothing was changed; send /setup "
                "again."), False
    current = {k: v for k, v in dotenv_values(env_path).items() if v is not None} if env_path.exists() else {}
    problems = [f"{NAMES.get(k, k)}: {why}" for k, v in changes.items() if (why := check_value(k, v, current))]
    if problems or not changes:
        return ("⚠️ Settings from the phone not applied: " + ("; ".join(problems) or "nothing to change")
                + ". Nothing was changed."), False
    base = normalized(current)
    # The file writer fills empty or missing lines with the wizard's defaults; keep what the desk does with them now
    if not current.get("TRADING_MODE", "").strip():
        base["TRADING_MODE"] = "simulation"
    if "MENTOR_CHECKS" in current and not current["MENTOR_CHECKS"].strip():
        base["MENTOR_CHECKS"] = "none"
    before = env_path.read_text() if env_path.exists() else None
    if before is not None:
        private_write(data_dir / BACKUP_NAME, before)
    write_env(env_path, {**base, **changes})
    if (why := load_problem(env_path)):   # never leave a file the desk (and this job) cannot start with
        if before is None:
            env_path.unlink(missing_ok=True)
        else:
            private_write(env_path, before)
        return (f"⚠️ Settings from the phone not applied: the new settings file did not load ({why}). The previous "
                "file was put back; nothing was changed."), False
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
    """One /setup conversation. `answer(text)` returns (reply, buttons); `pending` holds what the owner confirmed
    once `done`. `check_symbol(coin)`, `demo_warnings(coins)` and `check_keys()` reach Binance (tests replace them);
    `held` lists the coins the desk holds now."""

    def __init__(self, current: dict[str, str], check_symbol=None, demo_warnings=None, clock=time.time,
                 check_keys=None, held=()):
        self.current = {k: v for k, v in current.items() if v is not None}
        for key in ("STRATEGY", "TRADING_MODE", "AUTO_COINS"):   # read the way the desk reads them
            if key in self.current:
                self.current[key] = self.current[key].strip().lower()
        self.held = list(held)
        self.check_keys = check_keys or (lambda: None)
        self.pending: dict[str, str] = {}
        self._undo: dict[str, str] = {}
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
        now = self.current.get(key)
        if now == value or (key in LIMITS and now is not None and _number(now) == _number(value)):
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
                    + "\n".join(lines) + self._sold_note() + "\nSend YES to apply, or NO to go back.", [["YES", "NO"]])
        return self.menu("Please send a number from the menu.")

    def _back_to_saved(self) -> None:
        """The owner picked the strategy the server already runs: its saved coins, slots and scout stay as they are."""
        for key in TREND_KEYS:
            self.pending.pop(key, None)

    def _on_strategy(self, text: str, choice: str):
        if choice == "0":
            return self.menu()
        saved, was = self.current.get("STRATEGY") or "hourly", self.value("STRATEGY", "hourly")
        if choice == "1":
            if saved == "trend":
                self._back_to_saved()
                return self.menu("The daily trend rule stays in force, with your saved coins and risk level.")
            if was == "trend":
                return self.menu("The daily trend rule is already chosen (not applied yet).")
            self._undo = dict(self.pending)
            self._set("STRATEGY", "trend")
            self._set("AUTO_COINS", "false")   # the rule trades the owner's list; the scout's picks are untested
            self.state = "trend_coins"
            return (f"Coins for the trend rule:\n1 The 8 tested coins ({describe('SYMBOLS', TREND_COINS)}), "
                    f"recommended\n2 Keep your coins ({describe('SYMBOLS', self.value('SYMBOLS', HOURLY_COINS))})\n0 Back",
                    [["1 The 8 tested coins", "2 Keep my coins"], ["0 Back"]])
        if choice == "2":
            if saved == "hourly":
                self._back_to_saved()
                return self.menu("Strategy: hourly agents, with your saved coins, slots and scout.")
            self._set("STRATEGY", "hourly")   # back to the hourly agents' tested limits
            self._set("MAX_OPEN_POSITIONS", "3")
            self._set("AUTO_COINS", "true")
            if self.value("SYMBOLS") == TREND_COINS:
                self._set("SYMBOLS", HOURLY_COINS)
            return self.menu("Strategy: hourly agents (3 positions, coin scout on).")
        return self._on_menu("1", "1")

    def _on_trend_coins(self, text: str, choice: str):
        if choice == "0":
            self.pending = dict(self._undo)
            return self.menu("Strategy not changed.")
        if choice not in ("1", "2"):
            return ("Send 1 (the 8 tested coins), 2 (keep your coins) or 0 (back).",
                    [["1 The 8 tested coins", "2 Keep my coins"], ["0 Back"]])
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
        if not coins or len(coins) > 20 or not all(COIN.fullmatch(c) for c in coins):   # before asking Binance
            return ("Send 1 to 20 coin names like BTC, ETH, SOL (letters and digits only), or 0 to go back.",
                    [["0 Back"]])
        bad = next(([c] for c in coins if not self.check_symbol(c)), [])   # stops at the first one Binance lacks
        if bad:
            return (f"Binance does not list: {bad[0].removesuffix('USDT')}. Send names like BTC, ETH, SOL (at most "
                    "20), or 0 to go back.", [["0 Back"]])
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

    def _number_state(self, key: str, text: str, choice: str, read=_number, places: int = 8):
        if choice == "0":
            return self.menu()
        n = read(text)
        value = plain(n, places) if n is not None else text   # the exact text the settings file gets
        if (why := check_value(key, value, {**self.current, **self.pending})):
            return f"{NAMES[key]} {why}. Send it again, or 0 to go back.", [["0 Back"]]
        self._set(key, value)
        return self.menu(f"{NAMES[key]}: {describe(key, value)}.")

    def _on_budget(self, text: str, choice: str):
        return self._number_state("STARTING_BALANCE_USDT", text, choice, _amount, 2)

    def _on_interval(self, text: str, choice: str):
        return self._number_state("CYCLE_MINUTES", text, choice)

    def _on_report(self, text: str, choice: str):
        return self._number_state("DAILY_REPORT_HOUR_UTC", text, choice)

    def _on_pulse(self, text: str, choice: str):
        return self._number_state("PULSE_HOURS", text, choice)

    def _on_mode(self, text: str, choice: str):
        if choice == "0":
            return self.menu()
        if choice not in ("1", "2"):
            return "Send 1 (demo account) or 2 (paper trading).", [["1 Demo account", "2 Paper trading"]]
        mode = "testnet" if choice == "1" else "simulation"
        if mode == (self.current.get("TRADING_MODE") or "simulation"):
            self.pending.pop("TRADING_MODE", None)
            return self.menu(f"Trading mode: {describe('TRADING_MODE', mode)} (as saved).")
        if (why := check_value("TRADING_MODE", mode, self.current)):
            return self.menu(f"Not changed: {why}.")
        if self.held:   # the positions live on one account; the other one knows nothing of them
            return self.menu(f"Not changed: the desk holds {describe('SYMBOLS', ','.join(self.held))}. Change the trading "
                             "mode on the server with desk setup (it offers a fresh record), or once nothing is held.")
        if mode == "testnet":
            try:
                problem = self.check_keys()
            except Exception as e:  # noqa: BLE001 - the check itself failed: say so, change nothing
                problem = f"the check did not work ({type(e).__name__})"
            if problem:
                return self.menu(f"Not changed: Binance did not accept the demo keys saved on the server ({problem}). "
                                 "Set them on the server with desk setup.")
        self._set("TRADING_MODE", mode)
        return self.menu(f"Trading mode: {describe('TRADING_MODE', mode)}. The results so far stay in the same record; "
                         "for a fresh record use desk setup on the server.")

    def _on_confirm(self, text: str, choice: str):
        if choice in ("yes", "y"):
            self.done = True
            return "", None
        return self.menu("Not applied yet.")

    def _sold_note(self) -> str:
        """The trend rule sells held coins that are not in its list at its next daily decision: say which ones."""
        if self.value("STRATEGY", "hourly") != "trend" or not self.held:
            return ""
        coins = set(self.value("SYMBOLS", HOURLY_COINS).split(","))
        sold = [c for c in self.held if c not in coins]
        return (f"\n⚠️ The desk holds {describe('SYMBOLS', ','.join(sold))}, which is not in the list: the trend rule "
                "sells it at its next daily decision (just after 00:00 UTC).") if sold else ""

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
