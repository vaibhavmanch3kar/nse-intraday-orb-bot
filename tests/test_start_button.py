"""The Start button: shown while the bot is not running, asks first, says which mode the bot would start in, starts
the program the same way the watchdog does, and never changes the mode, the arming or any setting."""

import datetime as dt
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import accounts
import config as C
import health
import livemode
import watchdog
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class Probe:
    def __init__(self, running=False, ok=True):
        self.running, self.ok, self.started = running, ok, 0

    def bot_running(self):
        return self.running

    def start_bot(self):
        self.started += 1
        return self.ok


@pytest.fixture
def probe(monkeypatch):
    p = Probe()
    monkeypatch.setattr(watchdog, "RealProbe", lambda: p)
    return p


def settings(monkeypatch, **values):
    monkeypatch.setattr(livemode, "windows_setting", lambda name: values.get(name, ""))


# ---------------------------------------------------------------- which mode it would start in
def test_mode_is_paper_unless_both_settings_are_on_and_today_is_armed(isolated, monkeypatch):
    settings(monkeypatch)
    assert web.start_mode() == (False, "PAPER (virtual money)")
    settings(monkeypatch, DRY_RUN="false", CONFIRM_LIVE="YES")
    live, text = web.start_mode()
    assert live is False and text == "PAPER (virtual money); real money is not armed today"
    livemode.arm(dt.datetime.now(IST))
    live, text = web.start_mode()
    assert live is True and text.startswith("REAL MONEY")
    settings(monkeypatch, DRY_RUN="false", CONFIRM_LIVE="no")
    assert web.start_mode()[0] is False                       # armed, but one setting is off


def test_start_is_offered_only_while_the_bot_is_not_running(isolated, monkeypatch):
    settings(monkeypatch)
    card = web.live_controls({}, False)
    assert "action='/start'" in card and "▶ Start bot" in card and "name='csrf'" in card
    assert "It would start in <b>PAPER (virtual money)</b>" in card and "never from this button" in card
    assert "It will trade in PAPER mode." in card and "REAL orders" not in card
    running = web.live_controls({"paused": False}, True)
    assert "/start" not in running and "action='/stop'" in running and "action='/pause'" in running
    settings(monkeypatch, DRY_RUN="false", CONFIRM_LIVE="YES")
    livemode.arm(dt.datetime.now(IST))
    live = web.live_controls({}, False)
    assert "It will trade in REAL MONEY mode. REAL orders can be placed." in live and "class='red small'" in live


# ---------------------------------------------------------------- starting
def test_start_asks_windows_once_and_leaves_a_running_bot_alone(isolated, probe):
    assert web.start_bot() == "Starting the bot. It appears here within a minute." and probe.started == 1
    assert "dashboard: Start pressed - bot start requested" in open(watchdog.logs_dir() + "/watchdog.log", encoding="utf-8").read()
    probe.running = True
    assert web.start_bot() == "The bot is already running." and probe.started == 1
    probe.running, probe.ok = False, False
    assert "could not be started" in web.start_bot() and probe.started == 2


def test_start_changes_no_setting_arming_or_mode(isolated, probe, monkeypatch):
    import os
    before_env = dict(os.environ)
    dry, confirm = C.DRY_RUN, C.CONFIRM_LIVE
    web.start_bot()
    assert dict(os.environ) == before_env and (C.DRY_RUN, C.CONFIRM_LIVE) == (dry, confirm)
    assert not os.path.exists(livemode.arm_path())
    assert sorted(os.listdir(str(isolated))) == ["logs"]      # only the watchdog's log line


# ---------------------------------------------------------------- the form on the real page
@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: "")
    monkeypatch.setattr(health, "logged_in", lambda: False)

    def no_kite():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", no_kite)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(base, path, fields, origin=True, csrf=True):
    if csrf:
        fields = {**fields, "csrf": web.CSRF_TOKEN}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = base
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(fields).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, urllib.parse.unquote(r.url), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, "", e.read().decode()


def test_the_form_needs_the_confirmation_the_page_token_and_the_dashboards_own_origin(server, probe):
    code, url, _ = post(server, "/start", {"confirm": ""})
    assert code == 200 and "Not started" in url and probe.started == 0
    assert post(server, "/start", {"confirm": "yes"}, csrf=False)[0] == 403 and probe.started == 0
    assert post(server, "/start", {"confirm": "yes"}, origin=False)[0] == 403 and probe.started == 0
    code, url, body = post(server, "/start", {"confirm": "yes"})
    assert code == 200 and "Starting the bot" in url and probe.started == 1
    assert "Starting the bot. It appears here within a minute." in body
    with pytest.raises(urllib.error.HTTPError):               # a link or an address cannot start it: POST only
        urllib.request.urlopen(server + "/start")
    assert probe.started == 1


def test_health_address_answers_on_the_real_server(server):
    import json
    try:
        with urllib.request.urlopen(server + "/api/health") as r:
            rep = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:                       # 503 when a check is bad: the body is the same list
        rep = json.loads(e.read().decode())
    assert rep["overall"] in ("ok", "warn", "bad") and [c["key"] for c in rep["checks"]][0] == "bot"
