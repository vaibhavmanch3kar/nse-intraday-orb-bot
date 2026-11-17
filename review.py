"""
Session review: replay one past trading day through the ORB rules, stock by stock.

For each watchlist stock it shows the opening range, the first signal (or why there was
none), and what that trade would have done: entry at the next bar's open, then stop /
target / square-off, in R (multiples of the risk). Portfolio limits (max positions,
daily loss) are NOT applied here - this is a per-stock view for reviewing the rules.

Results are cached in data/review_<day>.json; the dashboard's Refresh button rebuilds them.
"""

import bisect
import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor

import config as C
import costs
import data_yahoo
import marketdata
import strategy


def _cache_path(day):
    return os.path.join(C.DATA_DIR, f"review_{day}.json")


def fetch_all(symbols, range_="60d"):  # 60 days: also feeds the Time Machine history
    now = dt.datetime.now(data_yahoo.IST)

    def one(sym):
        try:
            bars, _, _ = marketdata.fetch(sym, C.INTERVAL_MIN, range_)
            return sym, data_yahoo.closed_bars(bars, now, C.INTERVAL_MIN), None
        except data_yahoo.DataError as e:
            return sym, [], str(e)

    with ThreadPoolExecutor(max_workers=4) as ex:
        return {s: (b, err) for s, b, err in ex.map(one, symbols)}


def in_time_order(bars):
    return all(a["t"] <= b["t"] for a, b in zip(bars, bars[1:]))


def _window(bars, day, index_bars, in_order=None):
    """The shortest piece of `bars` that gives exactly the same replay of `day`, or None to use all bars.

    The rules only ever look back at: today's bars, the last VOLUME_LOOKBACK_DAYS earlier sessions (volume
    average, previous close) and 4 x TRAIL_EMA bars (the EMA trail). So the piece starts at the start of a
    session at least that far back and ends with `day`; index bars are cut to `day` (the index checks only
    read the same day). This turns the History check from minutes into seconds without changing a result.
    Needs bars in time order; anything else uses all bars, as before."""
    if not (in_time_order(bars) if in_order is None else in_order):
        return None
    first = bisect.bisect_left(bars, day, key=lambda b: b["t"].date())
    if first >= len(bars) or bars[first]["t"].date() != day:
        return None
    last = first
    while last + 1 < len(bars) and bars[last + 1]["t"].date() == day:
        last += 1
    start = max(0, first - 4 * max(0, C.TRAIL_EMA))     # EMA trail window of the first bar of the day
    sessions, k = 0, first                               # ...and VOLUME_LOOKBACK_DAYS whole earlier sessions
    while k > 0 and sessions < C.VOLUME_LOOKBACK_DAYS:
        d = bars[k - 1]["t"].date()
        while k > 0 and bars[k - 1]["t"].date() == d:
            k -= 1
        sessions += 1
    start = min(start, k)
    while start > 0 and bars[start - 1]["t"].date() == bars[start]["t"].date():
        start -= 1                                       # begin at the start of a session
    ix = [b for b in index_bars if b["t"].date() == day] if index_bars else index_bars
    return bars[start:last + 1], (ix if ix else index_bars)


def replay_day(bars, day, index_bars=None, in_order=None):
    """First ORB signal of `day` and its simulated outcome (one trade per stock per day).
    in_order: pass True/False when the caller already checked in_time_order(bars) (History does, once)."""
    w = _window(bars, day, index_bars, in_order)
    if w:
        bars, index_bars = w
    return _replay_day(bars, day, index_bars)


def _replay_day(bars, day, index_bars=None):
    idxs = [i for i, b in enumerate(bars) if b["t"].date() == day]
    out = {"bars": len(idxs), "or_high": None, "or_low": None, "open": None, "close": None,
           "day_change_pct": None, "signal_time": None, "direction": None, "entry": None,
           "stop": None, "target": None, "exit_time": None, "exit": None, "exit_reason": None,
           "r": None, "pnl_per_share": None, "note": "", "closes": []}
    if not idxs:
        out["note"] = "no data for this day"
        return out
    day_bars = [bars[i] for i in idxs]
    out["open"], out["close"] = day_bars[0]["o"], day_bars[-1]["c"]
    out["day_change_pct"] = (out["close"] / out["open"] - 1) * 100
    out["closes"] = [round(b["c"], 2) for b in day_bars]
    orng = strategy.opening_range(day_bars)
    if orng:
        out["or_high"], out["or_low"] = orng["high"], orng["low"]

    last_reason = ""
    for k, i in enumerate(idxs):
        ev = strategy.evaluate(bars, i, index_bars)
        if ev["reason"] not in ("opening range bar", "opening range forming", "inside range", "no fresh breakout"):
            last_reason = f"{bars[i]['t']:%H:%M} {ev['reason']}"
        if not ev["signal"] or strategy.is_square_off_bar(bars[i]):
            continue
        if k + 1 >= len(idxs):
            out["note"] = "signal on the last bar"
            break
        nxt = bars[idxs[k + 1]]
        d, stop = ev["signal"], ev["stop"]
        out.update(signal_time=f"{bars[i]['t']:%H:%M}", direction=d, stop=stop)
        if (d == "LONG" and nxt["o"] <= stop) or (d == "SHORT" and nxt["o"] >= stop):
            out["note"] = "next bar opened beyond the stop - skipped"
            break
        slip = nxt["o"] * C.SLIPPAGE_PCT / 100
        entry = nxt["o"] + slip if d == "LONG" else nxt["o"] - slip
        target = strategy.target_for(entry, stop, d)
        out.update(entry=entry, target=target)
        pos = {"direction": d, "stop": stop, "target": target, "entry": entry}
        for held, j in enumerate(idxs[k + 1:], start=1):
            b = bars[j]
            hit = strategy.manage_exit(pos, bars, j, held)
            if not hit and strategy.is_square_off_bar(b):
                hit = ("square-off", b["c"])
            if hit:
                end = b["t"] + dt.timedelta(minutes=C.INTERVAL_MIN)
                out.update(exit_time=f"{end:%H:%M}", exit=hit[1], exit_reason=hit[0])
                break
        else:
            out.update(exit_time="close", exit=day_bars[-1]["c"], exit_reason="end of data")
        # per-share P&L after slippage and charges, using 100 shares to spread fixed charges
        gross = costs.gross_pnl(entry, out["exit"], 1, d)
        charges = costs.round_trip(entry, out["exit"], 100, d) / 100
        out["pnl_per_share"] = gross - charges
        out["r"] = out["pnl_per_share"] / abs(entry - stop)
        break
    else:
        out["note"] = last_reason or ("stayed inside the opening range" if orng else "no opening range")
    if not out["note"] and not out["direction"]:
        out["note"] = last_reason
    return out


def add_time_machine(rows, n=10000, data=None, index_data=None, day=None):
    """Time Machine odds for every signal in the review (5-min data fetched only for those stocks)."""
    import timemachine
    sig = {s: r for s, r in rows.items() if r.get("direction") and r.get("entry")}
    if not sig:
        return
    got = marketdata.fetch_many(list(sig), timemachine.STEP_MIN, "60d", C.FETCH_WORKERS)
    for s, r in sig.items():
        g = got.get(s)
        if isinstance(g, Exception) or g is None:
            r["time_machine"] = {"n": 0, "error": str(g)}
            continue
        h, m = map(int, r["signal_time"].split(":"))
        start = (dt.datetime(2000, 1, 1, h, m) + dt.timedelta(minutes=C.INTERVAL_MIN)).strftime("%H:%M")
        res = timemachine.replay(timemachine.slot_moves(g[0]), r["direction"], r["entry"], r["stop"],
                                 start, C.SQUARE_OFF, n=n)
        res["text"] = timemachine.summary(res) if res.get("n") else "not enough 5-min history"
        res["verdict"] = timemachine.verdict(res, 1.0)
        if data and day and data.get(s):
            h = timemachine.history(data[s][0], day, r["direction"],
                                    (index_data or {}).get(strategy.index_for(s)))
            res["history"] = h
            res["history_text"] = timemachine.history_text(h)
        r["time_machine"] = res


def trading_days(data):
    days = sorted({b["t"].date() for bars, _ in data.values() for b in bars})
    return days


def build(day=None, symbols=None, refresh=False):
    """Review for `day` (date or ISO string). Default = most recent session with data."""
    symbols = symbols or C.WATCHLIST
    if day and not refresh and os.path.exists(_cache_path(day)):
        with open(_cache_path(day), encoding="utf-8") as f:
            return json.load(f)
    indices = sorted({strategy.index_for(s) for s in symbols}) if C.INDEX_FILTER else []
    fetched = fetch_all(list(symbols) + indices)
    index_data = {ix: fetched.pop(ix)[0] for ix in indices}
    data = fetched
    days = trading_days(data)
    if not days:
        return {"day": None, "days": [], "rows": {}, "error": "no data from Yahoo"}
    target = dt.date.fromisoformat(str(day)) if day else days[-1]
    if target not in days:
        target = days[-1]
    rows = {}
    for sym, (bars, err) in data.items():
        rows[sym] = ({"note": f"data error: {err}"} if err else
                     replay_day(bars, target, index_data.get(strategy.index_for(sym))))
    add_time_machine(rows, data=data, index_data=index_data, day=target)
    trades = [r for r in rows.values() if r.get("r") is not None]
    res = {
        "day": target.isoformat(), "days": [d.isoformat() for d in days],
        "built": dt.datetime.now(data_yahoo.IST).isoformat(timespec="seconds"),
        "rows": rows, "signals": len(trades),
        "wins": sum(1 for r in trades if r["r"] > 0),
        "total_r": sum(r["r"] for r in trades),
        "settings": {k: getattr(C, k) for k in ("OR_MINUTES", "MIN_OR_PCT", "MAX_OR_PCT", "VOLUME_MULT",
                                                "USE_VWAP_FILTER", "TARGET_R", "LAST_ENTRY", "SQUARE_OFF",
                                                "ALLOW_SHORT", "TRAIL_EMA", "TIME_STOP_BARS",
                                                "INDEX_FILTER")},
    }
    os.makedirs(C.DATA_DIR, exist_ok=True)
    for d in {res["day"], str(day) if day else res["day"]}:
        with open(_cache_path(d), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, default=str)
    return res


def latest(max_age_min=15):
    """Most recent session: reuse the newest cache if it was built recently, else rebuild."""
    c = latest_cached()
    if c and c.get("built"):
        age = dt.datetime.now(data_yahoo.IST) - dt.datetime.fromisoformat(c["built"])
        if age.total_seconds() < max_age_min * 60:
            return c
    return build(refresh=True)


def latest_cached():
    try:
        files = sorted(f for f in os.listdir(C.DATA_DIR) if f.startswith("review_") and f.endswith(".json"))
    except OSError:
        return None
    if not files:
        return None
    with open(os.path.join(C.DATA_DIR, files[-1]), encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import sys
    import theme as T
    r = build(sys.argv[1] if len(sys.argv) > 1 else None, refresh=True)
    print(T.banner(f"{T.LAMP} {T.term('review')}"))
    print(f"Review {r['day']}: {r['signals']} signals, {r['wins']} wins, total {r['total_r']:+.2f}R")
    for s, row in sorted(r["rows"].items()):
        if row.get("direction"):
            print(f"  {s:11s} {row['signal_time']} {row['direction']:5s} entry {row['entry']:.2f} "
                  f"-> {T.reason_label(row['exit_reason'])} {row['exit_time']} {row['r']:+.2f}R")
        else:
            print(f"  {s:11s} {row.get('note', '')}")
