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


# ---- 1.7.5: findings of the security and conversation review ----
def env_with(tmp_path, **values):
    env, data = tmp_path / ".env", tmp_path / "data"
    write_env(env, {"TRADING_MODE": "testnet", "BINANCE_TESTNET_API_KEY": "KEY", "BINANCE_TESTNET_API_SECRET": "SECRET",
                    "SYMBOLS": "BTCUSDT", "STRATEGY": "hourly", "CYCLE_MINUTES": "15", **values})
    return env, data


def test_a_forged_request_cannot_save_a_value_the_desk_cannot_load(tmp_path):
    env, data = env_with(tmp_path)
    before = env.read_text()
    for key, value in [("CYCLE_MINUTES", "15.0"), ("CYCLE_MINUTES", "1e1"), ("CYCLE_MINUTES", "$15"),
                       ("CYCLE_MINUTES", " 15"), ("MAX_OPEN_POSITIONS", "3.0"), ("DAILY_REPORT_HOUR_UTC", "6.0"),
                       ("STARTING_BALANCE_USDT", "50,000"), ("STARTING_BALANCE_USDT", "$5000"), ("PULSE_HOURS", "6,5"),
                       ("SYMBOLS", "BTCUSDT\n,ETHUSDT"), ("SYMBOLS", "BTCUSDT\n"), ("STRATEGY", "trend "),
                       ("TREND_VOL_TARGET", "5e-1")]:
        rs.write_request(data, {key: value})
        message, applied = rs.apply_request(env, data)
        assert not applied and "not applied" in message, (key, value)
    assert env.read_text() == before
    for key, value in [("CYCLE_MINUTES", "15"), ("PULSE_HOURS", "6.5"), ("STARTING_BALANCE_USDT", "2500.5"),
                       ("TREND_VOL_TARGET", "0.25")]:
        assert rs.check_value(key, value, {}) is None


def test_a_file_that_would_not_load_is_put_back(tmp_path, monkeypatch):
    env, data = env_with(tmp_path)
    before = env.read_text()
    monkeypatch.setattr(rs, "check_value", lambda *a: None)          # as if a check let a bad value through
    rs.write_request(data, {"CYCLE_MINUTES": "15.0"})
    message, applied = rs.apply_request(env, data)
    assert not applied and "did not load" in message and env.read_text() == before


def test_the_backup_never_writes_through_a_link(tmp_path):
    env, data = env_with(tmp_path)
    data.mkdir()
    readme = tmp_path / "README.md"
    readme.write_text("docs\n")
    (data / rs.BACKUP_NAME).symlink_to(readme)
    rs.write_request(data, {"PULSE_HOURS": "12"})
    assert rs.apply_request(env, data)[1]
    backup = data / rs.BACKUP_NAME
    assert readme.read_text() == "docs\n" and not backup.is_symlink() and "SECRET" in backup.read_text()
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600


def test_a_request_that_is_a_link_or_a_pipe_is_refused_without_waiting(tmp_path):
    env, data = env_with(tmp_path)
    data.mkdir()
    before = env.read_text()
    (data / rs.REQUEST_NAME).symlink_to(env)
    message, applied = rs.apply_request(env, data)
    assert not applied and "could not be read" in message and env.read_text() == before
    os.mkfifo(data / rs.REQUEST_NAME)
    message, applied = rs.apply_request(env, data)                  # returns at once instead of waiting for a writer
    assert not applied and not os.path.lexists(data / rs.REQUEST_NAME)


def test_a_request_sent_while_one_is_applied_waits_for_the_next_run(tmp_path, monkeypatch):
    env, data = env_with(tmp_path)
    rs.write_request(data, {"PULSE_HOURS": "12"})
    real = rs.check_value
    sent = []

    def during(*a):
        if not sent:   # the owner confirms a second /setup while the job is checking the first one
            sent.append(rs.write_request(data, {"DAILY_REPORT_HOUR_UTC": "8"}))
        return real(*a)
    monkeypatch.setattr(rs, "check_value", during)
    assert rs.apply_request(env, data)[1]
    assert rs.pending_request(data) == {"DAILY_REPORT_HOUR_UTC": "8"}
    monkeypatch.setattr(rs, "check_value", real)
    assert rs.apply_request(env, data)[1] and dotenv_values(env)["DAILY_REPORT_HOUR_UTC"] == "8"


def test_a_second_confirmation_keeps_the_first_and_an_old_request_is_not_applied(tmp_path):
    env, data = env_with(tmp_path)
    rs.write_request(data, {"PULSE_HOURS": "12"})
    rs.write_request(data, {"DAILY_REPORT_HOUR_UTC": "8"})
    assert rs.pending_request(data) == {"PULSE_HOURS": "12", "DAILY_REPORT_HOUR_UTC": "8"}
    request = json.loads((data / rs.REQUEST_NAME).read_text())
    request["requested_at"] = "2020-01-01T00:00:00+00:00"
    (data / rs.REQUEST_NAME).write_text(json.dumps(request))
    message, applied = rs.apply_request(env, data)
    assert not applied and "reached the update job only now" in message and "PULSE_HOURS" not in env.read_text()


def test_a_phone_change_keeps_what_the_desk_does_with_missing_lines(tmp_path):
    env, data = tmp_path / ".env", tmp_path / "data"
    env.write_text("SYMBOLS=BTCUSDT\nMENTOR_CHECKS=\nCYCLE_MINUTES=15\nSETTINGS_VERSION=4\n")
    rs.write_request(data, {"PULSE_HOURS": "12"})
    assert rs.apply_request(env, data)[1]
    v = dotenv_values(env)
    assert v["TRADING_MODE"] == "simulation" and v["MENTOR_CHECKS"] == "none" and v["PULSE_HOURS"] == "12"


def test_budgets_are_read_like_people_write_them():
    p = session()
    for typed, saved in [("10,000", "10000"), ("$4,000", "4000"), ("1234567", "1234567"), ("2500.5", "2500.5"),
                         ("10 000 USDT", "10000")]:
        p.answer("4")
        p.answer(typed)
        assert p.pending["STARTING_BALANCE_USDT"] == saved, typed
    p.answer("4")
    text, _ = p.answer("10,5")
    assert "must be a number from 10 to 10,000,000" in text and p.pending["STARTING_BALANCE_USDT"] == "10000"


def test_the_saved_value_is_not_offered_as_a_change():
    p = session({**session().current, "CYCLE_MINUTES": "15", "DAILY_REPORT_HOUR_UTC": "6"})
    p.answer("6"), p.answer("15"), p.answer("7"), p.answer("6.0")
    assert p.pending == {}


def test_changing_the_strategy_back_restores_the_saved_settings():
    hourly = session({**session().current, "SYMBOLS": "BTCUSDT,XRPUSDT", "MAX_OPEN_POSITIONS": "5", "AUTO_COINS": "false"})
    for msg in ("1", "1", "1", "1", "1", "2"):   # trend with the tested coins, Normal, then hourly again
        hourly.answer(msg)
    assert hourly.pending == {}
    trend = session({**session().current, "STRATEGY": "Trend", "SYMBOLS": rs.TREND_COINS, "MAX_OPEN_POSITIONS": "8",
                     "AUTO_COINS": "false"})
    assert "daily trend rule" in trend.start()[0]                     # read like the desk reads it
    trend.answer("1"), trend.answer("2"), trend.answer("1"), trend.answer("1")
    assert trend.pending == {}


def test_the_trend_coins_question_takes_only_its_answers():
    p = session()
    p.answer("1"), p.answer("1")
    text, _ = p.answer("3")
    assert "Send 1" in text and p.state == "trend_coins"
    p.answer("0 Back")
    assert p.pending == {} and p.state == "menu"


def test_a_long_coin_message_never_reaches_binance():
    asked = []
    p = rs.PhoneSetup({"STRATEGY": "hourly"}, check_symbol=lambda c: asked.append(c) or c != "FAKEUSDT")
    p.answer("2")
    text, _ = p.answer(" ".join(a + b for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    assert asked == [] and "1 to 20" in text
    p.answer("btc fake eth")
    assert asked == ["BTCUSDT", "FAKEUSDT"]                           # stops at the first coin Binance lacks


def test_a_mode_change_checks_the_keys_and_the_open_positions():
    base = {"STRATEGY": "hourly", "TRADING_MODE": "simulation", "BINANCE_TESTNET_API_KEY": "k",
            "BINANCE_TESTNET_API_SECRET": "s"}
    refused = rs.PhoneSetup(base, check_keys=lambda: "HTTP 401: Invalid API-key")
    refused.answer("5")
    text, _ = refused.answer("1")
    assert "did not accept the demo keys" in text and "TRADING_MODE" not in refused.pending
    holding = rs.PhoneSetup(base, check_keys=lambda: None, held=["ETHUSDT"])
    holding.answer("5")
    text, _ = holding.answer("1")
    assert "holds ETH" in text and "TRADING_MODE" not in holding.pending
    fine = rs.PhoneSetup(base, check_keys=lambda: None)
    fine.answer("5"), fine.answer("1")
    assert fine.pending == {"TRADING_MODE": "testnet"}


def test_the_confirm_screen_names_the_coins_the_trend_rule_will_sell():
    p = session(held=["PEPEUSDT", "BTCUSDT"])
    p.answer("1"), p.answer("1"), p.answer("1"), p.answer("1")
    text, _ = p.answer("9")
    assert "holds PEPE, which is not in the list" in text and "BTC," not in text
    two = session(held=["PEPEUSDT", "ZECUSDT"])
    two.answer("1"), two.answer("1"), two.answer("1"), two.answer("2")
    text, _ = two.answer("9")
    assert "holds PEPE ZEC, which are not in the list: the trend rule sells them" in text
    assert "Risk level: Normal → Calmer" in text


def test_a_star_message_without_a_setup_gets_the_help():
    tg = Telegram("t", "1")
    sent = []
    tg.call = lambda method, **p: sent.append(p) or {}
    handlers = {"/help": lambda: "help", "*": lambda text: None}
    tg.handle([{"update_id": 1, "message": {"chat": {"id": 1}, "text": "* hi"}}], handlers, None)
    assert sent[0]["text"] == "help"
