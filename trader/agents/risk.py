"""Risk Manager agent: the safety officer. It can veto any trade.

It decides HOW MUCH to buy and WHERE to exit (stop-loss / take-profit), and
blocks trades that break the limits in .env.
"""
from __future__ import annotations

from ..config import Settings


class RiskManagerAgent:
    name = "risk"

    def __init__(self, settings: Settings):
        self.s = settings

    def review(self, decision: dict, symbol: str, price: float, atr: float, portfolio: dict) -> dict:
        """portfolio: {cash, equity, open_positions, day_start_equity}"""
        action = decision["action"]
        if action == "HOLD":
            return {"approved": False, "reason": "HOLD - nothing to do"}

        if action == "SELL":  # closing a position always allowed - it reduces risk
            return {"approved": True, "reason": "closing position"}

        # ---- BUY checks ----
        if decision["confidence"] < self.s.min_confidence:
            return {"approved": False, "reason": f"confidence {decision['confidence']:.2f} < minimum {self.s.min_confidence}"}
        refusal, size_factor = self._limits(portfolio)
        if refusal:
            return refusal

        stop = price - self.s.stop_loss_atr * atr
        take = price + self.s.take_profit_atr * atr
        if stop <= 0:
            return {"approved": False, "reason": "volatility too high for a sensible stop-loss"}
        budget = min(portfolio["equity"] * self.s.max_position_pct * size_factor, portfolio["cash"] * 0.98)
        if self.s.risk_per_trade_pct:
            # Turtle-style sizing: a stop-loss should cost a fixed small share of equity, so calm coins get
            # bigger positions and wild coins smaller ones; MAX_POSITION_PCT stays as the ceiling.
            risk_budget = portfolio["equity"] * self.s.risk_per_trade_pct / ((price - stop) / price)
            budget = min(budget, risk_budget)
        if budget < 10:
            return {"approved": False, "reason": f"not enough free cash (${portfolio['cash']:.2f})"}
        return {
            "approved": True,
            "reason": "within limits" if size_factor == 1 else "within limits (half size while recovering)",
            "quote_amount": round(budget, 2),
            "stop_loss": stop,
            "take_profit": take,
            "risk_pct_of_equity": round(budget * (price - stop) / price / portfolio["equity"] * 100, 3),
        }

    def review_trend(self, symbol: str, price: float, share: float, portfolio: dict) -> dict:
        """A buy of the daily trend rule (trader/trend.py): the same position, daily-loss and overall-loss limits;
        the size is the rule's share of the account (`share`, already capped at MAX_POSITION_PCT), the stop-loss the
        rule's emergency stop TREND_STOP_PCT below the price, the take-profit the highest Binance accepts (the desk
        moves it up as the price rises, so a trend is never cut short)."""
        from ..trend import TAKE_MULT, params
        refusal, size_factor = self._limits(portfolio)
        if refusal:
            return refusal
        budget = min(portfolio["equity"] * min(share, self.s.max_position_pct) * size_factor, portfolio["cash"] * 0.98)
        if budget < 10:
            return {"approved": False, "reason": f"not enough free cash (${portfolio['cash']:.2f})"
                    if portfolio["cash"] * 0.98 < 10 else f"its share of the account is below the $10 minimum (${budget:.2f})"}
        stop_pct = params(self.s)[2]
        return {
            "approved": True,
            "reason": "within limits" if size_factor == 1 else "within limits (half size while recovering)",
            "quote_amount": round(budget, 2),
            "stop_loss": price * (1 - stop_pct),
            "take_profit": price * TAKE_MULT,
            "risk_pct_of_equity": round(budget * stop_pct / portfolio["equity"] * 100, 3),
        }

    def _limits(self, portfolio: dict) -> tuple[dict | None, float]:
        """The limits every buy must pass: (refusal or None, size factor: 0.5 while recovering from the overall brake)."""
        if portfolio["open_positions"] >= self.s.max_open_positions:
            return {"approved": False, "reason": f"already {portfolio['open_positions']} open positions "
                                                 f"(max {self.s.max_open_positions})"}, 1.0
        day_pnl = portfolio["equity"] / portfolio["day_start_equity"] - 1
        if day_pnl <= -self.s.max_daily_loss_pct:
            return {"approved": False, "limit": "daily",
                    "reason": f"daily loss limit hit ({day_pnl:.2%}) - no new buys until tomorrow (UTC)"}, 1.0
        start = portfolio.get("starting_balance") or portfolio["equity"]
        drawdown = portfolio["equity"] / start - 1
        size_factor = 1.0
        if self.s.max_drawdown_pct:
            tripped_days = portfolio.get("brake_tripped_days")  # None = brake not active
            if drawdown <= -self.s.max_drawdown_pct and tripped_days is None:
                return {"approved": False, "limit": "overall", "trip": True,
                        "reason": f"overall loss limit hit ({drawdown:.2%} vs start) - no new buys for "
                                  f"{self.s.brake_pause_days} days, then half size until the account recovers"}, 1.0
            if tripped_days is not None:
                if tripped_days < self.s.brake_pause_days:
                    return {"approved": False, "limit": "overall",
                            "reason": f"overall loss limit: paused, {self.s.brake_pause_days - tripped_days:.0f} days left"}, 1.0
                if drawdown <= -self.s.max_drawdown_pct / 2:
                    size_factor = 0.5  # recovering: trade, but at half size
        return None, size_factor

    def trailing_stop(self, position: dict, high_water: float, atr: float) -> float | None:
        """Seykota / Turtles: as the price makes new highs, the stop follows it up (never down).
        Returns the new stop when it should move by at least a quarter ATR, else None."""
        if not self.s.trail_atr or atr <= 0:
            return None
        new_stop = high_water - self.s.trail_atr * atr
        return new_stop if new_stop >= position["stop_loss"] + 0.25 * atr else None

    @staticmethod
    def trend_exit(position: dict, price: float) -> str | None:
        """The daily trend rule's own safety net: its emergency stop only (its take-profit is a placeholder that is
        raised with the price, never a reason to sell)."""
        return "stop-loss" if price <= position["stop_loss"] else None

    @staticmethod
    def exit_signal(position: dict, price: float) -> str | None:
        if price <= position["stop_loss"]:
            return "stop-loss"
        if price >= position["take_profit"]:
            return "take-profit"
        return None
