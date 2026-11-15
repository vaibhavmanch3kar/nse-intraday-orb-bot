"""
Opening range breakout (ORB) on 15-minute NSE candles. Shared by the live loop and backtest.

Rules, evaluated on each CLOSED 15m bar of today:
  Opening range (OR) = high/low of the first OR_MINUTES (default the 09:15 bar).
  LONG  when the bar closes above OR high and the previous bar closed at/below it.
  SHORT when the bar closes below OR low and the previous bar closed at/above it.
  Filters: OR width between MIN_OR_PCT and MAX_OR_PCT of price;
           bar volume >= VOLUME_MULT x average volume of the same time slot on prior days;
           close above VWAP for longs / below for shorts;
           bar starts no later than LAST_ENTRY.
  Stop = other side of the OR.  Target = entry +/- TARGET_R x risk.
  Exit at stop, target, or SQUARE_OFF time, whichever comes first.
"""

import datetime as dt
from collections import OrderedDict

import config as C


def hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def by_day(bars):
    days = OrderedDict()
    for b in bars:
        days.setdefault(b["t"].date(), []).append(b)
    return days


def opening_range(day_bars, or_minutes=None):
    """OR from today's bars, or None until all OR bars exist."""
    or_minutes = or_minutes or C.OR_MINUTES
    open_t = hm(C.MARKET_OPEN)
    if not day_bars:
        return None
    start = dt.datetime.combine(day_bars[0]["t"].date(), open_t, tzinfo=day_bars[0]["t"].tzinfo)
    end = start + dt.timedelta(minutes=or_minutes)
    or_bars = [b for b in day_bars if start <= b["t"] < end]
    expected = max(1, or_minutes // C.INTERVAL_MIN)
    if len(or_bars) < expected:
        return None
    return {"high": max(b["h"] for b in or_bars), "low": min(b["l"] for b in or_bars), "end": end}


def vwap(day_bars):
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in day_bars)
    vol = sum(b["v"] for b in day_bars)
    return pv / vol if vol > 0 else day_bars[-1]["c"]


def slot_avg_volume(prior_days, slot: dt.time, lookback=None):
    """Average volume of the bar starting at `slot` over the last `lookback` prior sessions."""
    lookback = lookback or C.VOLUME_LOOKBACK_DAYS
    vols = []
    for day_bars in list(prior_days)[-lookback:]:
        for b in day_bars:
            if b["t"].time() == slot and b["v"] > 0:
                vols.append(b["v"])
                break
    return (sum(vols) / len(vols), len(vols)) if vols else (None, 0)


def index_for(symbol):
    return C.SECTOR_INDEX.get(symbol, C.INDEX_DEFAULT)


def index_agrees(index_bars, bar_time, direction):
    """Correlation filter: the index must be above its day open for longs, below for shorts.
    Returns (ok, text). Missing index data never blocks a trade."""
    if not index_bars:
        return True, "no index data"
    day = [b for b in index_bars if b["t"].date() == bar_time.date() and b["t"] <= bar_time]
    if not day or day[-1]["t"] != bar_time:
        return True, "no index bar"
    chg = (day[-1]["c"] / day[0]["o"] - 1) * 100
    ok = chg > 0 if direction == "LONG" else chg < 0
    return ok, f"index {chg:+.2f}%"


def vix_multiplier(vix):
    """Risk multiplier from India VIX (None -> 1.0)."""
    if vix is None:
        return 1.0
    for upto, mult in C.VIX_BANDS:
        if vix < upto:
            return mult
    return C.VIX_BANDS[-1][1]


def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def evaluate(bars, idx=None, index_bars=None):
    """Evaluate the closed bar at bars[idx] (default: last). bars = oldest-first closed bars.

    Returns a dict with 'signal' in {'LONG', 'SHORT', None} and the reasons, so the
    dashboard can show why a stock is or isn't tradeable.
    """
    if idx is None:
        idx = len(bars) - 1
    out = {"signal": None, "reason": "", "or_high": None, "or_low": None,
           "close": None, "vwap": None, "vol_ratio": None, "stop": None, "bar_time": None}
    if idx < 0:
        out["reason"] = "no data"
        return out
    bar = bars[idx]
    out["close"], out["bar_time"] = bar["c"], bar["t"]
    today = bar["t"].date()
    day_bars = [b for b in bars[: idx + 1] if b["t"].date() == today]
    prior = [d for day, d in by_day(bars[: idx + 1]).items() if day < today]

    orng = opening_range(day_bars)
    if not orng:
        out["reason"] = "opening range forming"
        return out
    out["or_high"], out["or_low"] = orng["high"], orng["low"]
    if bar["t"] < orng["end"]:
        out["reason"] = "opening range bar"
        return out

    out["vwap"] = vw = vwap(day_bars)
    width_pct = (orng["high"] - orng["low"]) / orng["low"] * 100
    avg_vol, n_days = slot_avg_volume(prior, bar["t"].time())
    if avg_vol:
        out["vol_ratio"] = bar["v"] / avg_vol

    pos_in_day = day_bars.index(bar)
    prev_close = day_bars[pos_in_day - 1]["c"] if pos_in_day > 0 else None

    direction = None
    if prev_close is not None and bar["c"] > orng["high"] >= prev_close:
        direction = "LONG"
    elif C.ALLOW_SHORT and prev_close is not None and bar["c"] < orng["low"] <= prev_close:
        direction = "SHORT"
    if not direction:
        out["reason"] = "inside range" if orng["low"] <= bar["c"] <= orng["high"] else "no fresh breakout"
        return out

    if bar["t"].time() > hm(C.LAST_ENTRY):
        out["reason"] = f"{direction} after {C.LAST_ENTRY}"
        return out
    if width_pct < C.MIN_OR_PCT:
        out["reason"] = f"range {width_pct:.2f}% < {C.MIN_OR_PCT}%"
        return out
    if width_pct > C.MAX_OR_PCT:
        out["reason"] = f"range {width_pct:.2f}% > {C.MAX_OR_PCT}%"
        return out
    if n_days < 3 or out["vol_ratio"] is None:
        out["reason"] = "not enough volume history"
        return out
    if out["vol_ratio"] < C.VOLUME_MULT:
        out["reason"] = f"volume {out['vol_ratio']:.1f}x < {C.VOLUME_MULT}x"
        return out
    if C.USE_VWAP_FILTER and ((direction == "LONG" and bar["c"] <= vw) or
                              (direction == "SHORT" and bar["c"] >= vw)):
        out["reason"] = "wrong side of VWAP"
        return out
    if C.INDEX_FILTER:
        ok, txt = index_agrees(index_bars, bar["t"], direction)
        if not ok:
            out["reason"] = f"{direction} against index ({txt})"
            return out

    out["signal"] = direction
    if C.STOP_RULE == "mid":
        out["stop"] = (orng["high"] + orng["low"]) / 2
    else:
        out["stop"] = orng["low"] if direction == "LONG" else orng["high"]
    out["reason"] = f"{direction} breakout"
    return out


def target_for(entry, stop, direction):
    """Fixed target at TARGET_R, or None when TARGET_R <= 0 (no fixed target)."""
    if C.TARGET_R <= 0:
        return None
    risk = abs(entry - stop)
    return entry + C.TARGET_R * risk if direction == "LONG" else entry - C.TARGET_R * risk


def check_exit(pos, bar):
    """Stop/target hit inside a closed bar? Returns (reason, price) or None.
    If both are inside the same bar the stop is assumed first (conservative)."""
    d, stop, tgt = pos["direction"], pos["stop"], pos.get("target")
    if d == "LONG":
        if bar["o"] <= stop:
            return "stop (gap)", bar["o"]
        if bar["l"] <= stop:
            return "stop", stop
        if tgt is not None and bar["o"] >= tgt:
            return "target (gap)", bar["o"]
        if tgt is not None and bar["h"] >= tgt:
            return "target", tgt
    else:
        if bar["o"] >= stop:
            return "stop (gap)", bar["o"]
        if bar["h"] >= stop:
            return "stop", stop
        if tgt is not None and bar["o"] <= tgt:
            return "target (gap)", bar["o"]
        if tgt is not None and bar["l"] <= tgt:
            return "target", tgt
    return None


def manage_exit(pos, bars, i, bars_held):
    """All exit rules for the closed bar bars[i], `bars_held` bars after entry (1 = first bar).
    Order: stop/target inside the bar, then EMA trail and time stop on the bar's close."""
    bar = bars[i]
    hit = check_exit(pos, bar)
    if hit:
        return hit
    d, entry, close = pos["direction"], pos["entry"], bar["c"]
    sign = 1 if d == "LONG" else -1
    if C.TRAIL_EMA > 0:
        closes = [b["c"] for b in bars[max(0, i - 4 * C.TRAIL_EMA): i + 1]]
        e = ema(closes, C.TRAIL_EMA)
        if (close - e) * sign < 0:
            return f"trail EMA{C.TRAIL_EMA}", close
    if C.TIME_STOP_BARS > 0 and bars_held >= C.TIME_STOP_BARS and not pos.get("time_ok"):
        risk = abs(entry - pos["stop"]) or 1e-9
        if (close - entry) * sign / risk < C.TIME_STOP_MIN_R:
            return "time stop", close
        pos["time_ok"] = True  # passed the time check once; from here only stop/trail/target apply
    return None


def is_square_off_bar(bar):
    """True for the last bar that ends at or before SQUARE_OFF (exit at its close)."""
    end = (dt.datetime.combine(bar["t"].date(), bar["t"].time()) + dt.timedelta(minutes=C.INTERVAL_MIN)).time()
    nxt_end = (dt.datetime.combine(bar["t"].date(), end) + dt.timedelta(minutes=C.INTERVAL_MIN)).time()
    return end <= hm(C.SQUARE_OFF) < nxt_end
