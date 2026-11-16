"""Redesigned Live page (items 3-4): named parts that update in place from a read-only JSON address,
no page reload, both themes, and nothing is ever written by looking."""

import datetime as dt
import json
import os
import threading
import urllib.request

import pytest

import accounts
import config as C
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture
def server(monkeypatch):
    import review
    import scanner
    from test_dashboard_a11y import REVIEW, SCAN
    monkeypatch.setattr(review, "latest", lambda *a, **k: REVIEW)        # no price fetching in tests
    monkeypatch.setattr(scanner, "latest", lambda *a, **k: SCAN)
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=x&v=3")

    def no_kite():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", no_kite)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode(), r.headers.get("Content-Type")


def running_status(**extra):
    s = {"time": dt.datetime.now(IST).isoformat(), "mode": "paper", "trades_today": 1, "day_pnl": 50.0,
         "equity": 100050.0, "data_lag_min": 3.0, "data_source": {"using": "yahoo"},
         "positions": [{"symbol": "TCS", "direction": "LONG", "qty": 2, "entry": 3400.0, "stop": 3380.0,
                        "target": None, "price": 3410.0, "unrealised": 20.0, "r_unreal": 0.5, "held_min": 30,
                        "stop_dist_pct": 0.88, "opened": "x"}],
         "rows": {"TCS": {"close": 3410.0, "or_low": 3380.0, "or_high": 3398.0, "reason": "LONG breakout",
                          "bar": "10:00"}},
         "events": ["10:00:31 🏹 ENTER LONG TCS x2 @ 3400.00"]}
    s.update(extra)
    with open(os.path.join(C.DATA_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(s, f)


def snapshot():
    out = {}
    for root, _, files in os.walk(C.DATA_DIR):
        for name in files:
            p = os.path.join(root, name)
            out[p] = open(p, "rb").read()
    return out


def test_live_page_updates_in_place_without_reloading(server):
    running_status()
    body, _ = get(server + "/?view=paper")
    assert 'http-equiv="refresh"' not in body                       # no whole-page reload any more
    assert "/api/live?view=" in body and "data-region='acct'" in body and "data-region='positions'" in body
    assert "id='capital'" in body or 'id="capital"' in body
    assert body.index('id="capital"') > 0 and "data-region='capital'" not in body   # the form is never swapped
    assert "id='offline'" in body or 'id="offline"' in body


def test_api_live_sends_the_same_parts_as_json(server):
    running_status()
    text, ctype = get(server + "/api/live?view=paper")
    d = json.loads(text)
    assert ctype.startswith("application/json") and d["view"] == "paper"
    r = d["regions"]
    assert {"status", "acct", "positions", "watchlist", "events", "controls", "notice"} <= set(r)
    assert "TCS" in r["positions"] and "+0.50R" in r["positions"] and "Stop bot" in r["controls"]
    assert "Bot trading: Abhyas · PAPER" in r["status"] and r["notice"] == ""
    assert "2 <small>of 4</small>" not in r["acct"] and "1 <small>of 4</small>" in r["acct"]


def test_api_live_zerodha_view_is_read_only_and_separate(server):
    running_status()
    before = snapshot()
    d = json.loads(get(server + "/api/live?view=zerodha")[0])
    assert d["view"] == "zerodha" and "card acct zerodha" in d["regions"]["acct"]
    assert "Log in to Zerodha" in d["regions"]["acct"]
    assert d["regions"]["positions"] == ""                          # the bot's paper positions stay in the Paper view
    assert "Bot trading: Abhyas · PAPER" in d["regions"]["status"]  # the bot's mode is unchanged
    d2 = json.loads(get(server + "/api/live?view=<b>")[0])
    assert d2["view"] == "paper"                                    # unknown view -> the bot's own account
    assert snapshot() == before                                     # looking never writes a file


def test_not_running_hides_controls_and_shows_notice(server):
    running_status(time=(dt.datetime.now(IST) - dt.timedelta(hours=3)).isoformat())
    r = json.loads(get(server + "/api/live")[0])["regions"]
    assert r["controls"] == "" and "Bot is not running" in r["notice"] and "NOT RUNNING" in r["status"]


def test_both_themes_and_phone_navigation_are_in_every_page(server):
    for path in ("/", "/trades", "/review", "/scanner"):
        body, _ = get(server + path)
        assert "data-theme=light" in body and "prefers-color-scheme:light" in body
        assert "id='themeBtn'" in body and "class='bottom'" in body and "Skip to content" in body
