"""Stopping the bot really stops it: ending the launcher (Stop on the scheduled task) stops the bot the same way
as the STOP file, a stop request wakes it within a second, and stop_bot.ps1 never kills it."""

import os
import subprocess
import sys
import threading
import time

import pytest

import bot
import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles")
def test_watch_launcher_sees_the_launcher_end():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        gone = bot.watch_launcher(["bot.py", "--launcher-pid", str(p.pid)])
        assert gone() is False
        p.kill()
        p.wait(10)
        assert gone() is True
    finally:
        if p.poll() is None:
            p.kill()


def test_no_launcher_option_never_stops_the_bot():
    assert bot.watch_launcher(["bot.py"])() is False
    assert bot.watch_launcher(["bot.py", "--launcher-pid", "x"])() is False


def test_bot_stops_like_the_stop_file_when_the_launcher_ends(monkeypatch):
    b = bot.Bot()
    b.build = {"version": "test"}
    b.launcher_gone = lambda: True
    cycles = []
    monkeypatch.setattr(b, "cycle", lambda: cycles.append(1))
    t = time.perf_counter()
    assert b.run() is None
    assert time.perf_counter() - t < 10 and cycles == []
    assert any("launcher" in str(e) and "shutting down" in str(e) for e in b.events)


def test_a_stop_request_wakes_the_bot_within_a_second():
    b = bot.Bot()
    threading.Timer(0.3, lambda: open(C.STOP_FILE, "w").close()).start()
    t = time.perf_counter()
    b.nap(30)
    assert time.perf_counter() - t < 2.5
    b2 = bot.Bot()
    os.remove(C.STOP_FILE)
    t = time.perf_counter()
    b2.nap(0.5)                                  # no request: sleeps the whole time, as before
    assert time.perf_counter() - t >= 0.45


def test_launcher_and_stop_scripts():
    run = open(os.path.join(ROOT, "run_bot.ps1"), encoding="utf-8").read()
    assert "& $py bot.py --launcher-pid $PID" in run
    stop = open(os.path.join(ROOT, "stop_bot.ps1"), encoding="utf-8").read()
    assert '"STOP"' in stop and "AyodhyaORB_bot" in stop
    assert "Stop-Process" not in stop and "taskkill" not in stop.lower()     # never kills the bot
    assert "stop_bot.ps1" in open(os.path.join(ROOT, "7-STOP-BOT.bat"), encoding="utf-8").read()
    if os.name == "nt":
        for name in ("run_bot.ps1", "stop_bot.ps1"):
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                                  f"'{os.path.join(ROOT, name)}',[ref]$null,[ref]$e);$e.Count"],
                                 capture_output=True, text=True, timeout=60)
            assert out.stdout.strip() == "0", (name, out.stdout, out.stderr)


@pytest.mark.skipif(os.name != "nt", reason="Windows mutex")
def test_stop_script_says_not_running_when_no_bot(tmp_path):
    env = dict(os.environ, DATA_DIR=str(tmp_path))
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                          "if ([System.Threading.Mutex]::TryOpenExisting('Local\\AyodhyaORB_bot', [ref]$null)) "
                          "{ 'BOT-RUNNING' } else { & '" + os.path.join(ROOT, "stop_bot.ps1") + "' }"],
                         capture_output=True, text=True, timeout=60, env=env)
    if "BOT-RUNNING" in out.stdout:
        pytest.skip("the real bot is running on this PC: not touching it")
    assert "not running" in out.stdout and not (tmp_path / "STOP").exists()
