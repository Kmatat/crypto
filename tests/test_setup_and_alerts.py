import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from dotenv import dotenv_values

from trader import setup_wizard
from trader.binance_client import BinanceError
from trader.llm import ClaudeJSON
from trader.notify import Telegram, trade_messages


# ---------------------------------------------------------------- wizard
class FakeTelegram:
    sent = []

    def __init__(self, token, chat_id=""):
        self.token, self.chat_id = token, chat_id

    def bot_username(self):
        if self.token != "123:abc":
            raise RuntimeError("Unauthorized")
        return "desk_bot"

    def latest_chat_id(self):
        return "999"

    def send(self, text):
        FakeTelegram.sent.append((self.chat_id, text))
        return True


@pytest.fixture
def wiz(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "ROOT", tmp_path)
    monkeypatch.setattr(setup_wizard, "Telegram", FakeTelegram)
    monkeypatch.setattr(setup_wizard, "check_binance", lambda k, s: {"USDT": 5000.0, "BTC": 1.0})
    monkeypatch.setattr(setup_wizard, "check_claude", lambda model, key: key == "sk-ant-good")
    monkeypatch.setattr(setup_wizard, "check_symbol", lambda s: s in {"BTCUSDT", "ETHUSDT", "SOLUSDT"})
    env = tmp_path / ".env"

    def make(answers):
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return answers.pop(0)
        w = setup_wizard.Wizard(env, ask=answer, ask_secret=answer, say=lambda *a: None)
        w.prompts = prompts
        real_run = w.run

        def run_and_check():
            result = real_run()
            assert not answers, f"wizard skipped questions; unused answers: {answers}"
            return result
        w.run = run_and_check
        return w
    return env, make


def test_wizard_first_run_writes_private_env(wiz):
    env, make = wiz
    answers = ["",                      # mode: 1 = testnet
               "KEY", "SECRET",         # binance keys
               "",                      # budget: default capped to 90% of the 5000 USDT demo balance
               "y", "sk-ant-good", "2",  # Claude yes, key, Sonnet
               "", "123:abc", "", "7",  # Telegram yes, token, press Enter after messaging bot, report hour
               "btc, eth"]              # coins
    make(answers).run()
    assert not answers  # every question answered, none extra
    v = dotenv_values(env)
    assert v["TRADING_MODE"] == "testnet" and v["BINANCE_TESTNET_API_KEY"] == "KEY"
    assert v["STARTING_BALANCE_USDT"] == "4500"
    assert v["CLAUDE_MODEL"] == "claude-sonnet-5-5" and v["ANTHROPIC_API_KEY"] == "sk-ant-good"
    assert v["TELEGRAM_CHAT_ID"] == "999" and v["DAILY_REPORT_HOUR_UTC"] == "7"
    assert v["SYMBOLS"] == "BTCUSDT,ETHUSDT" and v["MAX_POSITION_PCT"] == "0.10"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert FakeTelegram.sent[-1][0] == "999"  # test message went to the detected chat


def test_wizard_rerun_keeps_values_on_enter(wiz):
    env, make = wiz
    make(["", "KEY", "SECRET", "", "y", "sk-ant-good", "", "", "123:abc", "", "", ""]).run()
    before = dotenv_values(env)
    # Enter everywhere: keep mode, keys, budget, Claude, model, Telegram (+ same chat), hour, coins
    answers = ["", "", "", "", "", "", "", "", "", "", "", ""]
    w = make(answers)
    w.run()
    assert dotenv_values(env) == before
    assert any("keep current ••••" in p for p in w.prompts)


def test_wizard_restricted_location_falls_back_to_paper(wiz, monkeypatch):
    env, make = wiz

    def blocked(k, s):
        raise BinanceError("HTTP 451: Service unavailable from a restricted location")
    monkeypatch.setattr(setup_wizard, "check_binance", blocked)
    make(["", "KEY", "SECRET", "2",   # testnet, keys, then "switch to paper trading"
          "n", "n", ""]).run()         # no Claude, no Telegram, default coins
    v = dotenv_values(env)
    assert v["TRADING_MODE"] == "simulation" and v["ANTHROPIC_API_KEY"] == "" and v["TELEGRAM_BOT_TOKEN"] == ""


def test_wizard_rejects_unknown_coin_and_bad_claude_key(wiz):
    env, make = wiz
    make(["2", "abc", "2,500",              # paper trading, invalid budget re-asked, then 2,500
          "y", "sk-bad", "", "n",           # Claude key fails, don't retry -> rules
          "n",                              # no Telegram
          "BTC, NOTACOIN", "SOL"]).run()    # typo is rejected, asked again
    v = dotenv_values(env)
    assert v["ANTHROPIC_API_KEY"] == "" and v["SYMBOLS"] == "SOLUSDT" and v["STARTING_BALANCE_USDT"] == "2500"


def test_wizard_offers_fresh_journal_when_mode_changes(wiz, tmp_path):
    env, make = wiz
    make(["2", "", "n", "n", ""]).run()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "journal.db").write_text("old")
    make(["1", "KEY", "SECRET", "", "n", "n", "", "y"]).run()  # switch paper -> testnet
    assert not (tmp_path / "data" / "journal.db").exists()
    assert len(list((tmp_path / "data").glob("journal-*.db"))) == 1


# ---------------------------------------------------------------- telegram
SUMMARY = [
    {"symbol": "BTCUSDT", "action": "BUY", "confidence": 0.8, "reasoning": "Trend up.", "stop_loss": 60000.0,
     "take_profit": 70000.0, "order": {"ok": True, "qty": 0.01, "avg_price": 65000.0, "quote": 650.0}},
    {"symbol": "ETHUSDT", "action": "SELL", "reason": "take-profit",
     "order": {"ok": True, "qty": 0.5, "avg_price": 3000.0, "quote": 1500.0}, "trade": {"pnl": 42.0, "pnl_pct": 2.9}},
    {"symbol": "SOLUSDT", "action": "BUY", "order": {"ok": False, "error": "Invalid API-key, IP, or permissions"}},
    {"symbol": "BNBUSDT", "error": "timeout"},
    {"symbol": "XRPUSDT", "action": "HOLD", "confidence": 0.6},
]


def test_trade_messages_cover_fills_failures_and_errors():
    msgs = trade_messages(SUMMARY)
    assert len(msgs) == 4  # HOLD produces no alert
    assert msgs[0][0] == "" and "BUY BTCUSDT" in msgs[0][1] and "$60,000.00" in msgs[0][1]
    assert "take-profit target reached" in msgs[1][1] and "+42.00" in msgs[1][1]
    assert msgs[2][0] == "order:SOLUSDT:BUY" and "desk setup" in msgs[2][1]
    assert msgs[3][0] == "error:BNBUSDT"


def test_send_once_deduplicates(monkeypatch):
    tg = Telegram("t", "1")
    sent = []
    monkeypatch.setattr(tg, "call", lambda method, **p: sent.append(p["text"]) or {})
    assert tg.send_once("k", "first") and not tg.send_once("k", "again") and tg.send_once("other", "x")
    assert sent == ["first", "x"]


def test_commands_only_answer_the_owner(monkeypatch):
    tg = Telegram("t", "42")
    replies = []
    monkeypatch.setattr(tg, "send", replies.append)
    updates = [{"update_id": 5, "message": {"chat": {"id": 7}, "text": "/report"}},       # stranger
               {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/status@desk_bot"}},
               {"update_id": 7, "message": {"chat": {"id": 42}, "text": "hello"}}]
    offset = tg.handle(updates, {"/status": lambda: "S", "/help": lambda: "H"}, None)
    assert replies == ["S", "H"] and offset == 8


def test_disabled_telegram_never_sends():
    assert Telegram("", "").send("x") is False


# ---------------------------------------------------------------- claude request shape
class FakeClaude(BaseHTTPRequestHandler):
    requests = []
    status, body = 200, {}

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeClaude.requests.append({"path": self.path, "beta": self.headers.get("anthropic-beta", ""), "body": data})
        payload = json.dumps(FakeClaude.body).encode()
        self.send_response(FakeClaude.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def claude_server(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeClaude)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    FakeClaude.requests.clear()
    FakeClaude.status = 200
    yield FakeClaude
    srv.shutdown()


def message(text, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "stop_reason": stop,
            "stop_sequence": None, "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
          "additionalProperties": False}


def test_claude_opus_request_uses_structured_output_and_fallbacks(claude_server):
    claude_server.body = message('{"ok": true}')
    assert ClaudeJSON("claude-opus-5-5", "k").ask("sys", "hi", SCHEMA, effort="low") == {"ok": True}
    req = claude_server.requests[-1]
    assert req["path"].startswith("/v1/messages") and "server-side-fallback-2026-07-01" in req["beta"]
    body = req["body"]
    assert body["fallbacks"] == "default" and body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in body and "thinking" not in body


def test_claude_haiku_request_has_no_fallbacks(claude_server):
    claude_server.body = message('{"ok": false}')
    assert ClaudeJSON("claude-haiku-5-5", "k").ask("sys", "hi", SCHEMA) == {"ok": False}
    req = claude_server.requests[-1]
    assert "fallbacks" not in req["body"] and "server-side-fallback" not in req["beta"]


def test_claude_refusal_and_auth_errors_fall_back_to_rules(claude_server):
    claude_server.body = message("", stop="refusal")
    assert ClaudeJSON("claude-opus-5-5", "k").ask("s", "p", SCHEMA) is None
    claude_server.status = 401
    claude_server.body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    assert ClaudeJSON("claude-opus-5-5", "bad").ask("s", "p", SCHEMA) is None
