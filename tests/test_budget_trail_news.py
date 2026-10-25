"""1.5.0: Claude's budget on small demo balances (/aibudget), the trailing stop on finished candles, keyword news."""
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.__main__ import ai_budget
from trader.agents.decision import DecisionAgent
from trader.agents.news import NewsAgent, hits, POSITIVE, NEGATIVE
from trader.config import AI_BUDGET_CEILING_USD, Settings
from trader.journal import Journal
from trader.llm import ClaudeJSON
from trader.orchestrator import Orchestrator

HOUR = 3_600_000
GLAMSTERDAM = ["Ethereum Delays Glamsterdam Testnet Fork Over Gas Security Flaws",
               "Ethereum delays Glamsterdam Hoodi testnet launch over security risks",
               "Ethereum delays Glamsterdam test after security devs flag risk to existing contracts"]


# ---- Claude's budget ----
def test_demo_money_has_no_percentage_cap_unless_the_owner_sets_one(monkeypatch):
    assert Settings(starting_balance=100).ai_pct_month() == 0
    assert Settings(starting_balance=100, ai_budget_pct_month=0.25).ai_pct_month() == 0.25
    monkeypatch.setenv("AI_BUDGET_PCT_MONTH", "0.5")
    assert Settings.load().ai_budget_pct_month == 0.5
    monkeypatch.setenv("AI_BUDGET_PCT_MONTH", "")
    assert Settings.load().ai_budget_pct_month is None


def make_bot(journal, **kw):
    s = Settings(offline=True, symbols=["BTCUSDT"], anthropic_key="k", starting_balance=100, **kw)
    bot = Orchestrator(s, journal)
    bot.llm = ClaudeJSON("claude-opus-5-5", "k", s.ai_daily_budget_usd, store=journal)  # offline normally has none
    if s.ai_pct_month():
        bot.llm.pct_daily_budget = s.starting_balance * s.ai_pct_month() / 100 / 30
    return bot


def test_a_100_dollar_demo_account_gets_the_flat_daily_budget():
    bot = make_bot(Journal(":memory:", 100))
    assert bot.llm.effective_daily_budget() == 5.0          # not $0.01 as in 1.4.x
    capped = make_bot(Journal(":memory:", 100), ai_budget_pct_month=0.25)
    assert capped.llm.effective_daily_budget() == pytest.approx(0.0083, rel=0.01)   # only if the owner asks for it


def test_aibudget_shows_sets_keeps_and_resets_the_cap(tmp_path, monkeypatch):
    j = Journal(str(tmp_path / "j.db"), 100)
    bot = make_bot(j, ai_budget_pct_month=0.25)
    bot.llm._roll_day()
    bot.llm._spent, bot.llm.budget_hit = 0.02, True
    bot.llm.by_caller = {"scout": {"calls": 2, "usd": 0.015}, "news": {"calls": 3, "usd": 0.005}}
    text = ai_budget(bot, j)
    assert "$0.01 a day" in text and "Coin scout: 2 calls, $0.01" in text and "used up" in text

    out = ai_budget(bot, j, "3")
    assert out.startswith("✅") and "$3.00 a day" in out and "$90 a month" in out
    assert bot.llm.effective_daily_budget() == 3.0 and not bot.llm.budget_hit   # applies at once: Claude is back
    assert j.get("ai_budget_override")["usd_per_day"] == 3.0
    assert any("set to $3.00" in e["text"] for e in j.events())

    # a restart keeps the owner's choice, whatever the settings file says (the client makes no call at start)
    monkeypatch.setattr(Settings, "use_claude", property(lambda self: True))
    again = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], anthropic_key="k", starting_balance=100,
                                  ai_budget_pct_month=0.25), j)
    assert again.llm.effective_daily_budget() == 3.0

    assert "back to the settings" in ai_budget(bot, j, "default")
    assert bot.llm.effective_daily_budget() == pytest.approx(0.0083, rel=0.01) and j.get("ai_budget_override") is None


def test_aibudget_refuses_nonsense_and_amounts_above_the_ceiling():
    j = Journal(":memory:", 100)
    bot = make_bot(j)
    assert "at most $20" in ai_budget(bot, j, str(AI_BUDGET_CEILING_USD + 1))
    assert "at most $20" in ai_budget(bot, j, "0")
    assert "Usage" in ai_budget(bot, j, "lots")
    assert j.get("ai_budget_override") is None and bot.llm.effective_daily_budget() == 5.0
    assert "$4.50 a day" in ai_budget(bot, j, "$4.5")
    assert "no API key" in ai_budget(SimpleNamespace(llm=None), j)


def test_a_stored_budget_above_the_ceiling_is_clamped_at_start(monkeypatch):
    j = Journal(":memory:", 100)
    j.set("ai_budget_override", {"usd_per_day": 500, "set_at": "2026-10-25T00:00:00+00:00", "by": "owner"})
    monkeypatch.setattr(Settings, "use_claude", property(lambda self: True))
    bot = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"], anthropic_key="k"), j)
    assert bot.llm.effective_daily_budget() == AI_BUDGET_CEILING_USD


# ---- trailing stop ----
def test_the_trailing_stop_ignores_an_unfinished_candle_spike():
    s = Settings(offline=True, symbols=["BTCUSDT"], trail_atr=2.0)
    j = Journal(":memory:", s.starting_balance)
    bot = Orchestrator(s, j)
    opened = dt.datetime(2026, 10, 25, 10, 20, tzinfo=dt.timezone.utc)
    t0 = int(dt.datetime(2026, 10, 25, 8, tzinfo=dt.timezone.utc).timestamp() * 1000)
    closed = [{"time": t0, "close": 120.0},              # before the position: ignored
              {"time": t0 + HOUR, "close": 101.0},       # 09:00-10:00, also before
              {"time": t0 + 2 * HOUR, "close": 103.0},   # 10:00-11:00: the hour the buy happened in
              {"time": t0 + 3 * HOUR, "close": 102.0}]
    position = {"entry_price": 100.0, "opened_at": opened.isoformat()}
    assert bot._trail_price(position, closed) == 103.0
    assert bot._trail_price(position, closed[:2]) == 100.0       # no finished candle yet: the entry price
    assert bot._trail_price({"entry_price": 100.0, "opened_at": None}, closed) == 120.0  # unknown age: all candles


def test_run_cycle_trails_on_finished_closes_not_the_live_price():
    s = Settings(offline=True, symbols=["BTCUSDT"], trail_atr=2.0)
    j = Journal(":memory:", s.starting_balance)
    bot = Orchestrator(s, j)
    candles = bot.market.candles("BTCUSDT")   # the offline price history
    last_close = candles[-2]["close"]
    spike = dict(candles[-1], close=last_close * 1.20, high=last_close * 1.20)   # a forming candle shooting up 20%
    series = candles[:-1] + [spike]
    bot.market.candles = lambda sym, limit=None: series
    bot.market.closed = lambda c: c[:-1]  # as on the real exchange: the last row is still forming
    opened = dt.datetime.fromtimestamp(series[-20]["time"] / 1000, dt.timezone.utc).isoformat()
    j.open_position("BTCUSDT", {"qty": 0.001, "avg_price": last_close * 0.9, "quote": last_close * 0.0009},
                    last_close * 0.8, last_close * 2)
    j.db.execute("UPDATE positions SET opened_at=? WHERE symbol='BTCUSDT'", (opened,))
    j.db.commit()
    seen = []
    real = bot._trail
    bot._trail = lambda sym, p, price, atr: seen.append(price) or real(sym, p, price, atr)
    bot.run_cycle()
    assert seen and seen[0] < spike["close"]
    assert seen[0] == max(c["close"] for c in series[-20:-1])


# ---- keyword news ----
def test_one_story_carried_by_three_feeds_counts_once():
    assert NewsAgent.stories(GLAMSTERDAM) == GLAMSTERDAM[:1]
    before = -1.0                                            # 1.4.x: three "delay" hits / 3 = the strongest bad news
    eth = NewsAgent.keyword_sentiment(GLAMSTERDAM, ["ETH"])["ETH"]
    assert eth["score"] == pytest.approx(-1 / 3, abs=0.001) and eth["score"] > before
    assert eth["source"] == "keywords" and "1 related stories" in eth["summary"]
    different = ["Ethereum ETF inflows hit a record", "Ethereum developers delay the next upgrade"]
    assert NewsAgent.stories(different) == different          # sharing the coin's name does not make one story


def test_keywords_match_whole_words():
    assert hits("Bitcoin rises again", POSITIVE) == 0         # 'gain' inside 'again' no longer counts
    assert hits("Bitcoin gains 5%", POSITIVE) == 1
    assert hits("Bank of Japan holds rates", NEGATIVE) == 0   # 'ban' inside 'bank'
    assert hits("Country bans bitcoin mining", NEGATIVE) == 1


def test_a_keyword_score_never_blocks_a_buy_but_claude_news_still_can():
    agent = DecisionAgent(None)
    good = {"technical": {"score": 0.9, "price": 100.0, "atr": 1.0}, "prediction": {"score": 0.9},
            "forex": {"score": 0.0}}
    words = agent.run("ETHUSDT", dict(good, news={"score": -0.9, "source": "keywords"}), holding=False, use_ai=False)
    claude = agent.run("ETHUSDT", dict(good, news={"score": -0.9, "source": "claude"}), holding=False, use_ai=False)
    assert words["action"] == "BUY"
    assert claude["action"] == "HOLD" and "news" in claude["reasoning"].lower()
