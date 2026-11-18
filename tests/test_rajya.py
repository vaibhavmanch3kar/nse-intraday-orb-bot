"""Rajya, the portfolio view: it measures what is in the account (cash, the bot's intraday sleeve, delivery
holdings), reads Zerodha with read-only calls only, changes no file of the bot, places nothing, and its
rebalancing table is a simulation."""

import datetime as dt
import json
import math
import os
import re

import pytest

import accounts
import buildinfo
import config as C
import rajya
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = dt.datetime(2026, 11, 18, 13, 0, tzinfo=IST)


def closes(start, daily_pcts):
    """[(day, close)] from a list of daily % changes."""
    out, price, d = [("2026-01-01", start)], start, dt.date(2026, 1, 1)
    for p in daily_pcts:
        d += dt.timedelta(days=1)
        price *= 1 + p / 100
        out.append((str(d), price))
    return out


WAVE = [1.0, -0.5, 0.8, -1.2, 0.3, 0.6, -0.9, 1.1, -0.4, 0.2] * 4          # 40 days


class FakeKite:
    """Records every call. Anything that is not a read is not defined, so calling it would raise."""

    def __init__(self, holdings=(), positions=(), opening=10000.0, payin=0.0, fail_holdings=None):
        self.calls, self._h, self._p, self._open, self._payin, self._fail = [], list(holdings), list(positions), opening, payin, fail_holdings

    def margins(self, seg):
        self.calls.append("margins")
        return {"net": self._open + self._payin, "available": {"opening_balance": self._open, "live_balance": self._open,
                                                                "intraday_payin": self._payin, "collateral": 0},
                "utilised": {"debits": 0}}

    def positions(self):
        self.calls.append("positions")
        return {"net": self._p}

    def holdings(self):
        self.calls.append("holdings")
        if self._fail:
            raise self._fail
        return self._h


def holding(sym, qty, avg, last, t1=0):
    return {"tradingsymbol": sym, "quantity": qty, "t1_quantity": t1, "average_price": avg, "last_price": last}


def position(sym, qty, avg, last, product="MIS"):
    return {"tradingsymbol": sym, "exchange": "NSE", "product": product, "quantity": qty, "average_price": avg,
            "last_price": last, "realised": 0.0, "unrealised": (last - avg) * qty, "pnl": (last - avg) * qty}


@pytest.fixture(autouse=True)
def fresh_cache():
    accounts.clear_zerodha_cache()
    yield
    accounts.clear_zerodha_cache()


def live_state(tmp, cash=5000.0, **positions):
    with open(os.path.join(str(tmp), "state_live.json"), "w", encoding="utf-8") as fh:
        json.dump({"cash": cash, "positions": positions}, fh)


# ---------------------------------------------------------------- reading the account
def test_zerodha_view_makes_only_three_read_calls_and_writes_nothing(isolated):
    kite = FakeKite(holdings=[holding("ITC", 10, 400.0, 420.0, t1=5)], positions=[position("KOTAKBANK", 2, 500.0, 499.0)])
    live_state(isolated, cash=5000.0, KOTAKBANK={"direction": "LONG", "qty": 2, "entry": 500.0, "stop": 494.0})
    g = rajya.gather("zerodha", NOW, kite)
    assert kite.calls == ["margins", "positions", "holdings"]
    assert sorted(os.listdir(str(isolated))) == ["state_live.json"]
    by = {i["symbol"]: i for i in g["items"]}
    assert by["KOTAKBANK"]["sleeve"] == "intraday" and by["KOTAKBANK"]["owner"] == "bot" and by["KOTAKBANK"]["stop"] == 494.0
    assert by["ITC"]["sleeve"] == "delivery" and by["ITC"]["qty"] == 15 and by["ITC"]["owner"] == "you" and by["ITC"]["stop"] is None
    assert by["ITC"]["value"] == 6300.0 and by["ITC"]["pnl"] == 300.0
    assert g["cash_equity"] == 10000.0 - 2.0 and g["bot_equity"] == 5000.0 - 2.0 and g["holdings_known"]


def test_the_holdings_reader_can_reach_nothing_but_holdings():
    class Kite:
        def holdings(self):
            return ["h"]

        def place_order(self, **kw):
            raise AssertionError("an order was attempted")

    h = rajya.HoldingsOnly(Kite())
    assert h.holdings() == ["h"]
    for name in ("place_order", "cancel_order", "modify_order", "_kite", "positions"):
        assert not hasattr(h, name), name


def test_money_added_today_is_counted_but_not_in_the_bots_budget(isolated):
    kite = FakeKite(positions=[position("KOTAKBANK", 2, 500.0, 500.0)], opening=4870.0, payin=6000.0)
    live_state(isolated, cash=4870.0, KOTAKBANK={"direction": "LONG", "qty": 2, "entry": 500.0, "stop": 494.0})
    g = rajya.gather("zerodha", NOW, kite)
    assert g["cash_equity"] == 10870.0 and g["bot_equity"] == 4870.0
    assert any("added today is counted here" in w for w in g["warnings"])
    res = rajya.measure(g, {}, NOW)
    assert res["budget"]["base"] == 4870.0 and abs(res["budget"]["day_limit"] - 4870.0 * C.MAX_DAILY_LOSS_PCT / 100) < 1e-9


def test_a_position_the_bot_does_not_know_is_marked_and_has_no_stop(isolated):
    kite = FakeKite(positions=[position("SBIN", -3, 800.0, 790.0), position("TCS", 1, 3000.0, 3100.0, product="CNC"),
                               position("INFY", 0, 0.0, 1500.0)])
    g = rajya.gather("zerodha", NOW, kite)
    by = {i["symbol"]: i for i in g["items"]}
    assert set(by) == {"SBIN", "TCS"}                                   # a closed position (qty 0) is not listed
    assert by["SBIN"]["owner"] == "you" and by["SBIN"]["direction"] == "SHORT" and by["SBIN"]["pnl"] == 30.0
    assert by["SBIN"]["exposure"] == -2370.0 and by["TCS"]["sleeve"] == "delivery"
    assert rajya.risk_budget(g["items"], g["bot_equity"], 0.0)["no_stop"] == ["SBIN", "TCS"]


def test_unreadable_holdings_are_said_plainly_and_the_rest_is_still_shown(isolated):
    kite = FakeKite(positions=[position("SBIN", 1, 800.0, 800.0)], fail_holdings=RuntimeError("boom"))
    g = rajya.gather("zerodha", NOW, kite)
    assert g["holdings_known"] is False and [i["symbol"] for i in g["items"]] == ["SBIN"]
    assert any("delivery holdings could not be read" in w for w in g["warnings"])


def test_not_logged_in_gives_an_error_not_an_empty_account(isolated, monkeypatch):
    def no_login():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", no_login)
    res = rajya.build("zerodha", now=NOW)
    assert res["error"] == "Not logged in to Zerodha today." and res["login_needed"] and "allocation" not in res
    page = web.render_rajya({"view": ["zerodha"]})
    assert "The account could not be read." in page and "Not logged in to Zerodha today." in page


def test_paper_view_reads_the_paper_file_and_never_touches_zerodha(isolated, monkeypatch):
    monkeypatch.setattr(accounts, "_kite_client", lambda: (_ for _ in ()).throw(AssertionError("Zerodha was contacted")))
    assert rajya.gather("paper", NOW)["warnings"] == ["No paper trades yet: the account is at its starting balance."]
    with open(os.path.join(str(isolated), "state_paper.json"), "w", encoding="utf-8") as fh:
        json.dump({"cash": 99000.0, "opening_balance": 100000.0, "day_pnl": -50.0, "positions": {
            "SBIN": {"direction": "SHORT", "qty": 10, "entry": 800.0, "last_price": 795.0, "stop": 806.0}}}, fh)
    g = rajya.gather("paper", NOW)
    assert g["cash_equity"] == 99050.0 and g["bot_equity"] == 99050.0 and g["day_pnl"] == 0.0
    assert g["items"][0]["direction"] == "SHORT" and g["items"][0]["stop"] == 806.0 and g["warnings"] == []
    assert sorted(os.listdir(str(isolated))) == ["state_paper.json"]


# ---------------------------------------------------------------- the arithmetic
def test_allocation_adds_up_and_names_the_sleeves():
    items = [rajya.item("ITC", "delivery", 10, 400.0, 500.0, owner="you"), rajya.item("HDFCBANK", "delivery", 2, 900.0, 1000.0, owner="you"),
             rajya.item("KOTAKBANK", "intraday", 2, 500.0, 500.0, 494.0)]
    a = rajya.allocation(items, 3000.0)
    assert a["total"] == 10000.0 and a["holdings_value"] == 7000.0 and a["intraday_value"] == 1000.0 and a["free_cash"] == 2000.0
    assert [r["symbol"] for r in a["rows"]] == ["ITC", "HDFCBANK", "KOTAKBANK"] and a["rows"][0]["pct"] == 50.0
    assert {s["name"]: s["pct"] for s in a["by_sector"]} == {"Other": 50.0, "Banks": 30.0}
    assert abs(sum(s["pct"] for s in a["by_sleeve"]) - 100.0) < 1e-9 and not a["leveraged"]
    lev = rajya.allocation([rajya.item("KOTAKBANK", "intraday", 20, 500.0, 500.0)], 4000.0)
    assert lev["leveraged"] and lev["free_cash"] == -6000.0 and lev["by_sleeve"][2]["value"] == 0.0


def test_concentration_counts_equal_sized_positions_and_flags_the_limit():
    items = [rajya.item(s, "delivery", 1, 100.0, v) for s, v in (("A", 6000.0), ("B", 2000.0), ("C", 1000.0), ("D", 1000.0))]
    c = rajya.concentration(rajya.allocation(items, 0.0))
    assert c["largest"] == "A" and c["largest_pct"] == 60.0 and c["top3_pct"] == 90.0 and c["over_limit"] == ["A"]
    assert abs(c["effective"] - 1 / (0.36 + 0.04 + 0.01 + 0.01)) < 1e-9
    even = rajya.concentration(rajya.allocation([rajya.item(s, "delivery", 1, 1.0, 100.0) for s in "ABCD"], 0.0))
    assert abs(even["effective"] - 4.0) < 1e-9 and even["over_limit"] == []
    assert rajya.concentration(rajya.allocation([], 500.0)) == {"positions": 0}


def test_correlation_beta_and_too_little_history():
    a = rajya.daily_returns(closes(100.0, WAVE))
    same = rajya.daily_returns(closes(50.0, WAVE))
    opposite = rajya.daily_returns(closes(100.0, [-x for x in WAVE]))
    double = rajya.daily_returns(closes(100.0, [2 * x for x in WAVE]))
    assert abs(rajya.correlation(a, same) - 1.0) < 1e-9 and rajya.correlation(a, opposite) < -0.99
    assert abs(rajya.beta(double, a) - 2.0) < 0.02 and abs(rajya.beta(a, a) - 1.0) < 1e-9
    short = rajya.daily_returns(closes(100.0, WAVE[:10]))
    assert rajya.correlation(a, short) is None and rajya.beta(short, a) is None
    flat = {d: 0.0 for d in a}
    assert rajya.correlation(a, flat) is None and rajya.beta(a, flat) is None
    assert rajya.worst_5pct([-5.0] + [1.0] * 19) == 5.0 and rajya.worst_5pct([-9.0, -5.0] + [1.0] * 38) == 5.0


def test_history_measures_replay_todays_sizes_over_past_days():
    items = [rajya.item("A", "delivery", 10, 100.0, 100.0), rajya.item("B", "delivery", 10, 100.0, 100.0),
             rajya.item("NEW", "delivery", 1, 10.0, 10.0)]
    cl = {"A": closes(100.0, WAVE), "B": closes(80.0, WAVE), C.INDEX_DEFAULT: closes(20000.0, [x / 2 for x in WAVE])}
    h = rajya.history_measures(items, 2010.0, cl)
    assert h["not_covered"] == ["NEW"] and [p["covered"] for p in h["per"]] == [True, True, False]
    assert h["pairs"] == [{"a": "A", "b": "B", "corr": pytest.approx(1.0), "high": True}] and h["avg_corr"] == pytest.approx(1.0)
    assert h["per"][0]["beta"] == pytest.approx(2.0, abs=0.02) and h["per"][0]["days"] == 40
    m = h["mix"]
    assert m["days"] == 40 and m["worst_move"] == pytest.approx(2000.0 * -0.012) and m["worst_day"] == "2026-01-05"
    assert m["loss_1in20"] == pytest.approx(24.0) and m["drawdown"] > 0
    assert m["vol_year_pct"] == pytest.approx(m["vol_day_pct"] * math.sqrt(252))
    short_side = rajya.history_measures([rajya.item("A", "intraday", -10, 100.0, 100.0)], 1000.0, cl)
    assert short_side["mix"]["worst_move"] == pytest.approx(1000.0 * -0.011)          # a short loses on the best day
    assert rajya.history_measures(items[2:], 10.0, cl)["mix"] is None and rajya.history_measures([], 0.0, {})["per"] == []


def test_risk_budget_uses_kavachs_limits_and_never_counts_a_locked_profit_as_risk():
    items = [rajya.item("KOTAKBANK", "intraday", 2, 500.0, 500.0, 494.0), rajya.item("SBIN", "intraday", -5, 800.0, 790.0, 795.0),
             rajya.item("ITC", "delivery", 10, 400.0, 420.0)]
    b = rajya.risk_budget(items, 5000.0, -30.0)
    assert b["at_risk"] == 12.0 + 25.0 and b["no_stop"] == ["ITC"] and b["open"] == 2 and b["open_limit"] == C.MAX_OPEN_POSITIONS
    assert b["day_limit"] == 5000.0 * C.MAX_DAILY_LOSS_PCT / 100 and b["day_left"] == b["day_limit"] - 30.0
    assert b["exposure"] == 1000.0 + 3950.0 and b["exposure_pct"] == pytest.approx(99.0) and b["exposure_limit_pct"] == C.MAX_EXPOSURE_PCT
    assert b["exit_charges"] > 0 and b["worst_today"] == pytest.approx(-30.0 - 37.0 - b["exit_charges"])
    won = rajya.risk_budget([rajya.item("X", "intraday", 2, 500.0, 520.0, 510.0)], 5000.0, 40.0)
    assert won["at_risk"] == 20.0 and won["day_left"] == won["day_limit"]                # a gain adds no room
    trailed = rajya.risk_budget([rajya.item("X", "intraday", -2, 500.0, 480.0, 475.0)], 5000.0, 0.0)
    assert trailed["at_risk"] == 0.0                                                       # stop beyond the price


def test_stress_rows_are_plain_arithmetic_on_stated_assumptions():
    items = [rajya.item("A", "delivery", 10, 100.0, 100.0), rajya.item("K", "intraday", 2, 500.0, 500.0, 494.0)]
    cl = {"A": closes(100.0, WAVE), C.INDEX_DEFAULT: closes(20000.0, [x / 2 for x in WAVE])}
    hist = rajya.history_measures(items, 6000.0, cl)
    budget = rajya.risk_budget(items, 5000.0, 0.0)
    s = {x["name"]: x for x in rajya.stress(items, 6000.0, hist, budget)}
    assert s["Every stop is hit"]["loss"] == pytest.approx(12.0 + budget["exit_charges"])
    beta_a = hist["per"][0]["beta"]
    assert s["NIFTY falls 10%"]["loss"] == pytest.approx(1000.0 * beta_a * 0.10 + 1000.0 * 1.0 * 0.10)
    assert "K: no price history, assumed to move 1 for 1" in s["NIFTY falls 10%"]["what"]
    assert s["NIFTY falls 20%"]["loss"] == pytest.approx(2 * s["NIFTY falls 10%"]["loss"])
    assert s["A alone falls 20%"]["loss"] == 200.0 and s["A alone falls 20%"]["pct"] == pytest.approx(200.0 / 6000.0 * 100)
    worst = next(v for k, v in s.items() if k.startswith("The worst day of the past year again"))
    assert "Left out, no price history: K." in worst["what"] and worst["loss"] == pytest.approx(12.0)   # A only: 1.2% of 1,000
    assert rajya.stress([], 5000.0, rajya.history_measures([], 5000.0, {}), rajya.risk_budget([], 5000.0, 0.0)) == []
    short = [rajya.item("S", "intraday", -10, 100.0, 100.0)]
    sh = {x["name"]: x for x in rajya.stress(short, 5000.0, rajya.history_measures(short, 5000.0, {}), rajya.risk_budget(short, 5000.0, 0.0))}
    assert sh["NIFTY falls 5%"]["loss"] == pytest.approx(-50.0) and "S alone rises 20%" in sh and "Every stop is hit" not in sh


def test_record_reads_the_trade_log_after_charges(isolated):
    assert rajya.record("live", NOW.date())["trades"] == 0
    with open(os.path.join(str(isolated), "trades_live.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("closed,symbol,gross,charges,net\n2026-11-16T10:00,A,110,10,100\n2026-11-17T10:00,B,-140,10,-150\n"
                 "2026-11-17T11:00,C,-40,10,-50\n2026-11-18T10:00,D,70,10,60\n")
    r = rajya.record("live", NOW.date())
    assert (r["trades"], r["wins"], r["net"], r["gross"], r["charges"]) == (4, 2, -40.0, 0.0, 40.0)
    assert r["drawdown"] == 200.0 and r["below_high"] == 140.0 and r["today_trades"] == 1 and r["today_net"] == 60.0
    assert r["first"] == "2026-11-16"


# ---------------------------------------------------------------- the rebalancing simulation
def holdings_alloc():
    items = [rajya.item("A", "delivery", 60, 90.0, 100.0), rajya.item("B", "delivery", 10, 190.0, 200.0),
             rajya.item("K", "intraday", 2, 500.0, 500.0, 494.0)]
    return rajya.allocation(items, 2000.0)                              # whole account 10,000: A 60%, B 20%


def test_cap_rule_sells_just_enough_and_buys_nothing():
    sim = rajya.rebalance(holdings_alloc(), "cap", 25.0)
    assert sim["executes"] is False and [t["symbol"] for t in sim["trades"]] == ["A"]
    t = sim["trades"][0]
    assert (t["action"], t["qty"], t["value"]) == ("SELL", 35, 3500.0) and t["before_pct"] == 60.0 and t["after_pct"] == 25.0
    assert t["charges"] == pytest.approx(rajya.delivery_charges(100.0, 35, "SELL")) and t["charges"] > rajya.DP_CHARGE
    assert sim["bought"] == 0 and sim["cash_change"] == pytest.approx(3500.0 - t["charges"])
    assert rajya.rebalance(holdings_alloc(), "cap", 60.0)["trades"] == []
    assert rajya.rebalance(holdings_alloc())["cap_pct"] == C.MAX_POSITION_PCT
    assert rajya.rebalance(rajya.allocation([], 1000.0))["holdings"] == 0


def test_equal_rule_moves_holdings_toward_the_same_size_in_whole_shares_only():
    sim = rajya.rebalance(holdings_alloc(), "equal")
    by = {t["symbol"]: t for t in sim["trades"]}
    assert (by["A"]["action"], by["A"]["qty"]) == ("SELL", 20) and (by["B"]["action"], by["B"]["qty"]) == ("BUY", 10)
    assert by["A"]["after_pct"] == 40.0 and by["B"]["after_pct"] == 40.0 and "K" not in by      # intraday is left out
    assert sim["cash_change"] == pytest.approx(-sim["charges"]) and sim["executes"] is False
    assert by["B"]["charges"] == pytest.approx(rajya.delivery_charges(200.0, 10, "BUY"))
    assert by["B"]["charges"] < rajya.DP_CHARGE                        # a buy pays no depository charge


# ---------------------------------------------------------------- the build, the page, and what it must never do
def build_zerodha(isolated, **kw):
    kite = FakeKite(holdings=[holding("ITC", 10, 400.0, 420.0), holding("HDFCBANK", 3, 900.0, 1000.0)],
                    positions=[position("KOTAKBANK", 2, 500.0, 499.0)], opening=5000.0, **kw)
    live_state(isolated, cash=5000.0, KOTAKBANK={"direction": "LONG", "qty": 2, "entry": 500.0, "stop": 494.0})
    cl = {"ITC": closes(400.0, WAVE), "HDFCBANK": closes(900.0, WAVE), "KOTAKBANK": closes(500.0, [-x for x in WAVE]),
          C.INDEX_DEFAULT: closes(20000.0, [x / 2 for x in WAVE])}
    asked = []

    def fetch(symbols, job=None):
        asked.append(list(symbols))
        return {s: cl[s] for s in symbols if s in cl}

    return rajya.build("zerodha", now=NOW, client=kite, fetch=fetch), kite, asked


def test_build_saves_one_result_per_view_and_changes_no_other_file(isolated):
    res, kite, asked = build_zerodha(isolated)
    assert kite.calls == ["margins", "positions", "holdings"]
    assert asked == [["HDFCBANK", "ITC", "KOTAKBANK", C.INDEX_DEFAULT]]
    assert sorted(os.listdir(str(isolated))) == ["rajya_zerodha.json", "state_live.json"]
    saved = rajya.latest("zerodha")
    assert saved["allocation"]["total"] == pytest.approx(4998.0 + 4200.0 + 3000.0) and rajya.latest("paper") is None
    assert saved["history"]["pairs"][0]["high"] and saved["history"]["pairs"][-1]["corr"] < -0.99
    assert rajya.status(saved, NOW)["stale"] is False
    assert "prices have moved" in rajya.status(saved, NOW + dt.timedelta(minutes=17))["why"]
    assert rajya.status(saved, NOW + dt.timedelta(days=1))["stale"] and rajya.status(None)["stale"]
    assert json.dumps(res, default=str, sort_keys=True) == json.dumps(
        rajya.measure(rajya.gather("zerodha", NOW, FakeKite(
            holdings=[holding("ITC", 10, 400.0, 420.0), holding("HDFCBANK", 3, 900.0, 1000.0)],
            positions=[position("KOTAKBANK", 2, 500.0, 499.0)], opening=5000.0)),
            {k: v for k, v in zip(["ITC", "HDFCBANK", "KOTAKBANK", C.INDEX_DEFAULT],
                                  [closes(400.0, WAVE), closes(900.0, WAVE), closes(500.0, [-x for x in WAVE]),
                                   closes(20000.0, [x / 2 for x in WAVE])])}, NOW) | {"built": res["built"]},
        default=str, sort_keys=True)                                    # same inputs, same picture


def test_page_shows_the_picture_and_the_simulation_and_has_nothing_that_trades(isolated):
    first = web.render_rajya({"view": ["zerodha"]})
    assert "The first picture appears here" in first and "It is not advice" in first
    build_zerodha(isolated, payin=6000.0)
    page = web.render_rajya({"view": ["zerodha"]})
    for text in ("Where the money is", "Risk budget", "How these positions have moved", "Do they move together?",
                 "What-if arithmetic", "Booked result and drawdown", "Rebalancing simulation", "Simulation only.",
                 "Rajya cannot place", "added today is counted here", "close to one bet", "No stop is known for: ITC, HDFCBANK",
                 "NIFTY falls 10%", "delivery · yours", "intraday · the bot", "LIVE · REAL ZERODHA ACCOUNT"):
        assert text in page, text
    body = page.split("<main", 1)[1]
    assert "Zerodha's figure, before charges" in body                  # why it can differ from the bot's booked result
    assert re.findall(r"<form[^>]*method='(\w+)'", body) == ["get"] and "method='post'" not in body.lower()
    assert "no holding is above 25% of the account" in body          # ITC is 23% of 18,198: nothing to do
    capped = web.render_rajya({"view": ["zerodha"], "cap": ["10"]}).split("id='rebalance'", 1)[1]
    assert "ITC" in capped and "HDFCBANK" in capped and "sell" in capped and "buy " not in capped.split("<table", 1)[1]
    equal = web.render_rajya({"view": ["zerodha"], "rule": ["equal"]})
    # target 3,600 each: ITC sells 1 share (420); HDFCBANK would need 0.6 of a share, so no buy is shown
    assert "sell 1" in equal.split("id='rebalance'", 1)[1] and "option value='equal' selected" in equal
    odd = web.render_rajya({"view": ["zerodha"], "cap": ["abc"], "rule": ["x"]})      # nonsense falls back to the default
    assert "no holding is above 25% of the account" in odd and "option value='cap' selected" in odd


def test_showing_an_account_never_changes_the_trading_mode(isolated, monkeypatch):
    with open(os.path.join(str(isolated), "status.json"), "w", encoding="utf-8") as fh:
        json.dump({"mode": "paper", "positions": []}, fh)
    before = {k: os.environ.get(k) for k in ("DRY_RUN", "CONFIRM_LIVE")}
    dry = C.DRY_RUN
    page = web.render_rajya({"view": ["zerodha"]})
    assert "The first picture appears here" in page
    assert {k: os.environ.get(k) for k in before} == before and C.DRY_RUN == dry
    assert sorted(os.listdir(str(isolated))) == ["status.json"]              # nothing written by looking


def test_rajya_has_no_way_to_trade_and_is_not_part_of_the_bot():
    src = open(os.path.join(ROOT, "rajya.py"), encoding="utf-8").read()
    imports = set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", src, re.M))
    assert not imports & {"broker", "kiteconnect", "orders", "bot", "livemode", "kavach", "risk", "anthropic", "openai"}
    for word in ("place_order", "cancel_order", "modify_order", "convert_position", "LiveBroker", "place_gtt"):
        assert word not in src, word
    assert re.findall(r"\.(margins|positions|holdings|orders|quote|ltp)\(", src) == ["holdings", "holdings"]
    assert "rajya.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}
    assert "rajya.py" in {os.path.basename(p) for p in buildinfo.dashboard_files()}
    assert set(accounts.READ_ONLY_CALLS) == {"margins", "positions", "orders"}       # Rajya did not widen the account view


def test_routes_job_and_links_exist():
    src = open(web.__file__, encoding="utf-8").read()
    assert 'u.path == "/rajya"' in src and 'jobs.register("rajya"' in src and '"rajya": "/rajya"' in src
    assert "/rajya" in web.live_limits({})
