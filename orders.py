"""
The order record: every real order the bot asks for, and everything that then happens to it, written to disk
before and after each step.

    data/orders/live_2026-11-19.jsonl      one line per event, never rewritten (append-only)

Why: an order id is not proof of execution, and a lost reply is not proof that nothing was sent. With each order
carrying its own tag, written down BEFORE it is sent, the bot (and you) can always answer "what did we ask
Zerodha for, and what became of it?", also after a crash: the next start reads this file and checks every
unfinished order against Zerodha (broker.LiveBroker.recover).

Each line: {at, tag, role, symbol, side, qty, type, price, trigger, order_id, status, filled, avg, note}
  role    E = entry, S = stop-loss, X = exit, F = flatten (closing shares the bot did not mean to hold)
  status  SENDING -> SENT -> Zerodha's own statuses (OPEN, COMPLETE, CANCELLED, REJECTED, TRIGGER PENDING ...)
          or NOT SENT (Zerodha never got it) / UNCERTAIN (no answer either way)
This module only writes and reads that file. It never talks to Zerodha.
"""

import datetime as dt
import json
import os
import secrets

import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TAG_PREFIX = "orb"                       # every order of this bot: tag starts with this (dashboard: "by the bot")
ROLES = {"E": "entry", "S": "stop-loss", "X": "exit", "F": "flatten"}
TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED", "NOT SENT")
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_tag(role):
    """A tag no other order has: 'orb' + role + 12 random characters (16 in all; Kite allows 20, letters and
    digits). The tag is how a lost order is found again in Zerodha's order book."""
    if role not in ROLES:
        raise ValueError(f"unknown order role {role!r}")
    return TAG_PREFIX + role + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def is_ours(tag):
    return bool(tag) and str(tag).startswith(TAG_PREFIX)


def role_of(tag):
    tag = str(tag or "")
    return tag[len(TAG_PREFIX)] if is_ours(tag) and len(tag) > len(TAG_PREFIX) and tag[len(TAG_PREFIX)] in ROLES else ""


def orders_dir():
    return os.path.join(C.DATA_DIR, "orders")


def path_for(mode, day):
    return os.path.join(orders_dir(), f"{mode}_{day}.jsonl")


class Journal:
    """Append-only record of one day's orders for one mode ('live')."""

    def __init__(self, mode="live", now=None):
        self.mode = mode
        self._now = now or (lambda: dt.datetime.now(IST))

    def path(self, day=None):
        return path_for(self.mode, day or self._now().date().isoformat())

    def record(self, tag, status, **fields):
        """Write one event. Never raises: a full disk must not stop an exit order from being sent, but the
        failure is returned so the caller can say so."""
        line = {"at": self._now().isoformat(timespec="seconds"), "tag": tag, "status": status, **fields}
        try:
            os.makedirs(orders_dir(), exist_ok=True)
            with open(self.path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(line, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return None
        except OSError as e:
            return f"{type(e).__name__}: {e}"

    def events(self, day=None):
        out = []
        try:
            with open(self.path(day), encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if raw:
                        try:
                            out.append(json.loads(raw))
                        except ValueError:
                            continue          # a line cut short by a crash: the lines before it still count
        except OSError:
            pass
        return out

    def latest(self, day=None):
        """{tag: the order as last recorded}, each merged from all of its events (later fields win)."""
        out = {}
        for e in self.events(day):
            tag = e.get("tag")
            if tag:
                out.setdefault(tag, {}).update({k: v for k, v in e.items() if v is not None})
        return out

    def unfinished(self, day=None):
        """Orders whose last recorded status is not final: what a restart has to ask Zerodha about."""
        return {t: o for t, o in self.latest(day).items() if o.get("status") not in TERMINAL}
