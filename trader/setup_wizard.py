"""Interactive setup: asks a few questions, checks every key right away, writes .env.

Run with:  python -m trader setup      (on the server: desk setup)
Re-running is safe - press Enter to keep each current value.
"""
from __future__ import annotations

import datetime as dt
import getpass
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values

from .binance_client import BinanceClient, BinanceError
from .config import ENV_PATH, PUBLIC_BASE_URL, ROOT, SETTINGS_VERSION, TESTNET_BASE_URL, check_minutes
from .llm import MODEL_CHOICES, ClaudeJSON
from .notify import Telegram

LAYOUT = [
    ("Settings file format (do not change)", ["SETTINGS_VERSION"]),
    ("Trading mode: testnet = Binance demo account (fake money), simulation = paper trading", [
        "TRADING_MODE", "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET", "STARTING_BALANCE_USDT"]),
    ("Claude AI (optional - empty key = built-in rules). Daily budget in USD, 0 = no cap", [
        "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "AI_DAILY_BUDGET_USD"]),
    ("Telegram alerts (optional)", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DAILY_REPORT_HOUR_UTC"]),
    ("What to trade: SYMBOLS are always watched; the scout may add coins up to MAX_COINS", [
        "SYMBOLS", "AUTO_COINS", "MAX_COINS", "SCOUT_HOURS", "MIN_VOLUME_MUSD", "CANDLE_INTERVAL"]),
    ("Timing: normal check interval, and the range the AI may choose from", [
        "CYCLE_MINUTES", "MIN_CHECK_MINUTES", "MAX_CHECK_MINUTES", "NEWS_REFRESH_MINUTES"]),
    ("Risk limits (the Risk Manager agent enforces these)", [
        "MAX_POSITION_PCT", "MAX_OPEN_POSITIONS", "MAX_DAILY_LOSS_PCT", "MIN_CONFIDENCE",
        "STOP_LOSS_ATR", "TAKE_PROFIT_ATR"]),
]
DEFAULTS = {
    "TRADING_MODE": "testnet", "STARTING_BALANCE_USDT": "10000", "CLAUDE_MODEL": "claude-opus-5-5",
    "DAILY_REPORT_HOUR_UTC": "6", "SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT", "CANDLE_INTERVAL": "1h",
    "AI_DAILY_BUDGET_USD": "5", "AUTO_COINS": "true", "MAX_COINS": "8", "SCOUT_HOURS": "6", "MIN_VOLUME_MUSD": "10",
    "CYCLE_MINUTES": "15", "MIN_CHECK_MINUTES": "5", "MAX_CHECK_MINUTES": "60", "NEWS_REFRESH_MINUTES": "30",
    "MAX_POSITION_PCT": "0.10", "MAX_OPEN_POSITIONS": "3", "MAX_DAILY_LOSS_PCT": "0.03",
    "MIN_CONFIDENCE": "0.60", "STOP_LOSS_ATR": "2.0", "TAKE_PROFIT_ATR": "3.0",
}


# ---- checks (separate functions so tests can replace them) ----
def check_binance(key: str, secret: str) -> dict[str, float]:
    return BinanceClient(TESTNET_BASE_URL, key, secret).balances()


def check_symbol(symbol: str) -> bool:
    try:
        BinanceClient(PUBLIC_BASE_URL).price(symbol)
        return True
    except BinanceError:
        return False


def check_claude(model: str, key: str) -> bool:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    return ClaudeJSON(model, key).ask("Reply with JSON.", "Say ok.", schema, effort="low") is not None


def write_env(path: Path, values: dict[str, str]) -> None:
    def fmt(v: str) -> str:
        return f'"{v}"' if any(c in v for c in ' #"\'') else v

    lines = ["# Crypto trading desk settings - written by `python -m trader setup`.",
             "# Keep this file private: it contains your API keys.", ""]
    known = set()
    for title, keys in LAYOUT:
        lines.append(f"# {title}")
        for k in keys:
            known.add(k)
            lines.append(f"{k}={fmt(values.get(k) or DEFAULTS.get(k, ''))}")
        lines.append("")
    extra = {k: v for k, v in values.items() if k not in known and v is not None}
    if extra:
        lines.append("# Other settings")
        lines += [f"{k}={fmt(v)}" for k, v in extra.items()]
    # Create the file private from the first byte and swap it in atomically: no instant where it is
    # world-readable, and no half-written file if the machine dies mid-write.
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Wizard:
    def __init__(self, env_path: Path = ENV_PATH, ask=input, ask_secret=getpass.getpass, say=print):
        self.env_path = Path(env_path)
        self._ask, self._ask_secret, self.say = ask, ask_secret, say
        self.old = {k: v for k, v in dotenv_values(self.env_path).items()} if self.env_path.exists() else {}
        self.v: dict[str, str] = dict(self.old)
        if self.v.get("CYCLE_MINUTES"):  # files from before 15-minute checks: move the old 60 default to 15
            self.v["CYCLE_MINUTES"] = str(check_minutes(self.v["CYCLE_MINUTES"], self.v.get("SETTINGS_VERSION")))
        self.v["SETTINGS_VERSION"] = SETTINGS_VERSION

    # ---- small input helpers ----
    def ask(self, question: str, default: str = "") -> str:
        answer = self._ask(f"{question} [{default}]: " if default else f"{question}: ").strip()
        return answer or default

    def ask_secret(self, question: str, current: str = "") -> str:
        hint = f" [Enter = keep current ••••{current[-4:]}]" if current else ""
        answer = self._ask_secret(f"{question}{hint} (hidden - nothing shows while you paste): ").strip()
        return answer or current

    def ask_amount(self, question: str, default: str, allow_zero: bool = False) -> str:
        while True:
            a = self.ask(question, default).replace(",", "").replace("$", "")
            try:
                if float(a) > 0 or (allow_zero and float(a) == 0):
                    return a
            except ValueError:
                pass
            self.say("   Please type a positive number, e.g. 5000")

    def ask_int(self, question: str, default: str, low: int, high: int) -> str:
        while True:
            a = self.ask(question, default)
            if a.isdigit() and low <= int(a) <= high:
                return a
            self.say(f"   Please type a whole number from {low} to {high}.")

    def yes(self, question: str, default: bool) -> bool:
        answer = self.ask(f"{question} (y/n)", "y" if default else "n").lower()
        return answer.startswith("y")

    def choose(self, question: str, options: list[str], default: int) -> int:
        for i, o in enumerate(options, 1):
            self.say(f"   {i}) {o}")
        while True:
            a = self.ask(question, str(default))
            if a.isdigit() and 1 <= int(a) <= len(options):
                return int(a)
            self.say(f"   Please type a number from 1 to {len(options)}.")

    # ---- steps ----
    def run(self) -> dict[str, str]:
        self.say("\n=== Crypto trading desk - setup (about 5 minutes) ===")
        self.say("Press Enter to accept the value shown in [brackets].\n")
        self.step_mode()
        self.step_claude()
        self.step_telegram()
        self.step_coins()
        self.offer_fresh_journal()
        write_env(self.env_path, self.v)
        self.say(f"\n✅ Settings saved to {self.env_path} (only your user can read this file).")
        return self.v

    def step_mode(self) -> None:
        self.say("STEP 1 of 4 - Trading mode")
        current = 1 if self.v.get("TRADING_MODE", "testnet") == "testnet" else 2
        pick = self.choose("Choose 1 or 2", [
            "Binance demo account: real API orders with fake money (recommended)",
            "Paper trading: simulated orders at real prices, no Binance account needed"], current)
        self.v["TRADING_MODE"] = "testnet" if pick == 1 else "simulation"
        if pick == 2:
            self.v["STARTING_BALANCE_USDT"] = self.ask_amount("   Starting budget in USDT",
                                                              self.v.get("STARTING_BALANCE_USDT") or "10000")
            return
        self.say("\n   Binance demo keys: open https://testnet.binance.vision, click 'Log In with GitHub',")
        self.say("   then 'Generate HMAC_SHA256 Key'. Copy the API Key and the Secret Key.")
        while True:
            key = self.ask_secret("   API Key", self.v.get("BINANCE_TESTNET_API_KEY", ""))
            secret = self.ask_secret("   Secret Key", self.v.get("BINANCE_TESTNET_API_SECRET", ""))
            self.say("   Checking with Binance...")
            try:
                balances = check_binance(key, secret)
            except BinanceError as e:
                msg = str(e)
                if "restricted location" in msg.lower():
                    self.say("   ❌ Binance refuses connections from this server's country.")
                    self.say("      Your server must be in a Binance-supported country (see the guide: Germany works).")
                else:
                    self.say(f"   ❌ Binance did not accept these keys: {msg}")
                pick = self.choose("What now?", ["Try the keys again", "Switch to paper trading for now"], 1)
                if pick == 2:
                    self.v["TRADING_MODE"] = "simulation"
                    return
                continue
            except Exception as e:  # network problems etc.
                self.say(f"   ❌ Could not reach Binance: {e}")
                if self.yes("   Try again?", True):
                    continue
                self.v["TRADING_MODE"] = "simulation"
                return
            break
        self.v["BINANCE_TESTNET_API_KEY"], self.v["BINANCE_TESTNET_API_SECRET"] = key, secret
        usdt = balances.get("USDT", 0.0)
        shown = ", ".join(f"{v:,.4g} {k}" for k, v in balances.items() if k in ("USDT", "BTC", "ETH", "SOL", "BNB"))
        self.say(f"   ✅ Connected. Demo balance: {shown or 'empty'}")
        if usdt < 100:
            self.say("   ⚠️  The demo account has almost no USDT, so the agents cannot buy yet.")
        default_budget = self.v.get("STARTING_BALANCE_USDT") or str(int(min(10_000, usdt * 0.9)) or 10_000)
        try:
            float(default_budget)
        except ValueError:
            default_budget = "10000"
        if usdt and float(default_budget) > usdt * 0.95:
            default_budget = str(int(usdt * 0.9))
        while True:
            budget = self.ask_amount("   Budget the agents may use, in USDT", default_budget)
            if not usdt or float(budget) <= usdt * 0.95:
                break
            self.say(f"   The demo account only holds {usdt:,.0f} USDT; the budget must be at most {usdt * 0.95:,.0f}.")
        self.v["STARTING_BALANCE_USDT"] = budget

    def step_claude(self) -> None:
        self.say("\nSTEP 2 of 4 - Claude AI (optional)")
        self.say("   With Claude, AI reads the news and makes the final buy/sell call, with a written reason.")
        self.say("   Without it, the built-in rules decide (free). You can add it later by re-running setup.")
        if not self.yes("   Use Claude?", bool(self.v.get("ANTHROPIC_API_KEY"))):
            self.v["ANTHROPIC_API_KEY"] = ""
            return
        self.say("   Get a key at https://console.anthropic.com -> API Keys (the account needs some credit).")
        ids = [m[0] for m in MODEL_CHOICES]
        current = ids.index(self.v["CLAUDE_MODEL"]) + 1 if self.v.get("CLAUDE_MODEL") in ids else 1
        while True:
            key = self.ask_secret("   Claude API key", self.v.get("ANTHROPIC_API_KEY", ""))
            self.say("   Which model? (rough cost with checks every 15 minutes and up to 8 coins)")
            pick = self.choose("   Choose", [f"{label} - {cost}" for _, label, cost in MODEL_CHOICES], current)
            model = ids[pick - 1]
            self.say("   Checking with Claude...")
            if check_claude(model, key):
                self.say("   ✅ Claude answered.")
                self.v["ANTHROPIC_API_KEY"], self.v["CLAUDE_MODEL"] = key, model
                self.say("   Spending cap: once Claude has cost this much in a day, the agents use the free rules")
                self.say("   until midnight (UTC). This keeps the monthly bill predictable.")
                self.v["AI_DAILY_BUDGET_USD"] = self.ask_amount(
                    "   Daily limit for Claude in USD (0 = no limit)", self.v.get("AI_DAILY_BUDGET_USD") or "5",
                    allow_zero=True)
                return
            self.say("   ❌ Claude did not answer. Check the key, and that the account has credit.")
            if not self.yes("   Try again?", True):
                self.v["ANTHROPIC_API_KEY"] = ""
                self.say("   OK - using the built-in rules instead.")
                return

    def step_telegram(self) -> None:
        self.say("\nSTEP 3 of 4 - Phone alerts on Telegram (optional, recommended)")
        self.say("   You get a message for every buy, sell or failed order, a daily report,")
        self.say("   and you can send /status or /report to your bot at any time.")
        if not self.yes("   Set up Telegram alerts?", True):
            self.v["TELEGRAM_BOT_TOKEN"] = self.v["TELEGRAM_CHAT_ID"] = ""
            return
        self.say("   1. In Telegram, search for @BotFather, open it and send:  /newbot")
        self.say("   2. Choose a name (e.g. My Crypto Desk) and a username that ends in 'bot'")
        self.say("   3. BotFather replies with a token that looks like 123456789:AAH...")
        while True:
            token = self.ask_secret("   Paste the bot token", self.v.get("TELEGRAM_BOT_TOKEN", ""))
            try:
                username = Telegram(token).bot_username()
                break
            except Exception as e:
                self.say(f"   ❌ Telegram did not accept that token ({e}).")
                if not self.yes("   Try again?", True):
                    self.v["TELEGRAM_BOT_TOKEN"] = self.v["TELEGRAM_CHAT_ID"] = ""
                    return
        self.say(f"   ✅ Found your bot @{username}")
        chat_id = self.v.get("TELEGRAM_CHAT_ID", "") if token == self.v.get("TELEGRAM_BOT_TOKEN") else ""
        if chat_id and self.yes("   Keep sending alerts to the same Telegram chat as before?", True):
            pass
        else:
            chat_id = ""
            code = f"{secrets.randbelow(900000) + 100000}"  # one-time pairing code: only its sender becomes the owner
            self.say(f"   Now open https://t.me/{username} in Telegram, press START, and send this code: {code}")
            while not chat_id:
                self._ask("   then come back here and press Enter... ")
                try:
                    chat_id = Telegram(token).latest_chat_id(code) or ""
                except Exception as e:
                    self.say(f"   Could not read messages: {e}")
                if not chat_id:
                    self.say(f"   No message containing {code} found yet.")
                    if not self.yes("   Try again?", True):
                        self.v["TELEGRAM_BOT_TOKEN"] = self.v["TELEGRAM_CHAT_ID"] = ""
                        return
        tg = Telegram(token, chat_id)
        if tg.send("✅ Connected! Your crypto trading desk will send its alerts to this chat."):
            self.say("   ✅ Sent you a test message on Telegram.")
        self.v["TELEGRAM_BOT_TOKEN"], self.v["TELEGRAM_CHAT_ID"] = token, chat_id
        now_utc = dt.datetime.now(dt.timezone.utc).strftime("%H:%M")
        while True:
            hour = self.ask(f"   Daily report hour in UTC, 0-23 (it is {now_utc} UTC now)",
                            self.v.get("DAILY_REPORT_HOUR_UTC") or "6")
            if hour.isdigit() and 0 <= int(hour) <= 23:
                self.v["DAILY_REPORT_HOUR_UTC"] = hour
                return
            self.say("   Please type a whole hour from 0 to 23.")

    def step_coins(self) -> None:
        self.say("\nSTEP 4 of 4 - Coins and timing")
        while True:
            coins = self.ask("   Core coins, always watched (comma separated)", self.v.get("SYMBOLS") or DEFAULTS["SYMBOLS"])
            symbols = [c.strip().upper() for c in coins.split(",") if c.strip()]
            symbols = [s if s.endswith("USDT") else s + "USDT" for s in symbols]
            unknown = [s for s in symbols if not check_symbol(s)]
            if symbols and not unknown:
                self.v["SYMBOLS"] = ",".join(symbols)
                break
            self.say(f"   Binance does not list: {', '.join(unknown) or '(nothing entered)'}. "
                     "Use names like BTCUSDT or just BTC.")
        self.say("   The Market Scout can add more coins: every 6 hours it scans all Binance coins and, with")
        self.say("   Claude, searches the web for news to pick the most promising ones (each with a reason).")
        auto = self.yes("   Let the scout add coins?", self.v.get("AUTO_COINS", "true").lower() != "false")
        self.v["AUTO_COINS"] = "true" if auto else "false"
        if auto:
            low = len(symbols)
            current = self.v.get("MAX_COINS") or DEFAULTS["MAX_COINS"]
            self.v["MAX_COINS"] = self.ask_int(f"   Most coins to watch at once ({low}-20)",
                                               current if current.isdigit() and int(current) >= low else str(low),
                                               low, 20)
        self.say("   How often to check the market. With Claude, the AI may check more often (down to 5 min)")
        self.say("   when big news breaks, or less often (up to 60 min) when markets are quiet.")
        self.v["CYCLE_MINUTES"] = self.ask_int("   Normal check interval in minutes (5-60)",
                                               self.v.get("CYCLE_MINUTES") or DEFAULTS["CYCLE_MINUTES"], 5, 60)

    def offer_fresh_journal(self) -> None:
        """Changing mode or budget mid-test mixes two experiments; offer a clean start."""
        journal = ROOT / "data" / "journal.db"
        changed = [k for k in ("TRADING_MODE", "STARTING_BALANCE_USDT")
                   if self.old.get(k) and self.old.get(k) != self.v.get(k)]
        if not journal.exists() or not changed:
            return
        self.say("\n   You changed the trading mode or budget. Results so far were recorded under the old settings.")
        if self.yes("   Start a fresh journal? (the old one is kept as a backup file)", True):
            backup = archive_journal(journal)
            self.say(f"   Old journal saved as {backup.name}")


def archive_journal(journal: Path) -> Path:
    backup = journal.with_name(f"journal-{dt.datetime.now():%Y%m%d-%H%M%S}.db")
    journal.rename(backup)
    return backup


def run_wizard() -> None:
    try:
        Wizard().run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled - nothing was changed.")
        raise SystemExit(1)
