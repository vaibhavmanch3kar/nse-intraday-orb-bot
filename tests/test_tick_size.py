"""Every real order is priced on its own stock's price step (tick).

On 19 Nov 2026 Zerodha refused a real BHARTIARTL entry at 2156.95: "Tick size for this script is 0.10". The bot
rounded every price to 0.05, but NSE sets the step per stock, and 7 of the 12 watchlist stocks moved to 0.10.
The same rounding was used for the stop-loss and the closing order, so a filled entry could have been left with
its stop and its exit both refused. These tests use a Zerodha that refuses an off-step price, as the real one does.
"""

import datetime as dt
import json
import os

import pytest

import config as C
import risk
from broker import LiveBroker, TickSizes

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STEPS = {"BHARTIARTL": 0.10, "KOTAKBANK": 0.05, "TATASTEEL": 0.01, "MRF": 5.0}


class InputException(Exception):
    """Named like kiteconnect's: Zerodha said no."""


def on_step(price, tick):
    return abs(round(price / tick) * tick - price) < 1e-6


class Zerodha:
    VARIETY_REGULAR, EXCHANGE_NSE, PRODUCT_MIS = "regular", "NSE", "MIS"
    ORDER_TYPE_LIMIT, ORDER_TYPE_SL, VALIDITY_DAY = "LIMIT", "SL", "DAY"

    def __init__(self, steps=None, list_fails=False):
        self.steps = dict(STEPS if steps is None else steps)
        self.list_fails = list_fails
        self.list_calls = 0
        self.book, self.placed, self.refused = {}, [], []

    def instruments(self, exchange=None):
        self.list_calls += 1
        if self.list_fails:
            raise ConnectionError("proxy down")
        return [{"tradingsymbol": s, "tick_size": t, "segment": "NSE"} for s, t in self.steps.items()]

    def place_order(self, **p):
        tick = self.steps[p["tradingsymbol"]]
        for key in ("price", "trigger_price"):
            if p.get(key) is not None and not on_step(p[key], tick):
                self.refused.append(p)
                raise InputException(f"Tick size for this script is {tick:.2f}. Kindly enter price in the "
                                     f"multiple of tick size for this script")
        oid = f"o{len(self.book) + 1}"
        if p["order_type"] == "SL":
            o = {"status": "TRIGGER PENDING", "filled_quantity": 0, "average_price": 0.0}
        else:
            o = {"status": "COMPLETE", "filled_quantity": p["quantity"], "average_price": p["price"]}
        self.book[oid] = {**o, "order_id": oid, "tag": p.get("tag")}
        self.placed.append({**p, "id": oid})
        return oid

    def order_history(self, oid):
        return [self.book[oid]]

    def cancel_order(self, variety, oid):
        if self.book[oid]["status"] in ("OPEN", "TRIGGER PENDING"):
            self.book[oid]["status"] = "CANCELLED"

    def orders(self):
        return list(self.book.values())

    def order_margins(self, params):
        return [{"total": 1.0}]

    def margins(self, segment=None):
        return {"available": {"live_balance": 1e9}}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(C, "ORDER_FILL_TIMEOUT_SEC", 0)
    monkeypatch.setattr(C, "CANCEL_WAIT_SEC", 1)
    monkeypatch.setattr(C, "EXIT_RETRIES", 3)


def lb(z):
    return LiveBroker(z, sleep=lambda s: None)


# ---------------------------------------------------------------- rounding
def test_round_tick_uses_the_step_it_is_given():
    assert risk.round_tick(2156.95, True, 0.10) == 2157.0          # the refused price of 19 Nov, rounded up
    assert risk.round_tick(2156.95, False, 0.10) == 2156.9
    assert risk.round_tick(100.01, True, 0.05) == 100.05
    assert risk.round_tick(100.013, False, 0.01) == 100.01
    assert risk.round_tick(98123.0, True, 5.0) == 98125.0


def test_a_price_already_on_the_step_stays_where_it_is():
    # 2157.0 / 0.1 is 21569.999999999996 in floats: without care, rounding down would give 2156.9
    for price, tick in ((2157.0, 0.10), (1234.5, 0.05), (0.3, 0.10), (100.07, 0.01), (2710.0, 0.10)):
        assert risk.round_tick(price, True, tick) == price
        assert risk.round_tick(price, False, tick) == price


def test_rounding_never_crosses_to_the_wrong_side():
    for i in range(2000):
        price = 1000 + i * 0.0137
        for tick in (0.01, 0.05, 0.10, 0.5):
            up, down = risk.round_tick(price, True, tick), risk.round_tick(price, False, tick)
            assert on_step(up, tick) and on_step(down, tick)
            assert down <= price + 1e-9 <= up + 2e-9 and up - down <= tick + 1e-9


# ---------------------------------------------------------------- the orders Zerodha sees
def test_the_refused_bharti_entry_now_goes_through_with_its_stop():
    z = Zerodha()
    qty, avg, extra = lb(z).enter("BHARTIARTL", "LONG", 1, 2150.10, 2135.10)
    assert qty == 1 and extra["sl_order_id"] and not z.refused
    entry, sl = z.placed
    assert on_step(entry["price"], 0.10) and entry["price"] >= 2150.10
    assert on_step(sl["trigger_price"], 0.10) and on_step(sl["price"], 0.10)
    assert sl["trigger_price"] <= 2135.10                         # a long's stop is never moved closer to the price


def test_every_order_of_a_trade_is_on_its_stocks_step():
    for sym, ref, stop, direction in (("BHARTIARTL", 2144.05, 2135.15, "LONG"), ("BHARTIARTL", 2144.05, 2152.95, "SHORT"),
                                      ("KOTAKBANK", 500.95, 494.12, "LONG"), ("TATASTEEL", 151.237, 152.391, "SHORT")):
        z = Zerodha()
        b = lb(z)
        qty, avg, extra = b.enter(sym, direction, 3, ref, stop)
        assert qty == 3, (sym, z.refused)
        done, _ = b.exit(sym, {"direction": direction, "qty": 3, "sl_order_id": extra["sl_order_id"]},
                         ref * 1.003, "time stop")
        assert done == 3 and not z.refused
        for o in z.placed:
            for key in ("price", "trigger_price"):
                if o.get(key) is not None:
                    assert on_step(o[key], STEPS[sym]), (sym, key, o[key])


def test_a_short_stop_is_rounded_away_from_the_price():
    z = Zerodha()
    lb(z).enter("BHARTIARTL", "SHORT", 1, 2144.05, 2152.93)
    sl = z.placed[1]
    assert sl["transaction_type"] == "BUY" and sl["trigger_price"] == 2153.0 and sl["price"] >= sl["trigger_price"]


# ---------------------------------------------------------------- when the steps can't be read
def test_no_entry_is_sent_when_the_step_is_not_known():
    z = Zerodha(list_fails=True)
    qty, avg, extra = lb(z).enter("BHARTIARTL", "LONG", 1, 2150.10, 2135.10)
    assert qty == 0 and "price step" in extra["reason"] and z.placed == []


def test_a_closing_order_is_still_sent_when_the_step_is_not_known():
    z = Zerodha(list_fails=True)
    b = lb(z)
    done, avg = b.exit("BHARTIARTL", {"direction": "LONG", "qty": 2}, 2150.13, "square-off")
    assert done == 2 and on_step(z.placed[0]["price"], C.FALLBACK_TICK)
    assert any("price step is not known" in text for _, text in b.messages)


def test_a_stock_missing_from_the_list_gets_no_entry():
    z = Zerodha(steps={"KOTAKBANK": 0.05})
    qty, avg, extra = lb(z).enter("BHARTIARTL", "LONG", 1, 2150.10, 2135.10)
    assert qty == 0 and z.placed == []


# ---------------------------------------------------------------- the daily list
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_the_list_is_read_once_a_day_and_kept_on_disk(tmp_path):
    path = str(tmp_path / "ticks.json")
    clock = Clock(dt.datetime(2026, 11, 19, 9, 0, tzinfo=IST))
    z = Zerodha()
    ts = TickSizes(z, path=path, clock=clock)
    assert ts.get("BHARTIARTL") == 0.10 and ts.get("TATASTEEL") == 0.01 and ts.get("NOPE") is None
    assert z.list_calls == 1
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["day"] == "2026-11-19"
    again = TickSizes(Zerodha(list_fails=True), path=path, clock=clock)       # a restart the same day
    assert again.get("BHARTIARTL") == 0.10
    clock.t += dt.timedelta(days=1)                                          # the next day: read again
    z.steps["BHARTIARTL"] = 0.05
    assert ts.get("BHARTIARTL") == 0.05 and z.list_calls == 2


def test_a_failed_read_is_retried_after_a_pause_not_on_every_order(tmp_path):
    clock = Clock(dt.datetime(2026, 11, 19, 9, 0, tzinfo=IST))
    z = Zerodha(list_fails=True)
    ts = TickSizes(z, path=str(tmp_path / "ticks.json"), clock=clock)
    assert ts.get("BHARTIARTL") is None and ts.get("KOTAKBANK") is None
    assert z.list_calls == 1
    clock.t += dt.timedelta(seconds=TickSizes.RETRY_SEC + 1)
    z.list_fails = False
    assert ts.get("BHARTIARTL") == 0.10 and z.list_calls == 2


def test_yesterdays_saved_list_is_not_used(tmp_path):
    path = str(tmp_path / "ticks.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"day": "2026-11-18", "ticks": {"BHARTIARTL": 0.05}}, f)
    z = Zerodha()
    ts = TickSizes(z, path=path, clock=Clock(dt.datetime(2026, 11, 19, 9, 0, tzinfo=IST)))
    assert ts.get("BHARTIARTL") == 0.10 and z.list_calls == 1


def test_the_log_line_names_each_stocks_step():
    b = lb(Zerodha())
    assert b.tick_report(["BHARTIARTL", "KOTAKBANK", "NOPE"]) == "BHARTIARTL 0.10, KOTAKBANK 0.05, NOPE NOT KNOWN"


def test_the_default_list_lives_in_the_data_folder():
    assert TickSizes(Zerodha()).path == os.path.join(C.DATA_DIR, "kite_ticks_NSE.json")
