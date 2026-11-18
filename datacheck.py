"""
Candle validation: is today's price data for one stock complete and possible?

The strategy reads the opening range from the 09:15 candle and its signals from each 15-minute close, so a
missing, repeated, misplaced or impossible candle can produce a signal that never happened. `problems` lists
what is wrong in plain words ([] = nothing); Kavach refuses a new entry in a stock with a problem (kavach.py).
It only reads the candles it is given.
"""

import datetime as dt

import config as C


def _hm(t):
    return f"{t:%H:%M}"


def problems(bars, day, interval_min=None):
    """What is wrong with `day`'s candles in `bars` (dicts with t, o, h, l, c, v; any order of days, oldest first).
    Checks: the session starts with the opening candle; candles follow each other exactly one interval apart
    (none missing, repeated or out of order); each is on the time grid; each candle is possible (positive prices,
    high and low contain open and close, volume not negative)."""
    step = dt.timedelta(minutes=interval_min or C.INTERVAL_MIN)
    today = [b for b in bars if b["t"].date() == day]
    if not today:
        return []
    out = []
    oh, om = map(int, C.MARKET_OPEN.split(":"))
    first = today[0]["t"]
    if (first.hour, first.minute) != (oh, om):
        out.append(f"the {C.MARKET_OPEN} opening candle is missing (the first candle is {_hm(first)})")
    for prev, b in zip(today, today[1:]):
        gap = b["t"] - prev["t"]
        if gap <= dt.timedelta(0):
            out.append(f"candles repeated or out of order at {_hm(b['t'])}")
        elif gap > step:
            out.append(f"candles missing between {_hm(prev['t'])} and {_hm(b['t'])}")
        elif gap < step:
            out.append(f"a candle at {_hm(b['t'])} is off the {int(step.total_seconds() // 60)}-minute grid")
    for b in today:
        o, h, lo, c = b["o"], b["h"], b["l"], b["c"]
        try:
            possible = lo > 0 and h >= max(o, c) - 1e-9 and lo <= min(o, c) + 1e-9 and h >= lo
        except TypeError:
            possible = False
        if not possible:
            out.append(f"impossible candle at {_hm(b['t'])} (open {o}, high {h}, low {lo}, close {c})")
        elif (b.get("v") or 0) < 0:
            out.append(f"negative volume at {_hm(b['t'])}")
    return out


def has_bar(bars, when):
    """True if `bars` (for example an index) has a candle starting exactly at `when`."""
    return any(b["t"] == when for b in (bars or []))
