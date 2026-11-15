"""Backtest engine honesty: equity marked to market, fills, and no look-ahead."""

import datetime as dt

import backtest
import strategy
import config as C
from conftest import bar, history


def breakout_session(day, after):
    """09:15 OR 100-101, 09:30 inside, 09:45 LONG breakout (stop 100.0), then `after` bars."""
    return [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
            bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
            bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)] + after


def run(bars):
    return backtest.simulate({"SBIN": bars}, 100000, verbose=False)


def test_drawdown_counts_losses_inside_open_trades(monkeypatch):
    """A trade that dips deep while open, then recovers before it exits, must still show the dip."""
    monkeypatch.setattr(C, "TARGET_R", 0)  # no target, exit only at square-off
    hist, day = history()
    after = [bar(day, "10:00", 101.2, 101.3, 101.1, 101.2, 2000),   # entry at this open
             bar(day, "10:15", 101.2, 101.2, 100.2, 100.25, 2000),  # deep open loss, stop 100.0 not hit
             bar(day, "10:30", 100.25, 101.3, 100.2, 101.25, 2000)]
    t = dt.datetime.combine(day, dt.time(10, 45))
    while t.time() <= dt.time(15, 0):                                  # flat until square-off
        after.append(bar(day, t.strftime("%H:%M"), 101.25, 101.3, 101.2, 101.25, 2000))
        t += dt.timedelta(minutes=15)
    res = run(hist + breakout_session(day, after))
    assert res["trades"] == 1
    # qty is capped at 25% of equity (246 shares), so the open loss at 10:15 is about
    # 246 x 1.0 = 0.25% of equity. Before the fix the curve ignored it and showed only charges.
    assert res["max_dd_pct"] > 0.2, res["max_dd_pct"]


# ---------------------------------------------------------------- fills
def test_entry_fills_at_next_bar_open_not_signal_close(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    nxt = bar(day, "10:00", 101.6, 101.8, 101.5, 101.7, 2000)   # opens above the 101.2 signal close
    res = run(hist + breakout_session(day, [nxt, bar(day, "10:15", 101.7, 101.8, 99.5, 99.8)]))  # then stopped
    import csv
    import os
    rows = list(csv.DictReader(open(os.path.join(C.DATA_DIR, "backtest_trades.csv"), encoding="utf-8")))
    assert res["trades"] == 1
    entry = float(rows[0]["entry"])
    assert abs(entry - 101.6 * (1 + C.SLIPPAGE_PCT / 100)) < 0.01   # next open + slippage
    assert rows[0]["opened"].startswith(f"{day} 10:00")


def test_gap_through_stop_fills_at_the_open_not_the_stop(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    after = [bar(day, "10:00", 101.2, 101.3, 101.1, 101.2, 2000),
             bar(day, "10:15", 99.0, 99.2, 98.8, 99.1, 2000)]     # gaps below the 100.0 stop
    run(hist + breakout_session(day, after))
    import csv
    import os
    rows = list(csv.DictReader(open(os.path.join(C.DATA_DIR, "backtest_trades.csv"), encoding="utf-8")))
    assert rows[0]["reason"] == "stop (gap)"
    exit_ = float(rows[0]["exit"])
    assert exit_ < 100.0 and abs(exit_ - 99.0 * (1 - C.SLIPPAGE_PCT / 100)) < 0.01  # open - slippage
    assert float(rows[0]["r"]) < -1.0                                              # worse than 1R


def test_stop_inside_a_bar_fills_at_stop_minus_slippage(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    after = [bar(day, "10:00", 101.2, 101.3, 101.1, 101.2, 2000),
             bar(day, "10:15", 101.0, 101.1, 99.8, 99.9, 2000)]   # trades through 100.0 inside the bar
    run(hist + breakout_session(day, after))
    import csv
    import os
    rows = list(csv.DictReader(open(os.path.join(C.DATA_DIR, "backtest_trades.csv"), encoding="utf-8")))
    assert rows[0]["reason"] == "stop"
    assert abs(float(rows[0]["exit"]) - 100.0 * (1 - C.SLIPPAGE_PCT / 100)) < 0.01  # never exactly the stop


# ---------------------------------------------------------------- no look-ahead
def test_signal_does_not_change_when_future_bars_change():
    hist, day = history()
    base = hist + breakout_session(day, [bar(day, "10:00", 101.2, 101.3, 101.1, 101.2)])
    i = len(hist) + 2                                             # the 09:45 breakout bar
    before = strategy.evaluate(base, i)
    wild = base[: i + 1] + [bar(day, "10:00", 90, 120, 80, 85, 10 ** 9),
                            bar(day, "10:15", 85, 86, 70, 71, 10 ** 9)]
    after = strategy.evaluate(wild, i)
    assert before["signal"] == after["signal"] == "LONG"
    for k in ("stop", "or_high", "or_low", "vwap", "vol_ratio", "reason"):
        assert before[k] == after[k], k


def test_index_filter_ignores_future_index_bars(monkeypatch):
    monkeypatch.setattr(C, "INDEX_FILTER", True)
    hist, day = history()
    bars = hist + breakout_session(day, [])
    i = len(bars) - 1
    idx_up = [bar(day, "09:15", 200, 201, 199, 200.5), bar(day, "09:30", 200.5, 201, 200, 200.8),
              bar(day, "09:45", 200.8, 201.5, 200.6, 201.2)]
    later_crash = idx_up + [bar(day, "10:00", 201.2, 201.2, 150, 151), bar(day, "10:15", 151, 152, 140, 141)]
    assert strategy.evaluate(bars, i, idx_up)["signal"] == "LONG"
    assert strategy.evaluate(bars, i, later_crash)["signal"] == "LONG"


def test_earlier_trades_unchanged_when_later_days_change(monkeypatch):
    """Whatever happens on later days must not change trades that closed before them."""
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    t = dt.datetime.combine(day, dt.time(10, 0))
    rest = []
    while t.time() <= dt.time(15, 15):
        rest.append(bar(day, t.strftime("%H:%M"), 101.2, 101.4, 101.1, 101.3, 2000))
        t += dt.timedelta(minutes=15)
    first = hist + breakout_session(day, rest)
    nd = day + dt.timedelta(days=3 if day.weekday() == 4 else 1)
    calm = [bar(nd, f"{9 + (15 + 15 * k) // 60:02d}:{(15 + 15 * k) % 60:02d}", 101, 101.2, 100.8, 101) for k in range(25)]
    crazy = [bar(nd, f"{9 + (15 + 15 * k) // 60:02d}:{(15 + 15 * k) % 60:02d}", 101 + k, 130, 60, 101 - k, 10 ** 8)
             for k in range(25)]
    import csv
    import os

    def trades_of(bars):
        run(bars)
        rows = list(csv.DictReader(open(os.path.join(C.DATA_DIR, "backtest_trades.csv"), encoding="utf-8")))
        return [r for r in rows if r["closed"][:10] == str(day)]

    a, b = trades_of(first + calm), trades_of(first + crazy)
    assert a and a == b
