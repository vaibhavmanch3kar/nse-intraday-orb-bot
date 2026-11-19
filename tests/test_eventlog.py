"""Structured logs: every log line is also one JSON object per line, and secrets are removed before anything is
written, in the plain log, the JSON log and tracebacks alike."""

import datetime as dt
import io
import json
import logging
import os

import pytest

import eventlog

KEY, SECRET, TOKEN, PASS = "abcd1234apikey", "zyxw9876secretvalue", "tok3nValueForToday0001", "pr0xy-P@ss"


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", KEY)
    monkeypatch.setenv("KITE_API_SECRET", SECRET)
    monkeypatch.setenv("TRUEIP_USER", "proxyuser77")
    monkeypatch.setenv("TRUEIP_PASS", PASS)
    monkeypatch.setattr(eventlog, "_todays_token", lambda: TOKEN)


def test_known_values_are_removed_wherever_they_appear():
    text = f"login ok key {KEY} token {TOKEN}; proxy says {PASS} for proxyuser77; secret={SECRET}"
    out = eventlog.redact(text)
    for v in (KEY, SECRET, TOKEN, PASS, "proxyuser77"):
        assert v not in out
    assert out.count(eventlog.HIDDEN) >= 5 and out.startswith("login ok key [hidden] token [hidden]")


@pytest.mark.parametrize("text, gone", [
    ("GET /session?api_key=qqqq1111&request_token=rrrr2222&checksum=cccc3333 200", ("qqqq1111", "rrrr2222", "cccc3333")),
    ("{'access_token': 'unknownTokenXYZ', 'user_id': 'AB1234'}", ("unknownTokenXYZ",)),
    ("Authorization: token somekey:sometoken99", ("somekey:sometoken99",)),
    ("proxy http://someuser:somepass@10.1.2.3:8080 refused", ("someuser", "somepass")),
    ('"password": "hunter2hunter2"', ("hunter2hunter2",)),
])
def test_things_shaped_like_secrets_are_removed_even_when_unknown(text, gone):
    out = eventlog.redact(text, values=[])
    for g in gone:
        assert g not in out, out
    assert "[hidden]" in out


def test_ordinary_lines_and_short_values_are_left_alone(monkeypatch):
    monkeypatch.setenv("TRUEIP_USER", "ab")                    # too short to blank: it would eat ordinary text
    line = "ENTER LONG KOTAKBANK x2 @ 500.95 stop 494.10; about 12:18; user_id AB1234; http://127.0.0.1:5000/kavach"
    assert eventlog.redact(line) == line
    assert eventlog.redact("") == "" and eventlog.redact(None) is None


def logger_with(handler, name):
    lg = logging.getLogger(name)
    lg.handlers[:] = [handler]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg


def test_json_lines_file_has_one_object_per_record_without_secrets(tmp_path):
    lg = logger_with(eventlog.JsonLinesHandler(str(tmp_path), "bot"), "t.json")
    lg.info(f"order sent with {TOKEN}")
    try:
        raise RuntimeError(f"kite said no: api_secret={SECRET}")
    except RuntimeError:
        lg.exception("cycle error")
    lg.handlers[0].close()
    day = dt.datetime.now(eventlog.IST).date().isoformat()
    lines = open(tmp_path / f"events_{day}.jsonl", encoding="utf-8").read().splitlines()
    rows = [json.loads(x) for x in lines]
    assert [r["level"] for r in rows] == ["INFO", "ERROR"] and all(r["program"] == "bot" and r["pid"] == os.getpid() for r in rows)
    assert set(rows[0]) == {"ts", "level", "program", "logger", "pid", "msg"} and rows[0]["msg"] == "order sent with [hidden]"
    assert "RuntimeError" in rows[1]["exc"] and SECRET not in lines[1] and TOKEN not in lines[0]
    assert dt.datetime.fromisoformat(rows[0]["ts"]).utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert [r["level"] for r in eventlog.read(day, str(tmp_path), level="ERROR")] == ["ERROR"]
    assert eventlog.read("2000-01-01", str(tmp_path)) == []


def test_install_cleans_the_existing_handlers_and_adds_the_json_file_once(tmp_path):
    root = logging.getLogger("t.install")
    root.propagate = False
    buf = io.StringIO()
    plain = logging.StreamHandler(buf)
    plain.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.handlers[:] = [plain]
    assert eventlog.install("bot", str(tmp_path), root) and eventlog.install("bot", str(tmp_path), root)
    assert sum(isinstance(h, eventlog.JsonLinesHandler) for h in root.handlers) == 1
    root.warning(f"proxy http://u5er:{PASS}@1.2.3.4 failed with key {KEY}")
    try:
        raise ValueError(f"bad token {TOKEN}")
    except ValueError:
        root.exception("boom")
    text = buf.getvalue()
    assert text.startswith("WARNING proxy http://[hidden]:[hidden]@1.2.3.4 failed with key [hidden]")
    assert "ValueError: bad token [hidden]" in text and TOKEN not in text and KEY not in text and PASS not in text
    for h in root.handlers:
        h.close()


def test_install_never_raises():
    class Broken:
        handlers = property(lambda self: (_ for _ in ()).throw(RuntimeError("no")))

    assert eventlog.install("bot", "x", Broken()) is False


def test_the_bot_and_the_dashboard_both_install_it():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert 'eventlog.install("bot"' in open(os.path.join(root, "bot.py"), encoding="utf-8").read()
    assert 'eventlog.install("dashboard"' in open(os.path.join(root, "web.py"), encoding="utf-8").read()


def test_bot_logging_setup_writes_both_files_and_hides_secrets(isolated):
    import bot
    import config as C
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        bot.setup_logging()
        logging.getLogger("ayodhya").info(f"REAL MONEY check with {TOKEN}")
        for h in root.handlers:
            h.flush()
        day = dt.datetime.now(eventlog.IST).date().isoformat()
        plain = open(os.path.join(C.DATA_DIR, "logs", f"bot_{day}.log"), encoding="utf-8").read()
        rows = eventlog.read(day, os.path.join(C.DATA_DIR, "logs"))
        assert "REAL MONEY check with [hidden]" in plain and TOKEN not in plain
        assert rows[-1]["msg"] == "REAL MONEY check with [hidden]" and rows[-1]["program"] == "bot"
    finally:
        for h in root.handlers:
            if h not in saved:
                h.close()
        root.handlers[:] = saved
        root.setLevel(level)
