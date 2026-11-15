#!/usr/bin/env python3
"""
Kite NSE intraday opening-range-breakout bot.

    .venv\\Scripts\\python.exe bot.py          # PAPER mode (default), dashboard at http://127.0.0.1:5000
    New-Item STOP                             # graceful stop (or the dashboard's Stop button)

Live trading needs DRY_RUN=false AND CONFIRM_LIVE=YES, plus today's Kite login.
"""

import csv
import datetime as dt
import json
import logging
import os
import sys
import threading
import time

import archive
import config as C
import costs
import data_yahoo
import marketdata
import holidays
import risk
import strategy
import theme as T
from broker import LiveBroker, PaperBroker

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
log = logging.getLogger("ayodhya")


def now_ist():
    return dt.datetime.now(IST)


def setup_logging():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(C.DATA_DIR, "bot.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


TRADE_COLS = ["closed", "symbol", "direction", "qty", "entry", "exit", "stop", "target",
              "gross", "charges", "net", "r", "reason", "opened"] + strategy.CONTEXT_COLS


def _upgrade_trade_log(path, cols):
    """A log written before new columns were added gets the new header; old rows keep their
    values and leave the new columns empty. Nothing is lost."""
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if r.fieldnames == cols:
            return
        rows = list(r)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_trade(row, mode):
    path = os.path.join(C.DATA_DIR, f"trades_{mode}.csv")
    new = not os.path.exists(path)
    cols = TRADE_COLS
    if not new:
        _upgrade_trade_log(path, cols)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()})


def position_details(p, price, bars, now):
    """Extra live facts about an open position for the dashboard: open profit in R, how far the
    stop is, time in the trade, where the EMA trail is, and when the time stop is checked."""
    sign = 1 if p["direction"] == "LONG" else -1
    risk_ps = abs(p["entry"] - p["stop"]) or 1e-9
    out = {"r_unreal": (price - p["entry"]) * sign / risk_ps,
           "stop_dist_pct": abs(price - p["stop"]) / price * 100 if price else None,
           "held_min": None, "trail_ema": None, "trail_dist_pct": None,
           "time_check_at": None, "time_check": None}
    try:
        opened = dt.datetime.fromisoformat(p["opened"])
        out["held_min"] = int((now - opened).total_seconds() // 60)
    except (KeyError, TypeError, ValueError):
        opened = None
    if C.TRAIL_EMA > 0 and bars:
        out["trail_ema"] = strategy.ema([b["c"] for b in bars[-4 * C.TRAIL_EMA:]], C.TRAIL_EMA)
        out["trail_dist_pct"] = (price - out["trail_ema"]) * sign / price * 100 if price else None
    if C.TIME_STOP_BARS > 0:
        if p.get("time_ok"):
            out["time_check"] = "passed"
        else:
            try:
                first_bar = dt.datetime.fromisoformat(p["checked_upto"]) if p.get("checked_upto") else opened
                due = first_bar + dt.timedelta(minutes=C.INTERVAL_MIN * (C.TIME_STOP_BARS + 1))
                out["time_check_at"] = f"{due:%H:%M}"
                out["time_check"] = "pending"
            except (TypeError, ValueError):
                pass
    return out


def at(t: str, day: dt.date):
    return dt.datetime.combine(day, strategy.hm(t), tzinfo=IST)


class Bot:
    def __init__(self):
        self.live = C.is_live()
        self.mode = "live" if self.live else "paper"
        self.state = risk.State(os.path.join(C.DATA_DIR, f"state_{self.mode}.json"), C.PAPER_START_INR)
        self.broker = PaperBroker()
        self.kite = None
        self.last_bar = {}      # symbol -> start time of last processed closed bar
        self.prices = {}        # symbol -> latest price
        self.rows = {}          # symbol -> evaluation for the dashboard
        self.events = []        # recent messages for the dashboard
        self.data_lag_min = None
        self.vix, self.vix_day = None, None
        self.holiday_checked = None
        self.bars = {}          # symbol -> latest closed 15-min bars (for the dashboard)

    # -- helpers
    def note(self, msg, level=logging.INFO):
        log.log(level, msg)
        self.events = ([f"{now_ist():%H:%M:%S} {msg}"] + self.events)[:30]

    def ensure_live_broker(self):
        """Live mode needs today's Kite token; returns False until the user logs in."""
        if not self.live:
            return True
        if self.kite is not None:
            return True
        import kite_proxy
        if not kite_proxy.load_token():
            return False
        self.kite = kite_proxy.get_kite()
        self.broker = LiveBroker(self.kite)
        self.reconcile()
        return True

    def reconcile(self):
        held = self.broker.open_mis_quantities()
        for sym in list(self.state.positions):
            if held.get(sym, 0) == 0:
                self.note(f"{sym}: no open MIS position on Kite - dropping it from state", logging.WARNING)
                self.state.positions.pop(sym)
        self.state.save()

    def close_position(self, sym, ref_price, reason, when):
        pos = self.state.positions[sym]
        qty, price = self.broker.exit(sym, pos, ref_price, reason)
        if qty <= 0:
            self.note(f"{T.WARN_ICON} {sym}: exit FAILED ({reason}) - check Kite", logging.ERROR)
            return
        if qty < pos["qty"]:
            self.note(f"{sym}: only {qty}/{pos['qty']} closed", logging.ERROR)
            pos["qty"] = qty
        row = self.state.record_exit(sym, price, when)
        row["reason"] = reason
        log_trade(row, self.mode)
        icon = T.EXIT_WIN if row["net"] > 0 else T.EXIT_LOSS
        self.note(f"{icon} EXIT {row['direction']} {sym} x{qty} @ {price:.2f} ({T.reason_label(reason)}) "
                  f"net ₹{row['net']:.2f} ({row['r']:+.2f}R)")

    # -- one cycle
    def cycle(self):
        now = now_ist()
        today = now.date()
        self.state.new_day_if_needed(today)

        # stale positions from an earlier day (paper mode after a crash)
        for sym, p in list(self.state.positions.items()):
            if p["opened"][:10] < today.isoformat():
                self.close_position(sym, p.get("last_price", p["entry"]), "left over from earlier day", now)

        bars_by_sym = {}
        lags = []
        fetched = marketdata.fetch_many(C.WATCHLIST, C.INTERVAL_MIN, "10d", C.FETCH_WORKERS)
        for sym in C.WATCHLIST:
            got = fetched.get(sym)
            if isinstance(got, Exception) or got is None:
                self.rows[sym] = {"reason": f"data error: {got}"}
                continue
            bars, price, _ = got
            closed = data_yahoo.closed_bars(bars, now, C.INTERVAL_MIN)
            if not closed:
                continue
            bars_by_sym[sym] = closed
            self.bars[sym] = closed
            self.prices[sym] = price
            if closed[-1]["t"].date() == today:
                lags.append((now - closed[-1]["t"]).total_seconds() / 60 - C.INTERVAL_MIN)
        self.data_lag_min = max(lags) if lags else None

        index_bars = {}
        if C.INDEX_FILTER:
            for ix in {strategy.index_for(s) for s in bars_by_sym}:
                try:
                    b, _, _ = marketdata.fetch(ix, C.INTERVAL_MIN, "5d")
                    index_bars[ix] = data_yahoo.closed_bars(b, now, C.INTERVAL_MIN)
                except data_yahoo.DataError as e:
                    self.note(f"index {ix}: {e} - {T.term('index_filter')} skipped", logging.WARNING)
        if C.VIX_SIZING and self.vix_day != today:
            try:
                vix = marketdata.vix_by_day("1mo")
                earlier = [d for d in vix if d < today]
                self.vix = vix[max(earlier)] if earlier else None
            except data_yahoo.DataError as e:
                self.vix = None
                self.note(f"India VIX unavailable ({e}) - full size", logging.WARNING)
            self.vix_day = today
            if self.vix is not None:
                self.note(f"India VIX {self.vix:.2f} -> risk x{strategy.vix_multiplier(self.vix)}")

        square_off_time = at(C.SQUARE_OFF, today)

        # 1) manage open positions
        for sym, pos in list(self.state.positions.items()):
            px = self.prices.get(sym, pos["entry"])
            pos["last_price"] = px
            filled_stop = self.broker.stopped_out(sym, pos)
            if filled_stop is not None:
                row = self.state.record_exit(sym, filled_stop, now)
                row["reason"] = "exchange stop"
                log_trade(row, self.mode)
                self.note(f"{T.EXIT_LOSS} EXIT {sym} by exchange stop ({T.name('stop')}) @ {filled_stop:.2f} net ₹{row['net']:.2f}")
                continue
            if now >= square_off_time:
                self.close_position(sym, px, "square-off", now)
                continue
            opened = dt.datetime.fromisoformat(pos["opened"])
            sym_bars = bars_by_sym.get(sym, [])
            for i, b in enumerate(sym_bars):
                if b["t"] + dt.timedelta(minutes=C.INTERVAL_MIN) <= opened:
                    continue
                if b["t"] <= dt.datetime.fromisoformat(pos.get("checked_upto", "1970-01-01T00:00:00+05:30")):
                    continue
                pos["checked_upto"] = b["t"].isoformat()
                pos["held"] = pos.get("held", 0) + 1
                hit = strategy.manage_exit(pos, sym_bars, i, pos["held"])
                if hit:
                    reason, level = hit
                    # paper fills at the level; live exits at the market now
                    self.close_position(sym, px if self.live else level, reason, now)
                    break
        self.state.save()

        # 2) entries
        paused = os.path.exists(C.PAUSE_FILE)
        candidates = []
        for sym, closed in bars_by_sym.items():
            last = closed[-1]
            if last["t"].date() != today or self.last_bar.get(sym) == last["t"]:
                continue
            self.last_bar[sym] = last["t"]
            ev = strategy.evaluate(closed, index_bars=index_bars.get(strategy.index_for(sym)))
            row = {k: ev[k] for k in ("or_high", "or_low", "close", "vwap", "vol_ratio", "reason")}
            row["bar"] = f"{last['t']:%H:%M}"
            self.rows[sym] = row
            if ev["signal"]:
                candidates.append((sym, closed, ev, row))
        # strongest breakout volume first, so scarce slots go to the best signals
        candidates.sort(key=lambda c: -(c[2]["vol_ratio"] or 0.0))
        for sym, closed, ev, row in candidates:
            last = closed[-1]
            if now >= square_off_time:
                continue
            if paused:
                row["reason"] += " (paused)"
                continue
            ok, why = self.state.can_enter(sym, today, self.prices)
            if not ok:
                row["reason"] += f" - skip: {why}"
                continue
            direction, stop = ev["signal"], ev["stop"]
            ref = self.prices.get(sym, last["c"])
            risk_ps = abs(last["c"] - stop)
            chased = (ref - last["c"]) if direction == "LONG" else (last["c"] - ref)
            if chased > C.MAX_CHASE_R * risk_ps:
                row["reason"] += f" - skip: price ran {chased / risk_ps:.1f}R past signal"
                continue
            if (direction == "LONG" and ref <= stop) or (direction == "SHORT" and ref >= stop):
                row["reason"] += " - skip: price already at stop"
                continue
            mult = strategy.vix_multiplier(self.vix) if C.VIX_SIZING else 1.0
            qty, why = risk.size_position(self.state.equity(self.prices), ref, stop, direction,
                                          self.state.open_notional(), mult)
            if qty <= 0:
                row["reason"] += f" - skip: {why}"
                continue
            filled, fill_price, extra = self.broker.enter(sym, direction, qty, ref, stop)
            if filled <= 0:
                self.note(f"{sym}: entry not filled", logging.WARNING)
                continue
            target = strategy.target_for(fill_price, stop, direction)
            self.state.record_entry(sym, direction, filled, fill_price, stop, target, now,
                                    {**extra, "checked_upto": last["t"].isoformat(),
                                     **(ev["context"] or {})})
            self.note(f"{T.ENTER_ICON} ENTER {direction} {sym} x{filled} @ {fill_price:.2f} "
                      f"{T.name('stop')} (stop) {stop:.2f} "
                      + (f"{T.name('target')} (target) {target:.2f}" if target is not None
                         else "no fixed target (trailing)"))
        self.write_status(now)

    def write_status(self, now):
        positions = []
        for sym, p in self.state.positions.items():
            px = self.prices.get(sym, p["entry"])
            positions.append({**p, "symbol": sym, "price": px,
                              "unrealised": costs.gross_pnl(p["entry"], px, p["qty"], p["direction"]),
                              **position_details(p, px, self.bars.get(sym, []), now)})
        status = {
            "time": now.isoformat(), "mode": self.mode, "live_ready": (not self.live) or self.kite is not None,
            "paused": os.path.exists(C.PAUSE_FILE), "data_lag_min": self.data_lag_min,
            "data_source": marketdata.status(),
            "equity": self.state.equity(self.prices), "cash": self.state.d["cash"],
            "day_pnl": self.state.d["day_pnl"], "realized_pnl": self.state.d["realized_pnl"],
            "charges_paid": self.state.d["charges_paid"], "trades_today": self.state.d["trades_today"],
            "loss_streak": self.state.d["loss_streak"], "cooldown_until": self.state.d["cooldown_until"],
            "positions": positions, "rows": self.rows, "events": self.events,
        }
        tmp = os.path.join(C.DATA_DIR, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, default=str, indent=1)
        os.replace(tmp, os.path.join(C.DATA_DIR, "status.json"))

    def unlisted_holiday(self, now):
        """Fallback when the holiday file has no list for this year: after 09:45, no NIFTY bar
        for today (and no open positions) means the exchange is closed."""
        if holidays.year_listed(now.year) or self.holiday_checked == now.date():
            return False
        if now < at(C.MARKET_OPEN, now.date()) + dt.timedelta(minutes=30) or self.state.positions:
            return False
        self.holiday_checked = now.date()
        try:
            bars, _, _ = marketdata.fetch("^NSEI", C.INTERVAL_MIN, "5d")
        except data_yahoo.DataError:
            return False  # can't tell - keep running
        return not any(b["t"].date() == now.date() for b in bars)

    # -- main loop
    def end_of_day(self, now):
        """After the close: save today's (and any missed recent days') candles to the archive,
        while today's Kite login still works. Never stops the bot if it fails."""
        try:
            res = archive.fetch_and_save("5d", now=now)
            msg = (f"Candle archive: saved {len(res['days'])} day(s) from {res['source'].title()}"
                   if res["days"] else "Candle archive: already up to date")
            if res["failed"]:
                msg += f" (no data for {', '.join(res['failed'])})"
            self.note(msg)
        except Exception as e:
            self.note(f"{T.WARN_ICON} candle archive failed: {e!r}", logging.WARNING)

    def run(self):
        if not holidays.year_listed(now_ist().year):
            self.note(f"nse_holidays.json has no {now_ist().year} list - add it from the NSE circular "
                      "(using the NIFTY-data fallback meanwhile)", logging.WARNING)
        print(T.banner(f"{T.mode_label(self.mode)} mode"), flush=True)
        self.note(f"{T.BOW} {T.APP} started in {T.mode_label(self.mode)} mode, watching {len(C.WATCHLIST)} stocks")
        if C.DRY_RUN is False and not self.live:
            self.note("DRY_RUN=false but CONFIRM_LIVE is not YES - staying in PAPER mode", logging.WARNING)
        waiting_login_logged = False
        while True:
            if os.path.exists(C.STOP_FILE):
                self.note("STOP file found - Ayodhya ORB shutting down (open positions stay protected by exchange stops in live mode)")
                os.remove(C.STOP_FILE)
                break
            now = now_ist()
            today = now.date()
            if now.weekday() >= 5:
                self.note(f"Weekend - Ayodhya rests. Next trading day {holidays.next_trading_day(today):%a %d %b}")
                self.write_status(now)
                break
            hname = holidays.holiday_name(today)
            if hname:
                self.note(f"{T.term('holiday')} today ({hname}) - next trading day "
                          f"{holidays.next_trading_day(today):%a %d %b}")
                self.write_status(now)
                break
            if self.unlisted_holiday(now):
                self.note("No NIFTY data by 09:45 and this year isn't in nse_holidays.json - "
                          "treating today as a market holiday", logging.WARNING)
                self.write_status(now)
                break
            if now >= at(C.MARKET_CLOSE, today) + dt.timedelta(minutes=5):
                self.note(f"{T.name('square_off')}: market closed. Day P&L ₹{self.state.d['day_pnl']:.2f}, "
                          f"equity ₹{self.state.equity():.2f}")
                self.end_of_day(now)
                self.write_status(now)
                break
            if not self.ensure_live_broker():
                if not waiting_login_logged:
                    self.note(f"{T.term('live')} mode: waiting for today's Kite login ({T.name('login')}) - open http://127.0.0.1:5000", logging.WARNING)
                    waiting_login_logged = True
                self.write_status(now)
                time.sleep(15)
                continue
            if now >= at(C.MARKET_OPEN, today):
                try:
                    self.cycle()
                except Exception as e:  # keep running; positions are protected by exchange stops
                    self.note(f"cycle error: {e!r}", logging.ERROR)
                    log.exception("cycle error")
            else:
                self.write_status(now)
            # wake shortly after each 15-minute candle closes, plus a check every POLL_SEC
            time.sleep(C.POLL_SEC)


def main():
    setup_logging()
    import web
    threading.Thread(target=web.serve, daemon=True).start()
    Bot().run()


if __name__ == "__main__":
    main()
