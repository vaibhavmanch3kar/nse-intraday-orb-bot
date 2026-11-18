"""Watchdog item 1: saving a file waits out a busy file (Windows 'Access is denied' while another program has
it open), and a status file that can't be saved never stops the bot."""

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time

import pytest

import bot
import config as C
import risk
import safefile

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def busy_for(monkeypatch, n):
    """os.replace fails n times with 'Access is denied', then works."""
    real, calls = os.replace, []

    def fake(a, b):
        calls.append(1)
        if len(calls) <= n:
            raise PermissionError(5, "Access is denied")
        return real(a, b)

    monkeypatch.setattr(os, "replace", fake)
    return calls


def test_replace_waits_out_a_busy_file(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    path.write_text("old")
    (tmp_path / "x.json.tmp").write_text("new")
    calls, naps = busy_for(monkeypatch, 3), []
    safefile.replace(str(path) + ".tmp", str(path), sleep=naps.append)
    assert path.read_text() == "new" and len(calls) == 4 and len(naps) == 3


def test_replace_gives_up_after_about_a_second(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    (tmp_path / "x.json.tmp").write_text("new")
    calls, naps = busy_for(monkeypatch, 10 ** 6), []
    with pytest.raises(PermissionError):
        safefile.replace(str(path) + ".tmp", str(path), sleep=naps.append)
    assert len(calls) == safefile.TRIES and 1.0 <= safefile.TRIES * safefile.WAIT_SEC <= 2.0


def test_other_errors_are_not_retried(tmp_path):
    with pytest.raises(FileNotFoundError):
        safefile.replace(str(tmp_path / "missing.tmp"), str(tmp_path / "x.json"), sleep=lambda s: 1 / 0)


@pytest.mark.skipif(os.name != "nt", reason="the busy-file error is a Windows behaviour")
def test_real_reader_in_another_program(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("old")
    holder = subprocess.Popen([sys.executable, "-c",
                               f"import time; f = open(r'{path}'); f.read(1); print('open', flush=True); time.sleep(0.5)"],
                              stdout=subprocess.PIPE)
    holder.stdout.readline()                               # the other program has the file open now
    (tmp_path / "status.json.tmp").write_text("new")
    with pytest.raises(PermissionError):                   # what the bot did before: one try
        os.replace(str(path) + ".tmp", str(path))
    t = time.perf_counter()
    safefile.replace(str(path) + ".tmp", str(path))        # now: waits until the reader lets go
    holder.wait()
    assert path.read_text() == "new" and time.perf_counter() - t < 1.5


@pytest.mark.skipif(os.name != "nt", reason="the busy-file error is a Windows behaviour")
def test_retrying_save_always_gets_through_a_constant_reader(tmp_path):
    """Far worse than any dashboard: another program opens and reads the file in a tight loop. A plain
    os.replace fails often here; the bot's retrying save must get through every time."""
    path = tmp_path / "status.json"
    path.write_text("{}")
    reader = subprocess.Popen([sys.executable, "-c", (
        "import json, time\nprint('go', flush=True)\nt = time.time()\n"
        "while time.time() - t < 6:\n"
        "    try:\n"
        f"        json.load(open(r'{path}', encoding='utf-8'))\n"
        "    except Exception:\n        pass\n")], stdout=subprocess.PIPE, text=True)
    assert reader.stdout.readline().strip() == "go"
    plain_failed = saved = 0
    for i in range(120):
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"n": i, "pad": "y" * 6000}, f)
        try:
            os.replace(tmp, str(path))
        except PermissionError:
            plain_failed += 1
            safefile.replace(tmp, str(path))                  # what the bot does now: must not raise
        saved += 1
        time.sleep(0.01)
    reader.kill()
    assert saved == 120 and plain_failed > 0                    # the clash is real, and every save still landed
    assert json.load(open(path, encoding="utf-8"))["n"] == 119


def test_write_json_is_whole_or_not_at_all(tmp_path):
    path = str(tmp_path / "sub" / "a.json")
    safefile.write_json(path, {"a": 1}, indent=1)
    assert json.load(open(path)) == {"a": 1} and not os.path.exists(path + ".tmp")


def test_bot_state_save_survives_a_busy_file(monkeypatch, tmp_path):
    st = risk.State(str(tmp_path / "state_paper.json"), 10000.0)
    st.d["cash"] = 9990.0
    calls = busy_for(monkeypatch, 2)
    monkeypatch.setattr(safefile.time, "sleep", lambda s: None)
    st.save()
    assert len(calls) == 3 and json.load(open(st.path))["cash"] == 9990.0


def test_every_bot_file_save_uses_the_safe_replace():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("risk.py", "journal.py", "livemode.py", "restart.py", "capital.py"):
        src = open(os.path.join(root, name), encoding="utf-8").read()
        assert "safefile.replace(" in src and "os.replace(" not in src, name


def test_unsaved_status_is_a_warning_not_a_crash(monkeypatch, caplog):
    b = bot.Bot()
    busy = [True]

    def denied(tmp, path):
        if busy[0]:
            raise PermissionError(5, "Access is denied")
        os.replace(tmp, path)

    monkeypatch.setattr(safefile, "replace", denied)
    now = dt.datetime(2026, 11, 18, 9, 5, tzinfo=IST)
    with caplog.at_level(logging.INFO, logger="ayodhya"):
        b.write_status(now)                                 # used to raise and end the bot before 09:15
        b.write_status(now)
        busy[0] = False
        b.write_status(now)
    text = [r.getMessage() for r in caplog.records]
    assert sum("could not save the dashboard status file" in m for m in text) == 1     # said once
    assert any("saved again" in m for m in text)
    assert json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))["mode"] == b.mode


def test_bot_keeps_running_before_the_open_when_status_cant_be_saved(monkeypatch):
    b = bot.Bot()
    b.build = {"version": "test"}
    monkeypatch.setattr(bot, "now_ist", lambda: dt.datetime(2026, 11, 18, 9, 5, tzinfo=IST))

    def denied(tmp, path):
        if path.endswith("status.json"):
            raise PermissionError(5, "Access is denied")
        os.replace(tmp, path)

    monkeypatch.setattr(safefile, "replace", denied)
    rounds = []

    def nap(sec):
        rounds.append(1)
        if len(rounds) >= 3:
            open(C.STOP_FILE, "w").close()

    monkeypatch.setattr(b, "nap", nap)
    assert b.run() is None and len(rounds) == 3            # three rounds, no exception, stopped only by STOP
