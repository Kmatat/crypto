"""1.6.2: a STARTING_BALANCE_USDT changed after the record began takes effect (or says plainly why it cannot)."""
from types import SimpleNamespace

from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator
from trader.report import performance


def old_record(path, start=110.0, cash=99.0):
    j = Journal(path, start)       # the record began with the old budget...
    j.set("cash", cash)            # ...and lost 11 USDT since
    j.set("day_start", {"date": "2026-10-26", "equity": 99.0})
    return j


def test_a_new_budget_is_applied_like_a_deposit_and_the_history_kept(tmp_path):
    j = old_record(str(tmp_path / "j.db"))
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], starting_balance=5000), j)
    msg = bot.apply_budget_change()
    assert msg and msg[0].startswith("💰 Budget changed from $110.00 to $5,000.00")
    assert "-11.00 USDT" in msg[0]
    assert j.get("starting_balance") == 5000 and j.cash == 4989.0
    assert j.get("day_start")["equity"] == 4989.0            # the daily loss limit does not see a jump
    assert bot.portfolio()["equity"] == 4989.0 and performance(j, {})["return_pct"] == -0.22
    assert any("Budget changed" in e["text"] for e in j.events())
    assert bot.apply_budget_change() == []                    # applied once


def test_the_demo_wallet_must_be_able_to_pay_for_the_budget(tmp_path):
    from trader.health import health_text, settings_lines
    j = old_record(str(tmp_path / "j.db"))
    s = Settings(offline=True, symbols=["BTCUSDT"], starting_balance=5000)
    bot = Orchestrator(s, j)
    bot.testnet = SimpleNamespace(balances=lambda: {"USDT": 99.4, "BTC": 1.0},
                                  balances_total=lambda: {"USDT": 99.4})
    msg = bot.apply_budget_change()
    assert msg[0].startswith("🛑 Budget not changed") and "holds only 99.40 USDT" in msg[0]
    assert "2) Paper trading" in msg[0] and "at most 109" in msg[0]
    assert j.get("starting_balance") == 110 and j.cash == 99.0           # nothing changed
    text = health_text(bot, j, "1.6.2", "2026-10-26T08:00:00+00:00", None, None)
    assert "🛑 Budget: you set STARTING_BALANCE_USDT to $5,000" in text
    assert "not applied" in "\n".join(settings_lines(bot))
    bot.testnet = SimpleNamespace(balances=lambda: {"USDT": 6000.0})
    assert bot.apply_budget_change()[0].startswith("💰") and j.cash == 4989.0 and j.get("budget_problem") is None


def test_a_budget_below_the_money_in_positions_is_refused(tmp_path):
    j = old_record(str(tmp_path / "j.db"), start=1000, cash=50)          # 950 is invested
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], starting_balance=500), j)
    msg = bot.apply_budget_change()
    assert msg[0].startswith("🛑") and "$950.00" in msg[0] and j.get("starting_balance") == 1000


def test_a_withdrawal_is_not_reported_as_a_drawdown(tmp_path):
    j = Journal(str(tmp_path / "j.db"), 10_000)
    j.db.execute("INSERT INTO equity VALUES (?,?,?)", ("2026-10-25T08:00:00+00:00", 10_000, 10_000))
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], starting_balance=5000), j)
    bot.apply_budget_change()
    j.log_equity(5000)
    assert performance(j, {})["max_drawdown_pct"] == 0.0


def test_the_start_messages_include_the_budget_change(tmp_path):
    from trader.__main__ import start_checks
    j = old_record(str(tmp_path / "j.db"))
    s = Settings(offline=True, symbols=["BTCUSDT"], starting_balance=5000)
    lines = start_checks(Orchestrator(s, j), s)
    assert any(line.startswith("💰 Budget changed") for line in lines)


# ---- 1.6.3: what /diagnose and the price gap measure ----
def test_diagnose_compares_the_same_span_as_the_record(tmp_path):
    import datetime as dt
    from trader.health import diagnose_text
    j = Journal(str(tmp_path / "j.db"), 5000)
    began = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)).isoformat(timespec="seconds")
    j.db.execute("INSERT INTO equity VALUES (?,?,?)", (began, 5000, 5000))
    j.log_decision("TRXUSDT", {"action": "BUY", "confidence": 0.7, "source": "rules", "reasoning": "r",
                               "rules_action": "BUY", "combined_score": 0.4}, {"approved": True, "reason": "ok"}, {})
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], starting_balance=5000), j)
    asked = []
    text = diagnose_text(bot, j, 7, lambda symbols, period: asked.append(period) or None)
    assert asked and abs(asked[0] - 20 / 24) < 0.01                      # 20 hours, not 7 days
    assert "since the desk's record began 20 h ago (asked: 7 days)" in text


def test_the_rules_replay_gets_200_days_of_history_before_the_period(monkeypatch):
    from trader import __main__ as main, backtest
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    bot.market.client = object()
    asked, ran = [], {}
    monkeypatch.setattr(backtest, "fetch_history", lambda client, sym, days, interval="1h": asked.append(days) or [])

    class FakeBacktest:
        def __init__(self, s, hist, decision=None, days=None, btc_history=None, **kw):
            ran["days"] = days

        def run(self):
            return {"trades": 0}
    monkeypatch.setattr(backtest, "Backtest", FakeBacktest)
    main.rules_replay(bot, ["ETHUSDT"], 0.8)
    assert min(asked) >= 200 and ran["days"] == 0.8


def test_an_exchange_exit_is_judged_against_its_stop_price():
    j = Journal(":memory:", 5000)
    bot = Orchestrator(Settings(offline=True, symbols=["ENAUSDT"], starting_balance=5000), j)
    j.open_position("ENAUSDT", {"qty": 1000.0, "avg_price": 0.50, "quote": 500.0}, 0.48, 0.54)
    bot.last_price["ENAUSDT"] = 0.46                         # the price had fallen further by the next check
    bot._record_exchange_exit("ENAUSDT", {"state": "exited", "reason": "stop-loss",
                                          "fill": {"qty": 1000.0, "avg_price": 0.4795, "quote": 479.5, "fee": 0.48},
                                          "protection": {"kind": "exchange", "stop": 0.48, "take": 0.54}})
    row = j.db.execute("SELECT avg_price, market_price FROM orders WHERE side='SELL'").fetchone()
    assert row["market_price"] == 0.48 and abs(row["avg_price"] / row["market_price"] - 1) < 0.002
    assert performance(j, {})["avg_price_gap_pct"] < 0.2      # 0.1% slippage, not the 4% later fall


def test_no_claude_cap_is_called_no_cap():
    from trader.health import settings_lines
    from trader.llm import ClaudeJSON
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], ai_daily_budget_usd=0), Journal(":memory:", 10_000))
    bot.llm = ClaudeJSON("claude-haiku-5-5", "k", 0)
    text = "\n".join(settings_lines(bot))
    assert "no daily limit on real-money spend" in text and "no daily cap" in text and "$0.00/day" not in text


def test_the_goals_say_when_the_trade_sample_will_be_complete(tmp_path):
    import datetime as dt
    from trader.report import goals, goals_text
    j = Journal(str(tmp_path / "j.db"), 5000)
    began = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=2)).isoformat(timespec="seconds")
    j.db.execute("INSERT INTO equity VALUES (?,?,?)", (began, 5000, 5000))
    for i in range(20):   # 20 trades in 2 weeks: 10 a week, 130 to go -> about week 15
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, pnl, "
                     "pnl_pct, exit_reason) VALUES ('BTCUSDT', ?, ?, 1, 1, 1, 100, ?, 0, 'stop-loss')",
                     (began, began, 1.0 if i % 3 == 0 else -1.0))
    j.db.commit()
    p = performance(j, {})
    text = goals_text(goals(j, p), p)
    assert "20 of 150 trades needed (at 10 a week, complete in about week 15)" in text
