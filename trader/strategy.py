"""Strategy cards: a complete trading configuration as data, so the desk can create, test,
promote and roll back strategies on its own, within hard bounds.

A card is what the laboratory (trader/lab.py) evaluates and what the live desk runs: the
"champion" card is applied on top of the owner's settings at every cycle. Challengers are
proposed by the research agent (or generated as neighbours of the champion), must pass
the lab's exam on several periods and coin sets, then a forward test on data that did not
exist when they were proposed, and are promoted only within BOUNDS and (by default) with
the owner's approval on Telegram.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field

from .agents.mentor import ALL_CHECKS
from .config import Settings

# What a self-generated strategy may vary, and how far. Everything else is fixed by the owner.
BOUNDS = {
    "buy_threshold": (0.15, 0.60),
    "stop_loss_atr": (1.0, 4.0),
    "take_profit_atr": (2.0, 12.0),
    "trail_atr": (0.0, 5.0),
    "cooldown_hours": (0.0, 96.0),
    "min_reward_risk": (1.0, 4.0),
    "risk_per_trade_pct": (0.0, 0.01),     # may only shrink positions (the sizing review was unambiguous)
    "max_position_pct": (0.02, 0.10),      # never above the owner's 10%
    "max_open_positions": (1, 3),
    "interval": ("1h", "2h", "4h"),
    "weights": (0.0, 1.0),
    "coin_thresholds": (0.15, 0.60),   # per-coin buy thresholds from the threshold study, same range as the common one
}
MAX_CHANGES = 2            # a challenger may differ from the champion in at most this many fields (anti-overfitting)
INTERVALS = ("1h", "2h", "4h")


@dataclass
class StrategyCard:
    name: str
    buy_threshold: float = 0.35
    stop_loss_atr: float = 2.0
    take_profit_atr: float = 4.0
    trail_atr: float = 2.0
    checks: tuple = ("trend200", "btc", "cooldown", "reward")
    cooldown_hours: float = 24.0
    min_reward_risk: float = 2.0
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.10
    max_open_positions: int = 3
    interval: str = "1h"
    weights: dict = field(default_factory=lambda: {"technical": 0.50, "prediction": 0.30, "news": 0.10, "forex": 0.10})
    coin_thresholds: dict = field(default_factory=dict)   # symbol -> buy threshold (sell = its mirror); others use buy_threshold
    rationale: str = ""
    source: str = ""                 # who proposed it: "owner", "research:<url>", "neighbour"
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))

    # ---- identity ----
    @property
    def id(self) -> str:
        """Short stable id from the trading-relevant fields (not the name or rationale)."""
        body = json.dumps(self.params(), sort_keys=True)
        return hashlib.sha1(body.encode()).hexdigest()[:8]

    def params(self) -> dict:
        d = dataclasses.asdict(self)
        for k in ("name", "rationale", "source", "created_at"):
            d.pop(k)
        d["checks"] = sorted(d["checks"])
        return d

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["checks"] = list(self.checks)
        d["id"] = self.id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyCard":
        fields = {f.name for f in dataclasses.fields(cls)}
        clean = {k: v for k, v in d.items() if k in fields}
        clean["checks"] = tuple(clean.get("checks", ()))
        return cls(**clean)

    @classmethod
    def from_settings(cls, s: Settings, name: str = "owner settings") -> "StrategyCard":
        from .agents import decision as dmod
        return cls(name=name, buy_threshold=dmod.BUY_THRESHOLD, stop_loss_atr=s.stop_loss_atr,
                   take_profit_atr=s.take_profit_atr, trail_atr=s.trail_atr, checks=tuple(s.mentor_checks),
                   cooldown_hours=s.cooldown_hours, min_reward_risk=s.min_reward_risk,
                   risk_per_trade_pct=s.risk_per_trade_pct, max_position_pct=s.max_position_pct,
                   max_open_positions=s.max_open_positions, interval=s.interval, weights=dict(dmod.WEIGHTS),
                   coin_thresholds=dict(dmod.COIN_THRESHOLDS), source="owner")

    # ---- validation ----
    def violations(self) -> list[str]:
        out = []
        for key, bound in BOUNDS.items():
            if key == "interval":
                if self.interval not in bound:
                    out.append(f"interval {self.interval} not in {bound}")
            elif key == "weights":
                if set(self.weights) != {"technical", "prediction", "news", "forex"}:
                    out.append("weights must name technical, prediction, news, forex")
                elif any(not (0 <= v <= 1) for v in self.weights.values()) or abs(sum(self.weights.values()) - 1) > 1e-6:
                    out.append("weights must be between 0 and 1 and add up to 1")
            elif key == "coin_thresholds":
                lo, hi = bound
                for sym, v in self.coin_thresholds.items():
                    if not (isinstance(sym, str) and sym.endswith("USDT")):
                        out.append(f"coin threshold for {sym!r}: not a USDT pair")
                    elif not (lo <= float(v) <= hi):
                        out.append(f"coin threshold {sym}={v} outside {lo}..{hi}")
            else:
                lo, hi = bound
                v = getattr(self, key)
                if not (lo <= v <= hi):
                    out.append(f"{key}={v} outside {lo}..{hi}")
        bad = set(self.checks) - set(ALL_CHECKS)
        if bad:
            out.append(f"unknown checks {sorted(bad)}")
        if self.take_profit_atr < self.stop_loss_atr:
            out.append("take-profit must be at least the stop distance")
        elif "reward" in self.checks and self.take_profit_atr / self.stop_loss_atr < self.min_reward_risk - 1e-9:
            out.append(f"target {self.take_profit_atr:g} ATR / stop {self.stop_loss_atr:g} ATR is below the reward rule's "
                       f"{self.min_reward_risk:g}:1: no buy could ever pass the checklist")
        return out

    def diff(self, other: "StrategyCard") -> list[str]:
        """Fields that differ from another card (used for the MAX_CHANGES rule and for reports)."""
        a, b = self.params(), other.params()
        return [k for k in a if a[k] != b[k]]

    def describe_diff(self, other: "StrategyCard") -> str:
        a, b = self.params(), other.params()
        parts = []
        for k in self.diff(other):
            if k == "coin_thresholds":
                coins = sorted(set(a[k]) | set(b[k]))
                parts.append("coin thresholds: " + ", ".join(
                    f"{c.removesuffix('USDT')} {b[k].get(c, other.buy_threshold):g} → {a[k].get(c, self.buy_threshold):g}"
                    for c in coins))
            else:
                parts.append(f"{k}: {b[k]} → {a[k]}")
        return ", ".join(parts) or "identical"

    # ---- apply ----
    def settings(self, base: Settings) -> Settings:
        """A copy of the owner's settings with this card's trading parameters applied."""
        return dataclasses.replace(
            base, stop_loss_atr=self.stop_loss_atr, take_profit_atr=self.take_profit_atr, trail_atr=self.trail_atr,
            mentor_checks=tuple(self.checks), cooldown_hours=self.cooldown_hours, min_reward_risk=self.min_reward_risk,
            risk_per_trade_pct=self.risk_per_trade_pct, max_position_pct=min(self.max_position_pct, base.max_position_pct),
            max_open_positions=min(self.max_open_positions, base.max_open_positions), interval=self.interval)

    def apply_decision_rules(self) -> None:
        """Thresholds and weights live in the decision module (shared by live desk and backtest)."""
        from .agents import decision as dmod
        dmod.BUY_THRESHOLD = self.buy_threshold
        dmod.SELL_THRESHOLD = -self.buy_threshold
        dmod.WEIGHTS.clear()
        dmod.WEIGHTS.update(self.weights)
        dmod.COIN_THRESHOLDS.clear()
        dmod.COIN_THRESHOLDS.update({k: float(v) for k, v in self.coin_thresholds.items()})


def summary_line(card: StrategyCard) -> str:
    return (f"{card.name} [{card.id}]: buy≥{card.buy_threshold:g}, stop {card.stop_loss_atr:g} ATR, "
            f"target {card.take_profit_atr:g} ATR, trail {card.trail_atr:g}, checks {','.join(card.checks) or 'none'}, "
            f"risk {card.risk_per_trade_pct * 100:g}%, cap {card.max_position_pct * 100:g}%, "
            f"max {card.max_open_positions} pos, {card.interval} candles"
            + (f", own thresholds for {len(card.coin_thresholds)} coin(s)" if card.coin_thresholds else ""))


def thresholds_line(card: StrategyCard) -> str:
    """'BTC 0.4, ETH 0.35 (others 0.35)' for messages and the notebook."""
    if not card.coin_thresholds:
        return f"all coins {card.buy_threshold:g}"
    return ", ".join(f"{k.removesuffix('USDT')} {v:g}" for k, v in sorted(card.coin_thresholds.items())) \
        + f" (others {card.buy_threshold:g})"
