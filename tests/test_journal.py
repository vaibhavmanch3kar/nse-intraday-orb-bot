"""Daily journal (Item 1): recorded every round, survives restarts, marks how the day ended, rebuilds
days it missed - and never changes what the bot trades."""

import csv
import datetime as dt
import json
import os

import bot
import config as C
import data_yahoo
import journal
import risk
from conftest import IST, bar, history

DAY = dt.date(2026, 11, 17)


def at(h, m, s=0, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m, s), tzinfo=IST)


def state(**extra):
    s = risk.State(os.path.join(C.DATA_DIR, "state_paper.json"), C.PAPER_START_INR)
    s.d.update(extra)
    return s


def write_trades(rows, mode="paper"):
    path = os.path.join(C.DATA_DIR, f"trades_{mode}.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["closed", "symbol", "direction", "net", "charges", "r"])
        w.writeheader()
        w.writerows(rows)


def saved_rows(mode="paper"):
    return {r["date"]: r for r in journal.read_rows(mode)}


def test_round_signals_outage_and_drawdown():
    st = state(day_start_equity=100000.0)
    j = journal.DayJournal("paper", DAY, at(9, 0), st)
    j.signal(at(9, 45, 30), "ITC", "09:30", "LONG", True)
    j.signal(at(9, 45, 30), "SBIN", "09:30", "SHORT", False, "4 trades today")
    j.signal(at(9, 46, 30), "SBIN", "09:30", "SHORT", False, "already traded today")   # same candle: once
    j.round(at(9, 45, 30), 100000.0, 1, None)
    j.round(at(9, 50, 30), 100400.0, 1, at(9, 50, 0))          # outage starts
    j.round(at(9, 51, 30), 99900.0, 1, at(9, 50, 0))
    j.round(at(9, 58, 30), 100100.0, 1, None)                   # back
    write_trades([{"closed": "2026-11-17 10:45:00+05:30", "symbol": "ITC", "direction": "LONG",
                   "net": "100.0", "charges": "20.0", "r": "0.2"}])
    assert j.save() == ""
    r = saved_rows()["2026-11-17"]
    assert r["status"] == "running" and r["trades"] == "1" and r["wins"] == "1" and r["charges"] == "20.0"
    assert r["signals"] == "2" and r["taken"] == "1" and r["skipped"] == "1"
    assert r["skip_reasons"] == "max trades a day: 1"
    assert float(r["max_drawdown_inr"]) == 500.0 and abs(float(r["max_drawdown_pct"]) - 0.5) < 0.01
    assert r["outages"] == "1" and r["outage_min"] == "8"
    assert float(r["net_pnl"]) == 100.0 and r["bot_start"] == "09:00"
    d = json.load(open(journal.detail_path("paper", "2026-11-17"), encoding="utf-8"))
    assert len(d["equity"]) == 4 and d["outages"] == [{"start": "09:50:00", "end": "09:58:30"}]


def test_restart_same_day_keeps_the_day_and_finish_marks_it():
    st = state(day_start_equity=100000.0)
    j = journal.DayJournal("paper", DAY, at(9, 6), st)
    j.signal(at(9, 30, 30), "ITC", "09:15", "LONG", False, "paused")
    j.round(at(9, 46, 0), 100000.0, 0, at(9, 42, 0))             # dies during an outage
    j.save()
    j2 = journal.DayJournal("paper", DAY, at(9, 54), st)
    assert j2.d["signals"][0]["category"] == "bot paused"
    assert j2.d["outages"][0]["end"] == "09:46:00"                # closed where the bot was last seen
    assert j2.d["sessions"][0]["stop"] == "2026-11-17T09:46:00+05:30" and len(j2.d["sessions"]) == 2
    j2.signal(at(10, 0, 30), "ITC", "09:15", "LONG", True)       # same candle, now taken: taken wins
    j2.finish(at(15, 35, 10), 100130.0, "complete")
    j2.save()
    r = saved_rows()["2026-11-17"]
    assert r["status"] == "complete" and r["taken"] == "1" and r["skipped"] == "0"
    assert r["bot_start"] == "09:06" and r["bot_stop"] == "15:35" and r["note"] == "2 bot starts"
    j3 = journal.DayJournal("paper", DAY, at(15, 50), st)
    j3.finish(at(15, 51), 100130.0, "stopped")
    assert j3.d["status"] == "stopped 15:51"


def test_past_running_day_shows_incomplete_and_missing_days_are_rebuilt():
    write_trades([
        {"closed": "2026-11-16 10:45:13+05:30", "symbol": "ITC", "direction": "LONG", "net": "129.51",
         "charges": "19.29", "r": "0.26"},
        {"closed": "2026-11-18 11:00:00+05:30", "symbol": "SBIN", "direction": "SHORT", "net": "-200.0",
         "charges": "25.0", "r": "-0.4"},
    ])
    os.makedirs(os.path.join(C.DATA_DIR, "logs"), exist_ok=True)
    with open(os.path.join(C.DATA_DIR, "bot.log"), "w", encoding="utf-8") as f:
        f.write("2026-11-16 09:06:30 INFO    ayodhya: 🏹 Ayodhya ORB started in Abhyas · PAPER mode, watching 12 stocks\n"
                "2026-11-16 09:30:32 INFO    ayodhya: STOP file found - Ayodhya ORB shutting down\n"
                "2026-11-16 09:54:24 INFO    ayodhya: 🏹 Ayodhya ORB started in Abhyas · PAPER mode, watching 12 stocks\n")
    j = journal.DayJournal("paper", DAY, at(9, 0), state(day_start_equity=100129.51))
    j.round(at(9, 40), 100129.51, 0, None)
    j.save()
    rows = {r["date"]: r for r in journal.days("paper", today="2026-11-19")}
    assert list(rows) == ["2026-11-16", "2026-11-17", "2026-11-18"]
    r16 = rows["2026-11-16"]
    assert r16["status"] == "rebuilt" and r16["start_equity"] == 100000.0 and round(r16["net_pnl"], 2) == 129.51
    assert r16["bot_start"] == "09:06" and r16["bot_stop"] == "09:30" and "2 bot starts" in r16["note"]
    assert r16["signals"] is None and r16["max_drawdown_pct"] is None           # not recoverable
    assert rows["2026-11-17"]["status"] == "incomplete" and "last seen 09:40" in rows["2026-11-17"]["note"]
    assert round(rows["2026-11-18"]["start_equity"], 2) == 100129.51
    # the bot saves them once a day
    assert journal.backfill("paper", today="2026-11-19") == ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert saved_rows()["2026-11-16"]["status"] == "rebuilt"
    assert journal.backfill("paper", today="2026-11-19") == []


def test_rebuild_follows_opening_balance_changes(monkeypatch):
    import capital
    capital._write_json(capital.settings_path(), {"pending": None, "history": [
        {"applied_at": "2026-11-17T09:00:05+05:30", "from": 100000, "to": 50000}]})
    write_trades([{"closed": "2026-11-16 10:45:00+05:30", "symbol": "ITC", "direction": "LONG", "net": "100.0",
                   "charges": "20", "r": "0.2"},
                  {"closed": "2026-11-18 10:45:00+05:30", "symbol": "ITC", "direction": "LONG", "net": "50.0",
                   "charges": "20", "r": "0.1"}])
    rows = {r["date"]: r for r in journal.days("paper", today="2026-11-19")}
    assert rows["2026-11-16"]["start_equity"] == 100000.0 and rows["2026-11-16"]["opening_balance"] == 100000.0
    assert rows["2026-11-18"]["start_equity"] == 50100.0 and rows["2026-11-18"]["opening_balance"] == 50000.0


def test_csv_open_in_excel_keeps_the_detail(monkeypatch):
    j = journal.DayJournal("paper", DAY, at(9, 0), state(day_start_equity=100000.0))
    j.round(at(9, 30), 100000.0, 0, None)

    def locked(mode, rows):
        raise PermissionError("in use")
    monkeypatch.setattr(journal, "write_rows", locked)
    msg = j.save()
    assert "open in another program" in msg
    assert os.path.exists(journal.detail_path("paper", "2026-11-17"))


def test_paper_and_live_are_separate():
    j = journal.DayJournal("live", DAY, at(9, 0), state(day_start_equity=100000.0))
    j.round(at(9, 30), 100000.0, 0, None)
    j.save()
    assert saved_rows("live") and not saved_rows("paper")
    assert j.row()["opening_balance"] is None


# -- in the bot loop

def setup(monkeypatch, bars_ref, clock):
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars_ref[0], bars_ref[0][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])


def test_bot_records_taken_and_skipped_signals(monkeypatch):
    hist, day = history()
    today = hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
                    bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
                    bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000)]
    bars_ref = [today]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    setup(monkeypatch, bars_ref, clock)
    b = bot.Bot()
    b.cycle()                                                          # breakout -> entry
    bars_ref[0] = today + [bar(day, "10:00", 101.0, 101.1, 99.8, 99.9, 2000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()                                                          # stopped out
    bars_ref[0] = bars_ref[0] + [bar(day, "10:15", 99.9, 101.5, 99.9, 101.4, 4000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 30, 30), tzinfo=IST)
    b.cycle()                                                          # new breakout, already traded
    r = saved_rows()[day.isoformat()]
    assert r["status"] == "running" and r["trades"] == "1" and r["losses"] == "1"
    # 09:45 LONG taken; 10:00 SHORT (close below the range) and 10:15 LONG skipped
    assert r["signals"] == "3" and r["taken"] == "1" and r["skip_reasons"] == "already traded today: 2"
    sigs = json.load(open(journal.detail_path("paper", day.isoformat()), encoding="utf-8"))["signals"]
    assert [(s["bar"], s["direction"], s["outcome"]) for s in sigs] == [
        ("09:45", "LONG", "taken"), ("10:00", "SHORT", "skipped"), ("10:15", "LONG", "skipped")]
    assert float(r["net_pnl"]) < 0 and float(r["max_drawdown_inr"]) > 0
    clock[0] = dt.datetime.combine(day, dt.time(15, 36), tzinfo=IST)
    b.journal_finish(clock[0], "complete")
    assert saved_rows()[day.isoformat()]["status"] == "complete"


def test_journal_failure_never_stops_the_bot(monkeypatch):
    hist, day = history()
    bars_ref = [hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
                        bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
                        bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000)]]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    setup(monkeypatch, bars_ref, clock)

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(journal.DayJournal, "save", broken)
    b = bot.Bot()
    b.cycle()
    assert "SBIN" in b.state.positions                                  # traded exactly as before
    assert any("journal" in e and "disk full" in e for e in b.events)
    b.cycle()
    assert sum("disk full" in e for e in b.events) == 1                 # warned once
