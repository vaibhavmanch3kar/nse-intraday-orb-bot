"""Trades page (item 6): key figures on top, tabs (Overview, Journal, Trade log, Backtest), filters,
each account's own trades only."""

import csv
import os

import config as C
import journal
import web


def write(name, rows, cols=("closed", "symbol", "direction", "qty", "entry", "exit", "reason", "charges", "net", "r")):
    with open(os.path.join(C.DATA_DIR, name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cols))
        w.writeheader()
        w.writerows(rows)


T1 = {"closed": "2026-11-16 10:45:00+05:30", "symbol": "ITC", "direction": "LONG", "qty": "62", "entry": "291.9",
      "exit": "294.3", "reason": "time stop", "charges": "19.29", "net": "129.51", "r": "0.26"}
T2 = {"closed": "2026-11-17 11:00:00+05:30", "symbol": "SBIN", "direction": "SHORT", "qty": "10", "entry": "800",
      "exit": "805", "reason": "stop", "charges": "20", "net": "-70", "r": "-1"}


def test_tabs_kpis_and_filters():
    write("trades_paper.csv", [T1, T2])
    write("backtest_trades.csv", [dict(T1, opened="2026-11-16 09:45:00+05:30")],
          cols=("opened",) + tuple(T1))
    page = web.render_trades({"view": ["paper"]})
    for pid in ("overview", "journal", "log", "backtest"):
        assert f"data-tab='{pid}'" in page and f"id='{pid}'" in page
    assert "Win rate" in page and "50%" in page and "Max drawdown" in page and "+0.26R" not in page.split("ptabs")[0]
    assert "data-sym='SBIN' data-side='SHORT' data-res='lost'" in page and "data-res='won'" in page
    assert "<select data-f='sym'>" in page and "<option value='ITC'>ITC</option>" in page
    assert "/journey?src=paper&amp;i=1" in page and "/journey?src=backtest&amp;i=0" in page
    assert "class='rt'" in page                                      # rows become cards on a phone
    assert "30-trade edge check: Paper trading" in page


def test_daily_pnl_chart_from_the_journal():
    write("trades_paper.csv", [T1])
    journal.backfill("paper", today="2026-11-18")
    page = web.render_trades({"view": ["paper"]})
    assert "Net P&amp;L per trading day" in page


def test_zerodha_view_shows_only_live_trades():
    write("trades_paper.csv", [T1])
    page = web.render_trades({"view": ["zerodha"]})
    assert "data-sym='ITC'" not in page and "No real-money trades" in page
    assert "card acct zerodha" in page and "LIVE · REAL ZERODHA ACCOUNT" in page


def test_journal_tab_links_come_back_to_the_journal_tab():
    write("trades_paper.csv", [T1])
    page = web.render_trades({"view": ["paper"], "period": ["week"]})
    assert "period=month#journal" in page and "id='jcard'" in page
