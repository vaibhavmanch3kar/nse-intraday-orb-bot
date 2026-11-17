import threading
import urllib.error
import urllib.request

import pytest

import config as C
import web


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


def test_dashboard_renders_without_status(server):
    code, body = get(server + "/")
    assert code == 200 and "Log in to Zerodha" in body and "Not logged in today" in body


def test_failed_login_callback_rejected(server):
    assert get(server + "/kite/callback?status=cancelled")[0] == 400


def test_callback_exchanges_token(server, monkeypatch):
    seen = {}
    monkeypatch.setattr(web, "exchange_token", lambda t: seen.setdefault("t", t) and "CR0000")
    req = urllib.request.Request(server + "/kite/callback?request_token=abc&status=success")
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    with opener.open(req) as r:
        assert r.status == 200
    assert seen["t"] == "abc"


def test_pause_and_cross_origin_post(server):
    import os
    req = urllib.request.Request(server + "/pause", data=b"", method="POST",
                                 headers={"Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403 and not os.path.exists(C.PAUSE_FILE)
    req = urllib.request.Request(server + "/pause", data=("csrf=" + web.CSRF_TOKEN).encode(), method="POST",
                                 headers={"Origin": server})
    urllib.request.urlopen(req)
    assert os.path.exists(C.PAUSE_FILE)
