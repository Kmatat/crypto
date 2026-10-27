"""1.7.0: the daily trend strategy (STRATEGY=trend): the rule, its replay, and the desk trading it."""
import datetime as dt
import math

from trader import trend
from trader.agents.execution import ExecutionAgent, PaperBroker
from trader.agents.risk import RiskManagerAgent
from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator

DAY = trend.DAY_MS


def today_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) // DAY * DAY


def candles(closes, forming=None, end_ms=None):
    """Finished daily candles ending yesterday, plus the forming day (Binance's last row)."""
    end = today_ms() if end_ms is None else end_ms
    n = len(closes)
    rows = [{"time": end - (n - i) * DAY, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0}
            for i, c in enumerate(closes)]
    if forming is not None:
        rows.append({"time": end, "open": closes[-1], "high": forming, "low": forming, "close": forming, "volume": 1.0})
    return rows


def rising(n=130, start=100.0, step=0.01):
    return [start * (1 + step) ** i for i in range(n)]


def wavy(n=130, start=100.0, step=0.01, swing=0.03):
    return [start * (1 + step) ** i * (1 + swing * math.sin(i)) for i in range(n)]


# ---- the rule ----
def test_the_forming_day_is_never_decided_on():
    rows = candles(rising(80), forming=1.0)
    assert trend.finished(rows)[-1]["close"] == rows[-2]["close"]


def test_signal_windows():
    closes = wavy(130)
    g = trend.signal(closes, 50)
    assert g["on"] and abs(g["sma"] - sum(closes[-50:]) / 50) < 1e-9
    assert abs(g["momentum"] - (closes[-1] / closes[-61] - 1)) < 1e-12
    moves = [math.log(closes[i] / closes[i - 1]) for i in range(100, 130)]
    mean = sum(moves) / 30
    assert abs(g["vol"] - math.sqrt(sum((m - mean) ** 2 for m in moves) / 29) * math.sqrt(365)) < 1e-12
    assert trend.signal(closes[:60], 50) is None            # a recent listing: not enough history for 60-day momentum
    assert not trend.signal(list(reversed(closes)), 50)["on"]


def test_share_is_smaller_for_wild_coins_and_capped():
    assert trend.share({"vol": 0.25}, 8, 0.5, 0.10) == 0.10                 # 1/8 = 12.5%, capped at 10%
    assert abs(trend.share({"vol": 1.0}, 8, 0.5, 0.10) - 0.0625) < 1e-12   # 1/8 x 0.5 / 1.0
    assert trend.share({"vol": 0.0}, 8, 0.5, 0.10) == 0.10
    assert trend.share({"vol": 0.5}, 3, 0.5, 0.10) == 0.10                 # never above MAX_POSITION_PCT


def test_after_a_stop_the_coin_waits_for_its_signal_to_reset():
    closes = rising(120)
    rows = candles(closes)
    stop_ms = rows[-3]["time"] + DAY // 2                                   # fired three days ago
    assert not trend.armed(rows, 50, stop_ms)                               # still above its average since
    reset = closes[:-2] + [closes[-3] * 0.5, closes[-3] * 1.2]              # a close below the average, then above
    assert trend.armed(candles(reset), 50, stop_ms)
    assert trend.armed(rows, 50, None) and trend.armed(rows, 50, rows[0]["time"] - DAY)


def test_out_of_range_settings_fall_back_to_the_tested_values():
    s = Settings(offline=True, strategy="trend", trend_sma_days=3, trend_vol_target=9, trend_stop_pct=0.9)
    assert trend.params(s) == trend.TESTED
    assert any("tested values" in p for p in s.problems())
    assert trend.params(Settings(offline=True, trend_sma_days=100)) == (100, 0.5, 0.25)


# ---- the replay ----
def test_the_replay_rides_a_trend_and_sells_after_the_close_below():
    up = rising(140, 100, 0.01)
    down = [up[-1] * 0.97 ** i for i in range(1, 40)]
    history = {"AAAUSDT": candles(up + down)}
    s = Settings(offline=True, symbols=["AAAUSDT"], strategy="trend", max_open_positions=1, max_position_pct=1.0,
                 starting_balance=10_000)
    r = trend.backtest(s, history, start_ms=history["AAAUSDT"][70]["time"])
    assert r["trades"] == 1 and r["exits"]["signal"] == 1 and r["return_pct"] > 0
    assert r["basket_hold_pct"] is not None


def test_the_replay_stops_out_and_waits_for_the_reset():
    s = Settings(offline=True, symbols=["AAAUSDT"], strategy="trend", max_open_positions=1, max_position_pct=1.0)
    rows = candles(rising(170, 100, 0.01))
    rows[140]["low"] = 1.0                 # a wick through the emergency stop; the day still closes above its average
    r = trend.backtest(s, {"AAAUSDT": rows}, start_ms=rows[70]["time"])
    assert r["exits"]["stop-loss"] == 1 and r["trades"] == 1 and r["still_open"] == 0   # not bought straight back
    closes = rising(170, 100, 0.01)
    closes[150] = closes[150] * 0.5        # ten days later a close below the average resets it
    reset = candles(closes)
    reset[140]["low"] = 1.0
    r = trend.backtest(s, {"AAAUSDT": reset}, start_ms=reset[70]["time"])
    assert r["exits"]["stop-loss"] == 1 and r["still_open"] == 1                         # bought again after it


# ---- the risk check ----
def folio(**kw):
    return {"cash": 10_000.0, "equity": 10_000.0, "open_positions": 0, "day_start_equity": 10_000.0,
            "starting_balance": 10_000.0, "brake_tripped_days": None, **kw}


def test_review_trend_sizes_by_share_with_an_emergency_stop():
    risk = RiskManagerAgent(Settings(offline=True, strategy="trend", max_open_positions=8))
    v = risk.review_trend("BTCUSDT", 100.0, 0.0625, folio())
    assert v["approved"] and v["quote_amount"] == 625.0
    assert abs(v["stop_loss"] - 75.0) < 1e-9 and abs(v["take_profit"] - 190.0) < 1e-9
    assert not risk.review_trend("BTCUSDT", 100.0, 0.0625, folio(open_positions=8))["approved"]
    tripped = risk.review_trend("BTCUSDT", 100.0, 0.0625, folio(equity=8_900.0, cash=8_900.0, day_start_equity=8_900.0))
    assert tripped.get("trip") and not tripped["approved"]
    assert risk.review_trend("BTCUSDT", 100.0, 0.5, folio())["quote_amount"] == 1000.0   # MAX_POSITION_PCT caps it


# ---- the desk trading it ----
class FakeMarket:
    """Daily candles per coin and a live price; nothing moves between checks unless a test says so."""
    client = None
    interval = "1h"

    def __init__(self, series):
        self.series = series
        self.prices = {sym: rows[-1]["close"] for sym, rows in series.items()}

    def daily_candles(self, sym, limit=130):
        if sym not in self.series:
            raise ConnectionError("no data")
        return self.series[sym][-limit:]

    def price(self, sym):
        return self.prices[sym]

    def range_since(self, sym, since_ms):
        return None


def trend_desk(series, symbols=None, positions=8, **kw):
    symbols = symbols or list(series)
    j = Journal(":memory:", 10_000)
    s = Settings(offline=True, symbols=symbols, strategy="trend", max_open_positions=positions,
                 starting_balance=10_000, **kw)
    bot = Orchestrator(s, j)
    bot.market = FakeMarket(series)
    bot.execution = ExecutionAgent(PaperBroker(lambda sym: bot.last_price[sym], range_fn=bot.market.range_since))
    return bot, j


def test_the_daily_step_buys_the_strongest_coins_once_a_day():
    series = {"AAAUSDT": candles(wavy(130, 100, 0.02)), "BBBUSDT": candles(wavy(130, 100, 0.01)),
              "CCCUSDT": candles(list(reversed(wavy(130))))}
    bot, j = trend_desk(series, positions=1)
    out = bot.run_cycle()
    assert list(j.positions()) == ["AAAUSDT"]                     # the stronger 60-day gain gets the only slot
    assert any(r.get("action") == "BUY" and r.get("source") == "trend" for r in out)
    assert j.get("trend_day") == (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    sources = {r[0] for r in j.db.execute("SELECT source FROM decisions")}
    assert sources == {"trend"}
    n = j.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    bot.run_cycle()                                               # a later check the same day decides nothing again
    assert j.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == n
    p = j.positions()["AAAUSDT"]
    assert abs(p["stop_loss"] / p["entry_price"] - 0.75) < 1e-9 and p["protected"] == "emulated"
    assert bot.card is None and bot.s.max_open_positions == 1     # no strategy card in trend mode


def test_a_coin_that_closes_below_its_average_is_sold_the_next_day():
    up = wavy(130, 100, 0.02)
    bot, j = trend_desk({"AAAUSDT": candles(up)})
    bot.run_cycle()
    assert "AAAUSDT" in j.positions()
    bot.market.series["AAAUSDT"] = candles(up + [up[-1] * 0.5])  # the next day closes far below the average
    bot.market.prices["AAAUSDT"] = up[-1] * 0.5
    j.set("trend_day", "2000-01-01")                              # as if a new day had begun
    out = bot.run_cycle()
    assert "AAAUSDT" not in j.positions()
    assert any(r.get("action") == "SELL" and "average" in str(r.get("reason")) for r in out)


def test_switching_to_trend_sells_other_coins_and_adopts_the_rest():
    series = {"AAAUSDT": candles(wavy(130, 100, 0.02))}
    bot, j = trend_desk(series)
    bot.market.prices["UNIUSDT"] = 8.0
    j.open_position("UNIUSDT", {"qty": 10.0, "avg_price": 8.0, "quote": 80.0}, 7.6, 8.8)       # a scout pick
    price = series["AAAUSDT"][-1]["close"]
    j.open_position("AAAUSDT", {"qty": 1.0, "avg_price": price, "quote": price}, price * 0.97, price * 1.06)  # hourly
    bot.run_cycle()
    assert "UNIUSDT" not in j.positions()
    a = j.positions()["AAAUSDT"]
    assert abs(a["stop_loss"] / price - 0.75) < 1e-9 and abs(a["take_profit"] / price - 1.9) < 1e-9
    assert "AAAUSDT" in (j.get("trend_positions") or {})


def test_the_take_profit_moves_up_with_the_price():
    up = wavy(130, 100, 0.02)
    bot, j = trend_desk({"AAAUSDT": candles(up)})
    bot.run_cycle()
    take = j.positions()["AAAUSDT"]["take_profit"]
    bot.market.prices["AAAUSDT"] = take / 1.25                    # within 30% of the take-profit
    bot.run_cycle()
    assert abs(j.positions()["AAAUSDT"]["take_profit"] - take / 1.25 * 1.9) < 1e-6


def test_after_an_emergency_stop_the_coin_is_not_bought_straight_back():
    up = wavy(130, 100, 0.02)
    bot, j = trend_desk({"AAAUSDT": candles(up)})
    bot.run_cycle()
    p = j.positions()["AAAUSDT"]
    fill = {"ok": True, "order_id": "s", "qty": p["qty"], "avg_price": p["stop_loss"], "quote": p["qty"] * p["stop_loss"],
            "fee_quote": 0.0}
    j.close_position("AAAUSDT", fill, "stop-loss (emulated)")
    j.set("trend_day", "2000-01-01")
    bot.run_cycle()                                               # still above its average: waits
    assert "AAAUSDT" not in j.positions()
    why = j.db.execute("SELECT reasoning FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "emergency stop" in why


def test_unreadable_daily_prices_are_retried_then_left_for_tomorrow():
    bot, j = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02))}, symbols=["AAAUSDT", "ZZZUSDT"])
    bot.market.prices["ZZZUSDT"] = 1.0
    for _ in range(3):
        bot.run_cycle()
        assert j.get("trend_day") is None
    bot.run_cycle()
    assert j.get("trend_day") is not None and "AAAUSDT" in j.positions()


def test_trend_mode_runs_without_scout_head_or_card():
    bot, j = trend_desk({"AAAUSDT": candles(wavy(130))}, auto_coins=True, head_trader=True)
    assert bot.scout is None and bot.head is None and bot.apply_champion() is None


def test_too_few_position_slots_are_reported():
    s = Settings(offline=True, strategy="trend", symbols=list(trend.TESTED_COINS), max_open_positions=3)
    assert any("MAX_OPEN_POSITIONS 3" in p for p in s.problems())
    assert not [p for p in Settings(offline=True, strategy="trend", symbols=list(trend.TESTED_COINS),
                                    max_open_positions=8).problems() if "MAX_OPEN_POSITIONS" in p]
    assert any("STRATEGY='daily'" in p for p in Settings(offline=True, strategy="daily").problems())


def test_trend_goals_and_messages():
    from trader.notify import trade_messages
    from trader.report import goals, goals_text, performance
    j = Journal(":memory:", 10_000)
    p = performance(j, {})
    g = goals(j, p, {}, trend=True)
    assert g["checks"]["closed_trades"][1] == 30 and g["checks"]["weeks"][1] == 26
    assert "daily trend rule" in goals_text(g, p)
    assert goals(j, p, {})["checks"]["closed_trades"][1] == 150
    msg = trade_messages([{"symbol": "BTCUSDT", "action": "BUY", "source": "trend", "confidence": 1.0,
                           "reasoning": "buys 6.2% of the account", "stop_loss": 75.0, "take_profit": 190.0,
                           "order": {"ok": True, "qty": 1.0, "avg_price": 100.0, "quote": 100.0}}])[0][1]
    assert "daily trend rule" in msg and "Confidence" not in msg and "no profit target" in msg
    sell = trade_messages([{"symbol": "BTCUSDT", "action": "SELL", "reason": "trend: closed at or below its 50-day average",
                            "order": {"ok": True, "qty": 1.0, "avg_price": 90.0, "quote": 90.0}}])[0][1]
    assert "daily trend rule: closed at or below" in sell


def test_the_trend_goals_count_from_the_day_the_rule_started():
    from trader.report import goals, goals_text, performance
    j = Journal(":memory:", 10_000)
    old = "2026-09-01T00:00:00+00:00"
    j.db.execute("INSERT INTO equity VALUES (?,?,?)", (old, 10_000, 10_000))
    for i in range(4):   # the hourly strategy's four losing trades
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, pnl, "
                     "pnl_pct, exit_reason) VALUES ('UNIUSDT', ?, ?, 1, 1, 1, 100, -10, -10, 'stop-loss')", (old, old))
    j.set("cash", 9_960.0)
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="seconds")
    j.set("trend_start", {"ts": start, "equity": 9_960.0, "prices": {"BTCUSDT": 100.0}})
    j.db.execute("INSERT INTO equity VALUES (?,?,?)", (start, 9_960.0, 9_960.0))
    j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, pnl, "
                 "pnl_pct, exit_reason) VALUES ('BTCUSDT', ?, ?, 1, 1, 1, 100, 50, 50, 'trend: signal')", (start, start))
    j.set("cash", 10_010.0)
    j.db.commit()
    p = performance(j, {})
    g = goals(j, p, {"BTCUSDT": 110.0}, trend=True)
    assert g["checks"]["closed_trades"][0] == 1 and g["perf"]["profit_factor"] is None   # no losing trend trade yet
    assert abs(g["perf"]["return_pct"] - (10_010 / 9_960 - 1) * 100) < 0.01
    assert g["vs_btc_pct"] == 10.0 and abs(g["checks"]["weeks"][0] - 1.0) < 0.05
    text = goals_text(g, p)
    assert f"since {start[:10]}" in text and "1 of 30 trades" in text
    assert p["closed_trades"] == 5                                    # the report still shows the whole record


def test_switching_strategy_starts_the_trend_record_afresh():
    j = Journal(":memory:", 10_000)
    hourly = Orchestrator(Settings(offline=True, symbols=["AAAUSDT"]), j)
    assert hourly.note_strategy_mode() == []                                   # a new record: nothing to announce
    j.log_decision("AAAUSDT", {"action": "HOLD", "confidence": 0.5, "source": "rules", "reasoning": "r"},
                   {"approved": False, "reason": "HOLD"}, {})
    bot, _ = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02))})
    bot.journal = j
    j.set("trend_day", "2099-01-01")
    j.set("trend_start", {"ts": "2026-01-01T00:00:00+00:00", "equity": 1.0, "prices": {}})
    msg = bot.note_strategy_mode()
    assert msg and msg[0].startswith("🔁 Strategy switched to the daily trend rule")
    assert j.get("trend_day") is None and j.get("trend_start") is None and bot.note_strategy_mode() == []
    assert Orchestrator(Settings(offline=True, symbols=["AAAUSDT"]), j).note_strategy_mode()[0].startswith(
        "🔁 Strategy switched back to the hourly agents")


def test_the_daily_step_reports_raises_and_adoptions():
    up = wavy(130, 100, 0.02)
    bot, j = trend_desk({"AAAUSDT": candles(up)})
    price = up[-1]
    j.open_position("AAAUSDT", {"qty": 1.0, "avg_price": price, "quote": price}, price * 0.97, price * 1.06)
    out = bot.run_cycle()
    adopt = [r for r in out if r.get("action") == "ADOPT"]
    assert adopt and abs(adopt[0]["old_stop"] - price * 0.97) < 1e-9
    assert bot.trend_report["day"] and bot.trend_report["adopted"] == ["AAAUSDT"]
    assert bot.trend_report["coins"]["AAAUSDT"]["action"] == "HOLD"
    bot.market.prices["AAAUSDT"] = price * 1.5                                  # within 30% of the 1.9x take-profit
    out = bot.run_cycle()
    assert [r["action"] for r in out if r.get("action") == "RAISE"] == ["RAISE"] and bot.trend_report is None
    assert any("take-profit order raised" in e["text"] for e in j.events())


def test_a_retried_step_records_each_decision_once():
    bot, j = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02))}, symbols=["AAAUSDT", "ZZZUSDT"])
    bot.market.prices["ZZZUSDT"] = 1.0
    bot.run_cycle()                                   # buys AAA; ZZZ's prices cannot be read: tried again
    out = []
    for _ in range(3):
        out += bot.run_cycle()
    rows = j.db.execute("SELECT action, COUNT(*) n FROM decisions GROUP BY action").fetchall()
    assert {r["action"]: r["n"] for r in rows} == {"BUY": 1, "HOLD": 1}       # the buy once, then "holds" once
    assert any(r["symbol"] == "TREND" and "incomplete after 4 tries" in r["error"] for r in out)


def test_a_coin_priced_far_off_on_the_demo_exchange_is_not_bought():
    from types import SimpleNamespace
    series = {"AAAUSDT": candles(wavy(130, 100, 0.02)), "BBBUSDT": candles(wavy(130, 100, 0.015))}
    bot, j = trend_desk(series)
    real = {s: rows[-1]["close"] for s, rows in series.items()}
    bot.testnet = SimpleNamespace(price=lambda sym: real[sym] * (0.5 if sym == "BBBUSDT" else 1.001),
                                  balances_total=lambda: {}, balances=lambda: {})
    bot.run_cycle()
    assert list(j.positions()) == ["AAAUSDT"]
    why = j.db.execute("SELECT reasoning FROM decisions WHERE symbol='BBBUSDT' ORDER BY id DESC").fetchone()[0]
    assert "demo exchange prices it" in why and "-50.0%" in why
