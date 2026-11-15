"""Day Trader skill extras in the Time Machine: +/-2-bar pooling, verdict, close range, real history."""

import datetime as dt

import numpy as np

import timemachine
from conftest import IST, bar, history


def test_pooling_window_draws_from_neighbouring_slots():
    day = dt.date(2026, 11, 13)
    bars = [bar(day, f"10:{m:02d}", 100, 100, 100, 100 + m / 100) for m in range(0, 60, 5)]
    exact = timemachine.slot_moves(bars * 1, window=0)
    pooled = timemachine.slot_moves(bars * 1, window=2)
    assert not exact                                   # 1 bar per slot < 5 -> nothing usable
    assert len(pooled["10:25"]) == 5                    # 10:15..10:35 pooled together


def test_verdict_lines():
    def res(hit, stop):
        return {"n": 1000, "targets": {1.0: {"hit_first": hit, "stop_first": stop, "neither": 1 - hit - stop},
                                       "2.0": {"hit_first": hit / 3, "stop_first": stop, "neither": 0}}}
    assert timemachine.verdict(res(0.30, 0.28)).startswith("Close to a coin flip")
    assert timemachine.verdict(res(0.40, 0.30)).startswith("Leans toward the target")
    assert timemachine.verdict(res(0.17, 0.37)).startswith("Strongly toward the stop")
    assert "hit neither" in timemachine.verdict(res(0.2, 0.2))       # 60% neither -> extra line
    assert timemachine.verdict(res(0.01, 0.02)).startswith("No verdict")
    # 2R: break-even is 33% of decided replays; string keys (after JSON) still work
    assert "break-even is 33%" in timemachine.verdict(res(0.30, 0.60), 2.0)
    assert timemachine.verdict({"n": 0}).startswith("No verdict")


def test_close_range_and_seed():
    rng = np.random.default_rng(3)
    moves, t = {}, dt.datetime(2026, 1, 1, 9, 20)
    while t.strftime("%H:%M") < "15:30":
        c = rng.normal(0, 0.002, 300)
        c -= c.mean()
        moves[t.strftime("%H:%M")] = np.c_[np.maximum(c, 0), np.minimum(c, 0), c]
        t += dt.timedelta(minutes=5)
    a = timemachine.replay(moves, "LONG", 100, 99, "09:45", "15:10", n=4000)
    b = timemachine.replay(moves, "LONG", 100, 99, "09:45", "15:10", n=4000)
    assert a == b and a["seed"] == timemachine.SEED                 # reproducible
    assert a["close_p5"] < a["close_median"] < a["close_p95"]
    assert 99 < a["close_median"] < 101


def test_history_counts_real_signals():
    hist, day = history(days=6)
    # day 7: a long breakout that runs, so the replay records a result
    d7 = [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
          bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
          bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000),
          bar(day, "10:00", 101.2, 101.5, 101.1, 101.4, 2000),
          bar(day, "10:15", 101.4, 101.4, 99.5, 99.8, 2000)]   # stopped
    nxt = day + dt.timedelta(days=3 if day.weekday() == 4 else 1)
    bars = hist + d7
    h = timemachine.history(bars, nxt, "LONG")
    assert h["fired"] == 1 and h["losses"] == 1 and h["avg_r"] < -1
    assert "fired 1 times" in timemachine.history_text(h)
    assert timemachine.history(bars, nxt, "SHORT")["fired"] == 0
    assert "never fired" in timemachine.history_text(timemachine.history(bars, nxt, "SHORT"))
    assert timemachine.history(bars, day, "LONG")["fired"] == 0     # only EARLIER sessions count
