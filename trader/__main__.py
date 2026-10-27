"""Command line entry point.

  python -m trader setup              # answer a few questions, saves your settings (.env)
  python -m trader check              # verify connections (Binance, news, forex, Claude, Telegram)
  python -m trader run --once         # run one trading cycle
  python -m trader run                # run forever, one cycle every CYCLE_MINUTES
  python -m trader run --offline --cycles 200 --fast   # offline simulation with synthetic prices
  python -m trader report [--offline] # performance report
  python -m trader reset              # start a fresh journal (old one kept as a backup file)
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .config import AI_BUDGET_CEILING_USD, PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .journal import Journal
from .notify import Telegram, pace_message, scout_message, trade_messages
from .orchestrator import Orchestrator
from .report import goals, goals_text, performance, print_report, report_text, status_text, watchlist_text
from .lab import Lab, LabInterrupted, status_text as lab_status_text, week_report
from .agents.head import HeadTraderAgent
from .agents.research import ResearchAgent, review_text
from .calibration import study_text
from .notes import Notes, default_dir, lineage_text
from .notify import clean
from .scorecard import claude_scorecard, news_scorecard, scorecard_text
from .updater import DESK_STARTED, HEARTBEAT, read_status, revision, version_string
from .ai_router import ORDERS, AIRouter, is_ai_source
from .health import (agents_text, diagnose_text, free_ai_line, funnel, health_text, judged_since, pulse_text, stuck_rule,
                     write_json)

log = logging.getLogger("trader")

HELP_TEXT = (
    "🤖 Crypto trading desk commands\n"
    "/status - open positions and each coin's latest decision with the reason\n"
    "/report - performance so far (profit, win rate, order success)\n"
    "/watchlist - which coins the desk watches and why\n"
    "/strategies - the strategy in use, challengers under test, and what you can approve\n"
    "/approve <id> or /reject <id> - decide on a challenger that passed all tests\n"
    "/lab - run the weekly strategy lab now (takes a few minutes)\n"
    "/playbook - what the desk has learned, as the head trader passes it to every agent, and how it spends the AI budget\n"
    "/health - is everything working? last check, errors, settings problems, updates, and why buys were refused\n"
    "/agents [days] - how many agents are working, and which strategies were created, promoted or removed (default 7 days)\n"
    "/diagnose [days] - the desk's records versus its own rules replayed on the same prices: what stopped each buy signal\n"
    "/trendtest [days] - the daily trend rule replayed on real prices over the last days (default 365), with the desk's limits\n"
    "/aibudget [usd] - Claude's daily budget and where it went today; /aibudget 3 sets $3 a day, /aibudget default resets\n"
    "/ai [free-first|claude-first|default] - which AI answers first (the free AI or Claude), and the free AI's day\n"
    "/notes - the desk's notebook as a zip: strategies, trades, diary and results in Markdown, ready to give to Claude\n"
    "/review - Claude reads the notebook and writes a plain-language review (uses a little of the AI budget)\n"
    "/version - which code version is running, and whether a newer one exists\n"
    "/update - install the latest code now (it is tested first; the desk restarts itself)\n"
    "/help - this list\n\n"
    "You also get a message for every buy, sell or failed order, when the coin list or the check pace "
    "changes, a short pulse every few hours (PULSE_HOURS), and a daily report."
)


def print_cycle(n: int, summary: list[dict]) -> None:
    print(f"\n--- cycle {n} ({dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC) ---")
    for r in summary:
        if "error" in r:
            print(f"{r['symbol']:9s} ERROR {r['error']}")
            continue
        line = f"{r['symbol']:9s} {r.get('action', ''):4s}{'*' if is_ai_source(r.get('source')) else ' '}"
        if "confidence" in r:
            px = f"{r['price']:,.2f}" if r['price'] >= 1 else f"{r['price']:.6g}"
            line += f" conf={r['confidence']:.2f} score={r['score']:+.2f} price={px}  risk: {r['risk']}"
        if "reason" in r:
            line += f" ({r['reason']})"
        if "protect_error" in r:
            line += f"  protection not placed: {r['protect_error']}"
        if o := r.get("order"):
            line += f"  -> {'FILLED' if o['ok'] else 'FAILED'} " + (
                f"{o['qty']:.6g} @ {o['avg_price']:,.6g}" if o["ok"] else o["error"])
        if t := r.get("trade"):
            line += f"  P&L {t['pnl']:+.2f} USDT ({t['pnl_pct']:+.2f}%)"
        print(line)


def cmd_check(s: Settings) -> int:
    from .agents.forex import ForexAgent
    from .agents.news import NewsAgent
    from .binance_client import BinanceClient
    from .llm import ClaudeJSON

    failures = 0

    def step(name, fn):
        nonlocal failures
        try:
            print(f"[ OK ] {name}: {fn()}")
        except Exception as e:
            failures += 1
            print(f"[FAIL] {name}: {e}")

    print(f"Mode: {s.mode}")
    step("Real market prices (Binance)", lambda: f"BTCUSDT = {BinanceClient(PUBLIC_BASE_URL).price('BTCUSDT'):,.2f}")
    if s.mode == "testnet":
        c = BinanceClient(TESTNET_BASE_URL, s.api_key, s.api_secret)
        step("Binance testnet account", lambda: {k: v for k, v in c.balances().items() if k in ("USDT", "BTC", "ETH", "SOL")})

        def oco():
            missing = [sym for sym in s.symbols if not c.symbol_filters(sym)["oco_allowed"]]
            if missing:
                raise RuntimeError(f"not available for {', '.join(missing)} - the desk will watch those itself")
            return f"supported for {', '.join(s.symbols)}"
        step("Stop-loss orders on the exchange (OCO)", oco)
    step("News feeds", lambda: f"{len(NewsAgent().fetch_headlines())} headlines")
    step("Forex (ECB rates)", lambda: ForexAgent().run()["regime"])
    if s.auto_coins:
        def scout():
            from .agents.scout import MarketScoutAgent
            testnet = BinanceClient(TESTNET_BASE_URL, s.api_key, s.api_secret) if s.mode == "testnet" else None
            cands = MarketScoutAgent(BinanceClient(PUBLIC_BASE_URL), testnet, None, s).candidates()
            return f"{len(cands)} liquid coins to choose from, e.g. " + ", ".join(c["symbol"][:-4] for c in cands[:8])
        step("Market scout", scout)
    if s.anthropic_key:
        def claude():
            ok = ClaudeJSON(s.claude_model, s.anthropic_key).ask(
                "Reply with JSON.", "Say ok.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                "required": ["ok"], "additionalProperties": False}, effort="low")
            if ok is None:
                raise RuntimeError("no valid answer - check the API key and account credit")
            return f"{s.claude_model} answered"
        step("Claude", claude)
    else:
        print("[INFO] Claude: not configured")
    if s.use_free_ai:
        def free():
            from .free_ai import from_settings
            ai = from_settings(s)
            ok = ai.ask("Reply with JSON.", "Say ok.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                        "required": ["ok"], "additionalProperties": False}, effort="low")
            if ok is None:
                why = "; ".join(p["text"] for p in ai.take_problems()) or "no valid answer (busy, or the model is unknown)"
                raise RuntimeError(why)
            return f"{ai.label} answered ({', '.join(ai.models)})"
        step("Free AI", free)
    elif s.free_ai_provider:
        print(f"[FAIL] Free AI: FREE_AI_PROVIDER={s.free_ai_provider} is incomplete - run desk setup")
        failures += 1
    else:
        print("[INFO] Free AI: not configured" + ("" if s.use_claude else " - agents use built-in rules"))
    if s.telegram_token and s.telegram_chat_id:
        step("Telegram", lambda: f"@{Telegram(s.telegram_token, s.telegram_chat_id).bot_username()}")
    else:
        print("[INFO] Telegram: not configured - no phone alerts")
    return failures


def live_prices(s: Settings, j: Journal) -> dict[str, float]:
    """Current real prices for open positions and benchmarks, so the report values them correctly."""
    from .binance_client import BinanceClient
    prices = {}
    if not s.offline:
        bench = (j.get("benchmark_start") or {}).get("prices", {})
        for sym in set(j.positions()) | set(bench):
            try:
                prices[sym] = BinanceClient(PUBLIC_BASE_URL).price(sym)
            except Exception:
                pass
    return prices


def with_journal(s: Settings, fn):
    """Open a separate journal connection (the Telegram listener runs in its own thread)."""
    j = Journal(s.db_path, s.starting_balance)
    try:
        return fn(j)
    finally:
        j.db.close()


def maybe_daily_report(bot: Orchestrator, tg: Telegram, s: Settings) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    if not tg.enabled or now.hour < s.daily_report_hour_utc or bot.journal.get("last_daily_report") == today:
        return
    perf = performance(bot.journal, bot.last_price)
    text = report_text(perf, title=f"☀️ Daily report {today}")
    text += "\nWatching: " + ", ".join(sym.removesuffix("USDT") for sym in bot.watchlist()["symbols"])
    text += "\n\n" + goals_text(goals(bot.journal, perf, bot.last_price, trend=s.trend), perf)
    if card := scorecard_block(bot, bot.journal):
        text += "\n\n" + card
    bot.notes.refresh(bot.last_price, scorecard=card)  # the notebook's PERFORMANCE.md carries today's scorecard
    if tg.send(text):
        bot.journal.set("last_daily_report", today)


def scorecard_block(bot: Orchestrator, j: Journal) -> str:
    """Claude-vs-rules scorecard from real later prices (empty when there is nothing to judge or no data)."""
    client = bot.market.client
    if client is None:
        return ""

    def candles_after(symbol: str, since_ms: int) -> list[dict]:
        return client.klines(symbol, "1h", limit=130, start_ms=since_ms)
    try:
        claude = claude_scorecard(j, bot.s, candles_after)
        free = []
        if getattr(bot, "free_ai", None):
            card = claude_scorecard(j, bot.s, candles_after, source=f"ai:{bot.free_ai.provider}", name=bot.free_ai.label)
            if card["cases"]:
                free.append((bot.free_ai.label, card))
        news = news_scorecard(j, candles_after)
    except Exception as e:
        log.warning("Scorecard failed: %s", e)
        return ""
    if not claude["cases"] and not news["judged"] and not free:
        return ""
    text = scorecard_text(claude, news, float(j.get("ai_spend_total") or 0), free)
    j.set("scorecard_text", text)  # reused by the notebook between daily reports
    j.set("news_scorecard", news)  # the head trader tilts thresholds on news only once this shows news predicts
    return text


def status_extra(bot: Orchestrator, s: Settings) -> list[str]:
    lines = [f"Next check in about {bot.pace['minutes']} min: {bot.pace['reason']} ({bot.pace['source']})"]
    from .updater import read_status
    if (st := read_status()) and st.get("behind"):
        lines.append("🆕 A new code version is available - send /update" if not st.get("auto_update")
                     else "🆕 A new code version is available - it installs automatically within 30 min")
    if bot.llm:
        budget = bot.llm.effective_daily_budget()
        cap = f" of ${budget:.2f} daily cap" if budget else ""
        lines.append(f"Claude spend today: about ${bot.llm.spent_today:.2f}{cap}")
    if getattr(bot, "free_ai", None):
        lines.append(free_ai_line(bot.free_ai))
    return lines


def default_watchlist(s: Settings) -> dict:
    return {"symbols": list(s.symbols), "reasons": {c: "core coin (always watched)" for c in s.symbols}}


def single_instance(db_path: str):
    """Two desks on one journal would double-trade; hold a lock for the life of the process."""
    lock_path = Path(db_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("The desk is already running on this journal (on the server: `desk logs` to watch it, "
                         "`desk stop` first if you really want to run it by hand).")
    return fh


def weekly_backup(bot: Orchestrator, tg: Telegram) -> None:
    """Once a week, send a copy of the journal to the owner's phone: the demo's evidence should not live
    only on one free server that may be reclaimed."""
    import sqlite3
    week = dt.datetime.now(dt.timezone.utc).strftime("%G-W%V")
    if not tg.enabled or bot.s.db_path == ":memory:" or bot.journal.get("last_backup_week") == week:
        return
    target = Path(bot.s.db_path).with_name(f"journal-backup-{week}.db")
    try:
        with sqlite3.connect(target) as dest:
            bot.journal.db.backup(dest)
        if tg.send_document(target, f"📦 Weekly backup of the trading journal ({week}). Keep it somewhere safe."):
            bot.journal.set("last_backup_week", week)
            send_notebook(bot, tg, f"📒 Weekly notebook ({week}): the same facts as Markdown, for you or for Claude. "
                                   "Start with README.md inside.")
    except (sqlite3.Error, OSError) as e:
        log.warning("Journal backup failed: %s", e)
    finally:
        target.unlink(missing_ok=True)


def send_notebook(bot: Orchestrator, tg: Telegram, caption: str, notes: Notes | None = None) -> bool:
    """Zip the notebook and upload it to the owner's phone."""
    notes = notes or bot.notes
    path = notes.bundle()
    if not path:
        return False
    try:
        return tg.send_document(path, caption)
    finally:
        path.unlink(missing_ok=True)


def run_lab(bot: Orchestrator, tg: Telegram, trade_lock: threading.Lock, reason: str, should_stop=None) -> str:
    """Weekly research + exam + forward tests. Holds the trade lock, so it never interleaves with a trading
    decision (positions keep their stop-loss and take-profit on the exchange meanwhile). `should_stop` is checked
    between tests: a restart stops the lab within seconds instead of being killed after ten minutes."""
    with trade_lock:
        heartbeat(bot, note="weekly strategy lab running (no market checks until it finishes)")
        lab = Lab(bot.base, bot.journal, client=bot.market.client)
        if should_stop:
            lab.should_stop = should_stop
        research = ResearchAgent(bot.ai)   # web research needs Claude; the proposals may come from the free AI
        lessons = research.lessons(bot.journal)
        bot.journal.set("lessons", lessons)
        if lessons.get("notes"):
            bot.journal.event("lesson", "; ".join(lessons["notes"]))
        tried = {c["card"]["id"] for c in lab.challengers()}
        study_result, study_card = None, None
        if bot.head:
            try:  # the head trader's threshold study becomes a challenger like any other proposal
                # only coins the exam trades: a threshold for a coin the exam never sees would go live unexamined
                coins = list(dict.fromkeys(list(bot.base.symbols) + list(bot.base.lab_alt_symbols)))
                study_result, study_card = bot.head.study(lab, coins, should_stop=lab.should_stop)
            except LabInterrupted:
                raise
            except Exception as e:  # noqa: BLE001 - the lab must still run without the study
                log.exception("Threshold study failed")
                bot.journal.event("error", f"Threshold study failed: {e}")
        # the proposals stay with Claude (CLAUDE_ONLY in trader/ai_router.py), so the full playbook may go in
        knowledge = lineage_text(bot.journal) + ("\n" + bot.head.playbook(private=True) if bot.head else "")
        proposals = research.propose(lab.champion(), lessons, bot.base.research_candidates, tried, lineage=knowledge)
        if study_card:
            proposals = [study_card] + proposals
        result = lab.run_week(proposals)
        text = week_report(result) + f"\n({reason})"
        if study_result:
            text += "\n" + study_text(study_result)
        if lessons.get("notes"):
            text += "\nLessons from the last 30 days: " + "; ".join(lessons["notes"])
        ideas = getattr(research, "ideas_needing_code", None)
        if ideas:
            text += "\nIdeas that would need new code (for the developer): " + "; ".join(ideas[:3])
        tg.send(text)

        # the week's page in the notebook: what was known and decided, written once and kept
        scorecard = bot.journal.get("scorecard_text") or ""
        perf = performance(bot.journal, bot.last_price)
        perf_text = report_text(perf, title="Performance") + "\n" + goals_text(goals(bot.journal, perf, bot.last_price, trend=bot.base.trend), perf)
        review = None
        if bot.llm:
            data = research.review(bot.notes.digest(bot.last_price, scorecard=scorecard))
            if data:
                review = review_text(data)
                bot.journal.event("review", "Claude reviewed the notebook: " + str(data.get("summary", ""))[:300])
                bot.journal.set("last_review", {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                                "review": data})
                tg.send_long(clean(review, 12_000))
        week = dt.datetime.now(dt.timezone.utc).strftime("%G-W%V")
        bot.notes.weekly_snapshot(week, [("Laboratory", text), ("Lessons (post-mortem of the last 30 days)",
                                          "\n".join(f"- {n}" for n in lessons.get("notes", [])) or "- nothing notable"),
                                         ("Threshold study", study_text(study_result) if study_result else ""),
                                         ("Playbook", bot.head.playbook(private=True) if bot.head else ""),
                                         ("Claude's review", review or "(Claude not configured, or unavailable this week)"),
                                         ("Performance", "```\n" + perf_text + "\n```"),
                                         ("The AI versus the rules", "```\n" + scorecard + "\n```" if scorecard else "")])
        bot.notes.refresh(bot.last_price, scorecard=scorecard)
        return text


def maybe_weekly_lab(bot: Orchestrator, tg: Telegram, s: Settings, trade_lock: threading.Lock, should_stop=None) -> None:
    if not s.lab_enabled or s.offline or s.trend:   # the lab tests and promotes hourly strategy cards only
        return
    now = dt.datetime.now(dt.timezone.utc)
    week = now.strftime("%G-W%V")
    if now.weekday() != s.lab_weekday or now.hour < s.daily_report_hour_utc or bot.journal.get("lab_week") == week:
        return
    bot.journal.set("lab_week", week)
    try:
        run_lab(bot, tg, trade_lock, "scheduled weekly run", should_stop=should_stop)
    except LabInterrupted:
        bot.journal.set("lab_week", None)  # it runs again after the restart
        bot.journal.event("lab", "Weekly lab interrupted by a restart; it runs again after the restart")
    except Exception as e:
        log.exception("Weekly lab failed")
        bot.journal.event("error", f"Weekly lab failed: {e}")
        tg.send_once("lab", f"⚠️ The weekly strategy lab failed: {e}. The champion strategy keeps trading.")


def ai_budget(bot: Orchestrator, j: Journal, arg: str = "") -> str:
    """/aibudget: show Claude's daily cap and today's spend per agent; '/aibudget 3' sets $3 a day (kept across restarts,
    applied at once); '/aibudget default' goes back to the settings file. Changes are logged in the diary."""
    llm = bot.llm
    free = getattr(bot, "free_ai", None)
    if not llm:
        if free:
            return ("Claude is not configured, so there is no AI bill: the free AI answers within its daily allowance.\n"
                    + free_ai_line(free))
        return "Claude is not configured on this desk (no API key), so there is no AI budget. The free rules decide."
    arg = arg.strip().lower().lstrip("$")
    if arg in ("default", "reset"):
        j.set("ai_budget_override", None)
        llm.daily_budget = bot.base.ai_daily_budget_usd
        llm.pct_daily_budget = bot.base.starting_balance * bot.base.ai_pct_month() / 100 / 30 if bot.base.ai_pct_month() else 0.0
        llm.budget_hit = False
        j.event("owner", f"Claude's daily budget back to the settings: ${llm.effective_daily_budget():.2f}")
        return f"✅ Claude's daily budget is back to the settings file: ${llm.effective_daily_budget():.2f} a day."
    if arg:
        try:
            usd = float(arg)
        except ValueError:
            return "Usage: /aibudget, /aibudget 3 (dollars a day) or /aibudget default"
        if not 0 < usd <= AI_BUDGET_CEILING_USD:
            return f"Choose an amount above $0 and at most ${AI_BUDGET_CEILING_USD:g} a day."
        j.set("ai_budget_override", {"usd_per_day": usd, "set_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                     "by": "owner"})
        llm.daily_budget, llm.pct_daily_budget = usd, 0.0
        llm.budget_hit = llm.spent_today >= usd
        j.event("owner", f"Claude's daily budget set to ${usd:.2f} a day")
        return (f"✅ Claude may now spend up to ${usd:.2f} a day (at most ${usd * 30:,.0f} a month of real money). It applies "
                f"now and after restarts; /aibudget default goes back to the settings file.")
    by = llm.by_caller or {}
    cap = llm.effective_daily_budget()
    lines = [f"💸 Claude's budget: " + (f"${cap:.2f} a day" if cap else "no daily cap") + f" on {llm.model}; "
             f"spent today ${llm.spent_today:.2f}"
             + ((" (used up until 00:00 UTC: " + ("the free AI, then the free rules, answer" if free else "free rules")
                 + ")") if llm.budget_hit else "")]
    if j.get("ai_budget_override"):
        lines.append(f"Set by you with /aibudget on {j.get('ai_budget_override')['set_at'][:10]}.")
    for name, label in (("news", "News"), ("decision", "Buy/sell decisions"), ("scout", "Coin scout"),
                        ("research", "Strategy research"), ("review", "Reviews")):
        c = by.get(name) or {}
        if c.get("calls"):
            lines.append(f"• {label}: {c['calls']} calls, ${c['usd']:.2f}")
    now = dt.datetime.now(dt.timezone.utc)
    hours = max(1.0, now.hour + now.minute / 60)
    if free:
        lines.append(free_ai_line(free))
    lines.append(f"At today's pace about ${llm.spent_today / hours * 24 * 30:,.0f} a month. Change it with /aibudget 3 "
                 f"(dollars a day, at most ${AI_BUDGET_CEILING_USD:g}). A cheaper model (CLAUDE_MODEL=claude-haiku-5-5 in desk "
                 "setup) does the same work for a fraction of the cost.")
    return "\n".join(lines)


def ai_command(bot: Orchestrator, j: Journal, arg: str = "") -> str:
    """/ai: who answers the agents' questions, in which order, and the free AI's day. '/ai free-first' or
    '/ai claude-first' changes the order at once and keeps it across restarts."""
    free, claude, router = getattr(bot, "free_ai", None), bot.llm, getattr(bot, "ai", None)
    arg = arg.strip().lower()
    if arg == "default":
        j.set("ai_order_override", None)
        if isinstance(router, AIRouter):
            router.order = bot.base.ai_order if bot.base.ai_order in ORDERS else "free-first"
        j.event("owner", "AI order back to the settings file")
        return f"✅ Back to the settings file: {bot.base.ai_order if bot.base.ai_order in ORDERS else 'free-first'}."
    if arg:
        if arg not in ORDERS:
            return "Usage: /ai, /ai free-first, /ai claude-first or /ai default"
        if not (free and claude):
            return "Both a free AI and Claude are needed to choose an order (desk setup adds the missing one)."
        router.order = arg
        # stamped with the setting it replaces: a later change of AI_ORDER in desk setup wins over this choice
        j.set("ai_order_override", {"order": arg, "settings": bot.base.ai_order,
                                    "set_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        j.event("owner", f"AI order set to {arg}")
        return ("✅ The free AI answers first now; Claude only when it cannot (saves money)." if arg == "free-first" else
                "✅ Claude answers first now while its daily budget lasts; the free AI after (best judgement).")
    if not (free or claude):
        return ("No AI is configured: the built-in rules decide. A free AI costs nothing: run desk setup on the server "
                "and choose Google Gemini (see the guide, 'Add a free AI').")
    order = getattr(router, "order", "free-first") if free and claude else ""
    lines = ["🤖 The desk's AIs"]
    if free and claude:
        lines.append(("Order: the free AI first, Claude when it cannot answer, then the free rules (/ai claude-first "
                      "to change)") if order == "free-first" else
                     "Order: Claude first while its budget lasts, then the free AI, then the free rules (/ai free-first "
                     "to change)")
    if free:
        lines.append(free_ai_line(free))
        if free.preset.trains_on_free_data:
            lines.append("Note: on its free tier this provider may use what it reads to improve its models; the desk sends "
                         "it only public headlines, market numbers and its strategy rules, never balances, keys or the "
                         "notebook.")
    if claude:
        budget = claude.effective_daily_budget()
        lines.append(f"Claude ({claude.model}): ${claude.spent_today:.2f} spent today "
                     + (f"of ${budget:.2f}" if budget else "(no daily cap)") + " (/aibudget to change)")
        lines.append("Always Claude: web research (scout, weekly lab), the lab's strategy proposals and /review of the "
                     "private notebook.")
    else:
        lines.append("Claude is not configured: no web research (the scout judges from market data), the lab tests "
                     "rule-based variations of the strategy, and no /review.")
    return "\n".join(lines)


WARMUP_DAYS = 230   # history before a replayed period: the 200-day average plus a margin
TREND_NO_LAB = ("The weekly strategy lab is not used with the daily trend rule (STRATEGY=trend): it tests and "
                "promotes hourly strategies only. To measure the trend rule on more history, send /trendtest 1000.")


def rules_replay(bot: Orchestrator, symbols: list[str], period: float) -> dict | None:
    """The rule core over the same period and coins, with the strategy in force and real prices (for /diagnose).
    Uses the card's own rules, so it never changes what the live desk decides meanwhile."""
    from .agents.decision import DecisionAgent
    from .backtest import Backtest, fetch_history
    client = bot.market.client
    if client is None:
        return None
    if bot.base.trend:   # the daily trend rule on the owner's coins (its shares depend on how many there are)
        from . import trend
        now_ms = int(time.time() * 1000)
        need = trend.window(trend.params(bot.base)[0]) + 2
        hist = {sym: trend.fetch_daily(client, sym, int(period) + need, now_ms) for sym in bot.base.symbols}
        return trend.backtest(bot.base, hist, start_ms=now_ms - int(period * trend.DAY_MS))
    # the checklist's 200-day average and the indicators need history before the period: without it the replay's
    # rules are looser than the live desk's (a check without enough history is "unknown" and does not block)
    n = int(period) + WARMUP_DAYS
    hist = {sym: fetch_history(client, sym, n, bot.s.interval) for sym in symbols}
    btc = hist.get("BTCUSDT") or fetch_history(client, "BTCUSDT", n, bot.s.interval)
    return Backtest(bot.s, hist, decision=DecisionAgent.for_card(bot.card) if bot.card else None,
                    days=period, btc_history=btc).run()


def maybe_pulse(bot: Orchestrator, tg: Telegram, s: Settings) -> None:
    """Every PULSE_HOURS: what the desk did, so a silent phone always means something is wrong. And straight
    away, at most twice a day: one rule refusing every buy signal for a day (a sign of a broken setting)."""
    f = funnel(bot.journal, judged_since(bot.journal))  # only decisions under the strategy now in force
    if (stuck := stuck_rule(f)):
        tg.send_once("stuck-rule", f"🛑 '{stuck[0]}' refused every one of the {stuck[1]} buy signals made under the current "
                                   "strategy (last 24 hours at most). That usually means a setting the rule can never "
                                   "accept. Send /health.", every_hours=12)
    if not tg.enabled or not s.pulse_hours:
        return
    last = bot.journal.get("last_pulse")
    now = dt.datetime.now(dt.timezone.utc)
    if last and (now - dt.datetime.fromisoformat(last)).total_seconds() < s.pulse_hours * 3600:
        return
    if tg.send(pulse_text(bot, bot.journal, s.pulse_hours, version_string())):
        bot.journal.set("last_pulse", now.isoformat(timespec="seconds"))


def heartbeat(bot: Orchestrator, cycle: int = 0, note: str = "", seconds: float | None = None) -> None:
    """After every check: the time, for /health, the pulse and the update job's watchdog (a separate process
    that notices when the desk has stopped checking the market). `seconds`: how long the check took, so /health can
    say whether the server keeps up (the last 96 checks are kept)."""
    beat = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "cycle": cycle,
            "next_minutes": bot.pace["minutes"], "version": version_string(), "note": note}
    if seconds is not None:
        beat["seconds"] = round(seconds, 1)
        bot.journal.set("check_seconds", ((bot.journal.get("check_seconds") or []) + [round(seconds, 1)])[-96:])
    bot.journal.set("heartbeat", beat)
    if not bot.s.offline and bot.s.db_path != ":memory:":
        write_json(HEARTBEAT, beat)


def record_errors(bot: Orchestrator, summary: list[dict], seen: dict) -> None:
    """Errors in a check go to the diary (once per agent and coin per 6 hours), so /agents can say who is failing."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for r in summary:
        if "error" not in r:
            continue
        key = (r.get("stage", ""), r["symbol"])
        text = f"{r.get('stage', 'check')} {r['symbol']}: {r['error']}"[:400]
        last = seen.get(key)
        if last and last[0] == text and now - last[1] < 6 * 3600:
            continue
        seen[key] = (text, now)
        bot.journal.event("error", text[0].upper() + text[1:])


def start_checks(bot: Orchestrator, s: Settings) -> list[str]:
    """Messages for the owner at start: a corrected old setting, a strategy replaced, settings that cannot work,
    and the version change since the last start."""
    out = []
    for m in s.migrations:
        out.append(f"🔧 Settings corrected at start: {m}. Nothing for you to do; `desk setup` saves it to the file.")
        bot.journal.event("strategy", f"Settings corrected at start: {m}")
    out += bot.apply_budget_change()
    out += bot.note_strategy_mode()
    bot.apply_champion()
    if bot.champion_note:
        out.append(f"🔧 Strategy in force updated because {bot.champion_note}.")
    for p in s.problems():
        out.append(f"🛑 Settings problem: {p}. Fix it with `desk setup` on the server.")
        bot.journal.event("error", f"Settings problem: {p}")
    current = {"version": version_string(), "revision": revision()}
    prev = bot.journal.get("running_version")
    if prev and prev.get("revision") != current["revision"]:
        out.append(f"✅ Update confirmed: the desk now runs version {current['version']} (before: {prev.get('version')}).")
        bot.journal.event("update", f"Now running version {current['version']} (before: {prev.get('version')})")
    bot.journal.set("running_version", current)
    return out


def cmd_run(s: Settings, args) -> None:
    lock = single_instance(s.db_path)  # noqa: F841 - held until exit
    bot = Orchestrator(s)
    trade_lock = threading.Lock()
    stop = threading.Event()  # a restart (update, reboot) waits for the current check to finish; the lab stops early
    tg = Telegram(s.telegram_token, s.telegram_chat_id) if not s.offline else Telegram("")
    mode = "Binance testnet demo account (fake money)" if s.mode == "testnet" and not s.offline else \
        "offline test (made-up prices)" if s.offline else "paper trading at real prices"
    ais = ([f"free AI {bot.free_ai.label} ({', '.join(bot.free_ai.models)})"] if bot.free_ai else []) \
        + ([f"Claude ({s.claude_model})"] if s.use_claude else [])
    if bot.free_ai and s.use_claude and getattr(bot.ai, "order", "") == "claude-first":
        ais.reverse()
    ai = (" first, then ".join(ais) + ", then the built-in rules") if ais else "built-in rules"
    pace = f"every {s.cycle_minutes} min" + (
        f" (the AI may adjust it between {s.min_check_minutes} and {s.max_check_minutes})" if ais else "")
    coins = ", ".join(s.symbols) + (f" + scout picks (up to {s.max_coins} coins)" if bot.scout else "")
    print(f"Version {version_string()}  Mode: {mode}  AI: {ai}  checks: {pace}\nCoins: {coins}  journal: {s.db_path}")
    bot.journal.event("start", f"Desk started: {mode}; AI: {ai}; coins: {coins}; checks {pace}; version {bot.notes.version()}")
    started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not s.offline and s.db_path != ":memory:":  # the update job waits for this file to confirm a new version started
        write_json(DESK_STARTED, {"version": version_string(), "revision": revision(), "pid": os.getpid(),
                                  "started_at": started_at})
    notices = start_checks(bot, s)
    for line in notices:
        print(line)
    if bot.journal.get("last_pulse") is None:
        bot.journal.set("last_pulse", started_at)

    if tg.enabled:
        if bot.journal.get("last_daily_report") is None:  # first start: first daily report tomorrow
            bot.journal.set("last_daily_report", dt.datetime.now(dt.timezone.utc).date().isoformat())
        tg.send(f"🚀 Trading desk started (version {version_string()})\nMode: {mode}\nAI: {ai}\nCoins: {coins}\n"
                f"Budget: ${s.starting_balance:,.0f} · checks {pace}\n\n{HELP_TEXT}")
        if notices:
            tg.send("\n\n".join(notices))

        def watched(j: Journal) -> list[str]:
            return (j.get("watchlist") or default_watchlist(s))["symbols"]

        def update_now() -> str:
            from .updater import request_update, updater_installed
            if not updater_installed():
                return "Automatic updates are not set up on this machine. On the server, run:  desk update"
            request_update()
            return "⏳ Checking GitHub for new code now. You'll get a message with the result within a few minutes."

        from .updater import version_text

        def lab_status() -> str:
            return with_journal(s, lambda j: lab_status_text(Lab(bot.base, j, client=None)))

        def approve(arg: str) -> str:
            card_id = arg.strip().lower()
            if not card_id:
                return "Usage: /approve <id>  (ids are shown by /strategies)"
            card = with_journal(s, lambda j: Lab(bot.base, j, client=None).promote(card_id, by="owner"))
            if not card:
                return f"No challenger [{card_id}] is ready to approve. See /strategies."
            return f"✅ Promoted {card.name} [{card.id}]. The desk switches at its next check.\n{card.describe_diff(bot.card)}"

        def reject(arg: str) -> str:
            ok = with_journal(s, lambda j: Lab(bot.base, j, client=None).reject(arg.strip().lower()))
            return "Rejected." if ok else "No such challenger (or it is already the champion). See /strategies."

        def lab_now() -> str:
            if s.trend:
                return TREND_NO_LAB
            if not s.lab_enabled:
                return "The strategy lab is switched off (LAB_ENABLED=false)."
            def lab_thread():
                try:
                    run_lab(bot, tg, trade_lock, "requested on Telegram", should_stop=stop.is_set)
                except LabInterrupted:
                    pass  # the desk is stopping
                except Exception as e:  # noqa: BLE001
                    log.exception("Lab failed")
                    tg.send(f"⚠️ The strategy lab failed: {e}. The strategy in force keeps trading.")
            threading.Thread(target=lab_thread, daemon=True).start()
            return "🔬 Lab started: downloading history and testing challengers. Results arrive here in a few minutes."

        def playbook_now() -> str:
            if s.trend:
                return ("The head trader is not used by the daily trend rule (STRATEGY=trend): no AI decides a trade, "
                        "so there is no AI budget to pace and no threshold to tilt.")
            if not bot.head:
                return "The head trader is switched off (HEAD_TRADER=false)."
            text = with_journal(s, lambda j: HeadTraderAgent(bot.base, j, bot.ai).playbook(private=True))
            plan = bot.head.last_plan
            budget = ""
            if plan:
                budget = (f"\n\nAI budget at the last check: asked for {', '.join(plan['allowed']) or 'nobody'}"
                          + (f"; skipped {', '.join(plan['skipped'])}" if plan.get("skipped") else "") + f". {plan['why']}.")
            return "📒 Desk playbook (what every agent is told)\n" + (clean(text, 3000) or "Nothing learned yet: the first "
                                                                      "lab run and the first trades fill this in.") + budget

        def health_now() -> str:
            from .updater import installed_version
            return with_journal(s, lambda j: health_text(bot, j, version_string(), started_at, read_status(),
                                                         installed_version()))

        def agents_now(arg: str = "") -> str:
            try:
                days = max(1.0, min(90.0, float(arg))) if arg.strip() else 7.0
            except ValueError:
                days = 7.0
            return with_journal(s, lambda j: agents_text(bot, j, days))

        def diagnose_now(arg: str = "") -> str:
            try:
                days = max(0.5, min(30.0, float(arg))) if arg.strip() else 7.0
            except ValueError:
                days = 7.0

            def work() -> None:
                try:
                    tg.send_long(with_journal(s, lambda j: diagnose_text(
                        bot, j, days, lambda symbols, period: rules_replay(bot, symbols, period))))
                except Exception as e:  # noqa: BLE001 - a report must never take the desk down
                    log.exception("Diagnosis failed")
                    tg.send(f"⚠️ The diagnosis failed: {e}")
            threading.Thread(target=work, daemon=True).start()
            return (f"🔎 Reading the last {days:g} days of the desk's records and replaying its rules on the same prices. "
                    "The report arrives here in a minute or two.")

        def trendtest_now(arg: str = "") -> str:
            from . import trend
            try:
                days = int(max(30, min(1500, float(arg)))) if arg.strip() else 365
            except ValueError:
                days = 365
            if bot.market.client is None:
                return "The replay needs Binance's public prices, which offline mode does not read."
            ts = trend.preview_settings(bot.base)
            ts = dataclasses.replace(ts, starting_balance=float(bot.journal.get("starting_balance") or ts.starting_balance))

            def work() -> None:
                try:
                    now_ms = int(time.time() * 1000)
                    need = trend.window(trend.params(ts)[0]) + 2
                    hist = {sym: trend.fetch_daily(bot.market.client, sym, days + need, now_ms) for sym in ts.symbols}
                    r = trend.backtest(ts, hist, start_ms=now_ms - days * trend.DAY_MS)
                    tg.send(trend.result_text(r, ts, days) + ("" if s.trend else
                            "\n\nThe desk trades the hourly strategy now. To switch: `desk setup` on the server, "
                            "strategy question: choose the daily trend rule."))
                except Exception as e:  # noqa: BLE001 - a report must never take the desk down
                    log.exception("Trend replay failed")
                    tg.send(f"⚠️ The trend replay failed: {e}")
            threading.Thread(target=work, daemon=True).start()
            return f"📈 Replaying the daily trend rule over the last {days} days. The result arrives here in a minute."

        def aibudget_now(arg: str = "") -> str:
            return with_journal(s, lambda j: ai_budget(bot, j, arg))

        def ai_now(arg: str = "") -> str:
            return with_journal(s, lambda j: ai_command(bot, j, arg))

        def notes_now() -> str:
            if not bot.notes.enabled:
                return "The notebook is switched off for this journal."

            def build(j: Journal) -> bool:
                notes = Notes(j, bot.notes.dir, bot.base)
                notes.refresh(bot.last_price, scorecard=j.get("scorecard_text") or "")
                return send_notebook(bot, tg, "📒 The desk's notebook: strategies, trades, diary and results as Markdown. "
                                              "Upload it to Claude and ask what to improve (see README.md inside).", notes)
            if with_journal(s, build):
                return ("Inside the zip: README.md (start here), STRATEGY.md, TRADES.md, DIARY.md, PERFORMANCE.md, "
                        "strategies/ (one file per strategy) and weekly/ (one page per week).")
            return "Could not upload the notebook. On the server it is in data/notes (or run: desk notes)."

        def review_now() -> str:
            if not bot.llm:
                return "Claude is not configured, so there is no AI review. The notebook itself is available with /notes."

            def work() -> None:
                def run(j: Journal) -> str:
                    notes = Notes(j, bot.notes.dir, bot.base)
                    data = ResearchAgent(bot.llm).review(notes.digest(bot.last_price, scorecard=j.get("scorecard_text") or ""))
                    if not data:
                        return ("Claude could not review the notebook right now (daily AI budget used up, or the service "
                                "is unavailable). Try again later.")
                    j.event("review", "Claude reviewed the notebook: " + str(data.get("summary", ""))[:300])
                    j.set("last_review", {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "review": data})
                    return review_text(data)
                try:
                    tg.send_long(clean(with_journal(s, run), 12_000))
                except Exception as e:  # a review must never take the desk down
                    log.exception("Review failed")
                    tg.send(f"⚠️ The review failed: {e}")
            threading.Thread(target=work, daemon=True).start()
            return "🧠 Claude is reading the notebook. The review arrives here in a minute or two."

        tg.listen({
            "/update": update_now,
            "/version": version_text,
            "/strategies": lab_status,
            "/approve": approve,
            "/reject": reject,
            "/lab": lab_now,
            "/notes": notes_now,
            "/playbook": playbook_now,
            "/health": health_now,
            "/agents": agents_now,
            "/diagnose": diagnose_now,
            "/aibudget": aibudget_now,
            "/trendtest": trendtest_now,
            "/ai": ai_now,
            "/review": review_now,
            "/report": lambda: with_journal(s, lambda j: report_text(performance(j, bot.last_price)) + "\n\n"
                                            + goals_text(goals(j, performance(j, bot.last_price), bot.last_price, trend=s.trend),
                                                         performance(j, bot.last_price))
                                            + (("\n\n" + scorecard_block(bot, j)) if scorecard_block(bot, j) else "")),
            "/status": lambda: with_journal(s, lambda j: status_text(j, bot.last_price, watched(j),
                                                                     status_extra(bot, s))),
            "/watchlist": lambda: with_journal(s, lambda j: watchlist_text(j.get("watchlist") or default_watchlist(s))),
            "/help": lambda: HELP_TEXT,
            "/start": lambda: HELP_TEXT,
        })

    limit = 1 if args.once else args.cycles
    n = 0
    seen_errors: dict = {}
    last_minutes = s.cycle_minutes
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            n += 1
            started = time.monotonic()
            try:
                with trade_lock:
                    summary = bot.run_cycle()
                    if (why := Lab(bot.base, bot.journal, client=None).watch_live()):
                        tg.send(f"⏪ Strategy rolled back to the previous champion: {why}. Review /strategies.")
            except Exception as e:  # keep the desk alive; the service would restart it anyway
                log.exception("Cycle %s failed", n)
                tg.send_once("cycle", f"⚠️ A trading cycle failed: {e}\nThe desk keeps running and retries.")
                try:
                    bot.journal.event("error", f"A trading cycle failed: {e}")
                except Exception:  # noqa: BLE001 - the journal itself may be the problem
                    pass
                summary = []
            heartbeat(bot, n, seconds=time.monotonic() - started)
            try:
                record_errors(bot, summary, seen_errors)
                maybe_pulse(bot, tg, s)
            except Exception:  # noqa: BLE001 - reporting must never stop trading
                log.exception("Health reporting failed")
            if not args.quiet:
                print_cycle(n, summary)
                sys.stdout.flush()
            for key, msg in trade_messages(summary):
                tg.send_once(key, msg) if key else tg.send(msg)
            if bot.scout_update and (bot.scout_update["added"] or bot.scout_update["removed"]):
                tg.send(scout_message(bot.scout_update))
            if bot.pace["minutes"] != last_minutes:
                tg.send_once("pace", pace_message(bot.pace), every_hours=1)
                bot.journal.event("pace", pace_message(bot.pace).removeprefix("⏱️ "))
                last_minutes = bot.pace["minutes"]
            for r in summary:  # risk limits pausing new buys deserve one clear message
                if r.get("risk", "").startswith(("daily loss limit", "overall loss limit")):
                    tg.send_once("risk-limit", f"🛑 {r['risk'][0].upper() + r['risk'][1:]}.\nOpen positions keep "
                                               "their stop-loss and take-profit; no new positions are opened.",
                                 every_hours=12)
                    break
            weekly_backup(bot, tg)
            maybe_weekly_lab(bot, tg, s, trade_lock, should_stop=stop.is_set)
            if bot.free_ai:
                for p in bot.free_ai.take_problems():
                    tg.send_once(f"free-ai-{p['kind']}", f"🆓 Free AI: {p['text']}", every_hours=12)
            if bot.llm and bot.llm.budget_hit:
                cap = bot.llm.effective_daily_budget()
                why = (f" That cap is {s.ai_pct_month():g}% a month of your ${s.starting_balance:,.0f} starting balance."
                       if bot.llm.pct_daily_budget and cap == bot.llm.pct_daily_budget else "")
                why += " Send /aibudget to see where it went or raise it."
                after = ("The free AI answers within its daily allowance, then the built-in rules, until 00:00 UTC."
                         if bot.free_ai else "The agents use the free built-in rules until 00:00 UTC.")
                tg.send_once("ai-budget", f"💸 Claude's daily budget (${cap:.2f}) is used up. {after}{why}", every_hours=12)
            maybe_daily_report(bot, tg, s)
            if limit and n >= limit:
                break
            if not args.quiet:
                print(f"Next check in {bot.pace['minutes']} min: {bot.pace['reason']} ({bot.pace['source']})")
                sys.stdout.flush()
            if not args.fast and stop.wait(bot.pace["minutes"] * 60):
                break
    except KeyboardInterrupt:
        print("\nStopped by user.")
    if stop.is_set():
        print("Stopping after finishing the current check (restart or update).")
    print_report(performance(bot.journal, bot.last_price))


def main() -> None:
    ap = argparse.ArgumentParser(prog="trader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--cycles", type=int, default=0, help="stop after N cycles (0 = forever)")
    run.add_argument("--offline", action="store_true", help="synthetic prices, no network")
    run.add_argument("--fast", action="store_true", help="no waiting between cycles")
    run.add_argument("--quiet", action="store_true")
    rep = sub.add_parser("report")
    rep.add_argument("--offline", action="store_true", help="report on the offline simulation journal")
    sub.add_parser("check")
    sub.add_parser("setup")
    rst = sub.add_parser("reset")
    rst.add_argument("--force", action="store_true", help="reset even with open positions")
    sub.add_parser("version")
    nts = sub.add_parser("notes", help="rebuild the Markdown notebook from the journal (data/notes)")
    nts.add_argument("--zip", action="store_true", help="also write a zip of it next to the folder")
    nts.add_argument("--offline", action="store_true", help="the offline simulation's journal")
    sub.add_parser("backtest", add_help=False)  # arguments handled by trader/backtest.py
    upd = sub.add_parser("update", help="fetch, test, install and restart (see trader/updater.py)")
    upd.add_argument("--check-only", action="store_true")
    upd.add_argument("--force", action="store_true")
    args, _ = ap.parse_known_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "setup":
        from .setup_wizard import run_wizard
        run_wizard()
        return
    if args.cmd == "version":
        from .updater import version_text
        print(version_text())
        return
    if args.cmd == "backtest":
        from .backtest import main as backtest_main
        sys.exit(backtest_main(sys.argv[2:]))
    if args.cmd == "update":
        from .updater import main as update_main
        sys.exit(update_main(sys.argv[2:]))
    s = Settings.load(offline=getattr(args, "offline", False))
    if args.cmd == "check":
        sys.exit(1 if cmd_check(s) else 0)
    if args.cmd == "report":
        def show(j):
            px = live_prices(s, j)
            perf = performance(j, px)
            print_report(perf)
            print(goals_text(goals(j, perf, px, trend=s.trend), perf))
        with_journal(s, show)
        return
    if args.cmd == "notes":
        def build(j):
            notes = Notes(j, default_dir(s, j.path), s)
            written = notes.refresh(live_prices(s, j), scorecard=j.get("scorecard_text") or "")
            if written is None:
                print("The notebook could not be written (see the warning above).")
                return
            print(f"Notebook rebuilt in {notes.dir} ({len(written)} files).\nStart with README.md; STRATEGY.md, TRADES.md, "
                  f"DIARY.md and PERFORMANCE.md hold the record; strategies/ has one file per strategy card.")
            if args.zip:
                print(f"Zip: {notes.bundle()}")
        with_journal(s, build)
        return
    if args.cmd == "reset":
        from .setup_wizard import archive_journal
        if not Path(s.db_path).exists():
            print("No journal yet - nothing to reset.")
            return
        open_positions = with_journal(s, lambda j: list(j.positions()))
        if open_positions and not args.force:
            raise SystemExit(f"Still holding {', '.join(open_positions)}, possibly with live stop orders on the "
                             "exchange. Sell first (or wait for the desk to), or run:  desk reset --force")
        print(f"Old journal saved as {archive_journal(Path(s.db_path)).name}. The next run starts fresh.")
        return
    cmd_run(s, args)


if __name__ == "__main__":
    main()
