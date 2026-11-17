"""Plan part 6: the real-money safety rules, checked across the whole dashboard rather than one feature at a time.

- No dashboard request, whatever it sends, can make the bot trade real money unless BOTH Windows settings are on
  AND real money was armed today through the full arming steps. Nothing can change the settings themselves.
- Every button is refused without the dashboard's own page (Origin/Host and the per-start secret code).
- Logout stays blocked while the bot holds real-money positions; "Close all" is its own, separate request.
- The arming ends at midnight.
- The header badge says the real mode, and what is waiting, in the same words in every theme."""

import datetime as dt
import http.client
import json
import os
import pathlib
import re
import threading
import urllib.parse

import pytest

import bot
import config as C
import livemode
import theme as T
import web
from conftest import IST

WEB_SRC = pathlib.Path(web.__file__).read_text(encoding="utf-8")
POST_ROUTES = sorted(set(re.findall(r'"(/[a-z][a-z/-]*)"',
                                    WEB_SRC[WEB_SRC.index("    def do_POST"):WEB_SRC.index("\ndef serve")])))
# routes that act on this PC beyond writing a request file: never pressed for real in a test
NOT_PRESSED = {"/bot/go-back", "/dashboard/restart"}
HOSTILE = {"mode": "live", "DRY_RUN": "false", "CONFIRM_LIVE": "YES", "words": "REAL MONEY", "confirm": "yes",
           "live": "1", "arm": "yes", "amount": "25000", "nonce": "a" * 32}


def write_status(mode="paper", positions=0, live_env=False):
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": dt.datetime.now(IST).isoformat(), "mode": mode, "rows": {}, "events": [],
                   "live_env": live_env, "equity": 10000, "cash": 10000, "day_pnl": 0, "trades_today": 0,
                   "positions": [{"symbol": f"S{i}", "qty": 1} for i in range(positions)]}, f)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: None)

    def no_kite():
        raise SystemExit("no Kite settings in tests")
    monkeypatch.setattr(web, "_kite_proxy", no_kite)
    monkeypatch.setattr(livemode, "windows_setting", lambda n: "")
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def post(port, path, fields, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    h = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
         "Content-Type": "application/x-www-form-urlencoded"}
    h.update(headers or {})
    c.request("POST", path, body=urllib.parse.urlencode(fields).encode(), headers=h)
    r = c.getresponse()
    r.read()
    return r.status


def test_the_route_list_covers_the_new_buttons():
    for path in ("/mode/check", "/mode/real", "/mode/paper", "/positions/close-all", "/zerodha/logout", "/pause"):
        assert path in POST_ROUTES


@pytest.mark.parametrize("path", POST_ROUTES)
def test_every_button_is_refused_without_the_dashboard_page(server, path):
    write_status()
    assert post(server, path, HOSTILE) == 403                                         # no secret code
    assert post(server, path, {**HOSTILE, "csrf": web.CSRF_TOKEN}, {"Origin": "https://evil.example"}) == 403
    assert post(server, path, {**HOSTILE, "csrf": web.CSRF_TOKEN}, {"Host": f"evil.example:{server}"}) == 403
    for f in (livemode.arm_path(), livemode.close_all_path(), livemode.to_paper_path(), C.PAUSE_FILE):
        assert not os.path.exists(f)


def press_everything(port):
    flags = (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live(), dict(os.environ))
    for path in POST_ROUTES:
        if path not in NOT_PRESSED:
            post(port, path, {**HOSTILE, "csrf": web.CSRF_TOKEN})
    assert (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live(), dict(os.environ)) == flags   # settings never change


def test_no_button_makes_the_bot_trade_real_money_without_the_settings(server):
    write_status()
    press_everything(server)
    assert not bot.Bot().live
    livemode.arm(login_at=dt.datetime.now(IST))      # even a valid arming alone is not enough
    assert not bot.Bot().live


def test_no_button_arms_real_money_without_the_full_steps(server, monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    write_status(live_env=True)
    press_everything(server)                          # typed words, but no login, no readiness: never armed
    assert not os.path.exists(livemode.arm_path())
    b = bot.Bot()
    assert not b.live and b.mode == "paper"
    log = open(os.path.join(C.DATA_DIR, "logs", "mode_changes.log"), encoding="utf-8").read()
    assert "arm real money: refused" in log


def test_only_settings_plus_todays_arming_is_real_money(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    assert not bot.Bot().live
    livemode.arm(login_at=dt.datetime.now(IST))
    assert bot.Bot().live
    monkeypatch.setattr(C, "CONFIRM_LIVE", "yes")      # exactly YES, as before
    assert not bot.Bot().live


def test_the_arming_ends_at_midnight(monkeypatch):
    late = dt.datetime(2026, 11, 17, 23, 59, tzinfo=IST)
    monkeypatch.setattr(livemode, "_now", lambda: late)
    livemode.arm(login_at=late)
    assert livemode.bot_mode(True)[0]
    monkeypatch.setattr(livemode, "_now", lambda: late + dt.timedelta(minutes=2))
    live, note = livemode.bot_mode(True)
    assert not live and "not today" in note


def test_logout_blocked_with_live_positions_and_close_all_is_separate(server, monkeypatch, tmp_path):
    write_status("live", positions=2, live_env=True)
    msg = web.zerodha_logout()
    assert msg.startswith("Not logged out") and "2 real-money position(s)" in msg
    assert not os.path.exists(livemode.close_all_path())        # refusing logout never closes anything
    assert post(server, "/positions/close-all", {"csrf": web.CSRF_TOKEN}) == 303
    assert not os.path.exists(livemode.close_all_path())        # no confirmation: nothing requested
    assert post(server, "/positions/close-all", {"csrf": web.CSRF_TOKEN, "confirm": "yes"}) == 303
    assert os.path.exists(livemode.close_all_path())


@pytest.mark.parametrize("st", [{"mode": "paper"}, {"mode": "live"}, {"mode": "live", "switch": "to_paper"},
                                {"mode": "paper", "switch": "to_live"}])
def test_badge_reads_the_same_in_every_theme(monkeypatch, st):
    s = {"time": dt.datetime.now(IST).isoformat(), "positions": [{}, {}], **st}
    seen = {}
    for tid in T._ui.ids():
        monkeypatch.setattr(T, "theme_id", lambda tid=tid: tid)
        seen[tid] = re.sub(r"<[^>]+>", "", web.mode_badge(s))
    assert len(set(seen.values())) == 1, seen
    text = seen["default"]
    assert ("REAL MONEY" in text) == (st["mode"] == "live" or st.get("switch") == "to_live")
    if st.get("switch") == "to_paper":
        assert "→ PAPER when 2 close" in text


def test_phone_css_keeps_the_mode_word_and_the_waiting_switch():
    import webui
    phone = webui.BASE_CSS[webui.BASE_CSS.index("@media (max-width:760px)"):]
    hidden = re.search(r"\.mbadge \.long,\.mbadge \.lbl\{display:none\}", phone)
    assert hidden and ".mbadge b{" not in phone.split("display:none")[0][-40:]
    assert ".mswitch{display:none" not in webui.BASE_CSS and ".mbadge:has(.mswitch){white-space:normal" in phone
