"""Open positions on the dashboard show the best open profit so far and how much of it has been given back.
Information only: it comes from the best price the bot already records, and nothing trades on it."""

import buildinfo
import web


def pos(**over):
    p = {"symbol": "AXISBANK", "direction": "LONG", "qty": 10, "entry": 1000.0, "stop": 990.0, "price": 1004.0,
         "best_price": 1008.0, "unrealised": 40.0, "r_unreal": 0.4, "held_min": 30}
    p.update(over)
    return p


def test_long_position_that_came_back_from_its_best():
    g = web.giveback(pos())
    assert (round(g["best_r"], 2), round(g["best_inr"], 2)) == (0.8, 80.0)       # best 1008: +8 a share, risk 10
    assert (round(g["back_r"], 2), round(g["back_inr"], 2)) == (0.4, 40.0)       # now 1004: 4 a share given back
    row = web.position_row(pos())
    assert f"Best so far {web.pnl_r(0.8)} (₹80.00) · given back 0.40R (₹40.00)" in row
    assert "the bot does not trade on it" in row


def test_short_position_counts_the_other_way():
    g = web.giveback(pos(direction="SHORT", stop=1010.0, price=997.0, best_price=994.0))
    assert round(g["best_r"], 2) == 0.6 and round(g["back_r"], 2) == 0.3


def test_at_its_best_now_and_never_in_profit():
    assert "at its best now" in web.position_row(pos(price=1008.0))
    # the price now is beyond the recorded best (recorded on candles, the price is newer): nothing "given back"
    g = web.giveback(pos(price=1012.0))
    assert round(g["best_r"], 2) == 1.2 and g["back_r"] == 0.0
    never = web.position_row(pos(price=996.0, best_price=1000.0, unrealised=-40.0, r_unreal=-0.4))
    assert f"Best so far {web.pnl_r(0.0)} (₹0.00)" in never and "· given back" not in never and "at its best" not in never


def test_missing_figures_show_nothing_and_break_nothing():
    for bad in (pos(best_price=None), pos(stop=1000.0), {"symbol": "X", "direction": "LONG"}):
        assert web.giveback(bad) is None
    assert "Best so far" not in web.position_row(pos(best_price=None))


def test_it_is_display_only():
    import os
    assert "web.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}


def test_a_profit_that_turned_into_a_loss_gives_back_only_the_profit():
    g = web.giveback(pos(price=997.0))                       # best was +0.8R, now -0.3R
    assert round(g["best_r"], 2) == 0.8 and round(g["back_r"], 2) == 0.8
