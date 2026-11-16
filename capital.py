"""
The PAPER account's opening balance: which amount you chose, a pending change, and the history.

How a change works (paper only - live trading and the Zerodha account never read this file):
  1. The dashboard saves your choice as *pending* in data/paper_capital.json (after you confirm).
  2. At its next start, the paper bot applies it to data/state_paper.json before any trade:
     cash and the day's starting equity move by (new - old) opening balance. If paper positions are
     still open it waits for a later start.
  3. Past trades (trades_paper.csv), realised P&L and charges are never touched. Each applied change
     is recorded in the history, e.g. "16 Nov: 100,000 -> 50,000".
The opening balance in force is kept in the paper state itself ("opening_balance"), so applying a
change twice (e.g. after a crash between the two file writes) can't move the cash twice.
"""

import datetime as dt
import json
import os

import config as C

PRESETS = (10_000, 25_000, 50_000, 100_000, 500_000)
MIN_INR = 10_000
MAX_INR = 1_00_00_000          # ₹1 crore
SMALL_INR = 50_000             # below this, warn that trades get small and some stocks are skipped

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def settings_path():
    return os.path.join(C.DATA_DIR, "paper_capital.json")


def paper_state_path():
    return os.path.join(C.DATA_DIR, "state_paper.json")


def validate(amount):
    """Whole rupees between ₹10,000 and ₹1 crore. Returns int or raises ValueError (plain message)."""
    try:
        text = str(amount).replace(",", "").replace("₹", "").strip()
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError("Enter the amount in rupees, e.g. 50000.")
    if value != int(value):
        raise ValueError("Use whole rupees (no paise).")
    value = int(value)
    if not MIN_INR <= value <= MAX_INR:
        raise ValueError(f"The opening balance must be between ₹{MIN_INR:,} and ₹{MAX_INR:,}.")
    return value


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load():
    """{'pending': {'amount', 'requested_at'} or None, 'history': [{'applied_at', 'from', 'to'}]}"""
    try:
        with open(settings_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {"pending": d.get("pending"), "history": list(d.get("history") or [])}


def current_opening_balance(state_d=None):
    """The opening balance in force for the paper account (from the paper state)."""
    if state_d is None:
        try:
            with open(paper_state_path(), encoding="utf-8") as f:
                state_d = json.load(f)
        except (OSError, ValueError):
            state_d = {}
    return float(state_d.get("opening_balance", C.PAPER_START_INR))


def request(amount, now=None):
    """Save a pending change (after the user confirmed on the dashboard). Returns a plain message."""
    now = now or dt.datetime.now(IST)
    value = validate(amount)
    s = load()
    if value == current_opening_balance() and not s["pending"]:
        return f"The opening balance is already ₹{value:,}: nothing to change."
    if value == current_opening_balance():
        s["pending"] = None
        _write_json(settings_path(), s)
        return f"Pending change cancelled: the opening balance stays ₹{value:,}."
    s["pending"] = {"amount": value, "requested_at": now.isoformat(timespec="seconds")}
    _write_json(settings_path(), s)
    return (f"Saved: the paper opening balance becomes ₹{value:,} at the bot's next start "
            f"(past trades and P&L stay as they are).")


def cancel():
    s = load()
    if not s["pending"]:
        return "There is no pending change."
    s["pending"] = None
    _write_json(settings_path(), s)
    return "Pending change cancelled."


def apply_pending(state, now=None):
    """Called by the PAPER bot at start-up only. Applies a pending opening balance to `state`
    (a risk.State for state_paper.json). Returns a message for the log, or None if nothing pending."""
    s = load()
    pending = s["pending"]
    if not pending:
        return None
    new = float(pending["amount"])
    if state.positions:
        return (f"Opening balance change to ₹{new:,.0f} waits: {len(state.positions)} paper position(s) "
                f"still open. It will be applied at a later start.")
    now = now or dt.datetime.now(IST)
    old = current_opening_balance(state.d)
    if new == old:  # already in force (e.g. applied before a crash): just clear the pending entry
        s["pending"] = None
        _write_json(settings_path(), s)
        return f"Paper opening balance ₹{new:,.0f} is already in force."
    if new != old:
        delta = new - old
        state.d["cash"] += delta
        state.d["day_start_equity"] = state.d.get("day_start_equity", old) + delta
        state.d["opening_balance"] = new
        state.save()
        s["history"].append({"applied_at": now.isoformat(timespec="seconds"), "from": old, "to": new})
    s["pending"] = None
    _write_json(settings_path(), s)
    return (f"Paper opening balance changed from ₹{old:,.0f} to ₹{new:,.0f} "
            f"(cash {'+' if new > old else '-'}₹{abs(new - old):,.0f}; past trades and P&L unchanged)")
