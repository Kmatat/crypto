"""The owner's texts of the post-mortem & memory agent (trader/agents/memory.py): /postmortem on Telegram, the
POSTMORTEM.md page of the notebook, the memory rule's checklist line, its daily-report line, the cause mix of the
research agent's lessons, the review digest's section (only with the rule on), the replay's lines and the command
line `python -m trader postmortem`.

Everything here describes the record after the fact: how similar trades turned out, which causes were more common
among losers than among winners, with the counts and a plain chance figure. It is a measurement, never a forecast,
and nothing here places an order or changes a setting.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
import time

from .agents import memory as mem
from .agents.memory import (CAUSE_MIN_LOSERS, CAUSE_MIN_WINNERS, CAUSES, MEMORY_DAYS, MIN_POOL, MIN_SIMILAR,
                            OWNER_RULE_LOSS_SHARE, chance_text, describe_bucket, limit_text)

log = logging.getLogger(__name__)

MEMORY_REASON = "master traders' checklist: memory not met"   # the buys loop's reason when memory alone refused
FAMILY_WORDS = {"execution": "execution worse than the replay assumes",
                "news": "US release or measured news jump during the trade",
                "chop": "chop at entry", "failed_breakout": "failed breakout", "noise": "inside normal noise"}
FAMILY_RULES = [
    ("execution worse than the replay assumes",
     "a stop exit sold more than 0.4% below its stop price; bought more than 0.25 ATR above the finished candle's "
     "close; the demo exchange filled more than 0.5% away from the real market; less than 98% of the planned amount "
     "bought, or sold in parts",
     "0.4% = twice the 0.2% stop slippage every replay assumes; 0.25 ATR = an eighth of the 2-ATR stop spent before the "
     "trade starts (Perold's implementation shortfall: paper versus reality)"),
    ("US release or measured news jump during the trade",
     "a listed US release (CPI, jobs report, FOMC statement) from 2 hours before the buy to the sale, or a headline "
     "or release jump the event study measured in that window",
     "the event study measures a release's move over the 2 hours after it; releases are scheduled, so this says "
     "'a release fell inside the trade', not 'an unexpected release'"),
    ("chop at entry", "the hourly or the 4-hour chart was in chop (going nowhere) at the buy",
     "the chart reader's own definition (Kaufman's efficiency ratio below 0.30; Dow theory / Wyckoff: no trade "
     "inside a range)"),
    ("failed breakout", "bought on a breakout that closed back below its level within the chart reader's recent "
     "window (6 hourly bars, 2 four-hour bars = 8 hours) counted from the decision",
     "the published failed-breakout pattern (Connors & Raschke, 'Turtle Soup', 1995); judged on the price path, so "
     "winners can be tagged too"),
    ("inside normal noise", "the stop was at most as far away as the dip the coin reached in 1 of 10 ordinary 2-hour "
     "stretches of the week before the buy",
     "Kase's DevStop idea (stops from the coin's own two-bar range), without a distribution assumption"),
    ("no tag", "none of the rules applies", "an ordinary loss: this strategy loses about two trades in three by design"),
]
HOURLY_LOSSES = "this strategy loses about two trades in three by design"
TREND_LOSSES = ("the daily trend rule loses most of its trades by design: it wins 20-30%, and a few long trends pay "
                "for the rest")
NOT_A_FORECAST = ("This is a record of how similar trades turned out, not a forecast. The chance figure counts each "
                  "trade separately (trades bought together often lose together) and is worked out again at every "
                  "check, so a refusal can still be luck; in the replays it refused nothing most of the time.")
SEED_TREND = "The memory rule belongs to the hourly agents; the daily trend rule has no checklist."
SEED_TREND_STATUS = "No replay seed: the daily trend rule needs none. " + SEED_TREND
SEED_OFFLINE = "The replay needs Binance's public prices, which offline mode does not read."
TRY_IT = ("To try it: add memory to MENTOR_CHECKS (desk setup) after measuring it with\n"
          "desk backtest --days 730 --checks trend200,btc,cooldown,reward,memory")


# ---- small helpers ----
def _pct(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _when(iso_text: str | None, with_year: bool = False) -> str:
    if not iso_text:
        return "unknown"
    try:
        d = dt.datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    d = d.astimezone(dt.timezone.utc)
    return f"{d.day} {d:%b}" + (f" {d.year}" if with_year else "") + f" {d:%H:%M} UTC"


def _day(iso_text: str | None) -> str:
    if not iso_text:
        return "unknown"
    try:
        d = dt.datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_text)[:10]
    return f"{d.day} {d:%b} {d.year}"


def _hours(a: str, b: str) -> float:
    try:
        return (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0


def _esc(s) -> str:
    return str("" if s is None else s).replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_none yet_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def champion_card(j, s) -> dict | None:
    """The strategy card in force as stored by the desk (None before the first check or with the trend rule)."""
    if getattr(s, "trend", False):
        return None
    state = j.get("strategy_champion") or {}
    card = state.get("card")
    return card if isinstance(card, dict) else None


def memory_on(j, s) -> bool:
    """Is the memory rule in the checks of the strategy in force (the owner's MENTOR_CHECKS before the first check)?"""
    if getattr(s, "trend", False):
        return False
    card = champion_card(j, s)
    checks = card.get("checks") if card else getattr(s, "mentor_checks", ())
    return "memory" in tuple(checks or ())


def _card_id(j, s) -> str | None:
    """The id of the strategy card in force: the stored champion, or before the desk's first check the card of the
    owner's settings (the one the first check records)."""
    card = champion_card(j, s)
    if card:
        return card.get("id")
    if s is None or getattr(s, "trend", False):
        return None
    from .strategy import StrategyCard
    return StrategyCard.from_settings(s).id


def _ids_of(j, s, card_id: str | None) -> tuple[str, ...]:
    """The ids the memory counts as the strategy `card_id` when it is the card in force: its own and its twin with the
    memory rule switched the other way (memory.strategy_ids); just `card_id` for any other card."""
    if not card_id:
        return ()
    from .strategy import StrategyCard
    card = champion_card(j, s)
    if card and card.get("id") == card_id:
        return mem.strategy_ids(StrategyCard.from_dict(card))
    if s is not None and not getattr(s, "trend", False) and not card:
        own = StrategyCard.from_settings(s)
        if own.id == card_id:
            return mem.strategy_ids(own)
    return (card_id,)


def _exclude(j) -> list:
    from .notes import card_periods
    return card_periods(j.get("strategy_history") or [], "trend")


def _releases():
    return mem.release_times(mem.owner_calendar())


def _releases_for(j):
    """The listed releases with the owner's calendar next to this journal (none for an in-memory journal)."""
    path = getattr(j, "path", ":memory:")
    return mem.release_times(mem.owner_calendar(path) if path != ":memory:" else None)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---- the memory rule's checklist line (called by MemoryBook.verdict) ----
def checklist_detail(v: dict) -> str:
    """The words after '✓ memory:' / '✗ memory:' / '? memory:' in the checklist (Telegram, and the AI prompt when the
    rule is on)."""
    desc = describe_bucket(v.get("bucket") or "")
    n, losses, pool = v.get("n") or 0, v.get("losses") or 0, v.get("pool") or 0
    if v.get("rule") == "owner60":
        if v.get("ok") is None:
            return f"{n} similar past trades ({desc}) - it judges from {MIN_SIMILAR}"
        head = f"{losses} of {n} similar past trades lost ({losses / n:.0%}; {desc})"
        if v["ok"]:
            return head + f" - not more than {OWNER_RULE_LOSS_SHARE:.0%} (the first plan's rule, replay only)"
        return head + f" - more than {OWNER_RULE_LOSS_SHARE:.0%} (the first plan's rule, replay only): buy refused"
    if v.get("ok") is None:
        if pool < MIN_POOL:
            return f"only {pool} trades in memory (needs {MIN_POOL}) - it does not judge yet"
        return f"{n} similar past trades ({desc}) - it judges from {MIN_SIMILAR}"
    base, share = v.get("base") or 0.0, losses / n if n else 0.0
    head = f"{losses} of {n} similar past trades lost ({share:.0%}; {desc}) against {base:.0%} of all {pool} in memory"
    if v["ok"] is False:
        groups = v.get("groups") or 0
        return (head + f" ({groups} group{'s' if groups != 1 else ''} judged) - chance does that only "
                f"{chance_text(v.get('p'))} (limit {limit_text(v.get('level'))}): buy refused")
    if share <= base:
        return head + " - nothing unusual"
    return head + f" - chance does that {chance_text(v.get('p'))}, the limit is {limit_text(v.get('level'))}"


# ---- the replay's lines (python -m trader backtest --checks ...,memory) ----
def replay_memory_text(m: dict) -> str:
    """The backtest's lines about the memory rule: its refusals replayed anyway against the trades made, replayed the
    same way, and the memory at the end."""
    first = ("Memory rule as first planned (more than 60% losers among 10 or more similar trades):"
             if m.get("rule") == "owner60" else
             "Memory rule (similar past trades lost clearly more often than usual):")
    w, made, end = m.get("whatif") or {}, m.get("made") or {}, m.get("end") or {}
    refused = m.get("refused") or 0
    if not refused:
        body = " refused no buy signal."
    else:
        body = (f" refused {refused} buy signal{'s' if refused != 1 else ''}; {w.get('n', 0)} of them replayed anyway "
                "(one per coin while the previous would still be open)")
        if w.get("n"):
            made_share = made["losers"] / made["n"] if made.get("n") else None
            body += (f": {w['losers']} would have lost ({w['losers'] / w['n']:.0%}) against {_pct(made_share)} of the "
                     f"trades made, replayed the same way ({w['mean_pct']:+.2f}% per refused trade, "
                     + (f"{made['mean_pct']:+.2f}%" if made.get("mean_pct") is not None else "n/a")
                     + " per trade made).")
        else:
            body += "."
    refusing = end.get("refusing") or []
    tail = (f" At the end: {end.get('pool', 0)} trades in memory, {end.get('groups', 0)} groups judged, "
            + ("none refusing." if not refusing else f"{len(refusing)} refusing ("
               + "; ".join(describe_bucket(b) for b in refusing) + ")."))
    return (first + body + tail + " The memory starts empty and fills with this replay's own trades (closed before "
            "each decision); a measurement on past prices, not a forecast.")


# ---- the cause mix ----
def verdict_words(f: dict) -> str:
    v = f["verdict"]
    if v == "too_few":
        return f"too few trades to compare yet (needs {CAUSE_MIN_LOSERS} losers and {CAUSE_MIN_WINNERS} winners)"
    if v == "not_a_cause":
        return "as common among winners: not a cause by itself"
    if v == "more_among_losers":
        return f"clearly more common among losers: chance {chance_text(f['p'])}"
    return f"more common among losers, but chance could do that: {chance_text(f['p'])}"   # the gap's size is shown


def _share(a: int, n: int) -> str:
    return f"{a} of {n}" + (f" ({a / n:.0%})" if n else "")


def losses_words(trend: bool = False) -> str:
    """How often the strategy in force loses by design: the hourly agents about two trades in three, the daily trend
    rule most of them."""
    return TREND_LOSSES if trend else HOURLY_LOSSES


def mix_lines(mix: dict, trend: bool = False) -> list[str]:
    """One line per family: losers with it, winners with it, the verdict words; then the no-tag line (`trend`: the
    daily trend rule is in force)."""
    out = []
    for fam in CAUSES:
        f = mix["families"][fam]
        if not f["losers_judged"] and not f["winners_judged"]:
            out.append(f"• {FAMILY_WORDS[fam]}: not judged for these trades")
            continue
        out.append(f"• {FAMILY_WORDS[fam]}: {_share(f['losers'], f['losers_judged'])} losers, "
                   f"{_share(f['winners'], f['winners_judged'])} winners - {verdict_words(f)}")
    out.append(f"• no tag: {mix['losers_no_tag']} of {mix['losers']} losers - ordinary losses ({losses_words(trend)})")
    return out


def mix_table(mix: dict, trend: bool = False) -> str:
    rows = []
    for fam in CAUSES:
        f = mix["families"][fam]
        if not f["losers_judged"] and not f["winners_judged"]:
            rows.append([FAMILY_WORDS[fam], "-", "-", "-", "not judged for these trades"])
            continue
        rows.append([FAMILY_WORDS[fam], _share(f["losers"], f["losers_judged"]), _share(f["winners"], f["winners_judged"]),
                     chance_text(f["p"]) if f["p"] is not None else "-", verdict_words(f)])
    return (_table(["Cause", "Losers with it", "Winners with it", "Chance", "Verdict"], rows)
            + f"\n\nNo tag: {mix['losers_no_tag']} of {mix['losers']} losers and {mix['winners_no_tag']} of "
              f"{mix['winners']} winners (ordinary results: {losses_words(trend)}). A cause counts only when it is clearly more common among losers than among winners; each of "
              f"the five comparisons is judged at {mix['level']:.2%} so that the chance of any false alarm stays at "
              "5%. Trades bought together often win or lose together, so the true chance is somewhat larger.")


# ---- one trade ----
def _trade_head(it: dict) -> str:
    t = it["trade"]
    coin = str(t["symbol"]).removesuffix("USDT")
    reasons = t.get("exit_reasons") or [t.get("exit_reason")]
    reason = " + ".join(dict.fromkeys(str(r) for r in reasons if r)) or "sold"
    held = _hours(t["opened_at"], t["closed_at"])
    return (f"• {coin} {t['pnl_pct']:+.1f}% · {reason} after {held:.0f} h · bought {_when(t['opened_at'])}"
            + (" (replay)" if it["source"] == "replay" else ""))


def _trade_lines(it: dict) -> list[str]:
    out = [f"  - {tag['text']}" for tag in it["tags"]] or ["  - no tag"]
    ctx = it.get("ctx")
    if it["source"] == "live" and not ctx:
        out.append("  (no entry record: bought before entry records were kept; only fills and US releases are judged)")
    elif ctx and ctx.get("strategy") == "trend":
        out.append("  (chart causes not judged: the daily trend rule reads no hourly chart)")
    elif ctx and ctx.get("backfilled"):
        out.append("  (entry record rebuilt from the stored decision: breakouts and noise not judged)")
    if it.get("pending"):
        out.append("  (headline measurement pending)")
    return out


# ---- /postmortem ----
def postmortem_text(j, s, arg: str = "") -> str:
    """/postmortem [n | memory | seed-status]: the last n closed trades (default 5, at most 20) with their tags, the
    losers-against-winners mix, the replay seed's mix and the memory rule's state."""
    arg = (arg or "").strip().lower()
    if arg == "memory":
        return memory_text(j, s, _card_id(j, s))
    if arg in ("seed-status", "seed status"):
        return seed_status_text(j, s)
    try:
        n = max(1, min(20, int(arg))) if arg else 5
    except ValueError:   # the daily trend rule has no seed to offer (seed_refusal)
        return "Usage: /postmortem, /postmortem 10 (at most 20)" + (
            " or /postmortem memory" if getattr(s, "trend", False) else ", /postmortem memory or /postmortem seed")
    releases, coverage = _releases_for(j)
    items = mem.postmortems(j, releases, source="live", coverage=coverage)
    if not items:
        lines = ["🔍 Post-mortem: No closed trades yet: the post-mortem starts with the first sale."]
    else:
        shown = items[:n]
        lines = [f"🔍 Post-mortem of the last {len(shown)} closed trade{'s' if len(shown) != 1 else ''} (what the "
                 "record shows, not a forecast)"]
        for it in shown:
            lines.append(_trade_head(it))
            lines += _trade_lines(it)
        mix = mem.cause_mix(items)
        lines += ["", f"Causes over {mix['trades']} closed trades ({mix['losers']} losers, {mix['winners']} winners). "
                      "A cause matters only when it is clearly more common among losers than among winners:"]
        lines += mix_lines(mix, getattr(s, "trend", False))
    if not getattr(s, "trend", False):
        if line := replay_line(j, s):
            lines.append(line)
        lines.append("Memory rule: on (/postmortem memory)." if memory_on(j, s) else "Memory rule: off. " + TRY_IT)
    return "\n".join(lines)


def replay_line(j, s) -> str:
    """One line on the replay seed's cause mix (empty without a seed)."""
    seed = j.get("memory_seed") or {}
    releases, coverage = _releases_for(j)
    items = mem.postmortems(j, releases, source="replay", coverage=coverage)
    if not items:
        return ""
    mix = mem.cause_mix(items)
    card = seed.get("card_id")
    other = card and card not in _ids_of(j, s, _card_id(j, s))
    head = (f"Replay of {'an earlier strategy' if other else 'the strategy in force'}"
            f" ({seed.get('days', '?')} days before {_day(seed.get('until'))}, {len(seed.get('coins') or [])} coins, "
            f"labelled replay): {mix['trades']} trades, {_pct(mix['losers'] / mix['trades'] if mix['trades'] else None)} "
            "losers")
    parts = []
    for fam in ("failed_breakout", "chop", "noise", "news"):
        f = mix["families"][fam]
        if f["losers_judged"] and f["winners_judged"]:
            parts.append(f"{FAMILY_WORDS[fam]} {f['losers'] / f['losers_judged']:.0%} of losers vs "
                         f"{f['winners'] / f['winners_judged']:.0%} of winners - {verdict_words(f)}")
    return head + ("; " + "; ".join(parts) if parts else "") + "."


def seed_status_text(j, s) -> str:
    if getattr(s, "trend", False):   # /postmortem seed refuses on this desk (seed_refusal): never offer it
        return "🧠 " + SEED_TREND_STATUS
    seed = j.get("memory_seed")
    if not seed:
        return "🧠 No replay seed yet: " + ("it is made automatically within a day while the memory rule is on, or now "
                                            "with /postmortem seed." if memory_on(j, s) else
                                            "it is made only when the memory rule is on, or when you send /postmortem seed.")
    card_id = _card_id(j, s)
    return "🧠 " + seed_line(seed, card_id, memory_on(j, s), _ids_of(j, s, card_id))


def seed_line(seed: dict, card_id: str | None, on: bool = True, ids: tuple = ()) -> str:
    """The seed's state; `on`: the memory rule is in force (only then is the seed made again automatically); `ids`:
    the ids that count as the strategy in force (_ids_of; default just `card_id`)."""
    ids = tuple(ids) or ((card_id,) if card_id else ())
    if seed.get("error") and not seed.get("trades") and not seed.get("not_needed"):
        return (f"Seed: the last attempt failed ({seed['error']}); " + ("it tries again the next day while the rule is "
                "on, or now with /postmortem seed." if on else "send /postmortem seed to try again."))
    if seed.get("not_needed"):
        text = (f"Seed: none needed (checked {_when(seed.get('ts'))} for strategy [{seed.get('card_id')}]): the desk's "
                f"own trades cover the memory's {MEMORY_DAYS} days")
    else:
        text = (f"Seed: {seed.get('trades', 0)} replay trades made {_when(seed.get('ts'))} for strategy "
                f"[{seed.get('card_id')}]")
    if card_id and seed.get("card_id") not in ids:
        text += f" - an earlier strategy than the one in force [{card_id}], so it is not used until it is made again"
    elif card_id and seed.get("card_id") != card_id:
        text += (f" - the strategy in force [{card_id}] with the memory rule switched the other way; the replay never "
                 "applies that rule, so it counts")
    text += ("; refreshed when the strategy changes, or with /postmortem seed." if on else
             "; with the memory rule off it is not refreshed automatically: only with /postmortem seed.")
    if seed.get("error"):
        text += f" (A later attempt failed: {seed['error']}.)"
    return text


# ---- /postmortem memory ----
def memory_state(j, s, card_id: str | None, now_ms: int | None = None) -> dict:
    """The memory now: its rows, the book's cells at this moment, the groups judged and their verdicts."""
    now_ms = now_ms or _now_ms()
    ids = _ids_of(j, s, card_id)
    rows = mem.pool_rows(j, ids, _exclude(j))
    window = [r for r in rows if now_ms - MEMORY_DAYS * mem.DAY_MS < r["closed_ms"] <= now_ms]
    book = mem.MemoryBook(rows)
    cells = book.cells(now_ms)
    verdicts = {b: book.verdict(b, now_ms) for b, (n, _) in cells.items() if n >= MIN_SIMILAR}
    return {"rows": window, "cells": cells, "verdicts": verdicts, "pool": len(window),
            "losers": sum(1 for r in window if r["loss"]),
            "live": sum(1 for r in window if r["source"] == "live"),
            "replay": sum(1 for r in window if r["source"] == "replay"),
            "other_cards": sum(1 for r in window if r["source"] == "live" and ids and r.get("card_id") not in ids)}


def refused_count(j, since: str | None = None) -> int:
    """Buy signals the checklist refused with memory among the failed rules."""
    sql = "SELECT risk_reason FROM decisions WHERE action='BUY' AND risk_approved=0 AND risk_reason LIKE ?"
    args: list = ["master traders' checklist:%memory%"]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    n = 0
    for (reason,) in j.db.execute(sql, args):
        names = str(reason).removeprefix("master traders' checklist: ").removesuffix(" not met").split(", ")
        n += "memory" in names
    return n


def memory_text(j, s, card_id: str | None) -> str:
    """/postmortem memory: what is in the memory, the groups it judges, the limit, what it refuses now, the closest
    group, its refusals so far and the seed."""
    if getattr(s, "trend", False):
        return "🧠 " + SEED_TREND
    on = memory_on(j, s)
    st = memory_state(j, s, card_id)
    seed = j.get("memory_seed") or {}
    lines = ["🧠 Memory rule: " + ("on (MENTOR_CHECKS has memory)" if on else "off")]
    pool, base = st["pool"], (st["losers"] / st["pool"] if st["pool"] else None)
    coins = len(seed.get("coins") or [])
    src = [f"{st['live']} live"]
    if st["replay"]:
        src.append(f"{st['replay']} replay of the strategy in force [{card_id}] on {coins} coins, labelled replay")
    if st["other_cards"]:
        src.append(f"{st['other_cards']} live trades of earlier strategy cards included")
    lines.append(f"In memory now: {pool} closed trades of the last {MEMORY_DAYS} days ({'; '.join(src)})."
                 + (f" {base:.0%} of them lost - this strategy's normal." if base is not None else ""))
    if pool < MIN_POOL:
        lines.append(f"It does not judge yet: only {pool} trades in memory (needs {MIN_POOL}).")
    else:
        m = len(st["verdicts"])
        level = mem.sidak(mem.FAMILY_ALPHA, m)
        lines.append(f"Groups with {MIN_SIMILAR} or more trades: {m}. A buy is refused only when its group lost so much "
                     f"more often than {base:.0%} that chance would do it less than 1 time in {round(1 / level):,} (the "
                     f"5% limit split over the {m} group{'s' if m != 1 else ''}).")
        refusing = [v for v in st["verdicts"].values() if v["ok"] is False]
        lines.append("Refusing now: " + ("none." if not refusing else "; ".join(
            f"{describe_bucket(v['bucket'])} - {v['losses']} of {v['n']} lost ({v['losses'] / v['n']:.0%}); chance "
            f"{chance_text(v['p'])}" for v in refusing) + "."))
        rest = sorted((v for v in st["verdicts"].values() if v["ok"] is not False), key=lambda v: v["p"])
        if rest:
            v = rest[0]
            lines.append(f"Closest to the limit: {describe_bucket(v['bucket'])} - {v['losses']} of {v['n']} lost "
                         f"({v['losses'] / v['n']:.0%}); chance {chance_text(v['p'])}.")
    lines.append(f"Refusals at checks so far: {refused_count(j)} (one hourly signal can be refused at several checks, "
                 "4 at the 15-minute pace; /report judges those the memory rule made alone, one per coin at a time).")
    lines.append(NOT_A_FORECAST)
    if seed:
        lines.append(seed_line(seed, card_id, on, _ids_of(j, s, card_id)))
    else:
        lines.append("Seed: none yet - " + ("made automatically within a day while the rule is on, or now with "
                                             "/postmortem seed." if on else "made only when the rule is on, or with "
                                             "/postmortem seed."))
    if not on:
        lines.append(TRY_IT)
    return "\n".join(lines)


# ---- the daily report, the digest, the health row ----
_GROUP = re.compile(r"\(\d+%; (.*?)\) against")


def memory_daily_line(j, since: str) -> str | None:
    """The daily report's line (only when the memory rule is on): refusals in the last 24 h and the memory's size."""
    rows = j.db.execute("SELECT reports FROM decisions WHERE action='BUY' AND risk_approved=0 AND ts >= ? AND "
                        "risk_reason LIKE ?", (since, "master traders' checklist:%memory%")).fetchall()
    groups: dict[str, int] = {}
    for (reports,) in rows:
        m = _GROUP.search(str(reports or ""))
        if m:
            groups[m.group(1)] = groups.get(m.group(1), 0) + 1
    try:
        card = j.get("strategy_champion") or {}
        card_id = (card.get("card") or {}).get("id")
        pool = memory_state(j, None, card_id)["pool"]
    except Exception:  # noqa: BLE001 - a report line must never fail the report
        log.exception("Memory size unavailable")
        pool = None
    size = f"{pool} trades in memory" if pool is not None else "memory size unavailable"
    if not rows:
        return f"🧠 Memory rule: refused nothing in the last 24 h; {size}" + \
            (f" (it judges from {MIN_POOL})." if pool is not None and pool < MIN_POOL else ".")
    which = "; ".join(groups) if groups else "see /postmortem memory"
    return (f"🧠 Memory rule: refused buys at {len(rows)} check{'s' if len(rows) != 1 else ''} in the last 24 h "
            f"({which}); {size}.")


def digest_section(j, s) -> str:
    """The review digest's section (only when the memory rule is on): the live cause mix and the memory's state."""
    releases, coverage = _releases_for(j)
    items = mem.postmortems(j, releases, source="live", coverage=coverage)
    parts = ["# Post-mortem (losers against winners; a measurement after the fact)", ""]
    if items:
        parts.append(mix_table(mem.cause_mix(items), getattr(s, "trend", False)))
    else:
        parts.append("No closed trades yet.")
    parts += ["", memory_text(j, s, _card_id(j, s))]
    return "\n".join(parts)


def health_parts(j, s, since: str) -> tuple[str, str]:
    """(status, detail) of the /health roster row."""
    on = memory_on(j, s)
    live = j.db.execute("SELECT COUNT(*) FROM trade_context WHERE source='live'").fetchone()[0]
    replay = j.db.execute("SELECT COUNT(*) FROM trade_context WHERE source='replay'").fetchone()[0]
    recent = j.db.execute("SELECT COUNT(*) FROM trade_context WHERE source='live' AND created_at >= ?",
                          (since,)).fetchone()[0]
    seed = j.get("memory_seed") or {}
    detail = (f"{live} trades with an entry record, {replay} replay trades; memory rule {'on' if on else 'off'}; "
              + (f"seed {_day(seed.get('ts'))} for [{seed.get('card_id')}]" if seed.get("trades") else
                 "no seed needed (the desk's own trades cover the window)" if seed.get("not_needed") else
                 f"seed failed: {seed['error']}" if seed.get("error") else "no seed"))
    if on:
        try:
            if memory_state(j, s, _card_id(j, s))["pool"] < MIN_POOL:
                detail += f"; it does not judge yet (fewer than {MIN_POOL} trades)"
        except Exception:  # noqa: BLE001
            log.exception("Memory state unavailable")
    return ("working" if recent or on else "idle"), detail


# ---- the research agent's lessons and the weekly page ----
def lessons_causes(journal, days, exclude) -> tuple[dict, list[str]]:
    """The live cause mix over the last `days` (the same exclusions as the lessons) and one sentence per family that is
    clearly more common among losers."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    releases, coverage = _releases_for(journal)
    items = [i for i in mem.postmortems(journal, releases, source="live", exclude=exclude, coverage=coverage)
             if i["trade"]["closed_at"] >= since and not str(i["trade"].get("exit_reason") or "").startswith("trend:")]
    mix = mem.cause_mix(items)
    notes = []
    for fam in CAUSES:
        f = mix["families"][fam]
        if f["verdict"] == "more_among_losers":
            notes.append(f"{FAMILY_WORDS[fam]}: {f['losers']} of {f['losers_judged']} losers against {f['winners']} of "
                         f"{f['winners_judged']} winners (chance {chance_text(f['p'])})")
    return mix, notes


def lessons_text(causes: dict) -> str:
    """The weekly page's section from lessons["causes"]."""
    if not causes or not causes.get("trades"):
        return "- no closed trades in the period"
    return "\n".join(f"- {line[2:]}" for line in mix_lines(causes))


# ---- POSTMORTEM.md ----
def postmortem_md(j, s) -> str:
    """POSTMORTEM.md. Never raises: any error gives a one-line page, so the rest of the notebook is still written."""
    try:
        return _postmortem_page(j, s)
    except Exception as e:  # noqa: BLE001 - deliberately broad: the page is a by-product
        log.exception("POSTMORTEM.md failed")
        return f"# Post-mortem\n\nThe post-mortem could not be built this check: {e}\n"


def _postmortem_page(j, s) -> str:
    trend = getattr(s, "trend", False)
    releases, coverage = _releases_for(j)
    items = mem.postmortems(j, releases, source="live", coverage=coverage)
    replay = mem.postmortems(j, releases, source="replay", coverage=coverage)
    seed = j.get("memory_seed") or {}
    out = ["# Post-mortem", "",
           "_Why closed trades won or lost, from the desk's own record (a measurement after the fact, not a forecast). "
           "Rebuilt after every check. Rules and thresholds: below._", "",
           "## How a trade is tagged", "",
           _table(["Cause", "Rule", "Why"], [list(r) for r in FAMILY_RULES[:-1]]
                  + [["no tag", "none of the rules applies", "an ordinary loss: " + losses_words(trend)]]), "",
           "Every closed trade, winner or loser, gets the same tests. Live trades of the hourly agents are judged on all "
           "five; the daily trend rule's on fills and US releases only; replay trades on releases, chop, failed "
           "breakouts and noise (a replay fills at the modelled price and has no headlines). A cause not judged is "
           "shown as not judged, never as no.", "",
           "## Live trades: losers against winners", ""]
    out.append(mix_table(mem.cause_mix(items), trend) if items else "No closed trades yet: the post-mortem starts with "
                                                                      "the first sale.")
    out += ["", "## Replay trades (labelled replay)", ""]
    if replay:
        out.append(f"Seed of strategy [{seed.get('card_id')}]: {seed.get('days', '?')} days before "
                   f"{_day(seed.get('until'))}, coins {', '.join(c.removesuffix('USDT') for c in seed.get('coins') or [])}, "
                   f"made {_when(seed.get('ts'))}.")
        out += ["", mix_table(mem.cause_mix(replay))]
    elif trend:
        out.append("No seed. " + SEED_TREND)
    elif seed.get("not_needed"):
        out.append(f"No seed needed: the desk's own trades cover the memory's {MEMORY_DAYS} days (checked "
                   f"{_when(seed.get('ts'))}).")
    else:
        out.append("No seed: the replay of the strategy in force is made only when the memory rule is on, or with "
                   "/postmortem seed.")
    out += ["", "## The memory rule", "", memory_text(j, s, _card_id(j, s)), ""]
    card = j.get("memory_scorecard")
    if card and card.get("cases"):
        out += [memory_score_line(card), ""]
    out += ["## The last 50 closed trades", ""]
    rows = []
    for it in items[:50]:
        t = it["trade"]
        tags = "; ".join(tag["text"] for tag in it["tags"]) or "no tag"
        if not it.get("ctx"):
            tags += " (no entry record)"
        rows.append([_when(t["closed_at"]), str(t["symbol"]).removesuffix("USDT"), f"{t['pnl_pct']:+.2f}%",
                     " + ".join(t.get("exit_reasons") or []), f"{_hours(t['opened_at'], t['closed_at']):.0f} h", tags])
    out.append(_table(["Closed", "Coin", "Result", "Exit", "Held", "Tags with evidence"], rows))
    return "\n".join(out)


def memory_score_line(card: dict) -> str:
    """The scorecard's line on the memory rule's refusals (scorecard.checklist_scorecard)."""
    lost = card.get("losers", 0)
    base = card.get("base_loss_pct")
    return (f"🧠 Memory rule refusals judged: {card['cases']} · would have lost {lost} "
            f"({lost / card['cases']:.0%}" + (f", the desk's trades lose {base:.0f}%" if base is not None else "")
            + f") · refusals saved ${card['saved_usd']:,.2f} · cost ${card['cost_usd']:,.2f} · net "
              f"${card['net_usd']:+,.2f} · Verdict: {card['verdict']}")


# ---- the seed's messages ----
def seed_start_text(coins: list[str], until_iso: str | None, desk: bool = True, days: int = mem.SEED_DAYS) -> str:
    """`until_iso`: the desk's first own trade with a chart picture (the live start), or None: the seed ends now;
    `days`: the days replayed (memory.seed_span: the part of the memory's window before the live start)."""
    if days <= 0:
        return (f"🧠 The desk's own trades already cover the memory's {MEMORY_DAYS} days: no replay is needed"
                + (" (the answer arrives here)." if desk else "."))
    when = f"before {_day(until_iso)} (the desk's own record starts then)" if until_iso else "up to now"
    return (f"🧠 Replaying the strategy in force over the {days} days {when} on "
            f"{', '.join(c.removesuffix('USDT') for c in coins)} to fill the memory (labelled replay)."
            + (" Market checks pause for a few minutes, as during the weekly lab; positions keep their exchange stops. "
               "The result arrives here." if desk else ""))


def seed_done_text(meta: dict, on: bool) -> str:
    if meta.get("error"):   # with the rule off nothing retries by itself (main.memory_seed_due)
        if not on:
            return (f"⚠️ The memory could not be filled: {meta['error']}. The memory rule is off, so it is not tried "
                    "again by itself: send /postmortem seed again later. The desk trades normally meanwhile.")
        return (f"⚠️ The memory could not be filled: {meta['error']}. It tries again tomorrow; the desk trades normally "
                f"meanwhile (until {MIN_POOL} trades are in memory the rule refuses nothing).")
    if meta.get("not_needed"):
        return (f"🧠 No replay needed: the desk's own trades cover the memory's {MEMORY_DAYS} days, so the memory holds "
                f"those only (strategy [{meta['card_id']}]). The memory rule is {'on' if on else 'off'}.")
    share = meta["losers"] / meta["trades"] if meta.get("trades") else None
    start = None
    try:
        until = dt.datetime.fromisoformat(meta["until"])
        start = (until - dt.timedelta(days=meta["days"])).isoformat()
    except (KeyError, TypeError, ValueError):
        pass
    return (f"🧠 Memory filled: {meta['trades']} replay trades of strategy [{meta['card_id']}] on "
            f"{len(meta.get('coins') or [])} coins ({_day(start)} to {_day(meta.get('until'))}); {_pct(share)} of them "
            "lost - that is how this strategy works: many small losses, a few larger wins. The memory rule is "
            f"{'on' if on else 'off'}.")


def seed_refusal(s) -> str | None:
    """Why /postmortem seed cannot run on this desk, or None."""
    if getattr(s, "trend", False):
        return SEED_TREND
    if getattr(s, "offline", False):
        return SEED_OFFLINE
    return None


def seed_coins(s) -> list[str]:
    """The lab's coins: the watched ones and the second coin set."""
    return list(dict.fromkeys(list(s.symbols) + list(getattr(s, "lab_alt_symbols", ()))))


# ---- python -m trader postmortem --replay ----
def replay_items(records: list[dict], releases) -> list[dict]:
    rels, coverage = releases
    rows = mem.seed_rows(records, "replay", "1h")
    items = []
    for ctx in rows:
        trade = {"symbol": ctx["symbol"], "opened_at": ctx["opened_at"], "closed_at": ctx["closed_at"],
                 "pnl": ctx["pnl"], "pnl_pct": ctx["pnl_pct"], "exit_reasons": [ctx["exit_reason"]], "sales": 1}
        items.append({"trade": trade, "ctx": ctx, "tags": mem.causes(trade, ctx, [], rels, [], 0, "replay"),
                      "loss": (ctx["pnl"] or 0) <= 0, "judged": mem.judged_families(trade, ctx, "replay", coverage),
                      "pending": 0, "source": "replay"})
    return items


def replay_report(records: list[dict], releases) -> str:
    """The cause mix of a replay's trades: trades, losers, the five families losers against winners with the verdict
    words, the no-tag line, and the loss rate with and without a failed breakout."""
    items = replay_items(records, releases)
    if not items:
        return "Post-mortem of the replay: no closed trades."
    mix = mem.cause_mix(items)
    lines = [f"Post-mortem of the replay's {mix['trades']} trades ({mix['losers']} losers, "
             f"{_pct(mix['losers'] / mix['trades'])}; a measurement after the fact, not a forecast):"]
    lines += mix_lines(mix)
    fb = [i for i in items if any(t["family"] == "failed_breakout" for t in i["tags"])]
    rest = [i for i in items if "failed_breakout" in i["judged"] and i not in fb]
    if fb and rest:
        lines.append(f"Loss rate of trades with a failed breakout: {_pct(sum(i['loss'] for i in fb) / len(fb))} "
                     f"({len(fb)} trades) against {_pct(sum(i['loss'] for i in rest) / len(rest))} without "
                     f"({len(rest)} trades).")
    lines.append(f"Each comparison is judged at {mix['level']:.2%} (5% split over the five causes); replay trades are "
                 "not judged on execution or headlines.")
    return "\n".join(lines)


# ---- the command line ----
def main(argv: list[str]) -> int:
    """python -m trader postmortem [--trades 20] | --replay --days 730 [--symbols ...] [--checks ...] | --seed."""
    ap = argparse.ArgumentParser(prog="trader postmortem")
    ap.add_argument("--trades", type=int, default=20, help="closed trades to show from the journal (at most 20)")
    ap.add_argument("--replay", action="store_true", help="replay the hourly rules on real prices and print the cause "
                                                          "mix of the replay's trades (no journal write)")
    ap.add_argument("--seed", action="store_true", help="fill the memory now: replay the strategy in force (labelled "
                                                        "replay)")
    ap.add_argument("--days", type=int, default=None, help="days to replay (default 730 with --replay; with --seed the "
                                                          "last 365 days, minus the part the desk's own trades cover)")
    ap.add_argument("--symbols", default=None, help="coins for --replay, e.g. BTC,ETH,SOL (default BTC,ETH,SOL)")
    ap.add_argument("--checks", default=None, help="checklist for --replay (default: the default checklist "
                                                   "trend200,btc,cooldown,reward, not your MENTOR_CHECKS)")
    ap.add_argument("--offline", action="store_true", help="the offline simulation's journal")
    args = ap.parse_args(argv)
    from .config import Settings
    if args.replay:
        return _replay_main(args)
    s = Settings.load(offline=args.offline)
    from .journal import Journal
    j = Journal(s.db_path, s.starting_balance)
    try:
        if args.seed:
            if why := seed_refusal(s):
                print(why)
                return 1
            from .binance_client import BinanceClient
            from .config import PUBLIC_BASE_URL
            from .strategy import StrategyCard
            state = j.get("strategy_champion")
            card = StrategyCard.from_dict(state["card"]) if state else StrategyCard.from_settings(s)
            coins = seed_coins(s)
            _, span = mem.seed_span(j, _now_ms(), args.days or mem.SEED_DAYS)
            print(seed_start_text(coins, mem.live_start(j), desk=False, days=span), file=sys.stderr)
            meta = mem.run_seed(s, card, j, BinanceClient(PUBLIC_BASE_URL), coins, _now_ms(),
                                days=args.days or mem.SEED_DAYS)
            print(seed_done_text(meta, memory_on(j, s)))
            return 0 if not meta.get("error") else 1
        print(postmortem_text(j, s, str(max(1, min(20, args.trades)))))
        return 0
    finally:
        j.db.close()


def _replay_main(args) -> int:
    from .backtest import Backtest, _symbols, fetch_history, print_result
    from .binance_client import BinanceClient
    from .config import PUBLIC_BASE_URL, Settings
    days = args.days or 730
    symbols = _symbols(args.symbols or "BTC,ETH,SOL")
    live = Settings()
    if args.checks is None:
        checks = live.mentor_checks
    elif args.checks in ("", "none"):
        checks = ()
    else:
        checks = tuple(c.strip() for c in args.checks.split(","))
    client = BinanceClient(PUBLIC_BASE_URL)
    print(f"Downloading {days} (+210 for long averages) days of hourly prices for {', '.join(symbols)} ...",
          file=sys.stderr)
    history = {sym: fetch_history(client, sym, days) for sym in symbols}
    btc = history.get("BTCUSDT") or fetch_history(client, "BTCUSDT", days)
    s = Settings(offline=True, symbols=symbols, mentor_checks=checks)
    releases = _releases()
    r = Backtest(s, history, days=days, btc_history=btc, record=True, releases=releases).run()
    print_result(r)
    if r.get("mentor_vetoes"):
        print(f"Checklist vetoes      {r['mentor_vetoes']}")
    if r.get("memory"):
        print(replay_memory_text(r["memory"]))
    print()
    print(replay_report(r.get("trade_records") or [], releases))
    return 0
