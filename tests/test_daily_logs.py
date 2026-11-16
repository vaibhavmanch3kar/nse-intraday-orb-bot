"""One log file per day (Item 4)."""

import datetime as dt
import logging
import os

import bot
import config as C
import journal

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

OLD = ("2026-11-14 20:58:57 INFO    bot: Bot started in PAPER mode, watching 12 stocks\n"
       "2026-11-16 09:06:30 INFO    ayodhya: 🏹 Ayodhya ORB started in Abhyas · PAPER mode, watching 12 stocks\n"
       "2026-11-16 09:40:00 ERROR   ayodhya: cycle error\n"
       "Traceback (most recent call last):\n"
       "  File \"bot.py\", line 1\n"
       "2026-11-16 10:45:13 INFO    ayodhya: 🏆 EXIT LONG ITC x62 @ 294.30 net ₹129.51 (+0.26R)\n")


def logs_dir():
    return os.path.join(C.DATA_DIR, "logs")


def read(name):
    return open(os.path.join(logs_dir(), name), encoding="utf-8").read()


def test_handler_writes_one_file_per_india_date():
    h = bot.DailyFileHandler(logs_dir())
    h.setFormatter(logging.Formatter("%(message)s"))
    for when, msg in ((dt.datetime(2026, 11, 17, 23, 59, tzinfo=IST), "late"),
                      (dt.datetime(2026, 11, 18, 0, 1, tzinfo=IST), "after midnight"),
                      (dt.datetime(2026, 11, 18, 9, 0, tzinfo=IST), "morning")):
        rec = logging.LogRecord("ayodhya", logging.INFO, __file__, 1, msg, None, None)
        rec.created = when.timestamp()
        h.emit(rec)
    h.close()
    assert read("bot_2026-11-17.log") == "late\n"
    assert read("bot_2026-11-18.log") == "after midnight\nmorning\n"


def test_old_log_is_split_by_date_and_kept():
    with open(os.path.join(C.DATA_DIR, "bot.log"), "w", encoding="utf-8") as f:
        f.write(OLD)
    assert bot.split_old_log(C.DATA_DIR) == ["2026-11-14", "2026-11-16"]
    assert not os.path.exists(os.path.join(C.DATA_DIR, "bot.log"))
    assert read("bot_until_2026-11-16.log") == OLD                         # nothing lost
    assert read("bot_2026-11-14.log").count("\n") == 1
    day16 = read("bot_2026-11-16.log")
    assert day16.count("\n") == 5 and "Traceback" in day16                # traceback stays with its day
    assert bot.split_old_log(C.DATA_DIR) == []                            # only once
    # the journal's rebuild reads the per-day file
    assert any("started in" in ln for ln in journal._log_lines("2026-11-16"))


def test_old_lines_go_before_new_ones_of_the_same_day():
    os.makedirs(logs_dir())
    with open(os.path.join(logs_dir(), "bot_2026-11-16.log"), "w", encoding="utf-8") as f:
        f.write("2026-11-16 15:00:00 INFO    ayodhya: newer\n")
    with open(os.path.join(C.DATA_DIR, "bot.log"), "w", encoding="utf-8") as f:
        f.write(OLD)
    bot.split_old_log(C.DATA_DIR)
    assert read("bot_2026-11-16.log").endswith("newer\n") and read("bot_2026-11-16.log").startswith("2026-11-16 09:06")


def test_setup_logging_uses_daily_files(monkeypatch):
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        with open(os.path.join(C.DATA_DIR, "bot.log"), "w", encoding="utf-8") as f:
            f.write(OLD)
        bot.setup_logging()
        assert isinstance(root.handlers[0], bot.DailyFileHandler)
        logging.getLogger("ayodhya").info("hello from the test")
        root.handlers[0].close()
        today = dt.datetime.now(IST).date().isoformat()
        text = read(f"bot_{today}.log")
        assert "hello from the test" in text and "one file per day in data\\logs (2 days" in text
    finally:
        for h in root.handlers:
            if h not in saved:
                h.close()
        root.handlers[:] = saved


def test_split_failure_never_stops_logging(monkeypatch):
    root = logging.getLogger()
    saved = root.handlers[:]

    def locked(_):
        raise PermissionError("file in use")
    monkeypatch.setattr(bot, "split_old_log", locked)
    try:
        bot.setup_logging()
        logging.getLogger("ayodhya").info("still logging")
        root.handlers[0].close()
        today = dt.datetime.now(IST).date().isoformat()
        text = read(f"bot_{today}.log")
        assert "still logging" in text and "was not split" in text
    finally:
        for h in root.handlers:
            if h not in saved:
                h.close()
        root.handlers[:] = saved
