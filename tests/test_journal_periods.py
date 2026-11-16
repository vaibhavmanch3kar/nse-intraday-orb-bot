"""Daily | Weekly | Monthly tabs on the Trades page (Item 2): grouping, numbers, separate accounts,
CSV download, and the page only reads."""

import csv
import io
import os
import threading
import urllib.request

import pytest

import accounts
import config as C
import journal
import web


def row(date, start, net, trades=1, wins=1, status="complete", dd=0.5, skipped=1, outage=0, r=0.2,
        reasons="already traded today: 1"):
    return {"date": date, "status": status, "opening_balance": 100000, "start_equity": start,
            "end_equity": start + net, "net_pnl": net, "return_pct": net / start * 100, "charges": 20.0 * trades,
            "trades": trades, "wins": wins, "losses": trades - wins, "total_r": r, "best_trade": net,
            "worst_trade": net, "max_drawdown_inr": None, "max_drawdown_pct": dd, "signals": trades + skipped,
            "taken": trades, "skipped": skipped, "skip_reasons": reasons, "outages": 1 if outage else 0,
            "outage_min": outage, "bot_start": "09:00", "bot_stop": "15:35", "note": ""}


@pytest.fixture
def sample():
    rows = [row("2026-11-16", 100000.0, 129.51, dd=0.1, outage=8),
            row("2026-11-17", 100129.51, -400.0, wins=0, r=-0.8, dd=0.6),
            row("2026-11-20", 99729.51, 300.0, trades=2, wins=1, r=0.5, dd=0.3, status="stopped 14:00",
                reasons="max trades a day: 2"),
            row("2026-12-01", 100029.51, 50.0, dd=0.2)]
    journal.write_rows("paper", rows)
    return rows


def test_weekly_and_monthly_grouping(sample):
    weeks = journal.summarise(journal.days("paper", today="2026-12-02"), "week")
    assert [w["key"] for w in weeks] == ["2026-W47", "2026-W49"]
    w = weeks[0]
    assert w["label"] == "2026-W47 · 16–20 Nov" and w["days"] == 3 and w["full_days"] == 2
    assert w["trades"] == 4 and w["wins"] == 2 and w["win_rate"] == 50.0
    assert round(w["net"], 2) == 29.51 and round(w["return_pct"], 4) == round(29.51 / 1000, 4)
    assert w["best_day"] == 300.0 and w["worst_day"] == -400.0 and w["outage_min"] == 8 and w["skipped"] == 3
    # worst of: intraday 0.6% and close-to-close 100129.51 -> 99729.51 (0.40%)
    assert w["max_drawdown_pct"] == 0.6 and w["drawdown_known"]
    assert w["skip_reasons"] == "already traded today: 2; max trades a day: 2"   # most first, ties A-Z
    assert round(w["avg_r"], 3) == round((0.2 - 0.8 + 0.5) / 4, 3)
    months = journal.summarise(journal.days("paper", today="2026-12-02"), "month")
    assert [m["label"] for m in months] == ["November 2026", "December 2026"]
    assert months[1]["days"] == 1 and months[1]["start_equity"] == 100029.51


def test_close_to_close_drawdown_counts_when_bigger():
    rows = [row("2026-11-16", 100000.0, -1500.0, dd=0.2), row("2026-11-17", 98500.0, -1000.0, dd=0.3)]
    w = journal.summarise(rows, "week")[0]
    assert round(w["max_drawdown_pct"], 2) == 2.5


def test_rebuilt_days_show_unknowns():
    rows = [dict(row("2026-11-16", 100000.0, 129.51), status="rebuilt", max_drawdown_pct=None, skipped=None,
                 outage_min=None, signals=None, skip_reasons="")]
    w = journal.summarise(rows, "week")[0]
    assert w["skipped"] is None and w["outage_min"] is None and not w["drawdown_known"]
    assert journal.drawdown_text(w) == "–"
    rows.append(dict(row("2026-11-17", 100129.51, -1000.0), max_drawdown_pct=None))
    assert journal.drawdown_text(journal.summarise(rows, "week")[0]) == "1.00%*"     # close-to-close is known


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


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8-sig"), r.headers


def snapshot():
    out = {}
    for root, _, files in os.walk(C.DATA_DIR):
        for f in files:
            p = os.path.join(root, f)
            out[p] = open(p, "rb").read()
    return out


def test_trades_page_tabs(server, sample):
    before = snapshot()
    day, _ = get(server + "/trades?view=paper")
    assert "Daily journal" in day and "Mon 16 Nov 2026" in day and "stopped 14:00" in day
    assert "/journal.csv?view=paper&amp;period=day" in day
    week, _ = get(server + "/trades?view=paper&period=week")
    assert "2026-W47 · 16–20 Nov" in week and "3 <span class='muted'>(2 full)</span>" in week
    month, _ = get(server + "/trades?view=paper&period=month")
    assert "November 2026" in month and "December 2026" in month
    bad, _ = get(server + "/trades?view=paper&period=<script>evil1</script>")
    assert "evil1" not in bad and "Mon 16 Nov 2026" in bad             # unknown period -> daily, never echoed
    assert snapshot() == before                                        # viewing writes nothing


def test_zerodha_view_reads_only_the_live_journal(server, sample):
    page, _ = get(server + "/trades?view=zerodha")
    assert "No days yet" in page and "journal_live.csv" in page and "Mon 16 Nov 2026" not in page


def test_csv_download(server, sample):
    text, headers = get(server + "/journal.csv?view=paper&period=week")
    assert "attachment" in headers["Content-Disposition"] and "journal_paper_week.csv" in headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["period"] for r in rows] == ["2026-W47", "2026-W49"] and rows[0]["trades"] == "4"
    text, _ = get(server + "/journal.csv?view=paper&period=day")
    assert [r["date"] for r in csv.DictReader(io.StringIO(text))] == [r["date"] for r in sample]
    text, _ = get(server + "/journal.csv?view=zerodha&period=month")
    assert list(csv.DictReader(io.StringIO(text))) == []
