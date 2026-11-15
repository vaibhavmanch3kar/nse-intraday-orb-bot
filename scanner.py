#!/usr/bin/env python3
"""
Daily swing-setup scanner (research only - the bot does NOT trade these).

Runs on the last CLOSED daily candle of every SCAN_LIST stock (default Nifty 50) and flags:
  1. 20-day high breakout   close above the highest high of the previous 20 sessions
  2. Bull flag              a >= 8% pole within ~10 sessions, then 3-10 sessions of tight,
                            shallow consolidation (range and pullback <= half the pole),
                            closing in the upper half of the flag and above the 20-day MA
  3. 20-day MA reclaim      closed below the 20 DMA yesterday, above it today
  4. Oversold               RSI(14) below 30
  5. Oversold bounce        RSI(14) was below 30 in the previous 3 sessions and today
                            closed above yesterday's high
Context for each: above/below the 200 DMA (the trend filter from the TWS interview) and
distance from the 52-week high. Plan: stop = close - 1.5 x ATR(14), target = 2R, and the
quantity that risks RISK_PER_TRADE_PCT of PAPER_START_INR.

    .venv\\Scripts\\python.exe scanner.py
"""

import datetime as dt
import json
import os

import config as C
import data_yahoo
import marketdata

SETUPS = ["20-day high breakout", "Bull flag", "20-day MA reclaim", "Oversold", "Oversold bounce"]


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi_series(closes, n=14):
    """Wilder RSI for every bar (None until enough data)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n + 1)]
    ag, al = sum(gains) / n, sum(losses) / n

    def val(ag, al):
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100 - 100 / (1 + ag / al)
    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(0.0, ch)) / n
        al = (al * (n - 1) + max(0.0, -ch)) / n
        out[i] = val(ag, al)
    return out


def atr(bars, n=14):
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def bull_flag(bars):
    """Return pole % if the last bars form a bull flag, else None."""
    if len(bars) < 30:
        return None
    for flag_len in range(3, 11):
        top_i = len(bars) - 1 - flag_len
        top = bars[top_i]["h"]
        if any(b["h"] > top for b in bars[top_i + 1:]):   # flag must not exceed the pole top
            continue
        pole_low = min(b["l"] for b in bars[max(0, top_i - 10): top_i + 1])
        pole = top - pole_low
        if pole_low <= 0 or pole / pole_low < 0.08:
            continue
        flag = bars[top_i + 1:]
        f_hi, f_lo = max(b["h"] for b in flag), min(b["l"] for b in flag)
        if f_hi - f_lo > 0.5 * pole or f_lo < top - 0.5 * pole:
            continue
        if bars[-1]["c"] < f_lo + 0.5 * (f_hi - f_lo):
            continue
        return pole / pole_low * 100
    return None


def scan_symbol(bars):
    """Setups on the last bar of `bars` (oldest-first, closed daily bars)."""
    if len(bars) < 60:
        return None
    closes = [b["c"] for b in bars]
    last, prev = bars[-1], bars[-2]
    s20, s20_prev = sma(closes, 20), sma(closes[:-1], 20)
    s200 = sma(closes, 200)
    rsi = rsi_series(closes)
    a = atr(bars)
    hits = []
    if last["c"] > max(b["h"] for b in bars[-21:-1]):
        hits.append("20-day high breakout")
    pole = bull_flag(bars)
    if pole and s20 and last["c"] > s20:
        hits.append("Bull flag")
    if s20 and s20_prev and prev["c"] < s20_prev and last["c"] > s20:
        hits.append("20-day MA reclaim")
    if rsi[-1] is not None and rsi[-1] < 30:
        hits.append("Oversold")
    if any(r is not None and r < 30 for r in rsi[-4:-1]) and last["c"] > prev["h"]:
        hits.append("Oversold bounce")
    hi52 = max(b["h"] for b in bars[-250:])
    stop = last["c"] - 1.5 * a if a else None
    risk_rs = C.PAPER_START_INR * C.RISK_PER_TRADE_PCT / 100
    qty = 0
    if stop and last["c"] > stop:
        qty = int(min(risk_rs // (last["c"] - stop), C.PAPER_START_INR * C.MAX_POSITION_PCT / 100 // last["c"]))
    return {
        "date": last["t"].date().isoformat(), "close": last["c"], "change_pct": (last["c"] / prev["c"] - 1) * 100,
        "setups": hits, "rsi": rsi[-1], "sma20": s20, "sma200": s200,
        "above_200dma": (last["c"] > s200) if s200 else None,
        "from_52w_high_pct": (last["c"] / hi52 - 1) * 100, "atr": a, "pole_pct": pole,
        "stop": stop, "target": last["c"] + 2 * (last["c"] - stop) if stop else None, "qty": qty,
        "closes": [round(c, 2) for c in closes[-60:]],
    }


def scan(symbols=None, refresh=False, now=None):
    symbols = symbols or C.SCAN_LIST
    now = now or dt.datetime.now(data_yahoo.IST)
    got = marketdata.fetch_many(symbols, range_="2y", workers=C.FETCH_WORKERS, interval="1d")
    rows, errors = {}, {}
    for s, g in got.items():
        if isinstance(g, Exception):
            errors[s] = str(g)
            continue
        bars = g[0]
        # today's daily bar is still forming during market hours
        close_t = dt.datetime.combine(now.date(), dt.time(15, 30), tzinfo=data_yahoo.IST)
        if bars and bars[-1]["t"].date() == now.date() and now < close_t:
            bars = bars[:-1]
        r = scan_symbol(bars)
        if r:
            rows[s] = r
    day = max((r["date"] for r in rows.values()), default=None)
    res = {"day": day, "built": now.isoformat(timespec="seconds"), "rows": rows, "errors": errors,
           "counts": {k: sum(1 for r in rows.values() if k in r["setups"]) for k in SETUPS}}
    os.makedirs(C.DATA_DIR, exist_ok=True)
    with open(os.path.join(C.DATA_DIR, "scan_latest.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)
    return res


def latest(max_age_min=30):
    p = os.path.join(C.DATA_DIR, "scan_latest.json")
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        age = dt.datetime.now(data_yahoo.IST) - dt.datetime.fromisoformat(c["built"])
        if age.total_seconds() < max_age_min * 60:
            return c
    except (OSError, ValueError, KeyError):
        pass
    return scan()


if __name__ == "__main__":
    import theme as T
    r = scan()
    print(T.banner(f"{T.FLAG} {T.term('scanner')}"))
    print(f"Swing scan for {r['day']} ({len(r['rows'])} stocks) - research only, the bot doesn't trade these")
    for k in SETUPS:
        names = [s for s, x in sorted(r["rows"].items()) if k in x["setups"]]
        print(f"  {k:22s} {len(names):2d}  {', '.join(names)}")
    if r["errors"]:
        print("  data errors:", ", ".join(r["errors"]))
