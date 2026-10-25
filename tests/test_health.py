"""1.4.1: the stale take-profit that blocked every buy, /health, /agents, the pulse, the watchdog, update messages."""
import datetime as dt
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from trader import lab as lab_module
from trader.config import Settings, legacy_take_profit
from trader.health import (agents_text, funnel, funnel_text, health_text, pulse_text, roster, roster_text, since_iso,
                           strategy_changes, strategy_changes_text, stuck_rule)
from trader.journal import Journal
from trader.lab import Lab
from trader.notify import RECENT_SECONDS, Telegram
from trader.orchestrator import Orchestrator
from trader.agents.research import ResearchAgent
from trader.strategy import StrategyCard
from tests.test_lab import TEST_WINDOWS, fake_fetch
from tests.test_updater import FakeTG, git, make, publish, repos  # noqa: F401 - the repos fixture


def iso(minutes_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------- the bug: an old default that refused every buy
def test_old_settings_file_take_profit_is_corrected_at_load(monkeypatch):
    monkeypatch.setenv("SETTINGS_VERSION", "2")
    monkeypatch.setenv("TAKE_PROFIT_ATR", "3.0")
    s = Settings.load()
    assert s.take_profit_atr == 4.0 and "TAKE_PROFIT_ATR 3.0 -> 4.0" in s.migrations[0] and s.problems() == []
    monkeypatch.setenv("SETTINGS_VERSION", "3")                       # a file written by today's wizard: kept
    s = Settings.load()
    assert s.take_profit_atr == 3.0 and not s.migrations and "refuse every buy" in s.problems()[0]
    assert legacy_take_profit("3.0", "2", "1.5") == (None, "")         # 3.0 with a 1.5:1 minimum is a real choice


def test_the_old_default_blocks_every_buy_and_the_desk_says_so(tmp_path):
    def run(take):
        s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], mentor_checks=("reward",),
                     take_profit_atr=take, notes_dir=str(tmp_path / f"n{take}"))
        bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
        for _ in range(150):
            bot.run_cycle()
        return bot, funnel(bot.journal, since_iso(24))
    blocked, f = run(3.0)
    assert f["buy_signals"] >= 20 and f["approved"] == 0 and f["refused"] == {"checklist: reward": f["buy_signals"]}
    assert stuck_rule(f) == ("checklist: reward", f["buy_signals"])
    assert "refused: checklist: reward" in funnel_text(f) and blocked.base.problems()
    trading, f2 = run(4.0)
    assert f2["approved"] > 0 and stuck_rule(f2) is None


def test_impossible_strategies_are_refused_and_the_owner_champion_follows_the_settings():
    assert any("no buy could ever pass" in v for v in StrategyCard("x", take_profit_atr=3.0).violations())
    assert StrategyCard("x", take_profit_atr=3.0, min_reward_risk=1.5).violations() == []
    j = Journal(":memory:", 10_000)
    stale = StrategyCard.from_settings(Settings(take_profit_atr=3.0))
    j.set("strategy_champion", {"card": stale.to_dict(), "since": iso(60), "by": "owner settings", "previous": None})
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), j)
    card = bot.apply_champion()
    assert card.take_profit_atr == 4.0 and "owner's settings changed" in bot.champion_note
    assert j.get("strategy_history")[-1]["event"] == "promote" and j.events()[-1]["kind"] == "strategy"
    lab_pick = StrategyCard("lab pick", trail_atr=3.0)                 # chosen by the lab: left alone
    j.set("strategy_champion", {"card": lab_pick.to_dict(), "since": iso(), "by": "owner", "previous": None})
    bot2 = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), j)
    assert bot2.apply_champion().id == lab_pick.id and bot2.champion_note is None
    broken = StrategyCard("broken", take_profit_atr=3.0)                # an impossible champion is replaced
    j.set("strategy_champion", {"card": broken.to_dict(), "since": iso(), "by": "owner", "previous": None})
    bot3 = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), j)
    assert bot3.apply_champion().take_profit_atr == 4.0 and "could not trade" in bot3.champion_note


# ---------------------------------------------------------------- the funnel, the roster, strategy changes
def row(j, action, rules_action, approved, reason, source="rules", reasoning="r", minutes_ago=0):
    j.db.execute("INSERT INTO decisions (ts, symbol, action, confidence, source, combined_score, reasoning, risk_approved,"
                 " risk_reason, reports, rules_action) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(minutes_ago), "BTCUSDT", action, 0.9, source, 0.5, reasoning, approved, reason, "{}", rules_action))
    j.db.commit()


def test_the_stuck_alarm_ignores_refusals_from_before_the_strategy_in_force():
    from trader.__main__ import maybe_pulse
    from trader.health import judged_since
    s = Settings(offline=True, symbols=["BTCUSDT"], pulse_hours=0)
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    j = bot.journal
    for _ in range(33):  # the old desk: every buy refused by the reward rule, then the correction at start
        row(j, "BUY", "BUY", 0, "master traders' checklist: reward not met", minutes_ago=120)
    j.set("strategy_champion", {"card": StrategyCard.from_settings(s).to_dict(), "since": iso(5), "by": "owner settings",
                                "previous": None})
    alerts = []
    tg = SimpleNamespace(enabled=True, send=lambda t: True, send_once=lambda k, t, every_hours=6: alerts.append(t))
    maybe_pulse(bot, tg, s)
    assert alerts == [] and stuck_rule(funnel(j, judged_since(j))) is None
    bot.journal.set("heartbeat", {"ts": iso(), "cycle": 1})
    text = health_text(bot, j, "1.4.2", iso(10), {"checked_at": iso()}, "1.4.2")
    assert "Since the strategy in force took effect" in text and "refused every buy signal" not in text
    for _ in range(25):  # the same fault under the strategy now in force is still caught
        row(j, "BUY", "BUY", 0, "master traders' checklist: reward not met")
    maybe_pulse(bot, tg, s)
    assert alerts and "made under the current strategy" in alerts[0]


def test_funnel_explains_what_happened_to_each_buy_signal():
    j = Journal(":memory:", 10_000)
    row(j, "BUY", "BUY", 0, "master traders' checklist: reward, cooldown not met")
    row(j, "BUY", "BUY", 0, "already 3 open positions (max 3)")
    row(j, "BUY", "BUY", 1, "within limits")
    row(j, "HOLD", "BUY", 0, "HOLD - nothing to do", source="claude")
    row(j, "HOLD", "BUY", 0, "HOLD - nothing to do", reasoning="Clearly negative news for this coin")
    row(j, "HOLD", "HOLD", 0, "HOLD - nothing to do")
    f = funnel(j, since_iso(1))
    assert f["buy_signals"] == 5 and f["approved"] == 1 and f["claude_vetoes"] == 1 and f["news_vetoes"] == 1
    assert f["refused"] == {"checklist: reward": 1, "checklist: cooldown": 1, "all position slots in use": 1}
    assert stuck_rule(f) is None and "1 vetoed by Claude" in funnel_text(f)


def test_agents_report_counts_agents_and_strategy_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(lab_module, "WINDOWS", TEST_WINDOWS)
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"], lab_alt_symbols=("BNBUSDT", "XRPUSDT"),
                 notes_dir=str(tmp_path / "n"))
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    for _ in range(5):
        bot.run_cycle()
    bot.journal.set("heartbeat", {"ts": iso(), "cycle": 5})
    lab = Lab(s, bot.journal, client=None, fetch=fake_fetch)
    lab.add_challenger(StrategyCard("wild", max_position_pct=0.5))                  # refused at entry
    lab.run_week(ResearchAgent().neighbours(lab.champion(), 2, set()))
    bot.journal.event("watchlist", "Coin list changed", {"added": ["XRPUSDT"], "removed": ["SOLUSDT"]})
    c = strategy_changes(bot.journal, since_iso(24))
    assert c["created"] == 3 and c["by_source"] == {"neighbour": 2, "proposal": 1} and c["refused_at_entry"] == 1
    assert c["exam_passed"] + c["exam_failed"] == 2 and c["coins_added"] == ["XRPUSDT"]
    text = strategy_changes_text(c, 1)
    assert "Created: 3" in text and "Coins added: XRP" in text and "removed: SOL" in text
    rows = roster(bot, bot.journal)
    names = [r["name"] for r in rows]
    assert len(rows) == 14 and "Head trader" in names and "Laboratory" in names
    by = {r["name"]: r for r in rows}
    assert by["Market data"]["status"] == "working" and by["Market scout"]["status"] == "off"   # offline: no scout
    report = agents_text(bot, bot.journal, 1)
    assert report.startswith("🤖 14 agents:") and "Strategy in force" in report and "🧪 Strategies (last 1 days)" in report
    bot.journal.event("error", "Market data BTCUSDT: timeout")
    assert {r["name"]: r for r in roster(bot, bot.journal)}["Market data"]["status"] == "failing"
    assert "1 failing" in roster_text(roster(bot, bot.journal))


def test_health_and_pulse_texts():
    s = Settings(offline=True, symbols=["BTCUSDT"], take_profit_atr=3.0, mentor_checks=("reward",))
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.run_cycle()
    bot.journal.set("heartbeat", {"ts": iso(), "cycle": 1})
    text = health_text(bot, bot.journal, "1.4.1 (abc)", iso(30), {"checked_at": iso(10), "result": "up_to_date"}, "1.4.1")
    assert text.startswith("🛑 Needs attention") and "Settings problem" in text and "Last market check" in text
    assert "Installed code is" not in text
    late = health_text(bot, bot.journal, "1.4.0 (abc)", iso(300), None, "1.4.1")
    assert "a restart is pending" in late and "not set up" in late
    ok = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    ok.run_cycle()
    ok.journal.set("heartbeat", {"ts": iso(), "cycle": 1})
    assert health_text(ok, ok.journal, "1.4.1", iso(), {"checked_at": iso()}, "1.4.1").startswith("✅ All systems working")
    p = pulse_text(ok, ok.journal, 6, "1.4.1")
    assert p.startswith("💓 Desk pulse, last 6 h") and "Agents working" in p


def test_desk_loop_helpers_pulse_heartbeat_errors_and_start_checks():
    from trader.__main__ import heartbeat, maybe_pulse, record_errors, start_checks
    s = Settings(offline=True, symbols=["BTCUSDT"], pulse_hours=6, migrations=("TAKE_PROFIT_ATR 3.0 -> 4.0: old default",))
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    notices = start_checks(bot, s)
    assert any("Settings corrected" in n for n in notices) and bot.journal.get("running_version")
    bot.journal.set("running_version", {"version": "1.3.0 (02923ca)", "revision": "02923ca"})
    assert any("Update confirmed" in n and "1.3.0" in n for n in start_checks(bot, s))
    bot.run_cycle()
    heartbeat(bot, 1)
    assert bot.journal.get("heartbeat")["cycle"] == 1
    seen = {}
    record_errors(bot, [{"symbol": "ETHUSDT", "stage": "market data", "error": "timeout"}], seen)
    record_errors(bot, [{"symbol": "ETHUSDT", "stage": "market data", "error": "timeout"}], seen)   # deduplicated
    assert [e["text"] for e in bot.journal.events() if e["kind"] == "error"] == ["Market data ETHUSDT: timeout"]
    sent = []
    tg = SimpleNamespace(enabled=True, send=lambda t: sent.append(t) or True, send_once=lambda k, t, every_hours=6: sent.append(t))
    bot.journal.set("last_pulse", iso(30))
    maybe_pulse(bot, tg, s)
    assert sent == []                                                       # not yet due
    bot.journal.set("last_pulse", iso(7 * 60))
    maybe_pulse(bot, tg, s)
    assert len(sent) == 1 and sent[0].startswith("💓") and bot.journal.get("last_pulse") > iso(5)


def test_commands_sent_during_a_restart_are_answered_if_recent():
    tg = Telegram("t", "1")
    now = time.time()
    upd = lambda i, text, age: {"update_id": i, "message": {"chat": {"id": 1}, "text": text, "date": now - age}}
    tg.call = lambda method, **kw: [upd(1, "/update", RECENT_SECONDS + 600), upd(2, "/status", 60)]
    replies = []
    tg.send = lambda text: replies.append(text) or True
    offset = tg.catch_up({"/status": lambda: "status!", "/update": lambda: "update!", "/help": lambda: "help"})
    assert offset == 3 and replies == ["status!"]                         # the old /update is skipped


# ---------------------------------------------------------------- the update job: watchdog, recovery, messages
def write(path, data):
    path.write_text(json.dumps(data))


def test_watchdog_alerts_restarts_and_reports_recovery(repos):
    dev, server = repos
    tg = FakeTG()
    u, calls = make(server, tg)
    beat = server / "data" / "heartbeat.json"
    write(beat, {"ts": iso(10), "next_minutes": 15})
    u.run()
    assert not any("Watchdog" in m for m in tg.sent)                       # fresh: quiet
    write(beat, {"ts": iso(70), "next_minutes": 15})
    u.run()
    assert "has not finished a market check for 70 minutes" in tg.sent[-1] and calls == []
    u.run()
    assert sum("Watchdog" in m for m in tg.sent) == 1                      # once per stall
    write(beat, {"ts": iso(130), "next_minutes": 15})
    u.run()
    assert "Watchdog: alert" not in tg.sent[-1]
    write(beat, {"ts": iso(70 + 60), "next_minutes": 15})
    st = json.loads((server / "data" / "update-status.json").read_text())
    st["stall_notified"] = json.loads(beat.read_text())["ts"]
    (server / "data" / "update-status.json").write_text(json.dumps(st))
    u.run()
    assert "restarting the desk" in tg.sent[-1] and calls == ["restart"]
    write(beat, {"ts": iso(1), "next_minutes": 15})
    u.run()
    assert "checking the market again" in tg.sent[-1]
    write(beat, {"ts": iso(100), "next_minutes": 15, "note": "weekly strategy lab running"})
    n = len(tg.sent)
    u.run()
    assert len(tg.sent) == n                                                # a long lab run is not a stall


def test_update_waits_for_the_new_desk_to_confirm(repos, tmp_path):
    dev, server = repos
    new = publish(dev, "v2\n", "Feature")
    tg = FakeTG()
    started = server / "data" / "desk-started.json"
    u, calls = make(server, tg)
    u.restart_wait, u.poll_seconds = 5, 0.01

    def restart():
        calls.append("restart")
        write(started, {"revision": new, "started_at": iso(-1)})
    u.restart = restart
    st = u.run()
    assert st["result"] == "updated" and "send /health to confirm" not in tg.sent[-1] and st.get("in_progress") is None


def test_an_interrupted_update_is_undone_or_confirmed_and_always_reported(repos):
    dev, server = repos
    old = git(server, "rev-parse", "HEAD")
    new = publish(dev, "v2\n", "Feature")
    status = server / "data" / "update-status.json"
    # 1) stopped during the tests: code was pulled, the desk never restarted -> undone, then installed again
    git(server, "pull", "-q", "--ff-only", "origin", "main")
    write(status, {"in_progress": {"old": old, "new": new, "label": "version x", "step": "tests"}})
    tg = FakeTG()
    u, calls = make(server, tg)
    st = u.run()
    assert any("interrupted during the tests step and has been undone" in m for m in tg.sent)
    assert st["result"] == "updated" and git(server, "rev-parse", "HEAD") == new
    # 2) stopped after the new desk started -> confirmed, nothing undone
    write(status, {"in_progress": {"old": old, "new": new, "label": "version x", "step": "confirm"}})
    write(server / "data" / "desk-started.json", {"revision": new, "started_at": iso()})
    tg2 = FakeTG()
    u2, calls2 = make(server, tg2)
    u2.run()
    assert "completed" in tg2.sent[0] and git(server, "rev-parse", "HEAD") == new and calls2 == []


def test_unexpected_failures_and_the_system_time_limit_are_reported(repos, monkeypatch):
    from trader import updater
    dev, server = repos
    publish(dev, "v2\n", "Feature")
    tg = FakeTG()
    u, calls = make(server, tg)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    u._run = boom
    st = u.run(force=True)
    assert st["result"] == "check_failed" and "failed unexpectedly: disk full" in tg.sent[-1]
    status = server / "data" / "update-status.json"
    assert json.loads(status.read_text())["in_progress"]["step"] == "install"   # left for the next run to undo
    st = json.loads(status.read_text())
    st.pop("in_progress")
    status.write_text(json.dumps(st))
    git(server, "reset", "-q", "--hard", "HEAD~1")
    tg2 = FakeTG()
    u2, _ = make(server, tg2)

    def stopped(*a, **k):
        raise updater.Interrupted()
    u2._run = stopped
    st = u2.run(force=True)
    assert "stopped by the server during the install step" in tg2.sent[-1]
    assert json.loads((server / "data" / "update-status.json").read_text())["in_progress"]["step"] == "install"


def test_git_timeouts_become_ordinary_failures(monkeypatch):
    from trader import updater

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=120)
    monkeypatch.setattr(updater.subprocess, "run", hang)
    with pytest.raises(subprocess.CalledProcessError) as e:
        updater.git(".", "fetch")
    assert "timed out" in e.value.stderr
