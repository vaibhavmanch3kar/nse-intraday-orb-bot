"""Saved weekly and monthly reports (Item 3)."""

import csv
import datetime as dt
import os
import threading
import urllib.error
import urllib.request

import pytest

import accounts
import archive
import bot
import config as C
import journal
import reports
import web
from test_journal_periods import row

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture
def week():
    journal.write_rows("paper", [row("2026-11-16", 100000.0, 129.51, outage=8),
                                 row("2026-11-17", 100129.51, -400.0, wins=0, r=-0.8, status="incomplete"),
                                 row("2026-11-20", 99729.51, 300.0, trades=2, r=0.5)])
    with open(os.path.join(C.DATA_DIR, "trades_paper.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["closed", "symbol", "direction", "qty", "entry", "exit", "reason",
                                          "charges", "net", "r"])
        w.writeheader()
        w.writerow({"closed": "2026-11-16 10:45:13+05:30", "symbol": "ITC", "direction": "LONG", "qty": "62",
                    "entry": "291.9", "exit": "294.3", "reason": "time stop", "charges": "19.29",
                    "net": "129.51", "r": "0.26"})
        w.writerow({"closed": "2026-11-23 10:00:00+05:30", "symbol": "SBIN", "direction": "SHORT", "qty": "10",
                    "entry": "800", "exit": "790", "reason": "target", "charges": "20", "net": "80", "r": "1"})


def test_due_on_the_last_trading_day_of_a_week_or_month():
    assert reports.due(dt.date(2026, 11, 18)) == []
    assert reports.due(dt.date(2026, 11, 20)) == [("week", "2026-W47")]          # Friday
    assert reports.due(dt.date(2026, 11, 30)) == [("month", "2026-11")]          # Monday, Dec 1 is Tuesday


def test_week_report_file(week):
    path = reports.write("paper", "week", "2026-W47", today="2026-11-23")
    assert path.endswith(os.path.join("reports", "paper_week_2026-W47.html"))
    text = open(path, encoding="utf-8").read()
    assert "Week 2026-W47 · 16–20 Nov" in text and "Paper account (virtual money)" in text
    assert "1 of 3 day(s) not fully recorded" in text and "incomplete" in text
    assert "<svg" in text and "ITC" in text and "SBIN" not in text               # only this week's trades
    assert "+₹29.51" in text and "8 min" in text
    assert reports.write("paper", "week", "2026-W40") is None                    # nothing that week
    assert reports.write("live", "week", "2026-W47") is None                     # live journal is separate
    month = reports.write("paper", "month", "2026-11", today="2026-11-23")
    assert "November 2026" in open(month, encoding="utf-8").read()
    assert reports.saved("paper") == ["paper_month_2026-11.html", "paper_week_2026-W47.html"]
    for n in ("paper_week_2026-W49.html", "paper_week_2026-W48.html", "paper_month_2026-10.html"):
        open(os.path.join(reports.reports_dir(), n), "w").close()
    assert reports.saved("paper") == ["paper_week_2026-W49.html", "paper_month_2026-11.html",   # newest first
                                      "paper_week_2026-W48.html", "paper_week_2026-W47.html", "paper_month_2026-10.html"]
    assert reports.saved("live") == []


def test_catch_up_after_a_missed_close(week):
    # the bot died on Friday 20 Nov before the close; Monday's first round writes the missed weekly report
    assert [os.path.basename(p) for p in reports.catch_up("paper", dt.date(2026, 11, 23))] == ["paper_week_2026-W47.html"]
    assert reports.catch_up("paper", dt.date(2026, 11, 23)) == []                  # never twice


def test_bot_saves_the_weekly_report_after_friday_close(monkeypatch, week):
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(archive, "fetch_and_save", lambda *a, **k: {"days": [], "source": "kite", "failed": []})
    b = bot.Bot()
    b.end_of_day(dt.datetime(2026, 11, 20, 15, 36, tzinfo=IST))
    assert os.path.exists(os.path.join(reports.reports_dir(), "paper_week_2026-W47.html"))
    assert any("weekly report saved" in e for e in b.events)
    monkeypatch.setattr(reports, "write", lambda *a, **k: 1 / 0)
    b.end_of_day(dt.datetime(2026, 11, 20, 15, 40, tzinfo=IST))                  # must not raise
    assert any("report not saved" in e for e in b.events)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=x&v=3")

    def no_kite():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", no_kite)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def fetch(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_dashboard_links_and_serves_only_report_files(server, week):
    reports.write("paper", "week", "2026-W47", today="2026-11-23")
    _, page = fetch(server + "/trades?view=paper")
    assert "/reports/paper_week_2026-W47.html" in page and "Saved reports" in page
    _, page = fetch(server + "/trades?view=zerodha")
    assert "/reports/paper_week_2026-W47.html" not in page
    code, text = fetch(server + "/reports/paper_week_2026-W47.html")
    assert code == 200 and "Week 2026-W47" in text
    for bad in ("/reports/..%2Fstate_paper.json", "/reports/../trades_paper.csv", "/reports/paper_week_2026-W48.html",
                "/reports/notes.html"):
        assert fetch(server + bad)[0] == 404
