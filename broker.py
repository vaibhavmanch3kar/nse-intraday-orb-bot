"""
Paper and live (Kite) execution. Both expose the same methods:

  enter(symbol, direction, qty, ref_price, stop)  -> (filled_qty, avg_price, extra)
  exit(symbol, pos, ref_price, reason)             -> (filled_qty, avg_price)
  stopped_out(symbol, pos)                         -> avg exit price if the exchange stop filled, else None

Live mode places a real SL (stop-limit) order at the exchange right after entry, so the
stop works even if this PC or the bot stops. Targets and the square-off (SQUARE_OFF, 15:00) are handled
by the bot (a resting target order could double-fill with the stop).

Execution safety (2.0), all in LiveBroker, the ONLY place that sends an order to Zerodha:
  - every order has its own tag and is written to the order record (orders.py) before it is sent and after
    every change, so nothing the bot asked for can be forgotten;
  - an order id is not a fill: quantities come only from Zerodha's order status;
  - a lost reply is not "nothing was sent": the order is looked for by its tag; if Zerodha can't be asked, the
    state is UNCERTAIN and no further entry order is sent until it is cleared (settle);
  - an order that is neither filled nor cancelled when the bot stops waiting is watched until it is final, and
    shares that filled late are closed again;
  - the margin is checked with Zerodha before an entry; if it can't be checked the entry is skipped;
  - the stop-loss order is verified after it is placed and on every round (ensure_stop); a missing one is
    placed again, and if that fails the position is closed;
  - recover() compares the bot's positions and unfinished orders with Zerodha at every start and every few
    minutes, fixes what is safe to fix (a stale working order, a missing stop) and reports the rest. It never
    opens or closes a position the bot has no record of.
"""

import datetime as dt
import json
import logging
import os
import time

import config as C
import orders
import safefile
from risk import round_tick

log = logging.getLogger("broker")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

FINAL = ("COMPLETE", "CANCELLED", "REJECTED")
# Zerodha said no (wrong input, no margin, not allowed, not logged in): the order does not exist.
DEFINITE_ERRORS = ("InputException", "OrderException", "PermissionException", "TokenException", "MarginException")
LOST_LOOKS = 5          # looks at the order book before a send with no answer is written off as NOT SENT


class OrderNotSent(Exception):
    """Zerodha did not take the order (it said no, or the order book shows it never arrived)."""


class OrderUncertain(Exception):
    """No answer either way: the order may exist. No further entry order until this is cleared."""


class StatusUnknown(Exception):
    """Zerodha could not be asked about an order just now."""


def _side(direction, closing=False):
    buy = (direction == "LONG") != closing
    return "BUY" if buy else "SELL"


class TickSizes:
    """Each NSE stock's price step (tick), from Kite's instrument list. Zerodha refuses an order whose price is not
    a multiple of it, and NSE sets it per stock (0.01, 0.05, 0.10, ...), so every real order is rounded to its own
    stock's step. Read once a day and kept in data/kite_ticks_NSE.json. If the list can't be read, get() says
    None and it is tried again after RETRY_SEC (not on every order)."""
    RETRY_SEC = 300

    def __init__(self, kite, path=None, clock=None):
        self.kite = kite
        self.path = path or os.path.join(C.DATA_DIR, "kite_ticks_NSE.json")
        self.clock = clock or (lambda: dt.datetime.now(IST))
        self.day = None
        self.ticks = {}
        self.failed_at = None

    def _load(self):
        now = self.clock()
        today = now.date().isoformat()
        if self.day == today:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("day") == today and cached.get("ticks"):
                self.day, self.ticks = today, cached["ticks"]
                return
        except (OSError, ValueError, AttributeError):
            pass
        if self.failed_at is not None and (now - self.failed_at).total_seconds() < self.RETRY_SEC:
            return
        try:
            rows = self.kite.instruments("NSE") or []
            ticks = {}
            for r in rows:
                t = float(r.get("tick_size") or 0)
                if t > 0 and r.get("tradingsymbol"):
                    ticks[r["tradingsymbol"]] = t
            if not ticks:
                raise ValueError("no price steps in Kite's instrument list")
        except Exception as e:  # noqa: BLE001 - network, proxy, session: unknown for now, tried again later
            self.failed_at = now
            log.warning("Kite's instrument list (price steps) could not be read: %s", type(e).__name__)
            return
        self.day, self.ticks, self.failed_at = today, ticks, None
        try:
            safefile.write_json(self.path, {"day": today, "ticks": ticks})
        except OSError as e:
            log.warning("price steps not saved (%s); they are kept in memory", e)

    def get(self, symbol):
        """The stock's price step, or None if it is not known."""
        self._load()
        t = self.ticks.get(symbol) if self.day else None
        return float(t) if t else None


class PaperBroker:
    live = False
    uncertain = None

    def enter(self, symbol, direction, qty, ref_price, stop):
        slip = ref_price * C.SLIPPAGE_PCT / 100
        price = ref_price + slip if direction == "LONG" else ref_price - slip
        return qty, round(price, 2), {}

    def exit(self, symbol, pos, ref_price, reason):
        slip = ref_price * C.SLIPPAGE_PCT / 100
        price = ref_price - slip if pos["direction"] == "LONG" else ref_price + slip
        return pos["qty"], round(price, 2)

    def stopped_out(self, symbol, pos):
        return None  # paper stops are checked on candles by the bot


class LiveBroker:
    live = True
    STOP_LIMIT_PCT = 0.5        # the stop-loss is a stop-LIMIT: its limit sits this far beyond its trigger

    def __init__(self, kite, sleep=time.sleep, journal=None, ticks=None):
        self.kite = kite
        self.sleep = sleep
        self.ticks = ticks if ticks is not None else TickSizes(kite)   # each stock's price step
        self.tick_warned = set()
        self.journal = journal or orders.Journal("live")
        self.uncertain = None       # plain words while an order's fate is unknown: no new entry meanwhile
        self.unsettled = {}         # tag -> order left neither filled nor cancelled (settle() finishes it)
        self.lost = {}              # tag -> order sent with no answer and not (yet) in the order book
        self.late_exits = []        # shares of a position's closing order that filled after the bot stopped counting
        self._closing_tracked = True   # False while closing shares the bot never had on record (a late entry fill)
        self._closing_entry_px = None  # ... and the price those shares were bought (sold) at
        self.round_trips = []       # shares bought and sold again by the broker itself, for the bot to book
        self.messages = []          # plain lines for the bot's log and events (the bot takes and clears them)
        self.last_final = True

    # -- helpers
    def tick(self, symbol):
        """The price step to round this stock's stop and exit orders to: its own, or FALLBACK_TICK if it is not
        known (a closing order must never be held back). enter() sends nothing when it is not known."""
        t = self.ticks.get(symbol)
        if t:
            return t
        if symbol not in self.tick_warned:
            self.tick_warned.add(symbol)
            self._say(f"{symbol}: Zerodha's price step is not known - its stop and exit orders are rounded to "
                      f"{C.FALLBACK_TICK:.2f}", logging.ERROR)
        return C.FALLBACK_TICK

    def tick_report(self, symbols):
        """One plain line: the price step of each stock (for the log at the start of real-money trading)."""
        return ", ".join(f"{s} {self.ticks.get(s):.2f}" if self.ticks.get(s) else f"{s} NOT KNOWN" for s in symbols)

    def _say(self, text, level=logging.WARNING):
        log.log(level, text)
        self.messages.append((level, text))

    def _flag(self, why):
        self.uncertain = why
        self._say(f"ORDER STATE UNCERTAIN: {why}. No new entries until this is cleared.", logging.ERROR)

    def _lookup(self, tag):
        """(order dict or None, could Zerodha be asked). Finds an order in today's order book by its tag."""
        try:
            book = self.kite.orders() or []
        except Exception as e:  # noqa: BLE001 - network, proxy, session: we simply could not ask
            log.warning("order book not readable (%s)", type(e).__name__)
            return None, False
        for o in book:
            if o.get("tag") == tag:
                return o, True
        return None, True

    # -- low level
    def _place(self, symbol, side, qty, order_type, price=None, trigger=None, role="E"):
        """Send one order. Returns (order_id, tag). Raises OrderNotSent or OrderUncertain."""
        k = self.kite
        tag = orders.new_tag(role)
        params = dict(variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE, tradingsymbol=symbol,
                      transaction_type=side, quantity=int(qty), product=k.PRODUCT_MIS,
                      order_type=order_type, validity=k.VALIDITY_DAY, tag=tag)
        if price is not None:
            params["price"] = price
        if trigger is not None:
            params["trigger_price"] = trigger
        rec = dict(role=role, symbol=symbol, side=side, qty=int(qty), type=order_type, price=price, trigger=trigger)
        self.journal.record(tag, "SENDING", **rec)
        try:
            oid = k.place_order(**params)
        except Exception as e:  # noqa: BLE001 - every failure is sorted into "no" or "don't know" below
            kind = type(e).__name__
            if kind in DEFINITE_ERRORS:
                self.journal.record(tag, "NOT SENT", note=f"Zerodha refused: {kind}: {e}")
                raise OrderNotSent(f"{kind}: {e}") from e
            # The reply was lost, or the order never left. The order itself tells: look for its tag.
            found, asked = None, False
            for _ in range(3):
                found, ok = self._lookup(tag)
                asked = asked or ok
                if found:
                    break
                self.sleep(1)
            if found:
                oid = found.get("order_id")
                self.journal.record(tag, "SENT", order_id=oid, note=f"reply lost ({kind}); found in the order book")
                log.warning("order %s %s %s: reply lost (%s) but the order exists", oid, side, symbol, kind)
                return oid, tag
            self.lost[tag] = {**rec, "looks": 0, "tracked": self._closing_tracked,
                              "entry_px": self._closing_entry_px}
            if asked:
                self.journal.record(tag, "NOT SENT", note=f"send failed ({kind}: {e}); not in the order book")
                # watched for a while longer by settle(), in case it turns up late
                raise OrderNotSent(f"{kind}: {e}") from e
            self.journal.record(tag, "UNCERTAIN", note=f"send failed ({kind}: {e}) and the order book can't be read")
            self._flag(f"{symbol}: a {side} order got no answer and Zerodha's order book can't be read")
            raise OrderUncertain(f"{kind}: {e}") from e
        self.journal.record(tag, "SENT", order_id=oid)
        log.info("order %s %s %s x%s %s price=%s trigger=%s tag=%s", oid, side, symbol, qty, order_type, price,
                 trigger, tag)
        return oid, tag

    def _status(self, oid, tries=3):
        """(status, filled, average price) from Zerodha's own record of the order."""
        err = None
        for i in range(tries):
            try:
                h = self.kite.order_history(oid)
                last = h[-1] if h else {}
                return (last.get("status", ""), int(last.get("filled_quantity") or 0),
                        float(last.get("average_price") or 0.0))
            except Exception as e:  # noqa: BLE001
                err = e
                if i < tries - 1:
                    self.sleep(1)
        raise StatusUnknown(f"{type(err).__name__}: {err}")

    def _cancel(self, oid):
        try:
            self.kite.cancel_order(self.kite.VARIETY_REGULAR, oid)
        except Exception as e:  # already filled / cancelled
            log.warning("cancel %s: %s", oid, e)

    def cancel(self, oid):
        """Cancel one order of the bot (for example a stop that no longer matches its position)."""
        self._cancel(oid)

    def _limit_and_wait(self, symbol, side, qty, ref_price, buffer_pct, role="E"):
        """A marketable limit order, watched until it is final. Returns (filled, average price).
        self.last_final says whether Zerodha confirmed the order finished (filled, cancelled or rejected)."""
        up = side == "BUY"
        price = round_tick(ref_price * (1 + buffer_pct / 100 if up else 1 - buffer_pct / 100), up, self.tick(symbol))
        self.last_final = True
        oid, tag = self._place(symbol, side, qty, self.kite.ORDER_TYPE_LIMIT, price=price, role=role)
        deadline = time.monotonic() + C.ORDER_FILL_TIMEOUT_SEC
        status, filled, avg = "", 0, 0.0
        while True:
            try:
                status, filled, avg = self._status(oid, tries=1)
            except StatusUnknown:
                status = ""
            if status in FINAL or time.monotonic() >= deadline:
                break
            self.sleep(1)
        if status not in FINAL:
            # not filled in time: cancel, then WAIT for Zerodha to confirm the cancel. Until it does, the order
            # can still fill, so its quantity is not known yet.
            self._cancel(oid)
            for _ in range(max(1, C.CANCEL_WAIT_SEC)):
                self.sleep(1)
                try:
                    status, filled, avg = self._status(oid, tries=1)
                except StatusUnknown:
                    continue
                if status in FINAL:
                    break
        self.journal.record(tag, status or "UNCERTAIN", order_id=oid, filled=filled, avg=avg)
        if status == "REJECTED":
            log.error("order %s rejected", oid)
        if status not in FINAL:
            self.last_final = False
            self.unsettled[tag] = {"order_id": oid, "role": role, "symbol": symbol, "side": side, "qty": int(qty),
                                   "accounted": filled, "price": price, "tracked": self._closing_tracked,
                                   "entry_px": self._closing_entry_px}
            self._flag(f"{symbol}: order {oid} is neither filled nor cancelled yet")
        return filled, avg

    def margin_ok(self, symbol, side, qty, price):
        """(ok, why not). Asks Zerodha what the order needs and what is free. If it can't be asked: not ok."""
        if not C.MARGIN_CHECK:
            return True, ""
        k = self.kite
        try:
            need = k.order_margins([dict(exchange=k.EXCHANGE_NSE, tradingsymbol=symbol, transaction_type=side,
                                         variety=k.VARIETY_REGULAR, product=k.PRODUCT_MIS,
                                         order_type=k.ORDER_TYPE_LIMIT, quantity=int(qty), price=price)])
            need = float((need[0] if need else {}).get("total") or 0.0)
            avail = (k.margins("equity") or {}).get("available") or {}
            free = avail.get("live_balance") if avail.get("live_balance") is not None else avail.get("cash")
            free = float(free or 0.0)
        except Exception as e:  # noqa: BLE001
            return False, f"could not check the margin with Zerodha ({type(e).__name__})"
        if need > free:
            return False, f"Zerodha needs ₹{need:,.0f} margin for this order; ₹{free:,.0f} is free"
        return True, ""

    def _protect(self, symbol, direction, qty, stop):
        """Place the exchange stop-loss for `qty` and check Zerodha took it. Returns its order id, or None."""
        exit_side = _side(direction, closing=True)
        tick = self.tick(symbol)
        trig = round_tick(stop, direction == "SHORT", tick)
        band = self.STOP_LIMIT_PCT / 100
        lim = round_tick(stop * (1 - band) if exit_side == "SELL" else stop * (1 + band),
                         exit_side == "BUY", tick)
        try:
            sl_id, tag = self._place(symbol, exit_side, qty, self.kite.ORDER_TYPE_SL, price=lim, trigger=trig,
                                     role="S")
        except Exception as e:  # noqa: BLE001 - refused, lost or uncertain: there is no stop we can rely on
            log.error("STOP ORDER FAILED for %s (%s)", symbol, e)
            return None
        try:
            status, _, _ = self._status(sl_id)
        except StatusUnknown:
            return sl_id                      # it was accepted; ensure_stop checks it again next round
        self.journal.record(tag, status, order_id=sl_id)
        if status in ("REJECTED", "CANCELLED"):
            log.error("STOP ORDER %s for %s was %s by Zerodha", sl_id, symbol, status)
            return None
        return sl_id

    # -- interface
    def enter(self, symbol, direction, qty, ref_price, stop):
        side = _side(direction)
        if self.uncertain:
            return 0, 0.0, {"reason": f"order state uncertain ({self.uncertain})"}
        if not self.ticks.get(symbol):
            return 0, 0.0, {"reason": "Zerodha's price step for this stock is not known (Kite's instrument list "
                                      "could not be read), so no order is sent"}
        ok, why = self.margin_ok(symbol, side, qty, ref_price)
        if not ok:
            return 0, 0.0, {"reason": why}
        try:
            filled, avg = self._limit_and_wait(symbol, side, qty, ref_price, C.LIMIT_BUFFER_PCT, role="E")
        except OrderNotSent as e:
            return 0, 0.0, {"reason": f"Zerodha did not take the entry order ({e})"}
        except OrderUncertain as e:
            return 0, 0.0, {"reason": f"no answer from Zerodha for the entry order ({e})"}
        if filled <= 0:
            return 0, 0.0, {}
        # exchange stop for the filled quantity
        sl_id = self._protect(symbol, direction, filled, stop)
        if sl_id is None:
            self._say(f"{symbol}: the stop-loss order could not be placed - closing the position now", logging.ERROR)
            done, exit_avg = self.exit(symbol, {"direction": direction, "qty": filled}, avg, "stop order failed")
            left = filled - done
            # what was bought and sold again here never becomes a position: the bot books its result from this
            closed = ({"closed": {"qty": done, "entry": avg, "exit": exit_avg, "stop": stop}} if done > 0 else {})
            if left > 0:      # could not close it all: the bot must keep track of what is left and retry
                self._say(f"{symbol}: {left} shares are still open WITHOUT an exchange stop - the bot keeps "
                          f"trying; check Kite NOW", logging.ERROR)
                return left, avg, {"sl_order_id": None, **closed}
            return 0, 0.0, {"reason": "the stop-loss order could not be placed, so the position was closed",
                            **closed}
        return filled, avg, {"sl_order_id": sl_id}

    MAX_PRICE_JUMP_PCT = 20.0   # a "last price" further than this from the bot's own is treated as a bad reading

    def last_price(self, symbol, near=None):
        """The stock's last traded price at Zerodha right now, or None if it can't be read (or is implausibly
        far from `near`, the price the bot already has). Read-only."""
        try:
            key = f"{self.kite.EXCHANGE_NSE}:{symbol}"
            px = float(((self.kite.ltp(key) or {}).get(key) or {}).get("last_price") or 0.0)
        except Exception:  # noqa: BLE001 - no quote just now: the caller uses the price it has
            return None
        if px <= 0 or (near and abs(px / near - 1) * 100 > self.MAX_PRICE_JUMP_PCT):
            return None
        return px

    def through_stop_limit(self, pos, price):
        """Is `price` beyond the LIMIT of the position's stop-loss order? There the order can no longer fill: a
        sell limit rests above the market (a buy limit below it), so the exchange is not protecting the position."""
        stop = pos.get("stop")
        if not price or not stop:
            return False
        band = self.STOP_LIMIT_PCT / 100
        return price < stop * (1 - band) if pos["direction"] == "LONG" else price > stop * (1 + band)

    def stopped_out(self, symbol, pos):
        oid = pos.get("sl_order_id")
        if not oid:
            return None
        try:
            status, filled, avg = self._status(oid)
        except StatusUnknown:
            return None
        return avg if status == "COMPLETE" and filled > 0 else None

    def ensure_stop(self, symbol, pos):
        """Is the position's exchange stop still there? Returns (state, new order id or None):
        'ok', 'filled' (the stop was hit), 'triggered' (it was hit but has not, or not fully, filled),
        'unknown' (Zerodha can't be asked just now), 'replaced' (it was gone and a new one is placed) or
        'missing' (it is gone and could not be placed again)."""
        oid = pos.get("sl_order_id")
        if oid:
            try:
                status, filled, _ = self._status(oid)
            except StatusUnknown:
                return "unknown", None
            if status == "COMPLETE":
                return "filled", None
            if status == "OPEN" or filled > 0:
                # The stop is a stop-LIMIT order. OPEN means its trigger was hit and the limit order behind it
                # is resting unfilled: the price went through the limit, so the exchange no longer protects
                # the position. Shares already sold (filled > 0, whatever the status) mean the same. Only the
                # bot can finish the exit now, and a new stop for the full quantity would oversell.
                return "triggered", None
            if status not in ("CANCELLED", "REJECTED"):
                return "ok", None
            self._say(f"{symbol}: the exchange stop-loss order {oid} is {status} - placing it again", logging.ERROR)
        new = self._protect(symbol, pos["direction"], int(pos["qty"]), pos["stop"])
        return ("replaced", new) if new else ("missing", None)

    def exit(self, symbol, pos, ref_price, reason):
        oid = pos.get("sl_order_id")
        stop_qty, stop_avg = 0, 0.0         # shares the stop-loss order itself sold before it was cancelled
        if oid:
            try:
                status, filled, avg = self._status(oid)
                if status == "COMPLETE":
                    return filled, avg
            except StatusUnknown:
                # can't ask about the stop: sending a closing order now could sell twice. Leave the exchange
                # stop in place and try again next round.
                self._say(f"{symbol}: Zerodha can't be asked about the stop order just now - exit postponed "
                          f"(the exchange stop stays in place)", logging.ERROR)
                return 0, 0.0
            # Cancel the stop and WAIT for Zerodha to confirm it. Until the cancel is confirmed the stop can
            # still fill, and a closing order sent meanwhile would sell the same shares twice.
            self._cancel(oid)
            status = ""
            for _ in range(max(1, C.CANCEL_WAIT_SEC)):
                self.sleep(1)
                try:
                    status, filled, avg = self._status(oid, tries=1)
                except StatusUnknown:
                    continue
                if status in FINAL:
                    break
            if status == "COMPLETE":        # the stop filled while it was being cancelled
                return filled, avg
            if status not in FINAL:
                self._say(f"{symbol}: the cancel of the stop-loss order is not confirmed yet - exit postponed to "
                          f"the next round (no closing order is sent while the stop may still fill)", logging.ERROR)
                return 0, 0.0
            stop_qty, stop_avg = int(filled or 0), float(avg or 0.0)
            if stop_qty:
                self._say(f"{symbol}: the stop-loss order had already sold {stop_qty} of {int(pos['qty'])} shares "
                          f"@ {stop_avg:.2f} - closing only the other {int(pos['qty']) - stop_qty}", logging.WARNING)
        remaining = int(pos["qty"]) - stop_qty
        done_qty, value = stop_qty, stop_qty * stop_avg
        if remaining <= 0:
            return done_qty, stop_avg
        side = _side(pos["direction"], closing=True)
        for attempt in range(C.EXIT_RETRIES):
            # Each attempt is priced from the stock's price NOW. They used to reuse the price the bot had read at
            # the start of the round for every attempt, up to several minutes old by the last one: in a fast
            # move the limit stayed behind the market and no attempt could fill.
            price = self.last_price(symbol, near=ref_price) or ref_price
            try:
                filled, avg = self._limit_and_wait(symbol, side, remaining, price,
                                                   C.LIMIT_BUFFER_PCT * (attempt + 1), role="X")
            except OrderNotSent:
                continue                      # Zerodha has no such order: safe to try again
            except OrderUncertain:
                break                         # it may exist: sending another could sell twice
            done_qty += filled
            value += filled * avg
            remaining -= filled
            if remaining <= 0 or not self.last_final:
                break                         # done, or the last order is not final: never stack a second on it
        if remaining > 0:
            log.error("%s: %d shares still open after the exit attempts - Zerodha auto square-off "
                      "will close MIS at ~15:20; check Kite NOW", symbol, remaining)
        return done_qty, (value / done_qty if done_qty else 0.0)

    def open_mis_quantities(self):
        """Net MIS quantity per NSE symbol from Kite (for startup reconciliation)."""
        out = {}
        for p in self.kite.positions().get("day", []):
            if p.get("product") == "MIS" and p.get("exchange") == "NSE":
                out[p["tradingsymbol"]] = int(p.get("quantity") or 0)
        return out

    @staticmethod
    def _protects_unrecorded_shares(o, tag, positions, net):
        """Is this working order a stop-loss of the bot that protects shares Zerodha shows but the bot has no
        record of? That is what a crash leaves behind when it comes between an entry filling (with its stop
        placed) and the position being saved. Such a stop used to be cancelled as a "leftover" at the next start,
        which removed the only protection those shares had. It stays until the shares are gone."""
        sym = o.get("tradingsymbol")
        have = net.get(sym, 0)
        if orders.role_of(tag) != "S" or sym in positions or have == 0:
            return False
        return (have > 0) == (o.get("transaction_type") == "SELL")       # it must reduce the position, not add to it

    def closing_fills(self, symbol, direction, need):
        """(shares, average price) of the newest `need` shares that today's closing-side MIS orders in this
        stock have filled at Zerodha: the bot's own stop and exit orders, Zerodha's forced square-off, or an
        order you placed yourself. (0, 0.0) if there are none. Raises if the order book can't be read."""
        k = self.kite
        side = _side(direction, closing=True)
        book = list(k.orders() or [])
        if book and all(o.get("order_timestamp") for o in book):
            book.sort(key=lambda o: str(o["order_timestamp"]))
        left, got, value = int(need), 0, 0.0
        for o in reversed(book):                        # newest first: earlier fills were booked when they happened
            if left <= 0:
                break
            if o.get("tradingsymbol") != symbol or o.get("transaction_type") != side:
                continue
            if o.get("product") not in (None, k.PRODUCT_MIS) or o.get("exchange") not in (None, k.EXCHANGE_NSE):
                continue
            filled, price = int(o.get("filled_quantity") or 0), float(o.get("average_price") or 0.0)
            if filled <= 0 or price <= 0:
                continue
            take = min(filled, left)
            got, value, left = got + take, value + take * price, left - take
        return got, (value / got if got else 0.0)

    # -- finishing what was left open
    def settle(self):
        """Run every round. Finishes orders that were not final when the bot stopped waiting, and looks again
        for orders that were sent with no answer. Shares that filled late on an entry are closed again.
        Clears `uncertain` once nothing is open-ended."""
        for tag, o in list(self.unsettled.items()):
            try:
                status, filled, avg = self._status(o["order_id"], tries=1)
            except StatusUnknown:
                continue
            if status not in FINAL:
                self._cancel(o["order_id"])
                continue
            del self.unsettled[tag]
            self.journal.record(tag, status, order_id=o["order_id"], filled=filled, avg=avg, note="settled later")
            extra = filled - int(o.get("accounted") or 0)
            if extra > 0:
                self._late_fill(o, extra, avg)
        for tag, o in list(self.lost.items()):
            found, asked = self._lookup(tag)
            if not asked:
                continue
            if found is None:
                o["looks"] += 1
                if o["looks"] >= LOST_LOOKS:
                    del self.lost[tag]
                    self.journal.record(tag, "NOT SENT", note=f"not in the order book after {LOST_LOOKS} looks")
                continue
            del self.lost[tag]
            self.journal.record(tag, "SENT", order_id=found.get("order_id"), note="turned up after a lost reply")
            self._say(f"{o['symbol']}: an order sent with no answer DID reach Zerodha ({found.get('order_id')})",
                      logging.ERROR)
            self.unsettled[tag] = {**o, "order_id": found.get("order_id"), "accounted": 0}
        if self.uncertain and not self.unsettled and not self.lost:
            self._say("order state is clear again: every order is accounted for", logging.INFO)
            self.uncertain = None

    def _late_fill(self, o, extra, avg):
        """`extra` shares filled after the bot had stopped counting that order."""
        sym, side = o["symbol"], o["side"]
        if o.get("role") == "E":
            # an entry filled late: these shares have no stop and no record. Close them.
            direction = "LONG" if side == "BUY" else "SHORT"
            self._say(f"{sym}: {extra} shares of an entry order filled late @ {avg:.2f} - closing them now",
                      logging.ERROR)
            self._closing_tracked, self._closing_entry_px = False, float(avg)   # never a position on record
            try:
                done, exit_avg = self.exit(sym, {"direction": direction, "qty": extra}, avg, "late fill")
            finally:
                self._closing_tracked, self._closing_entry_px = True, None
            if done > 0:
                self.round_trips.append({"symbol": sym, "direction": direction, "qty": int(done),
                                         "entry": float(avg), "exit": float(exit_avg), "why": "late fill closed"})
            if done < extra:
                self._say(f"{sym}: {extra - done} late-filled shares could NOT be closed - check Kite NOW",
                          logging.ERROR)
        else:
            # A closing order filled further after the bot stopped counting it: the position is smaller than the
            # record says. The bot is told (take_late_exits) so that it books these shares before it does
            # anything else with the position. It used to learn of them only from the next comparison with
            # Zerodha, and meanwhile placed a new stop for, or sold again, shares it no longer held.
            self._say(f"{sym}: {extra} more shares of a closing order filled late @ {avg:.2f}", logging.WARNING)
            if o.get("tracked", True):
                self.late_exits.append({"symbol": sym, "qty": int(extra), "avg": float(avg)})
            elif o.get("entry_px"):             # the close of late-filled entry shares itself filled late
                self.round_trips.append({"symbol": sym, "direction": "LONG" if side == "SELL" else "SHORT",
                                         "qty": int(extra), "entry": float(o["entry_px"]), "exit": float(avg),
                                         "why": "late fill closed"})

    def take_round_trips(self):
        """Shares the broker bought and sold again by itself since the last call:
        [{symbol, direction, qty, entry, exit, why}]."""
        out, self.round_trips = self.round_trips, []
        return out

    def take_late_exits(self):
        """Late fills of positions' closing orders since the last call: [{symbol, qty, avg}]."""
        out, self.late_exits = self.late_exits, []
        return out

    def closing_pending(self, symbol):
        """Is a closing order for this stock's position still open-ended (neither filled nor cancelled for
        certain, or sent with no answer yet)? While it is, no new stop and no second closing order may go out."""
        return any(o.get("role") == "X" and o.get("symbol") == symbol and o.get("tracked", True)
                   for o in list(self.unsettled.values()) + list(self.lost.values()))

    def recover(self, positions, startup=True):
        """Compare the bot's record with Zerodha. `positions` is the bot's own {symbol: position}.

        Returns {"drop": [symbols Zerodha no longer holds], "resize": {symbol: quantity Zerodha holds},
                 "cancelled": [stale working orders cancelled], "problems": [things only you can decide],
                 "notes": [what was checked]}.
        It cancels stale working orders of the bot (never a stop of a position the bot holds). It never opens a
        position and never closes one it has no record of. Raises if Zerodha can't be read."""
        out = {"drop": [], "resize": {}, "cancelled": [], "problems": [], "notes": [], "kept_stops": []}
        book = self.kite.orders() or []
        net = self.open_mis_quantities()
        by_tag = {o.get("tag"): o for o in book if orders.is_ours(o.get("tag"))}
        # 1) orders the record says are unfinished (a crash or restart in the middle of one)
        if startup:
            for tag in self.journal.unfinished():
                o = by_tag.get(tag)
                if o is None:
                    self.journal.record(tag, "NOT SENT", note="not in Zerodha's order book at the next start")
                    continue
                self.journal.record(tag, o.get("status") or "UNCERTAIN", order_id=o.get("order_id"),
                                    filled=int(o.get("filled_quantity") or 0),
                                    avg=float(o.get("average_price") or 0.0), note="checked at start")
        # 2) working orders of the bot that nothing is waiting for any more: cancel them. A stop belonging to
        #    a position the bot holds is never touched; an order still being settled is left to settle().
        known_stops = {p.get("sl_order_id") for p in positions.values() if p.get("sl_order_id")}
        waiting = {o["order_id"] for o in self.unsettled.values()}
        for tag, o in by_tag.items():
            oid = o.get("order_id")
            if o.get("status") in FINAL or oid in known_stops or oid in waiting:
                continue
            if orders.role_of(tag) == "S" and o.get("tradingsymbol") in positions and not startup:
                continue                      # a stop just placed this round, not yet saved with its position
            if self._protects_unrecorded_shares(o, tag, positions, net):
                out["kept_stops"].append(o.get("tradingsymbol"))
                continue                      # see _protects_unrecorded_shares: never cancel their only protection
            self._cancel(oid)
            out["cancelled"].append(f"{o.get('tradingsymbol')} {o.get('transaction_type')} {oid} "
                                    f"({orders.ROLES.get(orders.role_of(tag), 'order')})")
        # 3) the bot's positions against Zerodha's
        for sym, pos in positions.items():
            want = int(pos["qty"]) if pos["direction"] == "LONG" else -int(pos["qty"])
            have = net.get(sym, 0)
            if have == 0:
                out["drop"].append(sym)
            elif have == want:
                continue
            elif (have > 0) == (want > 0) and abs(have) < abs(want):
                out["resize"][sym] = abs(have)
            else:
                out["problems"].append(f"{sym}: the bot has {want:+d} shares on record, Zerodha shows {have:+d}")
        # 4) MIS positions at Zerodha that the bot has no record of
        for sym, have in net.items():
            if have == 0 or sym in positions:
                continue
            ours = any(orders.is_ours(o.get("tag")) and o.get("tradingsymbol") == sym
                       and int(o.get("filled_quantity") or 0) > 0 for o in book)
            if ours:
                kept = (" Their stop-loss order was found working and has been LEFT IN PLACE."
                        if sym in out["kept_stops"] else " They have NO stop-loss order.")
                out["problems"].append(f"{sym}: Zerodha shows {have:+d} MIS shares from the bot's own orders that "
                                       f"the bot has no record of - close them in Kite, or check the order record."
                                       + kept)
            else:
                out["notes"].append(f"{sym}: {have:+d} MIS shares at Zerodha are not the bot's (left alone)")
        out["notes"].append(f"checked {len(positions)} bot position(s), "
                            f"{sum(1 for v in net.values() if v)} open Zerodha MIS position(s), "
                            f"{len(by_tag)} bot order(s) today")
        return out
