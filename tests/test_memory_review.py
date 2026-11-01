"""1.9.1, the final review's fixes: the btc rule's read after an earlier failed attempt, a held Bitcoin's daily line,
the lab's stored cards and the owner's memory choice, the stuck-rule alarm, the scorecard with the rule off, the seed
(its span, a stop, a failure with the rule off, repeated requests, a seed made with the rule off) and the owner's
texts. Nothing here changes how a desk without `memory` in MENTOR_CHECKS trades."""
import dataclasses
import datetime as dt
import json
import threading
import time
from types import SimpleNamespace

import pytest

from trader import __main__ as main_mod
from trader import lab as lab_module
from trader.agents import memory
from trader.agents.events import iso, ms_of
from trader.config import Settings
from trader.health import stuck_rule_for
from trader.journal import Journal
from trader.lab import Lab, status_text
from trader.notes import Notes, lineage_text
from trader.notify import daily_missing_text
from trader.orchestrator import Orchestrator
from trader.postmortem_report import (memory_daily_line, memory_on, memory_state, postmortem_md, postmortem_text,
                                      seed_done_text, seed_start_text, seed_status_text, verdict_words)
from trader.strategy import StrategyCard
from tests.test_memory import CHECKS4, DAY, H, KEY, _seed_fetch, checklist_of, deciding, online_desk, populated

ON = CHECKS4 + ("memory",)


# ---- the btc rule after an earlier failed attempt (f17adda) ----
def test_a_later_good_bitcoin_read_is_used_after_an_earlier_failed_attempt(monkeypatch):
    """After a failed attempt at Bitcoin's daily prices earlier in the check (the flag the records and the btc rule
    share), the chart step's good read of Bitcoin's rows in the same check still decides the btc rule, as in 1.9.0:
    an alt is refused while Bitcoin is below its 200-day average, with no second download."""
    bot, client, _ = online_desk(monkeypatch, {"BTCUSDT": [200.0] * 259, "ETHUSDT": [10.0] * 259}, CHECKS4, held=())
    real = bot._release_list

    def failed_earlier():           # runs right after the check resets its flags, before any coin is read
        bot._btc_failed = True
        return real()
    monkeypatch.setattr(bot, "_release_list", failed_earlier)
    bot.decision.run = deciding({"ETHUSDT"})
    bot.run_cycle()
    assert bot._daily_rows["BTCUSDT"] and bot._btc_failed
    text = checklist_of(bot.journal, "ETHUSDT")
    assert "✗ btc: Bitcoin 101.99 vs its 200-day average 199.51" in text and "? btc" not in text
    assert "ETHUSDT" not in bot.journal.positions() and client.daily_calls.count("BTCUSDT") == 1


# ---- a held Bitcoin's daily line ----
def test_a_held_bitcoins_missing_daily_line_says_why_without_a_false_promise(monkeypatch):
    """While the btc rule is on, the desk reads a held Bitcoin's daily prices for the alts (at most once an hour), so
    "the desk downloads no extra prices for it" is false for Bitcoin: its chart (an AI input, /chart, CHART.md) says it
    was not refreshed at this check. Other held coins, and Bitcoin with the btc rule off, keep the old words."""
    held_words = "not refreshed while the coin is held (the desk downloads no extra prices for it)"
    for checks, btc_words in ((CHECKS4, "not refreshed at this check (while the btc rule is on, the desk reads "
                                        "Bitcoin's daily prices at most once an hour, at checks where it judges a "
                                        "coin it does not hold)"),
                              (("trend200", "cooldown", "reward"), held_words)):
        bot, client, _ = online_desk(monkeypatch, {"BTCUSDT": [200.0] * 259, "ETHUSDT": [10.0] * 259}, checks,
                                     held=("BTCUSDT", "ETHUSDT"))
        bot.run_cycle()                              # the shared copy is empty: the held coins' chart steps read none
        assert bot.charts["BTCUSDT"]["summary"].endswith("daily: " + btc_words), checks
        assert daily_missing_text(bot, bot.journal, "BTCUSDT") == btc_words
        assert daily_missing_text(bot, bot.journal, "ETHUSDT") == held_words
        assert bot.charts["ETHUSDT"]["summary"].endswith("daily: " + held_words)


# ---- the lab's stored cards and the owner's memory choice ----
class FakeBacktest:
    """The lab's replays, stubbed: the challenger (cooldown 12 h) beats the champion; every run's checks are kept."""
    runs: list = []

    def __init__(self, settings, data, decision=None, days=0, btc_history=None, end_ms=None, analysis_cache=None, **k):
        self.s = settings

    def run(self):
        FakeBacktest.runs.append((self.s.cooldown_hours, tuple(self.s.mentor_checks)))
        better = self.s.cooldown_hours == 12.0
        return {"return_pct": 3.0 if better else 1.0, "trades": 30, "profit_factor": 1.5 if better else 1.1,
                "max_drawdown_pct": 2.0, "basket_hold_pct": 0.5}


def test_the_lab_puts_and_judges_every_card_with_the_owners_memory_choice(monkeypatch):
    """A challenger stored while the memory rule was on, promoted after the owner switched it off: the forward test
    compares like with like (no memory on either side), the card put in force has no memory rule (so memory_on() and
    the review digest agree with MENTOR_CHECKS right after the weekly run), no 'settings' event follows at the next
    check, and the texts do not call the old memory choice a change of the challenger. The reverse for a rollback."""
    monkeypatch.setattr(lab_module, "Backtest", FakeBacktest)
    fetch = lambda client, sym, need, interval: [{"time": 1_000 * H}]   # noqa: E731 - the stub ignores the candles
    s_on = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=ON, auto_promote=True)
    s_off = dataclasses.replace(s_on, mentor_checks=CHECKS4)
    j = Journal(":memory:", 10_000)
    owner_on = Orchestrator(s_on, j).apply_champion()
    stale = dataclasses.replace(owner_on, name="faster re-entry", cooldown_hours=12.0, source="research")
    other = dataclasses.replace(owner_on, name="wider stop", stop_loss_atr=2.5, source="research")
    ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec="seconds")
    j.set("strategy_challengers", [{"card": c.to_dict(), "proposed_at": ago, "status": status, "notes": [],
                                    "lab": None, "forward": None}
                                   for c, status in ((stale, "lab_passed"), (other, "lab_failed"))])
    assert "memory" in stale.checks and "memory" in other.checks          # stored while the rule was on
    bot = Orchestrator(s_off, j)                   # the owner removes `memory`; the desk restarts
    bot.apply_champion()
    for text in (status_text(Lab(s_off, j, fetch=fetch)), lineage_text(j),
                 Notes(j, None, s_off).strategy_md([], j.get("strategy_history"))):
        changes = [line for line in text.splitlines() if "stop_loss_atr: 2.0 → 2.5" in line or "cooldown_hours: 24" in line]
        assert len(changes) == 2 and not any("memory" in line for line in changes), text
    FakeBacktest.runs = []
    result = Lab(s_off, j, fetch=fetch).run_week([])
    assert FakeBacktest.runs and all("memory" not in checks for _, checks in FakeBacktest.runs)
    assert {cooldown for cooldown, _ in FakeBacktest.runs} == {12.0, 24.0}   # challenger and champion, like with like
    promoted = result["promoted"]
    assert promoted.name == "faster re-entry" and "memory" not in promoted.checks
    state = j.get("strategy_champion")
    assert state["card"]["id"] == promoted.id and state["by"] == "auto-promote"
    assert memory_on(j, s_off) is False                                     # right after the weekly run
    assert "# Post-mortem" not in Notes(j, None, s_off).digest({})
    assert [c["status"] for c in j.get("strategy_challengers")] == ["promoted", "lab_failed"]
    hist = len(j.get("strategy_history"))
    assert bot.apply_champion().id == promoted.id and len(j.get("strategy_history")) == hist   # no 'settings' event
    # /approve: the card returned (and put in force) carries the owner's choice too
    j.set("strategy_challengers", j.get("strategy_challengers") + [
        {"card": dataclasses.replace(stale, name="approved", trail_atr=2.5).to_dict(), "proposed_at": ago,
         "status": "ready", "notes": [], "lab": None, "forward": None}])
    approved = Lab(s_off, j).promote(dataclasses.replace(stale, name="approved", trail_atr=2.5).id, by="owner")
    assert approved and "memory" not in approved.checks and approved.describe_diff(bot.card) == "trail_atr: 2.0 → 2.5"
    # a rollback to a card stored before the owner switched the rule ON: the rule is in force at once
    bot_on = Orchestrator(s_on, j)
    bot_on.apply_champion()                        # the owner adds `memory` again; the desk restarts
    assert memory_on(j, s_on)
    back = Lab(s_on, j).rollback("profit factor 0.50 over 30 live trades (limit 0.7)")
    assert back.name == "faster re-entry" and "memory" in back.checks and memory_on(j, s_on)
    hist = len(j.get("strategy_history"))
    assert bot_on.apply_champion().id == back.id and len(j.get("strategy_history")) == hist


# ---- the stuck-rule alarm after the owner's switch ----
def _refusals(j, n=24, reason="master traders' checklist: memory not met"):
    now = dt.datetime.now(dt.timezone.utc)
    for k in range(n):
        ts = (now - dt.timedelta(minutes=15 * (n - k))).isoformat(timespec="seconds")
        j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, "
                     "risk_approved, risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (ts, "ETHUSDT", "BUY", 0.8, "rules", 0.5, "score 0.5", 0, reason, "{}", "BUY"))
    j.db.commit()


def test_the_stuck_rule_alarm_stops_when_the_owner_switches_the_memory_rule_behind_a_lab_card():
    """The memory rule refused every buy signal; the owner removes it and restarts. Behind a lab or /approve card the
    card's 'since' is kept (for the rollback watch), but the alarm judges only decisions made after the switch."""
    s_on = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=ON)
    s_off = dataclasses.replace(s_on, mentor_checks=CHECKS4)
    j = Journal(":memory:", 10_000)
    owner = Orchestrator(s_on, j).apply_champion()
    Lab(s_on, j).set_champion(dataclasses.replace(owner, name="lab A", trail_atr=2.5), by="owner", note="ready")
    state = j.get("strategy_champion")
    j.set("strategy_champion", dict(state, since=(dt.datetime.now(dt.timezone.utc)
                                                  - dt.timedelta(days=2)).isoformat(timespec="seconds")))
    _refusals(j)
    bot = Orchestrator(s_on, j)
    bot.apply_champion()
    assert stuck_rule_for(bot, j) == ("checklist: memory", 24)
    bot = Orchestrator(s_off, j)                   # the owner removed `memory`; the desk restarts
    bot.apply_champion()
    assert j.get("strategy_champion")["since"] != j.get("strategy_history")[-1]["ts"]   # 'since' kept
    assert stuck_rule_for(bot, j) is None


# ---- the scorecard with the memory rule off ----
class _Hours:
    def klines(self, symbol, interval, limit=500, start_ms=None):
        t = start_ms - start_ms % H
        return [{"time": t + i * H, "open": 100.0, "high": 100.5 + 0.02 * i, "low": 99.5, "close": 100.2,
                 "volume": 1.0} for i in range(min(limit, 1000))]


def test_the_scorecard_judges_the_memory_rules_refusals_only_while_it_is_on(monkeypatch):
    """Refusals made while the rule was on stay out of the scorecard (and so of the review digest, an AI prompt) once
    the owner switched it off: the report is then 1.9.0's (nothing to judge: no scorecard at all)."""
    monkeypatch.setattr(main_mod, "trades_trend", lambda bot: False)
    now = dt.datetime.now(dt.timezone.utc)
    for checks, shown in ((CHECKS4, False), (ON, True)):
        s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=checks)
        j = Journal(":memory:", 10_000)
        for k in range(5):
            ts = (now - dt.timedelta(days=10 + 6 * k)).isoformat(timespec="seconds")
            j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, "
                         "risk_approved, risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (ts, "ETHUSDT", "BUY", 0.8, "rules", 0.5, "score", 0, "master traders' checklist: memory not met",
                          json.dumps({"technical": {"price": 100.0, "atr": 1.0}}), "BUY"))
        j.db.commit()
        bot = SimpleNamespace(s=s, base=s, market=SimpleNamespace(client=_Hours()), free_ai=None, journal=j)
        text = main_mod.scorecard_block(bot, j)
        assert ("Memory rule refusals judged:" in text) == shown
        assert (j.get("scorecard_text") is not None) == shown and (j.get("news_scorecard") is not None) == shown
        if not shown:
            assert text == "" and j.get("memory_scorecard") is None


# ---- the seed ----
def _live_start(j, opened_ms):
    j.add_trade_context({"source": "live", "symbol": "ETHUSDT", "opened_at": iso(opened_ms), "strategy": "hourly",
                         "card_id": "c", "features_version": memory.FEATURES_VERSION, "bucket": KEY})


def test_the_seed_covers_only_the_window_the_desks_own_record_does_not(monkeypatch):
    """The memory counts the last days only; the seed ends where the desk's own record starts. It replays just the part
    of the window before that start, so the download stays the window's length, every replay trade counts when it is
    made, and once the desk's record covers the window nothing is downloaded or replayed (recorded as not needed, so
    the automatic seed does not come back every day)."""
    from trader.agents import decision as dmod
    monkeypatch.setattr(dmod, "BUY_THRESHOLD", 0.05)
    base = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"])
    card = StrategyCard(name="x", checks=("reward", "memory"), buy_threshold=0.05)
    now = 2400 * H
    j = Journal(":memory:", 10_000)
    _live_start(j, now - 30 * DAY)
    calls = []
    meta = memory.run_seed(base, card, j, None, ["BTCUSDT", "ETHUSDT"], now, days=60, fetch=_seed_fetch(calls))
    assert meta["error"] is None and meta["trades"] > 0 and meta["days"] == 30
    assert {d for _, d in calls} == {60}                                    # not 60 + the record's 30 days
    assert all(ms_of(r["opened_at"]) >= now - 60 * DAY for r in j.trade_contexts("replay"))
    assert meta["until"] == iso(now - 30 * DAY)
    assert seed_done_text(meta, True).startswith(f"🧠 Memory filled: {meta['trades']} replay trades")
    assert seed_start_text(["BTCUSDT"], meta["until"], days=30).startswith("🧠 Replaying the strategy in force over "
                                                                           "the 30 days before")
    # the desk's own record covers the whole window: nothing to download
    j2 = Journal(":memory:", 10_000)
    _live_start(j2, now - 70 * DAY)
    calls.clear()
    meta = memory.run_seed(base, card, j2, None, ["BTCUSDT", "ETHUSDT"], now, days=60, fetch=_seed_fetch(calls))
    assert calls == [] and meta["not_needed"] and meta["trades"] == 0 and meta["error"] is None
    assert j2.trade_contexts("replay") == [] and j2.get("memory_seed")["card_id"] == card.id
    assert seed_done_text(meta, True).startswith("🧠 No replay needed: the desk's own trades cover the memory's")
    assert seed_start_text(["BTCUSDT"], meta["until"], days=0).startswith("🧠 The desk's own trades already cover")
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).date().isoformat()
    bot = SimpleNamespace(s=SimpleNamespace(mentor_checks=card.checks), journal=j2, card=card,
                          market=SimpleNamespace(client=object()))
    assert main_mod.memory_seed_due(bot, SimpleNamespace(trend=False, offline=False), later) is False
    s = Settings(offline=True, symbols=["BTCUSDT"], mentor_checks=card.checks)
    j2.set("strategy_champion", {"card": card.to_dict(), "since": "2026-01-01", "by": "owner settings",
                                 "previous": None})
    assert "Seed: none needed" in seed_status_text(j2, s) and "No seed needed" in postmortem_md(j2, s)


def test_a_seed_stopped_by_a_restart_records_nothing_and_runs_again_after_it(monkeypatch):
    """A stop during the seed's downloads (an update's restart): no failure recorded, no 'tried today', no phone
    message, so the seed is still due after the restart (as the weekly lab after an interruption)."""
    j = Journal(":memory:", 10_000)
    card = StrategyCard(name="x", checks=("reward", "memory"))
    with pytest.raises(memory.SeedInterrupted):
        memory.run_seed(Settings(offline=True), card, j, None, ["BTCUSDT"], 2400 * H, days=60,
                        fetch=_seed_fetch([]), should_stop=lambda: True)
    assert j.get("memory_seed") is None
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=("reward", "memory"))
    bot = Orchestrator(s, Journal(":memory:", 10_000))
    bot.apply_champion()
    sent = []
    tg = SimpleNamespace(enabled=True, send=lambda text, **k: sent.append(text) or True)
    monkeypatch.setattr(main_mod, "memory_seed_due", lambda bot, s, today: True)
    answers = iter([False, True, True, True])        # the loop's own check passes; the stop comes during the download
    main_mod.maybe_memory_seed(bot, tg, s, threading.Lock(), should_stop=lambda: next(answers))
    assert sent == [] and bot.journal.get("memory_seed") is None
    assert any("interrupted by a restart" in e["text"] for e in bot.journal.events() if e["kind"] == "memory")
    monkeypatch.undo()
    online = SimpleNamespace(trend=False, offline=False)
    bot.market.client = object()
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert main_mod.memory_seed_due(bot, online, today)                  # still due after the restart, the same day
    calls = []
    monkeypatch.setattr(main_mod, "run_memory_seed", lambda *a, **k: calls.append(a))
    main_mod.maybe_memory_seed(bot, tg, online, threading.Lock(), should_stop=lambda: True)
    assert calls == []                                                   # the desk is stopping: not even started


def test_a_failed_seed_with_the_rule_off_promises_no_automatic_retry():
    off, on = seed_done_text({"error": "read timed out"}, False), seed_done_text({"error": "read timed out"}, True)
    assert "tries again tomorrow" not in off and "send /postmortem seed again later" in off
    assert "refuses nothing" not in off
    assert "It tries again tomorrow" in on and "until 30 trades are in memory the rule refuses nothing" in on


def test_a_second_postmortem_seed_does_not_queue_a_second_replay(tmp_path, monkeypatch):
    """While a replay runs, /postmortem seed answers that it is running; the next request after it ends starts one."""
    from tests.test_trend_main import run_desk
    s, tg, bot = run_desk(tmp_path, monkeypatch, symbols=["BTCUSDT", "ETHUSDT"])
    monkeypatch.setattr(main_mod, "seed_refusal", lambda s: None)        # the desk is online in reality
    started, release, calls = threading.Event(), threading.Event(), []

    def slow_seed(base, card, journal, client, coins, now_ms, days=365, fetch=None, should_stop=None):
        calls.append(card.id)
        started.set()
        release.wait(10)
        meta = {"card_id": card.id, "ts": "2026-10-31T00:00:00+00:00", "days": 365, "coins": coins,
                "until": "2026-10-31T00:00:00+00:00", "trades": 150, "losers": 100, "cpu_s": 1.0, "error": None,
                "tried_day": "2026-10-31"}
        journal.set("memory_seed", meta)
        return meta
    monkeypatch.setattr(memory, "run_seed", slow_seed)
    handler = tg.handlers["/postmortem"]
    assert handler("seed").startswith("🧠 Replaying the strategy in force")
    assert started.wait(10)
    assert handler("seed") == main_mod.SEED_BUSY == "🧠 A replay is already running; the result arrives here."
    release.set()
    for _ in range(200):                                                  # the replay's thread ends at once
        if not main_mod.SEED_RUNNING.locked():
            break
        time.sleep(0.05)
    assert not main_mod.SEED_RUNNING.locked() and calls == [bot.card.id]
    assert sum(1 for text, _ in tg.sent if text.startswith("🧠 Memory filled")) == 1


def test_a_seed_made_with_the_rule_off_still_counts_after_switching_it_on():
    """The seed replays the card without the memory rule anyway, so adding `memory` (a new card id) keeps it, and the
    desk's own trades stay trades of the strategy in force: no wasted second replay, nothing relabelled."""
    j, s_off = populated(CHECKS4)
    off = memory_state(j, s_off, j.get("strategy_champion")["card"]["id"])
    s_on = dataclasses.replace(s_off, mentor_checks=ON)
    bot = Orchestrator(s_on, j)
    card = bot.apply_champion()                    # the owner adds `memory`; the card's id changes
    assert card.id != j.get("memory_seed")["card_id"] and j.get("memory_seed")["card_id"] in memory.strategy_ids(card)
    st = memory_state(j, s_on, card.id)
    assert (st["replay"], st["live"], st["other_cards"]) == (off["replay"], off["live"], 0) == (40, 40, 0)
    bot._memory_book = None                        # the live rule's book (built once per check) counts the same seed
    assert bot._memory_verdict(KEY, int(time.time() * 1000))["pool"] == st["pool"] == 80
    text = postmortem_text(j, s_on, "memory")
    assert "earlier strateg" not in text and "with the memory rule switched the other way" in text
    bot.market.client = object()
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert main_mod.memory_seed_due(bot, SimpleNamespace(trend=False, offline=False), today) is False
    # a seed of another strategy is still "earlier"
    j.set("memory_seed", dict(j.get("memory_seed"), card_id="0ld0ld00"))
    assert "an earlier strategy than the one in force" in postmortem_text(j, s_on, "memory")
    assert main_mod.memory_seed_due(bot, SimpleNamespace(trend=False, offline=False), today)


# ---- the champion's live record across the owner's switch ----
def test_the_champions_live_record_counts_from_its_promotion_across_a_memory_switch():
    s_off = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], mentor_checks=CHECKS4)
    s_on = dataclasses.replace(s_off, mentor_checks=ON)
    j = Journal(":memory:", 10_000)
    now = dt.datetime.now(dt.timezone.utc)
    stamp = lambda t: t.isoformat(timespec="seconds")   # noqa: E731
    p = Orchestrator(s_off, j).apply_champion()
    a = dataclasses.replace(p, name="lab A", trail_atr=2.5)
    t0 = now - dt.timedelta(days=20)
    j.set("strategy_champion", {"card": a.to_dict(), "since": stamp(t0), "by": "owner", "previous": p.to_dict()})
    hist = j.get("strategy_history")
    hist[0]["ts"] = stamp(t0 - dt.timedelta(days=30))
    hist.append({"ts": stamp(t0), "event": "promote", "id": a.id, "name": a.name, "by": "owner", "note": "ready"})
    j.set("strategy_history", hist)

    def trade(opened, pnl):
        j.db.execute("INSERT INTO closed_trades (symbol, opened_at, closed_at, entry_price, exit_price, qty, cost, "
                     "proceeds, pnl, pnl_pct, exit_reason) VALUES ('ETHUSDT', ?, ?, 100, 100, 1, 100, 100, ?, ?, "
                     "'stop-loss')", (stamp(opened), stamp(opened + dt.timedelta(hours=2)), pnl, pnl))
    for k in range(24):                            # under A, before the owner's switch
        trade(t0 + dt.timedelta(hours=12 * k + 1), -1.0 if k % 4 else 0.5)
    j.db.commit()
    Orchestrator(s_on, j).apply_champion()         # the owner adds memory; restart: a 'settings' event, 'since' kept
    for k in range(7):                             # under A + memory
        trade(now + dt.timedelta(seconds=10 + k), -1.0 if k % 3 else 0.6)
    j.db.commit()
    trades = [dict(r) for r in j.db.execute("SELECT * FROM closed_trades ORDER BY id")]
    md = Notes(j, None, s_on).strategy_md(trades, j.get("strategy_history"))
    record = md.split("### Live record under this champion")[1].strip().splitlines()[0]
    assert record.startswith("31 closed trades") and f"Champion since {t0:%Y-%m-%d %H:%M} UTC (owner)" in md
    assert Lab(s_on, j).watch_live().endswith("over 31 live trades (limit 0.7)")   # the same count


# ---- the owner's texts ----
def test_the_owners_texts_name_what_they_count():
    j, s = populated(ON)
    j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, risk_approved, risk_reason, reports) VALUES "
                 "(?,?,?,?,?,?,?)", (iso(int(time.time() * 1000)), "ETHUSDT", "BUY", 0.9, 0,
                                     "master traders' checklist: memory not met", "{}"))
    j.db.commit()
    text = postmortem_text(j, s, "memory")
    assert ("Refusals at checks so far: 1 (one hourly signal can be refused at several checks, 4 at the 15-minute pace; "
            "/report judges those the memory rule made alone, one per coin at a time).") in text
    assert "Refused buys so far" not in text
    assert memory_daily_line(j, "2000").startswith("🧠 Memory rule: refused buys at 1 check in the last 24 h")
    within = verdict_words({"verdict": "within_chance", "p": 1 / 72})
    assert within == "more common among losers, but chance could do that: about 1 in 72"


def test_the_postmortem_replays_help_names_its_default_checklist(capsys):
    from trader.postmortem_report import main
    with pytest.raises(SystemExit):
        main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "the default checklist trend200,btc,cooldown,reward, not your MENTOR_CHECKS" in out
    assert "the live setting" not in out


# ---- after the recheck: a /postmortem seed stopped by a restart, and the /approve reply ----
def _wait_unlocked():
    for _ in range(200):
        if not main_mod.SEED_RUNNING.locked():
            return True
        time.sleep(0.05)
    return False


def test_a_postmortem_seed_stopped_by_a_restart_runs_after_it_and_answers_the_phone(tmp_path, monkeypatch):
    """The start reply promises "the result arrives here", the docs "a restart postpones it": the request is kept, runs
    after the restart also with the rule off (nothing else would run it then), and the result goes to the phone once."""
    from tests.test_trend_main import run_desk
    s, tg, bot = run_desk(tmp_path, monkeypatch, symbols=["BTCUSDT", "ETHUSDT"])
    assert "memory" not in bot.s.mentor_checks                           # the rule is off
    monkeypatch.setattr(main_mod, "seed_refusal", lambda s: None)        # the desk is online in reality

    def stopped(*a, **k):
        raise memory.SeedInterrupted()
    monkeypatch.setattr(memory, "run_seed", stopped)
    sent = len(tg.sent)
    assert tg.handlers["/postmortem"]("seed").startswith("🧠 Replaying the strategy in force")
    assert _wait_unlocked()
    assert bot.journal.get("memory_seed_requested") is True and len(tg.sent) == sent   # no message while stopping
    online = SimpleNamespace(trend=False, offline=False)
    bot.market.client = object()
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert main_mod.memory_seed_due(bot, online, today) is False         # the rule is off: the daily seed never runs
    assert main_mod.memory_seed_requested(bot, online)
    assert not main_mod.memory_seed_requested(bot, SimpleNamespace(trend=True, offline=False))

    def filled(base, card, journal, client, coins, now_ms, days=365, fetch=None, should_stop=None):
        meta = {"card_id": card.id, "ts": "2026-10-31T00:00:00+00:00", "days": 365, "coins": coins,
                "until": "2026-10-31T00:00:00+00:00", "trades": 150, "losers": 100, "cpu_s": 1.0, "error": None,
                "tried_day": today}
        journal.set("memory_seed", meta)
        return meta
    monkeypatch.setattr(memory, "run_seed", filled)
    main_mod.maybe_memory_seed(bot, tg, online, threading.Lock(), should_stop=lambda: False)   # after the restart
    assert [t for t, _ in tg.sent[sent:] if t.startswith("🧠 Memory filled")] and len(tg.sent) == sent + 1
    assert not bot.journal.get("memory_seed_requested")
    main_mod.maybe_memory_seed(bot, tg, online, threading.Lock(), should_stop=lambda: False)   # answered: not again
    assert len(tg.sent) == sent + 1
    # an unexpected failure outside the replay answers the request once instead of retrying at every check
    bot.journal.set("memory_seed_requested", True)
    monkeypatch.setattr(main_mod, "run_memory_seed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    main_mod.maybe_memory_seed(bot, tg, online, threading.Lock(), should_stop=lambda: False)
    main_mod.maybe_memory_seed(bot, tg, online, threading.Lock(), should_stop=lambda: False)
    assert [t for t, _ in tg.sent[sent + 1:]] == ["⚠️ The memory could not be filled: disk full. The desk trades "
                                                  "normally meanwhile."]
    assert not bot.journal.get("memory_seed_requested")


def test_the_approve_reply_names_the_typed_id_when_the_memory_choice_changed_it(tmp_path, monkeypatch):
    """A challenger stored while the rule was on, approved with the rule off: the card put in force has another id;
    the reply names both and the memory choice, so the owner recognises the challenger they typed."""
    from tests.test_trend_main import run_desk
    s, tg, bot = run_desk(tmp_path, monkeypatch, symbols=["BTCUSDT", "ETHUSDT"])
    assert "memory" not in bot.s.mentor_checks
    stored = dataclasses.replace(bot.card, name="approved", trail_atr=2.5, source="research",
                                 checks=tuple(bot.card.checks) + ("memory",))
    same = dataclasses.replace(bot.card, name="plain", stop_loss_atr=2.5, source="research")
    ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat(timespec="seconds")
    bot.journal.set("strategy_challengers", [{"card": c.to_dict(), "proposed_at": ago, "status": "ready", "notes": [],
                                              "lab": None, "forward": None} for c in (stored, same)])
    reply = tg.handlers["/approve"](stored.id)
    in_force = bot.journal.get("strategy_champion")["card"]["id"]
    assert in_force != stored.id
    assert reply.startswith(f"✅ Promoted approved [{in_force}] (the challenger [{stored.id}] with your current choice "
                            "of the memory rule: off). The desk switches at its next check."), reply
    reply = tg.handlers["/approve"](same.id)                             # the same choice: the old reply
    assert reply.startswith(f"✅ Promoted plain [{same.id}]. The desk switches at its next check."), reply
