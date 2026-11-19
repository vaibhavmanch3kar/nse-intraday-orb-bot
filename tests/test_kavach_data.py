"""Kavach's data rules (2.0): real money never trades on delayed (Yahoo) prices; a stock whose candles failed
validation is not entered; a stale signal is skipped; with the index filter on, missing index data stops the
entry. Plus candle validation itself, and the feed-health record the bot writes."""

import datetime as dt
import json
import os

import pytest

import bot
import config as C
import datacheck
import kavach
import risk
from conftest import IST, bar, history

DAY = dt.date(2026, 11, 18)


def t(hhmm, day=DAY):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), IST)


def day_bars(times=("09:15", "09:30", "09:45", "10:00"), day=DAY):
    return [bar(day, x, 100, 100.5, 99.5, 100.2) for x in times]


# ---------------------------------------------------------------- candle validation
def test_a_complete_morning_has_no_problems():
    assert datacheck.problems(day_bars(), DAY, 15) == []
    assert datacheck.problems([], DAY, 15) == []
    assert datacheck.problems(day_bars(day=dt.date(2026, 11, 17)), DAY, 15) == []


@pytest.mark.parametrize("bars, text", [
    (day_bars(("09:30", "09:45")), "the 09:15 opening candle is missing (the first candle is 09:30)"),
    (day_bars(("09:15", "09:30", "10:00")), "candles missing between 09:30 and 10:00"),
    (day_bars(("09:15", "09:30", "09:30")), "candles repeated or out of order at 09:30"),
    (day_bars(("09:15", "09:30", "09:40")), "a candle at 09:40 is off the 15-minute grid"),
    ([bar(DAY, "09:15", 100, 99, 99.5, 100.2)], "impossible candle at 09:15"),          # high below the open
    ([bar(DAY, "09:15", 100, 101, 0, 100.2)], "impossible candle at 09:15"),            # zero low
    ([bar(DAY, "09:15", 100, 101, 99, 100.2, v=-5)], "negative volume at 09:15"),
])
def test_each_kind_of_bad_data_is_named(bars, text):
    assert text in " | ".join(datacheck.problems(bars, DAY, 15))


def test_has_bar():
    assert datacheck.has_bar(day_bars(), t("09:45")) and not datacheck.has_bar(day_bars(), t("10:15"))
    assert not datacheck.has_bar(None, t("09:45"))


# ---------------------------------------------------------------- Kavach's data check
@pytest.fixture
def st(tmp_path):
    s = risk.State(str(tmp_path / "state.json"), 100000.0)
    s.new_day_if_needed(DAY)
    return s


def ask(st, data):
    return kavach.pre_trade(st, "SBIN", "LONG", 1000.0, 990.0, DAY, data=data)


def test_real_money_never_trades_on_yahoo_prices(st):
    d = ask(st, {"live": True, "source": "yahoo"})
    assert not d.ok and d.check == "data"
    assert "real-money entries need Kite prices" in d.reason and "yahoo" in d.reason
    assert not ask(st, {"live": True, "source": None}).ok
    assert ask(st, {"live": True, "source": "kite"}).ok
    assert ask(st, {"live": False, "source": "yahoo"}).ok        # paper may practise on Yahoo
    assert ask(st, None).ok                                      # a backtest has no feed to check


def test_bad_candles_stale_signals_and_missing_index_stop_the_entry(st, monkeypatch):
    ok = {"live": True, "source": "kite"}
    d = ask(st, dict(ok, problems=["candles missing between 09:30 and 10:00"]))
    assert d.check == "data" and "price data problem: candles missing between 09:30 and 10:00" in d.reason
    monkeypatch.setattr(C, "MAX_SIGNAL_AGE_MIN", 5.0)
    assert ask(st, dict(ok, signal_age_min=4.9)).ok
    d = ask(st, dict(ok, signal_age_min=21.0))
    assert d.check == "data" and "closed 21 min ago" in d.reason and "stale" in d.reason
    monkeypatch.setattr(C, "MAX_SIGNAL_AGE_MIN", 0.0)
    assert ask(st, dict(ok, signal_age_min=500.0)).ok            # 0 switches the age limit off
    monkeypatch.setattr(C, "INDEX_FILTER", True)
    assert "index data is missing" in ask(st, dict(ok, index_missing=True)).reason
    monkeypatch.setattr(C, "INDEX_FILTER", False)
    assert ask(st, dict(ok, index_missing=True)).ok              # no index filter, nothing to check


def test_data_is_checked_before_the_account_and_the_size(st):
    st.d["trades_today"] = C.MAX_TRADES_PER_DAY
    d = ask(st, {"live": True, "source": "yahoo"})
    assert d.check == "data" and [c[0] for c in d.checks] == ["time", "paused", "balance", "data"]


# ---------------------------------------------------------------- in the running bot
def breakout_session(day):
    """Quiet history, then today: opening range 99.6-100.4 and a high-volume close above it at 09:30."""
    hist, _ = history(5, start=dt.date(2026, 11, 9))
    return hist + [bar(day, "09:15", 100, 100.4, 99.6, 100.0), bar(day, "09:30", 100, 101.0, 99.9, 100.9, v=5000)]


def make_bot(monkeypatch, now, source, live=False, bars=None):
    import data_yahoo
    import marketdata
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(bot, "now_ist", lambda: now)
    bars = bars or breakout_session(now.date())
    monkeypatch.setattr(marketdata, "fetch_many", lambda syms, *a, **k: {s: (bars, 100.9, now) for s in syms})
    monkeypatch.setattr(data_yahoo, "closed_bars",
                        lambda b, n, i: [x for x in b if x["t"] + dt.timedelta(minutes=i) <= n])
    b = bot.Bot()
    b.live = live
    monkeypatch.setattr(bot.Bot, "price_source", lambda self: source)
    return b


def test_paper_bot_enters_and_records_feed_health(monkeypatch):
    b = make_bot(monkeypatch, t("09:46"), "kite")
    b.cycle()
    assert "SBIN" in b.state.positions
    f = b.feed
    assert f["source"] == "kite" and f["newest_candle"] == "09:30" and f["pickup_sec"] == 60.0
    assert f["stocks_ok"] == 1 and f["problems"] == {} and f["errors"] == [] and f["live_needs_kite"] is False
    saved = json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))
    assert saved["feed"]["newest_candle"] == "09:30"


def test_real_money_bot_skips_the_entry_on_yahoo_prices(monkeypatch):
    b = make_bot(monkeypatch, t("09:46"), "yahoo", live=True)
    b.balance_ok = True
    b.cycle()
    assert b.state.positions == {} and b.feed["live_needs_kite"] is True
    assert "skip: real-money entries need Kite prices" in b.rows["SBIN"]["reason"]


def test_a_stale_signal_is_skipped_in_the_bot(monkeypatch):
    monkeypatch.setattr(C, "MAX_SIGNAL_AGE_MIN", 5.0)
    b = make_bot(monkeypatch, t("09:52"), "kite")               # the 09:30 candle closed 7 minutes ago
    b.cycle()
    assert b.state.positions == {} and "stale data" in b.rows["SBIN"]["reason"]


def test_bad_candles_block_the_stock_and_are_logged_once(monkeypatch):
    hist, _ = history(5, start=dt.date(2026, 11, 9))
    gap = hist + [bar(DAY, "09:15", 100, 100.4, 99.6, 100.0), bar(DAY, "09:45", 100, 101.0, 99.9, 100.9, v=5000)]
    b = make_bot(monkeypatch, t("10:01"), "kite", bars=gap)
    b.cycle()
    b.cycle()
    assert b.state.positions == {}
    assert b.feed["problems"] == {"SBIN": "candles missing between 09:15 and 09:45"} and b.feed["stocks_ok"] == 0
    assert sum("price data problem: candles missing" in e for e in b.events) == 1
