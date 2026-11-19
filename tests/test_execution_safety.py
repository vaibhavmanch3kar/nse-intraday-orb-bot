"""Execution safety (2.0): every order is tagged and recorded; a lost reply is not "nothing was sent"; an order id
is not a fill; an order left open-ended is watched to its end and late fills are closed; the margin is checked
first; the stop-loss is verified when placed and on every round; and the bot's record is compared with Zerodha's.
All against a fake Zerodha: no real order is ever sent from a test."""

import csv
import datetime as dt
import os

import pytest

import bot
import config as C
import data_kite
import data_yahoo
import livemode
import orders
from broker import LiveBroker, OrderNotSent, OrderUncertain
from conftest import IST, bar, history


class InputException(Exception):
    """Named like kiteconnect's: Zerodha said no."""


class Zerodha:
    """A small stand-in for Kite: an order book, and switches for the ways the real one misbehaves."""
    VARIETY_REGULAR, EXCHANGE_NSE, PRODUCT_MIS = "regular", "NSE", "MIS"
    ORDER_TYPE_LIMIT, ORDER_TYPE_SL, VALIDITY_DAY = "LIMIT", "SL", "DAY"

    def __init__(self):
        self.book = {}                 # order id -> order
        self.fill = []                 # for each LIMIT order in turn: shares to fill (None = all)
        self.refuse = None             # an exception: Zerodha says no, nothing is created
        self.lose_reply = 0            # this many orders ARE created but the reply is lost
        self.drop_send = 0             # this many orders are NOT created and the send fails
        self.book_down = False         # the order book can't be read
        self.status_down = 0           # this many status questions fail
        self.cancel_slow = False       # a cancel is accepted but not confirmed yet
        self.sl_status = "TRIGGER PENDING"
        self.free, self.cancelled, self.net = 1e9, [], {}

    def place_order(self, **p):
        if self.refuse:
            raise self.refuse
        if self.drop_send:
            self.drop_send -= 1
            raise ConnectionError("send failed")
        oid = f"o{len(self.book) + 1}"
        o = {"order_id": oid, "tag": p["tag"], "tradingsymbol": p["tradingsymbol"],
             "transaction_type": p["transaction_type"], "quantity": p["quantity"], "order_type": p["order_type"],
             "price": p.get("price"), "trigger_price": p.get("trigger_price"), "filled_quantity": 0,
             "average_price": 0.0}
        if p["order_type"] == "SL":
            o["status"] = self.sl_status
        else:
            plan = self.fill.pop(0) if self.fill else None
            q = p["quantity"] if plan is None else plan
            o.update(status="COMPLETE" if q == p["quantity"] else "OPEN", filled_quantity=q,
                     average_price=p["price"] if q else 0.0)
        self.book[oid] = o
        if self.lose_reply:
            self.lose_reply -= 1
            raise ConnectionError("reply lost")
        return oid

    def instruments(self, exchange=None):
        """Kite's instrument list: a 0.05 price step for any stock a test trades."""
        return [{"tradingsymbol": s, "tick_size": 0.05} for s in sorted(set(C.WATCHLIST) | {"SBIN", "INFY"})]

    def order_history(self, oid):
        if self.status_down:
            self.status_down -= 1
            raise ConnectionError("status")
        return [dict(self.book[oid])]

    def cancel_order(self, variety, oid):
        self.cancelled.append(oid)
        if not self.cancel_slow and self.book[oid]["status"] in ("OPEN", "TRIGGER PENDING"):
            self.book[oid]["status"] = "CANCELLED"

    def orders(self):
        if self.book_down:
            raise ConnectionError("book")
        return [dict(o) for o in self.book.values()]

    def positions(self):
        return {"day": [{"tradingsymbol": s, "product": "MIS", "exchange": "NSE", "quantity": q}
                        for s, q in self.net.items()]}

    def order_margins(self, params):
        return [{"total": sum(p["quantity"] * p["price"] for p in params) / 5}]

    def margins(self, segment=None):
        return {"available": {"live_balance": self.free}}

    def sent(self, kind=None):
        return [o for o in self.book.values() if kind is None or o["order_type"] == kind]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(C, "ORDER_FILL_TIMEOUT_SEC", 0)
    monkeypatch.setattr(C, "CANCEL_WAIT_SEC", 2)
    monkeypatch.setattr(C, "EXIT_RETRIES", 3)


def lb(z):
    return LiveBroker(z, sleep=lambda s: None)


# ---------------------------------------------------------------- tags and the order record
def test_every_order_has_its_own_tag_and_is_recorded_before_and_after():
    z = Zerodha()
    b = lb(z)
    qty, avg, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    entry, stop = z.sent()
    assert qty == 10 and extra["sl_order_id"] == stop["order_id"]
    assert entry["tag"] != stop["tag"] and entry["tag"].startswith("orbE") and stop["tag"].startswith("orbS")
    for o in (entry, stop):
        assert len(o["tag"]) <= 20 and o["tag"].isalnum() and orders.is_ours(o["tag"])
    ev = b.journal.events()
    assert [e["status"] for e in ev if e["tag"] == entry["tag"]] == ["SENDING", "SENT", "COMPLETE"]
    assert [e["status"] for e in ev if e["tag"] == stop["tag"]] == ["SENDING", "SENT", "TRIGGER PENDING"]
    first = ev[0]
    assert (first["role"], first["symbol"], first["side"], first["qty"], first["type"]) == ("E", "SBIN", "BUY", 10, "LIMIT")
    assert b.journal.latest()[entry["tag"]]["filled"] == 10 and b.journal.unfinished().keys() == {stop["tag"]}


def test_tags_are_unique_and_roles_readable():
    tags = {orders.new_tag(r) for r in "ESXF" for _ in range(500)}
    assert len(tags) == 2000 and orders.role_of("orbXabc") == "X" and orders.role_of("orbbot") == ""
    assert orders.is_ours("orbbot") and not orders.is_ours("manual") and not orders.is_ours(None)
    with pytest.raises(ValueError):
        orders.new_tag("Q")


def test_a_cut_off_line_in_the_record_does_not_lose_the_rest(tmp_path):
    j = orders.Journal("live")
    j.record("orbEaaa", "SENDING", symbol="ITC")
    with open(j.path(), "a", encoding="utf-8") as f:
        f.write('{"at": "2026-11-19T10:00:00", "tag": "orbEbbb", "sta')          # the crash happened here
    assert [e["tag"] for e in j.events()] == ["orbEaaa"]


# ---------------------------------------------------------------- a lost reply, a refused order, no answer at all
def test_lost_reply_is_found_by_its_tag_and_nothing_is_sent_twice():
    z = Zerodha()
    z.lose_reply = 1
    b = lb(z)
    qty, avg, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 10 and extra["sl_order_id"] and b.uncertain is None
    assert len(z.sent("LIMIT")) == 1 and len(z.sent("SL")) == 1                 # one entry, one stop: no duplicate
    assert any("found in the order book" in (e.get("note") or "") for e in b.journal.events())


def test_refused_order_is_simply_not_sent():
    z = Zerodha()
    z.refuse = InputException("Insufficient funds")
    b = lb(z)
    qty, avg, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 0 and "did not take the entry order" in extra["reason"] and "Insufficient funds" in extra["reason"]
    assert z.book == {} and b.uncertain is None and b.lost == {}
    assert b.journal.events()[-1]["status"] == "NOT SENT"


def test_failed_send_that_never_arrived_is_watched_then_written_off():
    z = Zerodha()
    z.drop_send = 1
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 0 and z.book == {} and b.uncertain is None and len(b.lost) == 1     # the book was readable: no
    for _ in range(5):
        b.settle()
    assert b.lost == {} and b.journal.events()[-1]["note"].startswith("not in the order book after")


def test_no_answer_and_no_order_book_means_uncertain_and_no_new_entry():
    z = Zerodha()
    z.drop_send, z.book_down = 1, True
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 0 and "no answer from Zerodha" in extra["reason"] and b.uncertain
    qty, _, extra = b.enter("ITC", "LONG", 5, 300.0, 297.0)                      # refused without sending anything
    assert qty == 0 and "order state uncertain" in extra["reason"] and z.book == {}
    b.settle()
    assert b.uncertain                                                           # still can't ask
    z.book_down = False
    for _ in range(5):
        b.settle()
    assert b.uncertain is None and any("clear again" in m for _, m in b.messages)


def test_an_order_that_turns_up_late_and_filled_is_closed_again():
    z = Zerodha()
    z.book_down = True
    b = lb(z)
    with pytest.raises(OrderUncertain):
        z.lose_reply = 1
        b._place("SBIN", "BUY", 10, "LIMIT", price=801.6, role="E")             # it filled, but we never heard
    assert b.uncertain and len(z.sent()) == 1
    z.book_down = False
    b.settle()                                                                   # found: now an open-ended order
    b.settle()                                                                   # final and filled: shares we never meant to hold
    closing = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(closing) == 1 and closing[0]["quantity"] == 10 and closing[0]["tag"].startswith("orbX")
    assert b.uncertain is None and any("filled late" in m for _, m in b.messages)


# ---------------------------------------------------------------- an order id is not a fill
def test_cancel_not_confirmed_keeps_the_order_under_watch_and_closes_a_late_fill():
    z = Zerodha()
    z.fill, z.cancel_slow = [4], True                       # 4 of 10 filled; the cancel is not confirmed
    b = lb(z)
    qty, avg, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    entry = z.sent("LIMIT")[0]
    assert qty == 4 and z.sent("SL")[0]["quantity"] == 4 and b.uncertain and len(b.unsettled) == 1
    assert entry["order_id"] in z.cancelled
    z.book[entry["order_id"]].update(status="COMPLETE", filled_quantity=7)       # 3 more filled meanwhile
    b.settle()
    late = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(late) == 1 and late[0]["quantity"] == 3                           # exactly the extra shares
    assert b.uncertain is None and b.unsettled == {}


def test_status_questions_that_fail_do_not_lose_the_order():
    z = Zerodha()
    b = lb(z)
    z.status_down = 2                                        # the first two questions fail
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 10 and extra["sl_order_id"] and len(z.sent()) == 2


# ---------------------------------------------------------------- the stop-loss
def test_stop_rejected_by_zerodha_after_it_was_accepted_closes_the_position():
    z = Zerodha()
    z.sl_status = "REJECTED"
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 0 and "position was closed" in extra["reason"]
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 10


def test_if_the_close_after_a_failed_stop_is_partial_the_rest_stays_on_record():
    z = Zerodha()
    z.sl_status = "REJECTED"
    z.fill = [None, 6, 0, 0]                                 # entry fills; the closing orders fill 6, then nothing
    b = lb(z)
    qty, avg, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 4 and extra == {"sl_order_id": None}       # 4 shares still open: the bot must keep managing them
    assert any("WITHOUT an exchange stop" in m for _, m in b.messages)


def test_ensure_stop_every_state():
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    pos = {"direction": "LONG", "qty": 10, "stop": 790.0, "sl_order_id": extra["sl_order_id"]}
    assert b.ensure_stop("SBIN", pos) == ("ok", None)
    z.status_down = 3
    assert b.ensure_stop("SBIN", pos) == ("unknown", None)                       # can't ask: nothing is sent
    z.book[pos["sl_order_id"]]["status"] = "CANCELLED"                           # e.g. cancelled in the Kite app
    state, new = b.ensure_stop("SBIN", pos)
    assert state == "replaced" and new != pos["sl_order_id"] and z.book[new]["trigger_price"] == 790.0
    pos["sl_order_id"] = new
    z.book[new]["status"] = "REJECTED"
    z.refuse = InputException("not allowed")
    assert b.ensure_stop("SBIN", pos) == ("missing", None)
    z.refuse = None
    z.book[new].update(status="COMPLETE", filled_quantity=10)
    assert b.ensure_stop("SBIN", pos) == ("filled", None)
    assert b.ensure_stop("SBIN", {**pos, "sl_order_id": None})[0] == "replaced"  # no stop on record: place one


def test_exit_is_postponed_when_the_stop_cant_be_asked_about():
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    n = len(z.book)
    z.status_down = 3
    done, _ = b.exit("SBIN", {"direction": "LONG", "qty": 10, "sl_order_id": extra["sl_order_id"]}, 805.0, "target")
    assert done == 0 and len(z.book) == n                     # no closing order: it could have sold twice
    assert z.book[extra["sl_order_id"]]["status"] == "TRIGGER PENDING"            # the exchange stop stays


def test_exit_never_stacks_a_second_order_on_one_that_is_not_final():
    z = Zerodha()
    b = lb(z)
    z.fill, z.cancel_slow = [3], True
    done, _ = b.exit("SBIN", {"direction": "LONG", "qty": 10}, 805.0, "target")
    assert done == 3 and len(z.sent("LIMIT")) == 1 and b.uncertain


def test_exit_tries_again_when_zerodha_has_no_such_order():
    z = Zerodha()
    z.drop_send = 2
    b = lb(z)
    done, _ = b.exit("SBIN", {"direction": "LONG", "qty": 10}, 805.0, "target")
    assert done == 10 and len(z.sent("LIMIT")) == 1


# ---------------------------------------------------------------- margin before the entry
def test_margin_is_checked_with_zerodha_before_an_entry(monkeypatch):
    z = Zerodha()
    z.free = 1000.0                                          # 10 x 800 / 5 = 1,600 needed
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 0 and "needs ₹1,600 margin" in extra["reason"] and "₹1,000 is free" in extra["reason"]
    assert z.book == {}
    z.order_margins = lambda p: (_ for _ in ()).throw(ConnectionError("down"))
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 0 and "could not check the margin" in extra["reason"] and z.book == {}    # can't check: no order
    monkeypatch.setattr(C, "MARGIN_CHECK", False)
    assert b.enter("SBIN", "LONG", 10, 800.0, 790.0)[0] == 10


# ---------------------------------------------------------------- the bot's record against Zerodha
def test_recover_resolves_unfinished_orders_and_cancels_leftovers():
    z = Zerodha()
    b = lb(z)
    b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    entry, stop = z.sent()
    z.fill = [0]
    z.cancel_slow = True
    b._place("ITC", "BUY", 5, "LIMIT", price=300.0, role="E")                    # left working by a crash
    z.cancel_slow = False
    z.net = {"SBIN": 10}
    fresh = LiveBroker(z, sleep=lambda s: None)                                  # the bot after a restart
    rep = fresh.recover({"SBIN": {"direction": "LONG", "qty": 10, "sl_order_id": stop["order_id"]}})
    assert len(rep["cancelled"]) == 1 and "ITC BUY" in rep["cancelled"][0] and "entry" in rep["cancelled"][0]
    assert z.book[stop["order_id"]]["status"] == "TRIGGER PENDING"               # the position's stop is untouched
    assert rep["drop"] == [] and rep["resize"] == {} and rep["problems"] == []
    assert fresh.journal.latest()[stop["tag"]]["note"] == "checked at start"


def test_recover_drop_resize_mismatch_and_foreign_positions():
    z = Zerodha()
    b = lb(z)
    b.enter("AXISBANK", "LONG", 2, 1300.0, 1290.0)                               # the bot's own filled order
    z.net = {"SBIN": 6, "INFY": -3, "AXISBANK": 2, "TCS": 1, "LT": 0}
    mine = {"SBIN": {"direction": "LONG", "qty": 10}, "ITC": {"direction": "LONG", "qty": 5},
            "INFY": {"direction": "LONG", "qty": 3}}
    rep = b.recover(mine, startup=False)
    assert rep["drop"] == ["ITC"] and rep["resize"] == {"SBIN": 6}
    assert any(p.startswith("INFY: the bot has +3 shares on record, Zerodha shows -3") for p in rep["problems"])
    assert any(p.startswith("AXISBANK: Zerodha shows +2 MIS shares from the bot's own orders") for p in rep["problems"])
    assert any("TCS: +1 MIS shares at Zerodha are not the bot's" in n for n in rep["notes"])
    assert len(z.sent("LIMIT")) == 1                                             # recover sent no order at all


def test_recover_needs_zerodha_to_answer():
    z = Zerodha()
    z.book_down = True
    with pytest.raises(ConnectionError):
        lb(z).recover({})


# ---------------------------------------------------------------- in the running bot
def entry_day():
    hist, day = history()
    return hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
                   bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
                   bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000)], day


@pytest.fixture
def live(monkeypatch):
    """A REAL MONEY bot wired to the fake Zerodha."""
    bars, day = entry_day()
    now = dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars, bars[-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: now)
    monkeypatch.setattr(livemode, "_now", lambda: now)
    monkeypatch.setattr(data_kite, "candle_hold", lambda now=None: 0.0)
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    livemode.arm(login_at=now)
    b = bot.Bot()
    assert b.live
    z = Zerodha()
    b.kite, b.broker = z, LiveBroker(z, sleep=lambda s: None)
    b.state.d.update(cash=100000.0, day_start_equity=100000.0, balance_day=day.isoformat())
    b.balance_ok = True
    return b, z


def test_bot_enters_through_the_fake_and_records_the_stop(live):
    b, z = live
    b.cycle()
    pos = b.state.positions["SBIN"]
    assert pos["sl_order_id"] == z.sent("SL")[0]["order_id"] and pos["qty"] == z.sent("LIMIT")[0]["quantity"]


def test_bot_says_why_an_entry_was_not_filled(live):
    b, z = live
    z.free = 1.0
    b.cycle()
    assert b.state.positions == {} and "skip: Zerodha needs" in b.rows["SBIN"]["reason"]
    assert any("entry not filled - Zerodha needs" in e for e in b.events)


def test_uncertain_order_state_holds_new_entries(live):
    b, z = live
    b.broker.uncertain = "SBIN: order o9 is neither filled nor cancelled yet"
    b.broker.unsettled["orbEx"] = {"order_id": "o9", "role": "E", "symbol": "SBIN", "side": "BUY", "qty": 1, "accounted": 0}
    z.book["o9"] = {"order_id": "o9", "tag": "orbEx", "status": "OPEN", "filled_quantity": 0, "average_price": 0,
                    "tradingsymbol": "SBIN", "transaction_type": "BUY", "quantity": 1, "order_type": "LIMIT"}
    z.cancel_slow = True
    b.cycle()
    assert b.state.positions == {} and "skip: order state uncertain" in b.rows["SBIN"]["reason"]
    assert len(z.sent()) == 1                                                    # nothing new was sent


def test_bot_replaces_a_stop_that_went_missing(live):
    b, z = live
    b.cycle()
    old = b.state.positions["SBIN"]["sl_order_id"]
    z.book[old]["status"] = "CANCELLED"
    z.net = {"SBIN": b.state.positions["SBIN"]["qty"]}
    b.cycle()
    new = b.state.positions["SBIN"]["sl_order_id"]
    assert new != old and z.book[new]["status"] == "TRIGGER PENDING"
    assert any("stop-loss was missing and has been placed again" in e for e in b.events)


def test_bot_closes_a_position_whose_stop_cannot_be_placed(live):
    b, z = live
    b.cycle()
    qty = b.state.positions["SBIN"]["qty"]
    z.book[b.state.positions["SBIN"]["sl_order_id"]]["status"] = "CANCELLED"
    z.net = {"SBIN": qty}
    z.sl_status = "REJECTED"
    b.cycle()
    assert "SBIN" not in b.state.positions
    assert any(o["transaction_type"] == "SELL" and o["order_type"] == "LIMIT" and o["quantity"] == qty for o in z.sent())


def test_partial_exit_keeps_the_rest_on_record(live):
    b, z = live
    b.cycle()
    qty = b.state.positions["SBIN"]["qty"]
    z.fill = [qty - 3, 0, 0]                                  # the closing orders fill all but 3 shares
    b.close_position("SBIN", 101.0, "target", bot.now_ist())
    pos = b.state.positions["SBIN"]
    assert pos["qty"] == 3 and pos["sl_order_id"] is None     # it used to vanish from the bot's record here
    assert any("3 shares are still open" in e for e in b.events)
    z.net = {"SBIN": 3}
    b.cycle()
    assert z.book[b.state.positions["SBIN"]["sl_order_id"]]["quantity"] == 3     # a new stop for what is left


def test_bot_reconciles_at_start_and_holds_entries_on_a_mismatch(live):
    b, z = live
    b.state.positions["ITC"] = {"direction": "LONG", "qty": 5, "entry": 300.0, "stop": 297.0, "opened": str(bot.now_ist())}
    b.state.positions["INFY"] = {"direction": "LONG", "qty": 3, "entry": 1100.0, "stop": 1090.0, "opened": str(bot.now_ist())}
    z.net = {"INFY": -3}
    b.reconcile(startup=True)
    assert "ITC" not in b.state.positions                                        # Zerodha no longer holds it
    assert b.recon["ok"] and b.recon["dropped"] == ["ITC"] and len(b.recon["problems"]) == 1
    assert b.entry_hold().startswith("the bot's record does not match Zerodha (INFY")
    said = sum("DOES NOT MATCH ZERODHA" in e for e in b.events)
    b.reconcile(startup=False)
    assert sum("DOES NOT MATCH ZERODHA" in e for e in b.events) == said == 1     # said once
    z.net = {"INFY": 3}
    b.reconcile(startup=False)
    assert b.entry_hold() is None and any("matches Zerodha again" in e for e in b.events)


def test_reconcile_survives_zerodha_not_answering(live):
    b, z = live
    z.book_down = True
    b.reconcile(startup=True)
    assert b.recon["ok"] is False and "ConnectionError" in b.recon["error"]
    assert any("could not compare the bot's positions with Zerodha" in e for e in b.events)


def test_no_paper_path_touches_any_of_this(monkeypatch):
    b = bot.Bot()
    assert not b.live and b.entry_hold() is None
    b.reconcile()
    assert b.recon == {} and not hasattr(b.broker, "recover")


# ---------------------------------------------------------------- best / worst price of a real-money trade
def test_real_money_trade_logs_its_best_and_worst_price(live, monkeypatch):
    b, z = live
    b.cycle()
    pos = b.state.positions["SBIN"]
    entry, stop = pos["entry"], pos["stop"]
    assert pos["best_price"] == pos["worst_price"] == entry                    # tracked from the fill
    feed = data_yahoo.fetch
    price = [entry + 0.7]
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (feed(s, i, r)[0], price[0], None))
    b.cycle()                                                                  # the price read this round: +0.7
    price[0] = entry - 0.4
    b.cycle()                                                                  # ... then -0.4
    pos = b.state.positions["SBIN"]
    assert pos["best_price"] == entry + 0.7 and pos["worst_price"] == entry - 0.4
    # the exchange stop fills; the price read in that round (far below) came after the exit and is not counted
    z.book[pos["sl_order_id"]].update(status="COMPLETE", filled_quantity=pos["qty"], average_price=stop - 0.05)
    price[0] = stop - 3.0
    b.cycle()
    assert "SBIN" not in b.state.positions
    with open(os.path.join(C.DATA_DIR, "trades_live.csv"), newline="", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]
    assert row["reason"] == "exchange stop"
    assert float(row["best_price"]) == round(entry + 0.7, 2) and float(row["worst_price"]) == round(stop - 0.05, 2)
    assert float(row["best_r"]) > 0 and float(row["worst_r"]) <= -1.0


# ---------------------------------------------------------------- P2: an exit after the stop has partly filled
def test_exit_after_a_partly_filled_stop_closes_only_the_rest():
    """Red-team finding, 19 Nov 2026: the stop sold 4 of 10, then the bot's exit sold all 10 again."""
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    sl = extra["sl_order_id"]
    z.book[sl].update(status="OPEN", filled_quantity=4, average_price=789.5)   # triggered, 4 shares sold so far
    done, avg = b.exit("SBIN", {"direction": "LONG", "qty": 10, "sl_order_id": sl}, 786.0, "time stop")
    closing = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(closing) == 1 and closing[0]["quantity"] == 6                   # only the other 6
    assert done == 10 and abs(avg - (4 * 789.5 + 6 * closing[0]["price"]) / 10) < 1e-9
    assert z.book[sl]["status"] == "CANCELLED"
    assert any("had already sold 4 of 10" in text for _, text in b.messages)


def test_exit_sends_nothing_while_the_stop_cancel_is_not_confirmed():
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    n = len(z.book)
    z.cancel_slow = True                                                       # accepted, not confirmed
    done, _ = b.exit("SBIN", {"direction": "LONG", "qty": 10, "sl_order_id": extra["sl_order_id"]}, 805.0, "target")
    assert done == 0 and len(z.book) == n                                      # no closing order next to a live stop
    assert z.book[extra["sl_order_id"]]["status"] == "TRIGGER PENDING"
    assert any("cancel of the stop-loss order is not confirmed" in text for _, text in b.messages)


def test_exit_waits_for_a_slow_cancel_before_it_sells():
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    sl = extra["sl_order_id"]
    asked = []
    real = z.order_history

    def slow(oid):                                    # the cancel shows only on the second look
        if oid == sl and z.cancelled:
            asked.append(oid)
            if len(asked) >= 2:
                z.book[sl]["status"] = "CANCELLED"
        return real(oid)
    z.cancel_slow, z.order_history = True, slow
    done, _ = b.exit("SBIN", {"direction": "LONG", "qty": 10, "sl_order_id": sl}, 805.0, "target")
    assert done == 10 and len(asked) >= 2


def test_a_stop_that_fills_while_being_cancelled_is_the_exit():
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    sl = extra["sl_order_id"]
    n = len(z.book)

    def cancel(variety, oid):                         # too late: the exchange had filled it
        z.book[oid].update(status="COMPLETE", filled_quantity=10, average_price=789.0)
    z.cancel_order = cancel
    done, avg = b.exit("SBIN", {"direction": "LONG", "qty": 10, "sl_order_id": sl}, 805.0, "target")
    assert (done, avg) == (10, 789.0) and len(z.book) == n                     # no second sale


# ---------------------------------------------------------------- P1: a stop that triggers but does not fill
def test_a_triggered_stop_that_has_not_filled_is_not_ok():
    """Red-team finding, 19 Nov 2026: a stop-limit whose trigger was hit but whose limit did not fill (status
    OPEN) counted as a healthy stop, so the position sat unprotected until the 15-minute candle closed."""
    z = Zerodha()
    b = lb(z)
    qty, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    pos = {"direction": "LONG", "qty": 10, "stop": 790.0, "sl_order_id": extra["sl_order_id"]}
    n = len(z.book)
    z.book[pos["sl_order_id"]]["status"] = "OPEN"                                 # triggered, resting unfilled
    assert b.ensure_stop("SBIN", pos) == ("triggered", None)
    z.book[pos["sl_order_id"]].update(status="OPEN", filled_quantity=4)           # ... or partly filled
    assert b.ensure_stop("SBIN", pos) == ("triggered", None)
    z.book[pos["sl_order_id"]].update(status="CANCELLED", filled_quantity=4)      # cancelled after selling 4
    assert b.ensure_stop("SBIN", pos) == ("triggered", None)
    assert len(z.book) == n                               # and never a new stop for all 10 on top of those 4


def test_bot_closes_at_once_when_the_stop_triggered_without_filling(live):
    b, z = live
    b.cycle()
    pos = b.state.positions["SBIN"]
    qty, sl = pos["qty"], pos["sl_order_id"]
    z.book[sl]["status"] = "OPEN"                                                 # the price went through the limit
    z.net = {"SBIN": qty}
    b.cycle()
    assert "SBIN" not in b.state.positions
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == qty and z.book[sl]["status"] == "CANCELLED"
    assert any("stop-loss was triggered but has not filled" in e for e in b.events)
    with open(os.path.join(C.DATA_DIR, "trades_live.csv"), newline="", encoding="utf-8") as f:
        assert list(csv.DictReader(f))[0]["reason"] == "stop triggered, not filled"


def test_bot_closes_only_the_rest_when_the_stop_filled_partly(live):
    b, z = live
    b.cycle()
    pos = b.state.positions["SBIN"]
    qty, sl = pos["qty"], pos["sl_order_id"]
    assert qty >= 2
    z.book[sl].update(status="OPEN", filled_quantity=1, average_price=pos["stop"])
    z.net = {"SBIN": qty - 1}
    b.last_recon = bot.now_ist()                          # (the comparison with Zerodha is P3's subject)
    b.cycle()
    assert "SBIN" not in b.state.positions
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == qty - 1                   # not the full quantity again
