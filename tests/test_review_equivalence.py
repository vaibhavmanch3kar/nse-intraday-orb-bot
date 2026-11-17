"""The Review and History results must never change when their code is made faster.

`reference_replay_day` below is the Review replay exactly as it was in v1.8.0 (it evaluates every bar on the
whole history). The real `review.replay_day` and `timemachine.history` must give identical results on
seeded random sessions, with the original rules and with the bot's current rules (EMA trail, time stop,
index filter, no fixed target)."""

import datetime as dt
import random

import pytest

import config as C
import costs
import review
import strategy
import timemachine
from conftest import IST


def reference_replay_day(bars, day, index_bars=None):
    """v1.8.0 review.replay_day, unchanged (the slow, obviously-correct version)."""
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


def sessions(seed, n_days=14, gaps=True):
    """Seeded random 15-min sessions (09:15-15:15) with volume spikes, overnight gaps, a holiday-like
    missing day and the odd short session, plus a matching index series."""
    rnd = random.Random(seed)
    bars, idx = [], []
    d, px, ipx = dt.date(2026, 9, 1), 100.0, 20000.0
    made = 0
    while made < n_days:
        d += dt.timedelta(days=1)
        if d.weekday() >= 5 or (gaps and rnd.random() < 0.05):
            continue
        made += 1
        px *= 1 + rnd.gauss(0, 0.01)
        n = 25 if rnd.random() > 0.1 else rnd.randint(4, 24)        # some short sessions
        t = dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST)
        for k in range(n):
            o = px
            c = o * (1 + rnd.gauss(0, 0.004))
            h, lo = max(o, c) * (1 + abs(rnd.gauss(0, 0.002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
            v = 1000 * rnd.lognormvariate(0, 0.5) * (4 if rnd.random() < 0.08 else 1)
            bars.append({"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v})
            io = ipx
            ipx *= 1 + rnd.gauss(0, 0.002)
            idx.append({"t": t, "o": io, "h": max(io, ipx), "l": min(io, ipx), "c": ipx, "v": 0})
            px, t = c, t + dt.timedelta(minutes=15)
    return bars, idx


BOT_RULES = {"INDEX_FILTER": True, "TRAIL_EMA": 21, "TIME_STOP_BARS": 4, "TARGET_R": 0.0}
ORIGINAL_RULES = {"INDEX_FILTER": False, "TRAIL_EMA": 0, "TIME_STOP_BARS": 0, "TARGET_R": 2.0}


@pytest.mark.parametrize("rules", [ORIGINAL_RULES, BOT_RULES], ids=["original-rules", "bot-rules"])
def test_replay_day_matches_reference(monkeypatch, rules):
    for k, v in rules.items():
        monkeypatch.setattr(C, k, v)
    signals = 0
    for seed in range(12):
        bars, idx = sessions(seed)
        for day in sorted({b["t"].date() for b in bars}) + [dt.date(2030, 1, 1)]:
            want = reference_replay_day(bars, day, idx)
            assert review.replay_day(bars, day, idx) == want, (seed, day)
            signals += want["direction"] is not None
    assert signals >= 20   # the comparison really covered trades, not only quiet days


def test_history_matches_reference(monkeypatch):
    for k, v in BOT_RULES.items():
        monkeypatch.setattr(C, k, v)
    for seed in range(4):
        bars, idx = sessions(seed, n_days=20)
        day = max(b["t"].date() for b in bars)
        for direction in (None, "LONG", "SHORT"):
            got = timemachine.history(bars, day, direction, idx)
            days = sorted({b["t"].date() for b in bars if b["t"].date() < day})[C.VOLUME_LOOKBACK_DAYS:]
            rs = [r for r in (reference_replay_day(bars, d, idx) for d in days)
                  if r.get("r") is not None and (not direction or r["direction"] == direction)]
            assert got["sessions"] == len(days) and got["fired"] == len(rs)
            assert got["avg_r"] == ((sum(r["r"] for r in rs) / len(rs)) if rs else None)


def test_unsorted_bars_still_match(monkeypatch):
    """Bars out of time order must still give the reference answer (the fast path must not assume order)."""
    for k, v in BOT_RULES.items():
        monkeypatch.setattr(C, k, v)
    bars, idx = sessions(3)
    shuffled = bars[:]
    random.Random(1).shuffle(shuffled)
    for day in sorted({b["t"].date() for b in bars})[-3:]:
        assert review.replay_day(shuffled, day, idx) == reference_replay_day(shuffled, day, idx)
