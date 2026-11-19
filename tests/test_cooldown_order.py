"""The cool-down after losses in a row: positions that close at the same moment (the same candle) are counted
losses first, whatever order they are recorded in. Before this, a win recorded between two same-candle losses
wiped the count, so the result depended on recording order and the bot and the backtest could disagree."""

import datetime as dt
import itertools
import os

import pytest

import config as C
import risk

DAY = "2026-11-16"
T1, T2, T3 = f"{DAY} 10:00:00+05:30", f"{DAY} 11:00:00+05:30", f"{DAY} 12:00:00+05:30"
WIN, LOSS = 103.0, 99.0                      # exit prices for a long from 100 with a stop at 99


def state(tmp_path, name="s.json"):
    import datetime as dt
    st = risk.State(os.path.join(str(tmp_path), name), 100000)
    st.new_day_if_needed(dt.date.fromisoformat(DAY))
    return st


def close(st, exits):
    """exits: [(symbol, exit price, when)] recorded in this order."""
    for sym, _, _ in exits:
        if sym not in st.positions:
            st.positions[sym] = {"direction": "LONG", "qty": 10, "entry": 100.0, "stop": 99.0, "target": None,
                                 "opened": f"{DAY} 09:45:00+05:30"}
    for sym, px, when in exits:
        st.record_exit(sym, px, when)
    return st.d["loss_streak"], st.d["cooldown_until"]


def test_a_win_on_the_same_candle_does_not_wipe_a_loss_in_either_order(tmp_path):
    for n, order in enumerate(itertools.permutations([("A", LOSS, T1), ("B", LOSS, T1), ("C", WIN, T1)])):
        st = state(tmp_path, f"a{n}.json")
        assert close(st, list(order)) == (0, None)            # two losses then the win: nothing carried, no cool-down
        assert close(st, [("D", LOSS, T2)]) == (1, None)
    # the case the paper replay found: two losses and a win on one candle, then one more loss on the next
    for n, order in enumerate(itertools.permutations([("A", LOSS, T1), ("B", LOSS, T1), ("C", WIN, T1)])):
        st = state(tmp_path, f"b{n}.json")
        st.d["loss_streak"] = 1                                # one loss already behind us
        streak, cool = close(st, list(order))
        assert (streak, cool) == (0, "2026-11-19"), order      # 1 + 2 losses = 3, in every recording order


@pytest.mark.parametrize("before", [0, 1, 2])
@pytest.mark.parametrize("group", [("L",), ("W",), ("L", "W"), ("L", "L", "W"), ("L", "W", "W"), ("L", "L", "L", "W"),
                                   ("L", "L", "L", "L", "W"), ("L", "L", "W", "W")])
def test_every_recording_order_gives_what_losses_first_gives(tmp_path, before, group):
    def run(order, name):
        st = state(tmp_path, name)
        st.d["loss_streak"] = before
        exits = [(f"S{i}", LOSS if kind == "L" else WIN, T1) for i, kind in order]
        return close(st, exits)

    indexed = list(enumerate(group))
    want = run(sorted(indexed, key=lambda x: x[1] != "L"), "want.json")       # losses first, then wins
    limit = C.LOSS_STREAK_LIMIT
    losses = group.count("L")
    assert want == (0 if "W" in group else (before + losses) % limit if before + losses >= limit else before + losses,
                    "2026-11-19" if before + losses >= limit else None)
    for n, order in enumerate(set(itertools.permutations(indexed))):
        assert run(list(order), f"o{n}.json") == want, order


def test_exits_at_different_moments_are_counted_in_the_order_they_happen_as_before(tmp_path):
    st = state(tmp_path)
    assert close(st, [("A", LOSS, T1), ("B", LOSS, T2)]) == (2, None)
    assert close(st, [("C", WIN, T3)]) == (0, None)            # a later win still ends the streak
    assert close(st, [("D", LOSS, f"{DAY} 12:15:00+05:30"), ("E", LOSS, f"{DAY} 12:30:00+05:30")]) == (2, None)
    assert close(st, [("F", LOSS, f"{DAY} 12:45:00+05:30")]) == (0, "2026-11-19")
    st2 = state(tmp_path, "t.json")
    assert close(st2, [("A", LOSS, T1), ("B", WIN, T2), ("C", LOSS, T3)]) == (1, None)     # win in between: reset


def test_a_break_even_exit_counts_as_not_a_loss_as_before(tmp_path):
    st = state(tmp_path)
    st.d["loss_streak"] = 2
    st.positions["A"] = {"direction": "LONG", "qty": 10, "entry": 100.0, "stop": 99.0, "target": None, "opened": T1}
    row = st.record_exit("A", 110.0, T2)
    assert row["net"] > 0 and st.d["loss_streak"] == 0 and st.d["cooldown_until"] is None


def test_the_count_survives_a_restart_in_the_middle_of_a_candle(tmp_path):
    st = state(tmp_path)
    st.d["loss_streak"] = 2
    close(st, [("W", WIN, T1)])                                # the win is recorded, then the bot stops
    again = risk.State(st.path, 100000)                        # ...and starts again from the saved file
    assert again.d["streak_before_win"] == 2 and again.d["streak_moment"] == T1
    assert close(again, [("L", LOSS, T1)]) == (0, "2026-11-19")


def test_an_account_file_from_before_this_change_still_loads(tmp_path):
    import json
    p = os.path.join(str(tmp_path), "old.json")
    json.dump({"cash": 5000.0, "realized_pnl": 0.0, "charges_paid": 0.0, "day": DAY, "day_start_equity": 5000.0,
               "day_pnl": 0.0, "trades_today": 0, "traded_symbols": [], "loss_streak": 2, "cooldown_until": None,
               "positions": {}}, open(p, "w", encoding="utf-8"))
    st = risk.State(p, 0.0)
    assert close(st, [("A", LOSS, T1)]) == (0, "2026-11-19")


def test_bot_and_backtest_share_this_one_place():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("bot.py", "backtest.py"):
        src = open(os.path.join(root, name), encoding="utf-8").read()
        assert "record_exit(" in src and "loss_streak\"] +=" not in src and "loss_streak'] +=" not in src
    assert "def _count_streak" in open(os.path.join(root, "risk.py"), encoding="utf-8").read()


# ---------------------------------------------------------------- the cool-down is counted in trading days
def three_losses_on(tmp_path, day, name):
    st = risk.State(os.path.join(str(tmp_path), name), 100000)
    st.new_day_if_needed(day)
    for sym in ("A", "B", "C"):
        st.record_entry(sym, "LONG", 10, 100.0, 99.0, None, f"{day} 10:00:00+05:30")
    for sym in ("A", "B", "C"):
        st.record_exit(sym, 99.0, f"{day} 11:00:00+05:30")
    return st


def test_three_losses_on_a_friday_keep_the_bot_out_for_two_trading_days(tmp_path):
    """Review, 19 Nov 2026: the two days off were Saturday and Sunday, and Monday traded as usual."""
    fri = dt.date(2026, 11, 13)
    st = three_losses_on(tmp_path, fri, "fri.json")
    assert st.d["cooldown_until"] == "2026-11-18"                                # Mon and Tue off, back on Wednesday
    for day, allowed in ((dt.date(2026, 11, 16), False), (dt.date(2026, 11, 17), False), (dt.date(2026, 11, 18), True)):
        st.new_day_if_needed(day)
        assert st.can_enter("D", day)[0] is allowed, day


def test_the_real_holiday_calendar_is_used(tmp_path):
    """Tuesday 24 Nov 2026 is a market holiday (Guru Nanak Jayanti) in the bundled NSE list: after three losses
    on Friday the 20th the two trading days off are Monday the 23rd and Wednesday the 25th."""
    st = three_losses_on(tmp_path, dt.date(2026, 11, 20), "fri2.json")
    assert st.d["cooldown_until"] == "2026-11-26"


def test_a_cool_down_in_the_middle_of_the_week_is_what_it_was(tmp_path):
    st = three_losses_on(tmp_path, dt.date(2026, 11, 16), "mon.json")            # Monday
    assert st.d["cooldown_until"] == "2026-11-19"                                # Tue and Wed off, back on Thursday


def test_a_market_holiday_is_not_one_of_the_days_off(tmp_path, monkeypatch):
    import holidays
    monkeypatch.setattr(holidays, "holiday_name", lambda d: "Test holiday" if d == dt.date(2026, 11, 17) else None)
    st = three_losses_on(tmp_path, dt.date(2026, 11, 16), "hol.json")            # Monday; Tuesday is a holiday
    assert st.d["cooldown_until"] == "2026-11-20"                                # Wed and Thu off, back on Friday
