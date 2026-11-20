"""Shares you trade yourself in Kite, next to the bot (20 Nov 2026: the owner added 9 AXISBANK and 5 KOTAKBANK to
the bot's 1 and 5, and the bot held its new entries for the rest of the day as a "mismatch").

In a stock the bot holds, on the bot's side, your shares are managed with the bot's: no mismatch, one stop-loss
order covers every share (set to the full quantity when it was left as it was), and the bot's exit reads the real
quantity at that moment and closes all of it. In other stocks your shares are yours: the bot leaves them alone.
All against the fake Zerodha of test_execution_safety: no real order is ever sent from a test."""

import csv
import os

import bot
import config as C
from test_execution_safety import Zerodha, live, quick  # noqa: F401 - fixtures used by name


class Kite(Zerodha):
    """The fake Zerodha, with order modification (the stop's quantity)."""

    def __init__(self):
        super().__init__()
        self.modified, self.modify_fails = [], False

    def modify_order(self, variety, oid, **p):
        self.modified.append((oid, p))
        if self.modify_fails:
            raise ConnectionError("modify")
        self.book[oid]["quantity"] = p["quantity"]


def wire(b):
    z = Kite()
    b.kite, b.broker.kite = z, z
    return z


def you_buy(z, sym, qty, price, oid=None, side="BUY"):
    oid = oid or f"m{len(z.book) + 1}"
    z.book[oid] = {"order_id": oid, "tag": None, "status": "COMPLETE", "tradingsymbol": sym, "product": "MIS",
                   "exchange": "NSE", "transaction_type": side, "quantity": qty, "order_type": "MARKET",
                   "filled_quantity": qty, "average_price": price}


def entered(live):
    b, _ = live
    z = wire(b)
    b.cycle()
    pos = b.state.positions["SBIN"]
    return b, z, pos, int(pos["qty"])


def trades():
    with open(os.path.join(C.DATA_DIR, "trades_live.csv"), encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- the comparison
def test_your_shares_in_the_bots_stock_are_managed_with_it_and_hold_nothing(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    assert b.recon["problems"] == [] and b.entry_hold() is None
    assert pos["manual_qty"] == 9
    assert any(f"9 share(s) you bought in Kite are managed with the bot's {q}" in e for e in b.events)
    b.reconcile(startup=False)
    assert sum("are managed with the bot's" in e for e in b.events) == 1                  # said once


def test_more_shares_than_the_bot_and_you_bought_is_still_a_mismatch(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 11}
    b.reconcile(startup=False)
    assert b.entry_hold().startswith("the bot's record does not match Zerodha (SBIN")


def test_selling_some_of_your_shares_in_kite_is_followed(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    you_buy(z, "SBIN", 4, 101.5, side="SELL")
    z.net = {"SBIN": q + 5}
    b.reconcile(startup=False)
    assert pos["manual_qty"] == 5 and b.entry_hold() is None


def test_your_shares_in_another_stock_are_yours_and_left_alone(live):
    b, z, pos, q = entered(live)
    you_buy(z, "ITC", 5, 300.0)
    z.net = {"SBIN": q, "ITC": 5}
    sent = len(z.sent())
    b.reconcile(startup=False)
    assert b.entry_hold() is None and len(z.sent()) == sent
    assert any("ITC: +5 MIS shares at Zerodha are yours" in n for n in b.recon["notes"])


# ---------------------------------------------------------------- the stop-loss order
def test_a_stop_left_at_the_bots_size_is_set_to_every_share(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    state, _ = b.broker.ensure_stop("SBIN", pos)
    oid = pos["sl_order_id"]
    assert state == "ok" and z.book[oid]["quantity"] == q + 9
    (mid, p), = z.modified
    assert mid == oid and p["quantity"] == q + 9 and p["trigger_price"] == z.book[oid]["trigger_price"]
    assert b.broker.ensure_stop("SBIN", pos)[0] == "ok" and len(z.modified) == 1           # once is enough


def test_a_stop_that_cannot_be_resized_is_said_loudly_once(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    z.modify_fails = True
    b.broker.ensure_stop("SBIN", pos)
    b.broker.ensure_stop("SBIN", pos)
    said = [t for _, t in b.broker.messages if "could NOT be changed" in t]
    assert len(said) == 1 and f"set its quantity to {q + 9} in Kite" in said[0]


def test_a_stop_placed_again_covers_every_share(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    z.book[pos["sl_order_id"]]["status"] = "CANCELLED"
    state, new = b.broker.ensure_stop("SBIN", pos)
    assert state == "replaced" and z.book[new]["quantity"] == q + 9


# ---------------------------------------------------------------- the exit
def test_the_bots_exit_closes_every_share_in_the_stock_and_books_its_own(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    b.close_position("SBIN", 101.0, "time stop", bot.now_ist())
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == q + 9
    assert "SBIN" not in b.state.positions and int(trades()[-1]["qty"]) == q          # the bot books its own
    assert any("9 of your own share(s) in this stock closed with it" in e for e in b.events)


def test_shares_you_added_after_the_last_comparison_are_closed_too(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)                 # not compared yet: the record knows nothing of them
    b.close_position("SBIN", 101.0, "time stop", bot.now_ist())
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert sells[0]["quantity"] == q + 9 and int(trades()[-1]["qty"]) == q


def test_if_zerodha_cant_be_read_the_exit_closes_what_the_record_knows(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    z.book_down = True
    b.close_position("SBIN", 101.0, "time stop", bot.now_ist())
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert sells[0]["quantity"] == q + 9


def test_what_could_not_be_sold_stays_on_the_bots_record(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    b.reconcile(startup=False)
    z.fill = [q + 5, 0, 0]                       # 4 shares are left unsold
    b.close_position("SBIN", 101.0, "time stop", bot.now_ist())
    left = b.state.positions["SBIN"]
    assert left["qty"] == 4 and left["manual_qty"] == 0 and left["sl_order_id"] is None   # managed until it is out


def test_after_the_stop_closed_the_bots_shares_yours_are_closed_too(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)                 # the stop still covers only the bot's shares
    z.book[pos["sl_order_id"]].update(status="COMPLETE", filled_quantity=q, average_price=100.4)
    b.cycle()
    assert "SBIN" not in b.state.positions and int(trades()[-1]["qty"]) == q
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["quantity"] == 9
    assert any("9 more share(s) in this stock (yours) were closed after the stop" in e for e in b.events)


def test_the_brokers_own_closes_never_take_your_shares(live):
    b, z, pos, q = entered(live)
    you_buy(z, "SBIN", 9, 100.9)
    done, _ = b.broker.exit("SBIN", {"direction": "LONG", "qty": 2}, 101.0, "late fill")
    sells = [o for o in z.sent("LIMIT") if o["transaction_type"] == "SELL"]
    assert done == 2 and sells[-1]["quantity"] == 2


# ---------------------------------------------------------------- the daily loss limit counts your trades
class KiteWithPnl(Kite):
    """Zerodha's positions with last price and P&L (before charges), as the real one gives them."""

    def __init__(self):
        super().__init__()
        self.px = {}

    def positions(self):
        day = []
        for s, q in self.net.items():
            ltp, pnl = self.px.get(s, (None, None))
            day.append({"tradingsymbol": s, "product": "MIS", "exchange": "NSE", "quantity": q,
                        "last_price": ltp, "pnl": pnl})
        return {"day": day}


def wired_pnl(live):
    b, _ = live
    z = KiteWithPnl()
    b.kite, b.broker.kite = z, z
    return b, z


def test_your_result_is_zerodhas_pnl_less_the_bots_part(live):
    b, z = wired_pnl(live)
    b.cycle()
    pos = b.state.positions["SBIN"]
    q, e = int(pos["qty"]), float(pos["entry"])
    you_buy(z, "SBIN", 9, 100.9)
    z.net = {"SBIN": q + 9}
    z.px = {"SBIN": (100.0, round(q * (100.0 - e) + 9 * (100.0 - 100.9), 2))}
    b.reconcile(startup=False)
    assert abs(b.manual_pnl() - (-8.1)) < 0.02


def test_a_big_loss_in_your_own_trades_holds_entries_and_closes_the_bots(live):
    b, z = wired_pnl(live)
    b.cycle()
    q = int(b.state.positions["SBIN"]["qty"])
    you_buy(z, "ITC", 50, 300.0)
    z.net = {"SBIN": q, "ITC": 50}
    z.px = {"SBIN": (None, None), "ITC": (250.0, -2500.0)}           # 2.5% of the Rs 1,00,000 day start
    b.reconcile(startup=False)
    b.enforce_daily_limit({}, bot.now_ist())
    assert "SBIN" not in b.state.positions
    assert b.entry_hold().startswith("daily loss limit 2% reached counting your own trades")
    assert any("with your own trades in Kite" in e for e in b.events)
    itc_sells = [o for o in z.sent() if o["tradingsymbol"] == "ITC" and str(o.get("tag") or "").startswith("orb")]
    assert itc_sells == []                                           # your shares in other stocks: yours


def test_a_small_loss_in_your_own_trades_holds_nothing(live):
    b, z = wired_pnl(live)
    b.cycle()
    q = int(b.state.positions["SBIN"]["qty"])
    you_buy(z, "ITC", 5, 300.0)
    z.net = {"SBIN": q, "ITC": 5}
    z.px = {"ITC": (295.0, -25.0)}
    b.reconcile(startup=False)
    b.enforce_daily_limit({}, bot.now_ist())
    assert "SBIN" in b.state.positions and b.entry_hold() is None and b.manual_now == -25.0
    rows = b.manual_rows()
    assert rows == [{"symbol": "ITC", "direction": "LONG", "qty": 5, "avg": 300.0, "price": 295.0, "pnl": -25.0,
                     "at": b.manual_book["at"]}]


def test_paper_has_no_manual_trades(monkeypatch):
    b = bot.Bot()
    assert not b.live and b.manual_pnl() == 0.0 and b.entry_hold() is None


# ---------------------------------------------------------------- the dashboard
def test_open_positions_show_your_shares():
    import datetime as dt

    import web
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    s = {"mode": "live", "time": now.isoformat(), "manual_pnl": -12.5,
         "positions": [{"symbol": "AXISBANK", "direction": "LONG", "qty": 1, "manual_qty": 9, "entry": 1389.3,
                        "price": 1390.1, "stop": 1368.8, "unrealised": 0.8, "r_unreal": 0.04, "held_min": 37}],
         "manual": [{"symbol": "ITC", "direction": "LONG", "qty": 5, "avg": 300.0, "price": 297.5, "pnl": -12.5}]}
    page = web.live_positions(s, "zerodha")
    assert "+9 YOURS" in page and "× 1 bot + 9 yours" in page
    assert "ITC" in page and "not managed by the bot" in page and "▼ ₹12.50" in page
    assert "counts toward the daily loss limit" in page
    paper = {**s, "mode": "paper", "positions": [{k: v for k, v in s["positions"][0].items() if k != "manual_qty"}]}
    assert web.live_positions(paper, "paper").count("YOURS") == 0       # the Kite list is real money only


def test_health_counts_your_trades_in_the_daily_loss_line():
    import datetime as dt

    import health
    now = dt.datetime(2026, 11, 20, 11, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    s = {"time": (now - dt.timedelta(seconds=20)).isoformat(), "mode": "live", "equity": 10000.0, "day_pnl": -50.0,
         "manual_pnl": -100.0, "recon": {"at": now.isoformat(), "ok": True, "problems": []}}
    rows = health.checks(now, s, login=True, disk_free_gb=40.0, proxy={})
    line = next(r for r in rows if r["key"] == "day_loss")
    assert line["detail"].startswith("1.48% of the 2% limit") and "your own trades in Kite -100.00 included" in line["detail"]
