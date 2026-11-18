"""The Kavach page and the price-feed line (read-only), and the dashboard reading the bot's status file only
when it has changed, so that it no longer gets in the way of the bot saving it."""

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

import config as C
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def write_status(tmp_path, monkeypatch, **extra):
    path = str(tmp_path / "status.json")
    s = {"time": dt.datetime.now(IST).isoformat(), "mode": "live", "equity": 4870.6, "trades_today": 1,
         "day_pnl": -10.0, "loss_streak": 1, "positions": [{"symbol": "ITC", "entry": 290.0, "qty": 2}],
         "rows": {}, "events": [], **extra}
    json.dump(s, open(path, "w", encoding="utf-8"))
    monkeypatch.setattr(web, "STATUS_FILE", path)
    return path


FEED = {"source": "kite", "checked_at": "2026-11-18T10:46:40+05:30", "newest_candle": "10:30", "pickup_sec": 12.4,
        "stocks_ok": 11, "stocks": 12, "problems": {"SBIN": "candles missing between 09:30 and 10:00"}, "errors": [],
        "live_needs_kite": False}


@pytest.fixture(autouse=True)
def real_defaults(monkeypatch):
    """The shared test settings switch Kavach's newer limits off; here they have their real default values."""
    import kavach
    for name, default, _ in kavach.LIMITS:
        if name in ("MAX_SIGNAL_AGE_MIN", "MAX_PER_SECTOR", "MAX_BAR_VOLUME_PCT"):
            monkeypatch.setattr(C, name, default)


def test_kavach_page_shows_limits_usage_checks_and_feed(tmp_path, monkeypatch):
    write_status(tmp_path, monkeypatch, feed=FEED)
    page = web.render_kavach()
    for text in ("Kavach", "Limits in force", "% of the account a single trade may lose", "RISK_PER_TRADE_PCT",
                 "as built", "The nine checks every entry passes", "Real money trades only on Kite prices",
                 "Open positions", "1 <small>of 3</small>", "Losses in a row", "Risk per trade now", "₹24.35",
                 "Price feed", "picked up 12 s after it closed", "11 of 12 stocks OK", "1 with a problem",
                 "candles missing between 09:30 and 10:00", "cannot guarantee the price it fills at"):
        assert text in page, text
    assert "LOOSER than built-in" not in page and "<form" not in page.split("<main", 1)[1]      # read-only


def test_a_loosened_limit_is_flagged_on_the_page(tmp_path, monkeypatch):
    write_status(tmp_path, monkeypatch)
    monkeypatch.setattr(C, "RISK_PER_TRADE_PCT", 1.0)
    page = web.render_kavach()
    assert "1 limit(s) are set looser than the built-in value" in page and "LOOSER than built-in" in page
    assert "feed health each round from its next start" in page          # an older bot wrote no feed record


def test_feed_line_warns_when_real_money_is_not_on_kite():
    line = web.feed_line({"feed": dict(FEED, source="yahoo", live_needs_kite=True, problems={}, stocks_ok=12)})
    assert "Yahoo" in line and "real-money entries blocked: not on Kite prices" in line
    assert web.feed_line({}) == "-"
    assert "Price feed" in web.live_limits({"feed": FEED}) and "/kavach" in web.live_limits({})


def test_kavach_route(tmp_path, monkeypatch):
    write_status(tmp_path, monkeypatch, feed=FEED)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/kavach") as r:
            assert r.status == 200 and "Limits in force" in r.read().decode()
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- the dashboard touches the status file rarely
def test_status_is_opened_only_when_it_changed(tmp_path, monkeypatch):
    path = write_status(tmp_path, monkeypatch, marker=1)
    opened = []
    real_open = open

    def counting_open(p, *a, **k):
        if str(p) == path:
            opened.append(1)
        return real_open(p, *a, **k)

    monkeypatch.setattr("builtins.open", counting_open)
    first = web.read_status()
    for _ in range(50):
        assert web.read_status()["marker"] == 1
    assert len(opened) == 1                                       # 51 polls, one open
    first["marker"] = 99
    assert web.read_status()["marker"] == 1                       # callers get their own copy
    monkeypatch.setattr("builtins.open", real_open)
    time.sleep(0.02)
    write_status(tmp_path, monkeypatch, marker=2, pad="x" * 50)
    assert web.read_status()["marker"] == 2                       # the bot saved a new status: read again


def test_status_file_is_looked_at_no_more_than_once_a_second(tmp_path, monkeypatch):
    path = write_status(tmp_path, monkeypatch, marker=1)
    monkeypatch.setattr(web, "STATUS_POLL_SEC", 1.0)
    looked = []
    real_stat = os.stat
    monkeypatch.setattr(web.os, "stat", lambda p, *a, **k: (looked.append(1) if str(p) == path else None) or real_stat(p, *a, **k))
    for _ in range(200):
        assert web.read_status()["marker"] == 1
    assert len(looked) == 1                                       # 200 polls inside a second: one look


def test_missing_or_half_written_status_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", str(tmp_path / "none.json"))
    assert web.read_status() == {}
    bad = tmp_path / "bad.json"
    bad.write_text('{"time": ')
    monkeypatch.setattr(web, "STATUS_FILE", str(bad))
    assert web.read_status() == {}
