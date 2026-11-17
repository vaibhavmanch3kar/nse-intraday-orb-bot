"""Every page header shows the mode the bot ACTUALLY trades in (from its own status), in plain words, in every
theme. The Paper | Zerodha view switch never changes it."""

import datetime as dt
import json
import os
import re
import threading
import urllib.request

import pytest

import config as C
import theme as T
import web
from conftest import IST


def status(mode="paper", running=True, paused=False):
    t = dt.datetime.now(IST) - (dt.timedelta(0) if running else dt.timedelta(hours=3))
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": t.isoformat(), "mode": mode, "paused": paused, "rows": {}, "events": [],
                   "positions": [], "equity": 10000, "cash": 10000, "day_pnl": 0, "trades_today": 0}, f)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: None)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url, theme_id=None):
    req = urllib.request.Request(url, headers={"Cookie": f"ayodhya-ui-theme={theme_id}"} if theme_id else {})
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def badge(page):
    m = re.search(r"<span class='mode-slot' data-region='modebadge'>(.*?)</span></span>", page, flags=re.S)
    assert m, "no mode badge in the header"
    return re.sub(r"<[^>]+>", "", m.group(1))


@pytest.mark.parametrize("path", ["/", "/?view=paper", "/?view=zerodha", "/trades", "/trades?view=zerodha",
                                  "/settings"])
def test_every_page_shows_the_real_mode_whatever_the_view(server, path):
    status("paper")
    assert "Bot trading: PAPER · virtual money" in badge(get(server + path))
    status("live")
    text = badge(get(server + path))
    assert "REAL MONEY" in text and "PAPER" not in text


def test_review_and_scanner_frames_carry_the_badge_too():
    status_s = {"time": dt.datetime.now(IST).isoformat(), "mode": "paper"}
    for path in ("/review", "/scanner", "/journey"):
        assert "Bot trading:" in badge(webui_page(path, status_s))


def webui_page(path, s):
    import webui
    return webui.shell("x", path, "<p>body</p>", mode=web.mode_badge(s))


def test_not_running_and_paused(server):
    status("paper", running=False)
    assert badge(get(server + "/")) == "○Bot not running · last run: PAPER"
    status("live", running=False)
    assert "last run: REAL MONEY" in badge(get(server + "/"))
    status("paper", paused=True)
    assert "new entries paused" in badge(get(server + "/"))
    os.remove(web.STATUS_FILE)
    assert badge(get(server + "/")) == "○Bot not running"


def test_same_plain_words_in_every_theme(server):
    status("live")
    seen = {tid: badge(get(server + "/trades", tid)) for tid in T._ui.ids()}
    assert len(set(seen.values())) == 1 and "REAL MONEY" in seen["default"], seen


def test_live_page_refreshes_the_badge_in_place(server):
    status("paper")
    regions = json.loads(get(server + "/api/live?view=paper"))["regions"]
    assert "modebadge" in regions and "PAPER" in regions["modebadge"]
    status("live")
    assert "REAL MONEY" in json.loads(get(server + "/api/live?view=paper"))["regions"]["modebadge"]


def test_badge_never_changes_the_mode():
    flags = (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live())
    for s in ({"mode": "live", "time": dt.datetime.now(IST).isoformat()}, {}):
        web.mode_badge(s)
    assert (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live()) == flags
