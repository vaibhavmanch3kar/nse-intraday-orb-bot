"""End-to-end paper trade through Bot.cycle with fake Yahoo data and a fake clock."""

import datetime as dt
import json
import os

import bot
import config as C
import data_yahoo
from conftest import IST, bar, history


def setup(monkeypatch, bars_ref, clock):
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars_ref[0], bars_ref[0][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])


def test_paper_trade_entry_then_stop(monkeypatch):
    hist, day = history()
    today = hist + [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
        bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
        bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000),
    ]
    bars_ref = [today]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    setup(monkeypatch, bars_ref, clock)

    b = bot.Bot()
    b.cycle()
    pos = b.state.positions["SBIN"]
    assert pos["direction"] == "LONG" and pos["stop"] == 100.0
    assert pos["entry"] == round(101.2 * 1.0005, 2)
    assert b.state.d["trades_today"] == 1

    # next bar drops through the stop
    bars_ref[0] = today + [bar(day, "10:00", 101.0, 101.1, 99.8, 99.9, 2000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()
    assert "SBIN" not in b.state.positions
    assert b.state.d["day_pnl"] < 0 and b.state.d["loss_streak"] == 1
    trades = open(os.path.join(C.DATA_DIR, "trades_paper.csv"), encoding="utf-8").read()
    assert "SBIN" in trades and "stop" in trades
    status = json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))
    assert status["mode"] == "paper" and status["trades_today"] == 1

    # same symbol is not traded twice in a day
    bars_ref[0] = bars_ref[0] + [bar(day, "10:15", 99.9, 101.5, 99.9, 101.4, 4000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 30, 30), tzinfo=IST)
    b.cycle()
    assert "SBIN" not in b.state.positions


def test_default_video_rules_in_bot_loop(monkeypatch):
    """Index filter + time stop + EMA trail, no target: the bot's own loop end to end."""
    for k, v in {"INDEX_FILTER": True, "TIME_STOP_BARS": 4, "TRAIL_EMA": 21, "TARGET_R": 0}.items():
        monkeypatch.setattr(C, k, v)
    hist, day = history()
    today = hist + [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
        bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
        bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000),
    ]
    bars_ref = [today]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    setup(monkeypatch, bars_ref, clock)  # index fetch returns the same rising bars -> index agrees
    b = bot.Bot()
    b.cycle()
    pos = b.state.positions["SBIN"]
    assert pos["target"] is None
    # four flat bars: after 1 hour the trade is < 0.5R up -> time stop
    flat = [bar(day, t, 101.25, 101.4, 101.1, 101.3, 1500) for t in ("10:00", "10:15", "10:30", "10:45")]
    bars_ref[0] = today + flat
    clock[0] = dt.datetime.combine(day, dt.time(11, 0, 30), tzinfo=IST)
    b.cycle()
    assert "SBIN" not in b.state.positions
    assert "time stop" in open(os.path.join(C.DATA_DIR, "trades_paper.csv"), encoding="utf-8").read()


def test_square_off_closes_positions(monkeypatch):
    hist, day = history()
    today = hist + [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
        bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
        bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000),
    ]
    bars_ref, clock = [today], [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    setup(monkeypatch, bars_ref, clock)
    b = bot.Bot()
    b.cycle()
    assert "SBIN" in b.state.positions
    clock[0] = dt.datetime.combine(day, dt.time(15, 10, 5), tzinfo=IST)
    b.cycle()
    assert not b.state.positions


def test_pause_blocks_entries(monkeypatch):
    hist, day = history()
    today = hist + [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
        bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
        bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000),
    ]
    setup(monkeypatch, [today], [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)])
    open(C.PAUSE_FILE, "w").close()
    b = bot.Bot()
    b.cycle()
    assert not b.state.positions
    assert "paused" in b.rows["SBIN"]["reason"]


def test_forming_bar_is_ignored(monkeypatch):
    hist, day = history()
    today = hist + [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
        bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
        bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000),  # still forming at 09:55
    ]
    setup(monkeypatch, [today], [dt.datetime.combine(day, dt.time(9, 55), tzinfo=IST)])
    b = bot.Bot()
    b.cycle()
    assert not b.state.positions
