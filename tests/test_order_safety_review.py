"""Four real-money defects found by an independent review on 20 Nov 2026, each reproduced against the fake Zerodha
before the fix and checked here for the correct behaviour. No real order is ever sent from a test."""

import bot
import config as C
from test_execution_safety import Zerodha, live, quick  # noqa: F401 - fixtures used by name
from test_manual_shares import Kite, entered, wire, you_buy, your_stop  # noqa: F401


def working_bot_sells(z):
    return [o for o in z.book.values() if o["transaction_type"] == "SELL" and str(o.get("tag") or "").startswith("orb")
            and o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED")]


def working_stops(z):
    return [o for o in z.book.values() if o["order_type"] == "SL" and o["status"] == "TRIGGER PENDING"]


# A. the daily loss limit used to send a second sale while the first closing order was still working
def test_A_the_daily_limit_never_sells_shares_a_working_exit_order_is_already_selling(live):
    b, z, pos, q = entered(live)

    def cancel(variety, oid):                    # the stop cancels at once; the exit's own cancel is not confirmed
        z.cancelled.append(oid)
        if z.book[oid]["order_type"] == "SL" and z.book[oid]["status"] == "TRIGGER PENDING":
            z.book[oid]["status"] = "CANCELLED"
    z.cancel_order = cancel
    z.fill = [0]                                 # the closing order rests unfilled
    b.close_position("SBIN", 100.5, "target", bot.now_ist())
    assert "SBIN" in b.state.positions and b.broker.closing_pending("SBIN")
    b.state.d["day_pnl"] = -b.state.d["day_start_equity"] * C.MAX_DAILY_LOSS_PCT / 100 - 1.0     # a losing day
    z.fill = [0]
    b.cycle()
    assert sum(o["quantity"] for o in working_bot_sells(z)) <= q          # never more offered than is open


def test_A_the_exit_itself_counts_the_bots_working_exit_orders(live):
    b, z, pos, q = entered(live)
    z.book["x1"] = {"order_id": "x1", "tag": "orbXworking000001", "status": "OPEN", "tradingsymbol": "SBIN",
                    "transaction_type": "SELL", "quantity": q, "order_type": "LIMIT", "filled_quantity": 0,
                    "average_price": 0.0}
    b.close_position("SBIN", 100.5, "time stop", bot.now_ist())
    new_sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL" and o["order_id"] != "x1"]
    assert new_sells == []
    assert any("already being sold by a working exit order" in t for _, t in b.broker.messages)


# B. a stop used to be placed again for shares that were no longer open, and kept at the record's size
def test_B_no_stop_is_placed_for_shares_already_sold_by_hand(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", q, 100.9, side="SELL")    # the bot's shares sold in Kite...
    z.book[pos["sl_order_id"]]["status"] = "CANCELLED"     # ...and its stop cancelled
    z.net = {}
    sent = len(z.sent("SL"))
    b.cycle()
    assert len(z.sent("SL")) == sent and working_stops(z) == []      # no new stop for shares that are gone
    assert "SBIN" not in b.state.positions                           # compared at once: booked and dropped
    assert any("shows none of this position's shares open" in e for e in b.events)


def test_B_the_stop_shrinks_to_the_shares_that_are_open(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 3, 100.9, side="SELL")    # 3 of the bot's shares sold in Kite
    b.broker.ensure_stop("SBIN", pos)
    assert z.book[pos["sl_order_id"]]["quantity"] == q - 3


def test_B_a_working_stop_with_nothing_left_to_protect_is_cancelled(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", q, 100.9, side="SELL")    # everything sold in Kite, the bot's stop still working
    state, _ = b.broker.ensure_stop("SBIN", pos)
    assert state == "none_open" and z.book[pos["sl_order_id"]]["status"] == "CANCELLED"


def test_B_a_dropped_position_has_its_stop_cancelled(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", q, 100.9, side="SELL")
    z.net = {}
    b.reconcile(startup=False)
    assert "SBIN" not in b.state.positions and working_stops(z) == []


# C. a failed cancel when a position got smaller used to leave two stops working for the rest of the day
def test_C_a_failed_cancel_never_leaves_two_stops(live):
    b, z, pos, q = entered(live)
    old = pos["sl_order_id"]
    you_buy(z, "SBIN", 3, 100.9, side="SELL")
    z.net = {"SBIN": q - 3}
    real = z.cancel_order
    calls = {"n": 0}

    def flaky(variety, oid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("timeout")
        return real(variety, oid)
    z.cancel_order = flaky
    b.reconcile(startup=False)
    b.cycle()
    b.reconcile(startup=False)
    stops = working_stops(z)
    assert len(stops) == 1 and stops[0]["quantity"] == q - 3
    assert b.state.positions["SBIN"]["sl_order_id"] == stops[0]["order_id"]
    assert stops[0]["order_id"] == old or z.book[old]["status"] == "CANCELLED"


# D. your own stop for every share used to make the bot's exit sell nothing
def test_D_your_order_counts_only_against_your_shares(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    your_stop(z, "SBIN", q + 9, 99.0)            # your own stop covers your 9 AND the bot's shares
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    b.broker.ensure_stop("SBIN", pos)
    assert z.book[pos["sl_order_id"]]["quantity"] == q                    # the bot's stop: its own shares
    assert any(f"your own order(s) in Kite would sell {q + 9} share(s), but you hold 9" in t for _, t in b.broker.messages)
    b.close_position("SBIN", 101.0, "square-off", bot.now_ist())
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == q and "SBIN" not in b.state.positions
    assert z.book["ys1"]["status"] == "TRIGGER PENDING" and "ys1" not in z.cancelled     # your order: never touched
