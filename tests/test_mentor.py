"""Master traders' checklist, trailing stops and volatility-based sizing."""
from types import SimpleNamespace

import pytest

from trader.agents.execution import ExecutionAgent
from trader.agents.mentor import ALL_CHECKS, MentorAgent, daily_closes
from trader.agents.risk import RiskManagerAgent
from trader.backtest import Backtest
from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator
from tests.test_backtest import synthetic
from tests.test_protection import FakeBroker


def candles(price, n=200):
    return [{"time": i * 3_600_000, "open": price, "high": price, "low": price, "close": price, "volume": 1}
            for i in range(n)]


def test_each_check_passes_and_fails_for_the_documented_reason():
    m = MentorAgent(ALL_CHECKS, min_reward_risk=2.0, cooldown_hours=24)
    rising = [100 + i * 0.5 for i in range(260)]          # price well above both averages
    btc_up = [50_000 + i * 50 for i in range(260)]
    now = 1_000 * 3_600_000
    r = m.review("ETHUSDT", candles(230), rising, price=230, stop=226, take=238, atr=2, btc_daily=btc_up,
                 last_stop_ms=None, now_ms=now)
    assert r["pass"] and r["failed"] == [] and set(r["checks"]) == set(ALL_CHECKS)
    assert r["checks"]["trend200"]["source"] == "Paul Tudor Jones"

    falling = [300 - i * 0.5 for i in range(260)]         # price below its averages
    r = m.review("ETHUSDT", candles(170), falling, price=170, stop=166, take=178, atr=2, btc_daily=btc_up,
                 last_stop_ms=None, now_ms=now)
    assert {"trend200", "alignment"} <= set(r["failed"]) and "btc" not in r["failed"]

    btc_down = [90_000 - i * 100 for i in range(260)]
    r = m.review("ETHUSDT", candles(230), rising, 230, 226, 238, 2, btc_down, None, now)
    assert r["failed"] == ["btc"]
    r = m.review("BTCUSDT", candles(230), rising, 230, 226, 238, 2, btc_down, None, now)
    assert "btc" not in r["failed"]  # Bitcoin itself only needs its own trend

    r = m.review("ETHUSDT", candles(230), rising, 230, 226, 236, 2, btc_up, None, now)   # 6 up vs 4 down = 1.5:1
    assert r["failed"] == ["reward"]
    r = m.review("ETHUSDT", candles(230), rising, 230, 226, 238, 2, btc_up, None, now)   # exactly 2:1 passes
    assert "reward" not in r["failed"]

    r = m.review("ETHUSDT", candles(230), rising, 230, 226, 238, 2, btc_up, now - 6 * 3_600_000, now)
    assert r["failed"] == ["cooldown"] and "6 h ago" in r["checks"]["cooldown"]["detail"]
    r = m.review("ETHUSDT", candles(230), rising, 230, 226, 238, 2, btc_up, now - 30 * 3_600_000, now)
    assert "cooldown" not in r["failed"]

    spike = candles(230)
    spike[-1] = {**spike[-1], "close": 260}  # 15 ATR above the 20-hour average: chasing
    r = m.review("ETHUSDT", spike, rising, 260, 256, 268, 2, btc_up, None, now)
    assert "overextended" in r["failed"]


def test_unknown_history_never_blocks_and_summary_is_readable():
    m = MentorAgent(ALL_CHECKS)
    r = m.review("ETHUSDT", candles(100), daily=[100.0] * 30, price=100, stop=98, take=104, atr=1, btc_daily=None,
                 last_stop_ms=None, now_ms=0)
    assert r["pass"] and r["checks"]["trend200"]["ok"] is None and r["checks"]["btc"]["ok"] is None
    text = MentorAgent.summary(r)
    assert "? trend200" in text and "[Paul Tudor Jones]" in text and "✓ reward" in text


def test_daily_closes_from_hourly_candles():
    hourly = [{"time": i * 3_600_000, "close": float(i)} for i in range(50)]  # 2 full days + 2 hours
    assert daily_closes(hourly) == [23.0, 47.0, 49.0]


PORT = {"cash": 10_000, "equity": 10_000, "open_positions": 0, "day_start_equity": 10_000, "starting_balance": 10_000}


def test_turtle_sizing_risks_a_fixed_share_of_equity():
    r = RiskManagerAgent(Settings(risk_per_trade_pct=0.01, max_position_pct=0.15))
    buy = {"action": "BUY", "confidence": 0.9}
    calm = r.review(buy, "BTCUSDT", price=100, atr=0.5, portfolio=PORT)      # stop 1% away -> 100% would be needed
    assert calm["quote_amount"] == 1500                                       # capped at 15%
    wild = r.review(buy, "SOLUSDT", price=100, atr=2.5, portfolio=PORT)      # stop 5% away -> 20% wanted
    assert wild["quote_amount"] == 1500                                       # still capped at 15%
    wilder = r.review(buy, "PEPEUSDT", price=100, atr=5, portfolio=PORT)     # stop 10% away -> 10% of equity
    assert wilder["quote_amount"] == 1000 and wilder["take_profit"] == 120 and wilder["stop_loss"] == 90
    assert wilder["risk_pct_of_equity"] == pytest.approx(1.0)
    fixed = RiskManagerAgent(Settings(risk_per_trade_pct=0.0, max_position_pct=0.10))
    assert fixed.review(buy, "PEPEUSDT", 100, 5, PORT)["quote_amount"] == 1000


def test_trailing_stop_only_moves_up_and_in_steps():
    r = RiskManagerAgent(Settings(trail_atr=2.0))
    pos = {"stop_loss": 96.0}
    assert r.trailing_stop(pos, high_water=100, atr=2) is None          # 100-4 = 96: no move
    assert r.trailing_stop(pos, high_water=100.4, atr=2) is None        # +0.4 < a quarter ATR: not worth re-placing
    assert r.trailing_stop(pos, high_water=101, atr=2) == 97.0          # +1: move
    assert r.trailing_stop(pos, high_water=90, atr=2) is None           # never down
    assert RiskManagerAgent(Settings(trail_atr=0)).trailing_stop(pos, 150, 2) is None


@pytest.fixture
def bot():
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=(), trail_atr=2.0)
    b = Orchestrator(s, Journal(":memory:", s.starting_balance))
    b.broker = FakeBroker(lambda sym: b.last_price[sym])
    b.execution = ExecutionAgent(b.broker)
    return b


def test_trailing_stop_replaces_the_exchange_orders(bot):
    bot.last_price["BTCUSDT"] = 100.0
    bot.journal.open_position("BTCUSDT", {"qty": 1.0, "avg_price": 100.0, "quote": 100.0}, stop=96, take=108)
    bot._protect("BTCUSDT", bot.journal.positions()["BTCUSDT"])
    bot.broker.calls.clear()
    pos, _ = bot._trail("BTCUSDT", bot.journal.positions()["BTCUSDT"], price=100.4, atr=2)
    assert pos["stop_loss"] == 96 and pos["high_water"] == 100.4 and bot.broker.calls == []   # not enough yet
    pos, _ = bot._trail("BTCUSDT", pos, price=103, atr=2)
    assert pos["stop_loss"] == 99 and pos["high_water"] == 103
    assert bot.broker.calls == ["unprotect", "protect"] and pos["protected"] == "exchange"  # re-placed on Binance
    pos, _ = bot._trail("BTCUSDT", pos, price=98, atr=2)
    assert pos["stop_loss"] == 99  # never lowered

    # the exchange closed it while we were moving the stop: recorded as an exit, not a crash
    bot.broker.next_unprotect = {"state": "exited", "reason": "take-profit",
                                 "fill": {"status": "FILLED", "order_id": "x", "qty": 1.0, "avg_price": 108.0,
                                          "quote": 108.0, "fee_quote": 0}}
    pos, exit_result = bot._trail("BTCUSDT", pos, price=110, atr=2)
    assert pos is None and exit_result["trade"]["exit_reason"] == "take-profit (exchange)"


def test_checklist_vetoes_a_buy_in_the_live_cycle():
    s = Settings(offline=True, symbols=["ETHUSDT"], mentor_checks=("reward",), take_profit_atr=3.0)  # 1.5:1 fails
    b = Orchestrator(s, Journal(":memory:", s.starting_balance))
    b.broker = FakeBroker(lambda sym: b.last_price[sym])
    b.execution = ExecutionAgent(b.broker)
    b.decision.run = lambda sym, reports, holding, use_ai=True, threshold=None: {"action": "BUY", "confidence": 0.9, "reasoning": "r",
                                                    "source": "rules", "combined_score": 0.5}
    out = b.run_cycle()
    assert out[0]["action"] == "BUY" and not out[0]["approved"] and "checklist: reward" in out[0]["risk"]
    assert "✗ reward" in out[0]["mentor"] and b.journal.positions() == {} and "buy" not in b.broker.calls


def test_backtest_applies_checklist_trailing_and_cooldown():
    history = {"AAAUSDT": synthetic("AAAUSDT", 1500, 1, drift=0.0005), "BTCUSDT": synthetic("BTCUSDT", 1500, 2, drift=0.0005)}
    plain = Settings(offline=True, symbols=list(history), mentor_checks=(), trail_atr=0, risk_per_trade_pct=0)
    disciplined = Settings(offline=True, symbols=list(history), mentor_checks=("trend200", "btc", "cooldown", "reward"),
                           trail_atr=2.0, risk_per_trade_pct=0.01, take_profit_atr=4.0)
    r_plain, r_disc = Backtest(plain, history).run(), Backtest(disciplined, history).run()
    assert r_plain["mentor_vetoes"] == {} and r_plain["trades"] > 0
    assert r_disc["trades"] <= r_plain["trades"] and set(r_disc["mentor_vetoes"]) <= {"trend200", "btc", "cooldown", "reward"}
