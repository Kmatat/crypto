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
