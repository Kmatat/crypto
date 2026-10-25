"""Strategy laboratory: proposes, examines, forward-tests, promotes and rolls back strategy cards.

Why the exam is deliberately hard: independent reviews showed that on these markets random
entries do about as well as the indicators, so any loop that invents strategies and keeps the
best backtest will mostly be keeping luck. A challenger therefore has to
  1. stay within BOUNDS and differ from the champion in at most MAX_CHANGES fields,
  2. beat the champion on most of several periods (including periods that ended months ago)
     and on a second coin set, never losing badly on any of them, with enough trades to mean
     something,
  3. then beat the champion again on the weeks AFTER it was proposed (a forward test on data
     that did not exist when the idea was born),
  4. and finally be approved by the owner on Telegram (/approve <id>), unless AUTO_PROMOTE is on.
A promoted champion is watched: if its live trades turn clearly bad, the previous champion is
restored and the owner is told. Every promotion and rollback is logged.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time

from .agents import decision as dmod
from .backtest import EXTRA_DAYS, Backtest, fetch_history
from .binance_client import BinanceClient
from .config import PUBLIC_BASE_URL, Settings
from .journal import Journal
from .strategy import MAX_CHANGES, StrategyCard, summary_line, thresholds_line

log = logging.getLogger(__name__)

# (days, ending how many days ago). Two windows end in the past so "it works now" is not enough.
WINDOWS = [(90, 0), (180, 0), (365, 0), (180, 90), (180, 180)]
MIN_PASS_SHARE = 0.6        # challenger must beat the champion on at least this share of tests
MAX_WORSE_POINTS = 2.0      # ... and never be worse by more than this on any test
MIN_TOTAL_TRADES = 40       # across all tests; fewer is not evidence
FORWARD_MIN_DAYS = 28       # forward test length before a challenger can be promoted
FORWARD_MIN_TRADES = 8
ROLLBACK_MIN_TRADES = 30    # live trades under a champion before its profit factor is judged
ROLLBACK_PROFIT_FACTOR = 0.7
HOLDOUT_WINDOW = 90         # the threshold study never sees the most recent 90 days; its cards must win them


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def restore_decision_rules(saved: tuple) -> None:
    """Undo StrategyCard.apply_decision_rules() after an exam (the live desk's rules must not change)."""
    dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD = saved[0], saved[1]
    dmod.WEIGHTS.clear()
    dmod.WEIGHTS.update(saved[2])
    dmod.COIN_THRESHOLDS.clear()
    dmod.COIN_THRESHOLDS.update(saved[3])


class Lab:
    def __init__(self, s: Settings, journal: Journal, client: BinanceClient | None = None, fetch=None,
                 history: dict | None = None):
        self.s = s
        self.journal = journal
        self.client = client or (None if s.offline else BinanceClient(PUBLIC_BASE_URL))
        self._fetch = fetch or fetch_history
        self._history: dict[tuple[str, str], list[dict]] = dict(history or {})  # (symbol, interval) -> candles
        self._history_days: dict[tuple[str, str], int] = {k: 10_000 for k in self._history}  # how many days each holds
        self._analysis: dict[str, dict] = {}  # interval -> indicator cache shared by every window and card (see Backtest)

    # ---- state ----
    def champion(self) -> StrategyCard:
        d = self.journal.get("strategy_champion")
        if d:
            return StrategyCard.from_dict(d["card"])
        card = StrategyCard.from_settings(self.s)
        self.set_champion(card, by="owner settings", note="first run")
        return card

    def set_champion(self, card: StrategyCard, by: str, note: str = "") -> None:
        prev = self.journal.get("strategy_champion")
        self.journal.set("strategy_champion", {"card": card.to_dict(), "since": now_iso(), "by": by,
                                               "previous": prev["card"] if prev else None})
        hist = self.journal.get("strategy_history") or []
        hist.append({"ts": now_iso(), "event": "promote", "id": card.id, "name": card.name, "by": by, "note": note})
        self.journal.set("strategy_history", hist[-100:])
        if prev:
            old = StrategyCard.from_dict(prev["card"])
            self.journal.event("strategy", f"Champion is now {card.name} [{card.id}] ({by}{', ' + note if note else ''}). "
                                           f"Changes versus {old.name} [{old.id}]: {card.describe_diff(old)}")
        else:
            self.journal.event("strategy", f"First champion recorded: {summary_line(card)} ({by})")

    def challengers(self) -> list[dict]:
        return self.journal.get("strategy_challengers") or []

    def _save_challengers(self, items: list[dict]) -> None:
        self.journal.set("strategy_challengers", items[-50:])

    def add_challenger(self, card: StrategyCard) -> dict | None:
        champ = self.champion()
        items = self.challengers()
        if card.id == champ.id or any(c["card"]["id"] == card.id for c in items):
            return None
        problems = card.violations()
        changes = card.diff(champ)
        if len(changes) > MAX_CHANGES:
            problems.append(f"changes {len(changes)} fields (max {MAX_CHANGES}): {', '.join(changes)}")
        entry = {"card": card.to_dict(), "proposed_at": now_iso(), "status": "rejected" if problems else "proposed",
                 "notes": problems, "lab": None, "forward": None}
        items.append(entry)
        self._save_challengers(items)
        self.journal.event("challenger", f"Proposed {card.name} [{card.id}] ({card.source or 'proposal'}): "
                                         f"{card.describe_diff(champ)}" + (f" - rejected before testing: {problems[0]}"
                                                                           if problems else ""))
        return entry

    # ---- data ----
    def history(self, symbols: list[str], interval: str, days: int | None = None) -> dict[str, list[dict]]:
        """Candles for the exam windows (or at least `days`, for the head trader's longer threshold study)."""
        need = max(days or 0, max(d + off for d, off in WINDOWS) + 1)
        out = {}
        for sym in list(symbols) + ["BTCUSDT"]:
            key = (sym, interval)
            if key not in self._history or self._history_days.get(key, 0) < need:
                self._history[key] = self._fetch(self.client, sym, need, interval)
                self._history_days[key] = need
            out[sym] = self._history[key]
        return out

    # ---- exam ----
    def evaluate(self, card: StrategyCard, coin_sets: list[list[str]] | None = None) -> dict:
        coin_sets = coin_sets or [list(self.s.symbols), list(self.s.lab_alt_symbols)]
        saved = (dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD, dict(dmod.WEIGHTS), dict(dmod.COIN_THRESHOLDS))
        tests = []
        try:
            card.apply_decision_rules()
            settings = card.settings(self.s)
            for coins in coin_sets:
                hist = self.history(coins, card.interval)
                btc = hist["BTCUSDT"]
                data = {c: hist[c] for c in coins}
                last_ms = min(h[-1]["time"] for h in data.values())
                cache = self._analysis.setdefault(card.interval, {})
                for days, offset in WINDOWS:
                    end = last_ms - offset * 86_400_000 if offset else None
                    r = Backtest(settings, data, days=days, btc_history=btc, end_ms=end, analysis_cache=cache).run()
                    tests.append({"coins": ",".join(c.removesuffix("USDT") for c in coins), "days": days,
                                  "ending_days_ago": offset, "return_pct": r["return_pct"], "trades": r["trades"],
                                  "profit_factor": r["profit_factor"], "max_drawdown_pct": r["max_drawdown_pct"],
                                  "basket_hold_pct": r["basket_hold_pct"]})
        finally:
            restore_decision_rules(saved)
        returns = [t["return_pct"] for t in tests]
        pfs = [t["profit_factor"] for t in tests if t["profit_factor"] is not None]
        return {"tests": tests, "mean_return": round(sum(returns) / len(returns), 2), "worst_return": min(returns),
                "total_trades": sum(t["trades"] for t in tests), "mean_profit_factor": round(sum(pfs) / len(pfs), 2) if pfs else None,
                "max_drawdown_pct": max(t["max_drawdown_pct"] for t in tests), "evaluated_at": now_iso()}

    def compare(self, challenger: dict, champion: dict, strict_holdout: bool = False) -> dict:
        """Does the challenger earn promotion to the forward test? `strict_holdout` is for cards built from a
        study of past prices (the threshold study): most exam windows overlap what the study saw, so such a
        card must also win every window that the study never saw (the most recent HOLDOUT_WINDOW days)."""
        pairs = list(zip(challenger["tests"], champion["tests"]))
        wins = sum(1 for a, b in pairs if a["return_pct"] > b["return_pct"])
        worst_gap = max(b["return_pct"] - a["return_pct"] for a, b in pairs)
        reasons = []
        if strict_holdout:
            lost = [a["coins"] for a, b in pairs
                    if a["ending_days_ago"] == 0 and a["days"] <= HOLDOUT_WINDOW and a["return_pct"] <= b["return_pct"]]
            if lost:
                reasons.append(f"a study card must beat the champion on every held-out window (last {HOLDOUT_WINDOW} days); "
                               f"it lost on {', '.join(lost)}")
        if wins < math.ceil(MIN_PASS_SHARE * len(pairs)):
            reasons.append(f"beat the champion on only {wins} of {len(pairs)} tests")
        if worst_gap > MAX_WORSE_POINTS:
            reasons.append(f"worse than the champion by {worst_gap:.1f} points on one test (limit {MAX_WORSE_POINTS})")
        if challenger["total_trades"] < MIN_TOTAL_TRADES:
            reasons.append(f"only {challenger['total_trades']} trades across all tests (need {MIN_TOTAL_TRADES})")
        if challenger["mean_profit_factor"] is not None and challenger["mean_profit_factor"] < 1.0:
            reasons.append(f"average profit factor {challenger['mean_profit_factor']} below 1.0")
        if challenger["max_drawdown_pct"] > self.s.max_drawdown_pct * 100:
            reasons.append(f"worst drop {challenger['max_drawdown_pct']}% exceeds the {self.s.max_drawdown_pct:.0%} limit")
        return {"passed": not reasons, "wins": wins, "tests": len(pairs), "worst_gap": round(worst_gap, 2), "reasons": reasons}

    def forward_test(self, entry: dict, champion: StrategyCard) -> dict:
        """The weeks since the proposal: data that did not exist when the idea was born."""
        card = StrategyCard.from_dict(entry["card"])
        proposed = dt.datetime.fromisoformat(entry["proposed_at"])
        days = (dt.datetime.now(dt.timezone.utc) - proposed).total_seconds() / 86400
        if days < FORWARD_MIN_DAYS:
            return {"ready": False, "passed": None, "days": round(days, 1),
                    "reason": f"forward test needs {FORWARD_MIN_DAYS} days, has {days:.0f}"}
        results = {}
        for name, c in (("challenger", card), ("champion", champion)):
            saved = (dmod.BUY_THRESHOLD, dmod.SELL_THRESHOLD, dict(dmod.WEIGHTS), dict(dmod.COIN_THRESHOLDS))
            try:
                c.apply_decision_rules()
                hist = self.history(list(self.s.symbols), c.interval)
                data = {x: hist[x] for x in self.s.symbols}
                results[name] = Backtest(c.settings(self.s), data, days=int(days) + 1, btc_history=hist["BTCUSDT"],
                                         analysis_cache=self._analysis.setdefault(c.interval, {})).run()
            finally:
                restore_decision_rules(saved)
        ch, cp = results["challenger"], results["champion"]
        reasons = []
        if ch["trades"] < FORWARD_MIN_TRADES:
            reasons.append(f"only {ch['trades']} trades in the forward period (need {FORWARD_MIN_TRADES})")
        if ch["return_pct"] < cp["return_pct"] - 0.5:
            reasons.append(f"forward return {ch['return_pct']:+.2f}% vs champion {cp['return_pct']:+.2f}%")
        return {"ready": True, "passed": not reasons, "days": round(days, 1), "challenger_return": ch["return_pct"],
                "champion_return": cp["return_pct"], "challenger_trades": ch["trades"], "reasons": reasons,
                "tested_at": now_iso()}

    # ---- the weekly run ----
    def run_week(self, proposals: list[StrategyCard]) -> dict:
        champ = self.champion()
        champ_eval = self.evaluate(champ)
        self.journal.set("strategy_champion_eval", champ_eval)
        for card in proposals:
            self.add_challenger(card)
        items = self.challengers()
        promoted = None
        for entry in items:
            card = StrategyCard.from_dict(entry["card"])
            if entry["status"] == "proposed":
                ev = self.evaluate(card)
                verdict = self.compare(ev, champ_eval, strict_holdout=(card.source or "").startswith("study"))
                entry["lab"] = {**ev, "verdict": verdict}
                entry["status"] = "lab_passed" if verdict["passed"] else "lab_failed"
                entry["notes"] = verdict["reasons"]
                self.journal.event("lab", f"Exam of {card.name} [{card.id}]: {'passed' if verdict['passed'] else 'failed'} - "
                                          f"mean {ev['mean_return']:+.2f}% vs champion {champ_eval['mean_return']:+.2f}%, beat "
                                          f"it on {verdict['wins']}/{verdict['tests']} tests"
                                   + ("; " + "; ".join(verdict["reasons"]) if verdict["reasons"] else ""))
            if entry["status"] == "lab_passed":
                fwd = self.forward_test(entry, champ)
                entry["forward"] = fwd
                if fwd["ready"]:
                    entry["status"] = "ready" if fwd["passed"] else "forward_failed"
                    entry["notes"] = fwd.get("reasons", [])
                    self.journal.event("lab", f"Forward test of {card.name} [{card.id}]: {'passed' if fwd['passed'] else 'failed'}"
                                              f" - {fwd['challenger_return']:+.2f}% vs champion {fwd['champion_return']:+.2f}% "
                                              f"over {fwd['days']:.0f} days" + ("; " + "; ".join(fwd["reasons"]) if fwd.get("reasons") else "")
                                       + ("" if not fwd["passed"] else ("; promoted automatically" if self.s.auto_promote
                                                                        else "; waiting for the owner's /approve")))
            if entry["status"] == "ready" and self.s.auto_promote and promoted is None:
                promoted = card
                self.promote(card.id, by="auto-promote")
                entry["status"] = "promoted"
        self._save_challengers(items)
        self.journal.set("lab_last_run", now_iso())
        examined = sum(1 for e in items if (e.get("lab") or {}).get("evaluated_at", "") >= champ_eval["evaluated_at"][:10])
        ready = sum(1 for e in items if e["status"] == "ready")
        self.journal.event("lab", f"Weekly lab done: champion {champ.name} [{champ.id}] exam mean {champ_eval['mean_return']:+.2f}% "
                                  f"over {len(champ_eval['tests'])} tests; {examined} challenger(s) examined, {ready} ready for approval"
                           + (f"; promoted {promoted.name} [{promoted.id}]" if promoted else ""))
        return {"champion": champ, "champion_eval": champ_eval, "challengers": items, "promoted": promoted}

    # ---- promotion, rollback, live watch ----
    def promote(self, card_id: str, by: str) -> StrategyCard | None:
        for entry in self.challengers():
            if entry["card"]["id"] == card_id and entry["status"] in ("ready", "lab_passed", "promoted"):
                card = StrategyCard.from_dict(entry["card"])
                self.set_champion(card, by=by, note=entry["status"])
                items = self.challengers()
                for e in items:
                    if e["card"]["id"] == card_id:
                        e["status"] = "promoted"
                self._save_challengers(items)
                return card
        return None

    def reject(self, card_id: str) -> bool:
        items = self.challengers()
        for e in items:
            if e["card"]["id"] == card_id and e["status"] != "promoted":
                e["status"] = "rejected"
                e["notes"] = ["rejected by the owner"]
                self._save_challengers(items)
                self.journal.event("owner", f"Owner rejected {e['card']['name']} [{card_id}]")
                return True
        return False

    def rollback(self, reason: str) -> StrategyCard | None:
        state = self.journal.get("strategy_champion") or {}
        prev = state.get("previous")
        card = StrategyCard.from_dict(prev) if prev else StrategyCard.from_settings(self.s, "owner settings")
        self.set_champion(card, by="rollback", note=reason)
        hist = self.journal.get("strategy_history") or []
        hist.append({"ts": now_iso(), "event": "rollback", "id": card.id, "name": card.name, "by": "lab", "note": reason})
        self.journal.set("strategy_history", hist[-100:])
        self.journal.event("rollback", f"Rolled back to {card.name} [{card.id}]: {reason}")
        return card

    def watch_live(self) -> str | None:
        """Roll back when the champion's live trades turn clearly bad. Returns the reason, or None."""
        state = self.journal.get("strategy_champion") or {}
        if not state.get("previous") or state.get("by") in ("owner settings", "rollback"):
            return None
        rows = self.journal.db.execute("SELECT pnl FROM closed_trades WHERE closed_at >= ? ORDER BY id",
                                       (state["since"],)).fetchall()
        if len(rows) < ROLLBACK_MIN_TRADES:
            return None
        wins = sum(r[0] for r in rows if r[0] > 0)
        losses = -sum(r[0] for r in rows if r[0] <= 0)
        pf = wins / losses if losses else float("inf")
        if pf < ROLLBACK_PROFIT_FACTOR:
            reason = f"profit factor {pf:.2f} over {len(rows)} live trades (limit {ROLLBACK_PROFIT_FACTOR})"
            self.rollback(reason)
            return reason
        return None


# ---- texts for Telegram / terminal ----
def status_text(lab: Lab) -> str:
    champ = lab.champion()
    state = lab.journal.get("strategy_champion") or {}
    lines = [f"🏆 Champion since {state.get('since', '')[:10]} ({state.get('by', '')}):", summary_line(champ),
             f"Buy thresholds: {thresholds_line(champ)}"]
    ev = lab.journal.get("strategy_champion_eval")
    if ev:
        lines.append(f"Exam: mean {ev['mean_return']:+.2f}%, worst {ev['worst_return']:+.2f}%, "
                     f"{ev['total_trades']} trades, profit factor {ev['mean_profit_factor']}")
    items = [c for c in lab.challengers() if c["status"] not in ("promoted",)]
    if items:
        lines.append("\nChallengers:")
        for c in items[-8:]:
            card = StrategyCard.from_dict(c["card"])
            extra = ""
            if c.get("lab"):
                extra = f" exam {c['lab']['mean_return']:+.2f}% vs champion ({c['lab']['verdict']['wins']}/{c['lab']['verdict']['tests']} wins)"
            if c.get("forward") and c["forward"].get("ready"):
                extra += f"; forward {c['forward']['challenger_return']:+.2f}% vs {c['forward']['champion_return']:+.2f}%"
            note = f" - {c['notes'][0]}" if c.get("notes") else ""
            lines.append(f"• [{card.id}] {card.name}: {c['status']}{extra}{note}")
            lines.append(f"    changes: {card.describe_diff(champ)}")
    ready = [c for c in items if c["status"] == "ready"]
    if ready:
        lines.append("\nReady for your decision: send /approve <id> or /reject <id>.")
    last = lab.journal.get("lab_last_run")
    lines.append(f"\nLast lab run: {last[:16].replace('T', ' ') if last else 'never'} UTC")
    return "\n".join(lines)


def week_report(result: dict) -> str:
    champ = result["champion"]
    ev = result["champion_eval"]
    lines = [f"🔬 Weekly strategy lab", f"Champion {champ.name} [{champ.id}]: exam mean {ev['mean_return']:+.2f}%, "
             f"worst {ev['worst_return']:+.2f}%, {ev['total_trades']} trades"]
    fresh = [c for c in result["challengers"] if c.get("lab") and c["lab"].get("evaluated_at", "") >= ev["evaluated_at"][:10]]
    for c in fresh:
        card = StrategyCard.from_dict(c["card"])
        v = c["lab"]["verdict"]
        lines.append(f"• [{card.id}] {card.name} ({card.source or 'proposal'}): {c['status']} - "
                     f"exam {c['lab']['mean_return']:+.2f}%, beat champion {v['wins']}/{v['tests']}"
                     + (f"; {c['notes'][0]}" if c.get("notes") else ""))
    ready = [c for c in result["challengers"] if c["status"] == "ready"]
    if result.get("promoted"):
        lines.append(f"🚀 Promoted automatically: {summary_line(result['promoted'])}")
    elif ready:
        lines.append(f"✅ {len(ready)} challenger(s) passed the exam and the forward test - send /strategies then "
                     "/approve <id> to switch.")
    else:
        lines.append("No challenger has earned a switch. The champion stays.")
    return "\n".join(lines)
