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


def test_outage_logged_once_and_recovery_logged(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    bars, day = quiet_day()
    feed, clock = [bars], [dt.datetime.combine(day, dt.time(9, 45, 30), tzinfo=IST)]
    setup(monkeypatch, feed, clock)
    b = bot.Bot()
    b.cycle()
    assert not [r for r in caplog.records if "price data" in r.getMessage()]

    feed[0] = DNS_ERROR
    for hhmm in ((9, 46), (9, 47), (9, 48)):          # three failed rounds -> one warning
        clock[0] = dt.datetime.combine(day, dt.time(*hhmm), tzinfo=IST)
        b.cycle()
    warnings = [r for r in caplog.records if "price data problem" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "1 of 1 stocks failed (can't look up the data server's address" in warnings[0].getMessage()

    feed[0] = bars
    clock[0] = dt.datetime.combine(day, dt.time(9, 52), tzinfo=IST)
    b.cycle()
    b.cycle()                                         # a second good round logs nothing more
    back = [r.getMessage() for r in caplog.records if "price data back" in r.getMessage()]
    assert back == ["price data back: all 1 stocks OK (problem from 09:46, about 6 min)"]
    assert any("price data back" in e for e in b.events)   # also shown on the dashboard


def test_data_problem_plain_words():
    assert "DNS" in bot.data_problem(DNS_ERROR)
    assert bot.data_problem(Exception("ConnectTimeoutError(... 'Connection to x timed out')")) == \
        "connection to the data server timed out"
    assert "429" in bot.data_problem(Exception("rate limited (429)"))
    assert bot.data_problem(Exception("something else")) == "something else"
