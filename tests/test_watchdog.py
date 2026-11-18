"""Watchdog: an error that ends the bot is recorded in full; the launcher starts the bot again after a crash
(never after a deliberate stop, never outside market hours, at most 5 times a day); the 5-minute check starts
a missing bot or dashboard and ends a bot that stopped responding."""

import datetime as dt
import json
import logging
import os

import pytest

import bot
import config as C
import watchdog

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def t(hhmm, day=dt.date(2026, 11, 18)):                      # Wednesday, a trading day
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), IST)


def boom():
    raise PermissionError(5, "Access is denied: status.json")


# ---------------------------------------------------------------- item 2: crash capture
def test_record_crash_saves_the_full_error():
    try:
        boom()
    except PermissionError as e:
        path = watchdog.record_crash(e, now=t("09:11"), mode="paper", version="v1.9.0", positions=["ITC"])
    text = open(path, encoding="utf-8").read()
    assert os.path.basename(path) == "crash_2026-11-18_091100.txt"
    assert "PermissionError" in text and "in boom" in text and "Traceback" in text
    assert "mode: paper" in text and "open positions: ITC" in text
    d = watchdog.load(t("09:12"))
    assert d["events"][-1]["kind"] == "crash" and d["events"][-1]["file"] == "crash_2026-11-18_091100.txt"
    assert "crash: PermissionError" in open(os.path.join(C.DATA_DIR, "logs", "watchdog.log"), encoding="utf-8").read()


def test_record_crash_never_raises(monkeypatch):
    monkeypatch.setattr(C, "DATA_DIR", os.path.join(C.DATA_DIR, "no\x00where"))
    assert watchdog.record_crash(ValueError("x"), now=t("09:11")) is None


def fake_main(monkeypatch, run):
    monkeypatch.setattr(bot, "setup_logging", lambda: None)
    monkeypatch.setattr(bot, "claim_single_instance", lambda *a: True)
    monkeypatch.setattr(bot, "dashboard_listening", lambda *a: True)
    monkeypatch.setattr(bot, "enable_fault_log", lambda: None)

    class B:
        mode, build = "paper", {"version": "vTest"}

        class state:
            positions = {"SBIN": {}}

        def run(self):
            return run()

    monkeypatch.setattr(bot, "Bot", B)


def test_an_error_that_ends_the_bot_is_logged_and_gives_the_crash_exit_code(monkeypatch, caplog):
    fake_main(monkeypatch, boom)
    with caplog.at_level(logging.INFO, logger="ayodhya"), pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == bot.EXIT_CRASH == 1
    rec = [r for r in caplog.records if "ENDED WITH AN ERROR" in r.getMessage()][0]
    assert rec.levelno == logging.CRITICAL and rec.exc_info and "PermissionError" in rec.getMessage()
    files = [n for n in os.listdir(os.path.join(C.DATA_DIR, "logs")) if n.startswith("crash_")]
    assert len(files) == 1
    text = open(os.path.join(C.DATA_DIR, "logs", files[0]), encoding="utf-8").read()
    assert "version: vTest" in text and "open positions: SBIN" in text and "in boom" in text


def test_an_error_while_starting_the_bot_is_recorded_too(monkeypatch):
    fake_main(monkeypatch, lambda: None)

    def broken():
        raise RuntimeError("state file is not valid JSON")

    monkeypatch.setattr(bot, "Bot", broken)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 1 and watchdog.load()["events"][-1]["text"].startswith("RuntimeError: state file")


def test_ctrl_c_is_a_deliberate_stop(monkeypatch):
    def ctrl_c():
        raise KeyboardInterrupt

    fake_main(monkeypatch, ctrl_c)
    assert bot.main() is None                                # no error exit code
    assert watchdog.stopped_on_purpose() and "Ctrl+C" in watchdog.load()["stopped"]


def test_a_normal_end_and_a_restart_are_unchanged(monkeypatch):
    fake_main(monkeypatch, lambda: None)
    assert bot.main() is None
    fake_main(monkeypatch, lambda: 3)
    with pytest.raises(SystemExit) as e:
        bot.main()
    assert e.value.code == 3 and not [x for x in watchdog.load()["events"] if x["kind"] == "crash"]


def launcher():
    return open(os.path.join(ROOT, "run_bot.ps1"), encoding="utf-8").read()


def test_launcher_records_every_start_and_exit_code():
    ps = launcher()
    assert "launcher.log" in ps and 'Write-LauncherLog "starting the bot"' in ps
    assert "the bot ended with exit code $code" in ps
    assert ps.index("& $py bot.py --launcher-pid $PID") < ps.index("the bot ended with exit code")
    assert all(ord(c) < 128 for c in ps)                      # PowerShell 5.1 needs plain ASCII


# ---------------------------------------------------------------- item 3: start again after a crash
def test_after_exit_restarts_in_market_hours_up_to_five_times():
    for n in range(1, 6):
        assert watchdog.after_exit(1, now=t("10:00")) == ("RESTART", f"{n} of 5")
    verdict, why = watchdog.after_exit(1, now=t("10:30"))
    assert verdict == "STOP" and "5 times" in why
    d = watchdog.load(t("10:31"))
    assert d["restarts"] == 5 and d["events"][-1]["kind"] == "gave_up"
    assert [e["kind"] for e in d["events"]].count("restart") == 5


def test_after_exit_never_restarts_a_normal_end_or_a_deliberate_stop():
    assert watchdog.after_exit(0, now=t("10:00"))[0] == "STOP"
    watchdog.mark_stopped("Stop button", now=t("10:05"))
    verdict, why = watchdog.after_exit(1, now=t("10:06"))
    assert verdict == "STOP" and "on purpose" in why and watchdog.load(t("10:07"))["restarts"] == 0


def test_after_exit_only_on_trading_days_between_nine_and_the_close():
    assert watchdog.after_exit(1, now=t("08:30"))[0] == "STOP"
    assert watchdog.after_exit(1, now=t("15:31"))[0] == "STOP"
    assert watchdog.after_exit(1, now=t("11:00", dt.date(2026, 11, 21)))[0] == "STOP"      # Saturday
    assert watchdog.after_exit(1, now=t("09:00"))[0] == "RESTART"
    assert watchdog.after_exit(-1073741819, now=t("15:29"))[0] == "RESTART"                # a hard crash code


def test_a_new_day_starts_a_new_count():
    for _ in range(5):
        watchdog.after_exit(1, now=t("10:00"))
    assert watchdog.after_exit(1, now=t("10:00", dt.date(2026, 11, 19))) == ("RESTART", "1 of 5")


def test_cli_answers_the_launcher(capsys, monkeypatch):
    monkeypatch.setattr(watchdog, "now_ist", lambda: t("10:00"))
    assert watchdog.main(["watchdog.py", "after-exit", "1"]) == 0
    assert capsys.readouterr().out.strip() == "RESTART 1 of 5"
    assert watchdog.main(["watchdog.py", "after-exit", "0"]) == 0
    assert capsys.readouterr().out.strip().startswith("STOP")


def test_launcher_asks_the_watchdog_after_an_error():
    ps = launcher()
    assert "watchdog.py after-exit $code" in ps and '-like "RESTART*"' in ps
    assert "$LASTEXITCODE -eq 3 -and $restarts -lt 3" in ps            # the update restart is unchanged
    assert "param([switch]$NoBrowser)" in ps


@pytest.mark.skipif(os.name != "nt", reason="the launcher is a PowerShell script")
def test_launcher_really_starts_a_crashed_bot_again(tmp_path):
    """A copy of run_bot.ps1 in an empty folder with a stand-in bot that crashes twice, then ends normally."""
    import shutil
    import subprocess
    import sys
    ps = launcher().replace("AyodhyaORB_bot", "AyodhyaORB_launcher_test").replace("Start-Sleep -Seconds 10", "Start-Sleep -Seconds 1")
    (tmp_path / "run_bot.ps1").write_text(ps, encoding="ascii")
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy(sys.executable, scripts / "python.exe")
    shutil.copy(os.path.join(sys.prefix, "pyvenv.cfg"), tmp_path / ".venv" / "pyvenv.cfg")
    (tmp_path / "bot.py").write_text(
        "import os, sys\nn = len(open('runs.txt').read()) if os.path.exists('runs.txt') else 0\n"
        "open('runs.txt', 'a').write('x')\nsys.exit(0 if n >= 2 else 1)\n")
    (tmp_path / "watchdog.py").write_text(
        "import sys\nopen('asked.txt', 'a').write(sys.argv[2] + ' ')\nprint('RESTART test')\n")
    (tmp_path / "web.py").write_text("")                     # a stand-in dashboard that ends at once
    env = dict(os.environ, DATA_DIR=str(tmp_path / "data"))
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(tmp_path / "run_bot.ps1"), "-NoBrowser"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "runs.txt").read_text() == "xxx"       # crashed, crashed, ended normally
    assert (tmp_path / "asked.txt").read_text().split() == ["1", "1"]
    log_text = (tmp_path / "data" / "logs" / "launcher.log").read_text(encoding="utf-8-sig")
    assert log_text.count("starting the bot") == 3 and log_text.count("exit code 1") == 2
    assert "watchdog says: RESTART test" in log_text and "exit code 0" in log_text


# ---------------------------------------------------------------- item 4: the five-minute check
class Probe:
    def __init__(self, bot=True, dash=True):
        self.bot, self.dash, self.did = bot, dash, []

    def bot_running(self):
        return self.bot

    def dashboard_listening(self):
        return self.dash

    def start_bot(self):
        self.did.append("bot")

    def start_dashboard(self):
        self.did.append("dashboard")

    def kill(self, pid):
        self.did.append(("kill", pid))


def test_check_does_nothing_when_all_is_well_or_outside_market_hours():
    p = Probe()
    assert watchdog.check(t("10:00"), p) == [] and p.did == []
    for when in (t("08:55"), t("15:31"), t("11:00", dt.date(2026, 11, 21))):
        p = Probe(bot=False, dash=False)
        assert watchdog.check(when, p) == [] and p.did == []


def test_check_starts_a_missing_bot_and_dashboard():
    p = Probe(bot=False, dash=False)
    assert watchdog.check(t("10:00"), p) == ["started dashboard", "started bot"]
    assert p.did == ["dashboard", "bot"]
    d = watchdog.load(t("10:01"))
    assert d["restarts"] == 1 and [e["kind"] for e in d["events"]] == ["dashboard", "restart"]


def test_check_respects_a_deliberate_stop_but_still_keeps_the_dashboard():
    watchdog.mark_stopped("Stop button or STOP file", now=t("09:50"))
    p = Probe(bot=False, dash=False)
    assert watchdog.check(t("10:00"), p) == ["started dashboard"] and p.did == ["dashboard"]
    watchdog.bot_started(t("10:20"))                            # someone starts the bot by hand
    assert not watchdog.stopped_on_purpose(t("10:21"))
    p = Probe(bot=False)
    assert watchdog.check(t("10:25"), p) == ["started bot"]


def test_check_gives_up_after_five_starts_and_says_so_once():
    for i in range(5):
        assert watchdog.check(t(f"10:{i * 5:02d}"), Probe(bot=False)) == ["started bot"]
    for i in range(2):
        p = Probe(bot=False)
        assert watchdog.check(t(f"10:{30 + i * 5:02d}"), p) == [] and p.did == []
    assert [e["kind"] for e in watchdog.load(t("10:40"))["events"]].count("gave_up") == 1


def test_crash_restarts_and_watchdog_starts_share_one_daily_limit():
    for _ in range(3):
        watchdog.after_exit(1, now=t("10:00"))
    for i in range(2):
        watchdog.check(t(f"10:{10 + i * 5:02d}"), Probe(bot=False))
    assert watchdog.after_exit(1, now=t("10:30"))[0] == "STOP"


def run_bot_until_stopped(monkeypatch, stop):
    b = bot.Bot()
    b.build = {"version": "test"}
    monkeypatch.setattr(bot, "now_ist", lambda: t("10:00"))
    monkeypatch.setattr(watchdog, "now_ist", lambda: t("10:00"))
    monkeypatch.setattr(b, "cycle", lambda: None)
    monkeypatch.setattr(b, "nap", lambda s: stop(b))
    assert b.run() is None
    return b


def test_stop_button_marks_the_day_and_a_new_start_clears_it(monkeypatch):
    run_bot_until_stopped(monkeypatch, lambda b: open(C.STOP_FILE, "w").close())
    assert "Stop button" in watchdog.load(t("10:01"))["stopped"]
    assert watchdog.after_exit(1, now=t("10:02"))[0] == "STOP"
    run_bot_until_stopped(monkeypatch, lambda b: setattr(b, "launcher_gone", lambda: True))
    d = watchdog.load(t("10:03"))
    assert "launcher" in d["stopped"] and any(e["kind"] == "started" for e in d["events"])


def test_real_probe_checks_without_touching_anything():
    p = watchdog.RealProbe()
    assert p.bot_running() in (True, False) and p.dashboard_listening(timeout=0.2) in (True, False)
    assert "Win32_Process" in p.WMI_START and "WATCHDOG_CMD" in p.WMI_START     # started by Windows, not as a child


def test_scheduled_tasks_script():
    import subprocess
    ps = open(os.path.join(ROOT, "register_task.ps1"), encoding="utf-8").read()
    assert '"Kite ORB Bot"' in ps and '"Kite ORB Watchdog"' in ps and "watchdog.py check" in ps
    assert "pythonw.exe" in ps and "-RepetitionInterval (New-TimeSpan -Minutes 5)" in ps
    assert "-NoWatchdog" in ps and all(ord(c) < 128 for c in ps)
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                              f"'{os.path.join(ROOT, 'register_task.ps1')}',[ref]$null,[ref]$e);$e.Count"],
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "0", out.stdout + out.stderr


# ---------------------------------------------------------------- item 5: crash and restart alerts
def crash_and_restart(monkeypatch, at="10:00"):
    monkeypatch.setattr(watchdog, "now_ist", lambda: t(at))
    try:
        boom()
    except PermissionError as e:
        watchdog.record_crash(e, now=t(at), mode="live")
    watchdog.after_exit(1, now=t(at))


def test_notices_list_todays_crashes_and_restarts(monkeypatch):
    crash_and_restart(monkeypatch)
    watchdog.mark_stopped("Stop button", now=t("10:05"))        # not an alert
    ns = watchdog.notices(t("10:06"))
    assert [n["tone"] for n in ns] == ["dn", "warn"] and len({n["key"] for n in ns}) == 2
    assert ns[0]["title"] == "The bot stopped with an error" and "crash_2026-11-18_100000.txt" in ns[0]["body"]
    assert ns[1]["title"] == "The bot was started again" and "1 of 5" in ns[1]["body"]
    assert watchdog.notices(t("10:06", dt.date(2026, 11, 19))) == []       # a new day starts clean


def test_restarted_bot_says_why_in_its_own_log(monkeypatch):
    crash_and_restart(monkeypatch)
    b = run_bot_until_stopped(monkeypatch, lambda b: open(C.STOP_FILE, "w").close())
    text = " | ".join(b.events)
    assert "Watchdog at 10:00:00: PermissionError" in text and "starting it again (1 of 5 today)" in text


def test_live_page_and_popup_feed_carry_the_watchdog_record(monkeypatch):
    import threading
    import urllib.request
    import accounts
    import web
    import webui
    crash_and_restart(monkeypatch)
    banner = web.watchdog_banner()
    assert "Watchdog today" in banner and "The bot stopped with an error" in banner and "var(--dn)" in banner
    assert "Watchdog today" in web.live_notice({}, running=True) and "Watchdog today" in web.live_notice({}, running=False)
    assert "notices(r.notices)" in webui.ALERTS_JS and "ayodhya-notices-seen" in webui.ALERTS_JS
    accounts.reset_zerodha_events()
    monkeypatch.setattr(accounts, "_kite_client", lambda: (_ for _ in ()).throw(accounts.LoginNeeded("x")))
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/zerodha/events?since=-1") as r:
            d = json.loads(r.read().decode())
        assert [n["tone"] for n in d["notices"]] == ["dn", "warn"]
    finally:
        srv.shutdown()
        accounts.reset_zerodha_events()


def test_no_watchdog_events_means_no_banner():
    import web
    assert web.watchdog_banner() == "" and web.watchdog_notices() == []
