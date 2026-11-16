"""Price-data outages in the bot loop: the dashboard rows recover and the outage is logged."""

import datetime as dt

import bot
import config as C
import data_yahoo
from conftest import IST, bar, history


def setup(monkeypatch, feed, clock):
    """feed[0] is either a list of bars (data OK) or an exception to raise (outage)."""
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)

    def fake_fetch(sym, interval_min, range_, **kw):
        if isinstance(feed[0], Exception):
            raise data_yahoo.DataError(f"{sym}: {feed[0]}")
        return feed[0], feed[0][-1]["c"], None

    monkeypatch.setattr(data_yahoo, "fetch", fake_fetch)
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])


DNS_ERROR = Exception("HTTPSConnectionPool(host='query1.finance.yahoo.com', port=443): Max retries exceeded "
                      "(Caused by NameResolutionError(\"Failed to resolve 'query1.finance.yahoo.com'\"))")


def quiet_day():
    hist, day = history()
    return hist + [bar(day, "09:15", 100.0, 100.5, 99.5, 100.1, 1000),
                   bar(day, "09:30", 100.1, 100.3, 99.9, 100.0, 1000)], day


def test_error_row_clears_when_data_returns_before_any_candle(monkeypatch):
    """Outage before today's first candle closed: once data is back the row says so (not the old error)."""
    hist, day = history()
    feed, clock = [DNS_ERROR], [dt.datetime.combine(day, dt.time(9, 21), tzinfo=IST)]
    setup(monkeypatch, feed, clock)
    b = bot.Bot()
    b.cycle()
    assert b.rows["SBIN"]["reason"].startswith("data error")

    feed[0] = hist                                    # data back, still no closed candle of today
    clock[0] = dt.datetime.combine(day, dt.time(9, 25), tzinfo=IST)
    b.cycle()
    assert b.rows["SBIN"]["reason"] == "data OK again at 09:25 - waiting for the next candle"


def test_error_row_restores_last_evaluation(monkeypatch):
    """Outage after a candle was evaluated: the last evaluation comes back, not the error."""
    bars, day = quiet_day()
    feed, clock = [bars], [dt.datetime.combine(day, dt.time(9, 45, 30), tzinfo=IST)]
    setup(monkeypatch, feed, clock)
    b = bot.Bot()
    b.cycle()
    good = dict(b.rows["SBIN"])
    assert good["bar"] == "09:30" and not good["reason"].startswith("data error")

    feed[0] = DNS_ERROR
    clock[0] = dt.datetime.combine(day, dt.time(9, 50), tzinfo=IST)
    b.cycle()
    assert b.rows["SBIN"]["reason"].startswith("data error")

    feed[0] = bars                                    # back, no new candle yet
    clock[0] = dt.datetime.combine(day, dt.time(9, 55), tzinfo=IST)
    b.cycle()
    assert b.rows["SBIN"] == good


def test_new_candle_after_outage_is_evaluated(monkeypatch):
    bars, day = quiet_day()
    feed, clock = [DNS_ERROR], [dt.datetime.combine(day, dt.time(9, 45, 30), tzinfo=IST)]
    setup(monkeypatch, feed, clock)
    b = bot.Bot()
    b.cycle()
    feed[0] = bars + [bar(day, "09:45", 100.0, 100.2, 99.8, 100.1, 1000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)
    b.cycle()
    assert b.rows["SBIN"]["bar"] == "09:45"
