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
import datetime as dt
import fcntl
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .config import PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .journal import Journal
from .notify import Telegram, pace_message, scout_message, trade_messages
from .orchestrator import Orchestrator
from .report import goals, goals_text, performance, print_report, report_text, status_text, watchlist_text
from .lab import Lab, status_text as lab_status_text, week_report
from .agents.head import HeadTraderAgent
from .agents.research import ResearchAgent, review_text
from .calibration import study_text
from .notes import Notes, default_dir, lineage_text
from .notify import clean
from .scorecard import claude_scorecard, news_scorecard, scorecard_text
from .updater import DESK_STARTED, HEARTBEAT, read_status, revision, version_string
from .health import agents_text, funnel, health_text, judged_since, pulse_text, stuck_rule, write_json

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
        line = f"{r['symbol']:9s} {r.get('action', ''):4s}{'*' if str(r.get('source', '')).startswith('claude') else ' '}"
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
        print("[INFO] Claude: not configured - agents use built-in rules")
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
    text += "\n\n" + goals_text(goals(bot.journal, perf, bot.last_price), perf)
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
        news = news_scorecard(j, candles_after)
    except Exception as e:
        log.warning("Scorecard failed: %s", e)
        return ""
    if not claude["cases"] and not news["judged"]:
        return ""
    text = scorecard_text(claude, news, float(j.get("ai_spend_total") or 0))
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


def run_lab(bot: Orchestrator, tg: Telegram, trade_lock: threading.Lock, reason: str) -> str:
    """Weekly research + exam + forward tests. Holds the trade lock: never overlaps a trading decision."""
    with trade_lock:
        heartbeat(bot, note="weekly strategy lab running (no market checks until it finishes)")
        lab = Lab(bot.base, bot.journal, client=bot.market.client)
        research = ResearchAgent(bot.llm)
        lessons = research.lessons(bot.journal)
        bot.journal.set("lessons", lessons)
        if lessons.get("notes"):
            bot.journal.event("lesson", "; ".join(lessons["notes"]))
        tried = {c["card"]["id"] for c in lab.challengers()}
        study_result, study_card = None, None
        if bot.head:
            try:  # the head trader's threshold study becomes a challenger like any other proposal
                coins = list(dict.fromkeys(list(bot.base.symbols) + bot.watchlist()["symbols"]))
                study_result, study_card = bot.head.study(lab, coins)
            except Exception as e:  # noqa: BLE001 - the lab must still run without the study
                log.exception("Threshold study failed")
                bot.journal.event("error", f"Threshold study failed: {e}")
        knowledge = lineage_text(bot.journal) + ("\n" + bot.head.playbook() if bot.head else "")
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
        perf_text = report_text(perf, title="Performance") + "\n" + goals_text(goals(bot.journal, perf, bot.last_price), perf)
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
                                         ("Playbook", bot.head.playbook() if bot.head else ""),
                                         ("Claude's review", review or "(Claude not configured, or unavailable this week)"),
                                         ("Performance", "```\n" + perf_text + "\n```"),
                                         ("Claude versus the rules", "```\n" + scorecard + "\n```" if scorecard else "")])
        bot.notes.refresh(bot.last_price, scorecard=scorecard)
        return text


def maybe_weekly_lab(bot: Orchestrator, tg: Telegram, s: Settings, trade_lock: threading.Lock) -> None:
    if not s.lab_enabled or s.offline:
        return
    now = dt.datetime.now(dt.timezone.utc)
    week = now.strftime("%G-W%V")
    if now.weekday() != s.lab_weekday or now.hour < s.daily_report_hour_utc or bot.journal.get("lab_week") == week:
        return
    bot.journal.set("lab_week", week)
    try:
        run_lab(bot, tg, trade_lock, "scheduled weekly run")
    except Exception as e:
        log.exception("Weekly lab failed")
        bot.journal.event("error", f"Weekly lab failed: {e}")
        tg.send_once("lab", f"⚠️ The weekly strategy lab failed: {e}. The champion strategy keeps trading.")


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


def heartbeat(bot: Orchestrator, cycle: int = 0, note: str = "") -> None:
    """After every check: the time, for /health, the pulse and the update job's watchdog (a separate process
    that notices when the desk has stopped checking the market)."""
    beat = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "cycle": cycle,
            "next_minutes": bot.pace["minutes"], "version": version_string(), "note": note}
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
    tg = Telegram(s.telegram_token, s.telegram_chat_id) if not s.offline else Telegram("")
    mode = "Binance testnet demo account (fake money)" if s.mode == "testnet" and not s.offline else \
        "offline test (made-up prices)" if s.offline else "paper trading at real prices"
    ai = f"Claude ({s.claude_model})" if s.use_claude else "built-in rules"
    pace = f"every {s.cycle_minutes} min" + (
        f" (the AI may adjust it between {s.min_check_minutes} and {s.max_check_minutes})" if s.use_claude else "")
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
            if not s.lab_enabled:
                return "The strategy lab is switched off (LAB_ENABLED=false)."
            threading.Thread(target=lambda: run_lab(bot, tg, trade_lock, "requested on Telegram"), daemon=True).start()
            return "🔬 Lab started: downloading history and testing challengers. Results arrive here in a few minutes."

        def playbook_now() -> str:
            if not bot.head:
                return "The head trader is switched off (HEAD_TRADER=false)."
            text = with_journal(s, lambda j: HeadTraderAgent(bot.base, j, bot.llm).playbook())
            plan = bot.head.last_plan
            budget = ""
            if plan:
                budget = (f"\n\nClaude budget at the last check: asked for {', '.join(plan['allowed']) or 'nobody'}"
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
            "/review": review_now,
            "/report": lambda: with_journal(s, lambda j: report_text(performance(j, bot.last_price)) + "\n\n"
                                            + goals_text(goals(j, performance(j, bot.last_price), bot.last_price),
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
    stop = threading.Event()  # a restart (update, reboot) waits for the current check to finish
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            n += 1
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
            heartbeat(bot, n)
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
            maybe_weekly_lab(bot, tg, s, trade_lock)
            if bot.llm and bot.llm.budget_hit:
                tg.send_once("ai-budget", f"💸 Claude's daily budget (${bot.llm.effective_daily_budget():.2f}) is used up. "
                                          "The agents use the free built-in rules until 00:00 UTC.", every_hours=12)
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
            print(goals_text(goals(j, perf, px), perf))
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
