import datetime as dt
import json
import os

import config as C
import data_yahoo
import review
import web
from conftest import IST, bar, history


def day_with_breakout(day, after):
    return [
        bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
        bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
        bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000),   # LONG signal, stop 100.0
        bar(day, "10:00", 101.2, 101.5, 101.1, 101.4, 2000),   # entry at this open
    ] + after


def test_replay_finds_signal_and_target():
    hist, day = history()
    bars = hist + day_with_breakout(day, [bar(day, "10:15", 101.4, 104.0, 101.3, 103.8, 2000)])
    r = review.replay_day(bars, day)
    assert r["direction"] == "LONG" and r["signal_time"] == "09:45"
    assert r["exit_reason"] == "target" and r["exit_time"] == "10:30"
    assert 1.8 < r["r"] < 2.0  # 2R minus charges


def test_replay_stop_and_no_signal():
    hist, day = history()
    r = review.replay_day(hist + day_with_breakout(day, [bar(day, "10:15", 101.4, 101.4, 99.5, 99.8)]), day)
    assert r["exit_reason"] == "stop" and r["r"] < -1
    quiet = hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5), bar(day, "09:30", 100.5, 100.6, 100.4, 100.5)]
    r = review.replay_day(quiet, day)
    assert r["direction"] is None and "inside" in r["note"]
    assert review.replay_day(hist, dt.date(2030, 1, 1))["note"] == "no data for this day"


def test_build_caches_and_web_pages(monkeypatch):
    hist, day = history()
    bars = hist + day_with_breakout(day, [bar(day, "10:15", 101.4, 104.0, 101.3, 103.8, 2000)])
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN", "INFY"])
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars, bars[-1]["c"], None))
    res = review.build()
    assert res["day"] == day.isoformat() and res["signals"] == 2
    assert os.path.exists(os.path.join(C.DATA_DIR, f"review_{day}.json"))

    html_review = web.render_review(day.isoformat())
    assert "Session review" in html_review and "SBIN" in html_review and "<svg" in html_review
    html_trades = web.render_trades()
    assert "No trades yet" in html_trades


def test_live_page_shows_not_running_banner(monkeypatch):
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: None)
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    old = (dt.datetime.now(IST) - dt.timedelta(hours=5)).isoformat()
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": old, "mode": "paper"}, f)
    page = web.render()
    assert "Bot is not running" in page and "Stop bot" not in page
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": dt.datetime.now(IST).isoformat(), "mode": "paper"}, f)
    page = web.render()
    assert "RUNNING" in page and "Stop bot" in page
