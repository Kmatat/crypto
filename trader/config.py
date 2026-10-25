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


SETTINGS_VERSION = "2"  # 2 = 15-minute checks + AI coin scout


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
    ai_budget_pct_month: float = 0.25  # ... and never more than this % of the account per month (0 = off)
    auto_update: bool = True         # install new code from GitHub automatically (else only notify)
    # ---- strategy laboratory (trader/lab.py): the desk proposes, tests and promotes strategy cards ----
    lab_enabled: bool = True
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
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    daily_report_hour_utc: int = 6

    @property
    def use_claude(self) -> bool:
        return bool(self.anthropic_key) and not self.offline

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
        return cls(
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
            ai_budget_pct_month=_f("AI_BUDGET_PCT_MONTH", 0.25),
            auto_update=_b("AUTO_UPDATE", True),
            lab_enabled=_b("LAB_ENABLED", True),
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
            take_profit_atr=_f("TAKE_PROFIT_ATR", 4.0),
            mentor_checks=tuple(c.strip() for c in os.getenv("MENTOR_CHECKS", "trend200,btc,cooldown,reward").split(",")
                                if c.strip() and c.strip() != "none"),
            min_reward_risk=_f("MIN_REWARD_RISK", 2.0),
            cooldown_hours=_f("COOLDOWN_HOURS", 24),
            risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.01),
            trail_atr=_f("TRAIL_ATR", 2.0),
            db_path=str(db_path),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            daily_report_hour_utc=int(os.getenv("DAILY_REPORT_HOUR_UTC", 6)),
        )
