"""1.7.1: the daily trend rule on the owner's phone and terminal: commands, start messages, alerts, the daily summary."""
import datetime as dt
import signal
import time
from types import SimpleNamespace

from trader import __main__ as main, trend
from trader.config import Settings
from trader.journal import Journal
from trader.llm import ClaudeJSON
from trader.notify import TREND_REENTRY, Telegram, trade_messages, trend_message
from trader.orchestrator import Orchestrator
from trader.updater import version_string
from tests.test_trend import candles, trend_desk, wavy


def yesterday() -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()


def series():
    """A strong coin, a weaker one (both above their averages), a falling one, and a recent listing."""
    return {"AAAUSDT": candles(wavy(130, 100, 0.02)), "BBBUSDT": candles(wavy(130, 100, 0.01)),
            "CCCUSDT": candles(list(reversed(wavy(130)))), "DDDUSDT": candles(wavy(40))}


def phone():
    sent = []
    return sent, SimpleNamespace(enabled=True, send=lambda text, silent=False: sent.append((text, silent)) or True,
                                 send_once=lambda key, text, every_hours=6: sent.append((text, key)) or True)


# ---- the command list and the start messages ----
def test_the_help_lists_only_what_works_with_the_trend_rule():
    assert main.help_text(Settings(offline=True)) is main.HELP_TEXT
    text = main.help_text(Settings(offline=True, strategy="trend"))
    for cmd in ("/status", "/report", "/watchlist", "/strategies", "/trendtest", "/health", "/agents", "/diagnose",
                "/notes", "/review", "/aibudget", "/ai ", "/version", "/update", "/help"):
        assert cmd in text
    for cmd in ("/approve", "/reject", "/lab", "/playbook"):
        assert cmd not in text
    assert "26 weeks, 30 trades, profit factor 1.5 or more, worst drop at most 30%" in text
    assert "summary of the daily trend decisions after 00:00 UTC" in text and "needs Claude" in text


def test_the_start_messages_name_the_trend_rule_and_keep_the_hourly_wording(monkeypatch):
    s = Settings(offline=True, symbols=["BTCUSDT"])
    assert main.start_summary(Orchestrator(s, Journal(":memory:", 10_000)), s) == {
        "mode": "offline test (made-up prices)", "strategy": None, "ai": "built-in rules", "pace": "every 15 min",
        "coins": "BTCUSDT"}
    t = Settings(offline=True, strategy="trend", symbols=["BTCUSDT"], max_open_positions=1, trend_stop_pct=0.2)
    bot = Orchestrator(t, Journal(":memory:", 10_000))
    info = main.start_summary(bot, t)
    assert info["strategy"] == "daily trend rule (50-day average, 20% emergency stop)"
    assert info["ai"] == "none (the rule decides alone)"
    assert info["pace"] == ("every 15 min (prices and exchange protection; the trend decisions once a day after "
                            "00:00 UTC)")
    monkeypatch.setattr(Settings, "use_claude", property(lambda self: True))
    assert main.start_summary(bot, t)["ai"] == "none for trading (Claude only for /review)"


# ---- the alerts and the console ----
ADOPT = {"symbol": "ETHUSDT", "action": "ADOPT", "old_stop": 2910.0, "stop_loss": 2250.0, "take_profit": 5700.0,
         "entry_price": 3000.0, "sma_days": 50}
RAISE = {"symbol": "BTCUSDT", "action": "RAISE", "price": 150.0, "entry_price": 100.0, "take_profit": 285.0,
         "stop_loss": 75.0}


def test_takeovers_raises_and_stops_are_explained_on_the_phone():
    msgs = [m for _, m in trade_messages([ADOPT, RAISE])]
    assert msgs[0] == ("🔁 ETH: the position bought by the hourly strategy now follows the daily trend rule: emergency "
                       "stop moved from $2,910.00 to $2,250.00 (25% below the entry), held while it closes above its "
                       "50-day average")
    assert msgs[1] == ("⬆️ BTC is +50% since the buy: its take-profit order moved up to $285.00 so the trend is not cut "
                       "short (emergency stop unchanged at $75.00)")
    cheap = trade_messages([dict(ADOPT, old_stop=0.1455, stop_loss=0.1125, entry_price=0.15)])[0][1]
    assert "from $0.1455 to $0.1125" in cheap                       # a cheap coin's prices keep their digits
    # a coin taken over in the same check: its earlier raise (still with the hourly stop) is not announced
    assert len(trade_messages([dict(RAISE, symbol="ETHUSDT"), ADOPT])) == 1
    gave_up = trade_messages([{"symbol": "TREND", "stage": "trend", "error": "the decisions for 2026-10-26 stayed "
                                                                             "incomplete after 4 tries"}])
    assert gave_up[0][0] == "error:TREND" and gave_up[0][1].startswith("⚠️ Daily trend decisions: the decisions for")
    stop = {"symbol": "SOLUSDT", "action": "SELL", "reason": "stop-loss (exchange)", "trade": {"pnl": -25.0, "pnl_pct": -25.0},
            "order": {"ok": True, "qty": 1.0, "avg_price": 75.0, "quote": 75.0}}
    assert trade_messages([stop], trend=True)[0][1].endswith("\n" + TREND_REENTRY)
    assert TREND_REENTRY not in trade_messages([stop])[0][1]           # the hourly strategy has no such rule
    rule_sale = dict(stop, reason="trend: closed at or below its 50-day average")
    assert TREND_REENTRY not in trade_messages([rule_sale], trend=True)[0][1]


def test_no_result_row_can_crash_the_alerts_or_the_console(capsys):
    filled = {"ok": True, "qty": 1.0, "avg_price": 1.0, "quote": 1.0}
    odd = [{"symbol": "BTCUSDT", "action": "SELL", "reason": None, "order": filled},
           {"symbol": "ETHUSDT", "action": "BUY", "order": {"ok": False, "error": None}},
           {"symbol": "SOLUSDT", "action": "BUY", "order": filled},        # no stop, target or reasoning
           {"symbol": "XRPUSDT", "action": "RAISE"}, {"action": "HOLD"}, {"symbol": None, "action": None},
           "not a result", {"symbol": "ADAUSDT", "action": "HOLD", "risk": None}]
    texts = [m for _, m in trade_messages(odd, trend=True)]
    assert texts[0].startswith("🔴 SELL BTCUSDT") and "Reason: \n" in texts[0]
    assert "❌ BUY ETHUSDT FAILED: no reason given" in texts
    assert "ℹ️ BUY SOLUSDT: see /status for the details (this alert could not be written)" in texts
    assert len(texts) == 3
    decided = {"symbol": "AAAUSDT", "price": 120.0, "action": "BUY", "confidence": 1.0, "score": 0.05, "approved": True,
               "risk": "within limits", "reasoning": "buys 10.0% of the account", "source": "trend"}
    main.print_cycle(1, odd + [ADOPT, RAISE, decided])
    out = capsys.readouterr().out
    assert "AAAUSDT   BUY   above avg +5.0% price=120.00  risk: within limits" in out and "conf=" not in out
    assert "ETHUSDT   ADOPT  now follows the daily trend rule: stop 2,910 -> 2,250, take-profit order 5,700" in out
    assert "BTCUSDT   RAISE  take-profit order -> 285 (price 150, +50% since the buy; stop 75)" in out
    assert "not a result" in out


# ---- the daily summary ----
def test_the_daily_summary_is_short_and_says_everything():
    c = lambda action, risk, gap, approved=False, why="": {"action": action, "why": why, "approved": approved,
                                                         "risk": risk, "gap": gap}
    rep = {"coins": {"AAAUSDT": c("BUY", "within limits", 0.12, True, "buys 10.0% of the account"),
                     "BBBUSDT": c("HOLD", "holding", 0.031),
                     "CCCUSDT": c("SELL", "closing position", -0.02, True, "closed at or below its 50-day average"),
                     "DDDUSDT": c("HOLD", "no buy signal", -0.104),
                     "EEEUSDT": c("HOLD", "re-entry rule", 0.05),
                     "FFFUSDT": c("BUY", "already 3 open positions (max 3)", 0.2),
                     "GGGUSDT": c("HOLD", "demo exchange price", 0.1),
                     "NEWUSDT": c("HOLD", "no buy signal", None)},
           "adopted": ["BBBUSDT"], "slots": 3, "day": "2026-10-26", "complete": False, "tries": 4, "final": True}
    s = Settings(offline=True, strategy="trend", symbols=[*rep["coins"], "HHHUSDT"], max_open_positions=3)
    text = trend_message(rep, s)
    assert text.splitlines()[0] == "📈 Daily trend decisions (closes of 2026-10-26)"
    assert "🟢 Bought: AAA" in text and "🔴 Sold: CCC (closed at or below its 50-day average)" in text
    assert "Holding, close vs its 50-day average: AAA +12.0%, BBB +3.1%" in text
    assert "Out, closed at or below its average: DDD -10.4%" in text and "Not enough daily history yet: NEW" in text
    assert "Waiting after an emergency stop: EEE" in text
    assert ("Not bought: FFF (already 3 open positions (max 3)); GGG (its price on the Binance demo exchange is far "
            "from the real market)") in text
    assert "🔁 Now following the rule: BBB" in text and "No decision (its prices could not be read): HHH" in text
    assert "2 of 3 slots used" in text and "Not complete after 4 tries" in text
    assert len(text) < 1200


def test_the_daily_summary_is_sent_silently_once_a_day(monkeypatch):
    tg = Telegram("t", "1")
    calls = []
    monkeypatch.setattr(tg, "call", lambda method, **p: calls.append(p) or {})
    assert tg.send("loud") and tg.send("quiet", silent=True)
    assert "disable_notification" not in calls[0] and calls[1]["disable_notification"] is True

    bot, j = trend_desk(series(), positions=3)
    sent, fake = phone()
    bot.run_cycle()
    main.maybe_trend_summary(bot, fake)
    main.maybe_trend_summary(bot, fake)                    # the same report seen again
    assert len(sent) == 1 and sent[0][1] is True
    text = sent[0][0]
    assert text.startswith(f"📈 Daily trend decisions (closes of {yesterday()})")
    assert "🟢 Bought: AAA, BBB" in text and "2 of 3 slots used" in text and j.get("trend_summary_sent") == yesterday()
    j.set("trend_day", None)                               # the step runs again the same day (a switch back and forth)
    bot.run_cycle()
    main.maybe_trend_summary(bot, fake)
    assert len(sent) == 1

    retried, j2 = trend_desk({"AAAUSDT": candles(wavy(130, 100, 0.02))}, symbols=["AAAUSDT", "ZZZUSDT"])
    retried.market.prices["ZZZUSDT"] = 1.0
    sent2, fake2 = phone()
    for _ in range(3):                                     # ZZZ's prices cannot be read: retried, nothing sent yet
        retried.run_cycle()
        main.maybe_trend_summary(retried, fake2)
    assert sent2 == []
    j2.set("trend_tries", {**j2.get("trend_tries"), "first": j2.get("trend_tries")["first"] - 3700})
    retried.run_cycle()                                    # the fourth try, an hour after the first, closes the day
    main.maybe_trend_summary(retried, fake2)
    assert len(sent2) == 1 and "No decision (its prices could not be read): ZZZ" in sent2[0][0]
    assert "Not complete after 4 tries" in sent2[0][0]


def test_the_daily_report_shows_where_the_coins_stood():
    bot, j = trend_desk(series(), positions=3, daily_report_hour_utc=0)
    bot.run_cycle()
    sent, fake = phone()
    main.maybe_daily_report(bot, fake, bot.base)
    text = sent[0][0]
    assert "🎯 Demo goals (daily trend rule" in text and f"📈 Daily trend rule, closes of {yesterday()}" in text
    assert "Holding, close vs its 50-day average: AAA +" in text and "Out, closed at or below its average: CCC -" in text
    assert "Not enough daily history yet: DDD" in text and "2 of 3 slots used" in text
    assert "Bought:" not in text                                       # the order messages said that already
    assert j.get("last_daily_report") == dt.datetime.now(dt.timezone.utc).date().isoformat()
    bot.market.client = SimpleNamespace(klines=lambda *a, **k: 1 / 0)
    assert main.scorecard_block(bot, j) == ""              # no hourly scorecard with the trend rule


# ---- the Telegram commands' texts ----
def test_strategies_watchlist_and_status_in_trend_mode():
    bot, j = trend_desk(series(), positions=3, trend_sma_days=100)
    assert main.trend_watchlist_text(bot.base, j).endswith("• DDD: no daily decision yet")
    bot.run_cycle()
    text = main.trend_strategy_text(bot, j)
    assert text.startswith("📈 Strategy in force: the daily trend rule (STRATEGY=trend)")
    assert "Different from the tested settings: average 100 days (tested 50)" in text and "/trendtest" in text
    assert "4 coins" not in text and "3 position slots (fewer than the coins: only the strongest 3 are held)" in text
    assert f"last decided day: {yesterday()}" in text and "• AAA BUY on" in text and "• CCC HOLD on" in text
    assert text.endswith("The hourly champion and challengers are kept for STRATEGY=hourly.")
    assert j.get("strategy_champion") is None              # no hourly card recorded
    wl = main.trend_watchlist_text(bot.base, j)
    assert wl.startswith("🔭 Your coins (SYMBOLS; the scout is off in trend mode)")
    assert "• CCC: stays out · CCC closed at" in wl
    bot.llm = SimpleNamespace(spent_today=0.0, effective_daily_budget=lambda: 5.0)
    extra = main.status_extra(bot, bot.base, j)
    assert any(x.startswith(f"📈 Daily trend decisions: last for {yesterday()}") for x in extra)
    assert "Position slots: 2 of 3 in use" in extra and not any("Claude" in x for x in extra)
    bot.llm.spent_today = 0.12                             # a /review today: its cost is shown
    assert "Claude spend today: about $0.12 of $5.00 daily cap" in main.status_extra(bot, bot.base, j)
    tested = trend_desk(series(), positions=4)[0]
    assert "Settings: as tested." in main.trend_strategy_text(tested, tested.journal)
    hourly = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    hourly.llm = SimpleNamespace(spent_today=0.0, effective_daily_budget=lambda: 5.0)
    assert main.status_extra(hourly, hourly.base)[1:] == ["Claude spend today: about $0.00 of $5.00 daily cap"]


def test_ai_commands_say_only_review_uses_claude_in_trend_mode():
    bot, j = trend_desk(series())
    assert "only /review would use Claude" in main.ai_budget(bot, j)
    assert "Claude is not configured, so /review is not available" in main.ai_command(bot, j)
    bot.llm = ClaudeJSON("claude-haiku-5-5", "k", 5.0, store=j)
    assert "The daily trend rule asks no AI about trades: only /review uses Claude." in main.ai_budget(bot, j)
    reply = main.ai_budget(bot, j, "3")
    assert reply.startswith("✅ Claude may now spend up to $3.00 a day") and reply.endswith("only /review uses Claude.")
    assert j.get("ai_budget_override")["usd_per_day"] == 3.0 and bot.llm.daily_budget == 3.0   # setting it still works
    bot.llm.budget_hit = True
    assert "(used up until 00:00 UTC: /review waits until then)" in main.ai_budget(bot, j)
    text = main.ai_command(bot, j)
    assert "asks no AI about trades" in text and "Claude (claude-haiku-5-5)" in text
    assert "applies to the hourly strategy (STRATEGY=hourly): nothing changed" in main.ai_command(bot, j, "claude-first")
    assert j.get("ai_order_override") is None and main.ai_command(bot, j, "nonsense").startswith("Usage")


# ---- the loop's helpers ----
class Clock(dt.datetime):
    hour_now = 1

    @classmethod
    def now(cls, tz=None):
        return dt.datetime(2026, 10, 27, cls.hour_now, 30, tzinfo=dt.timezone.utc)


def test_late_daily_decisions_are_reported_from_two_oclock(monkeypatch):
    bot, j = trend_desk(series(), pulse_hours=0)
    day = yesterday()
    j.set("trend_day", day)
    j.set("trend_tries", {"day": day, "n": 4})
    j.event("error", f"Daily trend decisions: the decisions for {day} stayed incomplete after 4 tries (see the errors)")
    sent, fake = phone()
    monkeypatch.setattr(main, "dt", SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta,
                                                    date=dt.date))
    main.maybe_pulse(bot, fake, bot.base)
    assert sent == []                                      # 01:30 UTC: a retry is still normal
    Clock.hour_now = 3
    main.maybe_pulse(bot, fake, bot.base)
    assert sent == [(f"⏰ The daily trend decisions for {day} stayed incomplete after 4 tries; see /diagnose. Open "
                     "positions keep their emergency stop meanwhile. (Repeats at most every 6 hours.)", "trend-late")]


def test_heartbeat_and_diary_with_the_trend_rule():
    bot, j = trend_desk(series())
    bot.run_cycle()
    main.heartbeat(bot, 1)
    assert j.get("heartbeat")["trend_day"] == yesterday()
    hourly = Orchestrator(Settings(offline=True, symbols=["BTCUSDT"]), Journal(":memory:", 10_000))
    main.heartbeat(hourly, 1)
    assert "trend_day" not in hourly.journal.get("heartbeat")
    main.record_errors(bot, [{"symbol": "TREND", "stage": "trend", "error": "gave up"},
                             {"symbol": "ZZZUSDT", "stage": "trend", "error": "daily prices unavailable: timeout"}], {})
    errors = [e["text"] for e in j.events() if e["kind"] == "error"]
    assert errors == ["Trend ZZZUSDT: daily prices unavailable: timeout"]   # the give-up is recorded by the step itself


def test_desk_check_in_trend_mode(monkeypatch, capsys):
    from trader import binance_client
    from trader.agents.news import NewsAgent

    class Public:
        def __init__(self, *a, **k):
            pass

        def price(self, sym):
            return 65_000.0
    monkeypatch.setattr(binance_client, "BinanceClient", Public)
    full = candles(wavy(120, 100, 0.02))
    monkeypatch.setattr(trend, "fetch_daily", lambda client, sym, days, now_ms=None: full[-20:] if sym == "NEWUSDT" else full)
    monkeypatch.setattr(NewsAgent, "fetch_headlines", lambda self: 1 / 0)   # never asked in trend mode
    s = Settings(strategy="trend", symbols=["BTCUSDT", "NEWUSDT"], max_open_positions=2)
    assert main.cmd_check(s) == 1
    out = capsys.readouterr().out
    assert "[INFO] News / Forex / Market scout: not used by the daily trend rule" in out
    assert ("[FAIL] Daily history (trend rule): fewer than 61 finished days for NEW (20 days): the rule keeps it out "
            "until then") in out
    assert "\n       BTC closed at" in out and "\n       NEW: not enough daily history" in out
    assert "[INFO] Claude: not configured (only /review would use it in trend mode)" in out
    assert "[INFO] Free AI: not configured (not used by the daily trend rule)" in out
    assert "News feeds" not in out and "Forex" not in out.replace("News / Forex", "")
    assert main.cmd_check(Settings(strategy="trend", symbols=["BTCUSDT"], max_open_positions=1)) == 0
    assert "[ OK ] Daily history (trend rule): at least 61 finished days for the coin" in capsys.readouterr().out


# ---- the desk itself: one check with its phone commands ----
def run_desk(tmp_path, monkeypatch, **kw):
    phones, desks = [], []

    class Phone:
        enabled = True

        def __init__(self, *a, **k):
            self.sent, self.handlers = [], {}
            phones.append(self)

        def send(self, text, silent=False):
            self.sent.append((text, silent))
            return True

        def send_once(self, key, text, every_hours=6):
            return self.send(text)

        def send_long(self, text):
            return self.send(text)

        def send_document(self, path, caption=""):
            return True

        def listen(self, handlers):
            self.handlers = handlers

    class Desk(Orchestrator):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            desks.append(self)
    monkeypatch.setattr(main, "Telegram", Phone)
    monkeypatch.setattr(main, "Orchestrator", Desk)
    monkeypatch.setattr(main.Lab, "watch_live", lambda self: "profit factor 0.5 over 12 live trades")
    s = Settings(offline=True, db_path=str(tmp_path / "journal.db"), notes_dir=str(tmp_path / "notes"), **kw)
    old = signal.getsignal(signal.SIGTERM)
    try:
        main.cmd_run(s, SimpleNamespace(once=True, cycles=0, fast=True, quiet=True))
    finally:
        signal.signal(signal.SIGTERM, old)
    return s, phones[0], desks[0]


def test_the_desk_runs_the_trend_rule_with_its_own_phone_commands(tmp_path, monkeypatch, capsys):
    s, tg, bot = run_desk(tmp_path, monkeypatch, strategy="trend", symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                          max_open_positions=3)
    out = capsys.readouterr().out
    assert "Strategy: daily trend rule (50-day average, 25% emergency stop)  AI: none (the rule decides alone)" in out
    assert "an offline run makes one daily decision per real day" in out and "backtest --strategy trend" in out
    texts = [t for t, _ in tg.sent]
    assert texts[0].startswith("🚀 Trading desk started") and texts[0].endswith(main.help_text(s))
    assert "\nStrategy: daily trend rule (50-day average, 25% emergency stop)\nAI: none (the rule decides alone)\n" in texts[0]
    assert not any("rolled back" in t for t in texts)      # no card is judged on the trend rule's trades
    summaries = [(t, silent) for t, silent in tg.sent if t.startswith("📈 Daily trend decisions")]
    assert len(summaries) == 1 and summaries[0][1] is True
    h = tg.handlers
    assert h["/approve"]("abc") == h["/reject"]("abc") == main.TREND_NO_CARDS
    assert h["/help"]() == h["/start"]() == main.help_text(s)
    assert h["/strategies"]().startswith("📈 Strategy in force: the daily trend rule")
    status = h["/status"]()
    assert "📈 Daily trend decisions: last for" in status and "Position slots:" in status
    assert h["/watchlist"]().startswith("🔭 Your coins (SYMBOLS; the scout is off in trend mode)")
    assert "needs Binance's public prices" in h["/trendtest"]("")
    j = Journal(s.db_path, s.starting_balance)
    assert j.get("strategy_champion") is None              # /strategies recorded no hourly card
    start = [e["text"] for e in j.events() if e["kind"] == "start"][0]
    assert "; strategy: daily trend rule (50-day average, 25% emergency stop); AI: none" in start

    j.set("starting_balance", 5_000.0)                     # the replay reads the record's budget on its own connection
    bot.market.client = object()
    monkeypatch.setattr(trend, "fetch_daily", lambda client, sym, days, now_ms=None: candles(wavy(400, 100, 0.005)))
    assert h["/trendtest"]("100").startswith("📈 Replaying the daily trend rule over the last 100 days")
    for _ in range(200):
        if any("replayed over the last 100 days" in t for t, _ in tg.sent):
            break
        time.sleep(0.05)
    result = [t for t, _ in tg.sent if "replayed over the last 100 days" in t]
    assert result and "$5,000 start" in result[0]


def test_the_hourly_desk_starts_and_rolls_back_as_before(tmp_path, monkeypatch):
    s, tg, bot = run_desk(tmp_path, monkeypatch, symbols=["BTCUSDT"])
    assert tg.sent[0][0] == (f"🚀 Trading desk started (version {version_string()})\nMode: offline test (made-up "
                             f"prices)\nAI: built-in rules\nCoins: BTCUSDT\nBudget: $10,000 · checks every 15 min\n\n"
                             f"{main.HELP_TEXT}")
    assert any(t.startswith("⏪ Strategy rolled back to the previous champion") for t, _ in tg.sent)
    assert not any(t.startswith("📈 Daily trend decisions") for t, _ in tg.sent)
    assert tg.handlers["/help"]() == main.HELP_TEXT
