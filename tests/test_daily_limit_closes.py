"""The daily loss limit also closes what is open (review item, 19 Nov 2026).

It used to stop new entries only: with the limit reached, the positions already open stayed open and the day's
loss could go on growing in them. Now the bot closes them, in paper, in real money and in the backtest alike."""

import csv
import datetime as dt
import os

import backtest
import bot
import config as C
import risk
from conftest import IST, bar, history


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def breakout_day(day):
    """09:15 range 100-101, 09:30 inside, 09:45 LONG breakout (stop at the range low, 100.0)."""
    return [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
            bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
            bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)]


# a candle that falls, but not to the stop at 100.0: only the daily loss limit can close the trade on it
DROP = ("10:00", 101.2, 101.25, 100.5, 100.6, 2000)


def test_daily_limit_hit_counts_booked_and_open(tmp_path):
    st = risk.State(str(tmp_path / "s.json"), 100000.0)
    st.new_day_if_needed(dt.date(2026, 11, 19))
    assert not st.daily_limit_hit()
    st.record_entry("SBIN", "LONG", 100, 800.0, 790.0, None, "t0")
    assert not st.daily_limit_hit({"SBIN": 781.0})                             # -1,900 open: under 2%
    assert st.daily_limit_hit({"SBIN": 780.0})                                 # -2,000 open: the limit
    st.d["day_pnl"] = -1500.0
    assert st.daily_limit_hit({"SBIN": 795.0}) and not st.daily_limit_hit({"SBIN": 795.1})   # booked + open
    assert st.can_enter("INFY", dt.date(2026, 11, 19), {"SBIN": 795.0}) == (
        False, "daily loss limit 2.0% hit (booked + open)")


def test_backtest_closes_open_positions_at_the_limit(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    monkeypatch.setattr(C, "MAX_DAILY_LOSS_PCT", 0.1)                          # a small limit, so one trade reaches it
    hist, day = history()
    res = backtest.simulate({"SBIN": hist + breakout_day(day) + [bar(day, *DROP)]}, 100000, verbose=False)
    rows = read(os.path.join(C.DATA_DIR, "backtest_trades.csv"))
    assert res["trades"] == 1 and rows[0]["reason"] == "daily loss limit"
    assert abs(float(rows[0]["exit"]) - 100.6 * (1 - C.SLIPPAGE_PCT / 100)) < 0.006      # at the candle's close


def test_backtest_leaves_them_open_below_the_limit(monkeypatch):
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    later = [bar(day, *DROP), bar(day, "10:15", 100.6, 100.7, 99.5, 99.8, 2000)]   # the stop is hit a candle later
    backtest.simulate({"SBIN": hist + breakout_day(day) + later}, 100000, verbose=False)
    rows = read(os.path.join(C.DATA_DIR, "backtest_trades.csv"))
    assert len(rows) == 1 and rows[0]["reason"].startswith("stop")             # the usual 2% limit is far away


def paper_bot(monkeypatch, bars_ref, clock):
    import data_yahoo
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(C, "TARGET_R", 0)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars_ref[0], bars_ref[0][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])
    return bot.Bot()


def test_the_paper_bot_closes_at_the_limit_at_the_same_price_as_the_backtest(monkeypatch):
    hist, day = history()
    bars_ref = [hist + breakout_day(day)]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    b = paper_bot(monkeypatch, bars_ref, clock)
    b.cycle()
    assert "SBIN" in b.state.positions
    monkeypatch.setattr(C, "MAX_DAILY_LOSS_PCT", 0.1)
    b.cycle()                                                 # no candle since the entry yet: nothing to value it at
    assert "SBIN" in b.state.positions
    bars_ref[0] = bars_ref[0] + [bar(day, *DROP)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()
    assert "SBIN" not in b.state.positions
    row = read(os.path.join(C.DATA_DIR, "trades_paper.csv"))[0]
    assert row["reason"] == "daily loss limit"
    assert abs(float(row["exit"]) - 100.6 * (1 - C.SLIPPAGE_PCT / 100)) < 0.006            # as in the backtest
    assert any("Daily loss limit of 0.1% reached" in e for e in b.events)
    assert b.state.can_enter("INFY", day)[0] is False         # and nothing new opens today


def test_the_paper_bot_does_nothing_below_the_limit(monkeypatch):
    hist, day = history()
    bars_ref = [hist + breakout_day(day)]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    b = paper_bot(monkeypatch, bars_ref, clock)
    b.cycle()
    bars_ref[0] = bars_ref[0] + [bar(day, *DROP)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()
    assert "SBIN" in b.state.positions and not any("Daily loss limit" in e for e in b.events)
