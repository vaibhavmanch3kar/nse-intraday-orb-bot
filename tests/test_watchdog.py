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
