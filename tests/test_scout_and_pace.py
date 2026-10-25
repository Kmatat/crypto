import datetime as dt
from types import SimpleNamespace

import pytest

from trader.agents.news import NewsAgent
from trader.agents.scout import MarketScoutAgent
from trader.config import Settings
from trader.journal import Journal
from trader.notify import pace_message, scout_message, trade_messages
from trader.orchestrator import Orchestrator


def tick(symbol, price, change_pct=0.0, vol_musd=100.0, rng=0.03):
    open_ = price / (1 + change_pct / 100)
    return {"symbol": symbol, "price": price, "open": open_, "high": price * (1 + rng), "low": price * (1 - rng),
            "quote_volume": vol_musd * 1e6}


TICKERS = [
    tick("BTCUSDT", 80_000, -1, 900), tick("ETHUSDT", 2_400, -1, 400), tick("AVAXUSDT", 25, +4, 50),
    tick("DOGEUSDT", 0.2, +2, 40), tick("WLDUSDT", 0.5, -35, 80),          # major but moved 35%: not pickable
    tick("USDCUSDT", 1.0, 0, 1000), tick("NEWUSDUSDT", 1.001, 0, 60, rng=0.001),  # stablecoins
    tick("PAXGUSDT", 4000, 0, 30), tick("TINYUSDT", 3, 5, 2),              # gold token, too little volume
    tick("ETHBTC", 0.03, 0, 100), tick("SNDKBUSDT", 90, 1, 30), tick("PUMPXUSDT", 0.1, 45, 30),
]


class FakeMarket:
    def __init__(self, tickers=TICKERS, demo=None):
        self.tickers, self.demo = tickers, demo

    def tickers_24h(self):
        return self.tickers

    def all_prices(self):
        return self.demo


def scout(llm=None, testnet=None, **kw):
    return MarketScoutAgent(FakeMarket(), testnet, llm, Settings(**kw))


def test_candidates_drop_stablecoins_gold_thin_and_non_usdt_pairs():
    syms = [c["symbol"] for c in scout().candidates()]
    assert syms[:2] == ["BTCUSDT", "ETHUSDT"]  # sorted by volume
    assert set(syms) == {"BTCUSDT", "ETHUSDT", "AVAXUSDT", "DOGEUSDT", "WLDUSDT", "PUMPXUSDT"}  # SNDKB = stock token


def test_testnet_mode_keeps_only_coins_listed_at_a_sane_demo_price():
    demo = {"BTCUSDT": 80_100, "ETHUSDT": 2_401, "AVAXUSDT": 30}  # AVAX demo price 20% off; others not listed
    syms = [c["symbol"] for c in scout(testnet=FakeMarket(demo=demo)).candidates()]
    assert syms == ["BTCUSDT", "ETHUSDT"]


def test_rule_picks_are_liquid_major_coins_only():
    out = scout(max_coins=5).run(core=["BTCUSDT", "ETHUSDT"], held=["SOLUSDT"])
    assert out["source"] == "rules"
    # 3 free slots: AVAX and DOGE qualify; WLD moved too much; PUMPX is not a major coin; SNDKB is excluded
    assert out["symbols"] == ["BTCUSDT", "ETHUSDT", "AVAXUSDT", "DOGEUSDT", "SOLUSDT"]
    assert "we hold it" in out["reasons"]["SOLUSDT"] and "core" in out["reasons"]["BTCUSDT"]


class FakeLLM:
    def __init__(self, picks, research=True):
        self.picks, self.with_research, self.asked = picks, research, []

    def research(self, system, prompt, max_searches=5, **kw):
        return {"text": "AVAX upgrade went live today.", "sources": ["https://news.example/avax"]} \
            if self.with_research else None

    def ask(self, system, prompt, schema, effort="medium", **kw):
        self.asked.append(prompt)
        return {"picks": self.picks, "avoid": [{"symbol": "pumpx", "reason": "pump and dump"}], "summary": "Calm."}


def test_ai_picks_are_validated():
    llm = FakeLLM([
        {"symbol": "SNDKB", "asset_type": "tokenized_stock", "reason": "stock token"},
        {"symbol": "PUMPXUSDT", "asset_type": "crypto", "reason": "moon"},          # moved 45%: not eligible
        {"symbol": "avax", "asset_type": "crypto", "reason": "Upgrade went live."},
        {"symbol": "MADEUPUSDT", "asset_type": "crypto", "reason": "not a candidate"},
        {"symbol": "BTCUSDT", "asset_type": "crypto", "reason": "already core"},
    ])
    out = scout(llm=llm, max_coins=4).run(core=["BTCUSDT", "ETHUSDT"], held=[])
    assert out["source"] == "claude" and out["symbols"] == ["BTCUSDT", "ETHUSDT", "AVAXUSDT"]
    assert out["reasons"]["AVAXUSDT"] == "Upgrade went live." and out["avoid"] == {"PUMPXUSDT": "pump and dump"}
    assert out["sources"] == ["https://news.example/avax"]
    assert "AVAX upgrade went live today." in llm.asked[0]  # research notes reach the selection step


def test_ai_picks_that_are_all_invalid_fall_back_to_rules_but_none_is_respected():
    bad = FakeLLM([{"symbol": "MADEUP", "asset_type": "crypto", "reason": "x"}])
    assert scout(llm=bad, max_coins=4).run(core=["BTCUSDT"], held=[])["source"] == "rules"
    calm = FakeLLM([], research=False)
    out = scout(llm=calm, max_coins=4).run(core=["BTCUSDT"], held=[])
    assert out["source"] == "claude" and out["symbols"] == ["BTCUSDT"]
    assert "unavailable" in calm.asked[0]  # still decides from market data when web research fails


# ---------------------------------------------------------------- orchestrator: scout schedule, pace, slots
@pytest.fixture
def bot():
    s = Settings(offline=True, symbols=["BTCUSDT", "ETHUSDT"])
    return Orchestrator(s, Journal(":memory:", s.starting_balance))


class FakeScout:
    def __init__(self):
        self.runs, self.fail = 0, False

    def run(self, core, held):
        self.runs += 1
        if self.fail:
            raise RuntimeError("binance down")
        return {"symbols": core + ["AVAXUSDT"], "reasons": {"AVAXUSDT": "upgrade"}, "source": "claude",
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}


def test_scout_runs_on_schedule_and_when_settings_change(bot):
    bot.scout = FakeScout()
    bot.maybe_scout()
    assert bot.scout.runs == 1 and bot.scout_update["added"] == ["AVAXUSDT"]
    assert bot.watchlist()["symbols"] == ["BTCUSDT", "ETHUSDT", "AVAXUSDT"]
    bot.maybe_scout()
    assert bot.scout.runs == 1 and bot.scout_update is None  # fresh: not re-run
    bot.s.max_coins = 5
    bot.maybe_scout()
    assert bot.scout.runs == 2  # settings changed: re-run now


def test_failed_scout_waits_before_retrying(bot):
    bot.scout = FakeScout()
    bot.scout.fail = True
    with pytest.raises(RuntimeError):
        bot.maybe_scout()
    bot.maybe_scout()
    assert bot.scout.runs == 1


def test_pace_follows_ai_within_limits_and_speeds_up_near_exits(bot):
    assert bot._next_pace(None, {}) == {"minutes": 15, "reason": "normal pace", "source": "settings"}
    assert bot._next_pace({"minutes": 2, "reason": "Fed now"}, {})["minutes"] == 5
    assert bot._next_pace({"minutes": 240, "reason": "calm"}, {}) == {"minutes": 60, "reason": "calm", "source": "AI"}
    bot.journal.open_position("BTCUSDT", {"qty": 1, "avg_price": 100, "quote": 100}, stop=96, take=110)
    ctx = {"BTCUSDT": {"price": 97, "technical": {"atr": 4}}}  # 97 is only 0.25 ATR above the 96 stop-loss
    pace = bot._next_pace({"minutes": 30, "reason": "calm"}, ctx)
    assert pace["minutes"] == 5 and pace["source"] == "risk" and "BTCUSDT" in pace["reason"]


def test_limited_slots_go_to_the_strongest_buy_signals(bot):
    bot.s.max_open_positions = 1
    conf = {"BTCUSDT": 0.7, "ETHUSDT": 0.9}
    bot.decision.run = lambda sym, reports, holding, use_ai=True, threshold=None: {"action": "BUY", "confidence": conf[sym], "reasoning": "r",
                                                      "source": "rules", "combined_score": 0.5}
    out = {r["symbol"]: r for r in bot.run_cycle()}
    assert out["ETHUSDT"]["order"]["ok"] and "order" not in out["BTCUSDT"]
    assert "open positions" in out["BTCUSDT"]["risk"]
    assert list(bot.journal.positions()) == ["ETHUSDT"]


def test_offline_cycle_keeps_normal_pace(bot):
    bot.run_cycle()
    assert bot.pace["minutes"] == 15


# ---------------------------------------------------------------- settings, news pace, messages
def test_old_60_minute_setting_becomes_15_but_explicit_new_settings_are_kept(monkeypatch):
    monkeypatch.setenv("CYCLE_MINUTES", "60")
    monkeypatch.delenv("SETTINGS_VERSION", raising=False)
    assert Settings.load().cycle_minutes == 15
    monkeypatch.setenv("SETTINGS_VERSION", "2")
    assert Settings.load().cycle_minutes == 60


def test_news_agent_returns_ai_pace_and_refreshes_for_new_coins(monkeypatch):
    calls = []

    def ask(system, prompt, schema, effort="low", **kw):
        calls.append(prompt)
        return {"coins": [{"coin": "BTC", "score": 0.4, "summary": "ETF inflows"}], "market_mood": "upbeat",
                "check_every_minutes": 5, "pace_reason": "CPI data in 20 minutes"}
    agent = NewsAgent(SimpleNamespace(ask=ask))
    monkeypatch.setattr(agent, "fetch_headlines", lambda: ["Bitcoin ETF sees record inflows"])
    snap = {"BTC": {"change_1h_pct": 0.5, "change_24h_pct": 2.0, "volatility_pct": 0.8, "holding": True}}
    out = agent.run(["BTC"], snap)
    assert out["_pace"] == {"minutes": 5, "reason": "CPI data in 20 minutes"} and out["BTC"]["score"] == 0.4
    assert "BTC: 1h +0.50%" in calls[0] and "we hold it" in calls[0]
    agent.run(["BTC"], snap)
    assert len(calls) == 1  # cached
    agent.run(["BTC", "AVAX"], snap)
    assert len(calls) == 2  # a new coin needs fresh news


def test_watchlist_and_pace_messages():
    msg = scout_message({"symbols": ["BTCUSDT", "AVAXUSDT"], "reasons": {"AVAXUSDT": "Upgrade live."},
                         "added": ["AVAXUSDT"], "removed": ["SOLUSDT"], "source": "claude", "summary": "Calm day.",
                         "avoid": {"PUMPXUSDT": "pump"}})
    assert "AI scout" in msg and "➕ AVAX: Upgrade live." in msg and "➖ SOL" in msg and "Now watching: BTC, AVAX" in msg
    assert pace_message({"minutes": 5, "reason": "Fed decision", "source": "AI"}) == \
        "⏱️ Now checking every 5 min (set by the AI): Fed decision"
    assert trade_messages([{"symbol": "SCOUT", "error": "binance down"}])[0][1].startswith("⚠️ Market scout:")
