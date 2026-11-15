"""Advisor Phase 3: the weekly report is read-only, keeps sources apart and restores settings."""

import csv
import datetime as dt
import json
import os

import advisor
import archive
import config as C
from conftest import IST, bar, history


def setup_archive(monkeypatch, extra_short_day=True):
    """5 quiet days, then a LONG breakout day that gets stopped out, plus a 4-candle Sunday session."""
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "TARGET_R", 0)
    hist, day = history()
    session = [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
               bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
               bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000),
               bar(day, "10:00", 101.2, 101.3, 101.1, 101.2, 2000),
               bar(day, "10:15", 101.2, 101.2, 99.5, 99.8, 2000)]
    t = dt.datetime.combine(day, dt.time(10, 30))
    while t.time() <= dt.time(15, 15):                    # rest of a full 25-candle session, flat
        session.append(bar(day, t.strftime("%H:%M"), 99.8, 99.9, 99.7, 99.8, 1000))
        t += dt.timedelta(minutes=15)
    bars = hist + session
    if extra_short_day:
        sunday = day + dt.timedelta(days=(6 - day.weekday()) % 7 or 7)
        bars += [bar(sunday, t, 100, 100.2, 99.8, 100) for t in ("18:15", "18:30", "18:45", "19:00")]
    now = dt.datetime.combine(bars[-1]["t"].date() + dt.timedelta(days=1), dt.time(9, 0), tzinfo=IST)
    archive.save({"SBIN": bars, "^NSEI": bars}, "kite", now)
    return day


def test_helpers():
    assert advisor.band(0.3, advisor.WIDTH_EDGES, "%") == "<0.5%"
    assert advisor.band(0.75, advisor.WIDTH_EDGES, "%") == "0.75-1%"
    assert advisor.band(4, advisor.VOLUME_EDGES, "x") == "≥3x"
    assert advisor.band("", advisor.VOLUME_EDGES, "x") == "unknown"
    assert advisor.entry_window({"signal_bar": "10:45"}) == "10:30-11:00"
    assert advisor.entry_window({"signal_bar": ""}) == "unknown"
    assert advisor.reason_group({"reason": "stop (gap)"}) == "stop"
    assert advisor.reason_group({"reason": "trail EMA21"}) == "EMA trail"
    assert advisor.reason_group({"reason": "time stop"}) == "time stop"
    assert advisor.weekday({"opened": "2026-11-16 10:00:00+05:30"}) == "Mon"


def test_short_sessions_are_skipped(monkeypatch):
    setup_archive(monkeypatch)
    kept, skipped = advisor.session_days(archive.load())
    assert len(kept) == 6 and len(skipped) == 1 and skipped[0][1] == 4


def test_report_is_read_only_and_restores_settings(monkeypatch):
    day = setup_archive(monkeypatch)
    monkeypatch.setattr(C, "INDEX_FILTER", True)          # the bot's defaults: all five rules on
    monkeypatch.setattr(C, "TIME_STOP_BARS", 4)
    monkeypatch.setattr(C, "TRAIL_EMA", 21)
    bt_path = os.path.join(C.DATA_DIR, "backtest_trades.csv")
    with open(bt_path, "w", encoding="utf-8") as f:
        f.write("your,results\n")
    before = {k: getattr(C, k) for k in ("INDEX_FILTER", "TIME_STOP_BARS", "TRAIL_EMA", "USE_VWAP_FILTER",
                                         "VOLUME_MULT", "DATA_DIR")}

    rep = advisor.build_report(workers=1, today=dt.date(2026, 11, 20))

    assert open(bt_path, encoding="utf-8").read() == "your,results\n"     # never overwritten
    assert {k: getattr(C, k) for k in before} == before                    # settings restored
    s = rep["backtest"]["summary"]
    assert s["trades"] == 1 and rep["archive"]["sessions"] == 6
    assert any("Short sessions left out" in n for n in rep["notes"])
    rows = list(csv.DictReader(open(rep["backtest"]["trades_file"], encoding="utf-8")))
    assert rows[0]["signal_bar"] == "09:45" and rows[0]["opened"].startswith(f"{day} 10:00")

    keys = [r["rule"] for r in rep["rules"]]
    assert keys == ["index_filter", "time_stop", "trail", "vwap", "volume"]
    for r in rep["rules"]:
        assert abs(r["effect_net"] - (r["with_rule"]["net_pnl"] - r["without_rule"]["net_pnl"])) < 0.01

    sl = rep["backtest"]["slices"]
    assert sl["entry window"]["rows"][0]["bucket"] == "09:30-10:00"
    assert sl["range width"]["rows"][0]["bucket"] == "≥1%"
    assert sl["exit reason"]["at_entry"] is False
    assert rep["real"]["paper"]["trades"] == 0

    with open(os.path.join(C.DATA_DIR, "advisor", "report_2026-11-20.json"), encoding="utf-8") as f:
        text = f.read()
    assert "Infinity" not in text and "NaN" not in text
    assert json.loads(text)["backtest"]["summary"]["trades"] == 1


def test_paper_trades_are_reported_separately(monkeypatch):
    setup_archive(monkeypatch, extra_short_day=False)
    with open(os.path.join(C.DATA_DIR, "trades_paper.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["opened", "symbol", "direction", "net", "r", "reason", "signal_bar"])
        w.writeheader()
        w.writerow({"opened": "2026-11-16 10:00:00+05:30", "symbol": "SBIN", "direction": "LONG",
                    "net": "50", "r": "0.5", "reason": "time stop", "signal_bar": "09:45"})
    rep = advisor.build_report(rules=False, workers=1)
    assert rep["real"]["paper"]["trades"] == 1
    assert rep["backtest"]["summary"]["trades"] == 1          # not 2: never added together
    assert rep["rules"] == [] and "live" not in rep["real"]


def test_rules_already_off_are_listed(monkeypatch):
    setup_archive(monkeypatch, extra_short_day=False)
    monkeypatch.setattr(C, "TRAIL_EMA", 0)
    rep = advisor.build_report(workers=1)
    assert "trail" not in [r["rule"] for r in rep["rules"]]
    assert any("EMA trail" in x for x in rep["rules_already_off"])


def test_empty_archive_says_what_to_do():
    import pytest
    with pytest.raises(RuntimeError, match="archive.py"):
        advisor.build_report(workers=1)
