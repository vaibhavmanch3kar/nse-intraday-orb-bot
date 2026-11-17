"""Only the dashboard's own pages can press its buttons.

A form that changes anything (pause, resume, stop, opening balance, Zerodha logout) is accepted only with an exact
same-dashboard Origin/Referer, the dashboard's own Host, and the secret code (CSRF token) written into its pages.
GET pages also refuse a foreign Host header, so another website can't read them through DNS tricks."""

import datetime as dt
import http.client
import json
import os
import re
import threading
import urllib.parse

import pytest

import config as C
import web
from conftest import IST


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    monkeypatch.setattr(web, "login_status", lambda: (True, "Logged in as AB1234 (token from 2026-11-17T08:00)"))
    monkeypatch.setattr(web, "login_url", lambda: None)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    c.request(method, path, body=urllib.parse.urlencode(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, r.read().decode()


def write_running_status():
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": dt.datetime.now(IST).isoformat(), "mode": "paper", "positions": [], "rows": {},
                   "events": [], "equity": 10000, "cash": 10000, "day_pnl": 0, "trades_today": 0}, f)


def test_post_needs_the_dashboard_origin(server):
    good = {"csrf": web.CSRF_TOKEN}
    for headers in ({},                                                      # no Origin or Referer at all
                    {"Origin": "https://evil.example"},
                    {"Origin": f"http://127.0.0.1:{server}.evil.example"},  # look-alike the old check let through
                    {"Origin": f"http://127.0.0.1:{server + 1}"},            # another local port
                    {"Referer": "https://evil.example/page"}):
        code, _ = request(server, "POST", "/pause", good, headers)
        assert code == 403, headers
        assert not os.path.exists(C.PAUSE_FILE)


def test_post_needs_the_secret_code(server):
    origin = {"Origin": f"http://127.0.0.1:{server}"}
    for body in ({}, {"csrf": ""}, {"csrf": "wrong"}, {"csrf": web.CSRF_TOKEN[:-1]}):
        code, text = request(server, "POST", "/pause", body, origin)
        assert code == 403 and "Reload" in text
        assert not os.path.exists(C.PAUSE_FILE)
    code, _ = request(server, "POST", "/pause", {"csrf": web.CSRF_TOKEN}, origin)
    assert code == 303 and os.path.exists(C.PAUSE_FILE)


def test_referer_and_localhost_work_too(server):
    code, _ = request(server, "POST", "/pause", {"csrf": web.CSRF_TOKEN}, {"Referer": f"http://127.0.0.1:{server}/?view=paper"})
    assert code == 303 and os.path.exists(C.PAUSE_FILE)
    code, _ = request(server, "POST", "/resume", {"csrf": web.CSRF_TOKEN},
                      {"Origin": f"http://localhost:{server}", "Host": f"localhost:{server}"})
    assert code == 303 and not os.path.exists(C.PAUSE_FILE)


def test_foreign_host_header_is_refused(server):
    assert request(server, "GET", "/api/status", headers={"Host": f"evil.example:{server}"})[0] == 403
    assert request(server, "GET", "/", headers={"Host": "evil.example"})[0] == 403
    code, _ = request(server, "POST", "/pause", {"csrf": web.CSRF_TOKEN},
                      {"Host": f"evil.example:{server}", "Origin": f"http://127.0.0.1:{server}"})
    assert code == 403 and not os.path.exists(C.PAUSE_FILE)
    assert request(server, "GET", "/api/status")[0] == 200


def test_every_form_on_the_pages_carries_the_secret_code(server):
    write_running_status()
    forms = []
    for path in ("/", "/?view=paper", "/?view=zerodha"):
        code, page = request(server, "GET", path)
        assert code == 200
        forms += re.findall(r"<form[^>]*method=['\"]post['\"].*?</form>", page, flags=re.S | re.I)
    assert len(forms) >= 3   # pause, stop, opening balance (and logout once logged in)
    for f in forms:
        assert f"name='csrf' value='{web.CSRF_TOKEN}'" in f, f[:120]


def test_the_secret_code_is_new_for_every_dashboard_start():
    import importlib
    import sys
    old = web.CSRF_TOKEN
    mod = importlib.reload(sys.modules["web"])
    try:
        assert mod.CSRF_TOKEN != old and len(mod.CSRF_TOKEN) >= 40
    finally:
        importlib.reload(mod)
