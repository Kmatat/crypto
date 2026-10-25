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
from .agents.research import ResearchAgent
from .scorecard import claude_scorecard, news_scorecard, scorecard_text

log = logging.getLogger("trader")

HELP_TEXT = (
    "🤖 Crypto trading desk commands\n"
    "/status - open positions and each coin's latest decision with the reason\n"
    "/report - performance so far (profit, win rate, order success)\n"
    "/watchlist - which coins the desk watches and why\n"
    "/strategies - the strategy in use, challengers under test, and what you can approve\n"
    "/approve <id> or /reject <id> - decide on a challenger that passed all tests\n"
    "/lab - run the weekly strategy lab now (takes a few minutes)\n"
    "/version - which code version is running, and whether a newer one exists\n"
    "/update - install the latest code now (it is tested first; the desk restarts itself)\n"
    "/help - this list\n\n"
    "You also get a message for every buy, sell or failed order, when the coin list or the check pace "
    "changes, and a daily report."
)


def print_cycle(n: int, summary: list[dict]) -> None:
    print(f"\n--- cycle {n} ({dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC) ---")
    for r in summary:
        if "error" in r:
            print(f"{r['symbol']:9s} ERROR {r['error']}")
            continue
        line = f"{r['symbol']:9s} {r['action']:4s}{'*' if str(r.get('source', '')).startswith('claude') else ' '}"
        if "confidence" in r:
            px = f"{r['price']:,.2f}" if r['price'] >= 1 else f"{r['price']:.6g}"
            line += f" conf={r['confidence']:.2f} score={r['score']:+.2f} price={px}  risk: {r['risk']}"
        if "reason" in r:
            line += f" ({r['reason']})"
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
    return scorecard_text(claude, news, float(j.get("ai_spend_total") or 0))


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
    except (sqlite3.Error, OSError) as e:
        log.warning("Journal backup failed: %s", e)
    finally:
        target.unlink(missing_ok=True)


def run_lab(bot: Orchestrator, tg: Telegram, trade_lock: threading.Lock, reason: str) -> str:
    """Weekly research + exam + forward tests. Holds the trade lock: never overlaps a trading decision."""
    with trade_lock:
        lab = Lab(bot.base, bot.journal, client=bot.market.client)
        research = ResearchAgent(bot.llm)
        lessons = research.lessons(bot.journal)
        bot.journal.set("lessons", lessons)
        tried = {c["card"]["id"] for c in lab.challengers()}
        proposals = research.propose(lab.champion(), lessons, bot.base.research_candidates, tried)
        result = lab.run_week(proposals)
        text = week_report(result) + f"\n({reason})"
        if lessons.get("notes"):
            text += "\nLessons from the last 30 days: " + "; ".join(lessons["notes"])
        ideas = getattr(research, "ideas_needing_code", None)
        if ideas:
            text += "\nIdeas that would need new code (for the developer): " + "; ".join(ideas[:3])
        tg.send(text)
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
        tg.send_once("lab", f"⚠️ The weekly strategy lab failed: {e}. The champion strategy keeps trading.")


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
    print(f"Mode: {mode}  AI: {ai}  checks: {pace}\nCoins: {coins}  journal: {s.db_path}")

    if tg.enabled:
        if bot.journal.get("last_daily_report") is None:  # first start: first daily report tomorrow
            bot.journal.set("last_daily_report", dt.datetime.now(dt.timezone.utc).date().isoformat())
        tg.send(f"🚀 Trading desk started\nMode: {mode}\nAI: {ai}\nCoins: {coins}\n"
                f"Budget: ${s.starting_balance:,.0f} · checks {pace}\n\n{HELP_TEXT}")

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

        tg.listen({
            "/update": update_now,
            "/version": version_text,
            "/strategies": lab_status,
            "/approve": approve,
            "/reject": reject,
            "/lab": lab_now,
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
                summary = []
            if not args.quiet:
                print_cycle(n, summary)
                sys.stdout.flush()
            for key, msg in trade_messages(summary):
                tg.send_once(key, msg) if key else tg.send(msg)
            if bot.scout_update and (bot.scout_update["added"] or bot.scout_update["removed"]):
                tg.send(scout_message(bot.scout_update))
            if bot.pace["minutes"] != last_minutes:
                tg.send_once("pace", pace_message(bot.pace), every_hours=1)
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
                tg.send_once("ai-budget", f"💸 Claude's daily budget (${s.ai_daily_budget_usd:.2f}) is used up. "
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
