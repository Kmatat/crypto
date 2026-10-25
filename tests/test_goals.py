"""Demo-phase goals: encoded as data the owner reads, never as an instruction to the AI."""
import datetime as dt

from trader.agents.decision import SYSTEM
from trader.config import Settings
from trader.journal import Journal
from trader.orchestrator import Orchestrator
from trader.report import GOALS, goals, goals_text, performance, wilson


def test_wilson_range_widens_with_small_samples():
    assert wilson(0, 0) == (0.0, 100.0)
    lo, hi = wilson(125, 292)
    assert 36 < lo < 38 and 48 < hi < 50          # 43% observed -> roughly 37-49% likely
    lo2, hi2 = wilson(4300, 10_000)
    assert hi2 - lo2 < hi - lo                     # more trades, tighter range


def test_goals_collect_evidence_then_judge():
    j = Journal(":memory:", 10_000)
    j.log_equity(10_000)
    j.set("benchmark_start", {"ts": "x", "prices": {"BTCUSDT": 80_000.0, "ETHUSDT": 2_000.0}})
    j.set("ai_spend_total", 25.0)
    p = performance(j, {})
    g = goals(j, p, {"BTCUSDT": 88_000.0, "ETHUSDT": 2_100.0})
    assert g["verdict"] == "collecting evidence"
    assert g["checks"]["closed_trades"][:2] == (0, GOALS["closed_trades"]) and g["checks"]["weeks"][1] == GOALS["weeks"]
    assert g["vs_btc_pct"] == 10.0 and g["vs_basket_pct"] == 5.0            # basket = the coins, Bitcoin shown separately
    assert g["ai_cost_pct"] == 0.25 and g["net_return_pct"] == -0.25          # $25 of AI on $10,000, nothing earned yet
    text = goals_text(g, p)
    assert "collecting evidence" in text and "0 of 150 trades" in text and "AI cost 0.25%" in text
    assert "Bitcoin +10.0%" in text and "your coins +5.0%" in text


def test_goals_pass_and_fail_once_there_is_enough_evidence():
    j = Journal(":memory:", 10_000)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=13)).isoformat(timespec="seconds")
    j.db.execute("INSERT INTO equity VALUES (?,?,?)", (old, 10_000, 10_000))
    for i in range(160):  # 160 trades, 60% winners, winners twice the size of losers -> profit factor 3
        pnl = 20.0 if i % 5 < 3 else -10.0
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, "
                     "proceeds, pnl, pnl_pct, exit_reason) VALUES ('BTCUSDT', ?, ?, 100, 101, 1, 1000, ?, ?, ?, 'signal')",
                     (old, old, 1000 + pnl, pnl, pnl / 10))
    j.db.execute("INSERT INTO orders (ts, symbol, side, ok) VALUES ('t', 'BTCUSDT', 'BUY', 1)")
    j.set("cash", 10_000 + 160 * 8)  # 96 x 20 - 64 x 10 = 1,280
    j.db.commit()
    p = performance(j, {})
    g = goals(j, p, {})
    assert g["verdict"].startswith("PASS") and all(c[2] for c in g["checks"].values())
    j.set("ai_spend_total", 5_000.0)  # the AI bill ate the whole gain
    g = goals(j, performance(j, {}), {})
    assert g["verdict"].startswith("FAIL") and not g["checks"]["net_return_positive"][2]


def test_benchmark_prices_are_recorded_on_the_first_cycle():
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=())
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    assert bot.journal.get("benchmark_start") is None
    bot.run_cycle()
    bench = bot.journal.get("benchmark_start")
    assert set(bench["prices"]) == {"BTCUSDT", "ETHUSDT"} and bench["prices"]["BTCUSDT"] == bot.last_price["BTCUSDT"]
    bot.run_cycle()
    assert bot.journal.get("benchmark_start") == bench  # never overwritten


def test_ai_prompt_has_no_return_target():
    assert "There is no return target" in SYSTEM and "%" not in SYSTEM
