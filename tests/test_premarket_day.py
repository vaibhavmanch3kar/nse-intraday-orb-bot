"""Before 09:15 the bot already starts the new day: the dashboard must not show yesterday's trades and P&L as
today's. Nothing else changes: cash, open positions and the trading rules are untouched."""
import datetime as dt
import json
import os

import bot
import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WED_0900 = dt.datetime(2026, 11, 18, 9, 0, tzinfo=IST)     # a trading day, before the open


def yesterday_state(b):
    b.state.d.update(day="2026-11-17", trades_today=2, day_pnl=-11.37, traded_symbols=["HDFCBANK", "AXISBANK"],
                     cash=10118.14, day_start_equity=10129.51)
    b.state.save()


def run_one_round(monkeypatch, b, now):
    monkeypatch.setattr(bot, "now_ist", lambda: now)
    cycles = []
    monkeypatch.setattr(b, "cycle", lambda: cycles.append(1))
    monkeypatch.setattr(b, "nap", lambda s: open(C.STOP_FILE, "w").close())   # stop after one round
    assert b.run() is None
    return cycles


def test_counters_reset_before_the_open(monkeypatch):
    b = bot.Bot()
    b.build = {"version": "test"}
    yesterday_state(b)
    cycles = run_one_round(monkeypatch, b, WED_0900)
    assert cycles == []                                   # no trading round before 09:15
    d = b.state.d
    assert d["day"] == "2026-11-18" and d["trades_today"] == 0 and d["day_pnl"] == 0.0
    assert d["traded_symbols"] == []
    assert d["cash"] == 10118.14                           # money is untouched
    assert d["day_start_equity"] == 10118.14               # today's loss limit is based on today's start
    saved = json.load(open(os.path.join(C.DATA_DIR, f"state_{b.mode}.json"), encoding="utf-8"))
    assert saved["day"] == "2026-11-18" and saved["trades_today"] == 0


def test_status_shows_today_before_the_open(monkeypatch):
    b = bot.Bot()
    b.build = {"version": "test"}
    yesterday_state(b)
    run_one_round(monkeypatch, b, WED_0900)
    st = json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))
    assert st["trades_today"] == 0


def test_weekend_keeps_the_last_trading_day(monkeypatch):
    b = bot.Bot()
    b.build = {"version": "test"}
    yesterday_state(b)
    run_one_round(monkeypatch, b, dt.datetime(2026, 11, 21, 9, 0, tzinfo=IST))   # Saturday: the bot rests
    assert b.state.d["day"] == "2026-11-17" and b.state.d["trades_today"] == 2
