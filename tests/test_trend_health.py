"""1.7.0: /health, /agents, the pulse and /diagnose when the desk trades the daily trend rule (STRATEGY=trend)."""
import datetime as dt
from types import SimpleNamespace

from trader import health
from trader.config import Settings
from trader.health import (agents_text, diagnose_text, health_text, judged_since, pulse_text, roster, since_iso,
                           stuck_rule_for)
from trader.journal import Journal
from trader.llm import ClaudeJSON
from trader.orchestrator import Orchestrator
from tests.test_trend import candles, trend_desk, wavy


def iso(minutes_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def desk(adopt=False, record_days=None):
    """AAA rises (bought), CCC falls (stays out); one daily step done, a fresh heartbeat."""
    bot, j = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02)), "CCCUSDT": candles(list(reversed(wavy(130))))})
    if record_days:   # the record began this many days ago
        j.db.execute("INSERT INTO equity VALUES (?,?,?)", (iso(record_days * 1440), 10_000, 10_000))
        j.db.commit()
    if adopt:         # a position the hourly strategy opened an hour before the switch
        price = bot.market.series["AAAUSDT"][-1]["close"]
        j.open_position("AAAUSDT", {"qty": 1.0, "avg_price": price, "quote": price}, price * 0.97, price * 1.06)
        j.db.execute("UPDATE positions SET opened_at=?", (iso(60),))
        j.db.commit()
    bot.run_cycle()
    j.set("heartbeat", {"ts": iso(), "cycle": 1})
    return bot, j


def decision(j, reason, minutes_ago, approved=0, symbol="AAAUSDT"):
    j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, risk_approved,"
                 " risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(minutes_ago), symbol, "BUY", 1.0, "trend", 0.05, "buys 10.0% of the account", approved, reason,
                  "{}", "BUY"))
    j.db.commit()


def free_ai():
    return SimpleNamespace(status=lambda: {"usage": {"models": {}, "exhausted": []}, "models": ["m"], "shared": False,
                                           "daily": 100, "disabled": {}, "label": "Groq", "reset_hours": 5})


# ---- /agents ----
def test_the_trend_roster_lists_only_the_agents_the_rule_uses():
    bot, j = desk()
    rows = roster(bot, j)
    assert [r["name"] for r in rows] == ["Market data", "Trend rule", "Risk manager", "Execution"]
    assert {r["status"] for r in rows} == {"working"}
    by = {r["name"]: r for r in rows}
    day = j.get("trend_day")
    assert f"daily candles read for {day}" in by["Market data"]["detail"]
    assert by["Trend rule"]["detail"].startswith(f"decided {day}") and "holding 1 of 2 coins" in by["Trend rule"]["detail"]
    assert "approved 1 buys" in by["Risk manager"]["detail"] and "take-profit raised 0 times" in by["Execution"]["detail"]
    bot.market.prices["AAAUSDT"] = j.positions()["AAAUSDT"]["take_profit"] / 1.25   # within 30% of the take-profit
    bot.run_cycle()
    assert roster(bot, j)[3]["detail"].endswith("take-profit raised 1 time")
    j.event("error", "Market data AAAUSDT: timeout")
    assert roster(bot, j)[0]["status"] == "failing"


def test_agents_text_names_the_trend_rule_instead_of_strategy_cards():
    bot, j = desk()
    text = agents_text(bot, j)
    start = j.get("trend_start")["ts"][:10]
    assert text.startswith("🤖 4 agents: 4 working")
    assert (f"📈 Strategy in force: the daily trend rule (STRATEGY=trend) since {start}: hold a coin while its daily close "
            "is above its 50-day average, emergency stop 25% below the entry, decisions once a day after 00:00 UTC") in text
    assert "🧪 Strategies" not in text and "strategy cards" not in text and "🏆" not in text
    assert "Not used by the daily trend rule" in text and "head trader" in text
    fresh, _ = trend_desk({"AAAUSDT": candles(wavy(130))})
    assert "first daily decisions are made at the next check" in agents_text(fresh, fresh.journal)


# ---- the trend rule's own row: working, idle early in the day, late, gave up ----
def test_the_trend_rule_is_idle_early_in_the_day_and_a_warning_when_late(monkeypatch):
    bot, j = desk()
    expected = dt.date.fromisoformat(j.get("trend_day"))
    before = (expected - dt.timedelta(days=1)).isoformat()
    j.set("trend_day", before)                                       # today's decisions not made yet
    j.set("trend_tries", {"day": before, "n": 1})
    today = dt.datetime.combine(expected + dt.timedelta(days=1), dt.time(0, 10), dt.timezone.utc)
    monkeypatch.setattr(health, "now_utc", lambda: today)
    j.set("heartbeat", {"ts": today.isoformat(timespec="seconds"), "cycle": 2})
    rule = roster(bot, j)[1]
    assert rule["status"] == "idle" and rule["detail"].startswith(f"decides {expected.isoformat()} at the first check")
    late = today.replace(hour=3)
    monkeypatch.setattr(health, "now_utc", lambda: late)
    j.set("heartbeat", {"ts": late.isoformat(timespec="seconds"), "cycle": 3})
    rule = roster(bot, j)[1]
    assert rule["status"] == "failing" and "have not run yet" in rule["detail"]
    text = health_text(bot, j, "1.7.0", late.isoformat(), {"checked_at": late.isoformat()}, "1.7.0")
    assert text.startswith("⚠️ Working, with a warning") and "✅ All systems working" not in text
    assert "failing: Trend rule" in text and "⚠️ the daily trend decisions" in text


def test_a_day_that_gave_up_shows_its_last_error():
    bot, j = desk()
    day = j.get("trend_day")
    j.set("trend_tries", {"day": day, "n": 4})
    j.event("error", "Trend ZZZUSDT: daily prices unavailable: no data")
    j.event("error", f"Daily trend decisions: the decisions for {day} stayed incomplete after 4 tries (see the errors "
                     "before this one); the coins concerned wait until tomorrow")
    rule = roster(bot, j)[1]
    assert rule["status"] == "failing" and "stayed incomplete after 4 tries" in rule["detail"]
    assert rule["detail"].endswith("last error: Trend ZZZUSDT: daily prices unavailable: no data")
    assert health_text(bot, j, "1.7.0", iso(60), None, None).startswith("⚠️ Working, with a warning")


# ---- /health and the pulse ----
def test_health_in_trend_mode():
    bot, j = desk()
    bot.llm = ClaudeJSON("claude-haiku-5-5", "k", 0)
    bot.free_ai = free_ai()
    text = health_text(bot, j, "1.7.0", iso(60), {"checked_at": iso()}, "1.7.0")
    assert text.startswith("✅ All systems working")
    assert text.splitlines()[3].startswith(f"📈 Daily trend decisions: last for {j.get('trend_day')}")   # near the top
    assert "Daily trend rule since" in text and "Agents: 4 of 4 working" in text
    assert "spent today (trend mode: only /review uses Claude)" in text
    assert "(not used for trades in trend mode)" in text
    hourly = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    hourly.run_cycle()
    hourly.journal.set("heartbeat", {"ts": iso(), "cycle": 1})
    hourly.llm, hourly.free_ai = ClaudeJSON("claude-haiku-5-5", "k", 0), free_ai()
    plain = health_text(hourly, hourly.journal, "1.7.0", iso(), {"checked_at": iso()}, "1.7.0")
    assert "trend mode" not in plain and "📈" not in plain and "of 14 working" in plain


def test_the_pulse_says_when_the_rule_last_decided():
    bot, j = desk()
    p = pulse_text(bot, j, 6, "1.7.0")
    assert f"📈 Daily trend decisions: last for {j.get('trend_day')}" in p and "Agents working: 4 of 4" in p
    assert p.index("📈") < p.index("Agents working")


# ---- the stuck-rule alarm ----
def test_the_stuck_alarm_judges_the_trend_rule_on_its_week():
    bot, j = desk()
    assert judged_since(j, trend=True) == max(since_iso(7 * 24), j.get("trend_start")["ts"])
    j.set("trend_start", {"ts": iso(4 * 1440), "equity": 10_000, "prices": {}})
    for _ in range(3):                                               # before the rule started: not judged
        decision(j, "not enough free cash ($5.00)", 5 * 1440)
    assert stuck_rule_for(bot, j) is None
    for _ in range(2):
        decision(j, "not enough free cash ($5.00)", 2 * 1440)
    assert stuck_rule_for(bot, j) is None                            # one buy (approved) is in the week already
    j.db.execute("DELETE FROM decisions WHERE risk_approved=1")
    j.db.commit()
    assert stuck_rule_for(bot, j) is None                            # 2 signals: too few to judge
    decision(j, "not enough free cash ($5.00)", 1440)
    assert stuck_rule_for(bot, j) == ("not enough cash", 3)
    text = health_text(bot, j, "1.7.0", iso(60), None, None)
    assert text.startswith("🛑 Needs attention") and "'not enough cash' refused every buy signal (3 of 3)" in text
    assert "🛑 'not enough cash' refused every buy signal: send /health" in pulse_text(bot, j, 6, "1.7.0")
    j.set("trend_start", None)                                       # switched, no daily decisions yet: nothing judged
    assert stuck_rule_for(bot, j) is None
    hourly = Orchestrator(Settings(offline=True, symbols=["AAAUSDT"]), j)
    assert stuck_rule_for(hourly, j) is None                         # the hourly agents need 20 signals


# ---- /diagnose ----
def test_a_short_record_is_not_replayed():
    bot, j = desk()
    asked = []
    text = diagnose_text(bot, j, 7, lambda symbols, days: asked.append(days))
    assert asked == [] and "(the trend rule decides once a day: a replay needs at least 2 finished days)" in text


def test_diagnose_compares_what_the_desk_and_its_rule_hold():
    bot, j = desk(record_days=10)
    replay = lambda holding: (lambda symbols, days: {"trades": 2, "return_pct": 1.0, "basket_hold_pct": 2.0,
                                                     "holding": holding})
    same = diagnose_text(bot, j, 7, replay(["AAAUSDT"]))
    assert "✅ The desk holds what its rule would hold (AAA)" in same and "Closed trades: 2 in the replay, 0 live" in same
    assert "fewer trades than its rules" not in same and "traded about as often" not in same
    assert "• AAA: 1 → 1, furthest above its 50-day average +" in same
    assert "; last daily decision: BUY (within limits)" in same and "best score" not in same
    assert "• CCC: 0 → 0, never above its 50-day average (closest -" in same and "HOLD (no buy signal)" in same
    other = diagnose_text(bot, j, 7, replay(["CCCUSDT"]))
    assert "⚠️ The rule would hold CCC but the desk does not: its last daily decision was HOLD (no buy signal)" in other
    assert "⚠️ The desk holds AAA but the replayed rule would not: the replay sold it or did not buy it" in other


def test_an_adopted_hourly_position_is_named_as_such():
    bot, j = desk(adopt=True, record_days=10)
    text = diagnose_text(bot, j, 7, lambda symbols, days: {"trades": 0, "return_pct": 0.0, "basket_hold_pct": 0.0,
                                                           "holding": []})
    assert "The desk holds AAA but the replayed rule would not: it was bought by the hourly strategy and taken over" in text
    assert "last daily decision: HOLD (holding)" in text
