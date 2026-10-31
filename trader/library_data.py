"""Prices for the strategy library (release 1.9.0): Binance's public candles, kept on disk between runs.

The library replays rules on years of prices: about 430 public requests the first time (2-4 minutes), then only the
candles that finished since the last run (about 24 requests). They are kept in data/library/candles, one small gzip
file per coin and candle size, outside the trade journal so the weekly journal backup stays small. data/ is never
part of the code, so updates keep the cache.

Only finished candles are kept (Binance's last row is still forming), gaps are left as gaps (nothing is invented),
and a file that cannot be read is deleted and downloaded again. Every file is written to a temporary name first and
then renamed, so an interrupted run never leaves half a file behind.
"""
from __future__ import annotations

import bisect
import csv
import gzip
import io
import os
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

INTERVAL_MS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}
DAY_MS = 86_400_000
START_MS = 1_483_228_800_000     # 2017-01-01 UTC, before any tested coin was listed (never 0: see binance_client.klines)
HOURLY_DAYS = 1_095 + 210        # the hourly reference replays 3 years and needs 210 days for its 200-day averages
KEEP_HOURLY_DAYS = 1_400         # the hourly file is trimmed to this many days on every update
PAGE = 1_000                     # Binance's largest page
HEADER = ("time", "open", "high", "low", "close", "volume")


@dataclass
class Series:
    """One coin's finished candles at one candle size, as columns (about 50 bytes a candle instead of 385 as dicts)."""
    symbol: str
    interval: str
    time: array            # array('q'): open times in ms UTC, strictly increasing
    open: array
    high: array
    low: array
    close: array
    volume: array

    def __len__(self) -> int:
        return len(self.time)

    def upto(self, t_ms: int) -> int:
        """How many candles opened at or before t_ms."""
        return bisect.bisect_right(self.time, t_ms)

    def cut(self, end_ms: int) -> "Series":
        """A copy without the candles that opened after end_ms: what was known at that time."""
        n = self.upto(end_ms)
        return Series(self.symbol, self.interval, self.time[:n], self.open[:n], self.high[:n], self.low[:n],
                      self.close[:n], self.volume[:n])

    def dicts(self, start: int = 0) -> list[dict]:
        """The desk's candle format ({time, open, high, low, close, volume}), for the desk's own replays only."""
        return [{"time": self.time[i], "open": self.open[i], "high": self.high[i], "low": self.low[i],
                 "close": self.close[i], "volume": self.volume[i]} for i in range(start, len(self.time))]

    @classmethod
    def from_rows(cls, symbol: str, interval: str, rows: list[tuple]) -> "Series":
        cols = list(zip(*rows)) if rows else [()] * 6
        return cls(symbol, interval, array("q", cols[0]), *(array("d", c) for c in cols[1:]))


def cache_dir(s) -> Path:
    """data/library next to the journal (on the server: ~/crypto-desk/data/library)."""
    return Path(s.db_path).parent / "library"


def candle_path(directory: Path, symbol: str, interval: str) -> Path:
    return Path(directory) / "candles" / f"{symbol}_{interval}.csv.gz"


def load(path: Path) -> list[tuple] | None:
    """The cached candles as (time, open, high, low, close, volume) rows; None when the file is missing, unreadable
    or out of order."""
    try:
        with gzip.open(path, "rt", newline="") as fh:
            reader = csv.reader(fh)
            if tuple(next(reader)) != HEADER:
                return None
            rows, last = [], None
            for r in reader:
                t = int(r[0])
                if last is not None and t <= last:
                    return None
                rows.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
                last = t
            return rows
    except (OSError, EOFError, ValueError, IndexError, StopIteration, csv.Error, gzip.BadGzipFile):
        return None


def save(path: Path, rows: list[tuple]) -> None:
    """Header and rows (prices re-printed exactly), written to a temporary file and then renamed into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow((r[0], *(repr(float(x)) for x in r[1:])))
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", newline="", compresslevel=6) as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


def update(client, symbol: str, interval: str, directory: Path, since_ms: int, now_ms: int | None = None,
           refresh: bool = False, progress=None) -> tuple[Series, int]:
    """Read the cached file, download only the candles newer than its last one (pages of 1,000, retried on a
    hiccup), drop the candle still forming, trim the hourly file, save, and return (series, requests used)."""
    from .backtest import _klines_page
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    step = INTERVAL_MS[interval]
    path = candle_path(directory, symbol, interval)
    rows = None if refresh else load(path)
    if rows is None and path.exists() and not refresh:
        if progress:
            progress(f"{path.name} could not be read: downloading it again")
        path.unlink(missing_ok=True)
    rows = rows or []
    since_ms = max(int(since_ms), START_MS)
    start = rows[-1][0] + step if rows else since_ms
    requests, changed = 0, refresh or not rows
    while start + step <= now_ms:          # a candle that opened at `start` has finished by now
        page = _klines_page(client, symbol, interval, start)
        requests += 1
        if not page:
            break
        last = rows[-1][0] if rows else None
        for c in page:
            t = int(c["time"])
            if t + step > now_ms or (last is not None and t <= last):
                continue                    # still forming, or already cached (an overlapping page)
            rows.append((t, float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                         float(c["volume"])))
            last, changed = t, True
        if len(page) < PAGE:
            break
        start = int(page[-1]["time"]) + step
    if interval == "1h":
        keep = now_ms - KEEP_HOURLY_DAYS * DAY_MS
        if rows and rows[0][0] < keep:
            rows = rows[bisect.bisect_left([r[0] for r in rows], keep):]
            changed = True
    if changed:
        save(path, rows)
    return Series.from_rows(symbol, interval, rows), requests


def universe(client, directory: Path, symbols, now_ms: int | None = None, refresh: bool = False, hourly: bool = True,
             progress=None) -> tuple[dict[str, dict[str, Series]], int]:
    """{"1d": {symbol: Series}, "4h": {...}, "1h": {...}} for the coins, and the requests used. Daily and 4-hour
    candles from each coin's listing; hourly ones for the last 3 years and 210 days (the desk's hourly reference)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    out: dict[str, dict[str, Series]] = {"1d": {}, "4h": {}, "1h": {}}
    used = 0
    intervals = ("1d", "4h", "1h") if hourly else ("1d", "4h")
    for interval in intervals:
        since = START_MS if interval != "1h" else (now_ms - HOURLY_DAYS * DAY_MS) // INTERVAL_MS["1h"] * INTERVAL_MS["1h"]
        for sym in symbols:
            series, n = update(client, sym, interval, directory, since, now_ms, refresh, progress)
            used += n
            if len(series):
                out[interval][sym] = series
    return out, used
