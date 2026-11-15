import datetime as dt

import config as C
import strategy
from conftest import bar, history


def breakout_day(day, close=101.2, vol=3000, or_hi=101.0, or_lo=100.0):
    """09:15 OR 100-101, 09:30 inside, 09:45 breakout bar."""
    mid = round((or_hi + or_lo) / 2, 2)
    return [
        bar(day, "09:15", or_lo + 0.01, or_hi, or_lo, mid, 5000),
        bar(day, "09:30", mid, mid + 0.02, mid - 0.02, mid, 1000),   # inside the range
        bar(day, "09:45", mid, close + 0.1, mid, close, vol),
    ]


def test_long_breakout_signal():
    hist, day = history()
    ev = strategy.evaluate(hist + breakout_day(day))
    assert ev["signal"] == "LONG"
    assert ev["stop"] == 100.0
    assert ev["or_high"] == 101.0 and ev["or_low"] == 100.0
    assert ev["vol_ratio"] == 3.0


def test_short_breakout_signal():
    hist, day = history()
    bars = hist + [
        bar(day, "09:15", 100.5, 101.0, 100.0, 100.2, 5000),
        bar(day, "09:30", 100.2, 100.6, 100.1, 100.3, 1000),
        bar(day, "09:45", 100.3, 100.3, 99.7, 99.8, 3000),
    ]
    ev = strategy.evaluate(bars)
    assert ev["signal"] == "SHORT" and ev["stop"] == 101.0


def test_shorts_can_be_disabled(monkeypatch):
    monkeypatch.setattr(C, "ALLOW_SHORT", False)
    hist, day = history()
    bars = hist + [
        bar(day, "09:15", 100.5, 101.0, 100.0, 100.2, 5000),
        bar(day, "09:30", 100.2, 100.6, 100.1, 100.3, 1000),
        bar(day, "09:45", 100.3, 100.3, 99.7, 99.8, 3000),
    ]
    assert strategy.evaluate(bars)["signal"] is None


def test_no_signal_while_range_forming_or_inside():
    hist, day = history()
    d = breakout_day(day)
    assert strategy.evaluate(hist + d[:1])["reason"] == "opening range bar"
    assert strategy.evaluate(hist + d[:2])["reason"] == "inside range"


def test_only_fresh_breakout_counts():
    hist, day = history()
    d = breakout_day(day) + [bar(day, "10:00", 101.2, 101.6, 101.1, 101.5, 3000)]
    assert strategy.evaluate(hist + d)["signal"] is None  # prev bar already closed above


def test_low_volume_rejected():
    hist, day = history()
    ev = strategy.evaluate(hist + breakout_day(day, vol=1100))
    assert ev["signal"] is None and "volume" in ev["reason"]


def test_range_width_filters():
    hist, day = history()
    assert "range" in strategy.evaluate(hist + breakout_day(day, or_hi=100.1, close=100.3))["reason"]
    assert "range" in strategy.evaluate(hist + breakout_day(day, or_hi=103.0, close=103.2))["reason"]


def test_needs_volume_history():
    _, day = history()
    ev = strategy.evaluate(breakout_day(day))
    assert ev["signal"] is None and "history" in ev["reason"]


def test_late_breakout_rejected(monkeypatch):
    monkeypatch.setattr(C, "LAST_ENTRY", "09:30")
    hist, day = history()
    assert "after" in strategy.evaluate(hist + breakout_day(day))["reason"]


def test_vwap_filter():
    hist, day = history()
    # huge volume high in the OR pulls VWAP above the breakout close
    bars = hist + [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.9, 5000),
        bar(day, "09:30", 100.9, 104.0, 100.8, 100.9, 900000),
        bar(day, "09:45", 100.9, 101.3, 100.8, 101.2, 3000),
    ]
    # 09:30 bar: high 104 but OR is only the first 15 minutes, so OR high stays 101
    ev = strategy.evaluate(bars)
    assert ev["signal"] is None and "VWAP" in ev["reason"]


def test_check_exit_long():
    pos = {"direction": "LONG", "stop": 99.0, "target": 103.0}
    day = dt.date(2026, 11, 16)
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 101, 99.5, 100.5)) is None
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 101, 98.9, 100.5)) == ("stop", 99.0)
    assert strategy.check_exit(pos, bar(day, "10:00", 98.5, 99, 98, 98.2)) == ("stop (gap)", 98.5)
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 103.5, 99.5, 103)) == ("target", 103.0)
    # stop and target in the same bar -> stop (conservative)
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 103.5, 98.5, 103))[0] == "stop"


def test_check_exit_short():
    pos = {"direction": "SHORT", "stop": 101.0, "target": 97.0}
    day = dt.date(2026, 11, 16)
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 101.2, 99.5, 100.5)) == ("stop", 101.0)
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 100.5, 96.8, 97.5)) == ("target", 97.0)


def test_target_and_square_off_bar():
    assert strategy.target_for(100, 99, "LONG") == 102
    assert strategy.target_for(100, 101, "SHORT") == 98
    day = dt.date(2026, 11, 16)
    assert strategy.is_square_off_bar(bar(day, "14:45", 1, 1, 1, 1))      # ends 15:00 <= 15:10
    assert not strategy.is_square_off_bar(bar(day, "14:30", 1, 1, 1, 1))
    assert not strategy.is_square_off_bar(bar(day, "15:00", 1, 1, 1, 1))  # ends 15:15 > 15:10
