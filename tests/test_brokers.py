"""Live order logic against a fake Kite, plus the paper broker."""

import pytest

import config as C
from broker import LiveBroker, PaperBroker


class FakeKite:
    VARIETY_REGULAR = "regular"
    EXCHANGE_NSE = "NSE"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_SL = "SL"
    VALIDITY_DAY = "DAY"

    def __init__(self, fill_plan=None, fail_sl=False):
        self.orders = {}
        self.placed = []
        self.cancelled = []
        self.fill_plan = list(fill_plan or [])  # per placed non-SL order: qty to fill (None = all)
        self.fail_sl = fail_sl

    def place_order(self, **p):
        if p["order_type"] == "SL" and self.fail_sl:
            raise RuntimeError("SL rejected")
        oid = f"o{len(self.placed) + 1}"
        self.placed.append({**p, "id": oid})
        if p["order_type"] == "SL":
            self.orders[oid] = {"status": "TRIGGER PENDING", "filled_quantity": 0, "average_price": 0}
        else:
            plan = self.fill_plan.pop(0) if self.fill_plan else None
            q = p["quantity"] if plan is None else plan
            status = "COMPLETE" if q == p["quantity"] else "OPEN"
            self.orders[oid] = {"status": status, "filled_quantity": q, "average_price": p["price"]}
        return oid

    def order_history(self, oid):
        return [self.orders[oid]]

    def cancel_order(self, variety, oid):
        self.cancelled.append(oid)
        if self.orders[oid]["status"] in ("OPEN", "TRIGGER PENDING"):
            self.orders[oid]["status"] = "CANCELLED"

    def positions(self):
        return {"day": [{"tradingsymbol": "SBIN", "product": "MIS", "exchange": "NSE", "quantity": 10},
                        {"tradingsymbol": "INFY", "product": "CNC", "exchange": "NSE", "quantity": 5}]}


def lb(kite):
    return LiveBroker(kite, sleep=lambda s: None)


def test_entry_places_limit_then_exchange_stop():
    k = FakeKite()
    qty, avg, extra = lb(k).enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 10 and extra["sl_order_id"] == "o2"
    entry, sl = k.placed
    assert entry["transaction_type"] == "BUY" and entry["order_type"] == "LIMIT" and entry["product"] == "MIS"
    assert entry["price"] == pytest.approx(801.6)          # +0.20% buffer, 0.05 tick
    assert sl["transaction_type"] == "SELL" and sl["order_type"] == "SL"
    assert sl["trigger_price"] == 790.0 and sl["price"] < 790.0
    assert sl["quantity"] == 10


def test_short_entry_stop_is_buy_above():
    k = FakeKite()
    lb(k).enter("SBIN", "SHORT", 4, 800.0, 810.0)
    entry, sl = k.placed
    assert entry["transaction_type"] == "SELL" and entry["price"] < 800
    assert sl["transaction_type"] == "BUY" and sl["trigger_price"] == 810.0 and sl["price"] > 810.0


def test_partial_fill_is_cancelled_and_stop_covers_only_filled(monkeypatch):
    monkeypatch.setattr(C, "ORDER_FILL_TIMEOUT_SEC", 0)
    k = FakeKite(fill_plan=[6])
    qty, _, _ = lb(k).enter("SBIN", "LONG", 10, 800.0, 790.0)
    assert qty == 6 and "o1" in k.cancelled
    assert k.placed[1]["quantity"] == 6


def test_unfilled_entry_places_no_stop(monkeypatch):
    monkeypatch.setattr(C, "ORDER_FILL_TIMEOUT_SEC", 0)
    k = FakeKite(fill_plan=[0])
    assert lb(k).enter("SBIN", "LONG", 10, 800.0, 790.0)[0] == 0
    assert len(k.placed) == 1


def test_stop_order_failure_closes_position():
    k = FakeKite(fail_sl=True)
    assert lb(k).enter("SBIN", "LONG", 10, 800.0, 790.0)[0] == 0
    assert [o["transaction_type"] for o in k.placed] == ["BUY", "SELL"]


def test_exit_cancels_stop_then_sells():
    k = FakeKite()
    b = lb(k)
    qty, avg, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    pos = {"direction": "LONG", "qty": qty, **extra}
    done, px = b.exit("SBIN", pos, 805.0, "target")
    assert done == 10 and "o2" in k.cancelled
    assert k.placed[-1]["transaction_type"] == "SELL" and k.placed[-1]["quantity"] == 10


def test_exit_when_stop_already_filled_sends_nothing():
    k = FakeKite()
    b = lb(k)
    _, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    k.orders["o2"] = {"status": "COMPLETE", "filled_quantity": 10, "average_price": 789.5}
    pos = {"direction": "LONG", "qty": 10, **extra}
    assert b.stopped_out("SBIN", pos) == 789.5
    assert b.exit("SBIN", pos, 790.0, "square-off") == (10, 789.5)
    assert len(k.placed) == 2  # no extra sell


def test_exit_retries_until_filled(monkeypatch):
    monkeypatch.setattr(C, "ORDER_FILL_TIMEOUT_SEC", 0)
    k = FakeKite(fill_plan=[None, 4, 6])  # entry full, exit 4 then 6
    b = lb(k)
    _, _, extra = b.enter("SBIN", "LONG", 10, 800.0, 790.0)
    done, _ = b.exit("SBIN", {"direction": "LONG", "qty": 10, **extra}, 795.0, "square-off")
    assert done == 10
    assert [o["quantity"] for o in k.placed if o["order_type"] == "LIMIT"] == [10, 10, 6]


def test_open_mis_quantities():
    assert lb(FakeKite()).open_mis_quantities() == {"SBIN": 10}


def test_paper_broker_applies_slippage():
    pb = PaperBroker()
    assert pb.enter("X", "LONG", 5, 100.0, 99.0)[1] == 100.05
    assert pb.enter("X", "SHORT", 5, 100.0, 101.0)[1] == 99.95
    assert pb.exit("X", {"direction": "LONG", "qty": 5}, 100.0, "t") == (5, 99.95)
