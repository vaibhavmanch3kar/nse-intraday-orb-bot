"""The 30-trade edge check."""

import csv
import os

import config as C
import edge
import web


def rows(rs, risk=100.0):
    return [{"net": str(r * risk), "r": str(r)} for r in rs]


def test_too_early_until_30():
    c = edge.edge_check(rows([1.0, -1.0] * 10))
    assert c["verdict"] == "too_early" and c["n"] == 20
    assert edge.message(c)[0] == "Too early to judge"
    assert edge.edge_check([])["verdict"] == "too_early"


def test_positive_negative_and_unproven():
    assert edge.edge_check(rows([2.0, -1.0] * 15 + [0.5] * 10))["verdict"] == "positive"      # +0.5R, tight
    assert edge.edge_check(rows([-1.0, 0.2] * 20))["verdict"] == "negative"                   # -0.4R, tight
    c = edge.edge_check(rows([3.0, -1.0, -1.0] * 10))                                          # +0.33R, noisy
    assert c["verdict"] == "unproven" and c["r_low"] < 0 < c["r_high"]
    c = edge.edge_check(rows([2.0, -1.0, -1.0, -0.2] * 8))                                     # -0.05R, noisy
    assert c["verdict"] == "leaning_negative"
    assert "stop" in edge.message(c)[0].lower()


def test_stats():
    c = edge.edge_check(rows([2.0, -1.0, -1.0, 1.0]))
    assert c["win_rate"] == 0.5 and c["avg_r"] == 0.25
    assert c["avg_win"] == 150 and c["avg_loss"] == -100 and c["profit_factor"] == 1.5
    assert edge.edge_check([{"net": "x", "r": None}])["n"] == 0  # bad rows are skipped


def test_trades_page_shows_edge_cards():
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["closed", "symbol", "direction", "net", "r"])
        w.writeheader()
        for i in range(12):
            w.writerow({"closed": "2026-11-16 10:00", "symbol": "SBIN", "direction": "LONG",
                        "net": "50" if i % 2 else "-40", "r": "0.5" if i % 2 else "-0.4"})
    page = web.render_trades()
    assert "30-trade edge check: Paper trading" in page
    assert "12 of 30 trades logged" in page and "Too early to judge" in page
