"""
Paper and live (Kite) execution. Both expose the same methods:

  enter(symbol, direction, qty, ref_price, stop)  -> (filled_qty, avg_price, extra)
  exit(symbol, pos, ref_price, reason)             -> (filled_qty, avg_price)
  stopped_out(symbol, pos)                         -> avg exit price if the exchange stop filled, else None

Live mode places a real SL (stop-limit) order at the exchange right after entry, so the
stop works even if this PC or the bot stops. Targets and the 15:10 square-off are handled
by the bot (a resting target order could double-fill with the stop).
"""

import logging
import time

import config as C
from risk import round_tick

log = logging.getLogger("broker")


def _side(direction, closing=False):
    buy = (direction == "LONG") != closing
    return "BUY" if buy else "SELL"


class PaperBroker:
    live = False

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

    def __init__(self, kite, sleep=time.sleep):
        self.kite = kite
        self.sleep = sleep

    # -- low level
    def _place(self, symbol, side, qty, order_type, price=None, trigger=None):
        k = self.kite
        params = dict(variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE, tradingsymbol=symbol,
                      transaction_type=side, quantity=int(qty), product=k.PRODUCT_MIS,
                      order_type=order_type, validity=k.VALIDITY_DAY, tag="orbbot")
        if price is not None:
            params["price"] = price
        if trigger is not None:
            params["trigger_price"] = trigger
        oid = k.place_order(**params)
        log.info("order %s %s %s x%s %s price=%s trigger=%s", oid, side, symbol, qty, order_type, price, trigger)
        return oid

    def _status(self, oid):
        h = self.kite.order_history(oid)
        last = h[-1] if h else {}
        return (last.get("status", ""), int(last.get("filled_quantity") or 0),
                float(last.get("average_price") or 0.0))

    def _cancel(self, oid):
        try:
            self.kite.cancel_order(self.kite.VARIETY_REGULAR, oid)
        except Exception as e:  # already filled / cancelled
            log.warning("cancel %s: %s", oid, e)

    def _limit_and_wait(self, symbol, side, qty, ref_price, buffer_pct):
        up = side == "BUY"
        price = round_tick(ref_price * (1 + buffer_pct / 100 if up else 1 - buffer_pct / 100), up)
        oid = self._place(symbol, side, qty, self.kite.ORDER_TYPE_LIMIT, price=price)
        deadline = time.monotonic() + C.ORDER_FILL_TIMEOUT_SEC
        status, filled, avg = "", 0, 0.0
        while True:
            status, filled, avg = self._status(oid)
            if status in ("COMPLETE", "CANCELLED", "REJECTED"):
                break
            if time.monotonic() >= deadline:
                self._cancel(oid)
                self.sleep(1)
                status, filled, avg = self._status(oid)
                break
            self.sleep(1)
        if status == "REJECTED":
            log.error("order %s rejected", oid)
        return filled, avg

    # -- interface
    def enter(self, symbol, direction, qty, ref_price, stop):
        side = _side(direction)
        filled, avg = self._limit_and_wait(symbol, side, qty, ref_price, C.LIMIT_BUFFER_PCT)
        if filled <= 0:
            return 0, 0.0, {}
        # exchange stop for the filled quantity
        exit_side = _side(direction, closing=True)
        trig = round_tick(stop, direction == "SHORT")
        lim = round_tick(stop * (1 - 0.5 / 100) if exit_side == "SELL" else stop * (1 + 0.5 / 100),
                         exit_side == "BUY")
        try:
            sl_id = self._place(symbol, exit_side, filled, self.kite.ORDER_TYPE_SL, price=lim, trigger=trig)
        except Exception as e:
            log.error("STOP ORDER FAILED for %s (%s) - closing position now", symbol, e)
            self.exit(symbol, {"direction": direction, "qty": filled}, avg, "stop order failed")
            return 0, 0.0, {}
        return filled, avg, {"sl_order_id": sl_id}

    def stopped_out(self, symbol, pos):
        oid = pos.get("sl_order_id")
        if not oid:
            return None
        status, filled, avg = self._status(oid)
        return avg if status == "COMPLETE" and filled > 0 else None

    def exit(self, symbol, pos, ref_price, reason):
        oid = pos.get("sl_order_id")
        if oid:
            status, filled, avg = self._status(oid)
            if status == "COMPLETE":
                return filled, avg
            self._cancel(oid)
            self.sleep(1)
            status, filled, avg = self._status(oid)
            if status == "COMPLETE":  # stop filled while we were cancelling
                return filled, avg
        remaining, done_qty, value = int(pos["qty"]), 0, 0.0
        side = _side(pos["direction"], closing=True)
        for attempt in range(C.EXIT_RETRIES):
            filled, avg = self._limit_and_wait(symbol, side, remaining, ref_price,
                                               C.LIMIT_BUFFER_PCT * (attempt + 1))
            done_qty += filled
            value += filled * avg
            remaining -= filled
            if remaining <= 0:
                break
        if remaining > 0:
            log.error("%s: %d shares still open after %d exit attempts - Zerodha auto square-off "
                      "will close MIS at ~15:20; check Kite NOW", symbol, remaining, C.EXIT_RETRIES)
        return done_qty, (value / done_qty if done_qty else 0.0)

    def open_mis_quantities(self):
        """Net MIS quantity per NSE symbol from Kite (for startup reconciliation)."""
        out = {}
        for p in self.kite.positions().get("day", []):
            if p.get("product") == "MIS" and p.get("exchange") == "NSE":
                out[p["tradingsymbol"]] = int(p.get("quantity") or 0)
        return out
