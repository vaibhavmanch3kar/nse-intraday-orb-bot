"""Zerodha position-change popups: fills, partial fills and position open/close/changes are found by comparing
two read-only looks at Kite (orders + positions); the first look is only a baseline; no order can be placed;
every page carries the popup script; the endpoint never reaches the real Zerodha in tests."""

import datetime as dt
import json
import threading
import urllib.request

import pytest

import accounts
import config as C
import web
import webui

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T0 = dt.datetime(2026, 11, 18, 10, 0, tzinfo=IST)        # Wednesday, market open


def at(sec):
    return T0 + dt.timedelta(seconds=sec)


def order(oid, sym, side, qty, filled, status, avg=0.0, tag="orbbot"):
    return {"order_id": oid, "tradingsymbol": sym, "exchange": "NSE", "product": "MIS", "transaction_type": side,
            "quantity": qty, "filled_quantity": filled, "pending_quantity": qty - filled, "status": status,
            "average_price": avg, "tag": tag, "exchange_update_timestamp": "2026-11-18 10:00:01"}


def pos(sym, qty, avg=0.0, pnl=0.0):
    return {"tradingsymbol": sym, "exchange": "NSE", "product": "MIS", "quantity": qty, "average_price": avg,
            "pnl": pnl, "realised": pnl if qty == 0 else 0.0}


class FakeKite:
    """Serves whatever orders/positions the test sets; refuses to trade."""

    def __init__(self):
        self.orders_list, self.net, self.calls, self.fail = [], [], [], None

    def orders(self):
        self.calls.append("orders")
        if self.fail:
            raise self.fail
        return list(self.orders_list)

    def positions(self):
        self.calls.append("positions")
        return {"net": list(self.net), "day": []}

    def place_order(self, *a, **k):  # must never be reachable through the read-only wrapper
        raise AssertionError("order placed")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    accounts.reset_zerodha_events()

    def not_logged_in():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", not_logged_in)      # never the real account
    yield
    accounts.reset_zerodha_events()


def test_first_look_is_only_a_baseline():
    k = FakeKite()
    k.orders_list = [order("1", "SBIN", "BUY", 2, 2, "COMPLETE", 1110.5)]
    k.net = [pos("SBIN", 2, 1110.5)]
    r = accounts.zerodha_events(0, now=at(0), client=k)
    assert r["events"] == [] and r["seq"] == 0                       # the morning's fills are not replayed
    assert k.calls == ["orders", "positions"]


def test_new_fill_opens_a_position():
    k = FakeKite()
    accounts.zerodha_events(0, now=at(0), client=k)
    k.orders_list = [order("1", "SBIN", "BUY", 2, 2, "COMPLETE", 1110.5)]
    k.net = [pos("SBIN", 2, 1110.5)]
    r = accounts.zerodha_events(0, now=at(5), client=k)
    [e] = r["events"]
    assert (e["kind"], e["symbol"], e["side"], e["qty"], e["price"], e["by_bot"]) == ("fill", "SBIN", "BUY", 2, 1110.5, True)
    assert e["position"]["change"] == "opened" and e["position"]["after"] == 2 and e["seq"] == 1


def test_partial_fill_then_the_rest():
    k = FakeKite()
    k.orders_list = [order("1", "INFY", "BUY", 3, 0, "OPEN")]
    accounts.zerodha_events(0, now=at(0), client=k)
    k.orders_list = [order("1", "INFY", "BUY", 3, 1, "OPEN", 1120.0)]
    k.net = [pos("INFY", 1, 1120.0)]
    [p] = accounts.zerodha_events(0, now=at(5), client=k)["events"]
    assert (p["kind"], p["qty"], p["filled"], p["pending"]) == ("partial", 1, 1, 2)
    assert p["position"]["change"] == "opened"
    k.orders_list = [order("1", "INFY", "BUY", 3, 3, "COMPLETE", 1120.2)]
    k.net = [pos("INFY", 3, 1120.1)]
    [f] = accounts.zerodha_events(1, now=at(10), client=k)["events"]
    assert (f["kind"], f["qty"], f["filled"]) == ("fill", 2, 3)        # only the new 2 shares
    assert f["position"]["change"] == "added" and f["position"]["after"] == 3


def test_exit_fill_closes_the_position_with_pnl():
    k = FakeKite()
    k.orders_list = [order("1", "ITC", "BUY", 5, 5, "COMPLETE", 291.0)]
    k.net = [pos("ITC", 5, 291.0)]
    accounts.zerodha_events(0, now=at(0), client=k)
    k.orders_list.append(order("2", "ITC", "SELL", 5, 5, "COMPLETE", 293.0))
    k.net = [pos("ITC", 0, 0.0, pnl=10.0)]
    [e] = accounts.zerodha_events(0, now=at(5), client=k)["events"]
    assert e["side"] == "SELL" and e["position"]["change"] == "closed" and e["position"]["pnl"] == 10.0


def test_manual_trade_and_position_change_without_a_seen_fill():
    k = FakeKite()
    accounts.zerodha_events(0, now=at(0), client=k)
    k.orders_list = [order("9", "TCS", "SELL", 1, 1, "COMPLETE", 2400.0, tag="")]
    k.net = [pos("TCS", -1, 2400.0), pos("LT", 2, 4150.0)]          # LT: no order in the list
    ev = accounts.zerodha_events(0, now=at(5), client=k)["events"]
    tcs = [e for e in ev if e["symbol"] == "TCS"][0]
    lt = [e for e in ev if e["symbol"] == "LT"][0]
    assert tcs["kind"] == "fill" and tcs["by_bot"] is False and tcs["position"]["after"] == -1
    assert lt["kind"] == "position" and lt["position"]["change"] == "opened"


def test_reversed_and_reduced():
    assert accounts._position_change(2, -1) == "reversed"
    assert accounts._position_change(4, 2) == "reduced"
    assert accounts._position_change(-2, -4) == "added"
    assert accounts._position_change(3, 3) is None


def test_since_returns_only_newer_alerts_and_minus_one_only_the_number():
    k = FakeKite()
    accounts.zerodha_events(0, now=at(0), client=k)
    k.orders_list = [order("1", "SBIN", "BUY", 1, 1, "COMPLETE", 1110.0)]
    k.net = [pos("SBIN", 1, 1110.0)]
    accounts.zerodha_events(0, now=at(5), client=k)
    k.orders_list.append(order("2", "SBIN", "SELL", 1, 1, "COMPLETE", 1112.0))
    k.net = [pos("SBIN", 0, pnl=2.0)]
    accounts.zerodha_events(0, now=at(10), client=k)
    assert [e["seq"] for e in accounts.zerodha_events(1, now=at(10))["events"]] == [2]
    r = accounts.zerodha_events(-1, now=at(10))
    assert r["events"] == [] and r["seq"] == 2


def test_zerodha_is_asked_at_most_every_few_seconds(monkeypatch):
    k = FakeKite()
    monkeypatch.setattr(accounts, "_kite_client", lambda: k)
    accounts.zerodha_events(-1, now=at(0))
    accounts.zerodha_events(-1, now=at(2))                             # many pages, one look
    assert k.calls == ["orders", "positions"]
    accounts.zerodha_events(-1, now=at(accounts.EVENTS_POLL_SEC))
    assert k.calls.count("orders") == 2
    closed = dt.datetime(2026, 11, 18, 16, 0, tzinfo=IST)            # after the close: about once a minute
    accounts.zerodha_events(-1, now=closed)
    accounts.zerodha_events(-1, now=closed + dt.timedelta(seconds=30))
    assert k.calls.count("orders") == 3


def test_not_logged_in_and_errors_never_raise():
    r = accounts.zerodha_events(-1, now=at(0))
    assert r["login_needed"] is True and "Not logged in" in r["error"] and r["events"] == []
    k = FakeKite()
    accounts.zerodha_events(0, now=at(5), client=k)
    k.fail = ConnectionError("network down")
    r = accounts.zerodha_events(0, now=at(10), client=k)
    assert r["error"] and r["events"] == []


def test_a_new_login_starts_a_new_baseline():
    k = FakeKite()
    accounts.zerodha_events(0, now=at(0), client=k)
    accounts.zerodha_events(0, now=at(5))                              # logged out (fixture): baseline dropped
    k.orders_list = [order("1", "SBIN", "BUY", 1, 1, "COMPLETE", 1110.0)]
    k.net = [pos("SBIN", 1, 1110.0)]
    assert accounts.zerodha_events(0, now=at(10), client=k)["events"] == []   # baseline again, no flood


def test_a_new_day_starts_a_new_baseline():
    k = FakeKite()
    k.orders_list = [order("1", "SBIN", "BUY", 1, 1, "COMPLETE", 1110.0)]
    accounts.zerodha_events(0, now=at(0), client=k)
    k.orders_list = [order("7", "ITC", "BUY", 1, 1, "COMPLETE", 290.0)]
    assert accounts.zerodha_events(0, now=at(86400), client=k)["events"] == []


def test_alerts_can_only_read():
    ro = accounts.ReadOnlyKite(FakeKite())
    assert set(accounts.READ_ONLY_CALLS) == {"margins", "positions", "orders"}
    for name in ("place_order", "modify_order", "cancel_order", "convert_position", "exit_order"):
        with pytest.raises(PermissionError):
            getattr(ro, name)


def test_every_page_carries_the_popup_script():
    page = webui.shell("x", "/", "<p>body</p>")
    assert "/api/zerodha/events" in page and ".alerts{" in page
    if webui.T._ui:
        for tid in webui.T._ui.THEMES:
            tok = webui.T._ui.activate(tid)
            try:
                p = webui.shell("x", "/", "<p>body</p>")
                assert "/api/zerodha/events" in p and ".alert{" in p
            finally:
                webui.T._ui.reset(tok)


def test_endpoint_is_read_only_json(monkeypatch):
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        for q in ("?since=-1", "?since=abc", ""):
            with urllib.request.urlopen(url + "/api/zerodha/events" + q) as r:
                d = json.loads(r.read().decode())
            assert r.status == 200 and d["events"] == [] and d["login_needed"] is True
    finally:
        srv.shutdown()
