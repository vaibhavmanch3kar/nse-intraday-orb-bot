"""Kavach, the one risk engine: the same checks in the same order for the bot and the backtest; every refusal
names the rule; the limits in force are the intended ones; nothing in it can place an order."""

import datetime as dt
import os

import pytest

import config as C
import kavach
import risk

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = dt.date(2026, 11, 18)


def t(hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(DAY, dt.time(h, m), IST)


@pytest.fixture(autouse=True)
def real_defaults(monkeypatch):
    """The shared test settings switch Kavach's newer limits off; here they have their real default values."""
    for name, default, _ in kavach.LIMITS:
        if name in ("MAX_SIGNAL_AGE_MIN", "MAX_PER_SECTOR", "MAX_BAR_VOLUME_PCT"):
            monkeypatch.setattr(C, name, default)


@pytest.fixture
def st(tmp_path):
    s = risk.State(str(tmp_path / "state_paper.json"), 100000.0)
    s.new_day_if_needed(DAY)
    return s


def ask(st, symbol="SBIN", direction="LONG", entry=1000.0, stop=990.0, **kw):
    return kavach.pre_trade(st, symbol, direction, entry, stop, DAY, **kw)


def test_a_clean_trade_passes_every_check_in_order(st):
    d = ask(st, signal_close=1000.0, now=t("10:00"), square_off=t("15:10"))
    assert d.ok and bool(d) and d.reason == "" and d.check == ""
    assert d.qty == risk.size_position(100000.0, 1000.0, 990.0, "LONG")[0] == 25      # 25% per-stock limit
    assert [c[0] for c in d.checks] == list(kavach.CHECKS) and all(c[1] for c in d.checks)


@pytest.mark.parametrize("kw, check, text", [
    (dict(now=t("15:10"), square_off=t("15:10")), "time", "after the square-off time"),
    (dict(paused=True), "paused", "paused"),
    (dict(balance_ok=False), "balance", "Zerodha balance not read yet"),
    (dict(balance_ok=False, balance_problem="could not read your Zerodha balance (Timeout)"), "balance", "Timeout"),
    (dict(signal_close=996.0), "price", "price ran 0.7R past signal"),          # entry 1000 is 4 past 996; risk 6
    (dict(stop=1000.0), "price", "price already at stop"),
])
def test_each_refusal_names_its_rule(st, kw, check, text):
    d = ask(st, **kw)
    assert not d.ok and d.qty == 0 and d.check == check and text in d.reason
    assert d.checks[-1] == (check, False, d.reason)          # it stopped at the first failing check


def test_account_limits_come_from_the_account_state(st):
    st.positions["SBIN"] = {"direction": "LONG", "qty": 5, "entry": 1000.0}
    assert "already in position" in ask(st).reason and ask(st).check == "account"      # no averaging down
    st.positions.pop("SBIN")
    st.d["traded_symbols"].append("SBIN")
    assert "already traded today" in ask(st).reason                                    # no second entry
    st.d["traded_symbols"].clear()
    st.d["trades_today"] = C.MAX_TRADES_PER_DAY
    assert f"{C.MAX_TRADES_PER_DAY} trades today" in ask(st).reason
    st.d["trades_today"] = 0
    st.d["day_pnl"] = -st.d["day_start_equity"] * C.MAX_DAILY_LOSS_PCT / 100
    assert "daily loss limit" in ask(st).reason
    st.d["day_pnl"] = 0.0
    st.d["cooldown_until"] = "2026-11-20"
    assert "cooldown" in ask(st).reason


def test_open_losses_count_toward_the_daily_limit(st):
    st.positions["ITC"] = {"direction": "LONG", "qty": 100, "entry": 300.0}
    assert ask(st, prices={"ITC": 300.0}).ok
    d = ask(st, prices={"ITC": 279.0})                        # open loss 2,100 = 2.1% of 1,00,000
    assert not d.ok and "daily loss limit" in d.reason


def test_size_refusals_name_the_binding_limit(st):
    st.d["cash"] = 4870.6
    st.d["day_start_equity"] = 4870.6
    d = ask(st, "RELIANCE", "SHORT", entry=1361.0, stop=1373.0)
    assert d.check == "size" and "per-stock limit" in d.reason
    assert ask(st, "ITC", entry=300.0, stop=291.0).qty == 2


def test_sizing_can_use_booked_equity_as_the_backtest_does(st):
    st.positions["ITC"] = {"direction": "LONG", "qty": 100, "entry": 300.0}
    marked = ask(st, entry=100.0, stop=99.0, prices={"ITC": 310.0})            # account worth 1,01,000
    booked = ask(st, entry=100.0, stop=99.0, prices={"ITC": 310.0}, equity=st.equity())
    assert marked.qty == 252 and booked.qty == 250


def test_bot_and_backtest_reach_the_same_answer_as_the_old_inline_rules(st):
    """The old bot code, written out, against Kavach on many random situations."""
    import random
    rng = random.Random(11)
    for _ in range(2000):
        st.d.update(cash=rng.choice([4870.6, 20000.0, 100000.0]), trades_today=rng.choice([0, 1, 3, 4]),
                    day_pnl=rng.choice([0.0, -50.0, -2500.0]), cooldown_until=rng.choice([None, None, "2026-11-20"]),
                    traded_symbols=rng.choice([[], ["SBIN"]]))
        st.d["day_start_equity"] = st.d["cash"]
        st.positions.clear()
        for s in rng.sample(["ITC", "INFY", "TCS", "SBIN"], rng.choice([0, 1, 3])):
            st.positions[s] = {"direction": "LONG", "qty": 2, "entry": 500.0}
        direction = rng.choice(["LONG", "SHORT"])
        close = rng.uniform(200, 3000)
        stop = close * (0.99 if direction == "LONG" else 1.01)
        ref = close * (1 + rng.uniform(-0.012, 0.012))
        paused, bal_ok = rng.random() < 0.1, rng.random() > 0.1
        # --- the rules as bot.cycle had them before Kavach
        old = None
        if paused:
            old = "paused"
        elif not bal_ok:
            old = "Zerodha balance not read yet"
        else:
            ok, why = st.can_enter("SBIN", DAY, {})
            risk_ps = abs(close - stop)
            chased = (ref - close) if direction == "LONG" else (close - ref)
            if not ok:
                old = why
            elif chased > C.MAX_CHASE_R * risk_ps:
                old = f"price ran {chased / risk_ps:.1f}R past signal"
            elif (direction == "LONG" and ref <= stop) or (direction == "SHORT" and ref >= stop):
                old = "price already at stop"
            else:
                qty, why = risk.size_position(st.equity({}), ref, stop, direction, st.open_notional(), 1.0)
                old = qty if qty > 0 else why
        d = kavach.pre_trade(st, "SBIN", direction, ref, stop, DAY, prices={}, paused=paused, balance_ok=bal_ok,
                             signal_close=close)
        assert (d.qty if d.ok else d.reason) == old


def test_limits_in_force_are_the_intended_ones():
    by = {x["name"]: x for x in kavach.limits()}
    assert (by["RISK_PER_TRADE_PCT"]["value"], by["MAX_OPEN_POSITIONS"]["value"], by["MAX_TRADES_PER_DAY"]["value"],
            by["MAX_DAILY_LOSS_PCT"]["value"], by["LOSS_STREAK_LIMIT"]["value"]) == (0.5, 3, 4, 2.0, 3)
    assert by["MAX_EXPOSURE_PCT"]["value"] <= 100                # all positions together never exceed the account
    assert kavach.looser_than_default() == []


def test_a_loosened_limit_is_reported(monkeypatch):
    monkeypatch.setattr(C, "RISK_PER_TRADE_PCT", 2.0)
    monkeypatch.setattr(C, "MAX_DAILY_LOSS_PCT", 1.0)            # stricter: not reported
    assert [x["name"] for x in kavach.looser_than_default()] == ["RISK_PER_TRADE_PCT"]


def test_kavach_cannot_place_an_order_and_is_used_by_bot_and_backtest():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "kavach.py"), encoding="utf-8").read()
    for word in ("import broker", "place_order", "kiteconnect", "import kite", "import requests", "open("):
        assert word not in src.split('"""', 2)[2], word          # outside the description at the top
    for name in ("bot.py", "backtest.py"):
        code = open(os.path.join(root, name), encoding="utf-8").read()
        assert "kavach.pre_trade(" in code and "risk.size_position(" not in code, name
