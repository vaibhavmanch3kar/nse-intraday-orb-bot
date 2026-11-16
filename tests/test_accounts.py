"""Dashboard account views: Paper Account numbers, refresh, stale warnings; views are read-only."""

import datetime as dt
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

import accounts
import config as C
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MON_1030 = dt.datetime(2026, 11, 16, 10, 30, tzinfo=IST)      # a trading day, market open
SAT_1030 = dt.datetime(2026, 11, 21, 10, 30, tzinfo=IST)


def write_paper_state(d, mtime=None):
    path = os.path.join(C.DATA_DIR, "state_paper.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    if mtime:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


ONE_LONG = {"cash": 100250.0, "realized_pnl": 300.0, "charges_paid": 50.0, "day_pnl": 120.0,
            "positions": {"ITC": {"direction": "LONG", "qty": 62, "entry": 291.9, "stop": 284.0, "last_price": 293.0}}}


def test_paper_account_numbers():
    write_paper_state(ONE_LONG, mtime=MON_1030 - dt.timedelta(seconds=30))
    a = accounts.paper_account(now=MON_1030)
    assert a["start"] == C.PAPER_START_INR == 100000.0
    assert a["invested"] == pytest.approx(62 * 291.9)
    assert a["unrealised"] == pytest.approx(62 * (293.0 - 291.9))
    assert a["equity"] == pytest.approx(100250.0 + 62 * 1.1)
    assert a["available"] == pytest.approx(a["equity"] - a["invested"])
    assert a["total_pnl"] == pytest.approx(a["equity"] - 100000.0)
    assert (a["realised"], a["realised_today"], a["charges"]) == (300.0, 120.0, 50.0)
    assert a["warnings"] == []


def test_short_position_unrealised():
    write_paper_state({"cash": 100000.0, "positions": {"SBIN": {"direction": "SHORT", "qty": 10, "entry": 100.0,
                                                                "stop": 102.0, "last_price": 99.0}}},
                      mtime=MON_1030)
    assert accounts.paper_account(now=MON_1030)["unrealised"] == pytest.approx(10.0)


def test_no_state_file_means_starting_balance():
    a = accounts.paper_account(now=MON_1030)
    assert a["equity"] == a["available"] == 100000.0 and a["invested"] == 0
    assert "No paper trades yet" in a["warnings"][0]


def test_stale_warning_only_while_market_open():
    write_paper_state(ONE_LONG, mtime=MON_1030 - dt.timedelta(minutes=10))
    assert any("Stale" in w for w in accounts.paper_account(now=MON_1030)["warnings"])
    write_paper_state(ONE_LONG, mtime=SAT_1030 - dt.timedelta(hours=40))
    a = accounts.paper_account(now=SAT_1030)
    assert not any("Stale" in w for w in a["warnings"])
    assert any("earlier day" in w for w in a["warnings"])


def test_unreadable_state_reports_error():
    with open(os.path.join(C.DATA_DIR, "state_paper.json"), "w") as f:
        f.write("{not json")
    assert "Can't read" in accounts.paper_account(now=MON_1030)["error"]
    assert "Can&#x27;t read" in web.paper_card(accounts.paper_account(now=MON_1030))


def test_paper_card_shows_every_figure_and_lag_warning():
    write_paper_state(ONE_LONG, mtime=MON_1030)
    html_ = web.paper_card(accounts.paper_account(now=MON_1030),
                           {"mode": "paper", "data_lag_min": 25, "data_source": {"using": "yahoo"}})
    for label in ("Starting balance", "Total equity", "Available funds", "Invested", "Realised P&amp;L",
                  "Unrealised P&amp;L", "Total P&amp;L", "Charges paid", "Updated 10:30:00 IST"):
        assert label in html_
    assert "₹1,00,000.00" not in html_ and "₹100,000.00" in html_
    assert "about 25 min behind" in html_ and "+₹68.20" in html_


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=x&v=3")
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_live_page_has_refreshing_account_card(server):
    write_paper_state(ONE_LONG)
    code, body = get(server + "/")
    assert code == 200 and "id='acct'" in body and "/api/account?view=paper" in body
    code, frag = get(server + "/api/account?view=paper")
    assert code == 200 and "Paper Account" in frag and "<html" not in frag
