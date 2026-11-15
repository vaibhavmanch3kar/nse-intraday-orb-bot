"""Features adapted from the Day Trader skill video: NSE holidays, Nifty 50 fetching,
Time Machine replays, daily swing scanner."""

import datetime as dt

import numpy as np

import bot
import config as C
import data_yahoo
import holidays
import scanner
import timemachine
import web
from conftest import IST


# ---------------------------------------------------------------- holidays
def test_nse_holidays_2026():
    assert holidays.holiday_name(dt.date(2026, 11, 24)) == "Prakash Gurpurb Sri Guru Nanak Dev"
    assert not holidays.is_trading_day(dt.date(2026, 12, 25))
    assert not holidays.is_trading_day(dt.date(2026, 11, 21))          # Saturday
    assert holidays.is_trading_day(dt.date(2026, 11, 16))
    assert holidays.next_trading_day(dt.date(2026, 11, 23)) == dt.date(2026, 11, 25)
    assert holidays.next_trading_day(dt.date(2026, 12, 24)) == dt.date(2026, 12, 28)
    assert holidays.year_listed(2026) and not holidays.year_listed(2031)
    assert holidays.upcoming(dt.date(2026, 11, 14), 2)[0][0] == dt.date(2026, 11, 24)


def test_bot_exits_on_holiday(monkeypatch):
    monkeypatch.setattr(bot, "now_ist", lambda: dt.datetime(2026, 11, 24, 10, 0, tzinfo=IST))
    b = bot.Bot()
    b.run()  # returns instead of looping
    assert any("NSE holiday" in e for e in b.events)


def test_unlisted_holiday_fallback(monkeypatch):
    monkeypatch.setattr(holidays, "_load", lambda: {})
    day = dt.date(2031, 1, 6)
    old_bar = {"t": dt.datetime(2031, 1, 3, 15, 15, tzinfo=IST), "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: ([old_bar], 1.0, None))
    b = bot.Bot()
    assert not b.unlisted_holiday(dt.datetime.combine(day, dt.time(9, 30), tzinfo=IST))  # too early to tell
    assert b.unlisted_holiday(dt.datetime.combine(day, dt.time(9, 50), tzinfo=IST))
    b.holiday_checked = None
    today_bar = {**old_bar, "t": dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)}
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: ([today_bar], 1.0, None))
    assert not b.unlisted_holiday(dt.datetime.combine(day, dt.time(9, 50), tzinfo=IST))


# ---------------------------------------------------------------- data
def test_symbols_and_fetch_many(monkeypatch):
    assert data_yahoo.yahoo_symbol("M&M") == "M&M.NS" and data_yahoo.yahoo_symbol("^NSEI") == "^NSEI"
    assert len(C.NIFTY50) == 50 and len(set(C.NIFTY50)) == 50

    def fake(sym, *a, **k):
        if sym == "BAD":
            raise data_yahoo.DataError("nope")
        return ([], 1.0, None)
    monkeypatch.setattr(data_yahoo, "fetch", fake)
    got = data_yahoo.fetch_many(["SBIN", "BAD"], workers=2)
    assert got["SBIN"] == ([], 1.0, None) and isinstance(got["BAD"], data_yahoo.DataError)


# ---------------------------------------------------------------- time machine
def synthetic_moves(sd=0.002, drift=0.0, seed=1):
    rng = np.random.default_rng(seed)
    moves, t = {}, dt.datetime(2026, 1, 1, 9, 20)
    while t.strftime("%H:%M") < "15:30":
        c = rng.normal(0, sd, 400)
        c = c - c.mean() + drift  # exact drift, no sampling noise
        moves[t.strftime("%H:%M")] = np.c_[np.maximum(c, 0), np.minimum(c, 0), c]
        t += dt.timedelta(minutes=5)
    return moves


def test_time_machine_symmetric_without_drift():
    m = synthetic_moves()
    long = timemachine.replay(m, "LONG", 100, 99, "09:45", "15:10", n=20000)
    short = timemachine.replay(m, "SHORT", 100, 101, "09:45", "15:10", n=20000)
    l1, s1 = long["targets"][1.0], short["targets"][1.0]
    assert abs(l1["hit_first"] - l1["stop_first"]) < 0.05       # random walk: 1R vs 1R ~ 50/50
    assert abs(l1["hit_first"] - s1["hit_first"]) < 0.05        # long/short mirror
    assert long["targets"][2.0]["hit_first"] < l1["hit_first"]  # 2R is harder than 1R
    for v in long["targets"].values():
        assert abs(v["hit_first"] + v["stop_first"] + v["neither"] - 1) < 1e-9
    assert long["steps"] == (5 * 60 + 25) // 5                  # 09:45 -> 15:10 in 5-min steps


def test_time_machine_drift_and_wide_stop():
    up = synthetic_moves(drift=0.0005)
    r = timemachine.replay(up, "LONG", 100, 99, "09:45", "15:10", n=5000)
    assert r["targets"][1.0]["hit_first"] > r["targets"][1.0]["stop_first"] and r["drift_pct"] > 0
    wide = timemachine.replay(synthetic_moves(), "LONG", 100, 80, "09:45", "15:10", n=5000)
    assert wide["targets"][1.0]["neither"] > 0.95                # a 20% stop is rarely touched in a day
    assert timemachine.replay({}, "LONG", 100, 99, "09:45", "15:10")["n"] == 0
    assert "1R first" in timemachine.summary(r)


def test_slot_moves_skip_overnight():
    d1, d2 = dt.date(2026, 11, 12), dt.date(2026, 11, 13)
    mk = lambda d, hm, c: {"t": dt.datetime.combine(d, dt.time(*hm), tzinfo=IST), "o": c, "h": c, "l": c, "c": c}  # noqa: E731
    bars = [mk(d1, (15, 25), 100)] + [mk(d2, (9, 15 + 5 * k), 110 + k) for k in range(6)]
    m = timemachine.slot_moves(bars * 1)
    assert "09:15" not in m  # first bar of the day follows an overnight gap -> excluded


# ---------------------------------------------------------------- scanner
def daily(closes, start=dt.date(2026, 6, 1), hi_pad=0.5, lo_pad=0.5):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        out.append({"t": dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST), "o": c, "h": c + hi_pad,
                    "l": c - lo_pad, "c": c, "v": 1})
        d += dt.timedelta(days=1)
    return out


def test_scanner_breakout_and_reclaim():
    flat = [100.0] * 70
    r = scanner.scan_symbol(daily(flat + [103.0]))
    assert "20-day high breakout" in r["setups"] and "20-day MA reclaim" not in r["setups"]
    dip = flat[:-5] + [99, 98.5, 98, 97.5, 97.5, 100.5]
    r = scanner.scan_symbol(daily(dip))
    assert "20-day MA reclaim" in r["setups"]
    assert r["stop"] < r["close"] < r["target"] and r["qty"] > 0


def test_scanner_oversold_and_bounce():
    falling = [100 - 1.5 * k for k in range(70)]
    r = scanner.scan_symbol(daily(falling))
    assert "Oversold" in r["setups"] and r["rsi"] < 30
    bounce = falling + [falling[-1] + 3]
    r = scanner.scan_symbol(daily(bounce))
    assert "Oversold bounce" in r["setups"]


def test_scanner_bull_flag():
    base = [100.0] * 50
    pole = [100 + 2.5 * k for k in range(1, 7)]           # +15% in 6 sessions
    flag = [114.0, 113.5, 113.8, 114.2]                   # tight, shallow, upper half
    r = scanner.scan_symbol(daily(base + pole + flag, hi_pad=0.3, lo_pad=0.3))
    assert "Bull flag" in r["setups"] and r["pole_pct"] > 8
    deep = flag[:2] + [104.0, 105.0]                      # pulled back more than half the pole
    assert "Bull flag" not in scanner.scan_symbol(daily(base + pole + deep))["setups"]


def test_scanner_drops_forming_daily_bar(monkeypatch):
    now = dt.datetime(2026, 11, 16, 11, 0, tzinfo=IST)
    start = now.date()
    for _ in range(69):  # 70 weekdays ending on Monday 16 Nov
        start -= dt.timedelta(days=1)
        while start.weekday() >= 5:
            start -= dt.timedelta(days=1)
    bars = daily([100.0] * 69 + [103.0], start=start)
    assert bars[-1]["t"].date() == now.date()
    monkeypatch.setattr(data_yahoo, "fetch_many", lambda *a, **k: {"SBIN": (bars, 103.0, None)})
    r = scanner.scan(["SBIN"], now=now)
    assert r["rows"]["SBIN"]["date"] == "2026-11-13"     # Monday's forming candle ignored
    assert "20-day high breakout" not in r["rows"]["SBIN"]["setups"]


def test_scanner_and_calendar_pages(monkeypatch):
    fake = {"day": "2026-11-13", "built": "2026-11-14T21:00:00+05:30", "errors": {},
            "counts": {k: 0 for k in scanner.SETUPS},
            "rows": {"SBIN": {**scanner.scan_symbol(daily([100.0] * 70 + [103.0])), "setups": ["20-day high breakout"]}}}
    fake["counts"]["20-day high breakout"] = 1
    monkeypatch.setattr(scanner, "latest", lambda *a, **k: fake)
    page = web.render_scanner()
    assert "Swing scanner" in page and "SBIN" in page and "Research only" in page
    assert "NSE calendar" in web.market_calendar()
