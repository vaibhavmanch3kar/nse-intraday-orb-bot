"""Advisor Phase 1: the conditions at the signal bar are saved with every trade.
Recording only - these tests also pin that nothing about the trade itself changes."""

import csv
import datetime as dt
import os

import backtest
import bot
import config as C
import strategy
from conftest import IST, bar, history


def breakout_day(day):
    """09:15 OR 100-101 (opens 100.2 after a 100.0 close), 09:30 inside, 09:45 LONG breakout."""
    return [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
            bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
            bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)]


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_signal_carries_context_and_non_signal_does_not():
    hist, day = history()
    bars = hist + breakout_day(day)
    ev = strategy.evaluate(bars, index_bars=bars)
    assert ev["signal"] == "LONG"
    ctx = ev["context"]
    assert list(ctx) == strategy.CONTEXT_COLS
    assert ctx["signal_bar"] == "09:45"
    assert abs(ctx["or_width_pct"] - 1.0) < 1e-9          # (101 - 100) / 100
    assert abs(ctx["vol_ratio"] - 3.0) < 1e-9             # 3000 vs the 1000 slot average
    assert ctx["vwap_dist_pct"] > 0                       # long closes above VWAP
    assert abs(ctx["gap_pct"] - 0.2) < 1e-9               # 100.2 open vs 100.0 previous close
    assert abs(ctx["index_chg_pct"] - (101.2 / 100.2 - 1) * 100) < 1e-9

    inside = strategy.evaluate(bars[:-1])                 # 09:30 bar: inside the range
    assert inside["signal"] is None and inside["context"] is None


def test_missing_index_gives_empty_index_change():
    hist, day = history()
    ev = strategy.evaluate(hist + breakout_day(day), index_bars=None)
    assert ev["signal"] == "LONG" and ev["context"]["index_chg_pct"] is None


def test_backtest_trade_list_has_context_columns(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    after = [bar(day, "10:00", 101.2, 101.3, 101.1, 101.2, 2000),
             bar(day, "10:15", 101.2, 101.2, 99.5, 99.8, 2000)]       # stopped out
    res = backtest.simulate({"SBIN": hist + breakout_day(day) + after}, 100000, verbose=False)
    assert res["trades"] == 1
    rows = read(os.path.join(C.DATA_DIR, "backtest_trades.csv"))
    for col in strategy.CONTEXT_COLS:
        assert col in rows[0]
    assert rows[0]["signal_bar"] == "09:45"
    assert float(rows[0]["or_width_pct"]) == 1.0
    assert float(rows[0]["vol_ratio"]) == 3.0
    assert rows[0]["reason"].startswith("stop")


def test_paper_log_has_context_columns(monkeypatch):
    import data_yahoo
    hist, day = history()
    bars_ref = [hist + breakout_day(day)]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars_ref[0], bars_ref[0][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])

    b = bot.Bot()
    b.cycle()
    assert b.state.positions["SBIN"]["signal_bar"] == "09:45"
    bars_ref[0] = bars_ref[0] + [bar(day, "10:00", 101.0, 101.1, 99.8, 99.9, 2000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    rows = read(path)
    assert len(rows) == 1
    with open(path, encoding="utf-8") as f:
        assert f.readline().strip().split(",") == bot.TRADE_COLS
    assert rows[0]["signal_bar"] == "09:45" and float(rows[0]["or_width_pct"]) == 1.0
    assert float(rows[0]["gap_pct"]) == 0.2


def test_old_paper_log_is_upgraded_without_losing_rows():
    old_cols = ["closed", "symbol", "direction", "qty", "entry", "exit", "stop", "target",
                "gross", "charges", "net", "r", "reason", "opened"]
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    old = {"closed": "2026-11-16 10:15:00+05:30", "symbol": "INFY", "direction": "LONG", "qty": "5",
           "entry": "1500.0", "exit": "1490.0", "stop": "1490.0", "target": "", "gross": "-50.0",
           "charges": "20.0", "net": "-70.0", "r": "-1.4", "reason": "stop", "opened": "2026-11-16 09:45:00+05:30"}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=old_cols)
        w.writeheader()
        w.writerow(old)

    bot.log_trade({"closed": "2026-11-17 11:00:00+05:30", "symbol": "SBIN", "direction": "SHORT",
                   "qty": 3, "net": 12.5, "reason": "time stop", "signal_bar": "10:30",
                   "or_width_pct": 0.8}, "paper")
    rows = read(path)
    with open(path, encoding="utf-8") as f:
        assert f.readline().strip().split(",") == bot.TRADE_COLS
    assert len(rows) == 2
    assert {k: rows[0][k] for k in old_cols} == old       # old row unchanged
    assert rows[0]["signal_bar"] == "" and rows[0]["or_width_pct"] == ""
    assert rows[1]["symbol"] == "SBIN" and rows[1]["signal_bar"] == "10:30"
