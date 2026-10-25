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
           "free_ai_vetoes": 0, "news_vetoes": 0, "sell_signals": 0, "sells": 0}
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
        elif str(r["source"] or "").startswith("ai:"):
            out["free_ai_vetoes"] += 1
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
        if f.get("free_ai_vetoes"):
            parts.append(f"{f['free_ai_vetoes']} vetoed by the free AI")
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


def judged_since(j: Journal, hours: float = 24) -> str:
    """The stuck-rule alarm judges only decisions made under the strategy and settings now in force: refusals from
    before a correction (for example the old take-profit fixed at start) say nothing about the corrected desk."""
    state = j.get("strategy_champion") or {}
    return max(since_iso(hours), state.get("since") or "")


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
                     + ("AI research" if wl.get("source") == "claude" and wl.get("web", True) else
                        "the free AI with Claude's web research" if str(wl.get("source", "")).startswith("ai:")
                        and wl.get("web") else
                        "AI (market data only)" if str(wl.get("source", "")).startswith(("ai:", "claude")) else
                        "market scan"))
                    if bot.scout and wl.get("ts")
                    else ("waiting for its first run" if bot.scout else "AUTO_COINS=false"), err("scout")))
    cache = getattr(bot.news, "_cache", None)
    news_ts = dt.datetime.fromtimestamp(cache[0], dt.timezone.utc).isoformat(timespec="seconds") if cache else None
    mood = (cache[1].get("_mood") if cache else None) or "no headlines read yet"
    readers = (["the free AI"] if getattr(bot, "free_ai", None) else []) + (["Claude"] if bot.llm else [])
    if getattr(bot, "free_ai", None) and bot.llm and getattr(bot.ai, "order", "") == "claude-first":
        readers.reverse()
    rows.append(row("News", "headlines scored per coin by " + (", then ".join(readers) + ", then keywords" if readers
                                                                 else "keywords"),
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
                    (f"last check: AI for {', '.join(plan.get('allowed') or []) or 'nobody'}; {plan.get('why', 'no plan yet')}"
                     if head else "HEAD_TRADER=false")))
    claude_calls = j.db.execute("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND source = 'claude'", (since,)).fetchone()[0]
    free_calls = j.db.execute("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND source LIKE 'ai:%' "
                              "AND source NOT LIKE '%-reused'", (since,)).fetchone()[0]
    rows.append(row("Decision (portfolio manager)", "BUY / SELL / HOLD from all reports", "working" if f["decisions"] else "idle",
                    f"{f['decisions']} decisions, {claude_calls} with a fresh Claude opinion"
                    + (f", {free_calls} with a fresh free-AI opinion" if free_calls or getattr(bot, "free_ai", None) else ""),
                    err("decision")))
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
    rows.append(row("Scorecard", "measures each AI and the news against real later prices", "working" if cases else "idle",
                    f"{cases} AI decisions judged so far"))
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
    since = judged_since(j)
    current = funnel(j, since) if since > since_iso(24) else f
    if since > since_iso(24):
        lines.append(f"Since the strategy in force took effect ({since[11:16]} UTC): {current['buy_signals']} buy signals, "
                     f"{current['approved']} approved" + ("; refused: " + ", ".join(f"{k} {n}" for k, n in current["refused"].items())
                                                         if current["refused"] else ""))
    if (stuck := stuck_rule(current)):
        lines.append(f"🛑 '{stuck[0]}' refused every buy signal ({stuck[1]} of {stuck[1]}): check the settings")
    errors = [e for e in j.events(since=since_iso(24)) if e["kind"] == "error"]
    lines.append(f"Errors: {len(errors)}" + (f", last {ago(errors[-1]['ts'])}: {clean(errors[-1]['text'], 200)}" if errors else ""))
    if bot.llm:
        budget = bot.llm.effective_daily_budget()
        lines.append(f"Claude: ${bot.llm.spent_today:.2f} spent today" + (f" of ${budget:.2f}" if budget else "")
                     + (" (budget used up until 00:00 UTC)" if bot.llm.budget_hit else ""))
    if getattr(bot, "free_ai", None):
        lines.append(free_ai_line(bot.free_ai))
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
    verdict = "🛑 Needs attention" if (problems or late or bad or stuck_rule(current)) else "✅ All systems working"
    return verdict + "\n" + "\n".join(lines)


def pulse_text(bot, j: Journal, hours: float, version: str) -> str:
    f = funnel(j, since_iso(hours))
    rows = roster(bot, j, hours=hours)
    working = sum(r["status"] == "working" for r in rows)
    errors = sum(1 for e in j.events(since=since_iso(hours)) if e["kind"] == "error")
    lines = [f"💓 Desk pulse, last {hours:g} h · version {version}", funnel_text(f),
             f"Agents working: {working} of {len(rows)} · errors: {errors}"]
    if (stuck := stuck_rule(funnel(j, judged_since(j)))):
        lines.append(f"🛑 '{stuck[0]}' refused every buy signal: send /health")
    lines.append(f"Next check in about {(bot.pace or {}).get('minutes', bot.s.cycle_minutes)} min. /health for details.")
    return "\n".join(lines)


# ---------------------------------------------------------------- /diagnose: the live desk versus the rules' replay
def coin_funnel(j: Journal, since: str) -> dict[str, dict]:
    """Per coin: checks, buy signals, approved, refusals by reason, Claude and news vetoes, signals the head trader's
    portfolio tilt removed (score cleared the strategy's threshold but not the tilted one)."""
    out: dict[str, dict] = {}
    rows = j.db.execute("SELECT symbol, action, rules_action, risk_approved, risk_reason, source, reasoning, "
                        "combined_score, reports FROM decisions WHERE ts >= ?", (since,)).fetchall()
    for r in rows:
        c = out.setdefault(r["symbol"], {"decisions": 0, "buy_signals": 0, "approved": 0, "refused": {}, "claude": 0,
                                         "news": 0, "tilted": 0, "best_score": None})
        c["decisions"] += 1
        score = r["combined_score"]
        if score is not None:
            c["best_score"] = score if c["best_score"] is None else max(c["best_score"], score)
        if r["rules_action"] != "BUY":
            try:
                head = (json.loads(r["reports"] or "{}").get("head") or {})
            except ValueError:
                head = {}
            if head.get("why", "").startswith("+") and score is not None and score < head.get("threshold", 9) \
                    and score >= head.get("threshold", 9) - 0.05 * head["why"].count("+"):
                c["tilted"] += 1
            continue
        c["buy_signals"] += 1
        if r["action"] == "BUY":
            if r["risk_approved"]:
                c["approved"] += 1
            else:
                for label in _refusal(r["risk_reason"]):
                    c["refused"][label] = c["refused"].get(label, 0) + 1
        elif (r["reasoning"] or "").startswith("Clearly negative news"):
            c["news"] += 1
        elif str(r["source"] or "").startswith(("claude", "ai:")):
            c["claude"] += 1   # vetoed by an AI (Claude or the free AI)
    return out


def check_gaps(j: Journal, since: str, pace_minutes: float) -> dict:
    """When was the desk not checking the market? (one equity row is written per check)"""
    ts = [dt.datetime.fromisoformat(r[0]) for r in j.db.execute("SELECT ts FROM equity WHERE ts >= ? ORDER BY rowid", (since,))]
    limit = dt.timedelta(minutes=2 * max(pace_minutes, 15) + 15)
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > limit]
    longest = max(gaps, key=lambda g: g[1] - g[0], default=None)
    return {"checks": len(ts), "gaps": len(gaps), "down_hours": round(sum((b - a).total_seconds() for a, b in gaps) / 3600, 1),
            "longest": (longest[0].isoformat(timespec="minutes"), round((longest[1] - longest[0]).total_seconds() / 3600, 1))
            if longest else None}


def wallet_line(bot) -> str | None:
    """Testnet only: what the demo wallet on Binance really holds, next to the desk's own budget."""
    testnet = getattr(bot, "testnet", None)
    if testnet is None:
        return None
    try:
        held = testnet.balances_total()
    except Exception as e:  # noqa: BLE001 - a report line, never a failure
        return f"Demo wallet on Binance: could not be read ({clean(e, 100)})"
    usdt = held.get("USDT", 0.0)
    others = []
    for asset, qty in sorted(held.items(), key=lambda x: -x[1]):
        if asset == "USDT" or len(others) >= 4:
            continue
        try:
            others.append(f"{asset} ${qty * bot.market.client.price(asset + 'USDT'):,.0f}")
        except Exception:  # noqa: BLE001 - coins without a USDT price
            continue
    return f"Demo wallet on Binance: {usdt:,.2f} USDT" + (f" plus {', '.join(others)}" if others else "")


def settings_lines(bot) -> list[str]:
    s, base = bot.s, bot.base
    card = getattr(bot, "card", None)
    try:
        equity, cash = bot.equity(), bot.journal.cash
    except Exception:  # noqa: BLE001
        equity, cash = None, None
    budget = [f"Budget: starting balance ${base.starting_balance:,.0f}"
              + (f", equity now ${equity:,.2f} (cash ${cash:,.2f})" if equity is not None else "")
              + f"; positions about ${(equity or base.starting_balance) * s.max_position_pct:,.2f} each (minimum order $10)"]
    if bot.llm:
        pct = base.ai_pct_month()
        budget.append(f"Claude cap " + (f"${bot.llm.effective_daily_budget():.2f}/day" if bot.llm.effective_daily_budget()
                                        else "none") + " ("
                      + ("set with /aibudget" if bot.journal.get("ai_budget_override") else
                         f"the lower of ${base.ai_daily_budget_usd:g}/day and {pct:g}% a month of the balance" if pct else
                         f"AI_DAILY_BUDGET_USD; no percentage cap on demo money")
                      + f"); spent today ${bot.llm.spent_today:.2f}")
    if (w := wallet_line(bot)):
        budget.append(w)
    lines = [*budget, f"Strategy in force: stop {s.stop_loss_atr:g} ATR, target {s.take_profit_atr:g} ATR "
             f"({s.take_profit_atr / s.stop_loss_atr:.1f}:1, rule minimum {s.min_reward_risk:g}:1), trailing {s.trail_atr:g} ATR, "
             f"checks {', '.join(s.mentor_checks) or 'none'}, cooldown {s.cooldown_hours:g} h",
             f"Size: {s.max_open_positions} positions, at most {s.max_position_pct:.0%} each, risk {s.risk_per_trade_pct:.1%} "
             f"per trade, {s.interval} candles; coins: {', '.join(x.removesuffix('USDT') for x in base.symbols)}"
             + (f" + up to {base.max_coins - len(base.symbols)} scout picks" if base.auto_coins else ""),
             f"Decisions: buy threshold {thresholds_line(card) if card else 'common'}, minimum confidence "
             f"{base.min_confidence:g}; Claude {'on (' + base.claude_model + ')' if bot.llm else 'off'}"
             + (f", ${bot.llm.effective_daily_budget():.2f}/day cap" if bot.llm else "")
             + (f"; free AI {bot.free_ai.label} ({', '.join(bot.free_ai.models)}), {getattr(bot.ai, 'order', 'free-first')}"
                if getattr(bot, "free_ai", None) else "; free AI off")]
    return lines


def free_ai_line(free) -> str:
    """The free AI's day in one line: requests used per model, what is left, when it restarts."""
    st = free.status()
    used = st["usage"].get("models", {})
    per = ", ".join(f"{m} {used.get(m, {}).get('requests', 0)}" + ("" if st["shared"] else f"/{st['daily']}")
                    + (" (used up)" if m in st["usage"].get("exhausted", []) else "")
                    + (f" (off: {st['disabled'][m]})" if m in st["disabled"] else "") for m in st["models"])
    answers = sum(v.get("answers", 0) for v in used.values())
    bad = sum(v.get("bad", 0) for v in used.values())
    off = f" ⚠️ off: {st['disabled']['*']}" if "*" in st["disabled"] else ""
    return (f"Free AI ({st['label']}): requests today {per}" + (f" of {st['daily']} shared" if st["shared"] else "")
            + f"; {answers} answers, {bad} in the wrong form; daily allowance restarts in {st['reset_hours']:g} h{off}")


def diagnose_text(bot, j: Journal, days: float, replay=None) -> str:
    """The owner's "check the logs": what the desk did versus what its own rules would have done on the same prices.
    `replay(symbols, days)` returns a Backtest result for those coins over the period (None if unavailable)."""
    since = since_iso(days * 24)
    pace = (bot.pace or {}).get("minutes", bot.s.cycle_minutes)
    coins = coin_funnel(j, since)
    gaps = check_gaps(j, since, pace)
    opened = j.db.execute("SELECT COUNT(*) FROM orders WHERE side='BUY' AND ok=1 AND ts >= ?", (since,)).fetchone()[0]
    failed = j.db.execute("SELECT symbol, error, COUNT(*) n FROM orders WHERE ok=0 AND ts >= ? GROUP BY symbol, error "
                          "ORDER BY n DESC LIMIT 3", (since,)).fetchall()
    closed = j.db.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0), SUM(pnl > 0) FROM closed_trades WHERE closed_at >= ?",
                          (since,)).fetchone()
    lines = [f"🔎 Diagnosis, last {days:g} days", *settings_lines(bot)]
    for p in bot.base.problems():
        lines.append(f"🛑 Settings problem: {p}")
    lines.append(f"\nDesk activity: {gaps['checks']} market checks"
                 + (f"; not checking for {gaps['down_hours']} h in {gaps['gaps']} gap(s), longest {gaps['longest'][1]} h from "
                    f"{gaps['longest'][0].replace('T', ' ')} UTC" if gaps["gaps"] else "; no gaps"))
    lines.append(f"Live: {opened} buys filled, {closed[0]} trades closed ({closed[1]:+,.2f} USDT, {closed[2] or 0} winners)"
                 + (" · order failures: " + "; ".join(f"{f['symbol']} x{f['n']}: {clean(f['error'], 80)}" for f in failed)
                    if failed else ""))
    totals = {"buy_signals": 0, "approved": 0, "claude": 0, "news": 0, "tilted": 0, "refused": {}}
    lines.append("\nPer coin (buy signals → approved; what stopped the rest):")
    for sym, c in sorted(coins.items(), key=lambda x: -x[1]["buy_signals"]):
        stops = sorted(c["refused"].items(), key=lambda x: -x[1])
        extra = [f"{k} {n}" for k, n in stops[:3]]
        if c["claude"]:
            extra.append(f"AI veto {c['claude']}")
        if c["news"]:
            extra.append(f"bad news {c['news']}")
        if c["tilted"]:
            extra.append(f"portfolio tilt {c['tilted']}")
        best = f", best score {c['best_score']:+.2f}" if c["best_score"] is not None else ""
        lines.append(f"• {sym.removesuffix('USDT')}: {c['buy_signals']} → {c['approved']}"
                     + (f" ({'; '.join(extra)})" if extra else "") + best)
        for k in ("buy_signals", "approved", "claude", "news", "tilted"):
            totals[k] += c[k]
        for k, n in c["refused"].items():
            totals["refused"][k] = totals["refused"].get(k, 0) + n
    if not coins:
        lines.append("• no decisions recorded in this period")
    r = None
    if replay is not None and coins:
        try:
            r = replay(sorted(coins), days)
        except Exception as e:  # noqa: BLE001 - the replay needs the network; the rest of the report stands alone
            lines.append(f"\n(The rules' replay could not run: {clean(e, 120)})")
    if r is not None:
        lines.append(f"\nThe rules alone on the same coins and prices (no news, no AI, no head trader): {r['trades']} "
                     f"trades, {r['return_pct']:+.2f}% (holding the coins: {r['basket_hold_pct']:+.1f}%)")
        gap = r["trades"] - opened
        if gap > max(2, r["trades"] * 0.3):
            causes = sorted([("AI vetoes", totals["claude"]), ("bad-news blocks", totals["news"]),
                             ("the head trader's portfolio tilt", totals["tilted"])]
                            + [(k, n) for k, n in totals["refused"].items()], key=lambda x: -x[1])
            top = [f"{k} ({n})" for k, n in causes if n][:3]
            lines.append(f"⚠️ The live desk opened {gap} fewer trades than its rules would have. "
                         + (f"Largest live-only causes: {', '.join(top)}." if top else "")
                         + (f" The desk was not checking for {gaps['down_hours']} h." if gaps["down_hours"] else ""))
        else:
            lines.append("✅ The live desk traded about as often as its rules: no hidden blocker. Fewer trades mean the "
                         "market gave fewer signals that pass the rules.")
    return "\n".join(lines)
