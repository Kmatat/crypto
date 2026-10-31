"""The strategy library's yardstick: how a trading rule is judged (release 1.9.0, protocol 1).

Everything here is plain arithmetic, written down BEFORE any rule was run on real prices, so the bar a rule must clear
cannot be moved after its result is known:

  * the period is split once: settings are chosen on the first 70% of the days, and the chosen setting is judged once
    on the last 30% (the "judged period", which the choice never saw);
  * a walk-forward: choose on 2 years, test on the next 6 months, step 6 months, and stitch the test months together
    into one record that is entirely out of sample;
  * a luck adjustment that counts every variant ever tried (the deflated Sharpe ratio of Bailey & Lopez de Prado,
    2014): the more settings are tried, the better the best one looks by luck alone, so the bar rises with them;
  * nine conditions a rule must ALL meet to be called a candidate (see `verdict`).

Rule for later releases: changing a grid, a threshold, a cost or a rule after a real run is a new protocol. Then
PROTOCOL_VERSION goes up by one and TRIALS_BEFORE grows by the previous trials(), so the luck adjustment keeps counting
every variant ever tried. Re-running the same protocol on newer data adds no variant, but it is another look at mostly
the same years: every saved run is kept and quoted (LIBRARY.md "Earlier runs").

This module imports nothing from the desk: it cannot reach a trading decision.
"""
from __future__ import annotations

import math
import statistics

PROTOCOL_VERSION = 1
TRIALS_BEFORE = 0              # variants tried by earlier protocol versions
SPLIT = 0.70                   # the share of the period used to choose the settings
WF_IN_DAYS, WF_OUT_DAYS, WF_MIN_LAST_DAYS = 730, 182, 91   # walk-forward: choose on 2 years, test 6 months
MIN_TRADES_PER_YEAR = 5        # a setting with fewer rule trades in its choice window is not eligible
MIN_HOLDOUT_TRADES = 30        # rule trades needed in the judged period
MIN_WF_TRADES = 50             # rule trades needed in the stitched walk-forward record
LUCK_LEVEL = 0.95              # the deflated Sharpe ratio a candidate needs (luck 5 in 100 or fewer)
DAYS_PER_YEAR = 365            # crypto trades every day
DESK_TRIALS = {"desk-hourly": 200, "desk-trend": 20}   # variants the desk's own research tried (references only)
EULER_GAMMA = 0.5772156649015329
DAY_MS = 86_400_000

_NORMAL = statistics.NormalDist()


# ---- the calendar ----
def _segments(start: int, days: int) -> list[tuple[int, int, int, int]]:
    """Walk-forward segments (choose_a, choose_b, test_a, test_b), day-start ms, both ends included."""
    rest = days - WF_IN_DAYS
    if rest <= 0:
        return []
    full, left = divmod(rest, WF_OUT_DAYS)
    lengths = [WF_OUT_DAYS] * full
    if left >= WF_MIN_LAST_DAYS:
        lengths.append(left)                    # a long remainder is its own segment
    elif left and lengths:
        lengths[-1] += left                     # a short remainder joins the last segment
    out, test_a = [], start + WF_IN_DAYS * DAY_MS
    for n in lengths:
        out.append((test_a - WF_IN_DAYS * DAY_MS, test_a - DAY_MS, test_a, test_a + (n - 1) * DAY_MS))
        test_a += n * DAY_MS
    return out


def periods(first_join_ms: int, last_day_ms: int) -> dict:
    """The dates of the whole test: from the first coin's join day to the last finished day (both included).
    In-sample = the first floor(70%) days, holdout = the rest, and the rolling walk-forward segments."""
    start = first_join_ms // DAY_MS * DAY_MS
    end = last_day_ms // DAY_MS * DAY_MS
    days = (end - start) // DAY_MS + 1
    n_in = int(math.floor(SPLIT * days))
    return {"start": start, "end": end, "days": days,
            "in_sample": (start, start + (n_in - 1) * DAY_MS),
            "holdout": (start + n_in * DAY_MS, end),
            "segments": _segments(start, days)}


# ---- returns and the Sharpe ratio ----
def daily_returns(run: dict, start_ms: int, end_ms: int) -> list[float]:
    """One return per calendar day of the window: equity at the day's close over the day before (cash days are zero
    returns and count). The day before the window is the run's previous point, or its starting money."""
    prev, out = float(run["base"]), []
    for day, equity in run["curve"]:
        if day < start_ms:
            prev = equity
            continue
        if day > end_ms:
            break
        out.append(equity / prev - 1 if prev > 0 else 0.0)
        prev = equity
    return out


def sharpe(returns: list[float]) -> float:
    """Average daily return per unit of daily swing (sample standard deviation), risk-free rate 0 (USDT earns nothing
    on the desk). 0.0 when the swing is zero or there are fewer than two days."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = math.fsum(returns) / n            # plain floats (statistics.stdev is exact but far slower)
    sd = math.sqrt(math.fsum((x - mean) ** 2 for x in returns) / (n - 1))
    return mean / sd if sd > 0 else 0.0


def annual(sr_daily: float) -> float:
    """A daily Sharpe ratio in yearly terms (x sqrt(365)), for display only: the luck test uses daily figures."""
    return sr_daily * math.sqrt(DAYS_PER_YEAR)


def moments(returns: list[float]) -> tuple[float, float]:
    """Skewness and kurtosis of the daily returns (kurtosis not in excess: a normal curve has 3).
    (0.0, 3.0) when the swing is zero or there are fewer than three returns."""
    n = len(returns)
    if n < 3:
        return 0.0, 3.0
    mean = statistics.fmean(returns)
    m2 = math.fsum((x - mean) ** 2 for x in returns) / n
    if m2 <= 0:
        return 0.0, 3.0
    m3 = math.fsum((x - mean) ** 3 for x in returns) / n
    m4 = math.fsum((x - mean) ** 4 for x in returns) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


# ---- the luck adjustment ----
def psr(sr: float, sr_star: float, t: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe ratio (Bailey & Lopez de Prado 2012): how sure we can be that the true Sharpe ratio is
    above sr_star, given t daily returns with this skewness and kurtosis. 0.0 when it cannot be computed."""
    if t < 2:
        return 0.0
    room = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if room <= 0:
        return 0.0
    return _NORMAL.cdf((sr - sr_star) * math.sqrt(t - 1) / math.sqrt(room))


def expected_max_sharpe(n_trials: int, variance: float) -> float:
    """The daily Sharpe ratio the best of n_trials variants would show by luck alone, when their Sharpe ratios
    spread with this variance (Bailey & Lopez de Prado 2014, the deflated Sharpe ratio's SR0)."""
    if n_trials <= 1 or variance <= 0:
        return 0.0
    inv = _NORMAL.inv_cdf
    return math.sqrt(variance) * ((1 - EULER_GAMMA) * inv(1 - 1 / n_trials)
                                  + EULER_GAMMA * inv(1 - 1 / (n_trials * math.e)))


def dsr(returns: list[float], n_trials: int, variance: float) -> float:
    """Deflated Sharpe ratio of a record of daily returns: its probabilistic Sharpe ratio against the best result
    n_trials variants would show by luck alone."""
    skew, kurt = moments(returns)
    return psr(sharpe(returns), expected_max_sharpe(n_trials, variance), len(returns), skew, kurt)


def dsr_of(m: dict, n_trials: int, variance: float) -> float:
    """The same from a record's metrics (its daily Sharpe, length, skewness and kurtosis)."""
    return psr(m["sharpe_daily"], expected_max_sharpe(n_trials, variance), m["days"], m["skew"], m["kurtosis"])


def luck_in_100(dsr_value: float) -> int:
    """'A record this good would turn up by luck about k times in 100', rounded up (the cautious side): a pass
    (DSR >= 0.95) always shows 5 or fewer, a fail always 6 or more."""
    return int(math.ceil(round(100 * (1 - dsr_value), 6)))


def luck_words(dsr_value: float) -> str:
    """The luck figure in words for the owner."""
    if 100 * (1 - dsr_value) < 1:
        return "under 1 in 100"
    return f"about {luck_in_100(dsr_value)} times in 100"


# ---- what a record looks like ----
def rule_trades(trades: list[dict]) -> int:
    """Trades the rule itself closed (a signal, a stop or Williams' close); the closes the protocol makes at a window's
    end do not count toward any minimum."""
    return sum(1 for x in trades if x.get("reason") != "end of window")


def metrics(run: dict, start_ms: int, end_ms: int) -> dict:
    """The figures of one window (or of a stitched record): return, yearly growth, Sharpe, worst drop, trades..."""
    returns = daily_returns(run, start_ms, end_ms)
    equity = [e for d, e in run["curve"] if start_ms <= d <= end_ms]
    first = float(run["base"])
    for d, e in run["curve"]:
        if d < start_ms:
            first = e
    last = equity[-1] if equity else first
    total = last / first - 1 if first > 0 else 0.0
    days = len(returns)
    peak, worst = first, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak)
    trades = [x for x in run["trades"]
              if x.get("exit_ms") is None or start_ms <= x["exit_ms"] < end_ms + DAY_MS]
    wins = [x for x in trades if x["pnl"] > 0]
    gross_win = math.fsum(x["pnl"] for x in wins)
    gross_loss = -math.fsum(x["pnl"] for x in trades if x["pnl"] <= 0)
    invested = run.get("invested")
    if invested is not None:
        inv = [f for (d, _), f in zip(run["curve"], invested) if start_ms <= d <= end_ms]
        invested_pct = round(100 * statistics.fmean(inv), 1) if inv else 0.0
    else:
        invested_pct = None
    sr = sharpe(returns)
    skew, kurt = moments(returns)
    growth = (1 + total) ** (DAYS_PER_YEAR / days) - 1 if days and total > -1 else -1.0
    return {"return_pct": round(100 * total, 2), "cagr_pct": round(100 * growth, 2),
            "sharpe": round(annual(sr), 3), "sharpe_daily": sr, "skew": skew, "kurtosis": kurt,
            "max_drawdown_pct": round(100 * worst, 2), "trades": len(trades), "rule_trades": rule_trades(trades),
            "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "invested_pct": invested_pct,
            "fees_pct": round(100 * math.fsum(x.get("fees", 0.0) for x in trades) / first, 2) if first > 0 else 0.0,
            "days": days}


def min_trades(days: int) -> int:
    """Rule trades a choice window of this many days needs: 5 a year, rounded up."""
    return int(math.ceil(round(MIN_TRADES_PER_YEAR * days / DAYS_PER_YEAR, 9)))


def choose(rows: list[dict], years: float) -> dict | None:
    """The setting a choice window picks. A row is eligible with at least 5 rule trades a year; among the eligible,
    the highest LOWER half wins (a setting that works in only one market phase is luck); ties go to the earlier grid
    entry (the source's headline setting first). None when no setting is eligible."""
    need = int(math.ceil(round(MIN_TRADES_PER_YEAR * years, 9)))
    best, score = None, None
    for row in rows:
        row["eligible"] = row["trades"] >= need
        if not row["eligible"]:
            continue
        s = min(row["halves"])
        if score is None or s > score:
            best, score = row, s
    return best


def stitch(runs: list[dict], balance: float = 10_000.0) -> dict:
    """The walk-forward record: each test segment's daily returns applied in order to one account that starts with
    `balance` (so the record's return is the product of the segments'); trades and exposures are concatenated."""
    equity, curve, trades, invested = balance, [], [], []
    for run in runs:
        prev = float(run["base"])
        for d, e in run["curve"]:
            equity *= (e / prev) if prev > 0 else 1.0
            prev = e
            curve.append((d, equity))
        trades += run["trades"]
        invested += run.get("invested") or [0.0] * len(run["curve"])
    return {"curve": curve, "base": balance, "trades": trades, "invested": invested}


def trials() -> int:
    """Every variant this protocol tries (each family's grid and the desk's two references), plus TRIALS_BEFORE."""
    from .library import FAMILIES, REFERENCES   # here, not at the top: library imports this module
    return sum(len(f.grid) for f in FAMILIES) + len(REFERENCES) + TRIALS_BEFORE


def trial_variance(sharpes_daily: list[float]) -> float:
    """V: the spread (sample variance) of the variants' daily in-sample Sharpe ratios."""
    return statistics.variance(sharpes_daily) if len(sharpes_daily) >= 2 else 0.0


# ---- the verdict ----
def _pct(x) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def verdict(entry: dict, bench: dict, n_trials: int, variance: float) -> tuple[str, list[str]]:
    """PASS only if every one of the nine pre-declared conditions holds; otherwise FAIL with every failed condition,
    in this order. `bench` holds the holding yardstick's metrics: {"holdout": {...}, "walk_forward": {...}}."""
    reasons: list[str] = []
    ho, wf = entry.get("holdout"), entry.get("walk_forward")
    bh, bw = bench.get("holdout") or {}, bench.get("walk_forward") or {}
    if entry.get("chosen") is None:                                                    # 1
        reasons.append(f"too few trades in the first 70% at every setting (needs {entry.get('min_trades', '?')} rule "
                       f"trades, {MIN_TRADES_PER_YEAR} a year): nothing to choose")
    if ho is not None and ho["rule_trades"] < MIN_HOLDOUT_TRADES:                      # 2
        reasons.append(f"only {ho['rule_trades']} rule trades in the judged period (needs {MIN_HOLDOUT_TRADES})")
    if wf is not None and wf["rule_trades"] < MIN_WF_TRADES:                           # 3
        reasons.append(f"only {wf['rule_trades']} rule trades in the walk-forward (needs {MIN_WF_TRADES})")
    if ho is not None and not (ho["sharpe_daily"] > 0 and ho["return_pct"] > 0):      # 4
        reasons.append(f"did not beat cash in the judged period ({_pct(ho['return_pct'])}, Sharpe {ho['sharpe']:.2f})")
    if ho is not None and bh and not ho["sharpe_daily"] > bh["sharpe_daily"]:          # 5
        reasons.append(f"did not beat holding the coins in the judged period (Sharpe {ho['sharpe']:.2f} vs "
                       f"{bh['sharpe']:.2f})")
    if wf is not None and not (wf["sharpe_daily"] > 0 and wf["return_pct"] > 0):      # 6
        reasons.append(f"did not beat cash in the walk-forward ({_pct(wf['return_pct'])}, Sharpe {wf['sharpe']:.2f})")
    if wf is not None and bw and not wf["sharpe_daily"] > bw["sharpe_daily"]:          # 7
        reasons.append(f"did not beat holding the coins in the walk-forward (re-split every 6 months; Sharpe "
                       f"{wf['sharpe']:.2f} vs "
                       f"{bw['sharpe']:.2f})")
    deeper = []                                                                        # 8
    if ho is not None and bh and ho["max_drawdown_pct"] > bh["max_drawdown_pct"]:
        deeper.append(f"judged period {ho['max_drawdown_pct']:.0f}% vs {bh['max_drawdown_pct']:.0f}%")
    if wf is not None and bw and wf["max_drawdown_pct"] > bw["max_drawdown_pct"]:
        deeper.append(f"walk-forward {wf['max_drawdown_pct']:.0f}% vs {bw['max_drawdown_pct']:.0f}%")
    if deeper:
        reasons.append("fell deeper than holding the coins (" + "; ".join(deeper) + ")")
    if wf is not None:                                                                 # 9
        d = dsr_of(wf, n_trials, variance)
        if d < LUCK_LEVEL:
            reasons.append(f"a walk-forward record this good would turn up by luck {luck_words(d)} counting all "
                           f"{n_trials} variants (a pass needs 5 or fewer)")
    elif entry.get("chosen") is not None:
        reasons.append("no walk-forward record")
    return ("FAIL", reasons) if reasons else ("PASS", [])
