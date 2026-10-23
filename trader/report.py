"""Performance report: answers "how is the agent doing?" in business terms."""
from __future__ import annotations

from .journal import Journal


def performance(j: Journal, prices: dict[str, float] | None = None) -> dict:
    prices = prices or {}
    orders = j.db.execute("SELECT ok, COUNT(*) n FROM orders GROUP BY ok").fetchall()
    ok = sum(r["n"] for r in orders if r["ok"])
    total_orders = sum(r["n"] for r in orders)
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
    return {
        "starting_balance": start,
        "equity": round(equity, 2),
        "return_pct": round((equity / start - 1) * 100, 2),
        "realized_pnl": round(sum(t["pnl"] for t in trades), 2),
        "unrealized_pnl": round(unrealized, 2),
        "orders_sent": total_orders,
        "orders_filled": ok,
        "execution_success_rate_pct": round(ok / total_orders * 100, 1) if total_orders else None,
        "closed_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "open_positions": list(positions),
        "decisions": {r["action"]: r["n"] for r in decisions},
    }


EXPLAIN = {
    "execution_success_rate_pct": "orders the exchange confirmed as FILLED (technical reliability)",
    "win_rate_pct": "closed trades that made money",
    "profit_factor": "money won / money lost; above 1.0 = profitable overall, 1.5+ is good",
    "max_drawdown_pct": "worst drop from a peak - how painful the bad stretch was",
}


def print_report(p: dict) -> None:
    print("\n=========== DEMO PERFORMANCE REPORT ===========")
    for k, v in p.items():
        note = f"   <- {EXPLAIN[k]}" if k in EXPLAIN else ""
        print(f"{k:28s} {v}{note}")
    print("================================================\n")
