"""Log out of Zerodha from the dashboard: asks first, ends the session at Zerodha where it can, always deletes the
login saved on this PC, never while the bot holds real-money positions, and never shows or logs the token."""

import datetime as dt
import json
import logging
import os
import pathlib
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import config as C
import web
from conftest import IST

TOKEN = "tok-SECRET-0123456789"


class FakeKite:
    """Only invalidate_access_token exists: any other call (an order, say) fails the test."""

    def __init__(self, calls, error=None):
        self.calls, self.error = calls, error

    def invalidate_access_token(self, access_token=None):
        self.calls.append(access_token)
        if self.error:
            raise self.error


class FakeProxy:
    def __init__(self, folder, expired=False, error=None):
        self.TOKEN_FILE = pathlib.Path(folder) / ".kite_token.json"
        self.expired, self.error, self.calls = expired, error, []

    def save(self):
        self.TOKEN_FILE.write_text(json.dumps({"access_token": TOKEN, "user_id": "AB1234",
                                               "created": dt.datetime.now(IST).isoformat()}))

    def load_token(self):
        if not self.TOKEN_FILE.exists() or self.expired:
            return None
        return json.loads(self.TOKEN_FILE.read_text())["access_token"]

    def make_kite(self, access_token=None):
        assert access_token == TOKEN
        return FakeKite(self.calls, self.error)


@pytest.fixture
def kp(monkeypatch, tmp_path):
    p = FakeProxy(tmp_path)
    p.save()
    monkeypatch.setattr(web, "_kite_proxy", lambda: p)
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    return p


def status(mode="paper", positions=0, running=True):
    t = dt.datetime.now(IST) - (dt.timedelta(0) if running else dt.timedelta(hours=3))
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": t.isoformat(), "mode": mode, "rows": {}, "events": [],
                   "positions": [{"symbol": f"S{i}", "qty": 1} for i in range(positions)]}, f)


def test_logout_ends_the_session_and_deletes_the_saved_login(kp, caplog):
    status("paper")
    flags = (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live())
    with caplog.at_level(logging.INFO, logger="ayodhya"):
        msg = web.zerodha_logout()
    assert kp.calls == [TOKEN] and not kp.TOKEN_FILE.exists()
    assert "Zerodha ended the session" in msg and "Yahoo" in msg
    assert TOKEN not in msg and TOKEN not in caplog.text and "logged out" in caplog.text
    assert not os.path.exists(C.PAUSE_FILE)                    # paper mode: nothing paused
    assert (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live()) == flags    # never touches the trading mode


def test_offline_logout_still_deletes_the_saved_login(kp):
    kp.error = OSError("network down")
    status("paper")
    msg = web.zerodha_logout()
    assert kp.calls == [TOKEN] and not kp.TOKEN_FILE.exists()
    assert "could not be reached (OSError)" in msg and "06:00" in msg and "network down" not in msg


def test_expired_login_is_just_deleted(kp):
    kp.expired = True
    status("paper")
    msg = web.zerodha_logout()
    assert kp.calls == [] and not kp.TOKEN_FILE.exists() and "already expired" in msg


def test_not_logged_in(kp):
    kp.TOKEN_FILE.unlink()
    assert "not logged in" in web.zerodha_logout()


def test_refused_while_the_bot_holds_real_money_positions(kp):
    status("live", positions=2)
    msg = web.zerodha_logout()
    assert msg.startswith("Not logged out") and "2 real-money position(s)" in msg
    assert kp.calls == [] and kp.TOKEN_FILE.exists()


def test_live_bot_without_positions_is_paused_by_the_logout(kp):
    status("live", positions=0)
    msg = web.zerodha_logout()
    assert not kp.TOKEN_FILE.exists() and os.path.exists(C.PAUSE_FILE) and "paused" in msg


def test_old_live_status_from_a_stopped_bot_does_not_block(kp):
    status("live", positions=1, running=False)    # yesterday's file: the bot isn't running
    web.zerodha_logout()
    assert not kp.TOKEN_FILE.exists() and not os.path.exists(C.PAUSE_FILE)


@pytest.fixture
def server(monkeypatch, kp):
    monkeypatch.setattr(web, "login_url", lambda: None)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(base, fields):
    req = urllib.request.Request(base + "/zerodha/logout", data=urllib.parse.urlencode(fields).encode(),
                                 method="POST", headers={"Origin": base})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, urllib.parse.unquote(r.url)
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_button_asks_first_and_needs_the_dashboard_page(server, kp, monkeypatch):
    monkeypatch.setattr(web, "login_status", lambda: (True, "Logged in as AB1234"))
    status("paper")
    assert post(server, {"confirm": "yes"})[0] == 403 and kp.TOKEN_FILE.exists()            # no secret code
    code, url = post(server, {"confirm": "", "csrf": web.CSRF_TOKEN})                         # Cancel in the dialog
    assert code == 200 and "needs your confirmation" in url and kp.TOKEN_FILE.exists()
    code, url = post(server, {"confirm": "yes", "csrf": web.CSRF_TOKEN})
    assert code == 200 and "Logged out of Zerodha" in url and not kp.TOKEN_FILE.exists()


def test_button_shows_only_when_logged_in_and_is_locked_with_live_positions(kp, monkeypatch):
    status("paper")
    monkeypatch.setattr(web, "login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=x&v=3")
    card = web.live_login(True, "Logged in as AB1234", web.read_status())
    assert "action='/zerodha/logout'" in card and "Log out of Zerodha?" in card and "confirm(" in card
    assert "name='csrf'" in card
    assert "/zerodha/logout" not in web.live_login(False, "Not logged in today", web.read_status())
    status("live", positions=1)
    card = web.live_login(True, "Logged in as AB1234", web.read_status())
    assert "action='/zerodha/logout'" not in card and "disabled" in card and "1 real-money position(s)" in card
