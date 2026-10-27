"""1.7.4: the setup wizard on the phone (/setup on Telegram), applied by the update job."""
import json
import stat
import os
from types import SimpleNamespace

from dotenv import dotenv_values

from trader import remote_setup as rs
from trader.notify import Telegram
from trader.setup_wizard import write_env


def session(current=None, **kw):
    return rs.PhoneSetup(current or {"STRATEGY": "hourly", "SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT", "TRADING_MODE": "testnet",
                                     "BINANCE_TESTNET_API_KEY": "k", "BINANCE_TESTNET_API_SECRET": "s",
                                     "STARTING_BALANCE_USDT": "5000", "MAX_OPEN_POSITIONS": "3", "AUTO_COINS": "true"},
                         check_symbol=lambda c: c != "FAKEUSDT", **kw)


def test_switching_to_the_trend_rule_from_the_phone():
    p = session(demo_warnings=lambda coins: {"DOGEUSDT": "not listed on the demo exchange"})
    text, buttons = p.start()
    assert "keys and tokens stay on the server" in text and buttons[0][0] == "1 Strategy"
    p.answer("1 Strategy")
    text, _ = p.answer("1 Daily trend rule")
    assert "The 8 tested coins" in text
    text, _ = p.answer("1")
    assert "DOGE not listed on the demo exchange" in text and "Risk level" in text
    p.answer("2 Calmer")
    text, buttons = p.answer("9 Apply changes")
    assert "Strategy: hourly agents → daily trend rule" in text and buttons == [["YES", "NO"]]
    p.answer("YES")
    assert p.done and p.pending == {"STRATEGY": "trend", "AUTO_COINS": "false", "SYMBOLS": rs.TREND_COINS,
                                    "MAX_OPEN_POSITIONS": "8", "TREND_VOL_TARGET": "0.25"}


def test_the_phone_refuses_what_cannot_work():
    p = session()
    p.answer("2")
    text, _ = p.answer("btc, fake")
    assert "Binance does not list: FAKE" in text and "SYMBOLS" not in p.pending
    p.answer("btc eth")
    assert p.pending["SYMBOLS"] == "BTCUSDT,ETHUSDT"
    p.answer("4")
    text, _ = p.answer("abc")
    assert "Budget must be a number" in text
    p.answer("2500")
    assert p.pending["STARTING_BALANCE_USDT"] == "2500"
    p.answer("3")                                                     # risk level needs the trend rule first
    assert p.state == "menu"
    no_keys = session({"TRADING_MODE": "simulation"})
    no_keys.answer("5")
    text, _ = no_keys.answer("1")
    assert "needs Binance demo keys" in text and "TRADING_MODE" not in no_keys.pending
    p.answer("0")
    assert p.cancelled and not p.done


def test_a_quiet_session_ends_by_itself():
    now = [1000.0]
    p = session(clock=lambda: now[0])
    p.start()
    now[0] += rs.SESSION_MINUTES * 60 + 1
    assert p.expired


def test_the_update_job_applies_only_safe_values_and_keeps_the_previous_file(tmp_path):
    env, data = tmp_path / ".env", tmp_path / "data"
    write_env(env, {"TRADING_MODE": "testnet", "BINANCE_TESTNET_API_KEY": "KEY", "BINANCE_TESTNET_API_SECRET": "SECRET",
                    "SYMBOLS": "BTCUSDT", "STRATEGY": "hourly"})
    rs.write_request(data, {"STRATEGY": "trend", "SYMBOLS": "BTCUSDT,ETHUSDT", "MAX_OPEN_POSITIONS": "2"})
    message, applied = rs.apply_request(env, data)
    assert applied and "Strategy: hourly agents → daily trend rule" in message
    v = dotenv_values(env)
    assert v["STRATEGY"] == "trend" and v["SYMBOLS"] == "BTCUSDT,ETHUSDT" and v["BINANCE_TESTNET_API_KEY"] == "KEY"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600 and stat.S_IMODE(os.stat(data / rs.BACKUP_NAME).st_mode) == 0o600
    assert "STRATEGY=hourly" in (data / rs.BACKUP_NAME).read_text() and not (data / rs.REQUEST_NAME).exists()
    assert rs.apply_request(env, data) is None                        # consumed
    # a request is checked again: a key or an unsafe value is never written
    for bad in ({"BINANCE_TESTNET_API_KEY": "stolen"}, {"TRADING_MODE": "live"}, {"CYCLE_MINUTES": "1"},
                {"SYMBOLS": "BTCUSDT;rm -rf"}):
        rs.write_request(data, bad)
        message, applied = rs.apply_request(env, data)
        assert not applied and "not applied" in message
    assert dotenv_values(env)["BINANCE_TESTNET_API_KEY"] == "KEY" and dotenv_values(env)["TRADING_MODE"] == "testnet"


def test_the_update_job_applies_a_phone_request_without_installing_code(tmp_path, monkeypatch):
    from trader import updater as up
    from trader.config import Settings
    root = tmp_path
    (root / "data").mkdir()
    write_env(root / ".env", {"STRATEGY": "hourly", "SYMBOLS": "BTCUSDT"})
    rs.write_request(root / "data", {"STRATEGY": "trend"})
    up.request_update(root / "data" / "update-requested", purpose="settings")
    said, restarted = [], []
    u = up.Updater(Settings(), tg=SimpleNamespace(send=lambda t: said.append(t) or True), root=root,
                   status_path=root / "data" / "update-status.json", request_path=root / "data" / "update-requested",
                   restart=lambda: restarted.append(1), say=lambda *a: None)
    jobs = []
    monkeypatch.setattr(u, "_run_job", lambda wanted, check_only: jobs.append(wanted) or {})
    u.run()
    assert dotenv_values(root / ".env")["STRATEGY"] == "trend" and restarted == [1]
    assert any(t.startswith("✅ Settings changed from your phone") for t in said)
    assert jobs == [False] and not (root / "data" / "update-requested").exists()   # no code install was forced


def test_telegram_routes_answers_to_the_setup_and_shows_buttons(monkeypatch):
    tg = Telegram("t", "1")
    sent = []
    monkeypatch.setattr(tg, "call", lambda method, **p: sent.append(p) or {})
    handlers = {"/help": lambda: "help", "/setup": lambda: ("menu", [["1 Strategy"]]),
                "*": lambda text: ("got " + text, None) if text == "2" else None}
    updates = [{"update_id": i, "message": {"chat": {"id": 1}, "text": t}} for i, t in enumerate(["/setup", "2", "hello"])]
    tg.handle(updates, handlers, None)
    assert sent[0]["text"] == "menu" and sent[0]["reply_markup"]["keyboard"] == [[{"text": "1 Strategy"}]]
    assert sent[1]["text"] == "got 2" and sent[1]["reply_markup"] == {"remove_keyboard": True}
    assert sent[2]["text"] == "help"                                   # no conversation for it: the help answers
    assert json.dumps(sent)                                            # plain JSON for Telegram


def test_a_file_from_the_phone_loads_like_one_from_the_wizard(tmp_path, monkeypatch):
    from trader import config
    from trader.config import Settings
    env, data = tmp_path / ".env", tmp_path / "data"
    env.write_text("TRADING_MODE=simulation\nCYCLE_MINUTES=60\nTAKE_PROFIT_ATR=3.0\nSYMBOLS=BTCUSDT\n")   # an old file
    rs.write_request(data, {"PULSE_HOURS": "12"})
    assert rs.apply_request(env, data)[1]
    v = dotenv_values(env)
    assert v["SETTINGS_VERSION"] == config.SETTINGS_VERSION and v["CYCLE_MINUTES"] == "15" and v["TAKE_PROFIT_ATR"] == "4.0"
    monkeypatch.setattr(config, "ENV_PATH", env)
    s = Settings.load()
    assert s.pulse_hours == 12 and s.cycle_minutes == 15 and not s.migrations
