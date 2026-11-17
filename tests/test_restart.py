"""Apply Changes & Restart Bot: what runs vs what is in the folder, Apply After Market Close (checked at the
close), Apply Now only when safe (PAPER: flat and not right after a candle close, no new entries while waiting;
LIVE: before 09:15 or after the close, flat), the new code checked in a separate process first, the restart
checked within 90 s, failures with Try again / Go back / Leave it. Requests hold only a time and a nonce."""

import datetime as dt
import json
import os
import re
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
import preflight
import restart
import web
from conftest import IST

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- what runs vs what is in the folder
def test_files_are_split_into_bot_and_dashboard_only():
    b, d = buildinfo.bot_files(), buildinfo.dashboard_files()
    assert {"bot.py", "strategy.py", "risk.py", "config.py", "broker.py", "restart.py", "nse_holidays.json"} <= b
    assert {"web.py", "review.py", "scanner.py", "timemachine.py"} <= d - b
    assert "web.py" not in b


def test_secret_settings_are_never_recorded():
    names = buildinfo.setting_names()
    assert "MAX_TRADES_PER_DAY" in names and "DRY_RUN" in names
    for secret in C._SECRET_VARS:
        assert secret not in names
    assert not [n for n in names if "KEY" in n or "SECRET" in n or "PASS" in n or "TOKEN" in n]
    assert all(len(v) == 12 for v in buildinfo.settings_fp().values())


def test_snapshot_has_version_commit_and_dirty_flag():
    snap = buildinfo.snapshot("bot")
    assert snap["version"] and snap["commit"] and len(snap["commit"]) == 40 and snap["dirty"] in (True, False)
    assert snap["files"]["bot.py"] and snap["settings"]


def test_pending_tells_dashboard_only_from_bot_changes():
    disk = buildinfo.files_fp()
    run = {"files": dict(disk), "settings": {"MAX_TRADES_PER_DAY": "a"}}
    assert buildinfo.pending(run, {"files": dict(disk)}, disk, {"MAX_TRADES_PER_DAY": "a"}) == \
        {"bot_files": [], "settings": [], "dashboard_files": []}
    changed = dict(disk, **{"review.py": "x", "strategy.py": "y"})
    p = buildinfo.pending(run, {"files": dict(disk)}, changed, {"MAX_TRADES_PER_DAY": "b"})
    assert p == {"bot_files": ["strategy.py"], "settings": ["MAX_TRADES_PER_DAY"], "dashboard_files": ["review.py"]}


# ---------------------------------------------------------------- requests: a time and a nonce, nothing else
def test_request_holds_only_time_and_nonce_and_a_second_one_is_refused():
    ok, _ = restart.request_now()
    assert ok and set(restart.pending_request()) == {"requested_at", "nonce"}
    ok, msg = restart.request_now()
    assert not ok and "already pending" in msg
    assert restart.cancel() and restart.pending_request() is None
    log = open(os.path.join(C.DATA_DIR, "logs", "restart_requests.log"), encoding="utf-8").read()
    assert "apply now: requested" in log and "apply now: refused" in log and "cancel: done" in log


@pytest.mark.parametrize("bad", [
    {"requested_at": "2026-11-18T10:00:00+05:30", "nonce": "a" * 32, "mode": "live"},     # extra field
    {"requested_at": "2026-11-18T10:00:00+05:30", "nonce": "short"},
    {"requested_at": "yesterday", "nonce": "a" * 32},
    ["not", "a", "dict"],
])
def test_invalid_requests_are_refused_and_logged(bad):
    with open(restart.request_path(), "w", encoding="utf-8") as f:
        json.dump(bad, f)
    req, why = restart.take_request()
    assert req is None and why
    assert not os.path.exists(restart.request_path())
    assert restart.state()["stage"] == "failed" and "refused" in restart.state()["reason"]


# ---------------------------------------------------------------- when is Apply Now safe?
def at(hhmm, day=dt.date(2026, 11, 18)):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


OPEN, CLOSE = at("09:15"), at("15:30")


def test_safety_rules():
    assert restart.is_safe(False, 0, at("11:05"), OPEN, CLOSE)[0]
    assert restart.is_safe(False, 0, at("09:05"), OPEN, CLOSE) == (True, "before the market opens")
    assert restart.is_safe(False, 1, at("11:05"), OPEN, CLOSE) == (False, "1 open position")
    ok, why = restart.is_safe(False, 0, at("11:00"), OPEN, CLOSE, candle_hold=60)
    assert not ok and "new candle" in why                       # right after a candle close: wait
    assert not restart.is_safe(True, 0, at("11:05"), OPEN, CLOSE)[0]      # real money during the session
    assert restart.is_safe(True, 0, at("09:05"), OPEN, CLOSE)[0]
    assert restart.is_safe(True, 0, at("15:32"), OPEN, CLOSE)[0]
    assert not restart.is_safe(True, 1, at("15:32"), OPEN, CLOSE)[0]


def a_bot(monkeypatch, check_ok=True):
    b = bot.Bot()
    b.build = {"version": "vTEST", "commit": "c" * 40}
    monkeypatch.setattr(preflight, "run_separately", lambda *a, **k: {
        "ok": check_ok, "errors": [] if check_ok else ["strategy.py: SyntaxError: bad"], "version": "vNEW"})
    import data_kite
    monkeypatch.setattr(data_kite, "candle_hold", lambda now=None: 0.0)
    return b


def test_apply_now_restarts_a_flat_paper_bot_after_checking_the_new_code(monkeypatch):
    b = a_bot(monkeypatch)
    restart.request_now()
    assert b.apply_if_requested(at("11:05")) == restart.EXIT_RESTART
    st = restart.state()
    assert st["stage"] == "restarting" and st["before"]["mode"] == "paper"
    assert {"positions", "trades_today", "cash", "balance", "pid", "version"} <= set(st["before"])
    assert restart.pending_request() is None


def test_apply_now_waits_with_an_open_position_and_takes_no_new_entries(monkeypatch):
    b = a_bot(monkeypatch)
    b.state.d["positions"]["SBIN"] = {"entry": 100.0, "qty": 1, "direction": "LONG", "stop": 99.0,
                                      "opened": "2026-11-18T09:45:00+05:30"}
    restart.request_now()
    assert b.apply_if_requested(at("11:05")) is None
    assert b.restart_hold is True                                   # entries paused while waiting
    st = restart.state()
    assert st["stage"] == "waiting" and "1 open position" in st["reason"] and "no new entries" in st["reason"]
    assert restart.pending_request()
    b.state.d["positions"].clear()                                  # the position closed
    assert b.apply_if_requested(at("11:20")) == restart.EXIT_RESTART


def test_entries_are_held_like_pause_while_a_restart_waits():
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    assert "paused = os.path.exists(C.PAUSE_FILE) or self.restart_hold" in src


def test_real_money_restarts_only_outside_the_session(monkeypatch):
    b = a_bot(monkeypatch)
    b.live, b.mode = True, "live"
    restart.request_now()
    assert b.apply_if_requested(at("11:05")) is None
    assert b.restart_hold is False and "real money" in restart.state()["reason"]
    assert b.apply_if_requested(at("15:33")) == restart.EXIT_RESTART
    assert restart.state()["before"]["mode"] == "live"             # the mode is never changed


def test_broken_new_code_keeps_the_old_bot_running(monkeypatch):
    b = a_bot(monkeypatch, check_ok=False)
    restart.request_now()
    assert b.apply_if_requested(at("11:05")) is None
    st = restart.state()
    assert st["stage"] == "failed" and "keeps running the old code" in st["reason"] and "SyntaxError" in st["reason"]
    assert restart.pending_request() is None
    assert any("NOT applied" in str(e) for e in b.events)


def test_apply_after_close_checks_the_new_code_at_the_close(monkeypatch):
    b = a_bot(monkeypatch)
    ok, _ = restart.request_after_close()
    assert ok and not restart.request_after_close()[0]             # duplicate refused
    b.check_changes_at_close(at("15:35"))
    assert restart.state()["stage"] == "checked" and restart.after_close_requested() is None
    b2 = a_bot(monkeypatch, check_ok=False)
    restart.request_after_close()
    b2.check_changes_at_close(at("15:35"))
    assert restart.state()["stage"] == "failed" and "next start would fail" in restart.state()["reason"]


# ---------------------------------------------------------------- after the restart
def test_new_bot_checks_itself_and_records_a_good_start():
    restart.begin({"nonce": "a" * 32}, "paper", ["SBIN"], 2, 9500.0, 10000.0, pid=1, version="vOLD", now=at("10:00"))
    checks = restart.verify("paper", ["SBIN"], 2, 9500.0, 10000.0, os.getpid(), "vNEW", "vNEW",
                            now=at("10:00").replace(second=40))
    assert all(checks.values()) and restart.state()["stage"] == "running"
    restart.begin({"nonce": "a" * 32}, "paper", ["SBIN"], 2, 9500.0, 10000.0, pid=1, version="vOLD", now=at("10:00"))
    checks = restart.verify("paper", ["SBIN"], 2, 9400.0, 10000.0, os.getpid(), "vNEW", "vNEW",
                            now=at("10:02"))
    assert not checks["same cash"] and not checks["back within 90 s"]
    assert "checks failed" in restart.problem(now=at("10:05"))


def test_previous_good_version():
    restart.record_good_start("v1.8.0", "a" * 40)
    restart.record_good_start("v1.9.0", "b" * 40)
    assert restart.previous_good("b" * 40)["version"] == "v1.8.0"
    assert restart.previous_good("a" * 40)["version"] == "v1.9.0"


def test_bot_that_never_comes_back_is_flagged():
    restart.begin({"nonce": "a" * 32}, "paper", [], 0, 1.0, 1.0, pid=1, version="v", now=at("10:00"))
    assert restart.problem(status_time=at("09:59"), now=at("10:01")) is None              # 90 s to come back
    assert "has not come back" in restart.problem(status_time=at("09:59"), now=at("10:02"))
    assert restart.problem(status_time=at("10:00").replace(second=40), now=at("10:02")) is None


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


def test_launcher_is_a_supervisor_that_starts_the_bot_again():
    ps = open(os.path.join(ROOT, "run_bot.ps1"), encoding="utf-8").read()
    assert "$LASTEXITCODE -eq 3 -and $restarts -lt 3" in ps and "continue" in ps


def test_preflight_checks_the_real_folder():
    res = preflight.check()
    assert res["ok"] is True and res["errors"] == []


def test_preflight_reports_a_syntax_error(tmp_path):
    for f in os.listdir(ROOT):
        if f.endswith(".py") or f == "nse_holidays.json":
            with open(os.path.join(ROOT, f), "rb") as src, open(tmp_path / f, "wb") as dst:
                dst.write(src.read())
    (tmp_path / "strategy.py").write_text("def broken(:\n", encoding="utf-8")
    out = subprocess.run([sys.executable, "-B", str(tmp_path / "preflight.py")], capture_output=True, text=True,
                         timeout=120, cwd=str(tmp_path))
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert out.returncode == 1 and not res["ok"] and "strategy.py" in res["errors"][0]


# ---------------------------------------------------------------- the dashboard
@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    monkeypatch.setattr(web, "_pending_cache", {"t": 0.0, "key": None, "val": None})
    monkeypatch.setattr(buildinfo, "settings_on_next_start", lambda *a, **k: None)
    s = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", s.server_address[1])
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def csrf_of(url):
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


def test_apply_buttons_need_confirmation_and_refuse_duplicates(srv):
    status()
    post(srv, "/bot/apply-now", {"confirm": ""})
    assert restart.pending_request() is None                       # not without the confirmation
    assert post(srv, "/bot/apply-now", {"confirm": "yes"}, origin=False) == 403
    assert post(srv, "/bot/apply-now", {"confirm": "yes"}, csrf="wrong") == 403
    assert post(srv, "/bot/apply-now", {"confirm": "yes"}) == 200
    assert set(restart.pending_request()) == {"requested_at", "nonce"}
    assert post(srv, "/bot/apply-now", {"confirm": "yes"}) == 409  # one at a time
    post(srv, "/bot/restart-cancel", {"confirm": "yes"})
    assert restart.pending_request() is None
    assert post(srv, "/bot/apply-after-close", {"confirm": "yes"}) == 200
    assert post(srv, "/bot/apply-after-close", {"confirm": "yes"}) == 409
    with urllib.request.urlopen(srv + "/api/restart", timeout=5) as r:
        assert set(json.loads(r.read())) == {"stage", "reason", "at"}


def changed_build(**kw):
    disk = buildinfo.files_fp()
    return {"version": "v1.8.0", "commit": "a" * 40, "dirty": False, "started": "2026-11-18T09:00:01+05:30",
            "files": dict(disk, **{"strategy.py": "old"}), "settings": {}, **kw}


def test_changes_pending_page_offers_both_ways(srv):
    status(build=changed_build())
    page = web.render_settings()
    assert "Changes Pending" in page and "Bot code or settings" in page and "strategy.py" in page
    assert "Apply After Market Close (recommended)" in page and "Apply Now (when safe)" in page
    assert "Changes pending" in web.render("", {})
    status(mode="live", build=changed_build())
    page = web.render_settings()
    assert "Real money: only before 09:15 or after the close" in page
    restart.request_after_close()
    page = web.render_settings()
    assert "Set to apply after the market close" in page and "Apply Now (when safe)" not in page


def test_waiting_stage_shows_live_and_polls():
    status()
    restart.request_now()
    restart.set_stage("waiting", reason="1 open position; no new entries meanwhile")
    strip = web.restart_strip(json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8")))
    assert "Restart Pending – Waiting for Safe Conditions" in strip and "1 open position" in strip
    assert "/api/restart" in strip and "data-restart-stage='waiting'" in strip and "Cancel" in strip


def test_failure_banner_offers_try_again_go_back_and_leave(monkeypatch):
    status()
    restart.record_good_start("v1.8.0", "a" * 40)
    monkeypatch.setattr(buildinfo, "commit", lambda *a, **k: "b" * 40)
    restart.set_stage("failed", reason="the new code can't start, so the bot keeps running the old code: x")
    strip = web.restart_strip({"time": dt.datetime.now(IST).isoformat(), "mode": "paper"})
    assert "role='alert'" in strip and "Restart problem" in strip and "Try again" in strip
    assert "Go back to v1.8.0" in strip and "Leave it as it is" in strip and "2-START-BOT.bat" in strip


def test_go_back_refuses_edited_files_and_otherwise_switches_and_restarts(monkeypatch):
    status()
    monkeypatch.setattr(buildinfo, "commit", lambda *a, **k: "b" * 40)
    assert web.go_back({})[0] is False                              # nothing recorded yet
    restart.record_good_start("v1.8.0", "a" * 40)
    monkeypatch.setattr(buildinfo, "dirty", lambda *a, **k: True)
    ok, msg = web.go_back({})
    assert not ok and "not committed" in msg
    monkeypatch.setattr(buildinfo, "dirty", lambda *a, **k: False)
    monkeypatch.setattr(buildinfo, "_git", lambda *a, **k: "main")
    calls = []

    class Done:
        returncode, stderr = 0, ""

    monkeypatch.setattr(subprocess, "run", lambda args, **k: (calls.append(args), Done())[1])
    s = {"time": dt.datetime.now(IST).isoformat(), "mode": "paper"}
    ok, msg = web.go_back(s)
    assert ok and calls == [["git", "switch", "--detach", "a" * 40]]
    assert "git switch main" in msg and restart.pending_request()   # and the bot restarts when safe


def test_restart_strip_works_in_every_theme():
    import theme as T
    status()
    restart.request_now()
    for tid in T._ui.THEMES:
        tok = T._ui.activate(tid)
        try:
            page = web.page("Settings", "/settings", web.restart_strip({}))
        finally:
            T._ui.reset(tok)
        assert ".jobstrip{" in page and "Waiting for Safe Conditions" in page, tid


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
        assert first["pid"] and first["commit"]
        post(url, "/dashboard/restart", {"confirm": "yes"}, csrf=csrf_of(url))
        p.wait(15)
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
