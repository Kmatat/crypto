"""Command line entry point.

  python -m trader check              # verify connections (Binance testnet, news, forex, Claude)
  python -m trader run --once         # run one trading cycle
  python -m trader run                # run forever, one cycle every CYCLE_MINUTES
  python -m trader run --offline --cycles 200 --fast   # offline simulation with synthetic prices
  python -m trader report [--offline] # performance report
"""
from __future__ import annotations

import argparse
import logging
import time

from .config import PUBLIC_BASE_URL, TESTNET_BASE_URL, Settings
from .orchestrator import Orchestrator
from .report import performance, print_report


def print_cycle(n: int, summary: list[dict]) -> None:
    print(f"\n--- cycle {n} ---")
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


def cmd_check(s: Settings) -> None:
    from .agents.forex import ForexAgent
    from .agents.news import NewsAgent
    from .binance_client import BinanceClient
    from .llm import ClaudeJSON

    def step(name, fn):
        try:
            print(f"[ OK ] {name}: {fn()}")
        except Exception as e:
            print(f"[FAIL] {name}: {e}")

    print(f"Mode: {s.mode}")
    base = TESTNET_BASE_URL if s.mode == "testnet" else PUBLIC_BASE_URL
    c = BinanceClient(base, s.api_key, s.api_secret)
    step(f"Binance prices ({base})", lambda: f"BTCUSDT = {c.price('BTCUSDT')}")
    if s.mode == "testnet":
        step("Binance testnet account", lambda: {k: v for k, v in c.balances().items() if k in ("USDT", "BTC", "ETH", "SOL")})
    step("News feeds", lambda: f"{len(NewsAgent().fetch_headlines())} headlines")
    step("Forex (ECB rates)", lambda: ForexAgent().run())
    if s.anthropic_key:
        step("Claude", lambda: ClaudeJSON(s.claude_model, s.anthropic_key).ask(
            "Reply with JSON.", "Say ok.", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                            "required": ["ok"], "additionalProperties": False}, effort="low"))
    else:
        print("[INFO] Claude: no ANTHROPIC_API_KEY - agents will use rule-based logic")


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
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    s = Settings.load(offline=getattr(args, "offline", False))
    if args.cmd == "check":
        return cmd_check(s)
    if args.cmd == "report":
        from .journal import Journal
        return print_report(performance(Journal(s.db_path, s.starting_balance)))

    bot = Orchestrator(s)
    print(f"Mode={s.mode}{' (offline)' if s.offline else ''}  AI={'Claude' if s.use_claude else 'rules'}  "
          f"symbols={','.join(s.symbols)}  journal={s.db_path}")
    limit = 1 if args.once else args.cycles
    n = 0
    try:
        while True:
            n += 1
            summary = bot.run_cycle()
            if not args.quiet:
                print_cycle(n, summary)
            if limit and n >= limit:
                break
            if not args.fast:
                time.sleep(s.cycle_minutes * 60)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    print_report(performance(bot.journal, bot.last_price))


if __name__ == "__main__":
    main()
