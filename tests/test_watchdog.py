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
