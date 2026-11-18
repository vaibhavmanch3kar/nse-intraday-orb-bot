"""The Kavach page's real-money cards: the bot's record against Zerodha, and today's order record. Read-only."""

import datetime as dt

import accounts
import orders
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 11, 19, 10, 30, tzinfo=IST)


def test_paper_shows_neither_card():
    assert web.kavach_zerodha_card({"mode": "paper"}) == "" and web.kavach_orders_card({"mode": "paper"}) == ""


def test_zerodha_card_states():
    live = {"mode": "live"}
    assert "Not compared yet" in web.kavach_zerodha_card(live)
    bad = web.kavach_zerodha_card(dict(live, recon={"ok": False, "at": "2026-11-19T10:00:05+05:30", "error": "ConnectionError: x"}))
    assert "Zerodha could not be read at 10:00:05" in bad
    ok = web.kavach_zerodha_card(dict(live, recon={"ok": True, "at": "2026-11-19T10:05:00+05:30", "startup": True,
                                                   "problems": [], "notes": ["checked 1 bot position(s)"],
                                                   "dropped": ["ITC"], "resized": {"SBIN": 6}, "cancelled": []}))
    assert "matches Zerodha" in ok and "(at the start)" in ok and "Dropped ITC" in ok and "SBIN corrected to 6 shares" in ok
    assert "New entries are on hold" not in ok
    held = web.kavach_zerodha_card(dict(live, entry_hold="the bot's record does not match Zerodha (INFY: ...)",
                                        recon={"ok": True, "at": "2026-11-19T10:05:00+05:30",
                                               "problems": ["INFY: the bot has +3 shares on record, Zerodha shows -3"]}))
    assert "DOES NOT MATCH" in held and "New entries are on hold" in held and "INFY: the bot has +3" in held
    assert "never opens or closes a position because of a mismatch" in held


def test_orders_card_lists_each_order_once_with_its_last_state():
    j = orders.Journal("live", now=lambda: NOW)
    j.record("orbEaaa", "SENDING", role="E", symbol="SBIN", side="BUY", qty=2, type="LIMIT", price=1112.0)
    j.record("orbEaaa", "SENT", order_id="2611190001")
    j.record("orbEaaa", "COMPLETE", order_id="2611190001", filled=2, avg=1111.5)
    j.record("orbSbbb", "SENDING", role="S", symbol="SBIN", side="SELL", qty=2, type="SL", price=1099.0, trigger=1104.0)
    j.record("orbSbbb", "TRIGGER PENDING", order_id="2611190002")
    j.record("orbEccc", "SENDING", role="E", symbol="ITC", side="BUY", qty=5, type="LIMIT", price=300.0)
    j.record("orbEccc", "NOT SENT", note="Zerodha refused: InputException: Insufficient funds")
    card = web.kavach_orders_card({"mode": "live"}, now=NOW)
    assert "3 orders" in card and card.count("<tr>") == 4                       # a header row and one row per order
    assert "SBIN</b> BUY 2" in card and ">entry<" in card and ">stop-loss<" in card
    assert "COMPLETE" in card and "2 @ 1,111.50" in card and "TRIGGER PENDING" in card
    assert "NOT SENT" in card and "Insufficient funds" in card and "<form" not in card


def test_no_orders_today():
    card = web.kavach_orders_card({"mode": "live"}, now=NOW)
    assert "No real order has been asked for today" in card and "0 orders" in card


def test_kavach_page_carries_both_cards_in_real_money(monkeypatch):
    s = {"time": dt.datetime.now(IST).isoformat(), "mode": "live", "equity": 10000.0, "positions": [], "rows": {},
         "recon": {"ok": True, "at": dt.datetime.now(IST).isoformat(), "problems": [], "notes": []}}
    monkeypatch.setattr(web, "read_status", lambda: s)
    page = web.render_kavach()
    assert "Bot's record against Zerodha" in page and "Order record today" in page


def test_popups_know_the_bots_orders_by_tag_prefix():
    prev = {}
    cur = accounts._order_fills([
        {"order_id": "1", "tradingsymbol": "SBIN", "exchange": "NSE", "product": "MIS", "transaction_type": "BUY",
         "quantity": 2, "filled_quantity": 2, "status": "COMPLETE", "average_price": 1110.0, "tag": orders.new_tag("E")},
        {"order_id": "2", "tradingsymbol": "ITC", "exchange": "NSE", "product": "MIS", "transaction_type": "BUY",
         "quantity": 1, "filled_quantity": 1, "status": "COMPLETE", "average_price": 290.0, "tag": "orbbot"},
        {"order_id": "3", "tradingsymbol": "TCS", "exchange": "NSE", "product": "MIS", "transaction_type": "BUY",
         "quantity": 1, "filled_quantity": 1, "status": "COMPLETE", "average_price": 2400.0, "tag": None}])
    ev = {e["symbol"]: e["by_bot"] for e in accounts.diff_events(prev, cur, {}, {})}
    assert ev == {"SBIN": True, "ITC": True, "TCS": False}                       # new tags, the old tag, a manual order
