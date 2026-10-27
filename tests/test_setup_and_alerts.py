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

    def latest_chat_id(self, code=None):
        assert code and code.isdigit() and len(code) == 6  # the wizard pairs with a one-time code
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
               "",                      # Claude daily budget: default $5
               "",                      # free AI: no (the default when none is set)
               "", "123:abc", "", "7",  # Telegram yes, token, press Enter after messaging bot, report hour
               "2",                     # strategy: the hourly agents
               "btc, eth",              # core coins
               "", "",                  # scout may add coins (yes), up to 8
               ""]                      # check every 15 minutes
    make(answers).run()
    assert not answers  # every question answered, none extra
    v = dotenv_values(env)
    assert v["AI_DAILY_BUDGET_USD"] == "5" and v["AUTO_COINS"] == "true" and v["MAX_COINS"] == "8"
    assert v["CYCLE_MINUTES"] == "15" and v["SETTINGS_VERSION"] == "3"
    assert v["TRADING_MODE"] == "testnet" and v["BINANCE_TESTNET_API_KEY"] == "KEY"
    assert v["STARTING_BALANCE_USDT"] == "4500"
    assert v["CLAUDE_MODEL"] == "claude-sonnet-5-5" and v["ANTHROPIC_API_KEY"] == "sk-ant-good"
    assert v["TELEGRAM_CHAT_ID"] == "999" and v["DAILY_REPORT_HOUR_UTC"] == "7"
    assert v["SYMBOLS"] == "BTCUSDT,ETHUSDT" and v["MAX_POSITION_PCT"] == "0.10" and v["MENTOR_CHECKS"] == "trend200,btc,cooldown,reward"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert FakeTelegram.sent[-1][0] == "999"  # test message went to the detected chat


def test_wizard_rerun_keeps_values_on_enter(wiz):
    env, make = wiz
    make(["", "KEY", "SECRET", "", "y", "sk-ant-good", "", "3", "", "", "123:abc", "", "", "2", "", "", "12", "20"]).run()
    before = dotenv_values(env)
    assert before["AI_DAILY_BUDGET_USD"] == "3" and before["MAX_COINS"] == "12" and before["CYCLE_MINUTES"] == "20"
    # Enter everywhere: keep mode, keys, budget, Claude, model, AI cap, free AI (none), Telegram (+ same chat), hour,
    # strategy, coins, scout, max coins, interval
    answers = [""] * 18
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
          "n", "n", "n", "2", "", "", "", ""]).run()  # no Claude/free AI/Telegram, hourly, default coins, scout, max, interval
    v = dotenv_values(env)
    assert v["TRADING_MODE"] == "simulation" and v["ANTHROPIC_API_KEY"] == "" and v["TELEGRAM_BOT_TOKEN"] == ""


def test_wizard_rejects_unknown_coin_and_bad_claude_key(wiz):
    env, make = wiz
    make(["2", "abc", "2,500",              # paper trading, invalid budget re-asked, then 2,500
          "y", "sk-bad", "", "n",           # Claude key fails, don't retry -> rules
          "n",                              # no free AI
          "n",                              # no Telegram
          "2",                              # the hourly agents
          "BTC, NOTACOIN", "SOL",           # typo is rejected, asked again
          "n",                              # scout may not add coins
          "90", "20"]).run()                # interval outside 5-60 is re-asked
    v = dotenv_values(env)
    assert v["ANTHROPIC_API_KEY"] == "" and v["SYMBOLS"] == "SOLUSDT" and v["STARTING_BALANCE_USDT"] == "2500"
    assert v["AUTO_COINS"] == "false" and v["CYCLE_MINUTES"] == "20"


def test_wizard_offers_fresh_journal_when_mode_changes(wiz, tmp_path):
    env, make = wiz
    make(["2", "", "n", "n", "n", "2", "", "", "", ""]).run()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "journal.db").write_text("old")
    make(["1", "KEY", "SECRET", "", "n", "n", "n", "", "", "", "", "", "y"]).run()  # switch paper -> testnet
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
    queue = []  # (status, body) replies used in order before falling back to status/body
    status, body = 200, {}

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeClaude.requests.append({"path": self.path, "beta": self.headers.get("anthropic-beta", ""), "body": data})
        status, body = FakeClaude.queue.pop(0) if FakeClaude.queue else (FakeClaude.status, FakeClaude.body)
        payload = json.dumps(body).encode()
        self.send_response(status)
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
    FakeClaude.queue.clear()
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


def search_turn(text, stop, url=None, searches=0):
    content = []
    if searches:
        content += [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                     "input": {"query": "crypto news today"}},
                    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                     "content": [{"type": "web_search_result", "url": url, "title": "News",
                                  "encrypted_content": "abc", "page_age": None}]}]
    cites = [{"type": "web_search_result_location", "url": url, "title": "News", "encrypted_index": "x",
              "cited_text": "..."}] if url else None
    content.append({"type": "text", "text": text, "citations": cites})
    return {"id": "msg_s", "type": "message", "role": "assistant", "model": "m", "stop_reason": stop,
            "stop_sequence": None, "content": content,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "server_tool_use": {"web_search_requests": searches}}}


def test_claude_research_resumes_paused_search_and_collects_sources(claude_server):
    claude_server.queue += [(200, search_turn("AVAX: upgrade live. ", "pause_turn", "https://news.example/avax", 1)),
                            (200, search_turn("SUI: unlock next week.", "end_turn", "https://news.example/sui", 1))]
    llm = ClaudeJSON("claude-opus-5-5", "k")
    out = llm.research("sys", "find catalysts", max_searches=3)
    assert out == {"text": "AVAX: upgrade live. SUI: unlock next week.",
                   "sources": ["https://news.example/avax", "https://news.example/sui"]}
    first, second = claude_server.requests[-2:]
    assert first["body"]["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    resumed = second["body"]["messages"]
    assert [m["role"] for m in resumed] == ["user", "assistant"]  # paused turn sent back, no extra "continue"
    assert resumed[1]["content"][0]["type"] == "server_tool_use"
    # spend = 2 x (1000 in @ $4/M + 200 out @ $20/M) + 2 searches @ $0.01
    assert llm.spent_today == pytest.approx(2 * (0.004 + 0.004) + 0.02)


def test_haiku_uses_basic_web_search_tool(claude_server):
    claude_server.body = search_turn("notes", "end_turn")
    ClaudeJSON("claude-haiku-5-5", "k").research("sys", "p")
    assert claude_server.requests[-1]["body"]["tools"][0]["type"] == "web_search_20250305"


def test_daily_budget_stops_claude_calls(claude_server):
    claude_server.body = message('{"ok": true}')
    llm = ClaudeJSON("claude-opus-5-5", "k", daily_budget_usd=0.0001)
    assert llm.ask("s", "p", SCHEMA) == {"ok": True}       # first call allowed, spends ~$0.0001+
    n = len(claude_server.requests)
    assert llm.ask("s", "p", SCHEMA) is None and llm.research("s", "p") is None
    assert len(claude_server.requests) == n and llm.budget_hit  # no further requests sent


def test_wizard_moves_old_hourly_setting_to_15_minutes(wiz):
    env, make = wiz
    env.write_text("TRADING_MODE=simulation\nSTARTING_BALANCE_USDT=10000\nCYCLE_MINUTES=60\nSYMBOLS=BTCUSDT\n")
    make(["", "", "n", "n", "n", "", "", "n", ""]).run()  # Enter on the interval question keeps the new default
    v = dotenv_values(env)
    assert v["CYCLE_MINUTES"] == "15" and v["SETTINGS_VERSION"] == "3"


def test_wizard_max_coins_never_below_core_count(wiz):
    env, make = wiz
    make(["2", "", "n", "n", "n", "2", "btc,eth,sol", "", "2", "4", ""]).run()  # 2 < 3 core coins: asked again
    assert dotenv_values(env)["MAX_COINS"] == "4"


def test_secrets_never_reach_logs(caplog, monkeypatch):
    import requests as rq
    from trader.binance_client import BinanceClient, scrub

    tg = Telegram("123456:SECRET-TOKEN", "1")

    def boom(*a, **k):
        raise rq.ConnectionError("Max retries exceeded with url: /bot123456:SECRET-TOKEN/sendMessage")
    monkeypatch.setattr(rq, "post", boom)
    with caplog.at_level("WARNING"):
        assert tg.send("hello") is False
    assert "SECRET-TOKEN" not in caplog.text and "<bot-token>" in caplog.text

    assert scrub("url: /api/v3/order?symbol=BTCUSDT&timestamp=1&signature=abc123def") == \
        "url: /api/v3/order?symbol=BTCUSDT&timestamp=1&signature=<redacted>"
    c = BinanceClient("https://127.0.0.1:9", "key", "secret", timeout=0.2)
    with pytest.raises(rq.RequestException) as err:
        c.balances()
    assert "signature=<redacted>" in str(err.value) and "key" not in str(err.value).split("?")[0][-10:]


def test_pairing_code_picks_the_right_chat(monkeypatch):
    tg = Telegram("t")
    monkeypatch.setattr(tg, "call", lambda method, **p: [
        {"update_id": 1, "message": {"chat": {"id": 7, "type": "private"}, "text": "hi"}},        # stranger: START
        {"update_id": 2, "message": {"chat": {"id": 9, "type": "group"}, "text": "482100"}},     # code in a group
        {"update_id": 3, "message": {"chat": {"id": 42, "type": "private"}, "text": "482100"}},  # the owner
        {"update_id": 4, "message": {"chat": {"id": 8, "type": "private"}, "text": "my 482100 guess"}},  # not exact
    ])
    assert tg.latest_chat_id("482100") == "42" and tg.latest_chat_id("000000") is None


def test_wizard_moves_the_old_take_profit_default_that_blocked_every_buy(wiz):
    env, make = wiz
    env.write_text("TRADING_MODE=simulation\nSTARTING_BALANCE_USDT=10000\nSETTINGS_VERSION=2\nSTOP_LOSS_ATR=2.0\n"
                   "TAKE_PROFIT_ATR=3.0\nSYMBOLS=BTCUSDT\n")
    make(["", "", "n", "n", "n", "", "", "n", ""]).run()
    v = dotenv_values(env)
    assert v["TAKE_PROFIT_ATR"] == "4.0" and v["SETTINGS_VERSION"] == "3"
    env.write_text("TRADING_MODE=simulation\nSTARTING_BALANCE_USDT=10000\nSETTINGS_VERSION=2\nTAKE_PROFIT_ATR=3.0\n"
                   "MIN_REWARD_RISK=1.5\nSYMBOLS=BTCUSDT\n")
    make(["", "", "n", "n", "n", "", "", "n", ""]).run()
    assert dotenv_values(env)["TAKE_PROFIT_ATR"] == "3.0"   # chosen on purpose together with a 1.5:1 minimum


def test_wizard_sets_up_the_daily_trend_rule(wiz, monkeypatch):
    env, make = wiz
    monkeypatch.setattr(setup_wizard, "check_symbol", lambda s: True)
    said = []
    monkeypatch.setattr(setup_wizard, "check_demo_prices", lambda symbols: {"DOGEUSDT": "not listed on the demo exchange"})
    w = setup_wizard.Wizard(env, ask=lambda q: "", ask_secret=lambda q: "", say=said.append)
    w.v["TRADING_MODE"] = "testnet"
    w.step_trend("")
    assert any("DOGE not listed on the demo exchange" in str(x) for x in said)
    make(["2", "", "n", "n", "n", "", "", ""]).run()     # a new desk: the trend rule is the default, the 8 coins, normal
    v = dotenv_values(env)
    assert v["STRATEGY"] == "trend" and v["SYMBOLS"] == setup_wizard.TREND_COINS and v["MAX_OPEN_POSITIONS"] == "8"
    assert v["AUTO_COINS"] == "false" and v["TREND_VOL_TARGET"] == "0.5"
    make(["", "", "n", "n", "n", "", "btc,eth,sol", "2"]).run()   # keep the rule, 3 coins, calmer
    v = dotenv_values(env)
    assert v["SYMBOLS"] == "BTCUSDT,ETHUSDT,SOLUSDT" and v["MAX_OPEN_POSITIONS"] == "3" and v["TREND_VOL_TARGET"] == "0.25"
    make(["", "", "n", "n", "n", "2", "", "", ""]).run()   # back to the hourly agents: coins, scout (off), interval
    v = dotenv_values(env)
    assert v["STRATEGY"] == "hourly" and v["MAX_OPEN_POSITIONS"] == "3"
