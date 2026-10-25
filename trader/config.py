"""Settings loaded from environment variables / a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

TESTNET_BASE_URL = "https://testnet.binance.vision"
# Binance's public market-data-only service: real prices/candles, no account needed,
# and not blocked by country (unlike trading endpoints).
PUBLIC_BASE_URL = "https://data-api.binance.vision"


# Every environment variable the settings loader reads. The automated tests run with none of them set, and the
# update job removes them (and with them the API keys) from the tests' environment: a test must never see the
# server's real configuration. tests/test_hardening.py checks this list against Settings.load.
ENV_KEYS = (
    "AI_BUDGET_PCT_MONTH",
    "AI_DAILY_BUDGET_USD",
    "AI_ORDER",
    "ANTHROPIC_API_KEY",
    "AUTO_COINS",
    "AUTO_PROMOTE",
    "AUTO_UPDATE",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "BRAKE_PAUSE_DAYS",
    "CANDLE_INTERVAL",
    "CLAUDE_MODEL",
    "COOLDOWN_HOURS",
    "CYCLE_MINUTES",
    "DAILY_REPORT_HOUR_UTC",
    "FREE_AI_API_KEY",
    "FREE_AI_BASE_URL",
    "FREE_AI_DAILY_REQUESTS",
    "FREE_AI_MODELS",
    "FREE_AI_PROVIDER",
    "HEAD_TRADER",
    "JOURNAL_DB",
    "LAB_ALT_SYMBOLS",
    "LAB_ENABLED",
    "LAB_WEEKDAY",
    "MAX_CHECK_MINUTES",
    "MAX_COINS",
    "MAX_DAILY_LOSS_PCT",
    "MAX_DRAWDOWN_PCT",
    "MAX_OPEN_POSITIONS",
    "MAX_POSITION_PCT",
    "MENTOR_CHECKS",
    "MIN_CHECK_MINUTES",
    "MIN_CONFIDENCE",
    "MIN_REWARD_RISK",
    "MIN_VOLUME_MUSD",
    "NEWS_REFRESH_MINUTES",
    "NOTES_DIR",
    "PULSE_HOURS",
    "RESEARCH_CANDIDATES",
    "RISK_PER_TRADE_PCT",
    "SCOUT_HOURS",
    "SETTINGS_VERSION",
    "STARTING_BALANCE_USDT",
    "STOP_LOSS_ATR",
    "SYMBOLS",
    "TAKE_PROFIT_ATR",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TRADING_MODE",
    "TRAIL_ATR",
)

SETTINGS_VERSION = "3"
AI_BUDGET_CEILING_USD = 20.0   # the most the owner's /aibudget accepts per day (real money): a typo cannot run up a bill  # 2 = 15-minute checks + AI coin scout; 3 = take-profit default 4 ATR (checklist era)


def _version(v: str | None) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def legacy_take_profit(value: str | None, settings_version: str | None, min_reward_risk: str | None) -> tuple[float | None, str]:
    """Settings files written before version 3 pinned TAKE_PROFIT_ATR=3.0, the default of the time. With the
    checklist's 2:1 reward rule and a 2 ATR stop that is 1.5:1, so the reward rule refuses every buy: the desk can
    never trade. Such a value is the old default, not a choice, and moves to today's 4.0. An owner who also lowered
    MIN_REWARD_RISK to 1.5 or less chose 3.0 on purpose and keeps it. Returns (value to use or None, note)."""
    if value is None or _version(settings_version) >= 3:
        return None, ""
    try:
        if abs(float(value) - 3.0) > 1e-9:
            return None, ""
        if min_reward_risk is not None and float(min_reward_risk) <= 1.5 + 1e-9:
            return None, ""
    except ValueError:
        return None, ""
    return 4.0, ("TAKE_PROFIT_ATR 3.0 -> 4.0: your settings file still had the take-profit default of an older "
                 "version; with the 2 ATR stop it is only 1.5:1, which the checklist's 2:1 reward rule refuses on every buy")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


def check_minutes(value: str | None, settings_version: str | None) -> int:
    """Check interval; settings saved before version 2 used a 60-minute default, now 15."""
    minutes = int(value or 15)
    return 15 if minutes == 60 and not settings_version else minutes


@dataclass
class Settings:
    mode: str = "simulation"
    offline: bool = False
    api_key: str = field(default="", repr=False)      # repr=False: never printed if Settings is ever logged
    api_secret: str = field(default="", repr=False)
    anthropic_key: str = field(default="", repr=False)
    claude_model: str = "claude-opus-5-5"
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    interval: str = "1h"
    cycle_minutes: int = 15          # normal time between market checks
    min_check_minutes: int = 5       # fastest the AI / risk rules may check
    max_check_minutes: int = 60      # slowest the AI may check
    auto_coins: bool = True          # let the Market Scout add coins beyond `symbols`
    max_coins: int = 8               # most coins watched at once (core + scout picks)
    scout_hours: float = 6           # how often the scout looks for coins
    min_volume_musd: float = 10      # scout ignores coins with less 24h trading volume (million USD)
    news_refresh_minutes: int = 30
    ai_daily_budget_usd: float = 5.0  # Claude spending cap per day; 0 = no cap
    ai_budget_pct_month: float | None = None  # % of the account a month; unset = off on demo money, 0.25 with real money
    # ---- a free AI (trader/free_ai.py): answers the agents' everyday questions at no cost, within its daily limits ----
    free_ai_provider: str = ""       # gemini, groq, mistral, cloudflare, openrouter or custom; empty = none
    free_ai_key: str = field(default="", repr=False)
    free_ai_models: tuple = ()       # empty = the provider's recommended models, tried in order
    free_ai_base_url: str = ""       # custom services, and Cloudflare (the address holds the account id)
    free_ai_daily_requests: int = 0  # requests a day per model the desk allows itself; 0 = the provider's default
    ai_order: str = "free-first"     # free-first: the free AI answers, Claude only when it cannot; or claude-first
    auto_update: bool = True         # install new code from GitHub automatically (else only notify)
    # ---- strategy laboratory (trader/lab.py): the desk proposes, tests and promotes strategy cards ----
    lab_enabled: bool = True
    head_trader: bool = True         # the head trader paces Claude's budget, tilts thresholds, runs the threshold study
    lab_weekday: int = 0             # 0 = Monday: weekly research + exam
    lab_alt_symbols: tuple = ("BNBUSDT", "XRPUSDT", "ADAUSDT")  # second coin set every challenger must survive
    research_candidates: int = 3     # new strategy proposals per week
    auto_promote: bool = False       # True = promote a challenger that passed everything without asking the owner
    starting_balance: float = 10_000.0
    max_position_pct: float = 0.10   # ceiling per position; RISK_PER_TRADE_PCT can only make positions smaller
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10   # overall brake: no new buys once equity is this far below the start
    brake_pause_days: int = 14       # after the brake trips: pause this long, then resume at half size
    min_confidence: float = 0.60
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 4.0     # 2:1 reward-to-risk with the 2 ATR stop
    # ---- master traders' checklist (trader/agents/mentor.py) and related disciplines ----
    # Defaults chosen from backtests on 90/180/365 days (see README "Measuring the strategy"):
    # trend200 + btc + cooldown + reward with trailing stops and 1% risk sizing was the robust set.
    mentor_checks: tuple = ("trend200", "btc", "cooldown", "reward")  # also available: alignment, overextended
    min_reward_risk: float = 2.0     # PTJ: target distance must be >= this x stop distance
    cooldown_hours: float = 24       # Livermore: no re-entry this soon after a stop-loss on the same coin
    risk_per_trade_pct: float = 0.01  # Turtles: size so a stop-loss costs 1% of equity; 0 = fixed MAX_POSITION_PCT
    trail_atr: float = 2.0           # Seykota: trailing stop 2 ATR below the highest price since entry; 0 = off
    db_path: str = str(ROOT / "data" / "journal.db")
    notes_dir: str = ""              # the Markdown notebook; empty = next to the journal (data/notes)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    daily_report_hour_utc: int = 6
    pulse_hours: float = 6           # a short "the desk is alive, this is what it did" message this often; 0 = off
    migrations: tuple = ()           # settings corrected at load time (old defaults), reported once on Telegram

    @property
    def use_claude(self) -> bool:
        return bool(self.anthropic_key) and not self.offline

    @property
    def use_free_ai(self) -> bool:
        from .free_ai import PRESETS
        if self.offline or self.free_ai_provider not in PRESETS:
            return False
        if self.free_ai_provider == "custom":
            return bool(self.free_ai_base_url and self.free_ai_models)
        if "{account_id}" in (self.free_ai_base_url or PRESETS[self.free_ai_provider].base_url):
            return False
        return bool(self.free_ai_key)

    def ai_pct_month(self) -> float:
        """The percentage-of-account cap on Claude's spend. The demo account is fake money while Claude is paid in real
        dollars, so tying the AI budget to a fake balance only starves the AI: on demo money there is no percentage
        cap unless AI_BUDGET_PCT_MONTH is set, and the flat AI_DAILY_BUDGET_USD is the limit. With real money (not
        supported in this phase) the default would be 0.25% of the account a month."""
        if self.ai_budget_pct_month is not None:
            return self.ai_budget_pct_month
        return 0.0 if self.mode in ("simulation", "testnet") else 0.25

    def problems(self) -> list[str]:
        """Settings that make the desk unable to do its job. Reported at start and in /health."""
        out = []
        if self.stop_loss_atr <= 0 or self.take_profit_atr <= 0:
            out.append("STOP_LOSS_ATR and TAKE_PROFIT_ATR must be above 0")
        elif "reward" in self.mentor_checks and self.take_profit_atr / self.stop_loss_atr < self.min_reward_risk - 1e-9:
            out.append(f"TAKE_PROFIT_ATR {self.take_profit_atr:g} / STOP_LOSS_ATR {self.stop_loss_atr:g} = "
                       f"{self.take_profit_atr / self.stop_loss_atr:.2f}:1 is below MIN_REWARD_RISK {self.min_reward_risk:g}: "
                       "the checklist's reward rule will refuse every buy")
        position = self.starting_balance * self.max_position_pct
        if self.max_open_positions < 1 or self.max_position_pct <= 0:
            out.append("MAX_OPEN_POSITIONS and MAX_POSITION_PCT must allow at least one position")
        elif position < 10:
            out.append(f"STARTING_BALANCE_USDT ${self.starting_balance:,.0f} x MAX_POSITION_PCT {self.max_position_pct:g} = "
                       f"${position:.2f} per position, below the $10 minimum: no order can be placed")
        elif position < 20:
            out.append(f"STARTING_BALANCE_USDT is only ${self.starting_balance:,.0f}: positions of ${position:.2f} sit next to "
                       f"the $10 minimum, so " + ("any loss" if position < 10.5 else f"a loss of {(1 - 10 / position):.0%}")
                       + " stops all buying. The demo uses fake money: "
                       "10,000 makes its results comparable with the tests")
        from .free_ai import PRESETS
        if self.free_ai_provider and self.free_ai_provider not in PRESETS:
            out.append(f"FREE_AI_PROVIDER={self.free_ai_provider!r} is not one of {', '.join(PRESETS)}: no free AI is used")
        elif self.free_ai_provider and not self.offline and not self.use_free_ai:
            need = ("FREE_AI_BASE_URL and FREE_AI_MODELS" if self.free_ai_provider == "custom" else
                    "FREE_AI_BASE_URL with your Cloudflare account id" if self.free_ai_provider == "cloudflare"
                    and not self.free_ai_base_url else "FREE_AI_API_KEY")
            out.append(f"FREE_AI_PROVIDER={self.free_ai_provider} needs {need} (desk setup): no free AI is used")
        if self.ai_order not in ("free-first", "claude-first"):
            out.append(f"AI_ORDER={self.ai_order!r} must be free-first or claude-first: using free-first")
        pct = self.ai_pct_month()
        ai = pct and self.starting_balance * pct / 100 / 30
        if self.use_claude and ai and min(ai, self.ai_daily_budget_usd or ai) < 0.10:
            out.append(f"Claude's budget is ${min(ai, self.ai_daily_budget_usd or ai):.2f} a day (AI_BUDGET_PCT_MONTH "
                       f"{pct:g}% a month of a ${self.starting_balance:,.0f} balance): about one call a "
                       "day, so Claude is effectively off and the free rules decide")
        return out

    @classmethod
    def load(cls, offline: bool = False) -> "Settings":
        load_dotenv(ENV_PATH)
        mode = os.getenv("TRADING_MODE", "simulation").strip().lower()
        if mode not in ("simulation", "testnet"):
            raise SystemExit(
                f"TRADING_MODE={mode!r} is not allowed. This phase supports only "
                "'simulation' or 'testnet' (no real money)."
            )
        db_path = Path(os.getenv("JOURNAL_DB", "data/journal.db"))
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        if offline:
            db_path = db_path.with_name(db_path.stem + "_offline" + db_path.suffix)
        take, note = legacy_take_profit(os.getenv("TAKE_PROFIT_ATR"), os.getenv("SETTINGS_VERSION"),
                                        os.getenv("MIN_REWARD_RISK"))
        return cls(
            migrations=(note,) if note else (),
            mode=mode,
            offline=offline,
            api_key=os.getenv("BINANCE_TESTNET_API_KEY", ""),
            api_secret=os.getenv("BINANCE_TESTNET_API_SECRET", ""),
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
            symbols=[s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()],
            interval=os.getenv("CANDLE_INTERVAL", "1h"),
            cycle_minutes=check_minutes(os.getenv("CYCLE_MINUTES"), os.getenv("SETTINGS_VERSION")),
            min_check_minutes=int(os.getenv("MIN_CHECK_MINUTES", 5)),
            max_check_minutes=int(os.getenv("MAX_CHECK_MINUTES", 60)),
            auto_coins=_b("AUTO_COINS", True),
            max_coins=int(os.getenv("MAX_COINS", 8)),
            scout_hours=_f("SCOUT_HOURS", 6),
            min_volume_musd=_f("MIN_VOLUME_MUSD", 10),
            news_refresh_minutes=int(os.getenv("NEWS_REFRESH_MINUTES", 30)),
            ai_daily_budget_usd=_f("AI_DAILY_BUDGET_USD", 5),
            ai_budget_pct_month=_f("AI_BUDGET_PCT_MONTH", 0) if os.getenv("AI_BUDGET_PCT_MONTH", "").strip() else None,
            free_ai_provider=os.getenv("FREE_AI_PROVIDER", "").strip().lower(),
            free_ai_key=os.getenv("FREE_AI_API_KEY", "").strip(),
            free_ai_models=tuple(m.strip() for m in os.getenv("FREE_AI_MODELS", "").split(",") if m.strip()),
            free_ai_base_url=os.getenv("FREE_AI_BASE_URL", "").strip(),
            free_ai_daily_requests=int(float(os.getenv("FREE_AI_DAILY_REQUESTS") or 0)),
            ai_order=os.getenv("AI_ORDER", "free-first").strip().lower() or "free-first",
            auto_update=_b("AUTO_UPDATE", True),
            lab_enabled=_b("LAB_ENABLED", True),
            head_trader=_b("HEAD_TRADER", True),
            lab_weekday=int(os.getenv("LAB_WEEKDAY", 0)),
            lab_alt_symbols=tuple(x.strip().upper() + ("" if x.strip().upper().endswith("USDT") else "USDT")
                                  for x in os.getenv("LAB_ALT_SYMBOLS", "BNB,XRP,ADA").split(",") if x.strip()),
            research_candidates=int(os.getenv("RESEARCH_CANDIDATES", 3)),
            auto_promote=_b("AUTO_PROMOTE", False),
            starting_balance=_f("STARTING_BALANCE_USDT", 10_000),
            max_position_pct=_f("MAX_POSITION_PCT", 0.10),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", 3)),
            max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 0.03),
            max_drawdown_pct=_f("MAX_DRAWDOWN_PCT", 0.10),
            brake_pause_days=int(os.getenv("BRAKE_PAUSE_DAYS", 14)),
            min_confidence=_f("MIN_CONFIDENCE", 0.60),
            stop_loss_atr=_f("STOP_LOSS_ATR", 2.0),
            take_profit_atr=take if take is not None else _f("TAKE_PROFIT_ATR", 4.0),
            mentor_checks=tuple(c.strip() for c in os.getenv("MENTOR_CHECKS", "trend200,btc,cooldown,reward").split(",")
                                if c.strip() and c.strip() != "none"),
            min_reward_risk=_f("MIN_REWARD_RISK", 2.0),
            cooldown_hours=_f("COOLDOWN_HOURS", 24),
            risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.01),
            trail_atr=_f("TRAIL_ATR", 2.0),
            db_path=str(db_path),
            notes_dir=os.getenv("NOTES_DIR", ""),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            daily_report_hour_utc=int(os.getenv("DAILY_REPORT_HOUR_UTC", 6)),
            pulse_hours=_f("PULSE_HOURS", 6),
        )
