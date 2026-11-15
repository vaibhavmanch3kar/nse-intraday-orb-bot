"""Backtest engine honesty: equity marked to market, fills, and no look-ahead."""

import datetime as dt

import backtest
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
