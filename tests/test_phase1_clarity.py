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


# ---------------------------------------------------------------- 7. a day without trades says why
def test_skip_reason_names_the_limit_that_stopped_the_trade():
    import risk
    # Rs 4,870.60: risk budget Rs 24.35, per-stock limit Rs 1,217.65
    qty, why = risk.size_position(4870.6, 1361.0, 1373.0, "SHORT")
    assert qty == 0 and "1 share costs ₹1,361.00, more than the 25% per-stock limit (₹1,218 of ₹4,871)" in why
    qty, why = risk.size_position(4870.6, 300.0, 250.0, "LONG")
    assert qty == 0 and "1 share would risk ₹50.00, more than the ₹24.35 risk budget (0.5% of ₹4,871)" in why
    qty, why = risk.size_position(4870.6, 300.0, 291.0, "LONG", open_notional=3600.0)
    assert qty == 0 and "total exposure limit" in why
    qty, why = risk.size_position(4870.6, 300.0, 291.0, "LONG")
    assert qty == 2 and why == "ok"                              # quantities themselves are unchanged


def test_position_sizes_are_unchanged_by_the_clearer_reasons():
    import random
    import risk
    import costs

    def old(equity, entry, stop, direction, open_notional=0.0, risk_mult=1.0):
        per = abs(entry - stop)
        if per <= 0:
            return 0
        budget = equity * C.RISK_PER_TRADE_PCT / 100 * risk_mult
        qty = int(budget // per)
        qty = min(qty, int(equity * C.MAX_POSITION_PCT / 100 // entry),
                  int(max(0.0, equity * C.MAX_EXPOSURE_PCT / 100 - open_notional) // entry))
        while qty >= C.MIN_QTY:
            loss = per * qty + costs.round_trip(entry, stop, qty, direction)
            if loss <= budget:
                return qty
            qty = min(qty - 1, int(qty * budget / loss))
        return 0

    rng = random.Random(7)
    for _ in range(3000):
        eq = rng.choice([3000, 4870.6, 10000, 100000, 500000])
        entry = rng.uniform(150, 4500)
        stop = entry * (1 + rng.uniform(-0.02, 0.02))
        side = "LONG" if stop < entry else "SHORT"
        used = rng.choice([0, 0, eq * 0.5, eq * 0.74])
        assert risk.size_position(eq, entry, stop, side, used)[0] == old(eq, entry, stop, side, used)


def signals_status():
    return {"mode": "live", "equity": 4870.6, "time": t("10:46").isoformat(),
            "rows": {"RELIANCE": {"close": 1365.1, "reason": "SHORT breakout - skip: 1 share costs ₹1,361.00"},
                     "ITC": {"close": 291.3, "reason": "inside range"}, "SBIN": {"close": 1110.8, "reason": "inside range"},
                     "AXISBANK": {"close": 1370.7, "reason": "volume 0.7x < 1.2x"}}}


def test_todays_signals_card_lists_each_signal_and_why(monkeypatch):
    import journal
    monkeypatch.setattr(journal, "load_detail", lambda mode, day: {"signals": [
        {"time": "10:45:35", "symbol": "RELIANCE", "bar": "10:30", "direction": "SHORT", "outcome": "skipped",
         "reason": "1 share costs ₹1,361.00, more than the 25% per-stock limit (₹1,218 of ₹4,871)"},
        {"time": "11:00:40", "symbol": "ITC", "bar": "10:45", "direction": "LONG", "outcome": "taken", "reason": ""}]}
        if (mode, day) == ("live", "2026-11-18") else None)
    card = web.live_signals(signals_status(), now=t("11:05"))
    assert "2 signals · 1 traded · 1 skipped" in card
    assert "RELIANCE" in card and "skipped" in card and "per-stock limit" in card and ">traded<" in card
    assert "Watchlist right now: 2 inside range" in card and "1 volume too low" in card
    assert "one share of <b>AXISBANK, RELIANCE</b> costs more than the 25% per-stock limit (₹1,217.65)" in card
    assert "SBIN" not in card.split("costs more than")[0].split("one share of")[1]      # 1,110.80 fits


def test_no_signal_yet_is_explained(monkeypatch):
    import journal
    monkeypatch.setattr(journal, "load_detail", lambda mode, day: None)
    s = signals_status()
    s.update(mode="paper", equity=100000.0)
    card = web.live_signals(s, now=t("09:50"))
    assert "no signal yet" in card and "close outside the opening range" in card and "before 12:00" in card
    assert "costs more than" not in card                          # nothing is out of reach with Rs 1,00,000
    assert "signals" in web.live_regions("paper", s)


# ---------------------------------------------------------------- 5/6. Review explains its count; jobs say how long
def test_review_says_why_the_other_stocks_gave_no_signal():
    def none(note):
        return {"signal_time": None, "direction": None, "note": note}

    r = {"rows": {"A": {"signal_time": "09:45", "direction": "LONG", "note": ""},
                  "B": {"signal_time": "10:15", "direction": "SHORT", "note": ""},
                  "C": {"signal_time": "10:00", "direction": "LONG", "note": ""},
                  "D": none("09:30 volume 0.5x < 1.2x"), "E": none("09:45 volume 0.9x < 1.2x"),
                  "F": none("12:15 SHORT after 12:00"), "G": none("range 1.61% > 1.5%"),
                  "H": none("inside range"), "I": none("")}}
    line = web.review_funnel(r)
    assert "Of 9 stocks: <b>3</b> gave a signal" in line
    assert "2 volume too low" in line and "1 breakout came after the last entry time" in line
    assert "1 opening range too wide or too narrow" in line and "2 never closed outside the opening range" in line
    assert web.review_funnel({"rows": {}}) == ""


def test_finished_job_says_how_long_it_took(monkeypatch):
    class Done:
        def snapshot(self):
            return {"state": "idle", "done": 50, "total": 50, "elapsed_sec": 14.2, "step": "",
                    "finished": "2026-11-18T10:00:14+05:30", "error": ""}

    monkeypatch.setattr(web.jobs, "get", lambda name: Done())
    strip = web.job_strip("scanner", {"built": "2026-11-18T10:00:14+05:30"}, {"stale": False}, "/scanner?refresh=1", "stocks")
    assert "Completed" in strip and "took 0:14 for 50 stocks" in strip
