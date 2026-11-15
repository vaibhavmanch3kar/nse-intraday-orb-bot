"""Display features: equity curve, P&L bars, breakdowns, trade journey chart, richer position rows."""

import csv
import datetime as dt
import json
import os

import bot
import charts
import config as C
import data_yahoo
import web
from conftest import IST, bar, history


def write_trades(path, rows):
    cols = ["opened", "closed", "symbol", "direction", "qty", "entry", "exit", "stop", "target",
            "gross", "charges", "net", "r", "reason"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def trade(day, sym="SBIN", d="LONG", net=50.0, r=0.5, reason="trail EMA21"):
    return {"opened": f"{day} 10:00:00+05:30", "closed": f"{day} 11:15:00+05:30", "symbol": sym,
            "direction": d, "qty": 10, "entry": 101.2, "exit": 101.9, "stop": 100.0, "target": "",
            "gross": net + 20, "charges": 20, "net": net, "r": r, "reason": reason}


def test_chart_helpers_render_svg():
    eq = charts.equity_curve([100100, 99900, 100300], 100000, ["a", "b", "c"])
    assert eq.startswith("<svg") and "polyline" in eq and "polygon" in eq   # line + drawdown area
    assert "Trade 3: equity ₹100,300" in eq
    bars = charts.pnl_bars([100, -50, 25])
    assert bars.count("<rect") == 3 and "#ef4444" in bars and "#22c55e" in bars
    assert "No trades" in charts.equity_curve([], 100000) and "No trades" in charts.pnl_bars([])
    day = dt.date(2026, 11, 13)
    b = [bar(day, "09:15", 100, 101, 99.5, 100.5), bar(day, "09:30", 100.5, 102, 100.4, 101.8),
         bar(day, "09:45", 101.8, 102.5, 101.5, 102.2)]
    j = charts.journey(b, {"direction": "LONG", "entry": 101.9, "exit": 102.2, "stop": 99.5, "target": None,
                           "opened": b[1]["t"], "closed": b[2]["t"] + dt.timedelta(minutes=15)},
                       ema=[100.5, 101.0, 101.4], or_range=(99.5, 101))
    assert j.count("<polygon") == 2 and "stop 99.50" in j and "EMA trail" in j and "09:15" in j
    assert "No price data" in charts.journey([], {})


def test_trades_page_has_curve_bars_breakdowns_and_links():
    rows = [trade("2026-11-16", net=60, r=0.6), trade("2026-11-16", "INFY", "SHORT", -40, -0.4, "time stop"),
            trade("2026-11-17", net=-30, r=-0.3, reason="stop")]
    write_trades(os.path.join(C.DATA_DIR, "trades_paper.csv"), rows)
    page = web.render_trades()
    assert page.count("<svg") >= 2
    assert "By exit reason" in page and "Long vs short" in page and "By stock" in page
    assert "time stop" in page and "/journey?src=paper&amp;i=2" in page
    assert "Max drawdown" in page


def test_journey_page(monkeypatch):
    hist, day = history()
    today = [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000), bar(day, "09:30", 100.5, 100.6, 100.4, 100.5),
             bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000), bar(day, "10:00", 101.2, 101.6, 101.1, 101.5),
             bar(day, "10:15", 101.5, 102.0, 101.4, 101.9)]
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: (hist + today, 101.9, None))
    monkeypatch.setattr(data_yahoo, "closed_bars", lambda bars, now, i=15: bars)
    web._BARS_CACHE.clear()
    write_trades(os.path.join(C.DATA_DIR, "backtest_trades.csv"), [trade(day.isoformat())])
    page = web.render_journey("backtest", 0)
    assert "<svg" in page and "SBIN" in page and "75 min" in page and "Opening range" in page
    assert "Trade not found" in web.render_journey("backtest", 5)
    assert "Trade not found" in web.render_journey("nope", 0)
    old = trade("2020-01-01")
    write_trades(os.path.join(C.DATA_DIR, "backtest_trades.csv"), [old])
    assert "60 days" in web.render_journey("backtest", 0)


def test_position_details_and_row(monkeypatch):
    monkeypatch.setattr(C, "TIME_STOP_BARS", 4)
    monkeypatch.setattr(C, "TRAIL_EMA", 21)
    now = dt.datetime(2026, 11, 16, 10, 40, tzinfo=IST)
    p = {"direction": "LONG", "qty": 10, "entry": 100.0, "stop": 98.0, "target": None,
         "opened": "2026-11-16 10:00:30+05:30", "checked_upto": "2026-11-16T09:45:00+05:30"}
    day = dt.date(2026, 11, 16)
    bars = [bar(day, f"{9 + (15 + 15 * k) // 60:02d}:{(15 + 15 * k) % 60:02d}", 99 + k * 0.2, 99.5 + k * 0.2,
                98.8 + k * 0.2, 99.2 + k * 0.2) for k in range(6)]
    d = bot.position_details(p, 101.0, bars, now)
    assert abs(d["r_unreal"] - 0.5) < 1e-9 and d["held_min"] == 39
    assert d["time_check"] == "pending" and d["time_check_at"] == "11:00"
    assert d["trail_ema"] is not None and abs(d["stop_dist_pct"] - 2.97) < 0.01
    row = web.position_row({**p, **d, "symbol": "SBIN", "price": 101.0, "unrealised": 10.0})
    assert "+0.50R" in row and "ON TRACK" in row and "39 min" in row and "% away" in row
    d2 = bot.position_details({**p, "time_ok": True}, 99.0, bars, now)
    row2 = web.position_row({**p, **d2, "time_ok": True, "symbol": "SBIN", "price": 99.0, "unrealised": -10.0})
    assert "PASSED" in row2 and "-0.50R" in row2
    near = web.position_row({**p, **bot.position_details(p, 98.2, bars, now), "symbol": "SBIN", "price": 98.2,
                             "unrealised": -18.0})
    assert "NEAR STOP" in near and "AT RISK" in near


def test_status_json_carries_position_details(monkeypatch):
    hist, day = history()
    today = hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
                    bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
                    bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000)]
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: (today, today[-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST))
    b = bot.Bot()
    b.cycle()
    s = json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))
    pos = s["positions"][0]
    assert pos["symbol"] == "SBIN" and "r_unreal" in pos and "held_min" in pos
