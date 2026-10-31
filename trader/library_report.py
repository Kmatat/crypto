"""The strategy library's texts: the Telegram summary, the terminal table, the notebook page LIBRARY.md, the diary
line and the command replies.

Wording rules (the library measures the past; it does not forecast): "measured on past prices", never that a rule
"will" do something; luck as "a result this good would turn up by luck about k times in 100", never "k% chance it is
luck"; a rule that passes is "a candidate", never "a better strategy"; every number comes from the report.
"""
from __future__ import annotations

import datetime as dt
import time

from . import library_stats as st

HELP_LINE = ("/library [last|nohourly] - published trading rules (Turtle breakout, momentum, moving averages, RSI(2), Bollinger, "
             "MACD, Williams) and the desk's own two, replayed on years of real prices under one fixed test: settings "
             "chosen on older years, judged on newer ones, checked for luck. A measurement of the past; it trades "
             "nothing. Takes 20-40 min; 'last' shows the latest result, 'nohourly' leaves out the desk's hourly rules (less "
             "memory, about a third of the time)")
BUSY_TEXT = ("📚 A strategy library run started on the server (desk library) is still going. Send /library last when it "
             "has finished.")
TIMEOUT_TEXT = ("⚠️ The strategy library was still running after 4 hours and was stopped. Nothing on the desk changed. "
                "On the server, desk library shows its progress.")
SUMMARY_MAX = 3900     # Telegram allows 4,096 characters; /library last sends one message
NO_RUN_TEXT = "No library run yet. Send /library to run it (20-40 minutes)."
OFFLINE_TEXT = ("The strategy library needs Binance's public prices, which offline mode does not read. On a computer: "
                "python -m trader library")
CAVEATS = (
    "The 8 coins are today's survivors: coins that collapsed (such as LUNA or FTT) are not in the test. This flatters "
    "both the rules and holding, so compare the two rather than reading the absolute returns.",
    "Long-only spot trading: no shorting, no leverage (the desk cannot do either).",
    "Costs are a model: 0.1% fee per order, 0.05% slippage on market orders at the open, 0.2% on stop orders. Real "
    "slippage on thin coins can be worse.",
    "Binance prices only.",
    "Sharpe ratios are shown in yearly terms (x sqrt(365)) for display; the luck test uses the daily figures.",
    "A pass means the rule beat holding and cash on past prices and that its gain over cash is unlikely to be luck. "
    "It is not a forecast.",
    "The luck check is against cash only: the margin over holding is measured, not luck-checked.",
    "Every re-run is another look at mostly the same years: a rule that passes in one run and fails in others is not "
    "evidence (see 'Earlier runs' and 'passed in p of r runs').",
    "Most rules were published before crypto prices existed (1970-2012) or on other markets, so their authors could "
    "not fit them to these prices. Two exceptions: the momentum look-backs of 2 and 4 weeks come from a 2021 crypto "
    "study (Liu and Tsyvinski) whose data runs to May 2018, overlapping the first months tested, and the moving-average "
    "entry also cites a 2019 crypto study. And which published rules to include was chosen in 2026 by people who know "
    "that trend rules have been popular in crypto, a selection the luck adjustment cannot count.",
    "Each walk-forward segment starts flat and sells at its end, so a rule that stays invested pays an extra round trip "
    "of costs per segment; those closes do not count as the rule's own trades. The holding yardstick restarts the same "
    "way, as declared before the first run, so in the walk-forward it is the coins re-split equally every 6 months, not "
    "coins bought once: the re-split sells some of the winners and buys more of the laggards at every segment start, "
    "which can give a very different record. The verdicts use the re-split basket (the same restarts as the rules); "
    "the 'bought once' row is shown for information.",
    "The Williams fractions (0.5, 0.75, 1.0) are not from his book (secondary descriptions); the momentum look-backs of "
    "2 and 4 weeks are this library's reading of the crypto paper (which found that one week's return predicts the next "
    "1 to 4 weeks).",
    "Daily rules confirm a breakout on the day's close and buy at the next open (the Turtles bought during the day).",
)
CONDITIONS = (
    ("an eligible setting exists in the first 70%", f"at least {st.MIN_TRADES_PER_YEAR} rule trades a year",
     "otherwise nothing was chosen"),
    ("rule trades in the judged period", f"at least {st.MIN_HOLDOUT_TRADES}", "fewer is a handful of positions"),
    ("rule trades in the walk-forward", f"at least {st.MIN_WF_TRADES}",
     "the long record must hold real evidence; the closes at segment ends do not count"),
    ("judged-period Sharpe and return", "both above 0", "beats cash after costs"),
    ("judged-period Sharpe", "above holding's", "beats buy-and-hold, risk-adjusted (a direct comparison)"),
    ("walk-forward Sharpe and return", "both above 0", "beats cash after costs"),
    ("walk-forward Sharpe", "above holding's (the coins re-split equally at each segment's start)",
     "beats holding, restarted like the rule, over every segment's choice (a direct comparison)"),
    ("worst drop (judged period and walk-forward)", "not deeper than holding's",
     "a candidate must not need a worse tail than simply holding"),
    ("deflated Sharpe ratio of the walk-forward", f"at least {st.LUCK_LEVEL:g} (luck 5 in 100 or fewer)",
     "its gain over cash survives the luck adjustment for every variant tried"),
)


# ---- small formats ----
def _pct(x, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def _num(x, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _drop(x) -> str:
    return "n/a" if x is None else f"{x:.0f}%"


def params_text(p: dict | None) -> str:
    """{'entry': 20, 'exit': 10} -> 'entry 20 exit 10'; the MACD zero filter in words."""
    if not p:
        return "cash"
    out = []
    for k, v in p.items():
        if isinstance(v, bool):
            if v:
                out.append("above zero")
        else:
            out.append(f"{k} {v:g}")
    return " ".join(out)


def skipped_text(kind: str, free: float | None = None) -> str:
    """Why the desk's hourly reference was not run."""
    if kind == "memory":
        return ("skipped: the hourly rules' replay uses about 300 MB, so it runs only with at least 400 MB of free memory, "
                f"and this server had {free:.0f} MB free when it was its turn; it can be run on a computer with python -m "
                "trader library")
    if kind == "prices":
        return "skipped: the hourly prices of every coin were not available"
    return "skipped (--no-hourly)"


def start_text(first_run: bool) -> str:
    return ("📚 Strategy library started: 7 published trading rules and the desk's own two, on real Binance prices of 8 "
            "coins since 2018, under one fixed test. It runs at the lowest priority beside the desk, so the market checks "
            "come first, and takes about 20-40 minutes on this server"
            + (" (this first run also downloads about 50 MB of prices)" if first_run else "")
            + ". The result arrives here. It measures the past and trades nothing.")


def running_text(started_at: float) -> str:
    minutes = max(0, int((time.time() - started_at) // 60))
    return (f"📚 The strategy library is already running (started {minutes} min ago). The result arrives here when it "
            "finishes; /library last shows the previous one.")


def failure_text(log_tail: str) -> str:
    return (f"⚠️ The strategy library stopped with an error: {log_tail or 'no message'}. Nothing on the desk changed. "
            "The price files finished so far are kept, so the next /library does not download them again (the test "
            "itself starts from the beginning).")


def killed_text(signal_no: int, log_tail: str) -> str:
    """The child was stopped by a signal. A kill (9) on this server is almost always the kernel freeing memory (the
    library is set to be the first process it stops, so the desk keeps running); a stop (15) is usually the desk
    itself restarting or updating, which stops its children."""
    where = f" It was at: {log_tail}." if log_tail else ""
    if signal_no == 9:
        why = ("on this server that is almost always because memory ran out, and the library is set to be stopped "
               "first so the desk keeps running")
        retry = ("Try /library nohourly (it leaves out the hourly rules, the part that needs the most memory), or run "
                 "it later.")
    else:
        why = "usually because the desk was restarting or updating, which stops the library too"
        retry = "Send /library again once the desk is back."
    return (f"⚠️ The server stopped the strategy library (signal {signal_no}); {why}.{where} Nothing on the desk "
            f"changed. {retry}")


def _families(report: dict) -> list[dict]:
    return [e for e in report.get("entries", []) if e.get("kind") == "family"]


def _refs(report: dict) -> list[dict]:
    return [e for e in report.get("entries", []) if e.get("kind") == "reference"]


def _yard(report: dict, key: str, part: str) -> dict:
    return ((report.get("yardsticks") or {}).get(key) or {}).get(part) or {}


def _coins(report: dict) -> str:
    return " ".join(u["symbol"].removesuffix("USDT") for u in report.get("universe", []))


def _run_numbers(report: dict) -> str:
    minutes = None
    try:
        a = dt.datetime.fromisoformat(report["started_at"])
        b = dt.datetime.fromisoformat(report["finished_at"])
        minutes = (b - a).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        if report.get("seconds") is not None:
            minutes = report["seconds"] / 60
    wall = "n/a" if minutes is None else _duration(minutes * 60)
    cpu = report.get("cpu_seconds")
    return (f"Run: {wall}, CPU {'n/a' if cpu is None else _duration(cpu)}, memory {report.get('peak_mb', 'n/a')} "
            f"MB, {report.get('requests', 0)} price downloads")


def _duration(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


def _luck_short(e: dict) -> str:
    d = e.get("dsr")
    if d is None:
        return "luck n/a"
    return "luck under 1 in 100" if 100 * (1 - d) < 1 else f"luck about {st.luck_in_100(d)} in 100"


def _judged(e: dict, report: dict) -> str:
    ho, wf = e.get("holdout") or {}, e.get("walk_forward") or {}
    bh, bw = _yard(report, "hold", "holdout"), _yard(report, "hold", "walk_forward")
    return (f"judged period {_pct(ho.get('return_pct'), 0)} (Sharpe {_num(ho.get('sharpe'))} vs holding "
            f"{_num(bh.get('sharpe'))}), walk-forward {_num(wf.get('sharpe'))} vs {_num(bw.get('sharpe'))}, "
            f"{wf.get('rule_trades', 0)} trades")


def _family_line(e: dict, report: dict, short: bool = False) -> str:
    name = str(e.get("name", e.get("key")))
    head = f"{name}, {e.get('cite', '')}" if name.endswith(")") else f"{name} ({e.get('cite', '')})"
    reasons = e.get("reasons") or []
    if short:
        head = str(e.get("short") or name)
        if e.get("verdict") == "PASS":
            return f"✅ {head}: PASS, a candidate only, {_luck_short(e)}"
        return f"❌ {head}: " + (reasons[0] if reasons else "no result")[:160]
    if e.get("verdict") == "PASS":
        hist = (report.get("pass_history") or {}).get(e.get("key"))
        tail = f"; passed in {hist[0]} of the last {hist[1]} saved runs" if hist else ""
        return f"✅ {head}: PASS, a candidate only: {_judged(e, report)}, {_luck_short(e)}{tail}"
    if e.get("chosen") is None or not e.get("holdout"):
        return f"❌ {head}: {reasons[0] if reasons else 'no result'}"
    first = reasons[0] if reasons else "failed"
    if "would turn up by luck" in first:
        d = e.get("dsr") or 0.0
        first = f"a record this good would turn up by luck {st.luck_words(d)} (a pass needs 5 or fewer)"
    return f"❌ {head}: {_judged(e, report)}; {first}"


def _ref_line(e: dict) -> str:
    if e.get("verdict") == "SKIPPED":
        return f"⏭️ {e.get('name')}: {e.get('note', 'skipped')}"
    full, ho = e.get("full") or {}, e.get("holdout") or {}
    span = "whole period" if e.get("key") == "desk-trend" else "last 3 years"
    return (f"📏 {e.get('name')} (reference): {span} {_pct(full.get('return_pct'), 0)} (Sharpe "
            f"{_num(full.get('sharpe'))}); judged period {_pct(ho.get('return_pct'), 0)} ({_num(ho.get('sharpe'))}); "
            f"{_luck_short(e)} counting the {e.get('trials')} variants its research tried. Its settings were chosen on "
            "some of these years, so this is not a clean test.")


def _passed(report: dict) -> list[dict]:
    return [e for e in _families(report) if e.get("verdict") == "PASS"]


def _result_sentence(report: dict) -> str:
    passed = _passed(report)
    head = ("No published rule passed." if not passed else
            f"{len(passed)} published rule{'s' if len(passed) > 1 else ''} passed: "
            + ", ".join(e.get("name", e.get("key")) for e in passed) + ".")
    return (head + " Nothing changes on the desk: a rule that passes is only a candidate, and running one would be a "
            "separate release you approve. A pass means the rule beat holding and cash on past prices and that its gain "
            "over cash is unlikely to be luck; it does not mean it will continue.")


def summary_text(report: dict) -> str:
    """The Telegram summary (well under 4,000 characters). A report with keys missing still gives a text."""
    try:
        return _summary(report)
    except Exception as e:  # noqa: BLE001 - an old or damaged report must still answer the owner
        return f"📚 The latest library result could not be shown ({e}). Send /library to run it again."


def _summary(report: dict) -> str:
    period = report.get("period") or {}
    ins, hol, segs = report.get("in_sample") or ["?", "?"], report.get("holdout") or ["?", "?"], report.get("segments") or []
    run_day = str(report.get("finished_at") or "")[:10] or "?"
    n_coins = len(report.get("universe", []))
    hold, btc = _yard(report, "hold", "holdout"), _yard(report, "btc", "holdout")
    lines = [
        f"📚 Strategy library · protocol {report.get('protocol', '?')} · run {run_day} · measured on past prices; it "
        "trades nothing",
        f"{n_coins} coins ({_coins(report)}), {period.get('start', '?')} to {period.get('end', '?')}. Settings chosen on "
        f"the first 70% (to {ins[1]}), judged once on the rest ({hol[0]} to {hol[1]}), and walk-forward {len(segs)} "
        "times: 2 years to choose, the next 6 months to test"
        + (f" ({segs[0][2]} to {segs[-1][3]})" if segs else "") + ". Costs: 0.1% fee an order plus slippage. "
        f"{report.get('trials', '?')} variants tried in all.",
        "Sharpe = the yearly return per unit of yearly swing (higher is better); a rule must beat holding and cash on it.",
        "",
        f"Yardsticks in the judged period: holding the {n_coins} coins {_pct(hold.get('return_pct'), 0)} (Sharpe "
        f"{_num(hold.get('sharpe'))}, worst drop {_drop(hold.get('max_drawdown_pct'))}) · Bitcoin alone "
        f"{_pct(btc.get('return_pct'), 0)} ({_num(btc.get('sharpe'))}, {_drop(btc.get('max_drawdown_pct'))}) · cash 0%. "
        f"Walk-forward: holding re-split every 6 months (the bar the rules must beat) Sharpe "
        f"{_num(_yard(report, 'hold', 'walk_forward').get('sharpe'))}; bought once "
        f"{_pct(_yard(report, 'hold', 'walk_forward_once').get('return_pct'), 0)} (Sharpe "
        f"{_num(_yard(report, 'hold', 'walk_forward_once').get('sharpe'))}).",
        "",
    ]
    tail = ["", _result_sentence(report), _run_numbers(report) + ". Full table, sources and caveats: LIBRARY.md (/notes)."]
    if report.get("not_saved"):
        tail.append("⚠️ The journal could not be updated, so /library last and the notebook still show the previous run "
                    "(this result is in data/library/report.json and LIBRARY.md on the server).")
    text = "\n".join(lines + [_family_line(e, report) for e in _families(report)]
                     + [_ref_line(e) for e in _refs(report)] + tail)
    if len(text) > SUMMARY_MAX:   # unusually long reasons: each rule on one shorter line, so it stays one message
        text = "\n".join(lines + [_family_line(e, report, short=True) for e in _families(report)]
                         + [_ref_line(e) for e in _refs(report)] + tail)
    return text


def diary_line(report: dict) -> str:
    fams = _families(report)
    period = report.get("period") or {}
    best = max((e for e in fams if e.get("walk_forward")), key=lambda e: e["walk_forward"]["sharpe_daily"],
               default=None)
    hold = _yard(report, "hold", "walk_forward")
    text = (f"Strategy library (protocol {report.get('protocol')}, {period.get('start')} to {period.get('end')}): "
            f"{len(_passed(report))} of {len(fams)} published rules passed")
    if best:
        text += (f"; best walk-forward: {best['name']}, Sharpe {best['walk_forward']['sharpe']:.2f} against holding's "
                 f"{_num(hold.get('sharpe'))} ({_luck_short(best)})")
    return text + ". Measured on past prices; nothing changed on the desk."


# ---- the terminal table ----
def _row(name, chosen, ho, wf, verdict, luck="-") -> str:
    def cell(m, key, fmt):
        return "-" if not m or m.get(key) is None else fmt(m[key])
    return (f"{name[:26]:<26} {chosen[:36]:<36} {cell(ho, 'return_pct', lambda x: f'{x:+.1f}%'):>8} "
            f"{cell(ho, 'sharpe', lambda x: f'{x:.2f}'):>6} {cell(ho, 'max_drawdown_pct', lambda x: f'{x:.0f}%'):>5} "
            f"{cell(ho, 'rule_trades', str):>6} | {cell(wf, 'return_pct', lambda x: f'{x:+.0f}%'):>7} "
            f"{cell(wf, 'sharpe', lambda x: f'{x:.2f}'):>6} {cell(wf, 'max_drawdown_pct', lambda x: f'{x:.0f}%'):>5} "
            f"{cell(wf, 'rule_trades', str):>6} {luck:>5} | {verdict}")


def table_text(report: dict) -> str:
    """The table `python -m trader library` prints."""
    period = report.get("period") or {}
    segs = report.get("segments") or []
    lines = [
        f"Strategy library, protocol {report.get('protocol')} ({report.get('version', '')}): {_coins(report)}, "
        f"{period.get('start')} to {period.get('end')} ({period.get('days')} days)",
        f"Choose on {report.get('in_sample', ['?', '?'])[0]} to {report.get('in_sample', ['?', '?'])[1]} (70%), judge once "
        f"on {report.get('holdout', ['?', '?'])[0]} to {report.get('holdout', ['?', '?'])[1]}; walk-forward {len(segs)} "
        "segments (2 years to choose, 6 months to test)"
        + (f", {segs[0][2]} to {segs[-1][3]}" if segs else ""),
        f"Costs 0.1% fee + 0.05% slippage (0.2% on stops); {report.get('trials')} variants tried; by luck alone the best "
        f"would show a Sharpe of about {_num(report.get('luck_bar'))} a year",
        "",
        f"{'rule':<26} {'setting chosen':<36} {'judged period: return':>8} {'Sharpe':>6} {'drop':>5} {'trades':>6} | "
        f"{'walk-forward: return':>7} {'Sharpe':>6} {'drop':>5} {'trades':>6} {'luck':>5} | verdict",
    ]
    for key, name in (("hold", f"Hold the {len(report.get('universe', []))} coins"), ("btc", "Bitcoin alone"),
                      ("cash", "Cash")):
        lines.append(_row(name, {"hold": "WF: re-split every 6 months", "btc": "WF: restarted every 6 months"}.get(key, "-"),
                          _yard(report, key, "holdout"), _yard(report, key, "walk_forward"), "yardstick"))
        if _yard(report, key, "walk_forward_once"):
            lines.append(_row(f"{name.split(' the ')[0].split(' alone')[0]} (bought once)", "WF: bought once",
                              None, _yard(report, key, "walk_forward_once"),
                              "walk-forward: bought once, for information (no verdict uses it)"))
    for e in _families(report):
        chosen = params_text((e.get("chosen") or {}).get("params")) if e.get("chosen") else "none eligible"
        why = e.get("verdict", "?") + (": " + e["reasons"][0] if e.get("reasons") else "")
        luck = "-" if e.get("dsr") is None else str(st.luck_in_100(e["dsr"])) if 100 * (1 - e["dsr"]) >= 1 else "<1"
        lines.append(_row(e.get("short") or e.get("name", ""), chosen, e.get("holdout"), e.get("walk_forward"), why,
                          luck))
    for e in _refs(report):
        if e.get("verdict") == "SKIPPED":
            lines.append(_row(e.get("name", ""), "-", None, None, e.get("note", "SKIPPED")))
            continue
        luck = str(st.luck_in_100(e["dsr"])) if 100 * (1 - e["dsr"]) >= 1 else "<1"
        span = "whole period" if e["key"] == "desk-trend" else "last 3 years"
        lines.append(_row(e.get("name", ""), "tested defaults", e.get("holdout"), e.get("full"),
                          f"REFERENCE (right side: its {span}, not a walk-forward)", luck))
    lines += ["", "luck = a record this good would turn up by luck about k times in 100 if no variant had a real edge "
              "(a pass needs 5 or fewer)", _result_sentence(report), _run_numbers(report) + "."]
    return "\n".join(lines)


# ---- the notebook page ----
def _esc(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_none_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _metric_row(name: str, m: dict | None, intraday: bool = False) -> list:
    """One table row. 'Invested' is the average share of the account held at the day's close, which an intraday rule
    (bought and sold within the day) never holds: it is shown as 'intraday' instead of a misleading 0%."""
    m = m or {}
    return [name, _pct(m.get("return_pct")), _pct(m.get("cagr_pct")), _num(m.get("sharpe")),
            _drop(m.get("max_drawdown_pct")), m.get("rule_trades", "-"),
            "n/a" if m.get("win_rate_pct") is None else f"{m['win_rate_pct']:.0f}%", _num(m.get("profit_factor")),
            "intraday" if intraday and m else "n/a" if m.get("invested_pct") is None else f"{m['invested_pct']:.0f}%",
            "n/a" if m.get("fees_pct") is None else f"{m['fees_pct']:.2f}%"]


METRIC_HEADERS = ["Rule", "Return", "A year", "Sharpe", "Worst drop", "Rule trades", "Winners", "Profit factor",
                  "Invested", "Fees"]
NO_RUN_PAGE = ("# Strategy library\n\nNo library run yet: send /library on Telegram or run `desk library` on the "
               "server. It tests published trading rules on years of real prices under one fixed protocol and trades "
               "nothing.\n")


def library_md(j) -> str:
    """LIBRARY.md from the journal's latest library report. Never raises: a damaged or older report gives a short
    page, so the rest of the notebook is still written."""
    try:
        report = j.get("library_report")
        if report is None:
            return NO_RUN_PAGE
        if not isinstance(report, dict) or report.get("protocol") != st.PROTOCOL_VERSION:
            raise ValueError("the stored report is not a protocol-" + str(st.PROTOCOL_VERSION) + " report")
        runs = j.get("library_runs") or []
        return library_page(report, runs if isinstance(runs, list) else [])
    except Exception as e:  # noqa: BLE001 - deliberately broad: the page is a by-product
        return (f"# Strategy library\n\nThe latest library result could not be shown: {e}; send /library to run it "
                "again.\n")


def _choice_table(fams: list[dict], segs: list) -> list[list]:
    """One row per walk-forward segment: each family's chosen setting ('cash' when none was eligible)."""
    rows = []
    for k, seg in enumerate(segs):
        row = [f"{seg[2]} to {seg[3]}"]
        for e in fams:
            choices = e.get("choices") or []
            row.append(params_text(choices[k].get("params")) if k < len(choices) else "-")
        rows.append(row)
    return rows


def library_page(report: dict, runs: list[dict]) -> str:
    """The whole page, from a report (and the list of saved runs)."""
    now = dt.datetime.now(dt.timezone.utc)
    fams, refs = _families(report), _refs(report)
    period = report.get("period") or {}
    ins, hol, segs = report["in_sample"], report["holdout"], report.get("segments") or []
    hold_h, hold_w = _yard(report, "hold", "holdout"), _yard(report, "hold", "walk_forward")
    lines = [
        "# Strategy library: published trading rules, tested honestly", "",
        f"_Generated {now:%Y-%m-%d %H:%M} UTC from the latest run ({report.get('finished_at', '?')}, protocol "
        f"{report.get('protocol')}, version {report.get('version', '?')}). A measurement of past prices; nothing on the "
        "desk changes because of it._", "",
        "## Result in one paragraph", "", _result_sentence(report) + " " + _run_numbers(report) + ".", "",
        "## What was tested", "",
        _table(["Rule", "Source", "Bars", "Settings tried", "The rule in plain words"],
               [[e["name"], f"{e['source']} ({e['url']})", "daily" if e["timeframe"] == "1d" else "4-hour",
                 "; ".join(params_text(p) for p in e.get("grid", [])), e["rule"]] for e in fams]
               + [["Desk, daily trend rule (reference)", "trader/trend.py (README 'The daily trend strategy')", "daily",
                   "the tested defaults (50-day average, volatility target 0.5, 25% emergency stop)",
                   "hold a coin while it closes above its 50-day average, sized by its swing, under the desk's Risk "
                   "Manager and its overall loss brake"],
                  ["Desk, hourly rules (reference)", "trader/backtest.py (the hourly agents' rule core)", "hourly",
                   "the tested default card", "the agents' weighted score, the checklist, trailing stops, 3 slots, under "
                   "the desk's Risk Manager and both loss brakes (news and forex neutral, no Claude)"],
                  ["Hold the coins (yardstick)", "-", "daily", "-", "an equal slice of each coin bought at the start "
                   "(a later coin on its join day), never sold until the end; in the walk-forward it restarts with each "
                   "6-month segment like the rules, so there it is the coins re-split equally every 6 months (shown "
                   "next to the coins bought once)"],
                  ["Bitcoin alone (yardstick)", "-", "daily", "-", "all in Bitcoin at the start"],
                  ["Cash (yardstick)", "-", "-", "-", "doing nothing: 0%"]]), "",
        "## How it was judged", "",
        "Coins and the day each joins the test (210 days after its first Binance candle, so every 200-day average "
        "exists from its first day):", "",
        _table(["Coin", "First daily candle", "Joins"],
               [[u["symbol"].removesuffix("USDT"), u["first"], u["joins"]] for u in report.get("universe", [])]), "",
        f"- Period: {period.get('start')} to {period.get('end')} ({period.get('days')} days). Settings are chosen on "
        f"the first 70% ({ins[0]} to {ins[1]}) and the chosen setting is judged once on the rest, the judged period "
        f"({hol[0]} to {hol[1]}), which the choice never saw.",
        f"- Walk-forward: {len(segs)} segments; each chooses on the 2 years just before it and tests the next 6 months "
        "(a short remainder joins the last segment); the test months are stitched into one record that is entirely out "
        "of sample" + (f" ({segs[0][2]} to {segs[-1][3]})." if segs else "."),
        "- Costs: 0.1% fee per order, 0.05% slippage at the open, 0.2% on stop orders. Decisions on a finished bar's "
        "close, orders at the next bar's open; stops checked on each bar's low.",
        "- Sizing: one slot per coin, each buy at most 10% of the account (never more than the cash); the Turtle "
        "breakout risks at most 1% of the account at its stop; momentum is sized by the coin's swing. The desk's loss "
        "brakes are not applied to the library's rules (they are risk rules of the running desk); the desk's own two "
        "strategies run through their own replays, with the brakes those have.",
        f"- The choice: a setting needs at least {st.MIN_TRADES_PER_YEAR} rule trades a year in its choice window; "
        "among those, the one whose worse half (first or second half of the window) has the higher Sharpe ratio wins; "
        "ties go to the source's headline setting.",
        f"- The luck adjustment (deflated Sharpe ratio, Bailey & Lopez de Prado 2014) counts all "
        f"{report.get('trials')} variants tried: by luck alone, the best of {report.get('trials')} variants would show a "
        f"Sharpe of about {_num(report.get('luck_bar'))} a year. 'Luck about k in 100' means: if none of the variants "
        "had a real edge, a record at least this good would turn up by luck about k times in 100. It is not the chance "
        "that the rule is luck. The check is against cash; beating holding is measured, not luck-checked.", "",
        "A rule passes only if all nine conditions hold:", "",
        _table(["#", "Condition", "Threshold", "Why"], [[k + 1, *c] for k, c in enumerate(CONDITIONS)]), "",
        "## Judged period (the last 30%, judged once)", "",
        _table(METRIC_HEADERS, [_metric_row("Hold the coins", hold_h), _metric_row("Bitcoin alone",
                                _yard(report, "btc", "holdout")), _metric_row("Cash", _yard(report, "cash", "holdout"))]
               + [_metric_row(e["name"], e.get("holdout"), e.get("style") == "intraday") for e in fams]), "",
        "## Walk-forward (every segment's choice, then its unseen 6 months, stitched)", "",
        _table(METRIC_HEADERS + ["DSR", "Luck in 100"],
               [_metric_row("Hold the coins, re-split every 6 months (the bar)", hold_w) + ["-", "-"],
                _metric_row("Hold the coins, bought once (information)", _yard(report, "hold", "walk_forward_once"))
                + ["-", "-"],
                _metric_row("Bitcoin alone (restarted each segment)", _yard(report, "btc", "walk_forward")) + ["-", "-"],
                _metric_row("Bitcoin alone, bought once (information)", _yard(report, "btc", "walk_forward_once"))
                + ["-", "-"],
                _metric_row("Cash", _yard(report, "cash", "walk_forward")) + ["-", "-"]]
               + [_metric_row(e["name"], e.get("walk_forward"), e.get("style") == "intraday")
                  + [_num(e.get("dsr"), 3), _luck_short(e).removeprefix("luck ")] for e in fams]), "",
        "## Verdicts", "",
    ]
    for e in fams:
        if e.get("verdict") == "PASS":
            hist = (report.get("pass_history") or {}).get(e["key"])
            lines.append(f"- **{e['name']}: PASS, a candidate only** (never switched on by this release)"
                         + (f"; passed in {hist[0]} of the last {hist[1]} saved runs" if hist else ""))
        else:
            lines.append(f"- **{e['name']}: FAIL**: " + "; ".join(e.get("reasons") or ["no result"]))
    lines += ["", "## In-sample choices (the first 70%)", ""]
    for e in fams:
        idx = (e.get("chosen") or {}).get("index")
        lines += [f"**{e['name']}** (needs {e.get('min_trades', '?')} rule trades):", "",
                  _table(["Setting", "Sharpe", "First half", "Second half", "Rule trades", "Eligible", "Chosen"],
                         [[params_text(r["params"]), _num(r["sharpe"]), _num(r["halves"][0]), _num(r["halves"][1]),
                           r["trades"], "yes" if r["eligible"] else "no", "✔" if k == idx else ""]
                          for k, r in enumerate(e.get("in_sample_rows", []))]), ""]
    lines += ["## Walk-forward choices", "",
              _table(["Test segment"] + [e.get("short") or e["name"] for e in fams], _choice_table(fams, segs)), "",
              "## The desk's own strategies (references, never candidates)", "",
              "Their settings were chosen by earlier research on years that overlap this period, so their figures are "
              "not a clean test; the luck figure counts the variants that research tried. The daily trend rule is one "
              "continuous replay (its judged-period slice does not start flat) and values what it holds at the last "
              "close; the hourly rules cover only the last 3 years.", ""]
    ref_rows = []
    for e in refs:
        if e.get("verdict") == "SKIPPED":
            ref_rows.append([e["name"], e.get("note", "skipped")] + ["-"] * 6)
            continue
        f, h = e.get("full") or {}, e.get("holdout") or {}
        ref_rows.append([e["name"], f"{e['window'][0]} to {e['window'][1]}", _pct(f.get("return_pct")),
                         _num(f.get("sharpe")), _drop(f.get("max_drawdown_pct")), f.get("rule_trades", "-"),
                         f"{_pct(h.get('return_pct'))} (Sharpe {_num(h.get('sharpe'))})",
                         f"{_num(e.get('dsr'), 3)}, {_luck_short(e).removeprefix('luck ')} ({e.get('trials')} variants)"])
    lines += [_table(["Strategy", "Window", "Return", "Sharpe", "Worst drop", "Trades", "Judged period", "DSR, luck"],
                     ref_rows), "", "## Caveats", ""]
    lines += [f"- {c}" for c in report.get("caveats") or CAVEATS]
    data_rows = []
    for iv, coins in (report.get("data") or {}).items():
        for sym, d in coins.items():
            data_rows.append([sym.removesuffix("USDT"), iv, d["first"], d["last"], f"{d['candles']:,}"])
    lines += ["", "## Data", "",
              f"Binance public candles (data-api.binance.vision), cached in data/library. Downloads this run: "
              f"{report.get('requests', 0)}. " + _run_numbers(report) + ".", "",
              _table(["Coin", "Candles", "First", "Last", "Count"], data_rows), "",
              "## Earlier runs", "",
              _table(["Run", "Protocol", "Last day", "Passed", "Best walk-forward"],
                     [[str(r.get("ts", ""))[:16], r.get("protocol"), r.get("end"), ", ".join(r.get("passed") or [])
                       or "none", "-" if not r.get("best") else f"{r['best']['key']} (Sharpe {r['best']['wf_sharpe']:.2f}, "
                                                                f"luck {r['best']['luck_in_100']} in 100)"]
                      for r in reversed(runs or [])]), "",
              "## Reproduce", "",
              "`python -m trader library` on any computer with the project (on the server `/library` on Telegram, or "
              "`desk library` on servers installed with 1.9.0 or later). The same prices and the same last day give the same numbers (on another computer up to "
              "the last digit of rounding); a run on a later day uses a longer period, so its split dates and numbers "
              "differ."]
    return "\n".join(lines)
