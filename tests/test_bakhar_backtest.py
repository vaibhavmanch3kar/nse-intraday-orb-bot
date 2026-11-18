"""Bakhar's one-year backtest page: the bot's present rules over the candle archive, run as a dashboard job by the
one backtest engine. It writes only its own two files, says plainly whether the rules made or lost money, and a
cancelled run leaves the saved result alone."""

import datetime as dt
import os

import pytest

import bakhar
import buildinfo
import config as C
import jobs
import web
from conftest import IST
from test_yantra import market, rules  # noqa: F401  (rules is a fixture: the live bot's present rules)


@pytest.fixture
def data(rules, monkeypatch):  # noqa: F811
    monkeypatch.setattr(C, "WATCHLIST", ["AAA", "BBB"])
    return {"AAA": market(days=40, seed=3), "BBB": market(days=40, seed=5), "^NSEI": market(days=40, seed=9),
            "ZZZ": market(days=40, seed=11)}                       # ZZZ is not on the watchlist


NOW = dt.datetime(2026, 2, 9, 18, 0, tzinfo=IST)            # a Monday evening


def test_run_saves_its_own_two_files_and_matches_the_engine(isolated, data):
    import backtest
    import yantra_strategies as Y
    out = bakhar.build(now=NOW, data=data)
    assert sorted(os.listdir(str(isolated))) == ["bakhar_latest.json", "trades_bakhar.csv"]
    direct = backtest.simulate({s: data[s] for s in ("AAA", "BBB")}, C.PAPER_START_INR, False, {"^NSEI": data["^NSEI"]}, {},
                               trades_path=str(isolated / "direct.csv"), strat=Y.Orb())
    assert out["result"]["trades"] == direct["trades"] > 0 and out["result"]["net_pnl"] == direct["net_pnl"]
    assert open(bakhar.trades_path(), encoding="utf-8").read() == open(isolated / "direct.csv", encoding="utf-8").read()
    assert out["archive"]["stocks"] == ["AAA", "BBB"] and out["archive"]["days"] == 40
    assert out["range"]["r_low"] <= out["range"]["avg_r"] <= out["range"]["r_high"]
    assert len(web.trade_rows(bakhar.NAME)) == out["result"]["trades"]


def test_out_of_date_when_rules_settings_or_archive_change(isolated, data, monkeypatch):
    saved = bakhar.build(now=NOW, data=data)
    assert bakhar.status(saved, NOW)["stale"] is False and bakhar.status(None)["why"] == "not run yet"
    nxt = NOW + dt.timedelta(days=1)                                # Tuesday evening: that day is now final
    assert "is now in the archive" in bakhar.status(saved, nxt)["why"]
    monkeypatch.setattr(C, "VOLUME_MULT", 9.0)
    assert "settings changed (VOLUME_MULT)" in bakhar.status(saved, NOW)["why"]
    monkeypatch.setattr(C, "VOLUME_MULT", saved["settings"]["VOLUME_MULT"])
    monkeypatch.setattr(bakhar.yantra, "fingerprint", lambda: "different")
    assert "code has changed" in bakhar.status(saved, NOW)["why"]


def test_a_cancelled_run_keeps_the_saved_result(isolated, data):
    first = bakhar.build(now=NOW, data=data)

    class Stop(jobs.NoJob):
        def stop_if_cancelled(self):
            raise jobs.Cancelled()

    with pytest.raises(jobs.Cancelled):
        bakhar.build(job=Stop(), now=NOW + dt.timedelta(hours=1), data=data)
    assert bakhar.latest()["built"] == first["built"]
    assert sorted(os.listdir(str(isolated))) == ["bakhar_latest.json", "trades_bakhar.csv"]      # no half-written file


def test_an_empty_archive_is_an_error_not_an_empty_result(isolated):
    with pytest.raises(RuntimeError, match="no candles for the watchlist"):
        bakhar.build(now=NOW, data={})
    assert bakhar.latest() is None


def test_page_says_plainly_what_happened_and_only_offers_a_run(isolated, data, monkeypatch):
    first = web.render_backtest()
    assert "No run is saved yet" in first and "One day · session review" in first and "One year · backtest" in first
    out = bakhar.build(data=data)
    page = web.render_backtest()
    lost = out["result"]["net_pnl"] < 0
    assert ("these rules lost" if lost else "these rules made") in page and "A past result is not a forecast." in page
    assert "Simply holding the same stocks returned" in page and "Months that made money" in page
    assert "It cannot show what a real order book would have filled" in page
    body = page.split("<main", 1)[1]
    assert "method='post'" not in body.lower() and "action='/backtest'" in body
    monkeypatch.setattr(C, "VOLUME_MULT", 9.0)
    assert "This run is out of date:" in web.render_backtest()


def test_review_page_links_to_the_backtest_and_the_tab_stays_lit(isolated):
    import re
    page = web.render_backtest()
    tabs = re.search(r"<nav class='tabs'.*?</nav>", page, re.S).group(0)
    assert re.findall(r"<a class='on' href='([^']*)'", tabs) == ["/review"]
    assert "href='/backtest'" in web.bakhar_tabs("/review") and "class='on' href='/review'" in web.bakhar_tabs("/review")


def test_it_is_a_dashboard_file_with_a_job_and_a_route():
    assert "bakhar.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}
    src = open(web.__file__, encoding="utf-8").read()
    assert 'jobs.register("bakhar"' in src and 'u.path == "/backtest"' in src and '"bakhar": "/backtest"' in src
    code = open(bakhar.__file__, encoding="utf-8").read()
    for word in ("import broker", "kiteconnect", "place_order", "import bot"):
        assert word not in code
