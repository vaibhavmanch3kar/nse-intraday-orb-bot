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


# ---------------------------------------------------------------- 2. account value vs available funds
MARGINS = {"net": 10870.6, "available": {"opening_balance": 4870.6, "live_balance": 10870.6, "intraday_payin": 6000,
                                         "collateral": 0, "cash": 0}, "utilised": {"debits": 0}}


def test_money_added_today_is_explained():
    import accounts
    a = accounts.summarise_zerodha(MARGINS, {"net": []})
    assert (a["equity"], a["available"], a["payin"], a["sizing"]) == (4870.6, 10870.6, 6000.0, 4870.6)
    assert "includes ₹6,000.00 added today" in web.funds_sub(a)
    note = web.funds_note(a)
    assert "₹6,000.00 added today" in note and "sizes real-money trades from <b>₹4,870.60</b>" in note
    assert "counts from tomorrow" in note


def test_no_note_about_added_money_on_an_ordinary_day():
    import accounts
    m = {"net": 5000.0, "available": {"opening_balance": 5000.0, "live_balance": 5000.0}, "utilised": {}}
    a = accounts.summarise_zerodha(m, {"net": []})
    assert a["payin"] == 0 and "added today" not in web.funds_sub(a) + web.funds_note(a)
    assert "₹5,000.00" in web.funds_note(a)
    low = accounts.summarise_zerodha({"net": 3000.0, "available": {"opening_balance": 5000.0, "live_balance": 3000.0}},
                                     {"net": []})
    assert low["sizing"] == 3000.0                                # money taken out counts at once


# ---------------------------------------------------------------- 3. the view is not the trading mode
def running(mode):
    return {"time": dt.datetime.now(IST).isoformat(), "mode": mode}


def test_view_that_differs_from_the_trading_mode_says_so():
    live_paper = web.view_banner("paper", running("live"))
    assert "PAPER ACCOUNT" in live_paper and "the bot is trading REAL MONEY on Zerodha right now" in live_paper
    assert "?view=zerodha" in live_paper and "never changes the trading mode" in live_paper
    paper_zerodha = web.view_banner("zerodha", running("paper"))
    assert "the bot is trading PAPER" in paper_zerodha and "?view=paper" in paper_zerodha


def test_matching_view_or_a_stopped_bot_has_no_extra_line():
    assert "right now" not in web.view_banner("zerodha", running("live"))
    assert "right now" not in web.view_banner("paper", running("paper"))
    old = {"time": "2026-11-17T15:30:00+05:30", "mode": "live"}
    assert "right now" not in web.view_banner("paper", old) and "right now" not in web.view_banner("paper")


def test_the_default_view_follows_the_bot_and_a_view_never_sets_the_mode():
    assert web.current_view({}, {"mode": "live"}) == "zerodha" and web.current_view({}, {"mode": "paper"}) == "paper"
    assert web.current_view({"view": ["paper"]}, {"mode": "live"}) == "paper"
    before = (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live())
    web.view_banner("zerodha", running("paper")); web.view_banner("paper", running("live"))
    assert (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live()) == before
