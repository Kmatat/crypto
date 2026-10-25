"""Fixes from the security audit."""
import os
import stat
from types import SimpleNamespace

import pytest

from trader.agents.execution import ExecutionAgent, TestnetBroker
from trader.agents.risk import RiskManagerAgent
from trader.config import Settings
from trader.journal import Journal
from trader.llm import ClaudeJSON
from trader.notify import clean
from trader.orchestrator import Orchestrator
from trader.setup_wizard import write_env
from tests.test_protection import FakeClient


def test_env_file_is_private_from_the_first_byte_and_written_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "umask", lambda m: 0o022)
    env = tmp_path / ".env"
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))  # mode of the temp file before it becomes .env
        real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    write_env(env, {"TRADING_MODE": "simulation", "ANTHROPIC_API_KEY": "sk-secret"})
    assert seen == [0o600] and stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp")) and "sk-secret" in env.read_text()


def test_settings_repr_hides_secrets():
    s = Settings(api_key="K1", api_secret="S1", anthropic_key="A1", telegram_token="T1")
    assert not any(x in repr(s) for x in ("K1", "S1", "A1", "T1"))


def test_clean_strips_links_and_control_characters():
    assert clean("Buy now: https://evil.example/login \x1b[31m urgent") == "Buy now: [link removed] [31m urgent"
    assert clean("x" * 500, limit=100).endswith("…") and len(clean("x" * 500, limit=100)) == 100
    assert clean(None) == ""


def test_claude_spend_survives_a_restart():
    j = Journal(":memory:", 10_000)
    a = ClaudeJSON("claude-opus-5-5", "k", daily_budget_usd=1.0, store=j)
    a._roll_day()
    a._spent = 0.95
    a._save()
    b = ClaudeJSON("claude-opus-5-5", "k", daily_budget_usd=1.0, store=j)  # "restart"
    assert b.spent_today == pytest.approx(0.95)
    b._spent = 1.01
    b._save()
    c = ClaudeJSON("claude-opus-5-5", "k", daily_budget_usd=1.0, store=j)
    assert c.ask("s", "p", {"type": "object", "properties": {}, "additionalProperties": False}) is None and c.budget_hit


PORT = {"cash": 10_000, "equity": 10_000, "open_positions": 0, "day_start_equity": 10_000, "starting_balance": 10_000}


def test_overall_loss_brake_blocks_new_buys():
    r = RiskManagerAgent(Settings())
    buy = {"action": "BUY", "confidence": 0.9}
    assert r.review(buy, "BTCUSDT", 100, 2, PORT)["approved"]
    # down 11% overall but flat today: the daily limit would allow it, the overall brake does not
    v = r.review(buy, "BTCUSDT", 100, 2, PORT | {"equity": 8_900, "day_start_equity": 8_900, "cash": 8_900})
    assert not v["approved"] and v["limit"] == "overall" and "overall loss limit" in v["reason"]
    assert r.review({"action": "SELL", "confidence": 0.1}, "BTCUSDT", 100, 2, PORT | {"equity": 8_000})["approved"]


def test_live_stop_limit_leg_is_executing_not_gone():
    fc = FakeClient()
    b = TestnetBroker(fc)
    prot = b.protect("BTCUSDT", 0.02, 62000, 70000)
    fc.orders[11]["status"] = "EXPIRED"   # take-profit cancelled by the exchange...
    fc.orders[12]["status"] = "NEW"       # ...because the stop triggered and its limit order is now working
    assert b.protection_status("BTCUSDT", prot)["state"] == "executing"
    fc.orders[12]["status"] = "EXPIRED"
    assert b.protection_status("BTCUSDT", prot)["state"] == "gone"
    b.cancel_quietly("BTCUSDT", prot)
    assert fc.cancelled == [prot["list_client_id"]]
    fc.cancel_error = "HTTP 400: Unknown order sent."
    b.cancel_quietly("BTCUSDT", prot)  # never raises


def test_reconcile_flags_positions_whose_coins_vanished():
    s = Settings(offline=True, symbols=["BTCUSDT"])
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.journal.open_position("BTCUSDT", {"qty": 0.5, "avg_price": 100, "quote": 50}, 90, 120)
    bot.testnet = SimpleNamespace(balances_total=lambda: {"BTC": 0.5, "USDT": 900})
    assert bot.reconcile() == []
    bot.testnet = SimpleNamespace(balances_total=lambda: {"USDT": 10_000})  # testnet reset wiped the coins
    out = bot.reconcile()
    assert out[0]["symbol"] == "BTCUSDT" and "desk reset --force" in out[0]["error"]


def test_unprotected_position_always_forces_fast_checks():
    s = Settings(offline=True, symbols=["BTCUSDT"])
    bot = Orchestrator(s, Journal(":memory:", s.starting_balance))
    bot.journal.open_position("BTCUSDT", {"qty": 1, "avg_price": 100, "quote": 100}, 90, 120)
    far_from_exits = {"BTCUSDT": {"price": 105, "technical": {"atr": 1}}}
    assert bot._next_pace({"minutes": 30, "reason": "calm"}, far_from_exits)["source"] == "risk"
    bot.journal.set_protection("BTCUSDT", {"kind": "exchange"}, "exchange")
    assert bot._next_pace({"minutes": 30, "reason": "calm"}, far_from_exits)["source"] == "AI"


def test_single_instance_lock(tmp_path):
    from trader.__main__ import single_instance
    db = str(tmp_path / "journal.db")
    held = single_instance(db)
    with pytest.raises(SystemExit, match="already running"):
        single_instance(db)
    held.close()
    single_instance(db).close()


def test_env_keys_list_every_variable_the_settings_loader_reads():
    import re
    from pathlib import Path
    from trader import config
    read = set(re.findall(r'(?:getenv|_f|_b)\("([A-Z_]+)"', Path(config.__file__).read_text()))
    assert read and read <= set(config.ENV_KEYS)


def test_update_job_runs_the_tests_without_the_servers_settings_or_keys(monkeypatch, tmp_path):
    import sys
    from trader.updater import Updater
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("STARTING_BALANCE_USDT", "120")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "kept")
    u = Updater(Settings(), tg=SimpleNamespace(send=lambda t: True), root=tmp_path, status_path=tmp_path / "s.json",
                request_path=tmp_path / "r", say=lambda *a: None)
    probe = [sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY'), "
                                   "os.environ.get('STARTING_BALANCE_USDT'), os.environ.get('SOME_UNRELATED_VAR'))"]
    ok, out = u._run(probe, 30)
    assert ok and out.split() == ["None", "None", "kept"]
