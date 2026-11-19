import datetime as dt
import os

import pytest

import config as C
import costs
import risk


def test_charges_match_zerodha_formula():
    # buy 100 @ 1000, sell 100 @ 1010  (turnover 1,00,000 + 1,01,000)
    buy = costs.order_charges(1000, 100, "BUY")
    sell = costs.order_charges(1010, 100, "SELL")
    # brokerage capped at ₹20 each side
    exp_buy = 20 + 100000 * 0.00297 / 100 + 100000 * 0.0001 / 100
    exp_buy += exp_buy * 0.18 + 100000 * 0.003 / 100
    assert buy == pytest.approx(exp_buy)
    assert sell > buy  # STT only on sell
    assert costs.round_trip(1000, 1010, 100, "LONG") == pytest.approx(buy + sell)
    assert costs.gross_pnl(1000, 1010, 100, "SHORT") == -1000


def test_small_order_brokerage_is_percentage():
    assert costs.order_charges(100, 10, "BUY") < 2  # 0.03% of ₹1,000 = ₹0.30, not ₹20


def test_sizing_keeps_loss_plus_charges_within_budget():
    qty, why = risk.size_position(100000, 500, 495, "LONG")
    assert why == "ok" and qty > 0
    loss = 5 * qty + costs.round_trip(500, 495, qty, "LONG")
    assert loss <= 500 + 1e-9
    assert 500 * qty <= 25000  # per-stock cap


def test_sizing_caps():
    # tiny stop -> per-stock notional cap binds
    qty, _ = risk.size_position(100000, 1000, 999.5, "LONG")
    assert qty == 25
    # exposure almost used -> few/no shares
    qty, _ = risk.size_position(100000, 1000, 990, "LONG", open_notional=74500)
    assert qty == 0
    assert risk.size_position(100000, 100, 100, "LONG")[0] == 0


def test_state_gates_and_streak(tmp_path):
    st = risk.State(os.path.join(tmp_path, "s.json"), 100000)
    day = dt.date(2026, 11, 16)
    st.new_day_if_needed(day)
    assert st.can_enter("SBIN", day)[0]
    for i, sym in enumerate(["A", "B", "C"]):
        st.record_entry(sym, "LONG", 10, 100, 99, 102, "2026-11-16 10:00:00+05:30")
    assert st.can_enter("D", day) == (False, "3 positions open")
    for sym in ["A", "B", "C"]:
        st.record_exit(sym, 99, "2026-11-16 11:00:00+05:30")  # three losses
    assert st.d["cooldown_until"] == "2026-11-19"
    assert not st.can_enter("D", day)[0]
    assert not st.can_enter("D", dt.date(2026, 11, 18))[0]
    st.new_day_if_needed(dt.date(2026, 11, 19))
    assert st.can_enter("A", dt.date(2026, 11, 19))[0]


def test_daily_loss_limit_and_one_trade_per_symbol(tmp_path):
    st = risk.State(os.path.join(tmp_path, "s.json"), 100000)
    day = dt.date(2026, 11, 16)
    st.new_day_if_needed(day)
    st.record_entry("SBIN", "LONG", 100, 1000, 980, 1040, "x")
    st.record_exit("SBIN", 978, "y")  # -2,200 + charges > 2% of 1,00,000
    ok, why = st.can_enter("INFY", day)
    assert not ok and "daily loss" in why
    st.d["day_pnl"] = 0
    assert st.can_enter("SBIN", day) == (False, "already traded today")


def test_daily_loss_limit_counts_open_losses(tmp_path):
    st = risk.State(os.path.join(tmp_path, "s.json"), 100000)
    day = dt.date(2026, 11, 16)
    st.new_day_if_needed(day)
    st.record_entry("SBIN", "LONG", 100, 1000, 970, 1060, "x")   # 2.5% of equity at risk
    st.record_entry("INFY", "SHORT", 50, 1500, 1530, 1440, "x")
    # booked P&L is 0; open: SBIN -2,100 (1000 -> 979), INFY +100 (1500 -> 1498) => -2,000 = -2%
    prices = {"SBIN": 979.0, "INFY": 1498.0}
    assert st.open_pnl(prices) == -2000.0
    ok, why = st.can_enter("TCS", day, prices)
    assert not ok and "daily loss" in why and "open" in why
    # a small open loss (-0.0%) is fine: 2 positions < 3, 2 trades < 4
    assert st.can_enter("TCS", day, {"SBIN": 999.0, "INFY": 1498.0}) == (True, "ok")
    # without prices only booked P&L counts (the old behaviour) - this is what let entries through
    assert st.can_enter("TCS", day) == (True, "ok")
    # booked + open together: -1,500 booked and -600 open = -2.1%
    st.d["day_pnl"] = -1500.0
    assert not st.can_enter("TCS", day, {"SBIN": 994.0, "INFY": 1500.0})[0]


def test_state_persists(tmp_path):
    p = os.path.join(tmp_path, "s.json")
    st = risk.State(p, 50000)
    st.record_entry("SBIN", "SHORT", 5, 800, 810, 780, "t")
    st2 = risk.State(p, 99999)
    assert st2.positions["SBIN"]["direction"] == "SHORT" and st2.d["cash"] == 50000


def test_round_tick():
    assert risk.round_tick(100.01, True, 0.05) == 100.05
    assert risk.round_tick(100.04, False, 0.05) == 100.0
    assert risk.round_tick(100.05, True, 0.05) == 100.05
