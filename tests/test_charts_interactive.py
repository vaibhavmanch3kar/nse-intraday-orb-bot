"""Interactive charts (item 5): the numbers travel with a static fallback picture, the script is only
loaded where there is a chart, and the candle chart marks the entry and exit candles."""

import csv
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess

import pytest

import chartjs
import charts
import config as C
import web
import webui
from conftest import IST, bar


def chart_data(page):
    return [json.loads(html.unescape(m)) for m in re.findall(r"data-chart='([^']*)'", page)]


def test_container_escapes_and_keeps_a_fallback():
    box = chartjs.container({"type": "bars", "pts": [["<b>x</b>", 1.0, "it's"]]}, "<svg>fallback</svg>", "P&L")
    assert "<svg>fallback</svg>" in box and "<b>x</b>" not in box.split("data-chart=")[1].split("'>")[0]
    assert chart_data(box)[0]["pts"][0][0] == "<b>x</b>"
    assert "tabindex='0'" in box and "arrow keys" in box


def write_trades(rows):
    with open(os.path.join(C.DATA_DIR, "trades_paper.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["closed", "symbol", "direction", "net", "r", "reason"])
        w.writeheader()
        w.writerows(rows)


def test_trades_page_charts_carry_dates_tips_and_ranges():
    write_trades([{"closed": "2026-11-16 10:45:00+05:30", "symbol": "ITC", "direction": "LONG", "net": "129.51",
                   "r": "0.26", "reason": "time stop"},
                  {"closed": "2026-11-18 11:00:00+05:30", "symbol": "SBIN", "direction": "SHORT", "net": "-50",
                   "r": "-0.2", "reason": "stop"}])
    page = web.render_trades({"view": ["paper"]})
    data = chart_data(page)
    line = next(d for d in data if d["type"] == "line")
    bars = next(d for d in data if d["type"] == "bars")
    assert line["ranges"] and line["from0"] and line["dd"] and line["base"] == C.PAPER_START_INR
    assert line["pts"][0][:3] == ["16 Nov", 100129.51, "2026-11-16"] and "ITC LONG" in line["pts"][0][3]
    assert bars["pts"][1][1] == -50.0 and bars["pts"][1][0].startswith("#2")
    assert chartjs.JS.strip()[:40] in page and page.count("<svg") >= 2       # script + static fallbacks


def test_chart_script_only_where_needed():
    assert "AyodhyaCharts" not in webui.shell("x", "/scanner", "<p>no charts</p>")
    assert "AyodhyaCharts" in webui.shell("x", "/", chartjs.container({"type": "bars", "pts": []}, "", "x"))


def test_candle_chart_marks_entry_and_exit():
    day = dt.date(2026, 11, 16)
    bars = [bar(day, t, 100, 101, 99.5, 100.5) for t in ("09:15", "09:30", "09:45", "10:00")]
    trade = {"direction": "LONG", "entry": 100.6, "exit": 100.9, "stop": 99.5, "target": None,
             "opened": dt.datetime(2026, 11, 16, 9, 45, 30, tzinfo=IST),
             "closed": dt.datetime(2026, 11, 16, 10, 15, tzinfo=IST)}
    d = charts.journey_data(bars, trade, ema=[None, 100.1, 100.2, 100.3], or_range=(99.5, 101))
    assert d["type"] == "candles" and d["bars"][0][0] == "09:15" and d["band"] == [99.5, 101]
    assert [(m["k"], m["i"], m["upward"]) for m in d["marks"]] == [("entry", 2, True), ("exit", 3, False)]
    assert {l["n"] for l in d["lines"]} == {"stop", "entry"} and d["ema"][0] is None


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_scripts_are_valid_javascript(tmp_path):
    for name, code in (("charts", chartjs.JS), ("theme", webui.THEME_JS + webui.HEAD_JS), ("live", web.LIVE_JS),
                       ("trades", web.TRADES_JS)):
        p = tmp_path / f"{name}.js"
        p.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stderr)
