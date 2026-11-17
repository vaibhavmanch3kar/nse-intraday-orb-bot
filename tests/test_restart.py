"""Running version, waiting updates, and "restart when safe": PAPER only, before the open or with no open
position, checked after the restart; never automatic in LIVE mode. Restarting the dashboard never touches the bot."""

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlencode

import pytest

import bot
import buildinfo
import config as C
import restart
import web
from conftest import IST

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_files_are_split_into_bot_and_dashboard_only():
    b, d = buildinfo.bot_files(), buildinfo.dashboard_files()
    assert {"bot.py", "strategy.py", "risk.py", "config.py", "broker.py", "nse_holidays.json"} <= b
    assert {"web.py", "review.py", "scanner.py", "timemachine.py"} <= d - b
    assert "web.py" not in b                                   # the bot no longer loads the dashboard


def test_secret_settings_are_never_recorded():
    names = buildinfo.setting_names()
    assert "MAX_TRADES_PER_DAY" in names and "DRY_RUN" in names
    for secret in C._SECRET_VARS:
        assert secret not in names
    assert not [n for n in names if "KEY" in n or "SECRET" in n or "PASS" in n or "TOKEN" in n]
    fp = buildinfo.settings_fp()
    assert all(len(v) == 12 and all(ch in "0123456789abcdef" for ch in v) for v in fp.values())


def test_pending_tells_dashboard_only_from_bot_updates():
    disk = buildinfo.files_fp()
    run = {"files": dict(disk), "settings": {"MAX_TRADES_PER_DAY": "a"}}
    assert buildinfo.pending(run, {"files": dict(disk)}, disk, {"MAX_TRADES_PER_DAY": "a"}) == \
        {"bot_files": [], "settings": [], "dashboard_files": []}
    changed = dict(disk, **{"review.py": "x", "strategy.py": "y"})
    p = buildinfo.pending(run, {"files": dict(disk)}, changed, {"MAX_TRADES_PER_DAY": "b"})
    assert p == {"bot_files": ["strategy.py"], "settings": ["MAX_TRADES_PER_DAY"], "dashboard_files": ["review.py"]}


def at(hhmm, day=dt.date(2026, 11, 18)):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def a_bot():
    b = bot.Bot()
    b.build = {"version": "test"}
    return b


def test_restart_only_when_safe_in_paper():
    b = a_bot()
    assert b.restart_if_requested(at("10:00")) is None          # nothing asked
    restart.request()
    b.state.d["positions"]["SBIN"] = {"entry": 100.0, "qty": 1, "direction": "LONG", "stop": 99.0,
                                      "opened": "2026-11-18T09:45:00+05:30"}
    assert b.restart_if_requested(at("10:00")) is None          # open position during the day: wait
    assert b.restart_if_requested(at("10:01")) is None
    assert sum("waiting: 1 open position" in e for e in map(str, b.events)) == 1   # said once
    assert restart.pending_request()                            # still asked
    assert b.restart_if_requested(at("09:05")) == restart.EXIT_RESTART   # before the open: safe
    st = restart.state()
    assert st["phase"] == "restarting" and st["before"]["positions"] == ["SBIN"] and st["before"]["mode"] == "paper"
    assert restart.pending_request() is None


def test_flat_bot_restarts_during_the_day():
    b = a_bot()
    restart.request()
    assert b.restart_if_requested(at("11:00")) == restart.EXIT_RESTART


def test_live_mode_is_never_restarted_automatically():
    b = a_bot()
    b.live, b.mode = True, "live"
    restart.request()
    assert b.restart_if_requested(at("09:05")) is None
    assert restart.pending_request() is None and restart.state() is None
    assert any("never restarted automatically" in str(e) for e in b.events)


def test_new_bot_checks_itself_after_the_restart():
    restart.begin("paper", ["SBIN"], 2, pid=1, version="v", now=at("10:00"))
    checks = restart.verify("paper", ["SBIN"], 2, pid=os.getpid(), now=at("10:00").replace(second=20))
    assert all(checks.values()) and restart.state()["ok"] is True
    assert restart.problem(now=at("10:05")) is None
    restart.begin("paper", ["SBIN"], 2, pid=1, version="v", now=at("10:00"))
    checks = restart.verify("paper", [], 3, pid=os.getpid(), now=at("10:00").replace(second=20))
    assert not checks["same open positions"] and not checks["same trades today"]
    assert "checks failed" in restart.problem(now=at("10:05"))


def test_bot_that_never_comes_back_is_flagged():
    restart.begin("paper", [], 0, pid=1, version="v", now=at("10:00"))
    assert restart.problem(status_time=at("09:59"), now=at("10:00").replace(second=30)) is None   # give it 60 s
    msg = restart.problem(status_time=at("09:59"), now=at("10:02"))
    assert "has not come back" in msg
    assert restart.problem(status_time=at("10:00").replace(second=40), now=at("10:02")) is None   # it came back


def test_bot_main_exits_with_the_restart_code(monkeypatch):
    monkeypatch.setattr(bot, "setup_logging", lambda: None)
    monkeypatch.setattr(bot, "claim_single_instance", lambda *a: True)
    monkeypatch.setattr(bot, "dashboard_listening", lambda *a: True)

    class Restarting:
        def run(self):
            return restart.EXIT_RESTART

    monkeypatch.setattr(bot, "Bot", Restarting)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == restart.EXIT_RESTART


def test_launcher_starts_the_bot_again_on_the_restart_code():
    ps = open(os.path.join(ROOT, "run_bot.ps1"), encoding="utf-8").read()
    assert "$LASTEXITCODE -eq 3 -and $restarts -lt 3" in ps and "continue" in ps


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    s = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", s.server_address[1])
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def csrf_of(url):
    """The dashboard's per-start form token, read from its own page (as a browser would)."""
    import re
    html = urllib.request.urlopen(url + "/?view=paper", timeout=10).read().decode("utf-8")
    return re.search(r"name='csrf' value='([^']+)'", html).group(1)


def post(url, path, data, origin=True, csrf=None):
    data = dict(data, csrf=csrf if csrf is not None else web.CSRF_TOKEN)
    req = urllib.request.Request(url + path, data=urlencode(data).encode(), method="POST",
                                 headers={"Origin": url} if origin else {})
    try:
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


def status(**kw):
    s = {"time": dt.datetime.now(IST).isoformat(), "mode": "paper", "positions": [], **kw}
    with open(os.path.join(C.DATA_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(s, f)


def test_restart_request_needs_confirmation_and_paper(srv):
    status()
    post(srv, "/bot/restart-when-safe", {"confirm": ""})
    assert restart.pending_request() is None                   # not without the confirmation
    assert post(srv, "/bot/restart-when-safe", {"confirm": "yes"}, origin=False) == 403
    post(srv, "/bot/restart-when-safe", {"confirm": "yes"})
    assert restart.pending_request()
    post(srv, "/bot/restart-cancel", {"confirm": "yes"})
    assert restart.pending_request() is None
    status(mode="live")
    post(srv, "/bot/restart-when-safe", {"confirm": "yes"})
    assert restart.pending_request() is None                   # never in LIVE mode


def test_settings_page_shows_versions_and_waiting_updates(monkeypatch):
    disk = buildinfo.files_fp()
    old = dict(disk, **{"strategy.py": "old"})
    status(build={"version": "v1.8.0", "started": "2026-11-18T09:00:01+05:30", "files": old, "settings": {}})
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    monkeypatch.setattr(web, "_pending_cache", {"t": 0.0, "key": None, "val": None})
    monkeypatch.setattr(buildinfo, "settings_on_next_start", lambda *a, **k: None)
    page = web.render_settings()
    assert "Version and updates" in page and "v1.8.0" in page
    assert "Bot update waiting" in page and "strategy.py" in page and "Restart the bot when safe" in page
    live = web.render("", {})
    assert "Update waiting" in live and "bot update" in live
    status(mode="live", build={"version": "v1.8.0", "started": "x", "files": old, "settings": {}})
    page = web.render_settings()
    assert "never restarted automatically" in page and "Restart the bot when safe" not in page


def test_red_banner_when_the_restart_went_wrong(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    status()
    restart.begin("paper", ["SBIN"], 2, pid=1, version="v")
    restart.verify("paper", [], 2, pid=os.getpid())
    page = web.render("", {})
    assert "Restart problem" in page and "same open positions" in page


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dashboard_restarts_itself_without_touching_anything_else(tmp_path):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, DATA_DIR=str(tmp_path), WEB_PORT=str(port), DATA_SOURCE="yahoo")
    p = subprocess.Popen([sys.executable, "-B", "web.py", "--no-browser"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def version():
        with urllib.request.urlopen(url + "/api/version", timeout=3) as r:
            return json.loads(r.read())["dashboard"]
    new_pid = None
    try:
        for _ in range(100):
            try:
                first = version()
                break
            except OSError:
                time.sleep(0.1)
        assert first["pid"]                 # (the venv's python.exe is a launcher: its pid differs)
        post(url, "/dashboard/restart", {"confirm": "yes"}, csrf=csrf_of(url))
        p.wait(15)                                              # the old dashboard has stopped
        for _ in range(150):
            try:
                new_pid = version()["pid"]
                break
            except OSError:
                time.sleep(0.2)
        assert new_pid and new_pid != first["pid"]
    finally:
        if p.poll() is None:
            p.terminate()
        if new_pid:
            try:
                os.kill(new_pid, 9)
            except OSError:
                pass
