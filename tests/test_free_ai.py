"""1.6.0: a free AI (Gemini, Groq, ...) next to Claude, rationed by requests instead of dollars."""
import datetime as dt
import json
import logging
from types import SimpleNamespace

import pytest

from trader import free_ai
from trader.agents.decision import DECISION_SCHEMA, DecisionAgent
from trader.agents.head import HeadTraderAgent
from trader.agents.news import NewsAgent
from trader.ai_router import AIRouter, ai_name, is_ai_source
from trader.config import Settings
from trader.free_ai import FreeAI, conform, parse_json
from trader.journal import Journal
from trader.orchestrator import Orchestrator
from trader.scorecard import claude_scorecard, news_scorecard, scorecard_text

KEY = "AIzaSy-SECRET-123"
T0 = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc).timestamp()   # 05:00 in California


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def ok(answer, tokens=1500):
    content = answer if isinstance(answer, str) else json.dumps(answer)
    return Resp(200, {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": tokens}})


class Session:
    """Answers each POST with the next scripted response (a Resp, or a function of the request body)."""
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "body": json, "headers": headers})
        nxt = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return nxt(json) if callable(nxt) else nxt


class Clock:
    def __init__(self, t=T0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(provider="gemini", *script, store=None, clock=None, **kw):
    clock = clock or Clock()
    return FreeAI(provider, KEY, store=store, session=Session(*script), clock=clock, sleep=clock.sleep, **kw), clock


HOLD = {"action": "HOLD", "confidence": 0.7, "reasoning": "Mixed signals."}


# ---- the request ----
def test_the_key_travels_only_in_the_header_and_the_form_is_enforced():
    ai, _ = make("gemini", ok(HOLD))
    assert ai.ask("You decide.", "BTCUSDT ...", DECISION_SCHEMA, caller="decision") == HOLD
    call = ai.session.calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert KEY not in call["url"] and call["headers"]["Authorization"] == f"Bearer {KEY}"
    body = call["body"]
    assert body["model"] == "gemini-3.5-flash-lite" and "temperature" not in body
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "answer", "schema": DECISION_SCHEMA,
                                                                              "strict": True}}
    assert KEY not in repr(ai) and KEY not in repr(Settings(free_ai_key=KEY))


def test_groq_reasoning_models_get_low_effort_and_cloudflare_gets_the_form_in_the_prompt():
    groq, _ = make("groq", ok(HOLD))
    groq.ask("s", "p", DECISION_SCHEMA)
    assert groq.session.calls[0]["body"]["reasoning_effort"] == "low"
    cf, _ = make("cloudflare", ok(HOLD), base_url="https://api.cloudflare.com/client/v4/accounts/abc/ai/v1")
    cf.ask("s", "p", DECISION_SCHEMA)
    body = cf.session.calls[0]["body"]
    assert body["response_format"] == {"type": "json_object"} and "JSON Schema" in body["messages"][0]["content"]
    assert cf.session.calls[0]["url"].startswith("https://api.cloudflare.com/client/v4/accounts/abc/ai/v1/")


# ---- the answer ----
def test_answers_are_checked_against_the_form():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('<think>hmm {not json}</think> Here: {"a": 2} done') == {"a": 2}
    fixed = conform(DECISION_SCHEMA, {"action": "hold", "confidence": "0.7", "reasoning": "x", "extra": 1})
    assert fixed == {"action": "HOLD", "confidence": 0.7, "reasoning": "x"}
    with pytest.raises(ValueError):
        conform(DECISION_SCHEMA, {"action": "MAYBE", "confidence": 0.5, "reasoning": "x"})
    with pytest.raises(ValueError):
        conform(DECISION_SCHEMA, {"action": "BUY", "reasoning": "no confidence"})
    news = {"type": "object", "properties": {"coins": {"type": "array", "items": DECISION_SCHEMA},
                                             "n": {"type": "integer"}, "on": {"type": "boolean"}},
            "required": ["coins", "n", "on"]}
    assert conform(news, {"coins": [HOLD, {"bad": 1}], "n": 14.6, "on": "true"}) == {"coins": [HOLD], "n": 15, "on": True}


def test_a_wrong_answer_tries_the_next_model_and_is_counted():
    ai, _ = make("gemini", ok({"action": "BUY"}), ok(HOLD))
    assert ai.ask("s", "p", DECISION_SCHEMA) == HOLD
    models = [c["body"]["model"] for c in ai.session.calls]
    assert models == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    used = ai.status()["usage"]["models"]
    assert used["gemini-3.5-flash-lite"]["bad"] == 1 and used["gemini-3.1-flash-lite"]["answers"] == 1


def test_a_refused_schema_format_is_asked_again_with_the_form_in_the_prompt():
    refused = Resp(400, [{"error": {"code": 400, "message": "Invalid JSON payload: response_format.json_schema.schema "
                                                            "has unsupported field", "status": "INVALID_ARGUMENT"}}])
    ai, _ = make("gemini", refused, ok(HOLD))
    assert ai.ask("s", "p", DECISION_SCHEMA) == HOLD
    first, second = ai.session.calls[0]["body"], ai.session.calls[1]["body"]
    assert first["response_format"]["type"] == "json_schema" and second["response_format"] == {"type": "json_object"}
    assert first["model"] == second["model"]                      # same model, form in the prompt from now on
    ai.ask("s", "p", DECISION_SCHEMA)
    assert ai.session.calls[2]["body"]["response_format"] == {"type": "json_object"}


# ---- limits ----
def test_busy_model_cools_down_and_the_next_one_answers():
    busy = Resp(429, {"error": {"message": "Rate limit reached for model on requests per minute (RPM)"}},
                {"retry-after": "30"})
    ai, clock = make("groq", busy, ok(HOLD))
    assert ai.ask("s", "p", DECISION_SCHEMA) == HOLD
    ai.ask("s", "p", DECISION_SCHEMA)
    assert [c["body"]["model"] for c in ai.session.calls] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b",
                                                              "openai/gpt-oss-20b"]   # still cooling down
    clock.t += 31
    ai.ask("s", "p", DECISION_SCHEMA)
    assert ai.session.calls[-1]["body"]["model"] == "openai/gpt-oss-120b"


def test_daily_quota_used_up_until_googles_midnight(tmp_path):
    day_gone = Resp(429, [{"error": {"code": 429, "message": "You exceeded your current quota. Please retry in 49.0s.",
                                     "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [
                                         {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}])
    j = Journal(str(tmp_path / "j.db"), 100)
    ai, clock = make("gemini", day_gone, store=j)
    assert ai.ask("s", "p", DECISION_SCHEMA) is None and len(ai.session.calls) == 2   # both models said "day over"
    problems = ai.take_problems()
    assert [p["kind"] for p in problems] == ["daily"] and "restarts in about" in problems[0]["text"]
    assert ai.ask("s", "p", DECISION_SCHEMA) is None and len(ai.session.calls) == 2   # no request wasted
    assert ai.requests_left() == 0
    again, _ = make("gemini", ok(HOLD), store=j, clock=clock)                          # a restart remembers
    assert again.ask("s", "p", DECISION_SCHEMA) is None and not again.session.calls
    clock.t += 20 * 3600                                                              # past midnight in California
    assert again.ask("s", "p", DECISION_SCHEMA) == HOLD and again.requests_today() == 1


def test_daily_allowance_counts_failed_requests_and_survives_a_restart(tmp_path):
    j = Journal(str(tmp_path / "j.db"), 100)
    ai, clock = make("gemini", Resp(500, {"error": {"message": "internal"}}), ok(HOLD), store=j,
                     daily_requests=1)
    assert ai.ask("s", "p", DECISION_SCHEMA) == HOLD                 # model 1 failed, model 2 answered
    assert ai.requests_today() == 2 and ai.requests_left() == 0      # 1 a day each: both used, failure included
    assert ai.ask("s", "p", DECISION_SCHEMA) is None and len(ai.session.calls) == 2
    again, _ = make("gemini", ok(HOLD), store=j, clock=clock, daily_requests=1)
    assert again.requests_today() == 2 and again.ask("s", "p", DECISION_SCHEMA) is None


def test_the_per_minute_limit_waits_briefly_or_moves_on():
    ai, clock = make("gemini", ok(HOLD))                             # 8 a minute per model
    for _ in range(8):
        ai.ask("s", "p", DECISION_SCHEMA)
    ai.ask("s", "p", DECISION_SCHEMA)                                # a full minute to wait: the second model
    assert ai.session.calls[-1]["body"]["model"] == "gemini-3.1-flash-lite" and not clock.slept
    clock.t += 45                                                    # 15 s left in the first model's minute
    ai.ask("s", "p", DECISION_SCHEMA)
    assert clock.slept == [15] and ai.session.calls[-1]["body"]["model"] == "gemini-3.5-flash-lite"


def test_a_question_too_big_for_the_minute_limit_is_not_sent():
    ai, _ = make("groq", ok(HOLD))
    assert ai.ask("s", "x" * 40_000, DECISION_SCHEMA) is None and not ai.session.calls   # ~10k tokens > 6.5k a minute
    ai2, _ = make("groq", Resp(413, {"error": {"message": "Request too large"}}), ok(HOLD))
    assert ai2.ask("s", "p", DECISION_SCHEMA) == HOLD and len(ai2.session.calls) == 2


# ---- setup problems ----
def test_a_rejected_key_switches_the_provider_off_and_never_leaks_the_key(caplog):
    echo = Resp(401, {"error": {"message": f"Invalid API Key {KEY}"}})
    ai, _ = make("groq", echo)
    with caplog.at_level(logging.INFO):
        assert ai.ask("s", "p", DECISION_SCHEMA) is None
        assert ai.ask("s", "p", DECISION_SCHEMA) is None
    assert len(ai.session.calls) == 1                               # second model not tried, nothing re-sent
    problems = ai.take_problems()
    assert problems[0]["kind"] == "key" and KEY not in problems[0]["text"] and KEY not in caplog.text
    gemini_bad, _ = make("gemini", Resp(400, [{"error": {"code": 400, "message": "API key not valid. Please pass a "
                                                                                   "valid API key.",
                                                          "status": "INVALID_ARGUMENT"}}]))
    assert gemini_bad.ask("s", "p", DECISION_SCHEMA) is None and gemini_bad.take_problems()[0]["kind"] == "key"


def test_a_retired_model_is_switched_off_and_reported():
    ai, _ = make("groq", Resp(404, {"error": {"message": "The model `openai/gpt-oss-120b` does not exist"}}), ok(HOLD))
    assert ai.ask("s", "p", DECISION_SCHEMA) == HOLD
    assert ai.take_problems()[0]["kind"] == "model" and "openai/gpt-oss-120b" in ai.disabled
    ai.ask("s", "p", DECISION_SCHEMA)
    assert [c["body"]["model"] for c in ai.session.calls][-1] == "openai/gpt-oss-20b"


def test_an_unreachable_service_is_quietly_skipped():
    import requests

    def down(body):
        raise requests.ConnectionError("connection refused")
    ai, _ = make("gemini", down)
    assert ai.ask("s", "p", DECISION_SCHEMA) is None and len(ai.session.calls) == 2


# ---- the router ----
class FakeAI:
    def __init__(self, answer, provider="gemini"):
        self.answer, self.provider, self.asked, self.label = answer, provider, [], "Fake free AI"

    def ask(self, system, prompt, schema, effort="medium", caller="other"):
        self.asked.append(caller)
        return dict(self.answer) if self.answer else None

    def research(self, *a, **k):
        self.asked.append("research")
        return {"text": "notes", "sources": []}


def test_router_order_and_the_tasks_that_stay_with_claude():
    free, claude = FakeAI(HOLD), FakeAI({"action": "BUY", "confidence": 0.9, "reasoning": "c"})
    r = AIRouter(claude, free, "free-first")
    assert r.ask("s", "p", DECISION_SCHEMA, caller="decision")["_ai"] == "ai:gemini" and claude.asked == []
    free.answer = None
    assert r.ask("s", "p", DECISION_SCHEMA, caller="decision")["_ai"] == "claude"     # free could not: Claude
    r.order = "claude-first"
    free.answer = HOLD
    assert r.ask("s", "p", DECISION_SCHEMA, caller="news")["_ai"] == "claude"
    free.asked.clear()
    assert r.ask("s", "p", DECISION_SCHEMA, caller="review")["_ai"] == "claude" and free.asked == []
    assert r.research("s", "p", caller="scout") and free.asked == []                   # web research: Claude only
    alone = AIRouter(None, free)
    assert alone.research("s", "p") is None and alone.ask("s", "p", DECISION_SCHEMA, caller="review") is None
    assert is_ai_source("ai:gemini-reused") and is_ai_source("claude") and not is_ai_source("rules")
    assert ai_name("ai:groq-reused") == "groq" and ai_name("claude-reused") == "claude"


def test_decisions_and_news_record_which_ai_answered():
    free = FakeAI({"action": "HOLD", "confidence": 0.8, "reasoning": "Wait."})
    agent = DecisionAgent(AIRouter(None, free))
    good = {"technical": {"score": 0.9, "price": 100.0, "atr": 1.0}, "prediction": {"score": 0.9}, "forex": {"score": 0},
            "news": {"score": 0.0}}
    d = agent.run("BTCUSDT", good, holding=False)
    assert d["source"] == "ai:gemini" and d["action"] == "HOLD" and d["rules_action"] == "BUY"
    assert agent.run("BTCUSDT", good, holding=False, use_ai=False)["source"] == "ai:gemini-reused"
    news_ai = FakeAI({"coins": [{"coin": "BTC", "score": 0.4, "summary": "ETF inflows"}], "market_mood": "calm",
                      "check_every_minutes": 5, "pace_reason": "CPI soon"})
    agent = NewsAgent(AIRouter(None, news_ai))
    agent.fetch_headlines = lambda per_feed=25: ["Bitcoin ETF inflows rise"]
    out = agent.run(["BTC"], {"BTC": {"change_1h_pct": 0.1, "change_24h_pct": 1, "volatility_pct": 1, "holding": False}})
    assert out["BTC"]["source"] == "ai:gemini" and out["_pace"]["by"] == "ai:gemini"
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], cycle_minutes=15), Journal(":memory:", 10_000))
    assert bot._next_pace(out["_pace"], {})["minutes"] == 15        # a free model may not speed the checks up
    assert bot._next_pace({"minutes": 45, "reason": "quiet", "by": "ai:gemini"}, {})["minutes"] == 45
    assert bot._next_pace({"minutes": 5, "reason": "CPI", "by": "claude"}, {})["minutes"] == 5


def test_a_free_ais_bad_news_still_blocks_a_buy_but_keywords_do_not():
    agent = DecisionAgent(None)
    good = {"technical": {"score": 0.9, "price": 100.0, "atr": 1.0}, "prediction": {"score": 0.9}, "forex": {"score": 0}}
    assert agent.run("X", dict(good, news={"score": -0.8, "source": "ai:groq"}), holding=False)["action"] == "HOLD"
    assert agent.run("X", dict(good, news={"score": -0.8, "source": "keywords"}), holding=False)["action"] == "BUY"


# ---- the head trader's pacing ----
class FakeFree:
    def __init__(self, left, mins=600, burst=6):
        self.left, self.mins, self.preset, self.provider, self.label = left, mins, SimpleNamespace(burst=burst), "gemini", "G"

    def requests_left(self):
        return self.left

    def minutes_to_reset(self):
        return self.mins

    def ask(self, *a, **k):
        return None


def cands():
    return [{"symbol": s, "holding": False, "score": 0.6 - i * 0.01, "threshold": 0.35, "eligible": True}
            for i, s in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"])]


def test_head_rations_a_free_ai_by_requests_not_dollars():
    s = Settings(cycle_minutes=15, news_refresh_minutes=30)
    j = Journal(":memory:", 10_000)
    noon = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)
    head = HeadTraderAgent(s, j, AIRouter(None, FakeFree(700)))
    assert len(head.plan_ai(cands(), noon)) == 6                    # plenty left: the per-check burst is the limit
    assert "requests left" in head.last_plan["why"]
    head = HeadTraderAgent(s, j, AIRouter(None, FakeFree(60)))      # 60 left, 20 kept for news: one every ~1 check
    assert len(head.plan_ai(cands(), noon)) == 1
    head = HeadTraderAgent(s, j, AIRouter(None, FakeFree(15)))      # less than the news agent needs
    assert head.plan_ai(cands(), noon) == set() and "no AI questions left" in head.last_plan["why"]


# ---- settings ----
def test_settings_for_a_free_ai(monkeypatch):
    monkeypatch.setenv("FREE_AI_PROVIDER", "Gemini")
    monkeypatch.setenv("FREE_AI_API_KEY", " k ")
    monkeypatch.setenv("FREE_AI_MODELS", "gemini-a, gemini-b")
    s = Settings.load()
    assert s.free_ai_provider == "gemini" and s.free_ai_key == "k" and s.free_ai_models == ("gemini-a", "gemini-b")
    assert s.use_free_ai and s.ai_order == "free-first" and not s.problems()
    assert not Settings.load(offline=True).use_free_ai
    assert any("needs FREE_AI_API_KEY" in p for p in Settings(free_ai_provider="groq").problems())
    assert any("not one of" in p for p in Settings(free_ai_provider="chatgpt").problems())
    assert any("account id" in p for p in Settings(free_ai_provider="cloudflare", free_ai_key="t").problems())
    assert Settings(free_ai_provider="cloudflare", free_ai_key="t",
                    free_ai_base_url="https://api.cloudflare.com/client/v4/accounts/abc/ai/v1").use_free_ai
    assert any("AI_ORDER" in p for p in Settings(ai_order="random").problems())
    for key in ("FREE_AI_PROVIDER", "FREE_AI_API_KEY", "FREE_AI_MODELS", "FREE_AI_BASE_URL", "FREE_AI_DAILY_REQUESTS",
                "AI_ORDER"):
        from trader.config import ENV_KEYS
        assert key in ENV_KEYS


def free_bot(monkeypatch, journal=None, **kw):
    monkeypatch.setattr(Settings, "use_free_ai", property(lambda self: True))
    s = Settings(offline=True, symbols=["BTCUSDT"], free_ai_provider="gemini", free_ai_key=KEY, **kw)
    return Orchestrator(s, journal or Journal(":memory:", 10_000))


def test_every_agent_shares_one_router_and_ai_command_switches_the_order(monkeypatch, tmp_path):
    from trader.__main__ import ai_command
    j = Journal(str(tmp_path / "j.db"), 10_000)
    bot = free_bot(monkeypatch, j)
    assert isinstance(bot.ai, AIRouter) and bot.decision.llm is bot.ai and bot.news.llm is bot.ai and bot.head.llm is bot.ai
    text = ai_command(bot, j)
    assert "Google Gemini (free tier)" in text and "requests today" in text and "Claude is not configured" in text
    assert "both a free ai and claude" in ai_command(bot, j, "claude-first").lower()
    bot.llm = FakeAI(HOLD, provider="claude")
    bot.llm.model, bot.llm.spent_today = "claude-opus-5-5", 0.0
    bot.llm.effective_daily_budget = lambda: 5.0
    bot.ai.claude = bot.llm
    assert ai_command(bot, j, "claude-first").startswith("✅") and bot.ai.order == "claude-first"
    assert "Usage" in ai_command(bot, j, "whatever")
    again = free_bot(monkeypatch, j)
    assert again.ai.order == "claude-first"                          # kept across a restart


def test_scorecard_judges_each_ai_on_its_own():
    s = Settings(starting_balance=10_000)
    j = Journal(":memory:", 10_000)
    rep = {"technical": {"price": 100.0, "atr": 2.0}, "news": {"score": 0.6, "source": "ai:gemini"}}
    for src in ("claude", "ai:gemini", "ai:gemini", "rules"):
        j.log_decision("BTCUSDT", {"action": "HOLD", "confidence": 0.6, "source": src, "reasoning": "r",
                                   "rules_action": "BUY"}, {"approved": False, "reason": "hold"}, rep)
    up = [{"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i} for i in range(130)]
    claude = claude_scorecard(j, s, lambda sym, ms: up)
    gemini = claude_scorecard(j, s, lambda sym, ms: up, source="ai:gemini", name="Gemini")
    assert claude["cases"] == 1 and gemini["cases"] == 2 and gemini["cost_usd"] > 0
    news = news_scorecard(j, lambda sym, ms: up)
    assert news["by_source"]["ai:gemini"]["judged"] == 1
    text = scorecard_text(claude, news, 1.5, [("Gemini", gemini)])
    assert "🆓 Gemini vs the free rules: 2 judged" in text and "bill $0" in text


def test_health_counts_the_free_ais_work(monkeypatch):
    from trader.health import free_ai_line, funnel, funnel_text, since_iso
    bot = free_bot(monkeypatch)
    line = free_ai_line(bot.free_ai)
    assert line.startswith("Free AI (Google Gemini (free tier)): requests today gemini-3.5-flash-lite 0/400")
    j = bot.journal
    j.log_decision("BTCUSDT", {"action": "HOLD", "confidence": 0.6, "source": "ai:gemini", "reasoning": "r",
                               "rules_action": "BUY"}, {"approved": False, "reason": "hold"}, {})
    f = funnel(j, since_iso(24))
    assert f["free_ai_vetoes"] == 1 and "1 vetoed by the free AI" in funnel_text(f)


def test_the_wizard_sets_up_a_free_ai(monkeypatch, tmp_path):
    from dotenv import dotenv_values
    from trader import setup_wizard as w
    asked = []
    monkeypatch.setattr(w, "check_free_ai", lambda p, k, m, b: (asked.append((p, k, m, b)) or True, ""))
    answers = iter(["y", "4", "acc-123", "cf-token"])                 # yes, Cloudflare, account id, token
    wiz = w.Wizard(env_path=tmp_path / ".env", ask=lambda q: next(answers, ""), ask_secret=lambda q: next(answers, ""),
                   say=lambda *a: None)
    wiz.v["ANTHROPIC_API_KEY"] = ""
    wiz.step_free_ai()
    assert wiz.v["FREE_AI_PROVIDER"] == "cloudflare" and wiz.v["FREE_AI_API_KEY"] == "cf-token"
    assert wiz.v["FREE_AI_BASE_URL"] == "https://api.cloudflare.com/client/v4/accounts/acc-123/ai/v1"
    answers = iter(["y", "1", "gem-key", "2"])                         # Gemini, key, then Claude first
    wiz = w.Wizard(env_path=tmp_path / ".env", ask=lambda q: next(answers, ""), ask_secret=lambda q: next(answers, ""),
                   say=lambda *a: None)
    wiz.v["ANTHROPIC_API_KEY"] = "sk-ant"
    wiz.step_free_ai()
    assert wiz.v["FREE_AI_PROVIDER"] == "gemini" and wiz.v["AI_ORDER"] == "claude-first"
    w.write_env(tmp_path / ".env", wiz.v)
    assert dotenv_values(tmp_path / ".env")["FREE_AI_PROVIDER"] == "gemini"
