"""Is the desk working? Health, the decision funnel, the agent roster and strategy changes, for Telegram.

A quiet desk and a broken desk look the same from a phone: no messages. This module answers the owner's
three questions without a terminal:
  /health   is everything running: version, last market check, errors, settings problems, updates
  /agents   how many agents are working, what each did, which strategies were created, examined,
            promoted, rejected or rolled back, and which coins were added or removed
  the pulse a short message every PULSE_HOURS saying what the desk did, so silence always means a problem

The decision funnel is the most useful part: of the buy signals the rules produced, how many were refused,
by which rule. A single rule refusing every signal for a day is reported by itself: that is how a broken
setting (for example a take-profit that can never pass the reward rule) shows up within hours.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .journal import Journal
from .notify import clean
from .strategy import StrategyCard, thresholds_line

CHECKLIST = "master traders' checklist:"
STUCK_MIN_SIGNALS = 20


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def since_iso(hours: float) -> str:
    return (now_utc() - dt.timedelta(hours=hours)).isoformat(timespec="seconds")


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        minutes = (now_utc() - dt.datetime.fromisoformat(iso)).total_seconds() / 60
    except ValueError:
        return iso
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h ago"
    return f"{minutes / 1440:.0f} days ago"


# ---------------------------------------------------------------- small files next to the journal
def data_dir(db_path: str) -> Path | None:
    return None if db_path == ":memory:" else Path(db_path).parent


def write_json(path: Path | None, data: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass  # a status file must never stop trading


def read_json(path: Path | None) -> dict | None:
    try:
        return json.loads(path.read_text()) if path else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the decision funnel
def _refusal(reason: str) -> list[str]:
    """'master traders' checklist: reward, cooldown not met' -> ['checklist: reward', 'checklist: cooldown']."""
    reason = reason or ""
    if reason.startswith(CHECKLIST):
        names = reason[len(CHECKLIST):].replace(" not met", "").split(",")
        return [f"checklist: {n.strip()}" for n in names if n.strip()]
    for key, label in (("already", "all position slots in use"), ("daily loss limit", "daily loss limit"),
                       ("overall loss limit", "overall loss brake"), ("not enough free cash", "not enough cash"),
                       ("confidence", "confidence too low"), ("volatility", "volatility too high")):
        if reason.startswith(key):
            return [label]
    return [reason.split("(")[0].split(":")[0].strip() or "other"]


def funnel(j: Journal, since: str) -> dict:
    """What happened to the rules' buy signals since `since`, and how many checks ran."""
    rows = j.db.execute("SELECT action, rules_action, risk_approved, risk_reason, source, reasoning FROM decisions "
                        "WHERE ts >= ?", (since,)).fetchall()
    checks = j.db.execute("SELECT COUNT(*) FROM equity WHERE ts >= ?", (since,)).fetchone()[0]
    out = {"checks": checks, "decisions": len(rows), "buy_signals": 0, "approved": 0, "refused": {}, "claude_vetoes": 0,
           "news_vetoes": 0, "sell_signals": 0, "sells": 0}
    for r in rows:
        if r["rules_action"] == "SELL":
            out["sell_signals"] += 1
            out["sells"] += r["action"] == "SELL" and bool(r["risk_approved"])
        if r["rules_action"] != "BUY":
            continue
        out["buy_signals"] += 1
        if r["action"] == "BUY":
            if r["risk_approved"]:
                out["approved"] += 1
            else:
                for label in _refusal(r["risk_reason"]):
                    out["refused"][label] = out["refused"].get(label, 0) + 1
        elif (r["reasoning"] or "").startswith("Clearly negative news"):
            out["news_vetoes"] += 1
        elif str(r["source"] or "").startswith("claude"):
            out["claude_vetoes"] += 1
    orders = j.db.execute("SELECT side, ok, COUNT(*) n FROM orders WHERE ts >= ? GROUP BY side, ok", (since,)).fetchall()
    out["orders"] = {f"{o['side']}:{'filled' if o['ok'] else 'failed'}": o["n"] for o in orders}
    out["closed_trades"] = j.db.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM closed_trades WHERE closed_at >= ?",
                                        (since,)).fetchone()[:]
    return out


def funnel_text(f: dict) -> str:
    lines = [f"Checks: {f['checks']} · decisions: {f['decisions']} · buy signals from the rules: {f['buy_signals']}"]
    if f["buy_signals"]:
        parts = [f"{f['approved']} approved"]
        if f["claude_vetoes"]:
            parts.append(f"{f['claude_vetoes']} vetoed by Claude")
        if f["news_vetoes"]:
            parts.append(f"{f['news_vetoes']} blocked by bad news")
        refused = sorted(f["refused"].items(), key=lambda x: -x[1])
        if refused:
            parts.append("refused: " + ", ".join(f"{k} {n}" for k, n in refused[:5]))
        lines.append("Of those: " + "; ".join(parts))
    filled = f["orders"].get("BUY:filled", 0) + f["orders"].get("SELL:filled", 0)
    failed = f["orders"].get("BUY:failed", 0) + f["orders"].get("SELL:failed", 0)
    n, pnl = f["closed_trades"]
    lines.append(f"Orders filled {filled}" + (f", failed {failed}" if failed else "") + f" · closed trades {n}"
                 + (f" ({pnl:+,.2f} USDT)" if n else ""))
    return "\n".join(lines)


def stuck_rule(f: dict) -> tuple[str, int] | None:
    """A single refusal reason that blocked every buy signal (with enough signals to mean something)."""
    if f["buy_signals"] < STUCK_MIN_SIGNALS or f["approved"]:
        return None
    for label, n in f["refused"].items():
        if n >= f["buy_signals"]:
            return label, n
    return None


# ---------------------------------------------------------------- strategies created, examined, promoted, removed
def strategy_changes(j: Journal, since: str) -> dict:
    items = j.get("strategy_challengers") or []
    hist = [h for h in (j.get("strategy_history") or []) if h.get("ts", "") >= since]
    created = [c for c in items if c.get("proposed_at", "") >= since]
    by_source: dict[str, int] = {}
    for c in created:
        src = (c["card"].get("source") or "proposal").split(":")[0]
        by_source[src] = by_source.get(src, 0) + 1

    def examined(c, key):
        return (c.get(key) or {}).get("evaluated_at" if key == "lab" else "tested_at", "") >= since
    out = {
        "created": len(created), "by_source": by_source,
        "refused_at_entry": sum(1 for c in created if c["status"] == "rejected" and c.get("notes")
                                and c["notes"][0] != "rejected by the owner"),
        "exam_passed": sum(1 for c in items if examined(c, "lab") and c["lab"].get("verdict", {}).get("passed")),
        "exam_failed": sum(1 for c in items if examined(c, "lab") and not c["lab"].get("verdict", {}).get("passed")),
        "forward_passed": sum(1 for c in items if examined(c, "forward") and c["forward"].get("passed")),
        "forward_failed": sum(1 for c in items if examined(c, "forward") and c["forward"].get("passed") is False),
        "promoted": [h for h in hist if h["event"] == "promote"],
        "rolled_back": [h for h in hist if h["event"] == "rollback"],
        "rejected_by_owner": sum(1 for e in j.events(since=since) if e["kind"] == "owner" and "rejected" in e["text"]),
        "in_forward_test": [c for c in items if c["status"] == "lab_passed"],
        "awaiting_approval": [c for c in items if c["status"] == "ready"],
        "total_known": len(items),
    }
    added, removed = [], []
    for e in j.events(since=since):
        if e["kind"] == "watchlist" and e.get("data"):
            added += e["data"].get("added", [])
            removed += e["data"].get("removed", [])
    out["coins_added"], out["coins_removed"] = added, removed
    return out


def strategy_changes_text(c: dict, days: float) -> str:
    src = ", ".join(f"{n} {k}" for k, n in sorted(c["by_source"].items())) or "none"
    lines = [f"🧪 Strategies (last {days:g} days)",
             f"Created: {c['created']} ({src})" + (f", {c['refused_at_entry']} refused before testing (out of bounds)"
                                                   if c["refused_at_entry"] else ""),
             f"Exam: {c['exam_passed']} passed, {c['exam_failed']} failed · forward test: {c['forward_passed']} passed, "
             f"{c['forward_failed']} failed",
             f"Removed: {c['rejected_by_owner']} rejected by you, {c['exam_failed'] + c['forward_failed']} retired after "
             "failing a test",
             f"In forward test now: {len(c['in_forward_test'])} · waiting for your /approve: {len(c['awaiting_approval'])}"]
    for h in c["promoted"]:
        lines.append(f"🏆 {h['ts'][:10]}: {h.get('name')} [{h.get('id')}] became the strategy in force ({h.get('by')}"
                     + (f": {clean(h['note'], 160)}" if h.get("note") else "") + ")")
    for h in c["rolled_back"]:
        lines.append(f"⏪ {h['ts'][:10]}: rolled back to {h.get('name')} [{h.get('id')}]: {clean(h.get('note'), 160)}")
    if not c["promoted"] and not c["rolled_back"]:
        lines.append("Strategy in force: unchanged in this period")
    if c["coins_added"] or c["coins_removed"]:
        lines.append("Coins added: " + (", ".join(x.removesuffix("USDT") for x in c["coins_added"]) or "none")
                     + " · removed: " + (", ".join(x.removesuffix("USDT") for x in c["coins_removed"]) or "none"))
    return "\n".join(lines)


# ---------------------------------------------------------------- the agent roster
def roster(bot, j: Journal, hours: float = 24) -> list[dict]:
    """One row per agent: name, status (working | idle | off | failing), what it did."""
    s = bot.s
    since = since_iso(hours)
    f = funnel(j, since)
    beat = j.get("heartbeat") or {}
    pace = (bot.pace or {}).get("minutes", s.cycle_minutes)
    fresh = bool(beat.get("ts")) and (now_utc() - dt.datetime.fromisoformat(beat["ts"])).total_seconds() < (2 * pace + 15) * 60
    errors = [e for e in j.events(since=since) if e["kind"] == "error"]

    def err(prefix: str) -> str | None:
        hit = [e for e in errors if e["text"].lower().startswith(prefix)]
        return hit[-1]["text"] if hit else None

    def row(name, role, status, detail, error=None):
        return {"name": name, "role": role, "status": "failing" if error else status,
                "detail": clean(error, 160) if error else detail}
    rows = []
    coins = len(bot.last_price)
    rows.append(row("Market data", "real Binance prices for every watched coin", "working" if fresh else "idle",
                    f"{coins} coins, last check {ago(beat.get('ts'))}", err("market data")))
    wl = j.get("watchlist") or {}
    rows.append(row("Market scout", "finds liquid coins worth watching", "working" if bot.scout else "off",
                    (f"{len(wl.get('symbols', []))} coins, refreshed {ago(wl.get('ts'))} by "
                     + ("AI research" if wl.get("source") == "claude" else "market scan")) if bot.scout and wl.get("ts")
                    else ("waiting for its first run" if bot.scout else "AUTO_COINS=false"), err("scout")))
    cache = getattr(bot.news, "_cache", None)
    news_ts = dt.datetime.fromtimestamp(cache[0], dt.timezone.utc).isoformat(timespec="seconds") if cache else None
    mood = (cache[1].get("_mood") if cache else None) or "no headlines read yet"
    rows.append(row("News", "headlines scored per coin" + (" by Claude" if bot.llm else " by keywords"),
                    "working" if cache else "idle", f"read {ago(news_ts)}: {clean(mood, 100)}", err("news")))
    fx = getattr(bot.forex, "_cache", None)
    rows.append(row("Forex", "US dollar strength (ECB rates)", "working" if fx else "idle",
                    f"regime {fx[1].get('regime')}" if fx else "not read yet", err("forex")))
    rows.append(row("Technical analysis", "trend, momentum, RSI, ATR per coin", "working" if fresh else "idle",
                    f"{coins} coins each check"))
    rows.append(row("Prediction", "statistical next-hours forecast per coin", "working" if fresh else "idle",
                    f"{coins} coins each check"))
    checks = list(s.mentor_checks)
    refused = sum(n for k, n in f["refused"].items() if k.startswith("checklist"))
    rows.append(row("Mentor (master traders' checklist)", "refuses buys that break documented trading rules",
                    "working" if checks else "off",
                    f"rules {', '.join(checks)}; refused {refused} of {f['buy_signals']} buy signals" if checks
                    else "MENTOR_CHECKS=none"))
    head = getattr(bot, "head", None)
    plan = getattr(head, "last_plan", {}) if head else {}
    rows.append(row("Head trader", "spends the AI budget where it changes a decision, tilts thresholds, runs the study",
                    "working" if head else "off",
                    (f"last check: Claude for {', '.join(plan.get('allowed') or []) or 'nobody'}; {plan.get('why', 'no plan yet')}"
                     if head else "HEAD_TRADER=false")))
    claude_calls = j.db.execute("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND source = 'claude'", (since,)).fetchone()[0]
    rows.append(row("Decision (portfolio manager)", "BUY / SELL / HOLD from all reports", "working" if f["decisions"] else "idle",
                    f"{f['decisions']} decisions, {claude_calls} with a fresh Claude opinion", err("decision")))
    rows.append(row("Risk manager", "position size, limits, loss brakes", "working" if f["decisions"] else "idle",
                    f"approved {f['approved']} buys; " + (", ".join(f"{k} {n}" for k, n in f["refused"].items()
                                                                   if not k.startswith("checklist")) or "no limit refusals")))
    filled = f["orders"].get("BUY:filled", 0) + f["orders"].get("SELL:filled", 0)
    failed = f["orders"].get("BUY:failed", 0) + f["orders"].get("SELL:failed", 0)
    protected = sum(1 for p in j.positions().values() if p.get("protected") in ("exchange", "emulated"))
    rows.append(row("Execution", "orders on Binance, stop-loss and take-profit on the exchange",
                    "working" if filled or failed else "idle",
                    f"{filled} orders filled, {failed} failed; {protected}/{len(j.positions())} positions protected",
                    err("buy ") or err("sell ")))
    last_lab = j.get("lab_last_run")
    rows.append(row("Research", "proposes strategy changes (web research with Claude, or variations)",
                    "working" if last_lab else "idle", f"last proposals {ago(last_lab)}"))
    rows.append(row("Laboratory", "examines, forward-tests, promotes and rolls back strategies",
                    "working" if s.lab_enabled else "off",
                    f"last run {ago(last_lab)}; {sum(1 for c in (j.get('strategy_challengers') or []) if c['status'] in ('lab_passed', 'ready'))} "
                    "challenger(s) still in test" if s.lab_enabled else "LAB_ENABLED=false", err("threshold study") or err("weekly lab")))
    cases = len(j.get("scorecard_cases") or {})
    rows.append(row("Scorecard", "measures Claude and the news against real later prices", "working" if cases else "idle",
                    f"{cases} Claude decisions judged so far"))
    return rows


def roster_text(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    marks = {"working": "🟢", "idle": "⚪", "off": "⚫", "failing": "🔴"}
    head = (f"🤖 {len(rows)} agents: {counts.get('working', 0)} working, {counts.get('idle', 0)} idle, "
            f"{counts.get('off', 0)} switched off, {counts.get('failing', 0)} failing")
    return "\n".join([head] + [f"{marks[r['status']]} {r['name']}: {r['detail']}" for r in rows])


def agents_text(bot, j: Journal, days: float = 7) -> str:
    state = j.get("strategy_champion") or {}
    champ = StrategyCard.from_dict(state["card"]) if state else None
    parts = [roster_text(roster(bot, j, hours=24))]
    if champ:
        parts.append(f"🏆 Strategy in force: {champ.name} [{champ.id}] since {state.get('since', '')[:10]} "
                     f"({state.get('by')}); buy thresholds {thresholds_line(champ)}")
    parts.append(strategy_changes_text(strategy_changes(j, since_iso(days * 24)), days))
    parts.append("Self-made 'agents' are strategy cards: the desk creates and tests them itself, within fixed limits. "
                 "Agents that need new code arrive only through tested updates.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- /health and the pulse
def health_text(bot, j: Journal, version: str, started_at: str, update_status: dict | None, code_version: str | None) -> str:
    beat = j.get("heartbeat") or {}
    pace = (bot.pace or {}).get("minutes", bot.s.cycle_minutes)
    lines = [f"🩺 Health check · version {version}, running since {started_at[:16].replace('T', ' ')} UTC"]
    if code_version and code_version != version.split(" ")[0]:
        lines.append(f"⚠️ Installed code is {code_version} but the running desk is {version}: a restart is pending")
    late = True
    if beat.get("ts"):
        minutes = (now_utc() - dt.datetime.fromisoformat(beat["ts"])).total_seconds() / 60
        late = minutes > 2 * pace + 15
        lines.append(("⚠️ " if late else "✅ ") + f"Last market check {ago(beat['ts'])} (cycle {beat.get('cycle')}), "
                     f"next in about {pace} min")
    else:
        lines.append("⏳ No market check finished yet since the start")
    problems = bot.base.problems()
    for p in problems:
        lines.append(f"🛑 Settings problem: {p}")
    for m in bot.base.migrations:
        lines.append(f"🔧 Corrected at start: {m}")
    f = funnel(j, since_iso(24))
    lines.append("\nLast 24 hours:\n" + funnel_text(f))
    if (stuck := stuck_rule(f)):
        lines.append(f"🛑 '{stuck[0]}' refused every buy signal ({stuck[1]} of {stuck[1]}): check the settings")
    errors = [e for e in j.events(since=since_iso(24)) if e["kind"] == "error"]
    lines.append(f"Errors: {len(errors)}" + (f", last {ago(errors[-1]['ts'])}: {clean(errors[-1]['text'], 200)}" if errors else ""))
    if bot.llm:
        budget = bot.llm.effective_daily_budget()
        lines.append(f"Claude: ${bot.llm.spent_today:.2f} spent today" + (f" of ${budget:.2f}" if budget else "")
                     + (" (budget used up: free rules until 00:00 UTC)" if bot.llm.budget_hit else ""))
    st = update_status or {}
    if st:
        lines.append(f"Updates: last check {ago(st.get('checked_at'))}, " + (st.get("last_result") or st.get("result") or "no result yet")
                     + (f"; ⚠️ {st['last_check_error']}" if st.get("last_check_error") else "")
                     + (f"; an update was interrupted during {st['in_progress'].get('step')}" if st.get("in_progress") else ""))
    else:
        lines.append("Updates: automatic updates are not set up on this machine")
    roster_rows = roster(bot, j)
    bad = [r for r in roster_rows if r["status"] == "failing"]
    lines.append(f"Agents: {sum(r['status'] == 'working' for r in roster_rows)} of {len(roster_rows)} working"
                 + (" · failing: " + ", ".join(r["name"] for r in bad) if bad else "") + " (details: /agents)")
    verdict = "🛑 Needs attention" if (problems or late or bad or stuck_rule(f)) else "✅ All systems working"
    return verdict + "\n" + "\n".join(lines)


def pulse_text(bot, j: Journal, hours: float, version: str) -> str:
    f = funnel(j, since_iso(hours))
    rows = roster(bot, j, hours=hours)
    working = sum(r["status"] == "working" for r in rows)
    errors = sum(1 for e in j.events(since=since_iso(hours)) if e["kind"] == "error")
    lines = [f"💓 Desk pulse, last {hours:g} h · version {version}", funnel_text(f),
             f"Agents working: {working} of {len(rows)} · errors: {errors}"]
    if (stuck := stuck_rule(f)):
        lines.append(f"🛑 '{stuck[0]}' refused every buy signal: send /health")
    lines.append(f"Next check in about {(bot.pace or {}).get('minutes', bot.s.cycle_minutes)} min. /health for details.")
    return "\n".join(lines)
