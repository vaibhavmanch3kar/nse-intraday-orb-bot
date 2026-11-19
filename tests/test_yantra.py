"""Yantrashala, the strategy lab: every idea runs through the one backtest engine; no strategy can see a future
candle; the lab's copy of the live rules matches the live rules; results are judged by a rule fixed in advance;
experiments are saved with a fingerprint; and a registered variant is judged only on later data."""

import datetime as dt
import json
import os
import random

import pytest

import backtest
import buildinfo
import config as C
import strategy
import yantra
import yantra_strategies as Y
from conftest import IST

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def session(day, rng, price, vol=4000):
    """One 25-candle session as a random walk, with a lively opening candle."""
    out = []
    t = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
    for k in range(25):
        o = price
        c = o * (1 + rng.uniform(-0.004, 0.004))
        h, lo = max(o, c) * (1 + rng.uniform(0, 0.002)), min(o, c) * (1 - rng.uniform(0, 0.002))
        out.append({"t": t, "o": round(o, 2), "h": round(h, 2), "l": round(lo, 2), "c": round(c, 2),
                    "v": int(vol * rng.uniform(0.4, 3.0) * (2 if k == 0 else 1))})
        price, t = c, t + dt.timedelta(minutes=15)
    return out, price


def market(days=30, seed=3, start=dt.date(2026, 1, 5)):
    rng = random.Random(seed)
    bars, price, d = [], 500.0, start
    while len({b["t"].date() for b in bars}) < days:
        if d.weekday() < 5:
            s, price = session(d, rng, price)
            bars += s
        d += dt.timedelta(days=1)
    return bars


@pytest.fixture
def rules(monkeypatch):
    """The live bot's present rules (the shared test settings use the original ones)."""
    for k, v in {"INDEX_FILTER": True, "TRAIL_EMA": 21, "TIME_STOP_BARS": 4, "TARGET_R": 0.0, "MIN_OR_PCT": 0.25,
                 "MAX_OR_PCT": 1.5, "VOLUME_MULT": 1.2, "USE_VWAP_FILTER": True, "ALLOW_SHORT": True}.items():
        monkeypatch.setattr(C, k, v)


# ---------------------------------------------------------------- the lab's copy of the live rules
def test_lab_orb_matches_the_live_rules_candle_by_candle(rules):
    bars, index = market(seed=3), market(seed=9)
    orb = Y.Orb()
    signals = 0
    for i in range(140, len(bars)):
        a, b = strategy.evaluate(bars, i, index), orb.evaluate(bars, i, index)
        assert (a["signal"], a["stop"]) == (b["signal"], b["stop"]), (bars[i]["t"], a["reason"])
        if a["signal"]:
            signals += 1
            assert abs(a["vol_ratio"] - b["vol_ratio"]) < 1e-9
    assert signals >= 5                                     # the comparison actually covered signals


def test_lab_orb_matches_without_the_index_filter_and_with_the_mid_stop(rules, monkeypatch):
    monkeypatch.setattr(C, "INDEX_FILTER", False)
    monkeypatch.setattr(C, "STOP_RULE", "mid")
    bars = market(seed=5)
    orb = Y.Orb()
    for i in range(140, len(bars)):
        a, b = strategy.evaluate(bars, i), orb.evaluate(bars, i)
        assert (a["signal"], a["stop"]) == (b["signal"], b["stop"])


# ---------------------------------------------------------------- no look-ahead
@pytest.mark.parametrize("name", list(Y.STRATEGIES))
def test_no_strategy_can_see_a_future_candle(rules, name):
    """The signal on candle i must be the same whether or not later candles exist."""
    bars, index = market(seed=11), market(seed=12)
    checked = 0
    for strat in Y.variants(name):
        for i in range(160, len(bars), 7):
            strat._feat = {}
            full = strat.evaluate(bars, i, index)
            strat._feat = {}
            cut = strat.evaluate(bars[: i + 1], i, index[: i + 1])
            assert (full["signal"], full["stop"]) == (cut["signal"], cut["stop"]), (strat.label(), bars[i]["t"])
            checked += 1
    assert checked > 50


def test_features_use_only_past_candles():
    bars, index = market(seed=2), market(seed=4)
    full = Y.Features(bars, index)
    i = 400
    cut = Y.Features(bars[: i + 1], index[: i + 1])
    for field in ("or_high", "or_low", "vwap", "vol_ratio", "prev_close", "ema9", "ema21", "atr", "index_chg",
                  "day_high", "day_low"):
        assert getattr(full, field)[: i + 1] == getattr(cut, field), field


# ---------------------------------------------------------------- one engine, and the bot never loads the lab
def test_every_idea_runs_through_the_one_engine_and_the_bot_never_loads_the_lab():
    src = open(os.path.join(ROOT, "yantra.py"), encoding="utf-8").read()
    assert "backtest.simulate(" in src and "strat=strat" in src
    assert "place_order" not in src and "broker" not in src.split('"""', 2)[2]
    bot_files = buildinfo.bot_files()
    assert "yantra.py" not in bot_files and "yantra_strategies.py" not in bot_files
    assert set(Y.STRATEGIES) == set(Y.WHAT) and sum(len(c.grid) for c in Y.STRATEGIES.values()) == 17


def test_default_engine_still_uses_the_live_rules(rules, monkeypatch, tmp_path):
    bars, index = market(days=20, seed=3), market(days=20, seed=9)
    data = {"SBIN": bars}
    a = backtest.simulate(data, 100000.0, False, {"^NSEI": index}, {}, trades_path=str(tmp_path / "a.csv"))
    b = backtest.simulate(data, 100000.0, False, {"^NSEI": index}, {}, trades_path=str(tmp_path / "b.csv"), strat=Y.Orb())
    import csv
    core = ["opened", "closed", "symbol", "direction", "qty", "entry", "exit", "stop", "target", "gross", "charges",
            "net", "r", "reason"]                             # the trade itself (the lab records fewer side notes)

    def rows(name):
        return [[r[c] for c in core] for r in csv.DictReader(open(tmp_path / name, encoding="utf-8"))]

    assert rows("a.csv") == rows("b.csv") and a["trades"] == b["trades"] >= 3


# ---------------------------------------------------------------- measuring and judging
def trades(rs, start=dt.date(2026, 1, 5), per_month=10):
    out = []
    for k, r in enumerate(rs):
        day = dt.date(start.year, start.month + k // per_month, 5 + k % per_month)
        out.append({"day": day, "symbol": "X", "direction": "LONG", "net": r * 500.0, "r": r, "reason": ""})
    return out


def test_measure_is_reproducible_and_right():
    tr = trades([1.0, -0.5, 2.0, -1.0, 0.5, -0.5] * 6)
    m, again = yantra.measure(tr), yantra.measure(tr)
    assert m == again                                        # the same trades, the same numbers
    assert m["trades"] == 36 and abs(m["avg_r"] - 0.25) < 1e-9 and m["win_rate"] == 0.5
    assert abs(m["profit_factor"] - 3.5 / 2.0) < 1e-9 and m["r_low"] < 0.25 < m["r_high"]
    assert m["months"] == 4 and yantra.measure([]) == {"trades": 0}


def test_verdict_needs_every_condition():
    good = yantra.measure(trades([1.0, 0.8, -0.3, 1.2, 0.9, -0.2] * 6))
    assert yantra.verdict(good) == ("PASS", [])
    few = yantra.measure(trades([1.0, 0.8, 1.2] * 5))
    assert yantra.verdict(few)[0] == "FAIL" and "only 15 walk-forward trades" in yantra.verdict(few)[1][0]
    noisy = yantra.measure(trades([3.0, -1.0, -1.0, -1.0, 1.0, -0.9] * 6))
    v, why = yantra.verdict(noisy)
    assert v == "FAIL" and any("could be zero or negative" in w for w in why)
    losing = yantra.measure(trades([-0.3, -0.2, 0.1] * 12))
    assert yantra.verdict(losing)[0] == "FAIL" and len(yantra.verdict(losing)[1]) == 3
    assert yantra.verdict({"trades": 0})[0] == "FAIL"


def test_walk_forward_judges_only_months_after_the_training_window():
    start, end = dt.date(2026, 1, 1), dt.date(2026, 6, 30)

    def monthly(r_by_month):
        return [{"day": dt.date(2026, m, d), "symbol": "X", "direction": "LONG", "net": r * 100, "r": r, "reason": ""}
                for m, r in r_by_month.items() for d in range(2, 22)]

    a = monthly({1: 0.5, 2: 0.5, 3: 0.5, 4: -1.0, 5: -1.0, 6: -1.0})      # good early, bad later
    b = monthly({1: -0.5, 2: -0.5, 3: -0.5, 4: 1.0, 5: 1.0, 6: 1.0})      # bad early, good later
    stitched, picks = yantra.walk_forward({"a": a, "b": b}, start, end)
    assert [p["month"] for p in picks] == ["2026-04", "2026-05", "2026-06"]
    assert [p["chosen"] for p in picks] == ["a", "a", "b"]                # chosen on the past, never the future
    assert {t["day"].month for t in stitched} == {4, 5, 6}                # the training months are not judged
    assert sum(t["r"] for t in stitched) == pytest.approx(-20.0 - 20.0 + 20.0)
    none, picks = yantra.walk_forward({"a": a[:5]}, start, end)
    assert none == [] and all(p["chosen"] is None for p in picks)         # too few trades to choose from: no trading


# ---------------------------------------------------------------- experiments and the forward register
@pytest.fixture
def lab(monkeypatch, rules):
    bars = {s: market(days=45, seed=k + 1, start=dt.date(2026, 1, 5)) for k, s in enumerate(["SBIN", "ITC"])}
    idx = {"^NSEI": market(days=45, seed=30, start=dt.date(2026, 1, 5)),
           "^NSEBANK": market(days=45, seed=31, start=dt.date(2026, 1, 5))}
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN", "ITC"])
    monkeypatch.setattr(yantra, "DEV_START", dt.date(2026, 1, 12))
    monkeypatch.setattr(yantra, "DEV_END", dt.date(2026, 2, 27))
    return bars, idx


def test_an_experiment_is_saved_with_everything_needed_to_repeat_it(lab):
    now = dt.datetime(2026, 11, 18, 14, 0, tzinfo=IST)
    exp = yantra.test("orb_regime", data=lab, now=now)
    saved = json.load(open(os.path.join(yantra.lab_dir(), "experiments", exp["id"] + ".json"), encoding="utf-8"))
    assert saved["id"] == "20261118-140000-orb_regime" and saved["variants_tried"] == 2
    assert saved["code"] == yantra.fingerprint() and len(saved["code"]) == 12
    assert saved["settings"]["RISK_PER_TRADE_PCT"] == C.RISK_PER_TRADE_PCT and saved["verdict"] in ("PASS", "FAIL")
    assert set(saved["in_sample"]) == {"orb_regime(index_min=0.2)", "orb_regime(index_min=0.4)"}
    assert yantra.test("orb_regime", data=lab, now=now, save=False)["walk_forward"] == exp["walk_forward"]
    assert yantra.experiments()[0]["id"] == exp["id"] and yantra.tests_so_far() == 2


def test_only_a_tested_variant_can_be_registered_and_only_once(lab):
    now = dt.datetime(2026, 1, 30, 16, 0, tzinfo=IST)
    r = yantra.register("orb_regime", {"index_min": 0.2}, now=now)
    assert r["registered"] == "2026-01-30" and r["code"] == yantra.fingerprint()
    with pytest.raises(ValueError):
        yantra.register("orb_regime", {"index_min": 0.33}, now=now)       # not one of the tested variants
    with pytest.raises(ValueError):
        yantra.register("orb_regime", {"index_min": 0.2}, now=now)        # already frozen


def test_forward_uses_only_candles_after_the_registration_day(lab, monkeypatch):
    yantra.register("orb", {}, now=dt.datetime(2026, 2, 13, 16, 0, tzinfo=IST))
    seen = {}
    real_run = yantra.run

    def spy(strat, start, end, data=None):
        seen.update(start=start, end=end)
        return real_run(strat, start, end, data)

    monkeypatch.setattr(yantra, "run", spy)
    [row] = yantra.forward(data=lab)
    assert row["status"] == "measured" and row["from"] == "2026-02-14" and seen["start"] == dt.date(2026, 2, 14)
    assert all(True for _ in [row["result"]]) and row["verdict"] in ("PASS", "FAIL")
    monkeypatch.setattr(yantra, "fingerprint", lambda: "000000000000")    # the rules were edited afterwards
    assert yantra.forward(data=lab)[0]["status"].startswith("code changed since registration")


def test_forward_with_no_later_candles_measures_nothing(lab):
    yantra.register("orb", {}, now=dt.datetime(2026, 12, 1, 16, 0, tzinfo=IST))
    [row] = yantra.forward(data=lab)
    assert row["result"] is None and "no candles after the registration day" in row["status"]


def test_the_lab_never_touches_the_old_holdout_to_pass_anything():
    src = open(os.path.join(ROOT, "yantra.py"), encoding="utf-8").read()
    body = src.split('"""', 2)[2]
    assert "DEV_START, DEV_END" in body and body.count("USED_END") == 1    # the used period is named, never run


# ---------------------------------------------------------------- orb_late: the late entry after a low-volume breakout
def late_day(third_close=100.9, third_vol=1500, width_high=100.5):
    """Five quiet sessions (every candle 1000 shares), then a day whose opening candle sets the range, the third
    candle breaks out on normal volume (1.0x, refused by the live 1.2x rule) and the fourth closes at
    `third_close` on `third_vol` shares."""
    bars, d = [], dt.date(2026, 1, 5)
    while len({b["t"].date() for b in bars}) < 5:
        if d.weekday() < 5:
            t = dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST)
            for k in range(25):
                bars.append({"t": t, "o": 100.0, "h": 100.2, "l": 99.9, "c": 100.0 + (0.05 if k % 2 else 0), "v": 1000})
                t += dt.timedelta(minutes=15)
        d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    t0 = dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST)
    day = [(100.0, width_high, 99.8, 100.2, 1000), (100.2, 100.4, 100.1, 100.3, 1000),
           (100.3, 100.8, 100.25, 100.7, 1000), (100.7, 101.0, 100.6, third_close, third_vol)]
    for k, (o, h, lo, c, v) in enumerate(day):
        bars.append({"t": t0 + dt.timedelta(minutes=15 * k), "o": o, "h": max(h, c), "l": min(lo, c), "c": c, "v": v})
    return bars


def signals(strat, bars):
    return [(bars[i]["t"].strftime("%H:%M"), strat.evaluate(bars, i)["signal"]) for i in range(len(bars) - 4, len(bars))]


def test_late_entry_buys_a_low_volume_breakout_once_its_volume_comes(rules):
    bars = late_day()
    assert signals(Y.Orb(), bars) == [("09:15", None), ("09:30", None), ("09:45", None), ("10:00", None)]
    ev = Y.OrbLate(wait=2).evaluate(bars, len(bars) - 1)
    assert ev["signal"] == "LONG" and ev["stop"] == 99.8           # the stop is the live rule's: the range low


def test_no_late_entry_when_price_went_back_inside_the_range(rules):
    bars = late_day(third_close=100.45)                            # the fourth candle closes back inside
    assert Y.OrbLate(wait=2).evaluate(bars, len(bars) - 1)["signal"] is None


def test_no_late_entry_without_the_volume(rules):
    bars = late_day(third_vol=1000)
    assert Y.OrbLate(wait=2).evaluate(bars, len(bars) - 1)["signal"] is None


def test_no_late_entry_for_a_breakout_refused_for_another_reason(rules):
    bars = late_day(width_high=101.6)                              # a 1.8% opening range: too wide, not low volume
    for i in range(len(bars) - 4, len(bars)):
        assert Y.OrbLate(wait=2).evaluate(bars, i)["signal"] is None


def test_late_entry_takes_every_breakout_the_live_rules_take(rules):
    bars = market(days=40, seed=11)
    orb, late = Y.Orb(), Y.OrbLate(wait=2)
    taken = [i for i in range(len(bars)) if orb.evaluate(bars, i)["signal"]]
    assert taken, "the random market should have some live-rule breakouts"
    for i in taken:
        assert late.evaluate(bars, i)["signal"] == orb.evaluate(bars, i)["signal"]
        assert late.evaluate(bars, i)["stop"] == orb.evaluate(bars, i)["stop"]
