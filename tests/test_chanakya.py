"""Chanakya, the research assistant: it lists measured facts for and against a trade, shows the plan with its
costs, repeats Kavach's answer without softening it, and reports the past record with its sample size. It gives
no probabilities, cannot reach the broker, changes no account file, and the same inputs give the same answer."""

import datetime as dt
import json
import os
import re

import buildinfo
import chanakya
import config as C
import risk
import web
from conftest import IST, bar, history

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def next_weekday(d):
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def breakout(direction="LONG", vol=3000, days=22):
    """22 quiet sessions at 100, then a day whose second candle closes beyond the opening range."""
    bars, d = history(days)
    day = next_weekday(d)
    bars.append(bar(day, "09:15", 100.0, 100.4, 99.9, 100.2, 2000))        # opening range 99.9 to 100.4
    if direction == "LONG":
        bars.append(bar(day, "09:30", 100.2, 101.0, 100.2, 100.9, vol))
    elif direction == "SHORT":
        bars.append(bar(day, "09:30", 100.2, 100.2, 99.3, 99.4, vol))
    else:
        bars.append(bar(day, "09:30", 100.2, 100.3, 100.0, 100.1, vol))
    return bars, dt.datetime.combine(day, dt.time(9, 46), tzinfo=IST)


def account(tmp, equity=100000.0):
    return {"state": risk.State(os.path.join(str(tmp), "state_test.json"), equity), "prices": {}, "live": False,
            "source": "yahoo", "check_data": False}


def by_name(a):
    return {c["name"]: c for c in a["checks"]}


FIGS = {"trades": 30, "win_rate": 0.3, "avg_r": -0.3, "r_low": -0.5, "r_high": -0.1, "net": -900.0, "profit_factor": 0.5}
RULE = "Bot's rule on this candle"
KAVACH = "Kavach (risk engine)"


# ---------------------------------------------------------------- what one stock's assessment says
def test_breakout_lists_measured_checks_and_counts_them(isolated):
    bars, now = breakout()
    a = chanakya.assess("TESTCO", bars, None, account(isolated), now=now)
    c = by_name(a)
    assert a["direction"] == "LONG" and a["setup"] == "LONG: breakout on this candle"
    assert (a["or_low"], a["or_high"]) == (99.9, 100.4)
    assert c[RULE]["state"] == "for"
    assert c["Volume"]["state"] == "for" and "3.00x" in c["Volume"]["value"]
    assert c["VWAP"]["state"] == "for" and c["Short trend (15-min)"]["state"] == "for"
    assert c["Daily trend"]["state"] == "for" and "20-day average 100.00" in c["Daily trend"]["value"]
    assert c["Room to run"]["value"] == "no known level ahead"
    assert c["Time of day"]["state"] == "for"
    assert c["Index today"]["state"] == "n/a" and c["Sector today"]["state"] == "n/a"      # nothing to measure
    assert c["Past record"]["state"] == "n/a"
    counted = [x for x in a["checks"] if x["state"] != "n/a"]
    assert a["of"] == len(counted) and a["score"] == sum(x["state"] == "for" for x in counted)
    assert a["against"] == sum(x["state"] == "against" for x in counted)
    assert a["score"] + a["against"] <= a["of"] < len(a["checks"])          # "not available" is never counted


def test_weak_volume_and_late_candle_count_against(isolated):
    bars, now = breakout(vol=500)
    c = by_name(chanakya.assess("TESTCO", bars, None, account(isolated), now=now))
    assert c["Volume"]["state"] == "against" and c[RULE]["state"] == "neutral"
    bars, d = history(22)
    day = next_weekday(d)
    bars += [bar(day, "09:15", 100.0, 100.4, 99.9, 100.2, 2000), bar(day, "12:30", 100.2, 101.0, 100.2, 100.9, 3000)]
    late = by_name(chanakya.assess("TESTCO", bars, None, account(isolated),
                                   now=dt.datetime.combine(day, dt.time(12, 46), tzinfo=IST)))
    assert late["Time of day"]["state"] == "against"


def test_short_side_mirrors_the_long_side(isolated):
    bars, now = breakout("SHORT")
    a = chanakya.assess("TESTCO", bars, None, account(isolated), now=now)
    c = by_name(a)
    assert a["direction"] == "SHORT" and a["plan"]["stop"] == 100.4 and a["plan"]["reference_2r"] < a["plan"]["entry"]
    assert c["VWAP"]["state"] == "for" and c["Daily trend"]["state"] == "for"
    assert a["summary"].startswith("TESTCO is below its opening range")


def test_index_and_sector_are_measured_when_given(isolated):
    bars, now = breakout()
    index = [dict(b, c=b["c"] * 0.99) if b["t"].date() == now.date() and b["t"].minute == 30 else b for b in bars]
    c = by_name(chanakya.assess("TESTCO", bars, index, account(isolated), peers={"A": -0.4, "B": -0.2}, now=now))
    assert c["Index today"]["state"] == "against" and "%" in c["Index today"]["value"]
    assert c["Sector today"]["state"] == "against" and "2 peer(s) average -0.30%" in c["Sector today"]["value"]


def test_inside_the_range_and_before_the_range_there_is_nothing_to_score(isolated):
    bars, now = breakout(None)
    a = chanakya.assess("TESTCO", bars, None, account(isolated), now=now)
    assert a["direction"] is None and a["setup"] == "inside the opening range" and a["checks"] == []
    assert a["plan"] is None and a["kavach"] is None and "No setup to assess" in a["summary"]
    first = chanakya.assess("TESTCO", bars[:-1], None, account(isolated), now=now)
    assert first["direction"] is None and first["checks"] == [] and first["score"] == 0
    assert chanakya.assess("TESTCO", [], None, account(isolated))["setup"] == "no data"


# ---------------------------------------------------------------- the plan and Kavach
def test_plan_carries_size_risk_and_costs_and_kavach_allows_it(isolated):
    bars, now = breakout()
    a = chanakya.assess("TESTCO", bars, None, account(isolated), now=now)
    p, k = a["plan"], a["kavach"]
    assert k["ok"] and k["qty"] == p["qty"] > 0
    assert p["entry"] == 100.9 and p["stop"] == 99.9 and abs(p["risk_per_share"] - 1.0) < 1e-9
    assert abs(p["reference_2r"] - 102.9) < 1e-9 and "none" in p["target"]
    assert p["value"] <= 25000.01 and abs(p["risk"] - p["qty"] * 1.0) < 1e-6     # the 25% per-stock limit binds
    assert p["charges"] > 0 and p["slippage"] > 0
    assert abs(p["cost_pct_of_risk"] - (p["charges"] + p["slippage"]) / p["risk"] * 100) < 1e-9
    c = by_name(a)
    assert c[KAVACH]["state"] == "for" and "Costs" in c
    assert f"Kavach would allow {k['qty']} share(s)." in a["summary"]


def test_a_kavach_refusal_is_shown_as_it_is_and_counts_against(isolated):
    bars, now = breakout()
    a = chanakya.assess("TESTCO", bars, None, account(isolated, equity=300.0), now=now)
    k = a["kavach"]
    assert k["ok"] is False and k["qty"] == 0 and k["check"] == "size" and k["reason"]
    c = by_name(a)
    assert c[KAVACH]["state"] == "against" and k["reason"] in c[KAVACH]["value"]
    assert c["Costs"]["state"] == "n/a" and "qty" not in a["plan"]
    assert f"Kavach would refuse it: {k['reason']}." in a["summary"]
    full = chanakya.assess("TESTCO", bars, None, account(isolated), now=now)
    assert a["score"] < full["score"]                      # many agreeing checks never turn a refusal into a yes


def test_account_limits_reach_chanakya_through_kavach(isolated, monkeypatch):
    bars, now = breakout()
    acct = account(isolated)
    monkeypatch.setattr(type(acct["state"]), "can_enter", lambda self, s, today, prices=None: (False, "daily loss limit hit"))
    a = chanakya.assess("TESTCO", bars, None, acct, now=now)
    assert a["kavach"] == {"ok": False, "qty": 0, "reason": "daily loss limit hit", "check": "account"}
    assert a["plan"]["qty"] > 0                            # the plan still shows what the size would have been


# ---------------------------------------------------------------- the past record
def test_past_record_needs_twenty_trades_and_a_range_clear_of_zero(isolated):
    bars, now = breakout()

    def run(figs):
        a = chanakya.assess("TESTCO", bars, None, account(isolated), {"TESTCO|LONG": figs, "TESTCO|SHORT": FIGS}, now=now)
        return by_name(a)["Past record"]["state"], a["summary"]

    state, text = run(FIGS)
    assert state == "against" and "The record is against it: 30 past trades averaged -0.30R after charges." in text
    state, text = run(dict(FIGS, trades=10))
    assert state == "neutral" and "The record proves nothing either way (10 past trades, average -0.30R)." in text
    state, text = run(dict(FIGS, avg_r=0.1, r_low=-0.2, r_high=0.4))
    assert state == "neutral" and "proves nothing either way" in text         # the range includes zero
    state, text = run(dict(FIGS, avg_r=0.3, r_low=0.1, r_high=0.5))
    assert state == "for" and "The record supports it" in text
    a = chanakya.assess("TESTCO", bars, None, account(isolated), {"OTHER|LONG": FIGS}, now=now)
    assert by_name(a)["Past record"]["state"] == "n/a" and "There is no past record" in a["summary"]


# ---------------------------------------------------------------- what Chanakya must never do
def test_same_inputs_give_the_same_answer_and_no_file_is_written(isolated):
    bars, now = breakout()
    one = chanakya.assess("TESTCO", bars, None, account(isolated), {"TESTCO|LONG": FIGS}, {"A": 0.3}, now)
    two = chanakya.assess("TESTCO", bars, None, account(isolated), {"TESTCO|LONG": FIGS}, {"A": 0.3}, now)
    assert json.dumps(one, default=str, sort_keys=True) == json.dumps(two, default=str, sort_keys=True)
    assert os.listdir(str(isolated)) == []                 # assessing saved nothing: not the account, not a result


def test_wording_gives_no_probability_promise_or_advice(isolated):
    bars, now = breakout()
    good = dict(FIGS, avg_r=0.3, r_low=0.1, r_high=0.5)
    texts = []
    for eq in (100000.0, 300.0):
        a = chanakya.assess("TESTCO", bars, None, account(isolated, eq), {"TESTCO|LONG": good}, now=now)
        texts += [a["summary"], a["setup"]] + [str(c["value"]) + " " + c["why"] for c in a["checks"]]
    blob = " ".join(texts).lower()
    for word in ("probab", "chance", "likely", "will rise", "will fall", "guarantee", "recommend", "should buy",
                 "should sell", "sure", "profit"):
        assert word not in blob, word
    assert not re.search(r"\d+\s*% (win|success)", blob)


def test_chanakya_has_no_way_to_the_broker_and_is_not_part_of_the_bot():
    src = open(os.path.join(ROOT, "chanakya.py"), encoding="utf-8").read()
    imports = set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", src, re.M))
    assert not imports & {"broker", "kiteconnect", "accounts", "orders", "bot", "livemode", "kite_client", "auth"}
    for word in ("place_order", "cancel_order", "modify_order", "LiveBroker", ".save("):
        assert word not in src, word
    assert not imports & {"anthropic", "openai", "requests", "urllib"}        # no language model, no network of its own
    assert "chanakya.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}
    assert "chanakya.py" in {os.path.basename(p) for p in buildinfo.dashboard_files()}


def test_ranking_puts_setups_first_then_most_agreeing_then_fewest_disagreeing():
    rows = [{"symbol": "Q", "direction": None, "score": 0, "against": 0},
            {"symbol": "B", "direction": "LONG", "score": 5, "against": 3},
            {"symbol": "A", "direction": "SHORT", "score": 5, "against": 1},
            {"symbol": "C", "direction": "LONG", "score": 9, "against": 2},
            {"symbol": "D", "direction": None, "score": 0, "against": 0}]
    assert [r["symbol"] for r in chanakya.rank(rows)] == ["C", "A", "B", "D", "Q"]


# ---------------------------------------------------------------- the build and its freshness
def fake_fetch(data):
    def fetch(symbols, job=None):
        return {s: data.get(s, ([], "no data")) for s in symbols}
    return fetch


RECORD = {"ALL": FIGS, "period": ["2025-11-17", "2026-11-17"], "AAA|LONG": FIGS}


def test_build_saves_a_ranking_reads_the_account_and_never_writes_it(isolated, monkeypatch):
    monkeypatch.setattr(C, "WATCHLIST", ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(chanakya, "history", lambda job=None: RECORD)
    up, now = breakout()
    flat, _ = breakout(None)
    with open(os.path.join(str(isolated), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"mode": "paper", "positions": []}, fh)
    res = chanakya.build(now=now, fetch=fake_fetch({"AAA": (up, None), "BBB": (flat, None)}))
    assert [r["symbol"] for r in res["rows"]] == ["AAA", "BBB"] and res["errors"] == {"CCC": "no data"}
    assert res["mode"] == "paper" and res["equity"] == C.PAPER_START_INR and res["record"]["ALL"]["trades"] == 30
    assert res["rows"][0]["history"] == FIGS and res["as_of"].endswith("09:45+05:30")
    saved = chanakya.latest()
    assert saved["rows"][0]["symbol"] == "AAA" and saved["settings"]["WATCHLIST"] == ["AAA", "BBB", "CCC"]
    assert sorted(os.listdir(str(isolated))) == ["chanakya_latest.json", "status.json"]      # no state file created
    assert chanakya.status(saved, now)["stale"] is False
    later = chanakya.status(saved, now + dt.timedelta(minutes=17))
    assert later["stale"] and "newer candle" in later["why"]
    monkeypatch.setattr(C, "VOLUME_MULT", 2.0)
    assert "settings changed (VOLUME_MULT)" in chanakya.status(saved, now)["why"]
    assert chanakya.status(None)["stale"]


def test_build_still_works_when_the_record_cannot_be_worked_out(isolated, monkeypatch):
    monkeypatch.setattr(C, "WATCHLIST", ["AAA"])

    def broken(job=None):
        raise ValueError("archive unreadable")

    monkeypatch.setattr(chanakya, "history", broken)
    up, now = breakout()
    res = chanakya.build(now=now, fetch=fake_fetch({"AAA": (up, None)}))
    assert res["record"]["error"] == "ValueError: archive unreadable" and res["rows"][0]["history"] is None
    assert "could not be worked out" in web.render_chanakya()


def test_a_cancelled_build_saves_nothing(isolated, monkeypatch):
    import jobs
    monkeypatch.setattr(C, "WATCHLIST", ["AAA"])
    monkeypatch.setattr(chanakya, "history", lambda job=None: {})

    class Stop(jobs.NoJob):
        def stop_if_cancelled(self):
            raise jobs.Cancelled()

    up, now = breakout()
    try:
        chanakya.build(job=Stop(), now=now, fetch=fake_fetch({"AAA": (up, None)}))
        raise AssertionError("the build should have stopped")
    except jobs.Cancelled:
        pass
    assert chanakya.latest() is None


# ---------------------------------------------------------------- the page
def test_page_before_the_first_build_and_after(isolated, monkeypatch):
    first = web.render_chanakya()
    assert "The first assessment appears here" in first and "not a probability and not advice" in first
    monkeypatch.setattr(C, "WATCHLIST", ["AAA", "BBB", "CCC"])
    monkeypatch.setattr(chanakya, "history", lambda job=None: dict(RECORD, **{"AAA|LONG": dict(FIGS, trades=10)}))
    up, now = breakout()
    flat, _ = breakout(None)
    chanakya.build(now=now, fetch=fake_fetch({"AAA": (up, None), "BBB": (flat, None)}))
    page = web.render_chanakya()
    assert "Ranking" in page and "Chanakya cannot place an order and cannot overrule Kavach" in page
    assert "Over that year these rules lost money." in page and "30 trades from 2025-11-17 to 2026-11-17" in page
    assert "id='c-AAA'" in page and "id='c-BBB'" not in page and "No setup right now" in page
    assert "No data for 1 stock(s): CCC" in page
    assert "10 trades, -0.30R" in page and "The plan the bot would use" in page
    assert "A stop limits the planned loss; it cannot guarantee" in page
    body = page.split("<main", 1)[1]
    assert body.count("<form") == 1 and 'action="/chanakya"' in body          # only Rebuild: nothing that trades


def test_page_shows_a_refusal_in_the_ranking(isolated, monkeypatch):
    monkeypatch.setattr(C, "WATCHLIST", ["AAA"])
    monkeypatch.setattr(chanakya, "history", lambda job=None: {})
    monkeypatch.setattr(C, "PAPER_START_INR", 300.0)
    up, now = breakout()
    res = chanakya.build(now=now, fetch=fake_fetch({"AAA": (up, None)}))
    assert res["rows"][0]["kavach"]["ok"] is False
    page = web.render_chanakya()
    ranking = page.split("Ranking", 1)[1].split("</table>", 1)[0]
    assert ">refused<" in ranking and "allowed," not in ranking


def test_routes_job_and_links_exist():
    src = open(web.__file__, encoding="utf-8").read()
    assert 'u.path == "/chanakya"' in src and 'jobs.register("chanakya"' in src
    assert '"chanakya": "/chanakya"' in src                 # Cancel returns to the page
    assert "/chanakya" in web.live_limits({})
