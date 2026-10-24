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
import logging
import sys
import time

from .config import PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .journal import Journal
from .notify import Telegram, trade_messages
from .orchestrator import Orchestrator
from .report import performance, print_report, report_text, status_text

log = logging.getLogger("trader")

HELP_TEXT = (
    "🤖 Crypto trading desk commands\n"
    "/status - open positions and each coin's latest decision with the reason\n"
    "/report - performance so far (profit, win rate, order success)\n"
    "/help - this list\n\n"
    "You also get a message for every buy, sell or failed order, and a daily report."
)


def print_cycle(n: int, summary: list[dict]) -> None:
    print(f"\n--- cycle {n} ({dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC) ---")
    for r in summary:
        if "error" in r:
            print(f"{r['symbol']:9s} ERROR {r['error']}")
            continue
        line = f"{r['symbol']:9s} {r['action']:4s}"
        if "confidence" in r:
            line += f" conf={r['confidence']:.2f} score={r['score']:+.2f} price={r['price']:.2f}  risk: {r['risk']}"
        if "reason" in r:
            line += f" ({r['reason']})"
        if o := r.get("order"):
            line += f"  -> {'FILLED' if o['ok'] else 'FAILED'} " + (
                f"{o['qty']:.6f} @ {o['avg_price']:.2f}" if o["ok"] else o["error"])
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
    step("News feeds", lambda: f"{len(NewsAgent().fetch_headlines())} headlines")
    step("Forex (ECB rates)", lambda: ForexAgent().run()["regime"])
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
    """Current real prices for open positions, so the report values them correctly."""
    from .binance_client import BinanceClient
    prices = {}
    if not s.offline:
        for sym in j.positions():
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
    if tg.send(report_text(performance(bot.journal, bot.last_price), title=f"☀️ Daily report {today}")):
        bot.journal.set("last_daily_report", today)


def cmd_run(s: Settings, args) -> None:
    bot = Orchestrator(s)
    tg = Telegram(s.telegram_token, s.telegram_chat_id) if not s.offline else Telegram("")
    mode = "Binance testnet demo account (fake money)" if s.mode == "testnet" and not s.offline else \
        "offline test (made-up prices)" if s.offline else "paper trading at real prices"
    ai = f"Claude ({s.claude_model})" if s.use_claude else "built-in rules"
    print(f"Mode: {mode}  AI: {ai}  symbols: {','.join(s.symbols)}  journal: {s.db_path}")

    if tg.enabled:
        if bot.journal.get("last_daily_report") is None:  # first start: first daily report tomorrow
            bot.journal.set("last_daily_report", dt.datetime.now(dt.timezone.utc).date().isoformat())
        tg.send(f"🚀 Trading desk started\nMode: {mode}\nAI: {ai}\nCoins: {', '.join(s.symbols)}\n"
                f"Budget: ${s.starting_balance:,.0f} · checks every {s.cycle_minutes} min\n\n{HELP_TEXT}")
        tg.listen({
            "/report": lambda: with_journal(s, lambda j: report_text(performance(j, bot.last_price))),
            "/status": lambda: with_journal(s, lambda j: status_text(j, bot.last_price, s.symbols)),
            "/help": lambda: HELP_TEXT,
            "/start": lambda: HELP_TEXT,
        })

    limit = 1 if args.once else args.cycles
    n = 0
    try:
        while True:
            n += 1
            try:
                summary = bot.run_cycle()
            except Exception as e:  # keep the desk alive; the service would restart it anyway
                log.exception("Cycle %s failed", n)
                tg.send_once("cycle", f"⚠️ A trading cycle failed: {e}\nThe desk keeps running and retries.")
                summary = []
            if not args.quiet:
                print_cycle(n, summary)
                sys.stdout.flush()
            for key, msg in trade_messages(summary):
                tg.send_once(key, msg) if key else tg.send(msg)
            maybe_daily_report(bot, tg, s)
            if limit and n >= limit:
                break
            if not args.fast:
                time.sleep(s.cycle_minutes * 60)
    except KeyboardInterrupt:
        print("\nStopped by user.")
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
    sub.add_parser("reset")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "setup":
        from .setup_wizard import run_wizard
        run_wizard()
        return
    s = Settings.load(offline=getattr(args, "offline", False))
    if args.cmd == "check":
        sys.exit(1 if cmd_check(s) else 0)
    if args.cmd == "report":
        print_report(with_journal(s, lambda j: performance(j, live_prices(s, j))))
        return
    if args.cmd == "reset":
        from pathlib import Path

        from .setup_wizard import archive_journal
        if Path(s.db_path).exists():
            print(f"Old journal saved as {archive_journal(Path(s.db_path)).name}. The next run starts fresh.")
        else:
            print("No journal yet - nothing to reset.")
        return
    cmd_run(s, args)


if __name__ == "__main__":
    main()
