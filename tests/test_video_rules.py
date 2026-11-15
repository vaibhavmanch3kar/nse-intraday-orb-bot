"""Optional rules from the TWS risk-management videos (all off by default)."""

import datetime as dt

import config as C
import risk
import strategy
from conftest import bar, history


def test_index_filter(monkeypatch):
    day = dt.date(2026, 11, 16)
    up = [bar(day, "09:15", 100, 101, 99, 100.5), bar(day, "09:30", 100.5, 101, 100, 100.8)]
    down = [bar(day, "09:15", 100, 101, 99, 99.5), bar(day, "09:30", 99.5, 100, 99, 99.2)]
    t = up[1]["t"]
    assert strategy.index_agrees(up, t, "LONG")[0] and not strategy.index_agrees(up, t, "SHORT")[0]
    assert strategy.index_agrees(down, t, "SHORT")[0] and not strategy.index_agrees(down, t, "LONG")[0]
    assert strategy.index_agrees(None, t, "LONG") == (True, "no index data")  # never blocks on missing data

    hist, d = history()
    bars = hist + [bar(d, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
                   bar(d, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
                   bar(d, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)]
    idx_down = [bar(d, "09:15", 200, 200, 198, 199), bar(d, "09:30", 199, 199, 197, 198),
                bar(d, "09:45", 198, 198, 196, 197)]
    assert strategy.evaluate(bars, index_bars=idx_down)["signal"] == "LONG"  # filter off by default
    monkeypatch.setattr(C, "INDEX_FILTER", True)
    ev = strategy.evaluate(bars, index_bars=idx_down)
    assert ev["signal"] is None and "against index" in ev["reason"]
    assert strategy.index_for("SBIN") == "^NSEBANK" and strategy.index_for("RELIANCE") == "^NSEI"


def test_vix_multiplier_and_sizing():
    assert strategy.vix_multiplier(12) == 1.0
    assert strategy.vix_multiplier(17) == 0.75
    assert strategy.vix_multiplier(25) == 0.5
    assert strategy.vix_multiplier(None) == 1.0
    full, _ = risk.size_position(100000, 500, 490, "LONG")
    half, _ = risk.size_position(100000, 500, 490, "LONG", risk_mult=0.5)
    assert half < full and half >= full // 2 - 1


def test_no_target_when_target_r_zero(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    assert strategy.target_for(100, 99, "LONG") is None
    pos = {"direction": "LONG", "stop": 99.0, "target": None, "entry": 100.0}
    day = dt.date(2026, 11, 16)
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 150, 99.5, 140)) is None
    assert strategy.check_exit(pos, bar(day, "10:00", 100, 101, 98, 99))[0] == "stop"


def test_ema_trail_exit(monkeypatch):
    monkeypatch.setattr(C, "TRAIL_EMA", 3)
    day = dt.date(2026, 11, 16)
    bars = [bar(day, f"{9 + (15 + 15 * k) // 60:02d}:{(15 + 15 * k) % 60:02d}", c, c + 0.1, c - 0.1, c)
            for k, c in enumerate([100, 101, 102, 103, 104, 102.5])]
    pos = {"direction": "LONG", "stop": 95.0, "target": None, "entry": 100.0}
    assert strategy.manage_exit(pos, bars, 4, 4) is None              # rising, above EMA
    assert strategy.manage_exit(pos, bars, 5, 5) == ("trail EMA3", 102.5)  # close fell below EMA


def test_time_stop(monkeypatch):
    monkeypatch.setattr(C, "TIME_STOP_BARS", 2)
    monkeypatch.setattr(C, "TIME_STOP_MIN_R", 0.5)
    day = dt.date(2026, 11, 16)
    flat = [bar(day, "10:00", 100, 100.3, 99.8, 100.1), bar(day, "10:15", 100.1, 100.3, 99.9, 100.2)]
    pos = {"direction": "LONG", "stop": 99.0, "target": 102.0, "entry": 100.0}
    assert strategy.manage_exit(pos, flat, 0, 1) is None
    assert strategy.manage_exit(pos, flat, 1, 2) == ("time stop", 100.2)   # only +0.2R after 2 bars
    good = [bar(day, "10:00", 100, 100.8, 99.9, 100.7), bar(day, "10:15", 100.7, 100.9, 100.5, 100.6)]
    pos = {"direction": "LONG", "stop": 99.0, "target": 102.0, "entry": 100.0}
    assert strategy.manage_exit(pos, good, 0, 2) is None and pos["time_ok"]  # +0.7R: passes, checked once
    assert strategy.manage_exit(pos, good, 1, 3) is None
