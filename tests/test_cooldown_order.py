"""The cool-down after losses in a row: positions that close at the same moment (the same candle) are counted
losses first, whatever order they are recorded in. Before this, a win recorded between two same-candle losses
wiped the count, so the result depended on recording order and the bot and the backtest could disagree."""

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
