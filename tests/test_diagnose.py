"""/diagnose: the desk's records versus its own rules replayed on the same prices."""
import datetime as dt
import json

from trader.config import Settings
from trader.health import check_gaps, coin_funnel, diagnose_text, since_iso
from trader.journal import Journal
from trader.orchestrator import Orchestrator


def iso(minutes_ago=0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_a_blocked_desk_is_flagged_with_its_cause():
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], mentor_checks=("reward",), take_profit_atr=3.0)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    for _ in range(100):
        bot.run_cycle()
    replay = lambda symbols, days: {"trades": 25, "return_pct": 1.2, "basket_hold_pct": -3.0}
    text = diagnose_text(bot, bot.journal, 1, replay)
    assert "🛑 Settings problem" in text and "target 3 ATR (1.5:1, rule minimum 2:1)" in text
    assert "opened 25 fewer trades than its rules" in text and "checklist: reward" in text
    assert "• BTC:" in text and "→ 0" in text


def test_a_desk_that_trades_like_its_rules_is_cleared():
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=())
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    for _ in range(100):
        bot.run_cycle()
    opened = bot.journal.db.execute("SELECT COUNT(*) FROM orders WHERE side='BUY' AND ok=1").fetchone()[0]
    assert opened > 0
    text = diagnose_text(bot, bot.journal, 1, lambda symbols, days: {"trades": opened, "return_pct": 0.1, "basket_hold_pct": 0})
    assert "✅ The live desk traded about as often as its rules" in text and "Settings problem" not in text
    broken = diagnose_text(bot, bot.journal, 1, lambda symbols, days: (_ for _ in ()).throw(RuntimeError("offline")))
    assert "replay could not run: offline" in broken


def test_portfolio_tilt_blocks_and_downtime_are_counted():
    j = Journal(":memory:", 10_000)
    j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, risk_approved,"
                 " risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(), "ETHUSDT", "HOLD", 0.8, "rules", 0.37, "r", 0, "HOLD - nothing to do",
                  json.dumps({"head": {"threshold": 0.40, "why": "+0.05 already holding BTC (correlation 0.85)"}}), "HOLD"))
    for minutes in (600, 590, 580, 300, 290):          # a 4.7 h hole between the 3rd and 4th check
        j.db.execute("INSERT INTO equity VALUES (?,?,?)", (iso(minutes), 10_000, 10_000))
    j.db.commit()
    assert coin_funnel(j, since_iso(24))["ETHUSDT"]["tilted"] == 1
    g = check_gaps(j, since_iso(24), 15)
    assert g["gaps"] == 1 and g["down_hours"] == 4.7 and g["checks"] == 5


def test_a_small_starting_balance_is_reported_with_its_consequences():
    from types import SimpleNamespace
    from trader.health import settings_lines
    tiny = Settings(starting_balance=100, anthropic_key="k", ai_budget_pct_month=0.25)
    p = tiny.problems()
    assert any("any loss stops all buying" in x for x in p) and any("Claude is effectively off" in x for x in p)
    # on demo money there is no percentage cap unless the owner sets one, so a small balance no longer starves Claude
    assert not any("Claude is effectively off" in x for x in Settings(starting_balance=100, anthropic_key="k").problems())
    assert any("a loss of 17%" in x for x in Settings(starting_balance=120).problems())
    assert Settings(starting_balance=10_000, anthropic_key="k").problems() == []
    s = Settings(offline=True, symbols=["BTCUSDT"], starting_balance=120)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.run_cycle()
    bot.testnet = SimpleNamespace(balances_total=lambda: {"USDT": 135.5, "BTC": 0.5})
    bot.market.client = SimpleNamespace(price=lambda sym: 80_000.0)
    lines = "\n".join(settings_lines(bot))
    assert "starting balance $120" in lines and "positions about $12" in lines
    assert "Demo wallet on Binance: 135.50 USDT plus BTC $40,000" in lines


def test_the_wizard_warns_about_a_tiny_demo_wallet(monkeypatch, tmp_path):
    from trader import setup_wizard as w
    said = []
    monkeypatch.setattr(w, "check_binance", lambda k, s: {"USDT": 150.0})
    answers = iter(["1", "", ""])   # demo account; then Enter on the budget question (and any extra prompt)
    wiz = w.Wizard(env_path=tmp_path / ".env", ask=lambda q: next(answers, ""), ask_secret=lambda q: "x", say=said.append)
    wiz.step_mode()
    text = "\n".join(map(str, said))
    assert "Only 150 USDT in the demo wallet" in text and "paper trading with 10000" in text
    assert wiz.v["STARTING_BALANCE_USDT"] == "135"
