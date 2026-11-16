"""
Account summaries for the dashboard. READ-ONLY: nothing here changes the bot's trading mode or settings,
writes a file, or places an order.

  paper_account()    the virtual Abhyas account, read from data/state_paper.json only
                     (never from the live state or Zerodha)
"""

import datetime as dt
import json
import os

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
