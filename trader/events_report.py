"""The owner-facing texts of the event study (trader/agents/events.py): /events, one line of the daily report and
the notebook page EVENTS.md.

Rules for every sentence: plain words; "measured", never "predicted"; a headline's time is its publication time;
a jump is a jump and a drift is a drift; every number says what it is compared with; headline text passes through
clean() because it came from the internet. Verdicts follow the scorecard's habit: none under 30 events per class,
and the verdict says whether a desk that looks every 15 minutes could have acted (the median minutes a jump took to
register, next to the median minutes to the extreme)."""
from __future__ import annotations

import datetime as dt
import statistics

from .agents.events import (BACKFILL_DAYS, BASELINE_DAYS, BASELINE_STEP_MIN, BURST_MINUTES, FLOOR_PCT, IMPULSE_MIN,
                            K_WEEKDAY, K_WEEKEND, KEEP_DAYS, KEEP_EVENT_DAYS, MIN_BASELINE_DAYS, MIN_MS, PLACEBO_DAYS,
                            POST_MIN, QUIET_MIN, REQUESTS_PER_EVENT, RETRACE, SETTLE_PER_CHECK, WAIT_DAYS,
                            WEEKEND_STEP_MIN, baseline_step_min, iso, k_for, ms_of, release_name, threshold,
                            usable_budget)
from .journal import Journal
from .notify import clean
from .report import wilson

MIN_VERDICT = 30          # events per class before a verdict is written (the scorecard's rule)
EVENTS_TREND = ("The event study works with the hourly agents (STRATEGY=hourly); the daily trend rule does not run it, "
                "so nothing is downloaded or measured while it trades. Anything measured earlier stays in the notebook "
                "(EVENTS.md).")
# what the calibration on real prices gave (build notes of 1.8.1; README "What prices do around news and scheduled
# releases"): the share of normal windows the jump rule calls a jump
CALIBRATION = {"study_placebo_pct": 9.5, "weekday_hours_pct": 7.5, "weekend_hours_pct": 8.4, "old_rule_pct": 45,
               # and how often the "price moved first" rule fires on normal windows: 9.0% of the study's placebo
               # windows, 9.7% of every weekday hour, 8.9% of every weekend hour (about one normal hour in ten)
               "moved_first_pct": 9.0}
FOMC_VOLUME = "usual for a 14:00 New York release: the US session closes inside the window"
TEXT_LIMIT = 3500         # Telegram cuts at 4,000; the footer must always fit
RETRACE_PCT = f"{RETRACE * 100:.0f}%"


# ---- small helpers ---------------------------------------------------------------------------------------------
def _coin(symbol: str) -> str:
    return (symbol or "").removesuffix("USDT")


def _dt(text: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms_of(text) / 1000, dt.timezone.utc)


def _day(text: str) -> str:
    d = _dt(text)
    return f"{d:%a} {d.day} {d:%b}"


def _when(text: str) -> str:
    d = _dt(text)
    return f"{d:%a} {d.day} {d:%b} {d:%H:%M} UTC"


def _minutes(a: str | None, b: str | None) -> int | None:
    return None if not a or not b else int((ms_of(b) - ms_of(a)) // 60_000)


def _median(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _rows(j: Journal, where: str = "", args: tuple = (), limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM news_events" + (" WHERE " + where if where else "") + " ORDER BY ts_utc DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in j.db.execute(sql, args)]


def _tone(score: float | None) -> str:
    if score is None:
        return "no reading"
    return f"{score:+.1f} ({'bullish' if score >= 0.3 else 'bearish' if score <= -0.3 else 'neutral'})"


def _is_inside_release(e: dict) -> bool:
    return e.get("kind") == "headline" and "within the" in (e.get("note") or "")


def _label(e: dict) -> str:
    return clean(e.get("label") or "", 160)


# ---- one event -------------------------------------------------------------------------------------------------
def event_sentence(e: dict) -> str:
    """One measured coin-event in plain words: what the coin did, the normal-day figure, the matched normal day."""
    coin = _coin(e["symbol"])
    macro = e["kind"] == "macro"
    if macro:
        head = f"{_label(e)} · {_when(e['ts_utc'])} · "
    else:
        n = e.get("n_headlines") or 1
        fast = " · fast news" if (e.get("velocity") or 0) >= 3 and n >= 2 else ""
        head = (f"\"{_label(e)}\" · published {_when(e['ts_utc'])}, {n} headline{'s' if n != 1 else ''} "
                f"({e.get('category') or 'other'}{fast}) · ")
    if e.get("impulse") is None:
        return head + f"{coin}: not measured ({e.get('note') or 'window incomplete'})."
    mag = e.get("magnitude_pct") or 0.0
    peak_min = _minutes(e["ts_utc"], e.get("peak_ts")) or 0
    still = peak_min >= IMPULSE_MIN
    before = "the release" if macro else "publication"
    if e["impulse"]:
        body = (f"{coin} {'rose' if mag > 0 else 'fell'} {abs(mag):.2f}% within {peak_min} min (the jump registered after "
                f"{e.get('propagation_min')} min)")
        if e.get("correction_delay_min") is not None:
            body += f", gave back {RETRACE_PCT} after {e['correction_delay_min']} min"
        else:
            body += f"; had not given back {RETRACE_PCT} by the end of the {POST_MIN // 60}-hour window"
        if still:
            body += "; still moving at the end of the 2-hour window"
        if e.get("volume_exhausted"):
            body += "; trading volume fell back to its pre-event level"
            if (e.get("category") or "") == "macro:fomc":
                body += f" ({FOMC_VOLUME})"
    else:
        body = (f"{coin}: no jump; {'drifted' if peak_min >= 90 else 'largest move'} {mag:+.2f}% "
                f"{'over' if peak_min >= 90 else 'in'} 2 h" + (", still moving at the end of the window" if still else ""))
    if e.get("moved_first"):
        body += f". Price moved first (a jump in the hour before {before})"
    body += "."
    if e.get("baseline_pct") is not None:
        # the rule's yardstick, not "what a normal day moves": it is read from 15-minute closes (30 at weekends),
        # which run lower than the 1-minute moves printed next to it; the matched day below is the like-for-like figure
        b, k = e["baseline_pct"], k_for(ms_of(e["ts_utc"]))
        how = f"{k:g} x {b:.2f}%" if k * b >= FLOOR_PCT else f"the {FLOOR_PCT:g}% floor, above {k:g} x {b:.2f}%"
        body += (f" A jump at that hour needed {threshold(b, k):.2f}% ({how}; {b:.2f}% is the normal days' usual "
                 f"largest 2-hour move on {baseline_step_min(ms_of(e['ts_utc']))}-minute closes).")
    if (e.get("volume_ratio") or 0) >= 2:
        body += f" Trading volume right after {before}: {e['volume_ratio']:.1f}x its normal level at that hour."
    if not macro and e.get("sentiment") is not None:
        body += f" The desk's news reading at the time: {_tone(e['sentiment'])}."
    what = "a normal day" if macro else f"a day with no {coin} headlines at that hour"
    if e.get("placebo_ts") and e.get("placebo_impulse") is not None:
        jumped = "a jump too" if e.get("placebo_impulse") else "no jump"
        body += f" Same hour on {_day(e['placebo_ts'])} ({what}): {e.get('placebo_magnitude_pct') or 0.0:+.2f}%, {jumped}."
    elif e.get("placebo_ts"):   # chosen and downloaded, but its window could not be measured: say so, show no figure
        why = next((p.removeprefix("placebo window: ") for p in (e.get("note") or "").split("; ")
                    if p.startswith("placebo window")), "")
        body += f" Same hour on {_day(e['placebo_ts'])} ({what}): could not be measured" + (f" ({why})." if why else ".")
    elif "could be judged" in (e.get("note") or ""):
        body += (" No normal day to compare: the free days within a week have too few normal days before them to be "
                 "judged.")
    else:
        body += " No normal day free to compare within a week."
    return head + body


def release_sentence(rows: list[dict]) -> str:
    """One release, every coin: 'CPI 14 Oct 12:30 UTC: BTC -2.27%, ETH -3.54% ... jumps in 7 of 8; normal days: ...'."""
    first = rows[0]
    name = release_name((first.get("category") or "").removeprefix("macro:"), first.get("label") or "")
    measured = [r for r in rows if r.get("impulse") is not None]
    parts = [f"{_coin(r['symbol'])} {r['magnitude_pct']:+.2f}%" + ("*" if r["impulse"] else "")
             for r in sorted(measured, key=lambda r: r["symbol"])]
    text = f"{name} {_when(first['ts_utc'])}: " + (", ".join(parts) if parts else "nothing measured")
    if measured:
        text += f"; jumps (*) in {sum(bool(r['impulse']) for r in measured)} of {len(measured)}"
        with_p = [r for r in measured if r.get("placebo_impulse") is not None]
        if with_p:
            text += (f"; normal days: {sum(bool(r['placebo_impulse']) for r in with_p)} of {len(with_p)} jumped, typical "
                     f"{_median([abs(r['placebo_magnitude_pct']) for r in with_p]):.2f}%")
    if len(measured) < len(rows):
        text += f"; {len(rows) - len(measured)} not measured"
    waiting = [r for r in rows if r.get("settled_at") is None]
    if waiting:
        text += f"; {len(waiting)} wait for the window"
    return text + "."


# ---- aggregates ------------------------------------------------------------------------------------------------
def category_table(j: Journal, kind_filter: str | None = None, symbol: str | None = None) -> list[dict]:
    """One aggregate per class (macro:cpi, macro:fomc, macro:nfp, headline/<category>) over the settled, measured
    rows. Headline bursts less than 4 hours before or after a listed release (the two 4-hour windows overlap) are left
    out of their class (so a release's move is not counted twice) and counted in 'inside'. Next to each event share
    stands the same share on the matched normal days (p_*), counted over the rows that have it. Sorted: macro classes
    first, then by size."""
    where, args = ["settled_at IS NOT NULL", "impulse IS NOT NULL"], []
    if kind_filter:
        where.append("kind=?")
        args.append(kind_filter)
    if symbol:
        where.append("symbol=?")
        args.append(symbol)
    rows = _rows(j, " AND ".join(where), tuple(args))
    groups: dict[str, list[dict]] = {}
    inside = 0
    for r in rows:
        if _is_inside_release(r):
            inside += 1
            continue
        key = r["category"] if r["kind"] == "macro" else f"headline/{r.get('category') or 'other'}"
        groups.setdefault(key, []).append(r)
    out = []
    for key, rs in groups.items():
        jumps = [r for r in rs if r["impulse"]]
        with_p = [r for r in rs if r.get("placebo_impulse") is not None]
        p_jumps = [r for r in with_p if r["placebo_impulse"]]
        mf, p_mf = [r for r in rs if r.get("moved_first") is not None], [r for r in rs if r.get("placebo_moved_first") is not None]
        p_ret = [r for r in p_jumps if r.get("placebo_retraced") is not None]
        p_vol = [r for r in p_jumps if r.get("placebo_volume_exhausted") is not None]
        agg = {"class": key, "kind": rs[0]["kind"], "n": len(rs), "jumps": len(jumps), "n_p": len(with_p),
               "releases": len({r["ts_utc"] for r in rs}),
               "p_jumps": sum(bool(r["placebo_impulse"]) for r in with_p),
               "typical_pct": _median([abs(r["magnitude_pct"]) for r in rs if r["magnitude_pct"] is not None]),
               "placebo_typical_pct": _median([abs(r["placebo_magnitude_pct"]) for r in with_p
                                               if r["placebo_magnitude_pct"] is not None]),
               "baseline_pct": _median([r.get("baseline_pct") for r in rs]),
               "propagation_min": _median([r["propagation_min"] for r in jumps]),
               "peak_min": _median([_minutes(r["ts_utc"], r.get("peak_ts")) for r in jumps]),
               "retraced": sum(r.get("correction_delay_min") is not None for r in jumps),
               "retrace_delay_min": _median([r.get("correction_delay_min") for r in jumps]),
               "moved_first": sum(bool(r["moved_first"]) for r in mf), "n_mf": len(mf),
               "p_moved_first": sum(bool(r["placebo_moved_first"]) for r in p_mf), "n_p_mf": len(p_mf),
               "p_retraced": sum(bool(r["placebo_retraced"]) for r in p_ret), "n_p_ret": len(p_ret),
               "volume_exhausted": sum(bool(r.get("volume_exhausted")) for r in jumps),
               "p_volume_exhausted": sum(bool(r["placebo_volume_exhausted"]) for r in p_vol), "n_p_vol": len(p_vol),
               "volume_ratio": _median([r.get("volume_ratio") for r in rs]),
               "inside": inside if rs[0]["kind"] == "headline" else 0}
        agg["jump_rate"] = agg["jumps"] / agg["n"] if agg["n"] else 0.0
        agg["placebo_rate"] = agg["p_jumps"] / agg["n_p"] if agg["n_p"] else None
        agg["lift_points"] = None if agg["placebo_rate"] is None else round(100 * (agg["jump_rate"] - agg["placebo_rate"]))
        toned = [r for r in jumps if r.get("sentiment") is not None and abs(r["sentiment"]) >= 0.3 and r.get("direction")]
        agg["agree"] = (sum((r["sentiment"] > 0) == (r["direction"] > 0) for r in toned), len(toned)) \
            if rs[0]["kind"] == "headline" else None
        out.append(agg)
    out.sort(key=lambda a: (a["kind"] != "macro", -a["n"], a["class"]))
    return out


def verdict(agg: dict) -> str:
    """The scorecard-style verdict for one class: none under MIN_VERDICT events; then whether these events jump more,
    less or no differently than the same hour on normal days (95% ranges), and whether a 15-minute desk had a look
    before the jumps registered - with the extreme's timing, because a fast jump is usually followed by a slow drift."""
    if agg["n"] < MIN_VERDICT:
        return f"no verdict yet ({agg['n']} of {MIN_VERDICT} events)"
    if not agg["n_p"]:
        return "no verdict: no normal-day matches to compare with"
    lo_e, hi_e = wilson(agg["jumps"], agg["n"])
    lo_p, hi_p = wilson(agg["p_jumps"], agg["n_p"])
    rates = f"{agg['jump_rate']:.0%} vs {agg['placebo_rate']:.0%}"
    if lo_e > hi_p:
        text = f"Prices jump more around these events than at the same hour on a normal day ({rates})"
    elif hi_e < lo_p:
        text = f"Prices jump less around these events than at the same hour on a normal day ({rates})"
    else:
        text = f"no clear difference from a normal hour ({rates}; the ranges overlap)"
    p, k = agg.get("propagation_min"), agg.get("peak_min")
    if p is not None:
        if p <= 15:
            text += (f". The jumps registered after a median {p:.0f} min: a desk that checks every 15 minutes sees them "
                     f"only afterwards")
        else:
            text += (f". The jumps took a median {p:.0f} min to register: a desk that checks every 15 minutes had at least "
                     f"one look before they did; whether acting then would have paid is not measured here")
        if k is not None:
            text += f"; the extreme came after a median {k:.0f} min"
    return text


def _normal(k: int, n: int, what: str) -> str:
    """' (normal days: 2 of 9)', the matched normal days' share next to an event share, or nothing without any."""
    return f" ({what}: {k} of {n})" if n else ""


def moved_first_text(agg: dict) -> str:
    """'price moved first in 4 of 40 (normal hours: 4 of 39)': with the normal-day share, or, while the class has
    none, the calibration's rate (the rule fires on about one normal hour in ten)."""
    text = f"price moved first in {agg['moved_first']} of {agg['n_mf']}"
    if agg["n_p_mf"]:
        return text + _normal(agg["p_moved_first"], agg["n_p_mf"], "normal hours")
    return text + f" (about 1 normal hour in 10 also shows this: {CALIBRATION['moved_first_pct']:g}% in the calibration)"


def category_sentence(agg: dict) -> str:
    """One line of the 'By category' list. Every share stands next to the same share on the matched normal days."""
    n_text = f"{agg['n']} event{'s' if agg['n'] != 1 else ''}"
    if agg["kind"] == "macro":   # one release gives one event per coin: say how many releases they come from
        n_text = (f"{agg['n']} coin-event{'s' if agg['n'] != 1 else ''} from {agg['releases']} "
                  f"release{'s' if agg['releases'] != 1 else ''}")
    if agg["n_p"] != agg["n"]:
        n_text += f" ({agg['n_p']} with a normal-day match)"
    if agg["placebo_rate"] is None:
        rate = f"jump in {agg['jump_rate']:.0%} (no normal-day match yet)"
    else:
        rate = (f"jump in {agg['jump_rate']:.0%} vs {agg['placebo_rate']:.0%} on those normal days "
                f"({agg['lift_points']:+d} points)")
    parts = [f"{agg['class']} · {n_text} · {rate}"]
    if agg["typical_pct"] is not None:
        typical = f"typical largest 2-hour move {agg['typical_pct']:.1f}%"
        if agg["placebo_typical_pct"] is not None:
            typical += f" vs {agg['placebo_typical_pct']:.1f}%"
        parts.append(typical)
    if agg["jumps"]:
        timing = f"the jump registered after a median {agg['propagation_min']:.0f} min"
        if agg["peak_min"] is not None:
            timing += f"; the extreme came after a median {agg['peak_min']:.0f} min"
        parts.append(timing)
        give = f"{agg['retraced']} of {agg['jumps']} jumps gave back {RETRACE_PCT} inside the window"
        if agg["retrace_delay_min"] is not None:
            give += f", a median {agg['retrace_delay_min']:.0f} min after the extreme"
        give += (_normal(agg["p_retraced"], agg["n_p_ret"], "jumps on the matched normal days")
                 or " (no jump on the matched normal days to compare yet)")
        parts.append(give)
    if agg["kind"] == "headline":
        if agg["n_mf"]:
            parts.append(moved_first_text(agg))
        if agg["agree"] and agg["agree"][1]:
            parts.append(f"the desk's news tone agreed with the jump's direction in {agg['agree'][0]} of {agg['agree'][1]} jumps")
        if agg["inside"]:
            parts.append(f"{agg['inside']} burst{'s' if agg['inside'] != 1 else ''} within 4 hours of a release, not counted")
    elif agg["jumps"]:
        vol = f"volume dried up after {agg['volume_exhausted']} of {agg['jumps']} jumps"
        vol += (_normal(agg["p_volume_exhausted"], agg["n_p_vol"], "jumps on the matched normal days")
                or " (no jump on the matched normal days to compare yet)")
        if agg["class"] == "macro:fomc":
            vol += f"; {FOMC_VOLUME}"
        parts.append(vol)
    v = verdict(agg)
    parts.append(v if v.startswith("no verdict") else "Verdict: " + v)
    return " · ".join(parts)


# ---- status pieces ---------------------------------------------------------------------------------------------
def next_release(j: Journal, now: dt.datetime | None = None) -> dict | None:
    now = now or dt.datetime.now(dt.timezone.utc)
    row = j.db.execute("SELECT ts_utc, name, kind FROM macro_calendar WHERE ts_utc > ? ORDER BY ts_utc LIMIT 1",
                       (now.isoformat(timespec="seconds"),)).fetchone()
    return dict(row) if row else None


def calendar_status(j: Journal) -> str:
    """'calendar runs to 10 Dec 2027 · next release US jobs report Fri 6 Nov 13:30 UTC' (or 'no release calendar')."""
    last = j.db.execute("SELECT MAX(ts_utc) FROM macro_calendar").fetchone()[0]
    if not last:
        return "no release calendar loaded"
    d = _dt(last)
    text = f"calendar runs to {d.day} {d:%b %Y}"
    nxt = next_release(j)
    if nxt:
        text += f" · next release {release_name(nxt['kind'], nxt['name'])} {_when(nxt['ts_utc'])}"
    else:
        text += " · no release ahead: refresh the calendar"
    return text


def status_numbers(j: Journal, s, now: dt.datetime | None = None) -> dict:
    """The study's counters: requests used today and allowed, the unmeasured events split into "open" (their 4-hour
    window is still running) and "queued" (complete, waiting for the per-check limit or the budget, the same cut-off
    the settle step uses), dropped and settled events, and the end of a pause after a failed download (or None)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rec = j.get("events_budget") or {}
    used = int(rec.get("used", 0)) if rec.get("day") == now.date().isoformat() else 0
    cutoff = iso(int(now.timestamp() * 1000) - (POST_MIN + 2) * MIN_MS)
    waiting = j.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NULL").fetchone()[0]
    queued = j.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NULL AND ts_utc <= ?",
                          (cutoff,)).fetchone()[0]
    on_record = j.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NOT NULL").fetchone()[0]
    retry = j.get("events_retry_at")
    paused = retry if retry and ms_of(retry) > now.timestamp() * 1000 else None
    return {"used": used, "budget": max(0, int(getattr(s, "events_daily_requests", 0))), "waiting": waiting,
            "open": waiting - queued, "queued": queued, "dropped": int(j.get("events_dropped") or 0),
            "on_record": on_record, "paused_until": paused}


def _events(n: int) -> str:
    return f"{n} event{'s' if n != 1 else ''}"


def _footer(j: Journal, s) -> str:
    n = status_numbers(j, s)
    text = f"Requests today: {n['used']} of {n['budget']}"
    if n["used"] + REQUESTS_PER_EVENT > n["budget"] and n["queued"]:
        text += f" (used up; {_events(n['queued'])} complete and queued: measuring resumes at 00:00 UTC)"
    elif n["queued"]:
        text += (f" · {_events(n['queued'])} complete and queued for measurement ({SETTLE_PER_CHECK} per check, up to "
                 f"{n['budget'] // REQUESTS_PER_EVENT} a day)")
    if n["paused_until"]:
        text += f" · downloads paused after a failure until {n['paused_until'][11:16]} UTC"
    if n["open"] or not n["queued"]:
        text += f" · {_events(n['open'])} " + ("waits for its" if n["open"] == 1 else "wait for their") + " window to complete"
    text += f" · {n['dropped']} dropped unmeasured"
    if n["dropped"]:
        text += " (the budget never reached them: raise EVENTS_DAILY_REQUESTS)"
    return text + f" · {calendar_status(j)} · full record in the notebook (EVENTS.md)."


# ---- /events ---------------------------------------------------------------------------------------------------
USAGE = ("Usage: /events (the latest events and every category), /events macro (the scheduled releases, every coin), "
         "/events btc (one coin).")


def events_text(j: Journal, s, arg: str = "") -> str:
    """The /events answer: the latest measured events in plain words, the aggregate per category with the normal-day
    comparison and the verdict, and the budget and calendar line. /events macro groups by release; /events btc shows
    one coin. Built to stay under 3,500 characters (the list is cut, never the footer)."""
    if getattr(s, "trend", False):
        return EVENTS_TREND
    if getattr(s, "events_daily_requests", 0) <= 0:
        return "The event study is switched off (EVENTS_DAILY_REQUESTS=0)."
    if not usable_budget(s):
        return (f"The event study is off: EVENTS_DAILY_REQUESTS={s.events_daily_requests} is below one measured event "
                f"({REQUESTS_PER_EVENT} downloads), so nothing could be measured. Use 0 (off) or at least "
                f"{REQUESTS_PER_EVENT}.")
    arg = (arg or "").strip().lower()
    symbol = None
    if arg and arg != "macro":
        if not arg.isalnum() or len(arg) > 10:
            return USAGE
        symbol = arg.upper().removesuffix("USDT") + "USDT"
        known = j.db.execute("SELECT 1 FROM news_events WHERE symbol=? LIMIT 1", (symbol,)).fetchone()
        if not known:
            return f"No events on record for {_coin(symbol)}. " + USAGE
    title = "📰 Events: what prices did around scheduled releases and headline bursts (measured after the fact, not predicted)"
    settled_any = j.db.execute("SELECT 1 FROM news_events WHERE settled_at IS NOT NULL LIMIT 1").fetchone()
    if not settled_any:
        nxt = next_release(j)
        when = f" (the next is {release_name(nxt['kind'], nxt['name'])}, {_when(nxt['ts_utc'])})" if nxt else ""
        return (title + f"\n\nNothing measured yet. Releases are measured {POST_MIN // 60} hours after they happen{when}; "
                f"headline bursts {POST_MIN // 60} hours after their first headline, once the news agent has read the "
                "feeds.\n\n" + _footer(j, s))
    footer = "\n\n" + _footer(j, s)
    if arg == "macro":
        body = ["Scheduled releases, every coin (* = a jump by the rule; normal days = the same hour on the matched days):"]
        releases = [r[0] for r in j.db.execute("SELECT DISTINCT ts_utc FROM news_events WHERE kind='macro' "
                                               "ORDER BY ts_utc DESC LIMIT 12")]
        for ts in releases:
            body.append("• " + release_sentence(_rows(j, "kind='macro' AND ts_utc=?", (ts,))))
        aggs = category_table(j, "macro")
    else:
        where = "settled_at IS NOT NULL" + (" AND symbol=?" if symbol else "")
        latest = _rows(j, where, (symbol,) if symbol else (), limit=8 if symbol else 6)
        body = ["Latest measured" + (f" for {_coin(symbol)}" if symbol else "") + ":"] + ["• " + event_sentence(e) for e in latest]
        aggs = category_table(j, None, symbol)
    cats = ["By category (events on record · jump rate, events vs the same hour on normal days · typical largest "
            "2-hour move · timing · verdict):"] + ["• " + category_sentence(a) for a in aggs]
    if not aggs:
        cats = ["By category: nothing measured yet for this selection."]
    text = title + "\n\n" + "\n".join(body) + "\n\n" + "\n".join(cats)
    while len(text) + len(footer) > TEXT_LIMIT and len(body) > 2:
        body.pop()
        text = title + "\n\n" + "\n".join(body) + "\n\n" + "\n".join(cats)
    while len(text) + len(footer) > TEXT_LIMIT and len(cats) > 2:
        cats.pop()
        text = title + "\n\n" + "\n".join(body) + "\n\n" + "\n".join(cats)
    if len(text) + len(footer) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT - len(footer) - 1] + "…"
    return text + footer


# ---- the daily report line -------------------------------------------------------------------------------------
def events_line(j: Journal, today: str) -> str:
    """One line for the daily report: what was measured yesterday (releases first), the record's size, the next
    release when nothing was measured; an empty string when nothing is on record at all."""
    on_record = j.db.execute("SELECT COUNT(*) FROM news_events WHERE settled_at IS NOT NULL").fetchone()[0]
    if not on_record:
        return ""
    day = dt.date.fromisoformat(today)
    yesterday = (day - dt.timedelta(days=1)).isoformat()
    rows = _rows(j, "settled_at IS NOT NULL AND impulse IS NOT NULL AND substr(settled_at, 1, 10)=?", (yesterday,))
    tail = f" · {on_record} on record · /events"
    if not rows:
        nxt = next_release(j)
        return ("📰 Events: nothing new measured yesterday" + tail
                + (f" · next release {release_name(nxt['kind'], nxt['name'])} {_when(nxt['ts_utc'])}" if nxt else ""))
    macro = [r for r in rows if r["kind"] == "macro"]
    bursts = [r for r in rows if r["kind"] != "macro"]

    def bursts_text() -> str:
        with_p = [r for r in bursts if r.get("placebo_impulse") is not None]
        text = (f"{len(bursts)} headline burst{'s' if len(bursts) != 1 else ''}"
                + (" measured yesterday" if not macro else "") + f", jumps in {sum(bool(r['impulse']) for r in bursts)}")
        if with_p:
            text += f" (normal-day rate {sum(bool(r['placebo_impulse']) for r in with_p)} in {len(with_p)})"
        return text
    if not macro:
        return "📰 Events: " + bursts_text() + tail
    # the newest release with its date (the back-fill after an install or an outage measures old releases), and how
    # many releases the coin-events come from, so the counts match the coins listed
    first = macro[0]
    same = [r for r in macro if r["ts_utc"] == first["ts_utc"]]
    releases = len({r["ts_utc"] for r in macro})
    coins = ", ".join(f"{_coin(r['symbol'])} {r['magnitude_pct']:+.1f}%" for r in sorted(same, key=lambda r: r["symbol"])[:4])
    with_p = [r for r in same if r.get("placebo_impulse") is not None]
    rel = (f"{release_name(first['category'].removeprefix('macro:'), first['label'])} {_when(first['ts_utc'])}: {coins} "
           f"within 2 h; jumps in {sum(bool(r['impulse']) for r in same)} of {len(same)}")
    if with_p:
        rel += f", on the matched normal days {sum(bool(r['placebo_impulse']) for r in with_p)} of {len(with_p)}"
    if releases > 1:
        rel = f"{len(macro)} coin-events of {releases} releases, the newest {rel}"
    text = f"📰 Events: {len(rows)} measured yesterday ({rel}" + (f"; {bursts_text()}" if bursts else "")
    return text + ")" + tail


# ---- the notebook page -----------------------------------------------------------------------------------------
def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_none yet_"
    esc = lambda c: str("" if c is None else c).replace("|", "\\|").replace("\n", " ").strip()  # noqa: E731
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _pct(v, digits: int = 2) -> str:
    return "" if v is None else f"{v:.{digits}f}%"


def _yes(v) -> str:
    return "" if v is None else ("yes" if v else "no")


def events_md(j: Journal, s) -> str:
    """EVENTS.md: what the page is, what a normal day is, the fixed parameters, the aggregate table, the releases and
    the headline bursts (newest 60 each), the calendar and the budget."""
    now = dt.datetime.now(dt.timezone.utc)
    n = status_numbers(j, s, now)
    lines = ["# Events: what prices did around scheduled releases and headline bursts", "",
             f"_Generated {now:%Y-%m-%d %H:%M} UTC from the trade journal. Measured after the fact, never predicted._", "",
             *([f"**{EVENTS_TREND}**", ""] if getattr(s, "trend", False) else []),
             "## What this page is", "",
             "Every scheduled US release (CPI, FOMC statement, jobs report) and every burst of headlines about a watched "
             "coin is measured once its window is complete: the largest move within 2 hours and whether it counts as a "
             "jump, how fast the jump registered, the extreme, the give-back, whether volume dried up, and whether the price "
             "had already moved in the hour before. Every event is compared with the same hour on a normal day. Nothing on "
             "this page changes a trade: it is a record the owner reads.", "",
             "## What \"a normal day\" is", "",
             f"The matched normal day is the nearest free day before the event at the same New York clock time (the "
             f"second-nearest for every other event, so a class's matches are not all 'the day before'); within {PLACEBO_DAYS} "
             "days, same weekday class, never a US market holiday, never a Thursday for an 08:30 New York event (weekly "
             "jobless claims). For a release it is a day with no listed release, headlines included: at 08:30 New York most "
             "weekdays carry some second-tier US number, so the comparison is 'top-tier release versus a typical morning'. "
             "For a headline burst it is an hour with no headlines about the coin, which understates the normal rate "
             "(headlines often follow moves), so a headline lift is an upper bound.", "",
             f"The jump rule itself is judged against the coin's normal days too (time of day matters): the baseline is the "
             f"median largest 2-hour move at the same New York clock time over the last {BASELINE_DAYS} normal days of the "
             f"same kind (usually 6 or 7 weekdays from {BASELINE_STEP_MIN}-minute candles; weekend days from "
             f"{WEEKEND_STEP_MIN}-minute candles for a weekend event; at least {MIN_BASELINE_DAYS}). A jump is a move of the "
             f"1-minute closes within 2 hours of more than K times that baseline, and at least {FLOOR_PCT:g}%: K = "
             f"{K_WEEKDAY:g} on weekdays, {K_WEEKEND:g} at weekends (weekend prices sit still for hours, then move at once). "
             f"The baseline is the rule's yardstick, not what a normal day moves: coarser closes miss part of the "
             f"swings, so it reads lower than the 1-minute moves; the matched normal day's own move, measured exactly "
             f"like the event's, is the like-for-like comparison. "
             f"The matched normal day is judged against its own preceding normal days the same way. Calibrated on real "
             f"prices: the rule calls {CALIBRATION['study_placebo_pct']:g}% of the 189 normal weekday windows of a 63-release "
             f"study a jump (08:30 and 14:00 New York), {CALIBRATION['weekday_hours_pct']:g}% of every weekday hour and "
             f"{CALIBRATION['weekend_hours_pct']:g}% of every weekend hour of 62 days. A baseline taken from the hours before "
             "the event was rejected: before 08:30 New York those are the quiet overnight hours while the 2 hours after "
             f"contain the US stock-market open, and that baseline called {CALIBRATION['old_rule_pct']}% of normal mornings "
             "jumps.", "",
             "## Fixed parameters", "",
             _table(["parameter", "value", "meaning"], [
                 ["noise window", "-360 to -60 min", "the coin's ordinary minute-to-minute movement before anything happened (recorded as baseline_atr)"],
                 ["moved-first window", f"-{QUIET_MIN} to 0 min", "a jump here, by the same rule, means the price moved before the headline"],
                 ["impulse window", f"0 to +{IMPULSE_MIN} min", "the jump, its direction (the sign of the largest move) and the extreme"],
                 ["correction window", f"extreme to +{POST_MIN} min", f"the first {RETRACE_PCT} give-back ('retrace', a parameter: no claim beyond the number) and volume exhaustion (10 minutes below the pre-event median)"],
                 ["K, floor", f"{K_WEEKDAY:g} on weekdays, {K_WEEKEND:g} at weekends; {FLOOR_PCT:g}%", f"a jump exceeds K x the normal-day baseline and the floor; K set from normal windows of real prices (about one in ten flagged: {CALIBRATION['study_placebo_pct']:g}% at the release hours)"],
                 ["baseline days", f"up to {BASELINE_DAYS}, at least {MIN_BASELINE_DAYS}", f"same weekday class, same New York clock time, no listed release, no US market holiday; {BASELINE_STEP_MIN}-minute candles ({WEEKEND_STEP_MIN} on weekends), 1,000 of them in one download"],
                 ["burst", f"{BURST_MINUTES} min", "headlines about a coin within 30 minutes of the first form one event, anchored to the first"],
                 ["placebo", f"within {PLACEBO_DAYS} days", "see above"],
                 ["budget", f"{n['budget']} requests a day", f"{REQUESTS_PER_EVENT} per measured coin-event, at most {SETTLE_PER_CHECK} coin-events per check (EVENTS_DAILY_REQUESTS; 0 = off)"],
                 ["kept", f"headlines {KEEP_DAYS} days, events {KEEP_EVENT_DAYS} days", f"unmeasured headline events are dropped after {WAIT_DAYS} days; releases wait as long as it takes; the last {BACKFILL_DAYS} days of releases are back-filled at install"],
             ]), "",
             "## By category", "",
             "Jump rate: events vs the same hour on their matched normal days; typical move: the median largest 2-hour move. "
             "'Gave back' and 'moved first' show the matched normal days' share in brackets (the moved-first rule fires "
             f"on about 1 normal hour in 10). No verdict under {MIN_VERDICT} events per class.", ""]
    aggs = category_table(j)
    lines.append(_table(["class", "events", "with match", "jump rate", "normal-day rate", "lift (points)", "typical move",
                         "normal-day typical", "median min to register", "median min to extreme", "gave back",
                         "moved first", "tone agreed", "verdict"],
                        [[a["class"], a["n"], a["n_p"], f"{a['jump_rate']:.0%}",
                          "" if a["placebo_rate"] is None else f"{a['placebo_rate']:.0%}",
                          "" if a["lift_points"] is None else f"{a['lift_points']:+d}", _pct(a["typical_pct"], 1),
                          _pct(a["placebo_typical_pct"], 1),
                          "" if a["propagation_min"] is None else f"{a['propagation_min']:.0f}",
                          "" if a["peak_min"] is None else f"{a['peak_min']:.0f}",
                          (f"{a['retraced']} of {a['jumps']}" + (f" (normal days: {a['p_retraced']} of {a['n_p_ret']})"
                                                                   if a["n_p_ret"] else "")) if a["jumps"] else "",
                          (f"{a['moved_first']} of {a['n_mf']}" + (f" (normal hours: {a['p_moved_first']} of {a['n_p_mf']})"
                                                                  if a["n_p_mf"] else " (normal hours: about 1 in 10)"))
                          if a["kind"] == "headline" and a["n_mf"] else "",
                          f"{a['agree'][0]} of {a['agree'][1]}" if a["agree"] and a["agree"][1] else "",
                          verdict(a)] for a in aggs]))
    lines += ["", "## Releases (newest first)", ""]
    rel = _rows(j, "kind='macro'", (), limit=60)
    lines.append(_table(["release", "time UTC", "coin", "jump", "largest move", "registered after", "extreme after",
                         "gave back after", "volume dried up", "normal day", "normal-day move", "normal-day jump",
                         "rule baseline (15-min closes)", "volume x normal", "note"],
                        [[r["label"], r["ts_utc"][:16].replace("T", " "), _coin(r["symbol"]), _yes(r["impulse"]),
                          _pct(r["magnitude_pct"]), r["propagation_min"], _minutes(r["ts_utc"], r.get("peak_ts")),
                          r["correction_delay_min"], _yes(r["volume_exhausted"]),
                          (r["placebo_ts"] or "")[:10], _pct(r["placebo_magnitude_pct"]), _yes(r["placebo_impulse"]),
                          _pct(r["baseline_pct"]), r.get("volume_ratio"), r["note"] or ("waiting" if r["settled_at"] is None else "")]
                         for r in rel]))
    lines += ["", "## Headline bursts (newest first)", ""]
    bursts = _rows(j, "kind='headline'", (), limit=60)
    lines.append(_table(["published UTC", "coin", "category", "headlines", "velocity", "desk's tone", "first headline",
                         "jump", "largest move", "registered after", "moved first", "normal day", "normal-day move",
                         "normal-day jump", "rule baseline (15-min closes)", "note"],
                        [[r["ts_utc"][:16].replace("T", " "), _coin(r["symbol"]), r["category"], r["n_headlines"],
                          r["velocity"], "" if r["sentiment"] is None else f"{r['sentiment']:+.2f}", _label(r)[:80],
                          _yes(r["impulse"]), _pct(r["magnitude_pct"]), r["propagation_min"], _yes(r["moved_first"]),
                          (r["placebo_ts"] or "")[:10], _pct(r["placebo_magnitude_pct"]), _yes(r["placebo_impulse"]),
                          _pct(r["baseline_pct"]), r["note"] or ("waiting" if r["settled_at"] is None else "")]
                         for r in bursts]))
    cal = j.get("events_calendar") or {}
    coming = [dict(r) for r in j.db.execute("SELECT ts_utc, name, kind FROM macro_calendar WHERE ts_utc > ? ORDER BY ts_utc LIMIT 3",
                                            (now.isoformat(timespec="seconds"),))]
    lines += ["", "## Calendar and budget", "",
              "- Next releases: " + ("; ".join(f"{c['name']} ({_when(c['ts_utc'])})" for c in coming) or "none listed"),
              f"- {calendar_status(j)}; shipped file: trader/macro_calendar.json (official sources named inside: BLS release "
              f"schedules, the Fed's FOMC calendar); the owner's additions: data/macro_calendar.json"
              + (f"; problems: {'; '.join(cal['problems'])}" if cal.get("problems") else ""),
              f"- Requests today: {n['used']} of {n['budget']} · events waiting for their window: {n['open']} · complete, "
              f"queued for measurement: {n['queued']} · dropped unmeasured: {n['dropped']} · settled on record: "
              f"{n['on_record']}" + (f" · downloads paused after a failure until {n['paused_until'][11:16]} UTC"
                                     if n["paused_until"] else "")]
    return "\n".join(lines)
