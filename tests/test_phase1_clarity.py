"""Ayodhya ORB 2.0, Phase 1: the dashboard says what its numbers mean. A candle's age is not a "delay"; money
added today is explained; the account in view is never mistaken for the account the bot trades; plans are sized
from the real capital, not a made-up Rs 1,00,000; and a day without trades says why."""

import datetime as dt

import config as C
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def t(hhmm, day=dt.date(2026, 11, 18)):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), IST)


def status(at, last_bar, lag, mode="live", using="kite", **extra):
    return {"time": at.isoformat(), "mode": mode, "data_lag_min": lag, "data_source": {"using": using},
            "rows": {"SBIN": {"bar": last_bar, "close": 1110.8, "reason": "inside range"}}, **extra}


# ---------------------------------------------------------------- 1. a candle's age is not a delay
def test_normal_candle_age_is_not_called_a_delay(monkeypatch):
    s = status(t("09:59"), "09:30", 14)
    monkeypatch.setattr(web, "watch_timing", lambda st, now=None: {"state": "waiting", "last": "09:30", "key": "", "text": ""})
    chips = web._status_chips(s, True, "zerodha", True, 14)
    assert "min behind" not in chips and "Kite data · last candle 09:30" in chips and "chip ok" in chips
    assert "not a delay" in chips                                   # the tooltip explains the number
    text = web.candle_age_text(s)
    assert "finished 14 min ago" in text and "on time" in text and "LATE" not in text


def test_a_really_late_candle_is_flagged(monkeypatch):
    s = status(t("10:10"), "09:30", 25)
    monkeypatch.setattr(web, "watch_timing", lambda st, now=None: {"state": "late", "last": "09:30", "key": "k", "text": ""})
    chips = web._status_chips(s, True, "zerodha", True, 25)
    assert "candle LATE" in chips and "chip amber" in chips
    assert "LATE" in web.candle_age_text(s)
    assert "Newest candle" in web.live_limits(s) and "Data delay" not in web.live_limits(s)
