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


# ---------------------------------------------------------------- 4. plans use the real capital
def test_sizing_capital_is_the_account_the_bot_trades(monkeypatch):
    import capital
    monkeypatch.setattr(capital, "current_opening_balance", lambda *a: 10000.0)
    assert web.sizing_capital({"mode": "paper"}) == (10000.0, "the paper opening balance")
    cap, label = web.sizing_capital({"mode": "live", "balance": {"inr": 4870.6}})
    assert cap == 4870.6 and "Zerodha balance" in label
    assert web.sizing_capital({"mode": "live", "balance": {"inr": None}})[0] == 10000.0   # not read yet: paper figure


def test_plan_quantity_follows_the_capital():
    # close 300, stop 291: Rs 9 risk a share
    assert web.plan_qty(300, 291, 100000) == 55          # Rs 500 risk -> 55 shares (the old fixed assumption)
    assert web.plan_qty(300, 291, 4870.6) == 2           # Rs 24.35 risk -> 2 shares
    assert web.plan_qty(1365, 1340, 4870.6) == 0         # 1 share costs more than 25% of the balance
    assert web.plan_qty(300, 300, 4870.6) == 0 and web.plan_qty(None, 291, 4870.6) == 0


def test_scanner_page_shows_the_real_risk_not_500(monkeypatch):
    import scanner
    row = {"setups": ["20-day high breakout"], "close": 300.0, "change_pct": 1.0, "rsi": 60.0, "stop": 291.0,
           "target": 318.0, "qty": 55, "above_200dma": True, "from_52w_high_pct": -2.0, "closes": [290, 295, 300]}
    big = dict(row, close=1365.0, stop=1340.0, target=1415.0)
    monkeypatch.setattr(scanner, "latest", lambda: {"day": "2026-11-17", "built": "2026-11-18T10:00:14+05:30",
                                                    "rows": {"ITC": row, "RELIANCE": big}, "counts": {}, "errors": {}})
    monkeypatch.setattr(scanner, "status", lambda c, now=None: {"stale": False})
    monkeypatch.setattr(web, "sizing_capital", lambda s=None: (4870.6, "your Zerodha balance, as the bot read it"))
    page = web.render_scanner()
    assert "Plan (₹24 risk)" in page and "₹500 risk" not in page and "qty 2" in page and "qty 55" not in page
    assert "qty 0: 1 share doesn&#x27;t fit the limits" in page or "qty 0: 1 share doesn't fit the limits" in page
    assert "0.5% of ₹4,870.60" in page and "your Zerodha balance" in page
