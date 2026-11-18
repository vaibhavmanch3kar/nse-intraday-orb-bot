"""
Account summaries for the dashboard. READ-ONLY: nothing here changes the bot's trading mode or settings,
writes a file, or places an order.

  paper_account()    the virtual Abhyas account, read from data/state_paper.json only
                     (never from the live state or Zerodha)
  zerodha_account()  the real Zerodha account from Kite's margins() and positions() calls only,
                     through a wrapper that refuses every other Kite method (so no order can be
                     placed from here). Kept in memory for 15 s; never written to a file or a log.
  zerodha_events()   position-change alerts for the dashboard popups: new fills, partial fills,
                     positions opened / closed / changed on Zerodha (the bot's or anyone's), found by
                     comparing Kite's orders() and positions() with the previous look. Same read-only
                     wrapper; kept in memory only.
"""

import datetime as dt
import json
import os
import threading

import config as C
import holidays

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def now_ist():
    return dt.datetime.now(IST)


def market_open(now):
    """True during NSE cash-market hours on a trading day."""
    open_t = dt.datetime.combine(now.date(), dt.time(*map(int, C.MARKET_OPEN.split(":"))), tzinfo=IST)
    close_t = dt.datetime.combine(now.date(), dt.time(*map(int, C.MARKET_CLOSE.split(":"))), tzinfo=IST)
    return holidays.is_trading_day(now.date()) and open_t <= now <= close_t


def stale_after_sec():
    """The bot saves its state every round (POLL_SEC); allow three missed rounds."""
    return max(180, 3 * C.POLL_SEC)


def _gross(entry, price, qty, direction):
    return (price - entry) * qty if direction == "LONG" else (entry - price) * qty


def paper_account(now=None, state_path=None):
    """The paper account: starting balance, equity, invested, available, realised/unrealised P&L.

    Positions are tracked like a cash account without leverage: the bot doesn't take cash away on
    entry, so 'available' = equity - invested. Unrealised P&L uses each position's last price the bot
    saw (before exit charges)."""
    now = now or now_ist()
    path = state_path or os.path.join(C.DATA_DIR, "state_paper.json")
    start = C.PAPER_START_INR
    out = {"kind": "paper", "start": start, "now": now.isoformat(), "warnings": [], "positions": []}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        as_of = dt.datetime.fromtimestamp(os.path.getmtime(path), IST)
    except FileNotFoundError:
        d, as_of = {}, None
        out["warnings"].append("No paper trades yet: the account is at its starting balance.")
    except (OSError, ValueError) as e:
        out.update(error=f"Can't read the paper account file ({e}).")
        return out

    start = float(d.get("opening_balance", start))   # chosen on the dashboard, applied by the bot
    out["start"] = start
    cash = float(d.get("cash", start))
    invested = unreal = 0.0
    for sym, p in sorted((d.get("positions") or {}).items()):
        px = p.get("last_price", p["entry"])
        u = _gross(p["entry"], px, p["qty"], p["direction"])
        invested += p["entry"] * p["qty"]
        unreal += u
        out["positions"].append({"symbol": sym, "direction": p["direction"], "qty": p["qty"],
                                 "entry": p["entry"], "price": px, "value": px * p["qty"], "unrealised": u})
    equity = cash + unreal
    out.update(cash=cash, invested=invested, unrealised=unreal, equity=equity,
               available=equity - invested,
               realised=float(d.get("realized_pnl", 0.0)), realised_today=float(d.get("day_pnl", 0.0)),
               charges=float(d.get("charges_paid", 0.0)), total_pnl=equity - start,
               as_of=as_of.isoformat() if as_of else None)

    if as_of and market_open(now):
        age = (now - as_of).total_seconds()
        if age > stale_after_sec():
            out["warnings"].append(f"Stale: last update {age / 60:.0f} min ago. Is the bot running?")
    if out["positions"] and as_of and not market_open(now) and as_of.date() < now.date():
        out["warnings"].append("Positions are from an earlier day: values are the last prices the bot saw.")
    return out


# -- Zerodha (real account, read-only) -----------------------------------------------------------

ZERODHA_CACHE_SEC = 15
READ_ONLY_CALLS = ("margins", "positions", "orders")   # all three only read; nothing can be placed
_cache = {"at": None, "data": None}
_cache_lock = threading.Lock()


class LoginNeeded(Exception):
    pass


class ReadOnlyKite:
    """Exposes only Kite's read-only account calls. Anything else (place_order, modify_order,
    cancel_order, convert_position, ...) raises, so this view can't trade even by mistake."""

    def __init__(self, kite):
        self._kite = kite

    def __getattr__(self, name):
        if name not in READ_ONLY_CALLS:
            raise PermissionError(f"the Zerodha account view is read-only: '{name}' is not allowed")
        return getattr(self._kite, name)


def _kite_client():
    import data_kite
    try:
        return data_kite._client()  # today's login token, no proxy; raises DataError if not logged in
    except data_kite.DataError as e:
        text = str(e)
        if "no Kite login" in text:
            raise LoginNeeded("Not logged in to Zerodha today. Log in to see your account "
                              "(logins end around 06:00 each day).")
        raise LoginNeeded(f"Kite settings problem: {text}")


def explain_kite_error(e):
    """(plain message, whether a fresh login would fix it)"""
    try:
        from kiteconnect import exceptions as kex
    except ImportError:  # pragma: no cover
        kex = None
    import requests
    if kex and isinstance(e, kex.TokenException):
        return "Your Zerodha session has expired or was logged out. Log in again.", True
    if kex and isinstance(e, kex.PermissionException):
        return f"Zerodha refused access ({e}).", False
    if kex and isinstance(e, kex.NetworkException):
        return f"Zerodha's servers didn't respond ({e}). It will try again.", False
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return "Can't reach Zerodha (is the internet down?). It will try again.", False
    if kex and isinstance(e, kex.KiteException):
        return f"Zerodha returned an error: {e}", False
    return f"Unexpected error while reading the Zerodha account: {e!r}", False


def _num(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def summarise_zerodha(m, p):
    """Kite margins('equity') + positions() -> the dashboard's account figures."""
    avail, used = m.get("available") or {}, m.get("utilised") or {}
    rows = []
    for x in (p or {}).get("net") or []:
        qty = int(x.get("quantity") or 0)
        rows.append({"symbol": x.get("tradingsymbol"), "exchange": x.get("exchange"), "product": x.get("product"),
                     "qty": qty, "avg": _num(x.get("average_price")), "price": _num(x.get("last_price")),
                     "realised": _num(x.get("realised")), "unrealised": _num(x.get("unrealised")),
                     "pnl": _num(x.get("pnl"))})
    open_rows = [r for r in rows if r["qty"]]
    realised = sum(r["realised"] for r in rows)
    unrealised = sum(r["unrealised"] for r in rows)
    opening = _num(avail.get("opening_balance"))
    return {"opening": opening, "available": _num(m.get("net")), "cash": _num(avail.get("live_balance")),
            "margin_used": _num(used.get("debits")),
            "invested": sum(abs(r["qty"]) * r["avg"] for r in open_rows),
            "realised": realised, "unrealised": unrealised, "pnl": realised + unrealised,
            "equity": opening + realised + unrealised,
            "positions": sorted(rows, key=lambda r: (r["qty"] == 0, r["symbol"] or ""))}


def zerodha_account(now=None, client=None):
    """The real account, or a dict with 'error' (and 'login_needed') saying what's wrong.
    A failed refresh keeps showing the last good values, marked with when they were fetched."""
    now = now or now_ist()
    with _cache_lock:
        cached = _cache["data"]
        if client is None and cached and (now - _cache["at"]).total_seconds() < ZERODHA_CACHE_SEC:
            return dict(cached, now=now.isoformat())
    out = {"kind": "zerodha", "now": now.isoformat(), "warnings": [], "positions": []}
    try:
        kite = ReadOnlyKite(client if client is not None else _kite_client())
        summary = summarise_zerodha(kite.margins("equity"), kite.positions())
    except LoginNeeded as e:
        out.update(error=str(e), login_needed=True)
        return out
    except Exception as e:  # noqa: BLE001 - every failure is shown on the card, never raised
        msg, login = explain_kite_error(e)
        if cached and not login:
            stale = dict(cached, now=now.isoformat())
            stale["warnings"] = [f"Refresh failed at {now:%H:%M:%S}: {msg} "
                                 f"Showing the values from {cached['fetched_at'][11:19]}."]
            return stale
        out.update(error=msg, login_needed=login)
        return out
    out.update(summary, fetched_at=now.isoformat())
    out["warnings"] = []
    with _cache_lock:
        _cache.update(at=now, data=dict(out))
    return out


def clear_zerodha_cache():
    with _cache_lock:
        _cache.update(at=None, data=None)


# -- Zerodha position-change alerts (dashboard popups) -------------------------------------------
# The dashboard asks /api/zerodha/events every few seconds; Zerodha is asked at most every
# EVENTS_POLL_SEC during market hours (EVENTS_IDLE_SEC otherwise), however many pages are open.
# The first look of the day (or after a login) is only a baseline: alerts are for changes after it.

EVENTS_POLL_SEC = 5
EVENTS_IDLE_SEC = 60
MAX_EVENTS = 100
BOT_TAG = "orbbot"                       # broker.LiveBroker tags every order it places
_events = {"seq": 0, "items": [], "orders": None, "positions": None, "day": None, "at": None,
           "error": None, "login_needed": False}
_events_lock = threading.Lock()


def _order_fills(orders):
    """{order_id: what matters for a fill} from Kite orders()."""
    out = {}
    for o in orders or []:
        oid = o.get("order_id")
        if not oid:
            continue
        out[oid] = {"symbol": o.get("tradingsymbol"), "exchange": o.get("exchange"),
                    "product": o.get("product"), "side": o.get("transaction_type"),
                    "quantity": int(_num(o.get("quantity"))), "filled": int(_num(o.get("filled_quantity"))),
                    "pending": int(_num(o.get("pending_quantity"))), "status": o.get("status") or "",
                    "avg": _num(o.get("average_price")), "tag": o.get("tag") or "",
                    "time": str(o.get("exchange_update_timestamp") or o.get("order_timestamp") or "")}
    return out


def _net_positions(p):
    """{(exchange, symbol, product): {qty, avg, pnl}} from Kite positions()['net']."""
    out = {}
    for x in (p or {}).get("net") or []:
        key = (x.get("exchange"), x.get("tradingsymbol"), x.get("product"))
        out[key] = {"qty": int(_num(x.get("quantity"))), "avg": _num(x.get("average_price")),
                    "pnl": _num(x.get("pnl")), "realised": _num(x.get("realised"))}
    return out


def _position_change(before, after):
    """'opened' / 'closed' / 'reversed' / 'added' / 'reduced', or None when the quantity didn't change."""
    if before == after:
        return None
    if before == 0:
        return "opened"
    if after == 0:
        return "closed"
    if (before > 0) != (after > 0):
        return "reversed"
    return "added" if abs(after) > abs(before) else "reduced"


def diff_events(prev_orders, cur_orders, prev_pos, cur_pos):
    """Alerts between two looks at Zerodha. One alert per fill (a partial fill says how much is still
    pending), carrying the position's change for that stock; a position change with no fill seen
    (for example one made while the dashboard was closed) gets its own alert."""
    events, touched = [], {}
    for oid, o in cur_orders.items():
        was = (prev_orders.get(oid) or {}).get("filled", 0)
        got = o["filled"] - was
        if got <= 0:
            continue
        partial = o["filled"] < o["quantity"] and o["status"] not in ("COMPLETE",)
        ev = {"kind": "partial" if partial else "fill", "order_id": oid, "symbol": o["symbol"],
              "exchange": o["exchange"], "product": o["product"], "side": o["side"], "qty": got,
              "filled": o["filled"], "quantity": o["quantity"], "pending": max(0, o["quantity"] - o["filled"]),
              "price": o["avg"], "status": o["status"], "by_bot": o["tag"] == BOT_TAG, "time": o["time"]}
        events.append(ev)
        touched[(o["exchange"], o["symbol"], o["product"])] = ev       # the last fill of the stock gets it
    for key in sorted(set(prev_pos) | set(cur_pos), key=lambda k: tuple(str(x) for x in k)):
        before = (prev_pos.get(key) or {}).get("qty", 0)
        now_p = cur_pos.get(key) or {"qty": 0, "avg": 0.0, "pnl": 0.0, "realised": 0.0}
        change = _position_change(before, now_p["qty"])
        if not change:
            continue
        pos = {"change": change, "before": before, "after": now_p["qty"], "avg": now_p["avg"],
               "pnl": now_p["pnl"], "realised": now_p["realised"]}
        if key in touched:
            touched[key]["position"] = pos
        else:
            events.append({"kind": "position", "symbol": key[1], "exchange": key[0], "product": key[2],
                           "position": pos})
    return events


def zerodha_events(since=0, now=None, client=None):
    """Alerts newer than `since` (an alert number). since < 0 asks only for the latest number, so a page
    that just opened starts from now instead of replaying the morning. Never raises."""
    now = now or now_ist()
    with _events_lock:
        every = EVENTS_POLL_SEC if market_open(now) else EVENTS_IDLE_SEC
        due = client is not None or _events["at"] is None or (now - _events["at"]).total_seconds() >= every
        if due:
            _events["at"] = now
            try:
                kite = ReadOnlyKite(client if client is not None else _kite_client())
                orders, positions = kite.orders(), kite.positions()
            except LoginNeeded as e:
                _events.update(error=str(e), login_needed=True, orders=None, positions=None)   # new baseline after login
            except Exception as e:  # noqa: BLE001 - a failed look keeps the baseline and tries again
                msg, login = explain_kite_error(e)
                _events.update(error=msg, login_needed=login)
                if login:
                    _events.update(orders=None, positions=None)
            else:
                cur_o, cur_p = _order_fills(orders), _net_positions(positions)
                if _events["orders"] is not None and _events["day"] == now.date():
                    for ev in diff_events(_events["orders"], cur_o, _events["positions"], cur_p):
                        _events["seq"] += 1
                        ev.update(seq=_events["seq"], seen_at=now.isoformat(timespec="seconds"))
                        _events["items"].append(ev)
                    del _events["items"][:-MAX_EVENTS]
                _events.update(orders=cur_o, positions=cur_p, day=now.date(), error=None, login_needed=False)
        items = [] if since < 0 else [e for e in _events["items"] if e["seq"] > since]
        return {"seq": _events["seq"], "events": items, "error": _events["error"],
                "login_needed": _events["login_needed"], "checked_at": _events["at"].isoformat(timespec="seconds"),
                "every_sec": every}


def reset_zerodha_events():
    with _events_lock:
        _events.update(seq=0, items=[], orders=None, positions=None, day=None, at=None, error=None,
                       login_needed=False)
