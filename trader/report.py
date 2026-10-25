"""Performance report: answers "how is the agent doing?" in business terms."""
from __future__ import annotations

from .journal import Journal
from .notify import clean


def performance(j: Journal, prices: dict[str, float] | None = None) -> dict:
    prices = prices or {}
    orders = j.db.execute("SELECT ok, COUNT(*) n FROM orders GROUP BY ok").fetchall()
    ok = sum(r["n"] for r in orders if r["ok"])
    total_orders = sum(r["n"] for r in orders)
    gaps = [abs(r["avg_price"] / r["market_price"] - 1) * 100 for r in j.db.execute(
        "SELECT avg_price, market_price FROM orders WHERE ok=1 AND market_price > 0 AND avg_price > 0")]
    trades = [dict(r) for r in j.db.execute("SELECT * FROM closed_trades ORDER BY id")]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    positions = j.positions()
    unrealized = sum((prices.get(s, p["entry_price"]) * p["qty"]) - p["cost"] for s, p in positions.items())
    start = float(j.get("starting_balance"))
    equity = j.cash + sum(prices.get(s, p["entry_price"]) * p["qty"] for s, p in positions.items())

    curve = [r["equity"] for r in j.db.execute("SELECT equity FROM equity ORDER BY rowid")] + [equity]
    peak, max_dd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)

    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    decisions = j.db.execute("SELECT action, COUNT(*) n FROM decisions GROUP BY action").fetchall()
    overrides = j.db.execute("SELECT COUNT(*) FROM decisions WHERE source LIKE 'claude%' AND rules_action IS NOT NULL "
                             "AND action != rules_action").fetchone()[0]
    fees = j.db.execute("SELECT COALESCE(SUM(fee), 0) FROM orders WHERE ok=1").fetchone()[0]
    exchange_exits = sum(1 for t in trades if "(exchange)" in t["exit_reason"] or "(emulated)" in t["exit_reason"])
    return {
        "starting_balance": start,
        "equity": round(equity, 2),
        "return_pct": round((equity / start - 1) * 100, 2),
        "realized_pnl": round(sum(t["pnl"] for t in trades), 2),
        "unrealized_pnl": round(unrealized, 2),
        "orders_sent": total_orders,
        "orders_filled": ok,
        "execution_success_rate_pct": round(ok / total_orders * 100, 1) if total_orders else None,
        "avg_price_gap_pct": round(sum(gaps) / len(gaps), 3) if gaps else None,
        "closed_trades": len(trades),
        "exits_by_exchange_orders": exchange_exits,
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "open_positions": list(positions),
        "decisions": {r["action"]: r["n"] for r in decisions},
        "claude_overrode_rules": overrides,
        "fees_paid": round(fees, 2),
    }


EXPLAIN = {
    "execution_success_rate_pct": "orders the exchange confirmed as FILLED (technical reliability)",
    "avg_price_gap_pct": "how far demo fill prices were from the real market (small = demo results are realistic)",
    "exits_by_exchange_orders": "trades closed by the stop-loss / take-profit orders sitting on the exchange",
    "win_rate_pct": "closed trades that made money",
    "claude_overrode_rules": "decisions where Claude chose differently from the free rules (compare outcomes over time)",
    "fees_paid": "exchange fees so far - the strategy's biggest fixed cost",
    "profit_factor": "money won / money lost; above 1.0 = profitable overall, 1.5+ is good",
    "max_drawdown_pct": "worst drop from a peak - how painful the bad stretch was",
}


def print_report(p: dict) -> None:
    print("\n=========== DEMO PERFORMANCE REPORT ===========")
    for k, v in p.items():
        note = f"   <- {EXPLAIN[k]}" if k in EXPLAIN else ""
        print(f"{k:28s} {v}{note}")
    print("================================================\n")


def _pct(v) -> str:
    return "n/a" if v is None else f"{v}%"


def report_text(p: dict, title: str = "📊 Performance report") -> str:
    """Short, phone-friendly version of the report (used for Telegram)."""
    lines = [
        title,
        f"Balance: ${p['equity']:,.2f} ({p['return_pct']:+.2f}% vs ${p['starting_balance']:,.0f} start)",
        f"Profit/loss: closed ${p['realized_pnl']:+,.2f} · open ${p['unrealized_pnl']:+,.2f}",
        f"Orders filled: {p['orders_filled']}/{p['orders_sent']} ({_pct(p['execution_success_rate_pct'])})",
        f"Closed trades: {p['closed_trades']} · win rate {_pct(p['win_rate_pct'])}"
        + (f" · profit factor {p['profit_factor']}" if p["profit_factor"] is not None else ""),
        f"Worst drop from peak: {p['max_drawdown_pct']}%",
    ]
    if p["avg_price_gap_pct"] is not None:
        lines.append(f"Demo vs real price gap: {p['avg_price_gap_pct']}%")
    if p["open_positions"]:
        lines.append("Holding: " + ", ".join(p["open_positions"]))
    d = p["decisions"]
    lines.append(f"Decisions so far: {d.get('BUY', 0)} buy · {d.get('SELL', 0)} sell · {d.get('HOLD', 0)} hold")
    return "\n".join(lines)


def watchlist_text(wl: dict) -> str:
    """Which coins are watched, and why (Telegram /watchlist)."""
    if wl.get("ts"):
        how = "AI scout with web research" if wl.get("source") == "claude" else "market scan"
        lines = [f"🔭 Watchlist (updated {wl['ts'][:16].replace('T', ' ')} UTC by {how})"]
    else:
        lines = ["🔭 Watchlist (core coins from your settings)"]
    if wl.get("summary"):
        lines.append(clean(wl["summary"]))
    lines += [f"• {s.removesuffix('USDT')}: {clean(wl.get('reasons', {}).get(s, ''), 200)}" for s in wl["symbols"]]
    if wl.get("avoid"):
        lines.append("Avoiding: " + "; ".join(f"{k.removesuffix('USDT')} ({clean(v, 120)})" for k, v in wl["avoid"].items()))
    # research sources are deliberately not echoed: links in chat are a phishing channel
    return "\n".join(lines)


def status_text(j: Journal, prices: dict[str, float], symbols: list[str], extra: list[str] | None = None) -> str:
    """Open positions plus the latest decision (and its reason) for each coin."""
    lines = ["📌 Status", *(extra or []), f"Free cash: ${j.cash:,.2f}"]
    positions = j.positions()
    for sym, pos in positions.items():
        price = prices.get(sym, pos["entry_price"])
        pnl = price * pos["qty"] - pos["cost"]
        shield = {"exchange": "🛡️ stop & target live on Binance", "emulated": "🛡️ simulated exchange orders",
                  "partial": "⚠️ partly filled on Binance, desk watching the rest",
                  }.get(pos.get("protected"), "⚠️ desk-side checks only (exchange orders not placed yet)")
        lines.append(f"• {sym}: bought ${pos['entry_price']:,.4g}, now ${price:,.4g} ({pnl:+,.2f} USDT) "
                     f"· stop ${pos['stop_loss']:,.4g} · target ${pos['take_profit']:,.4g} · {shield}")
    if not positions:
        lines.append("No open positions.")
    lines.append("")
    lines.append("Latest decisions:")
    found = False
    for sym in symbols:
        r = j.db.execute("SELECT ts, action, confidence, reasoning, risk_reason FROM decisions WHERE symbol=? "
                         "ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
        if r:
            found = True
            lines.append(f"• {sym} {r['action']} ({r['confidence']:.0%}) at {r['ts'][11:16]} UTC: "
                         f"{clean(r['reasoning'], 300)} [risk: {r['risk_reason']}]")
    if not found:
        lines.append("None yet - the first check is still running. Try again in a minute.")
    return "\n".join(lines)
