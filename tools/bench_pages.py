#!/usr/bin/env python3
"""
Benchmark the Review (Chitrakoot) and Scanner (Hanuman's Watch) work. Read-only: it never writes in data/
and never places orders. Runs at below-normal CPU priority so a running bot is not slowed.

    .venv\\Scripts\\python.exe -B tools\\bench_pages.py                 # calculations, on data\\candles (no network)
    .venv\\Scripts\\python.exe -B tools\\bench_pages.py --url http://127.0.0.1:5000   # page and API response times
    .venv\\Scripts\\python.exe -B tools\\bench_pages.py --network       # downloads, as one page build makes them

--network uses the data source in your settings (DATA_SOURCE); with Kite it spends Kite requests, so avoid it
while the bot is trading.
"""

import argparse
import csv
import datetime as dt
import glob
import os
import statistics
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import config as C  # noqa: E402


def low_priority():
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS


def load_candles(days=60):
    bars = {}
    for f in sorted(glob.glob(os.path.join(C.DATA_DIR, "candles", "*.csv")))[-days:]:
        with open(f, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                bars.setdefault(row["symbol"], []).append(
                    {"t": dt.datetime.fromisoformat(row["t"]), "o": float(row["o"]), "h": float(row["h"]),
                     "l": float(row["l"]), "c": float(row["c"]), "v": float(row["v"])})
    for s in bars:
        bars[s].sort(key=lambda b: b["t"])
    return bars


def timed(fn, *a, **k):
    t = time.perf_counter()
    r = fn(*a, **k)
    return r, time.perf_counter() - t


def bench_compute():
    import review
    import strategy
    import timemachine
    bars = load_candles()
    if not bars:
        print("No candles in data\\candles yet - run the bot for a day first.")
        return
    stocks = [s for s in C.WATCHLIST if s in bars]
    day = max(b["t"].date() for s in stocks for b in bars[s])
    ix = lambda s: bars.get(strategy.index_for(s))  # noqa: E731
    rows, t_replay = timed(lambda: {s: review.replay_day(bars[s], day, ix(s)) for s in stocks})
    hist = [timed(timemachine.history, bars[s], day, None, ix(s))[1] for s in stocks]
    print(f"Review replay of {day} for {len(stocks)} stocks: {t_replay:.2f} s")
    print(f"History check per stock: median {statistics.median(hist):.2f} s, max {max(hist):.2f} s")


def bench_url(base):
    for path in ("/api/live", "/", "/review", "/scanner", "/api/jobs"):
        times = []
        for _ in range(3):
            t = time.perf_counter()
            try:
                with urllib.request.urlopen(base + path, timeout=120) as r:
                    r.read()
                    code = r.status
            except Exception as e:  # report and go on
                code = type(e).__name__
            times.append(time.perf_counter() - t)
        print(f"GET {path:10s} {code}  first {times[0]:.3f} s, then {min(times[1:]):.3f} s")


def bench_network():
    import marketdata
    import strategy
    syms = list(C.WATCHLIST) + sorted({strategy.index_for(s) for s in C.WATCHLIST})
    _, t = timed(marketdata.fetch_many, syms, C.INTERVAL_MIN, "60d", C.FETCH_WORKERS)
    print(f"Review download, {len(syms)} symbols ({marketdata.status()['using']}): {t:.1f} s")
    _, t = timed(marketdata.fetch_many, C.SCAN_LIST, range_="2y", workers=C.FETCH_WORKERS, interval="1d")
    print(f"Scanner download, {len(C.SCAN_LIST)} symbols: {t:.1f} s")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--url", help="dashboard address to time, e.g. http://127.0.0.1:5000")
    ap.add_argument("--network", action="store_true", help="also time the downloads (spends data requests)")
    a = ap.parse_args()
    low_priority()
    bench_compute()
    if a.url:
        bench_url(a.url.rstrip("/"))
    if a.network:
        bench_network()


if __name__ == "__main__":
    main()
