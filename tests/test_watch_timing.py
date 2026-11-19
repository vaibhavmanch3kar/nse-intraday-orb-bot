"""The watchlist says when it last changed and when it should change next (one update per 15-minute candle),
and turns into a "No updates" warning, with one popup per missed candle, when an update is late or the bot
has stopped during market hours."""

import datetime as dt
import json
import threading
import urllib.request

import accounts
import config as C
import web
import webui

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def t(hhmm, day=dt.date(2026, 11, 18)):                     # Wednesday, a trading day
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), IST)


def status(at, last_bar=None, using="kite"):
    rows = {"SBIN": {"bar": last_bar, "reason": "inside range"}, "ITC": {"bar": last_bar}} if last_bar else {}
    return {"time": at.isoformat(), "rows": rows, "data_source": {"using": using}}


def test_between_candles_says_when_the_next_update_comes():
    w = web.watch_timing(status(t("09:34"), "09:15"), now=t("09:34"))
    assert w["state"] == "waiting" and w["last"] == "09:15" and w["key"] == ""
    assert "Next update about 09:46" in w["text"] and "09:30 candle closes at 09:45" in w["text"]
    assert "in 12 min" in w["text"]


def test_before_the_first_candle_closes():
    w = web.watch_timing(status(t("09:20")), now=t("09:20"))
    assert w["state"] == "waiting" and "No candles yet today" in w["text"] and "about 09:31" in w["text"]


def test_before_the_open():
    w = web.watch_timing(status(t("09:05")), now=t("09:05"))
    assert w["state"] == "before_open" and "First update about 09:31" in w["text"]


def test_a_late_candle_says_no_updates_with_a_key_per_candle():
    w = web.watch_timing(status(t("09:52"), "09:15"), now=t("09:52"))        # 09:30 candle due ~09:46
    assert w["state"] == "late" and w["text"].startswith("No updates") and "6 min late" in w["text"]
    assert "Kite" in w["text"] and w["key"] == "2026-11-18-0930"
    w2 = web.watch_timing(status(t("10:05"), "09:15"), now=t("10:05"))
    assert w2["key"] == "2026-11-18-0930"                                   # same missed candle, same popup
    ok = web.watch_timing(status(t("09:50"), "09:15"), now=t("09:50"))      # within the grace minutes
    assert ok["state"] == "waiting"


def test_bot_stopped_in_market_hours():
    s = status(t("10:00"), "09:30")
    w = web.watch_timing(s, now=t("10:10"))                                # status 10 min old
    assert w["state"] == "stopped" and w["text"].startswith("No updates") and w["key"] == "2026-11-18-stopped"


def test_after_the_close_and_on_weekends():
    w = web.watch_timing(status(t("15:31"), "15:00"), now=t("15:40"))
    assert w["state"] == "closed" and "Thu 19 Nov" in w["text"] and "09:31" in w["text"]
    sat = web.watch_timing(status(t("15:31")), now=t("11:00", dt.date(2026, 11, 21)))
    assert sat["state"] == "closed" and "Mon 23 Nov" in sat["text"]


def test_yesterdays_rows_are_not_todays_last_candle():
    w = web.watch_timing(status(t("15:20", dt.date(2026, 11, 17)), "15:00"), now=t("09:40"))
    assert w["last"] is None and w["state"] == "stopped"                    # yesterday's file: bot not running


def test_watchlist_shows_the_timing_line():
    html_ = web.live_watchlist(status(t("09:34"), "09:15"), now=t("09:34"))
    assert "class='wtime waiting'" in html_ and "last candle 09:15" in html_ and "data-late" not in html_
    late = web.live_watchlist(status(t("09:52"), "09:15"), now=t("09:52"))
    assert "class='wtime late'" in late and "data-late='2026-11-18-0930'" in late and "No updates" in late


def test_popup_script_and_styles_handle_the_warning():
    assert "watch(r.watch)" in webui.ALERTS_JS and "'No updates'" in webui.ALERTS_JS
    assert ".wtime.late" in webui.ALERTS_CSS


def test_events_endpoint_carries_the_watch_state(monkeypatch):
    accounts.reset_zerodha_events()
    monkeypatch.setattr(accounts, "_kite_client", lambda: (_ for _ in ()).throw(accounts.LoginNeeded("x")))
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/zerodha/events?since=-1") as r:
            d = json.loads(r.read().decode())
        assert set(d["watch"]) == {"state", "key", "text"} and d["watch"]["text"]
    finally:
        srv.shutdown()
        accounts.reset_zerodha_events()
