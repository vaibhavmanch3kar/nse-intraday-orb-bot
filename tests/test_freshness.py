"""Saved Review and Scan results follow the market data: the scan once a day after the daily candle is final,
past Reviews kept for good, today's Review "in progress" and refreshed after each 15-minute candle."""

import datetime as dt

import pytest

import config as C
import data_yahoo
import freshness
import jobs
import review
import scanner
import web
from conftest import IST, bar, history
from test_review import day_with_breakout

WED, THU, SAT = dt.date(2026, 11, 18), dt.date(2026, 11, 19), dt.date(2026, 11, 21)


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def test_candle_and_settle_times():
    assert freshness.last_candle_close(at(WED, "09:20")) is None          # first candle not closed yet
    assert freshness.last_candle_close(at(WED, "09:31")) is None          # closed, data not expected yet
    assert freshness.last_candle_close(at(WED, "09:33")) == at(WED, "09:30")
    assert freshness.last_candle_close(at(WED, "16:00")) == at(WED, "15:30")
    assert freshness.last_candle_close(at(SAT, "11:00")) is None
    assert freshness.settled_since(at(WED, "20:00"), at(THU, "15:44")) is None
    assert freshness.settled_since(at(WED, "20:00"), at(THU, "15:46")) == THU
    assert freshness.in_session(WED, at(WED, "15:44")) and not freshness.in_session(WED, at(WED, "15:46"))


def saved_scan(built, **extra):
    return {"day": "x", "built": built.isoformat(), "rows": {}, "errors": {},
            "settings": {k: getattr(C, k) for k in scanner.SETTING_KEYS}, **extra}


def test_scan_stays_valid_until_the_next_daily_candle_is_final(monkeypatch):
    c = saved_scan(at(WED, "20:00"))
    assert not scanner.is_stale(c, at(THU, "10:00"))                     # no more 30-minute rescans
    assert not scanner.is_stale(c, at(THU, "15:44"))
    st = scanner.status(c, at(THU, "15:46"))
    assert st["stale"] and "daily candle is now final" in st["why"]
    morning = saved_scan(at(WED, "09:29"))                                 # built during the day
    assert not scanner.is_stale(morning, at(WED, "15:00")) and scanner.is_stale(morning, at(WED, "15:50"))
    friday = saved_scan(at(dt.date(2026, 11, 20), "20:00"))
    assert not scanner.is_stale(friday, at(SAT, "12:00"))                  # weekend: nothing new
    assert scanner.status(None)["stale"]
    monkeypatch.setattr(C, "RISK_PER_TRADE_PCT", 1.0)
    st = scanner.status(c, at(THU, "10:00"))
    assert st["stale"] and "RISK_PER_TRADE_PCT" in st["why"]


def test_partial_scan_is_retried_after_ten_minutes():
    c = saved_scan(at(THU, "10:00"), partial={"skipped": ["SBIN"]})
    assert not scanner.is_stale(c, at(THU, "10:05")) and scanner.is_stale(c, at(THU, "10:11"))


def saved_review(day, built, **extra):
    return {"day": day.isoformat(), "built": built.isoformat(), "rows": {},
            "settings": {k: getattr(C, k) for k in review.SETTING_KEYS}, **extra}


def test_past_review_is_final_and_kept():
    c = saved_review(WED, at(WED, "18:00"))
    for now in (at(THU, "10:00"), at(SAT, "10:00"), at(WED, "23:00")):
        st = review.status(c, now)
        assert not st["stale"] and not st["in_progress"], now


def test_todays_review_is_in_progress_and_refreshed_after_each_candle():
    c = saved_review(THU, at(THU, "10:05"), in_progress=True, as_of="10:00")
    assert review.status(c, at(THU, "10:10"))["in_progress"]
    assert not review.status(c, at(THU, "10:16"))["stale"]                 # 10:15 candle: data not due yet
    st = review.status(c, at(THU, "10:17"))
    assert st["stale"] and "10:15 candle" in st["why"]
    st = review.status(saved_review(THU, at(THU, "15:40"), in_progress=True), at(THU, "15:46"))
    assert st["stale"] and "session has ended" in st["why"]


def test_v180_partial_day_cache_is_no_longer_final_forever():
    """v1.8.0 saved today's half-built session without any mark; it must still be refreshed."""
    legacy = saved_review(THU, at(THU, "11:02"))
    st = review.status(legacy, at(THU, "11:20"))
    assert st["in_progress"] and st["stale"]
    assert review.status(legacy, at(THU, "18:00"))["stale"]                # and rebuilt as final after close


def test_latest_review_moves_to_a_new_session_once_per_candle():
    c = saved_review(WED, at(WED, "18:00"))
    assert not review.status(c, at(THU, "09:20"), latest=True)["stale"]   # no candle closed yet today
    st = review.status(c, at(THU, "09:40"), latest=True)
    assert st["stale"] and "newer session" in st["why"]
    assert not review.status(c, at(THU, "09:40"))["stale"]                # a day link: Wednesday is final
    rebuilt_no_new_data = saved_review(WED, at(THU, "09:41"))              # data source still behind
    assert not review.status(rebuilt_no_new_data, at(THU, "09:44"), latest=True)["stale"]   # no loop
    assert review.status(rebuilt_no_new_data, at(THU, "09:48"), latest=True)["stale"]


def test_settings_change_makes_a_review_stale(monkeypatch):
    c = saved_review(WED, at(WED, "18:00"))
    monkeypatch.setattr(C, "VOLUME_MULT", 2.0)
    st = review.status(c, at(THU, "10:00"))
    assert st["stale"] and "VOLUME_MULT" in st["why"]


class _Clock(dt.datetime):
    NOW = None

    @classmethod
    def now(cls, tz=None):
        return cls.NOW.astimezone(tz) if tz else cls.NOW.replace(tzinfo=None)


def test_build_marks_todays_session_in_progress(monkeypatch):
    hist, _ = history(start=dt.date(2026, 11, 9))
    day = THU
    bars = [b for b in hist if b["t"].date() < day] + day_with_breakout(day, [])
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars, bars[-1]["c"], None))
    _Clock.NOW = at(day, "10:20")
    monkeypatch.setattr(dt, "datetime", _Clock)
    r = review.build(day.isoformat(), refresh=True)
    assert r["day"] == day.isoformat() and r["in_progress"] is True and r["as_of"] == "10:15"
    _Clock.NOW = at(day, "18:00")
    r = review.build(day.isoformat(), refresh=True)
    assert "in_progress" not in r and "as_of" not in r


def test_day_link_to_a_stale_in_progress_review_starts_a_rebuild(monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "start", lambda name, target=None: started.append((name, target)))
    # a fixed moment in the session: with the real clock, before 09:33 India time no candle has closed yet,
    # so a review built 30 hours earlier was not out of date and the test failed every night
    now = _Clock.NOW = at(THU, "11:02")
    monkeypatch.setattr(dt, "datetime", _Clock)
    d = now.date()
    review.write_json(review._cache_path(d.isoformat()),
                      saved_review(d, now - dt.timedelta(hours=30), in_progress=True, as_of="10:00",
                                   days=[d.isoformat()]))
    page = web.render_review(d.isoformat())
    assert ("review", d.isoformat()) in started and "Session in progress" in page


def test_scan_saved_by_v180_is_refreshed_once_with_a_plain_reason():
    c = {"day": "x", "built": at(WED, "09:29").isoformat(), "rows": {}, "errors": {}}
    st = scanner.status(c, at(WED, "10:00"))
    assert st["stale"] and st["why"] == "saved by an older version of the dashboard"
