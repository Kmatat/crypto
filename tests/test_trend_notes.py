"""1.7.0: the notebook under the daily trend rule (STRATEGY=trend): the rule instead of the laboratory, which strategy
each trade belongs to, the rule's exits and decisions in plain words; the hourly notebook stays as it was."""
import datetime as dt

from trader.config import Settings
from trader.journal import Journal
from trader.notes import Notes
from trader.orchestrator import Orchestrator
from tests.test_notes import desk
from tests.test_trend import candles, trend_desk, wavy

SCORECARD = "🧪 Claude vs the free rules (computed from real later prices)"


def texts(bot) -> dict:
    """Every notebook text, rendered directly so that a mistake fails the test (refresh() only logs it)."""
    n, j = bot.notes, bot.journal
    hist = j.get("strategy_history") or []
    trades = [dict(r) for r in j.db.execute("SELECT * FROM closed_trades ORDER BY id")]
    return {"README": n.readme_md(), "STRATEGY": n.strategy_md(trades, hist),
            "TRADES": n.trades_md(trades, hist, bot.last_price),
            "PERFORMANCE": n.performance_md(trades, hist, bot.last_price, SCORECARD),
            "DIGEST": n.digest(bot.last_price, scorecard=SCORECARD)}


def hourly_position(j: Journal, sym: str, price: float) -> None:
    """A position the hourly strategy opened two hours before the switch, with the decision it was bought on."""
    j.open_position(sym, {"qty": 1.0, "avg_price": price, "quote": price}, price * 0.97, price * 1.06)
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
    j.db.execute("UPDATE positions SET opened_at=? WHERE symbol=?", (when, sym))
    j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, risk_approved, "
                 "risk_reason, reports, rules_action) VALUES (?, ?, 'BUY', 0.7, 'rules', 0.4, 'hourly buy', 1, "
                 "'within limits', '{}', 'BUY')", (when, sym))
    j.db.commit()


def yesterday() -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()


def test_a_fresh_trend_notebook_describes_the_rule_not_a_laboratory(tmp_path):
    bot, j = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02)), "CCCUSDT": candles(list(reversed(wavy(130))))},
                        notes_dir=str(tmp_path / "notes"))
    bot.run_cycle()
    t = texts(bot)
    since = j.get("trend_start")["ts"][:10]

    st = t["STRATEGY"]
    assert st.startswith(f"# Strategy: the daily trend rule (STRATEGY=trend), in force since {since}")
    assert "No strategy recorded yet" not in st and "Champion" not in st and "laboratory" not in st.lower()
    assert "above its 50-day average" in st and "| Emergency stop below the entry (TREND_STOP_PCT) | 25% | 25% |" in st
    assert "1.9x the price" in st and "within 30%" in st
    assert "the tested values are used" not in st and "positions at once for" not in st   # settings as tested
    assert f"| AAA | {yesterday()} | BUY | within limits | buys " in st
    assert f"| CCC | {yesterday()} | HOLD | no buy signal | stays out · CCC closed at" in st
    assert "no closed trades under this strategy yet" in st and "/trendtest" in st
    assert "hourly strategy's cards" not in st                       # a fresh trend record has none
    assert (tmp_path / "notes" / "STRATEGY.md").read_text().startswith("# Strategy: the daily trend rule")
    assert not (tmp_path / "notes" / "strategies").exists()

    rd = t["README"]
    assert "fixed in code (trader/trend.py)" in rd and "TREND_SMA_DAYS, TREND_VOL_TARGET and TREND_STOP_PCT" in rd
    assert "| Strategy | the daily trend rule (STRATEGY=trend): one decision a day, at the first check after 00:00 UTC" in rd
    assert "no AI in the decisions" in rd and "| Emergency stop | 25% below the entry, checked by the desk at every market check (paper trading)" in rd
    assert "| Take-profit | an order at 1.9x the price" in rd and "no profit cap" in rd
    assert "| Overall loss brake | 10% below the start: no new buys" in rd and "ride on with their emergency stop" in rd
    assert "Compare the live record with /trendtest's replay of the same weeks." in rd
    assert "Laboratory bounds" not in rd and "champion" not in rd.lower() and "forward test" not in rd

    tr = t["TRADES"]
    assert "| Stop | Take-profit |" in tr and "| Target |" not in tr
    assert "emergency stop $" in tr and "take-profit order $" in tr and "(raised as the price rises; no target)" in tr
    assert "| daily trend rule |" in tr

    perf = t["PERFORMANCE"]
    assert "The AI versus the free rules" not in perf and SCORECARD not in perf   # the stored hourly scorecard
    assert "| Strategy |" in perf and "| daily trend rule |" in perf and "daily trend rule, since" in perf

    dg = t["DIGEST"]
    assert dg.startswith(f"The desk runs the fixed daily trend rule (STRATEGY=trend) since {since}: no laboratory, no AI "
                         "in the decisions; the rule is expected to win 20-30% of its trades")
    assert "Judge whether it follows its rule and how its live record compares with its replay." in dg
    assert SCORECARD not in dg and "# Strategy: the daily trend rule" in dg


def test_trades_say_which_strategy_they_belong_to_and_why_they_ended():
    up = wavy(130, 100, 0.02)
    series = {"AAAUSDT": candles(up), "BBBUSDT": candles(wavy(130, 50, 0.015))}
    bot, j = trend_desk(series)
    hourly_position(j, "BBBUSDT", series["BBBUSDT"][-1]["close"])
    bot.run_cycle()                                         # buys AAA, takes over the hourly BBB
    since = j.get("trend_start")["ts"][:10]
    tr = texts(bot)["TRADES"]
    assert "| AAAUSDT |" in tr and f"| owner settings, managed by the daily trend rule since {since} |" in tr
    assert tr.count("(raised as the price rises; no target)") == 2

    bot.market.series["AAAUSDT"] = candles(up + [up[-1] * 0.5])   # the next day AAA closes far below its average
    bot.market.prices["AAAUSDT"] = up[-1] * 0.5
    p = j.positions()["BBBUSDT"]                                  # and BBB's emergency stop fires on the exchange
    j.close_position("BBBUSDT", {"ok": True, "qty": p["qty"], "avg_price": p["stop_loss"],
                                 "quote": p["qty"] * p["stop_loss"]}, "stop-loss (exchange)")
    j.set("trend_day", "2000-01-01")
    bot.run_cycle()
    t = texts(bot)
    aaa, bbb = t["TRADES"].split("### ")[1:3]
    assert aaa.startswith(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d} AAA") and "trend: closed at or below" in aaa
    assert "- Strategy: daily trend rule\n" in aaa
    assert "- Decision at entry: BUY (daily trend rule): buys " in aaa and "confidence" not in aaa
    assert "- Decision at exit: SELL (daily trend rule): closed at or below its 50-day average · AAA closed at" in aaa
    assert f"- Strategy: owner settings, managed by the daily trend rule since {since}\n" in bbb
    assert "- Decision at entry: BUY, confidence 70% (rules): hourly buy" in bbb and "Decision at exit" not in bbb
    st = t["STRATEGY"]
    assert "| BBB |" in st and "| HOLD | re-entry rule | waits: its emergency stop fired" in st   # waits for the reset
    assert "| AAA |" in st and "| HOLD | no buy signal |" in st
    assert "1 closed trades" in st                          # the live record counts the rule's own trades only


def test_settings_outside_the_ranges_and_too_few_slots_are_explained():
    s = Settings(offline=True, strategy="trend", symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], max_open_positions=2,
                 trend_sma_days=3)
    n = Notes(Journal(":memory:", 10_000), None, s)
    text = n.strategy_md([], [])
    assert text.startswith("# Strategy: the daily trend rule (STRATEGY=trend), in force since its first check")
    assert "No strategy recorded yet" not in text
    assert "TREND_SMA_DAYS 3" in text and "the tested values are used" in text and "| 50 days | 50 days |" in text
    assert "Only 2 positions at once for 3 coins: when more coins qualify than there are free positions" in text
    assert "| BTC | - | no daily decision yet |" in text
    assert n.digest().startswith("The desk runs the fixed daily trend rule (STRATEGY=trend) since its first check")


def test_the_hourly_cards_are_kept_but_not_in_force(tmp_path):
    j = Journal(":memory:", 10_000)
    card = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), j).apply_champion()   # the hourly champion
    hist = j.get("strategy_history")
    n = Notes(j, tmp_path / "n", Settings(offline=True, strategy="trend", symbols=["BTCUSDT"]))
    assert "Status: **champion of the hourly strategy (not in force while STRATEGY=trend)**" in n.card_md(card, None, [], hist)
    st = n.strategy_md([], hist)
    assert f"(champion {card.name} [{card.id}]) are kept unchanged in strategies/ for STRATEGY=hourly" in st
    assert "## Champion" not in st
    hourly = Notes(j, None, Settings(offline=True, symbols=["BTCUSDT"]))
    assert "Status: **champion**." in hourly.card_md(card, None, [], hist)
    assert n.refresh() and (tmp_path / "n" / "strategies" / f"{card.id}.md").exists()


def test_back_on_the_hourly_strategy_the_rule_only_names_its_own_buys():
    bot, j = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02))})
    assert bot.note_strategy_mode() == []                   # a new record that starts on the trend rule
    bot.run_cycle()
    assert Orchestrator(Settings(offline=True, symbols=["AAAUSDT"]), j).note_strategy_mode()   # the owner switches back
    j.log_equity(10_000)
    n = Notes(j, None, Settings(offline=True, symbols=["AAAUSDT"]))
    trades = [dict(r) for r in j.db.execute("SELECT * FROM closed_trades")]
    tr = n.trades_md(trades, [], {})
    assert "| Stop | Target |" in tr and "emergency stop" not in tr and "| daily trend rule |" in tr   # bought by the rule
    perf = n.performance_md(trades, [], {}, SCORECARD)
    assert "| Champion |" in perf and "| owner settings |" in perf and SCORECARD in perf   # the week ended on hourly
    assert not n.digest().startswith("The desk runs")


def test_the_hourly_notebook_is_unchanged(tmp_path):
    bot = desk(tmp_path, cycles=80)
    bot.journal.open_position("XRPUSDT", {"qty": 0.01, "avg_price": 100.0, "quote": 1.0}, 97.0, 106.0)   # not traded here
    t = texts(bot)
    assert t["STRATEGY"].startswith("# Strategy\n") and f"## Champion: {bot.card.name} [{bot.card.id}]" in t["STRATEGY"]
    assert "## Laboratory bounds" in t["README"] and "Compare the champion's exam with the challengers'" in t["README"]
    assert "| Strategy changes | at most" in t["README"] and "daily trend rule" not in t["README"]
    assert "| Stop | Target | Protection | Strategy |" in t["TRADES"] and "daily trend rule" not in t["TRADES"]
    assert "The AI versus the free rules" in t["PERFORMANCE"] and "| Champion |" in t["PERFORMANCE"]
    assert t["DIGEST"].startswith("# Strategy\n") and SCORECARD in t["DIGEST"]
