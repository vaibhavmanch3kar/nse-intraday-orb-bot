#!/usr/bin/env python3
"""
Candle archive (learning advisor, Phase 2): keep every finished trading day's 15-minute candles
on disk, so history grows week by week instead of being limited to what Yahoo (~60 days) or Kite
(needs today's login) will hand back on the day you ask.

    .venv\\Scripts\\python.exe archive.py            # save every finished day the data source still has
    .venv\\Scripts\\python.exe archive.py --days 5   # only the last few days
    .venv\\Scripts\\python.exe archive.py --status   # what is in the archive

The bot also saves the day automatically once the market has closed (see Bot.end_of_day).

Files: data/candles/YYYY-MM-DD.csv, one per trading day, columns symbol,t,o,h,l,c,v,source.
Covers the watchlist plus the indexes the index filter uses. Only finished sessions are saved
(today only after MARKET_CLOSE). Kite data is preferred: a day saved from Kite is never replaced
by Yahoo data, and a symbol's day is otherwise only replaced by one with at least as many candles.
"""

import argparse
import csv
import datetime as dt
import os

import config as C
import marketdata
import strategy

COLS = ["symbol", "t", "o", "h", "l", "c", "v", "source"]
SOURCE_RANK = {"yahoo": 1, "kite": 2}


def archive_dir():
    return os.path.join(C.DATA_DIR, "candles")


def archive_symbols(watchlist=None):
    """Watchlist stocks, then the indexes the index filter compares them with (NIFTY always)."""
    stocks = list(watchlist or C.WATCHLIST)
    indexes = sorted({strategy.index_for(s) for s in stocks} | {C.INDEX_DEFAULT})
    return stocks + [ix for ix in indexes if ix not in stocks]


def complete_days(bars, now):
    """{date: bars} for sessions that are over at `now`: earlier days, and today after the close."""
    close_today = dt.datetime.combine(now.date(), strategy.hm(C.MARKET_CLOSE), tzinfo=now.tzinfo)
    out = {}
    for b in bars:
        d = b["t"].date()
        if d < now.date() or (d == now.date() and now >= close_today):
            out.setdefault(d, []).append(b)
    return out


def _path(day):
    return os.path.join(archive_dir(), f"{day.isoformat()}.csv")


def _read_day(day):
    """{symbol: {"source": s, "rows": [csv dicts]}} for one archived day."""
    path = _path(day)
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slot = out.setdefault(r["symbol"], {"source": r["source"], "rows": []})
            slot["rows"].append(r)
    return out


def _better(new_source, new_n, old):
    """Replace an archived symbol-day? Never swap Kite for Yahoo; otherwise keep the fuller day."""
    if old is None:
        return True
    new_rank = SOURCE_RANK.get(new_source, 0)
    old_rank = SOURCE_RANK.get(old["source"], 0)
    if new_rank != old_rank:
        return new_rank > old_rank
    return new_n >= len(old["rows"])


def save(bars_by_sym, source, now):
    """Write finished days for each symbol. Returns {"days": [dates written], "bars": n, "kept": n}."""
    os.makedirs(archive_dir(), exist_ok=True)
    by_day = {}
    for sym, bars in bars_by_sym.items():
        for day, day_bars in complete_days(bars, now).items():
            by_day.setdefault(day, {})[sym] = day_bars
    written, n_bars, kept = [], 0, 0
    for day in sorted(by_day):
        existing = _read_day(day)
        changed = False
        for sym, day_bars in by_day[day].items():
            if not _better(source, len(day_bars), existing.get(sym)):
                kept += 1
                continue
            existing[sym] = {"source": source, "rows": [
                {"symbol": sym, "t": b["t"].isoformat(), "o": b["o"], "h": b["h"], "l": b["l"],
                 "c": b["c"], "v": b["v"], "source": source} for b in day_bars]}
            n_bars += len(day_bars)
            changed = True
        if not changed:
            continue
        tmp = _path(day) + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            for sym in sorted(existing):
                w.writerows(existing[sym]["rows"])
        os.replace(tmp, _path(day))   # a crash mid-write never leaves a half-written day
        written.append(day)
    return {"days": written, "bars": n_bars, "kept": kept}


def days():
    """Archived trading days, oldest first."""
    if not os.path.isdir(archive_dir()):
        return []
    out = []
    for name in os.listdir(archive_dir()):
        if name.endswith(".csv"):
            try:
                out.append(dt.date.fromisoformat(name[:-4]))
            except ValueError:
                continue
    return sorted(out)


def load(symbols=None, start=None, end=None):
    """{symbol: bars oldest-first} from the archive, same bar format as the data sources.
    start/end: optional dates (inclusive)."""
    want = set(symbols) if symbols else None
    out = {}
    for day in days():
        if (start and day < start) or (end and day > end):
            continue
        for sym, slot in _read_day(day).items():
            if want is not None and sym not in want:
                continue
            for r in slot["rows"]:
                out.setdefault(sym, []).append(
                    {"t": dt.datetime.fromisoformat(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                     "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])})
    for bars in out.values():
        bars.sort(key=lambda b: b["t"])
    return out


def fetch_and_save(range_="5d", symbols=None, now=None):
    """Fetch recent candles from the data source in use and archive the finished days."""
    now = now or dt.datetime.now(marketdata.IST)
    syms = symbols or archive_symbols()
    got = marketdata.fetch_many(syms, C.INTERVAL_MIN, range_, C.FETCH_WORKERS)
    source = marketdata.status()["using"]
    bars_by_sym, failed = {}, []
    for s in syms:
        g = got.get(s)
        if isinstance(g, Exception) or not g or not g[0]:
            failed.append(s)
            continue
        bars_by_sym[s] = g[0]
    res = save(bars_by_sym, source, now)
    res.update(source=source, failed=failed, symbols=len(bars_by_sym))
    return res


def status():
    """Archive summary for the console."""
    ds = days()
    if not ds:
        return {"days": 0}
    sources, syms = {}, set()
    for d in ds:
        for sym, slot in _read_day(d).items():
            syms.add(sym)
            sources[slot["source"]] = sources.get(slot["source"], 0) + 1
    return {"days": len(ds), "first": ds[0], "last": ds[-1], "symbols": sorted(syms),
            "symbol_days_by_source": sources}


def main():
    import theme as T
    ap = argparse.ArgumentParser(description="Save finished days' 15-minute candles to data/candles")
    ap.add_argument("--days", type=int, default=None,
                    help="how far back to fetch (default: as far as the data source allows)")
    ap.add_argument("--status", action="store_true", help="only show what is archived")
    a = ap.parse_args()
    print(T.banner("Candle archive"))
    if not a.status:
        limit = marketdata.max_history_days()
        n = min(a.days or limit, limit)
        res = fetch_and_save(f"{n}d")
        print(f"Fetched {res['symbols']} symbols from {res['source'].title()} ({n} days back)")
        print(f"  Saved {len(res['days'])} day files, {res['bars']} candles"
              + (f"; kept {res['kept']} symbol-days already archived from a better or fuller source"
                 if res["kept"] else ""))
        if res["failed"]:
            print(f"  [!] no data for: {', '.join(res['failed'])}")
    st = status()
    if not st["days"]:
        print("Archive is empty.")
        return
    print(f"Archive: {st['days']} trading days, {st['first']} -> {st['last']}, {len(st['symbols'])} symbols")
    print("  Symbol-days by source: " + ", ".join(f"{k} {v}" for k, v in sorted(st["symbol_days_by_source"].items())))
    print(f"  Folder: {archive_dir()}")


if __name__ == "__main__":
    main()
