"""Profit and loss figures on the dashboard: an absolute value with a direction mark, never a +/- sign.

▲ green = profit, ▼ red = loss, ● grey = no change. The arrow carries the direction (never colour alone) and a
hidden word ("profit", "loss", "no change") is read out by screen readers. Display only: the helpers never change
a number, and the pages below are drawn from made-up files in the test's own data folder."""

import csv
import datetime as dt
import html
import json
import os
import re

import pytest

import accounts
import config as C
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MON_1030 = dt.datetime(2026, 11, 16, 10, 30, tzinfo=IST)


def visible(h):
    """What a sighted reader sees: the screen-reader words and the tags removed."""
    h = re.sub(r"<span class='sr'>[^<]*</span>", "", h)
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", h, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", h))


def number(text):
    """'▼ ₹1,234.50' -> -1234.5 (the arrow is the sign)."""
    sign = {"▲": 1, "▼": -1, "●": 0}[text[0]]
    return sign * float(re.sub(r"[^0-9.]", "", text[1:]))


# ---------------------------------------------------------------- the helpers
@pytest.mark.parametrize("fn,unit,v,seen,word,cls", [
    (web.pnl_inr, "₹", 8436.0, "▲ ₹8,436.00", "profit", "up"),
    (web.pnl_inr, "₹", -8436.0, "▼ ₹8,436.00", "loss", "dn"),
    (web.pnl_inr, "₹", 0.0, "● ₹0.00", "no change", "flat"),
    (web.pnl_pct, "%", 15.36, "▲ 15.36%", "profit", "up"),
    (web.pnl_pct, "%", -15.36, "▼ 15.36%", "loss", "dn"),
    (web.pnl_pct, "%", 0.0, "● 0.00%", "no change", "flat"),
    (web.pnl_r, "R", 0.23, "▲ 0.23R", "profit", "up"),
    (web.pnl_r, "R", -0.37, "▼ 0.37R", "loss", "dn"),
    (web.pnl_r, "R", 0.0, "● 0.00R", "no change", "flat"),
])
def test_profit_loss_and_zero_for_rupees_percent_and_r(fn, unit, v, seen, word, cls):
    h = fn(v)
    assert visible(h) == seen
    assert f"<span class='sr'>{word} </span>" in h                      # read out, not shown
    assert f"class='pnl {cls}'" in h                                    # colour from the theme's --up / --dn / --muted
    assert "aria-hidden='true'" in h                                    # the arrow itself is not read as a shape
    assert "-" not in visible(h) and "+" not in visible(h) and "−" not in visible(h)


def test_a_figure_that_rounds_to_zero_is_no_change_not_a_loss():
    assert visible(web.pnl_inr(-0.004)) == "● ₹0.00" and visible(web.pnl_pct(0.0049)) == "● 0.00%"
    assert visible(web.pnl_inr(-0.4, 0)) == "● ₹0" and visible(web.pnl_inr(-0.6, 0)) == "▼ ₹1"
    assert web.pnl_inr(None) == web.pnl_pct(None) == web.pnl_r(None) == "-"


def test_plain_text_form_for_titles_tooltips_and_charts():
    assert web.pnl_text(-12.5) == "▼ ₹12.50" and web.pnl_text(3.1, "pct") == "▲ 3.10%"
    assert web.pnl_text(0, "r") == "● 0.00R" and web.pnl_text(None) == "-"
    assert web.pnl_text(1234567.891, "inr", 0) == "▲ ₹1,234,568"


def test_the_css_classes_use_the_theme_colours():
    import webui
    assert ".up{color:var(--up)}" in webui.BASE_CSS and ".dn{color:var(--dn)}" in webui.BASE_CSS
    assert ".flat{color:var(--muted)}" in webui.BASE_CSS
    assert ".sr{position:absolute" in webui.BASE_CSS                   # the visually-hidden word


@pytest.mark.parametrize("v", [0.01, -0.01, 1.005, -99999.994, 12345.678, -0.5, 7.0, -1e6, 0.0])
def test_the_helpers_never_change_the_number(v):
    for fn, nd in ((web.pnl_inr, 2), (web.pnl_pct, 2), (web.pnl_r, 2), (web.pnl_inr, 0)):
        assert number(visible(fn(v, nd))) == pytest.approx(round(v, nd), abs=10 ** -nd)
    assert number(web.pnl_text(v)) == pytest.approx(round(v, 2), abs=0.01)
    assert isinstance(v, float)                                         # the caller's value is untouched


def test_old_names_still_work_with_their_old_arguments():
    # the Portfolio page calls _pct with two or three arguments: it must keep that signature (20 Nov 2026 crash)
    assert web._pct(12.345, 2) == "12.35%" and web._pct(5.0) == "5.0%"
    assert web._signed(-12.5) == "▼ ₹12.50" and web._signed(3, 0) == "▲ ₹3" and web._signed(0) == "● ₹0.00"
    assert web._pct_arrow(-0.3) == "▼ 0.30%" and web._pct_arrow(None) == "-"
    assert web._cls(-1) == "dn" and web._cls(2) == "up" and web._cls(0) == ""
    assert web._inr(-5.0) == "-₹5.00" and web._inr(5.0) == "₹5.00"     # a plain amount (balances) is unchanged
    assert visible(web._pl(-5.0)) == "▼ ₹5.00" and visible(web._money(-5.0)) == "▼ ₹5"


# ---------------------------------------------------------------- the pages
BARE_MINUS_RS = re.compile(r"[-−]\s*₹")
SIGNED_PCT = re.compile(r"(?<![\w.])[-−+]\d[\d,]*(\.\d+)?%")
SIGNED_RS = re.compile(r"\+\s*₹")


# signed numbers that are not a profit or loss keep their sign: a limit's threshold is a rule, not a result
NOT_PNL = [re.compile(r"close open positions at −\d+(\.\d+)?% in a day")]


def assert_no_signed_pnl(page, where):
    text = visible(page)
    for pat in NOT_PNL:
        text = pat.sub("", text)
    assert not BARE_MINUS_RS.search(text), (where, BARE_MINUS_RS.search(text) and
                                            text[max(0, BARE_MINUS_RS.search(text).start() - 60):][:120])
    assert not SIGNED_RS.search(text), where
    m = SIGNED_PCT.search(text)
    assert not m, (where, m and text[max(0, m.start() - 60):][:120])


@pytest.fixture
def losing_paper(monkeypatch):
    """A paper account that has lost money: booked loss, open loss, a losing position, losing trades."""
    def no_kite():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")
    monkeypatch.setattr(accounts, "_kite_client", no_kite)
    accounts.clear_zerodha_cache()
    with open(os.path.join(C.DATA_DIR, "state_paper.json"), "w", encoding="utf-8") as f:
        json.dump({"cash": 99000.0, "realized_pnl": -900.0, "charges_paid": 100.0, "day_pnl": -350.0,
                   "positions": {"ITC": {"direction": "LONG", "qty": 10, "entry": 300.0, "stop": 290.0,
                                         "last_price": 295.0}}}, f)
    status = {"mode": "paper", "equity": 98950.0, "day_pnl": -350.0, "trades_today": 2,
              "positions": [{"symbol": "ITC", "direction": "LONG", "qty": 10, "entry": 300.0, "price": 295.0,
                             "stop": 290.0, "best_price": 303.0, "unrealised": -50.0, "r_unreal": -0.5,
                             "held_min": 40, "stop_dist_pct": 1.7}]}
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f)
    with open(os.path.join(C.DATA_DIR, "trades_paper.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["opened", "closed", "symbol", "direction", "qty", "entry", "exit",
                                          "reason", "charges", "net", "r", "gross"])
        w.writeheader()
        for i, (net, r) in enumerate([(-500.0, -1.0), (250.0, 0.5), (-650.0, -1.3), (0.0, 0.0)]):
            w.writerow({"opened": f"2026-11-1{i + 2} 09:45:00+05:30", "closed": f"2026-11-1{i + 2} 11:00:00+05:30",
                        "symbol": "ITC", "direction": "LONG", "qty": "10", "entry": "300", "exit": "295",
                        "reason": "stop", "charges": "20", "net": str(net), "r": str(r), "gross": str(net + 20)})
    with open(os.path.join(C.DATA_DIR, "trades_manual_paper.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["closed", "symbol", "direction", "qty", "your_avg", "exit", "gross", "how"])
        w.writeheader()
        w.writerow({"closed": "2026-11-14 11:00", "symbol": "ITC", "direction": "LONG", "qty": "2",
                    "your_avg": "300", "exit": "295", "gross": "-10.0", "how": "with the bot's exit"})
    return status


def test_live_page_has_no_signed_pnl(losing_paper):
    page = web.render("", {"view": ["paper"]})
    assert "▼ ₹" in visible(page) and "<span class='sr'>loss </span>" in page
    assert "▼ 0.50R" in visible(page)                                     # the open position in R
    assert_no_signed_pnl(page, "live")


def test_trades_page_and_trade_log_have_no_signed_pnl(losing_paper):
    page = web.render_trades({"view": ["paper"]})
    text = visible(page)
    assert "▼ ₹650.00" in text and "▼ 1.30R" in text and "● ₹0.00" in text and "▲ ₹250.00" in text
    assert "▼ ₹10.00" in text                                             # your shares the bot closed
    assert_no_signed_pnl(page, "trades")


def test_scoreboard_card_has_no_signed_pnl(monkeypatch):
    import scoreboard
    sb = {"bot_pct": -1.234, "hold_pct": 2.5, "index_pct": 0.0, "bot_days": 3, "stocks": 2, "buy_cost_pct": 0.1,
          "few_days": True, "market_days": 4, "from": "2026-11-16", "to": "2026-11-19", "base_day": "2026-11-13",
          "rows": [{"day": "2026-11-16", "bot_pct": -1.234, "bot_ran": True, "hold_pct": 2.5, "index_pct": 0.0}]}
    monkeypatch.setattr(scoreboard, "build", lambda mode: sb)
    monkeypatch.setattr(scoreboard, "sentence", lambda s: "The bot is behind holding the watchlist.")
    card = web.scoreboard_card("paper")
    text = visible(card)
    assert "▼ 1.23%" in text and "▲ 2.50%" in text and "● 0.00%" in text
    assert "<span class='sr'>loss </span>" in card and "<span class='sr'>profit </span>" in card
    assert_no_signed_pnl(card, "scoreboard")


def test_kavach_page_has_no_signed_pnl(losing_paper):
    assert_no_signed_pnl(web.render_kavach(), "kavach")


def test_portfolio_page_has_no_signed_pnl(monkeypatch):
    import rajya
    alloc = {"total": 9000.0, "cash_equity": 5000.0, "holdings_value": 4000.0, "intraday_value": 998.0,
             "leveraged": False, "by_sleeve": [{"name": "delivery", "value": 4000.0, "pct": 44.4}], "by_sector": [],
             "rows": [{"symbol": "ITC", "direction": "LONG", "owner": "you", "sleeve": "delivery", "sector": "FMCG",
                       "qty": 10, "value": 4200.0, "pct": 46.7, "pnl": -120.0},
                      {"symbol": "KOTAKBANK", "direction": "LONG", "owner": "bot", "sleeve": "intraday",
                       "sector": "Banks", "qty": 2, "value": 998.0, "pct": 11.1, "pnl": 0.0}]}
    r = {"built": "2026-11-20T10:30:00+05:30", "allocation": alloc, "holdings_known": True, "warnings": [],
         "concentration": {"positions": 0},
         "budget": {"day_pnl": -75.5, "at_risk": 12.0, "at_risk_pct": 0.24, "base": 5000.0, "day_left": 24.5,
                    "day_limit": 100.0, "exposure_pct": 20.0, "exposure_limit_pct": 75.0, "open": 1, "open_limit": 3,
                    "worst_today": -87.5, "rows": [], "no_stop": []},
         "history": {"per": [], "pairs": [], "mix": None},
         "stress": [{"name": "NIFTY falls 10%", "what": "every holding falls 10%", "loss": 900.0, "pct": 10.0}],
         "record": {"trades": 3, "first": "2026-11-12", "gross": -40.0, "charges": 60.0, "net": -100.0,
                    "drawdown": 150.0, "below_high": 100.0, "today_trades": 1, "today_net": -30.0}}
    monkeypatch.setattr(rajya, "latest", lambda view: r)
    monkeypatch.setattr(rajya, "status", lambda *a, **k: {"stale": False, "in_progress": False, "why": ""})
    monkeypatch.setattr(web, "auto_start", lambda *a, **k: None)
    page = web.render_rajya({"view": ["paper"]})
    text = visible(page)
    assert "▼ ₹120.00" in text and "▼ ₹900.00" in text and "▼ 10.00%" in text and "▼ ₹100.00" in text
    # the rebalancing simulation's "cash would change by +₹x" is money moving, not a profit or loss: it keeps its sign
    assert_no_signed_pnl(page.split("id='rebalance'")[0], "portfolio")


def test_lab_page_has_no_signed_pnl():
    import safefile
    import yantra
    m = {"trades": 40, "win_rate": 0.25, "avg_r": -0.2, "r_low": -0.3, "r_high": -0.1, "profit_factor": 0.4,
         "net": -3000.0, "months": 5, "months_positive": 1}
    exp = {"id": "20261118-140000-orb", "at": "2026-11-18T14:00:00+05:30", "strategy": "orb", "what": "an idea",
           "period": ["2025-11-24", "2026-06-25"], "variants_tried": 1, "code": "abc123def456",
           "in_sample": {"orb(x=1)": m}, "walk_forward": m, "verdict": "FAIL", "why": ["no edge"]}
    safefile.write_json(os.path.join(yantra.lab_dir(), "experiments", exp["id"] + ".json"), exp)
    page = web.render_yantrashala()
    text = visible(page)
    assert "▼ 0.20R" in text and "▼ ₹3,000.00" in text
    assert_no_signed_pnl(page, "lab")


def test_chart_scripts_use_the_same_arrows():
    import chartjs
    import charts
    assert "'▲ +'" not in chartjs.JS and "signed(" not in chartjs.JS          # tooltips and P&L axis use pnl()
    assert "pnl(df)" in chartjs.JS and "pnl(p[1])" in chartjs.JS and "pnl(-m,0)" in chartjs.JS
    assert "▲" in chartjs.JS and "▼" in chartjs.JS and "●" in chartjs.JS
    svg = charts.pnl_bars([-120.0, 80.0])
    assert "-₹" not in svg and "▼ ₹120" in svg and "▲ ₹80" in svg
    import webui
    assert "p.pnl>=0?'+'" not in webui.ALERTS_JS and "+pnl(p.pnl)+" in webui.ALERTS_JS
