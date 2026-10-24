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


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass
class Settings:
    mode: str = "simulation"
    offline: bool = False
    api_key: str = ""
    api_secret: str = ""
    anthropic_key: str = ""
    claude_model: str = "claude-opus-5-5"
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    interval: str = "1h"
    cycle_minutes: int = 60
    starting_balance: float = 10_000.0
    max_position_pct: float = 0.10
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.03
    min_confidence: float = 0.60
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 3.0
    db_path: str = str(ROOT / "data" / "journal.db")
    telegram_token: str = ""
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
            cycle_minutes=int(os.getenv("CYCLE_MINUTES", 60)),
            starting_balance=_f("STARTING_BALANCE_USDT", 10_000),
            max_position_pct=_f("MAX_POSITION_PCT", 0.10),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", 3)),
            max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 0.03),
            min_confidence=_f("MIN_CONFIDENCE", 0.60),
            stop_loss_atr=_f("STOP_LOSS_ATR", 2.0),
            take_profit_atr=_f("TAKE_PROFIT_ATR", 3.0),
            db_path=str(db_path),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            daily_report_hour_utc=int(os.getenv("DAILY_REPORT_HOUR_UTC", 6)),
        )
