"""Strategy cards, the laboratory's exam, forward tests, promotion, rollback, research proposals."""
import datetime as dt

import pytest

from trader.agents import decision as dmod
from trader.agents.research import ResearchAgent
from trader.config import Settings
from trader.journal import Journal
from trader import lab as lab_module
from trader.lab import FORWARD_MIN_DAYS, Lab, status_text, week_report
from trader.orchestrator import Orchestrator
from trader.strategy import BOUNDS, MAX_CHANGES, StrategyCard, summary_line
from tests.test_backtest import synthetic


# ---------------------------------------------------------------- cards
def test_card_identity_bounds_and_diff():
    a = StrategyCard("a")
    b = StrategyCard("renamed copy", rationale="different words")
    assert a.id == b.id and a.diff(b) == []                       # id depends on trading fields only
    c = StrategyCard("c", trail_atr=3.0, max_open_positions=1)
    assert sorted(c.diff(a)) == ["max_open_positions", "trail_atr"] and "trail_atr: 2.0 → 3.0" in c.describe_diff(a)
    assert StrategyCard.from_dict(c.to_dict()) == c
    bad = StrategyCard("bad", max_position_pct=0.5, risk_per_trade_pct=0.05, interval="15m", take_profit_atr=1.0,
                       checks=("trend200", "moon"))
    v = bad.violations()
    assert any("max_position_pct" in x for x in v) and any("risk_per_trade_pct" in x for x in v)
    assert any("interval" in x for x in v) and any("take-profit" in x for x in v) and any("moon" in x for x in v)
    assert StrategyCard("ok").violations() == []
    assert BOUNDS["max_position_pct"][1] == 0.10 and BOUNDS["risk_per_trade_pct"][1] == 0.01  # may only shrink


def test_card_applies_to_settings_without_exceeding_the_owner():
    owner = Settings(max_position_pct=0.05, max_open_positions=2)
    card = StrategyCard("greedy", max_position_pct=0.10, max_open_positions=3, interval="4h", trail_atr=3.0)
    s = card.settings(owner)
    assert s.max_position_pct == 0.05 and s.max_open_positions == 2   # owner's limits win
    assert s.interval == "4h" and s.trail_atr == 3.0 and s.symbols == owner.symbols
    old = (dmod.BUY_THRESHOLD, dict(dmod.WEIGHTS))
    try:
        StrategyCard("x", buy_threshold=0.4).apply_decision_rules()
        assert dmod.BUY_THRESHOLD == 0.4 and dmod.SELL_THRESHOLD == -0.4
    finally:
        dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD = old[0], -old[0]
        dmod.WEIGHTS.clear()
        dmod.WEIGHTS.update(old[1])
    assert "4h candles" in summary_line(card)


# ---------------------------------------------------------------- lab with synthetic history
def fake_fetch(client, symbol, days, interval="1h"):
    hours = (days + 210) * 24
    seed = {"BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 3, "BNBUSDT": 4, "XRPUSDT": 5, "ADAUSDT": 6}.get(symbol, 7)
    return synthetic(symbol, min(hours, 2600), seed, drift=0.0003)


# Same shape as the real exam (five windows, two of them ending in the past) over far fewer days, so the suite
# also finishes quickly on the small free server whose updater runs it before every install.
TEST_WINDOWS = [(20, 0), (30, 0), (45, 0), (20, 10), (20, 20)]


@pytest.fixture
def lab(monkeypatch):
    monkeypatch.setattr(lab_module, "WINDOWS", TEST_WINDOWS)
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], lab_alt_symbols=("BNBUSDT", "XRPUSDT"))
    j = Journal(":memory:", 10_000)
    return Lab(s, j, client=None, fetch=fake_fetch)


def test_production_exam_windows_reach_a_year_back_and_include_past_periods():
    assert lab_module.WINDOWS == [(90, 0), (180, 0), (365, 0), (180, 90), (180, 180)]


def test_first_champion_comes_from_owner_settings(lab):
    champ = lab.champion()
    assert champ.source == "owner" and champ.checks == ("trend200", "btc", "cooldown", "reward")
    assert lab.journal.get("strategy_champion")["by"] == "owner settings"
    assert lab.champion().id == champ.id  # stable


def test_challenger_rules_bounds_and_change_count(lab):
    champ = lab.champion()
    assert lab.add_challenger(champ) is None                                   # identical to the champion
    wild = StrategyCard("wild", max_position_pct=0.5)
    assert lab.add_challenger(wild)["status"] == "rejected"                    # out of bounds
    busy = StrategyCard("busy", trail_atr=3, max_open_positions=1, interval="4h", cooldown_hours=48)
    e = lab.add_challenger(busy)
    assert e["status"] == "rejected" and f"max {MAX_CHANGES}" in e["notes"][0]  # too many changes at once
    ok = StrategyCard("ok", trail_atr=3.0)
    assert lab.add_challenger(ok)["status"] == "proposed" and lab.add_challenger(ok) is None  # no duplicates


def test_exam_covers_windows_and_coin_sets_and_verdict_is_strict(lab):
    champ = lab.champion()
    ev = lab.evaluate(champ)
    assert len(ev["tests"]) == 10 and {t["coins"] for t in ev["tests"]} == {"BTC,ETH", "BNB,XRP"}
    assert {(t["days"], t["ending_days_ago"]) for t in ev["tests"]} == set(TEST_WINDOWS)
    same = lab.compare(ev, ev)
    assert not same["passed"] and "only 0 of 10" in same["reasons"][0]        # equal is not better
    better = {**ev, "tests": [{**t, "return_pct": t["return_pct"] + 1} for t in ev["tests"]],
              "total_trades": 100, "mean_profit_factor": 1.2, "max_drawdown_pct": 3.0}
    assert lab.compare(better, ev)["passed"]
    mixed = {**better, "tests": [{**t, "return_pct": t["return_pct"] + (1 if i % 2 else -5)} for i, t in enumerate(ev["tests"])]}
    v = lab.compare(mixed, ev)
    assert not v["passed"] and any("worse than the champion by" in r for r in v["reasons"])
    thin = {**better, "total_trades": 5}
    assert any("only 5 trades" in r for r in lab.compare(thin, ev)["reasons"])


def test_week_run_then_forward_test_then_owner_approval(lab, monkeypatch):
    champ = lab.champion()
    cards = ResearchAgent().neighbours(champ, 2, set())
    result = lab.run_week(cards)
    statuses = {c["card"]["id"]: c["status"] for c in result["challengers"]}
    assert set(statuses) == {c.id for c in cards} and set(statuses.values()) <= {"lab_passed", "lab_failed"}
    assert lab.journal.get("strategy_champion_eval")["total_trades"] >= 0 and lab.journal.get("lab_last_run")
    text = week_report(result)
    assert "Weekly strategy lab" in text and ("champion stays" in text or "passed the exam" in text)

    # force one challenger through the exam and make it old enough for the forward test
    items = lab.challengers()
    items[0]["status"] = "lab_passed"
    items[0]["proposed_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=FORWARD_MIN_DAYS + 1)).isoformat()
    lab._save_challengers(items)
    monkeypatch.setattr(Lab, "forward_test", lambda self, e, c: {"ready": True, "passed": True, "days": 30.0,
                                                                 "challenger_return": 1.0, "champion_return": 0.5,
                                                                 "challenger_trades": 12, "reasons": []})
    result = lab.run_week([])
    ready = [c for c in result["challengers"] if c["status"] == "ready"]
    assert len(ready) == 1 and result["promoted"] is None                     # AUTO_PROMOTE off: owner decides
    assert "Ready for your decision" in status_text(lab) and "/approve" in status_text(lab)

    assert lab.promote("nope", by="owner") is None
    card = lab.promote(ready[0]["card"]["id"], by="owner")
    assert card and lab.champion().id == card.id and lab.journal.get("strategy_champion")["previous"]["id"] == champ.id
    assert lab.journal.get("strategy_history")[-1]["event"] == "promote"
    assert lab.reject(card.id) is False                                        # the champion cannot be rejected


def test_auto_promote_when_enabled(lab, monkeypatch):
    lab.s.auto_promote = True
    champ = lab.champion()
    card = ResearchAgent().neighbours(champ, 1, set())[0]
    e = lab.add_challenger(card)
    items = lab.challengers()
    items[0]["status"] = "lab_passed"
    items[0]["proposed_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
    lab._save_challengers(items)
    monkeypatch.setattr(Lab, "forward_test", lambda self, e, c: {"ready": True, "passed": True, "days": 40.0,
                                                                 "challenger_return": 2.0, "champion_return": 0.0,
                                                                 "challenger_trades": 15, "reasons": []})
    result = lab.run_week([])
    assert result["promoted"].id == card.id and lab.champion().id == card.id
    assert "Promoted automatically" in week_report(result)


def test_forward_test_waits_then_judges(lab):
    champ = lab.champion()
    card = ResearchAgent().neighbours(champ, 1, set())[0]
    entry = lab.add_challenger(card)
    fwd = lab.forward_test(entry, champ)
    assert fwd["ready"] is False and "needs 28 days" in fwd["reason"]
    entry["proposed_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=35)).isoformat()
    fwd = lab.forward_test(entry, champ)
    assert fwd["ready"] is True and fwd["passed"] in (True, False) and "challenger_trades" in fwd


def test_live_rollback_when_the_champion_turns_bad(lab):
    champ = lab.champion()
    card = ResearchAgent().neighbours(champ, 1, set())[0]
    lab.add_challenger(card)
    items = lab.challengers()
    items[0]["status"] = "ready"
    lab._save_challengers(items)
    lab.promote(card.id, by="owner")
    assert lab.watch_live() is None                                            # not enough trades yet
    j = lab.journal
    for i in range(30):
        pnl = -20.0 if i % 10 else 5.0                                         # 27 losers of $20, 3 winners of $5
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, "
                     "proceeds, pnl, pnl_pct, exit_reason) VALUES ('BTCUSDT', ?, ?, 1, 1, 1, 100, ?, ?, ?, 'stop-loss')",
                     ("2999-01-01T00:00:00+00:00", "2999-01-01T01:00:00+00:00", 100 + pnl, pnl, pnl))
    j.db.commit()
    why = lab.watch_live()
    assert why and "profit factor" in why and lab.champion().id == champ.id   # back to the previous champion
    assert lab.journal.get("strategy_history")[-1]["event"] == "rollback"
    assert lab.watch_live() is None                                            # a rollback champion is not re-judged


# ---------------------------------------------------------------- research
def test_neighbours_are_valid_small_steps_and_skip_tried_ones():
    champ = StrategyCard("champ")
    cards = ResearchAgent().neighbours(champ, 5, set())
    assert len(cards) == 5
    for c in cards:
        assert c.violations() == [] and 1 <= len(c.diff(champ)) <= MAX_CHANGES and c.source == "neighbour"
    again = ResearchAgent().neighbours(champ, 5, {c.id for c in cards})
    assert {c.id for c in again}.isdisjoint({c.id for c in cards})


def test_lessons_summarise_recent_losers():
    j = Journal(":memory:", 10_000)
    now = dt.datetime.now(dt.timezone.utc)
    rows = [("stop-loss (exchange)", 1, -1.5), ("stop-loss (exchange)", 1.5, -1.2), ("take-profit (exchange)", 20, 3.0),
            ("stop-loss (exchange)", 30, -2.0)]
    for reason, hours, pnl_pct in rows:
        opened = (now - dt.timedelta(hours=hours + 1)).isoformat(timespec="seconds")
        closed = (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, proceeds,"
                     " pnl, pnl_pct, exit_reason) VALUES ('BTCUSDT', ?, ?, 1, 1, 1, 100, ?, ?, ?, ?)",
                     (opened, closed, 100 + pnl_pct, pnl_pct, pnl_pct, reason))
    j.db.commit()
    les = ResearchAgent.lessons(j)
    assert les["trades"] == 4 and les["losers"] == 3 and les["noise_stops"] == 2
    assert les["exits"] == {"stop-loss": 3, "take-profit": 1} and any("two hours" in n for n in les["notes"])


def test_proposals_use_claude_when_available_and_fall_back():
    champ = StrategyCard("champ")
    good = {"name": "Turtle-style patience", "rationale": "Fewer trades.", "source": "Way of the Turtle",
            "buy_threshold": 0.3, "stop_loss_atr": 2.0, "take_profit_atr": 6.0, "trail_atr": 3.0,
            "checks": ["trend200", "btc", "cooldown", "reward"], "cooldown_hours": 24, "min_reward_risk": 2.0,
            "risk_per_trade_pct": 0.01, "max_position_pct": 0.10, "max_open_positions": 3, "interval": "1h"}

    class LLM:
        def research(self, *a, **k):
            return {"text": "notes", "sources": []}

        def ask(self, *a, **k):
            return {"proposals": [good, {**good, "name": "broken", "buy_threshold": "x"}],
                    "ideas_needing_code": ["Donchian breakout entries"]}
    agent = ResearchAgent(LLM())
    cards = agent.propose(champ, {"days": 30}, 3)
    assert [c.name for c in cards] == ["Turtle-style patience"] and cards[0].source.startswith("research:")
    assert agent.ideas_needing_code == ["Donchian breakout entries"]

    class Silent:
        def research(self, *a, **k):
            return None

        def ask(self, *a, **k):
            return None
    assert all(c.source == "neighbour" for c in ResearchAgent(Silent()).propose(champ, {}, 2))


# ---------------------------------------------------------------- live desk follows the champion
def test_desk_switches_strategy_when_the_champion_changes():
    s = Settings(offline=True, symbols=["BTCUSDT"])
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.run_cycle()
    assert bot.card.source == "owner" and bot.s.trail_atr == 2.0
    new = StrategyCard("calmer", trail_atr=3.0, max_open_positions=1, interval="4h")
    bot.journal.set("strategy_champion", {"card": new.to_dict(), "since": "x", "by": "owner", "previous": None})
    old_buy = dmod.BUY_THRESHOLD
    try:
        bot.run_cycle()
        assert bot.card.id == new.id and bot.s.trail_atr == 3.0 and bot.s.max_open_positions == 1
        assert bot.market.interval == "4h" and bot.risk.s.trail_atr == 3.0
    finally:
        StrategyCard.from_settings(Settings()).apply_decision_rules()
        dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD = old_buy, -old_buy
