#!/usr/bin/env python3
"""
Kite NSE intraday opening-range-breakout bot.

    .venv\\Scripts\\python.exe bot.py          # PAPER mode (default); the dashboard is web.py (3-DASHBOARD.bat)
    New-Item STOP                             # graceful stop (or the dashboard's Stop button)

Live trading needs DRY_RUN=false AND CONFIRM_LIVE=YES, AND real money armed today on the dashboard
(livemode.py), plus today's Kite login. Without today's arming it trades PAPER.
"""

import csv
import datetime as dt
import json
import logging
import os
import sys
import time

import archive
import backup
import buildinfo
import capital
import config as C
import costs
import data_yahoo
import datacheck
import marketdata
import holidays
import journal
import kavach
import livemode
import reports
import restart
import risk
import safefile
import strategy
import theme as T
import watchdog
from broker import LiveBroker, PaperBroker

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
log = logging.getLogger("ayodhya")


def now_ist():
    return dt.datetime.now(IST)


class DailyFileHandler(logging.Handler):
    """One log file per day: data/logs/bot_YYYY-MM-DD.log (India date), switching at midnight."""

    def __init__(self, folder):
        super().__init__()
        self.folder, self.day, self.stream = folder, None, None

    def emit(self, record):
        try:
            day = dt.datetime.fromtimestamp(record.created, IST).date().isoformat()
            if day != self.day:
                self.close_stream()
                os.makedirs(self.folder, exist_ok=True)
                self.stream = open(os.path.join(self.folder, f"bot_{day}.log"), "a", encoding="utf-8")
                self.day = day
            self.stream.write(self.format(record) + "\n")
            self.stream.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close_stream(self):
        if self.stream:
            self.stream.close()
        self.stream, self.day = None, None

    def close(self):
        self.close_stream()
        super().close()


def split_old_log(data_dir):
    """Once: split the old single data/bot.log into data/logs/bot_<date>.log files by the date on each
    line (lines without a date, like error tracebacks, stay with the line before). The original is
    kept as data/logs/bot_until_<last date>.log. Returns the dates written ([] if nothing to do)."""
    old = os.path.join(data_dir, "bot.log")
    if not os.path.exists(old):
        return []
    folder = os.path.join(data_dir, "logs")
    os.makedirs(folder, exist_ok=True)
    by_day, day = {}, None
    with open(old, encoding="utf-8", errors="replace") as f:
        for line in f:
            if len(line) > 10 and line[4] == "-" and line[7] == "-" and line[:4].isdigit():
                day = line[:10]
            if day:
                by_day.setdefault(day, []).append(line)
    # move the original first: if that fails nothing has changed, and a later start can't split twice
    keep = os.path.join(folder, f"bot_until_{max(by_day) if by_day else 'start'}.log")
    os.replace(old, keep)
    for d, lines in by_day.items():
        path = os.path.join(folder, f"bot_{d}.log")
        existing = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write("".join(lines) + existing)
        os.replace(path + ".tmp", path)
    return sorted(by_day)


def setup_logging():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        split = split_old_log(C.DATA_DIR)
    except OSError as e:   # e.g. the old log is open in an editor: try again at the next start
        split = f"not yet ({e})"
    fh = DailyFileHandler(os.path.join(C.DATA_DIR, "logs"))
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:                                   # secrets removed from every line, and the same lines as JSON (eventlog.py)
        import eventlog
        eventlog.install("bot", os.path.join(C.DATA_DIR, "logs"))
    except Exception:  # noqa: BLE001 - a logging extra must never stop the bot
        pass
    if isinstance(split, str):
        log.warning(f"the old data\\bot.log was not split into data\\logs {split}")
    elif split:
        log.info(f"the old data\\bot.log is now one file per day in data\\logs ({len(split)} days, "
                 f"{split[0]} to {split[-1]}); the original is kept there as bot_until_{split[-1]}.log")


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


def data_problem(err) -> str:
    """A long requests/urllib3 error in a few plain words (the full text stays on the dashboard row)."""
    s = str(err)
    if "NameResolution" in s or "getaddrinfo" in s or "Failed to resolve" in s:
        return "can't look up the data server's address (internet or DNS down?)"
    if "ConnectTimeout" in s or "timed out" in s.lower():
        return "connection to the data server timed out"
    if "429" in s:
        return "the data server is rate-limiting us (429)"
    if "Max retries" in s or "ConnectionError" in s or "Connection aborted" in s:
        return "can't connect to the data server"
    return s[:150]


class Bot:
    def __init__(self):
        # Real money needs both Windows settings AND today's arming on the dashboard (livemode.py); otherwise
        # paper, whoever starts the bot (the 9:00 task, a restart, or by hand).
        self.live, self.live_note = livemode.bot_mode(C.is_live())
        self.mode = "live" if self.live else "paper"
        # A new REAL MONEY record starts at ₹0, never a made-up amount: it trades only after reading the real
        # Zerodha balance (sync_live_balance). Paper keeps its virtual opening balance.
        self.state = risk.State(os.path.join(C.DATA_DIR, f"state_{self.mode}.json"),
                                0.0 if self.live else C.PAPER_START_INR)
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
        self.good_rows = {}     # symbol -> last evaluation row, shown again once data is back
        self.data_down_since = None  # time the current price-data problem started (None = data OK)
        self.started_at = now_ist()
        self.journal = None     # today's journal.DayJournal (recording only - never read by trading code)
        self.journal_warned = set()
        self.restart_noted = None   # last "restart requested - waiting" message (said once)
        self.restart_hold = False   # Apply Now is waiting for a safe moment: no new entries meanwhile (PAPER)
        self.switch = None          # Paper | Real Money change waiting: "to_paper" / "to_live" (livemode.py)
        self.switch_noted = None    # last "switch waiting" message (said once per reason)
        self.balance_ok = not self.live   # REAL MONEY: no new trades until today's Zerodha balance is read
        self.balance_problem = None       # why the balance could not be used (shown on the dashboard)
        self.balance_tried = None         # monotonic time of the last read attempt (retry once a minute)
        self.status_save_failed = False   # the last status.json save failed (warned once, see write_status)
        self.feed = {}                    # price-feed health of the last round (dashboard; see cycle)
        self.feed_warned = set()          # (stock, data problem) already said in the log today
        self.recon = {}                   # last comparison of the bot's record with Zerodha (reconcile)
        self.recon_hold = None            # why new entries are held after that comparison (None = not held)
        self.recon_said = set()           # mismatches already said in the log
        self.last_recon = None            # when the record was last compared with Zerodha
        self.launcher_gone = lambda: False   # main() sets this when run_bot.ps1 passes --launcher-pid
        try:                    # what this bot runs: version and file/setting hashes, for the dashboard
            self.build = buildinfo.snapshot("bot")
        except Exception:  # noqa: BLE001 - never stops the bot
            self.build = None

    # -- helpers
    def note(self, msg, level=logging.INFO):
        log.log(level, msg)
        self.events = ([f"{now_ist():%H:%M:%S} {msg}"] + self.events)[:30]

    def entry_hold(self):
        """Why no new entry may be sent right now, in plain words, or None. REAL MONEY only: an order whose fate
        is unknown (broker.uncertain), or the bot's record not matching Zerodha (reconcile)."""
        unsure = getattr(self.broker, "uncertain", None)
        if unsure:
            return f"order state uncertain ({unsure})"
        return self.recon_hold

    def price_source(self):
        """'kite' or 'yahoo': where this round's prices really came from (marketdata falls back to Yahoo when
        Kite can't be used). Kavach refuses real-money entries on anything but Kite."""
        return (marketdata.status() or {}).get("using")

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
        self.sync_live_balance(now_ist())
        return True

    def sync_live_balance(self, now):
        """REAL MONEY only: size trades from the actual Zerodha balance, not the bot's own record. Reads the equity
        balance (read-only) and uses the smaller of today's opening cash and the cash available now; pledged
        shares (collateral) don't count. It becomes today's cash, and the daily loss limit follows from it.
        With positions open (a restart mid-trade) the record is kept: Zerodha's figure then has margin blocked.
        If the balance can't be read, or is zero or less, no new trades open (open ones are still managed);
        it never falls back to a made-up amount."""
        if not self.live or self.kite is None:
            return
        import time as _t
        self.balance_tried = _t.monotonic()
        today = now.date().isoformat()
        if self.state.positions:
            self.balance_ok = self.state.d.get("balance_day") == today
            self.balance_problem = (None if self.balance_ok else
                                    "positions are open and today's Zerodha balance was not read before them")
            return
        try:
            avail = (self.kite.margins("equity") or {}).get("available") or {}
            vals = [float(avail[k]) for k in ("opening_balance", "live_balance") if avail.get(k) is not None]
            bal = min(vals) if vals else None
        except Exception as e:  # noqa: BLE001 - network, proxy or Zerodha error: no new trades
            bal, err = None, type(e).__name__
        else:
            err = None if bal is not None else "no balance in Zerodha's answer"
        if bal is None or bal <= 0:
            why = f"could not read your Zerodha balance ({err})" if bal is None else f"Zerodha balance is ₹{bal:,.2f}"
            if self.balance_problem != why:
                self.note(f"REAL MONEY: no new trades - {why}. Open positions are still managed.", logging.ERROR)
                livemode.log("balance", "NOT USABLE", why)
            self.balance_ok, self.balance_problem = False, why
            return
        d = self.state.d
        d["cash"] = bal
        if d.get("day") != today or d.get("balance_day") != today:
            d["day_start_equity"] = bal       # the 2% daily loss limit is of the real balance
        d.update(balance_day=today, balance_inr=bal, balance_at=now.isoformat(timespec="seconds"))
        self.state.save()
        self.balance_ok, self.balance_problem = True, None
        self.note(f"REAL MONEY: balance from Zerodha ₹{bal:,.2f} - trades are sized from it")
        livemode.log("balance", "read", f"₹{bal:,.2f}")

    def apply_paper_capital(self):
        """At start-up, before any trade: apply an opening-balance change chosen on the dashboard.
        PAPER mode only - live trading never reads the paper capital setting."""
        if self.live:
            return
        try:
            msg = capital.apply_pending(self.state, now_ist())
        except Exception as e:  # a bad settings file must never stop the bot
            self.note(f"paper opening balance change not applied ({e!r})", logging.WARNING)
            return
        if msg:
            self.note(msg)

    def log_data_health(self, fetched, now):
        """One log line when price data starts failing and one when it is back (not one per round)."""
        failed = [s for s in C.WATCHLIST if isinstance(fetched.get(s), Exception) or fetched.get(s) is None]
        if failed and self.data_down_since is None:
            self.data_down_since = now
            self.note(f"⚠ price data problem: {len(failed)} of {len(C.WATCHLIST)} stocks failed "
                      f"({data_problem(fetched.get(failed[0]))}) - retrying every round", logging.WARNING)
        elif not failed and self.data_down_since is not None:
            mins = (now - self.data_down_since).total_seconds() / 60
            self.note(f"price data back: all {len(C.WATCHLIST)} stocks OK "
                      f"(problem from {self.data_down_since:%H:%M}, about {mins:.0f} min)")
            self.data_down_since = None

    # -- daily journal (recording only: a journal problem is a warning, never a reason to stop)
    def journal_warn(self, text):
        if text and text not in self.journal_warned:
            self.journal_warned.add(text)
            self.note(f"{T.WARN_ICON} journal: {text}", logging.WARNING)

    def journal_round(self, now, candidates=(), square_off_time=None):
        try:
            if self.journal is None or self.journal.day != now.date().isoformat():
                rebuilt = journal.backfill(self.mode, now.date())
                if rebuilt:
                    self.note(f"journal: saved {len(rebuilt)} earlier day(s) ({', '.join(rebuilt)})")
                self.journal = journal.DayJournal(self.mode, now.date(), self.started_at, self.state)
                for path in reports.catch_up(self.mode, now.date()):
                    self.note(f"{T.SCROLL} report saved (missed after the last close): "
                              f"{os.path.join('data', 'reports', os.path.basename(path))}")
            j = self.journal
            for sym, _closed, ev, row in candidates:
                pos = self.state.positions.get(sym)
                taken = bool(pos) and pos.get("opened") == str(now)
                reason = row.get("reason", "")
                if " - skip: " in reason:
                    why = reason.split(" - skip: ", 1)[1]
                elif "(paused)" in reason:
                    why = "paused"
                elif square_off_time is not None and now >= square_off_time:
                    why = "after square-off time"
                else:
                    why = "entry not filled"
                j.signal(now, sym, row.get("bar", ""), ev.get("signal"), taken, why)
            j.round(now, self.state.equity(self.prices), len(self.state.positions), self.data_down_since)
            self.journal_warn(j.save())
        except Exception as e:  # noqa: BLE001
            self.journal_warn(f"could not update today's journal ({e!r})")

    def journal_finish(self, now, how):
        if self.journal is None or self.journal.day != now.date().isoformat():
            return
        try:
            self.journal.finish(now, self.state.equity(self.prices), how)
            self.journal_warn(self.journal.save())
        except Exception as e:  # noqa: BLE001
            self.journal_warn(f"could not close today's journal ({e!r})")

    def reconcile(self, startup=True):
        """REAL MONEY: compare the bot's positions and unfinished orders with Zerodha (broker.recover), at every
        start and every RECON_EVERY_SEC. What is safe is corrected here: a position Zerodha no longer holds is
        dropped, a smaller one is resized (its stop is placed again for the new size), a leftover working order
        is cancelled. Anything else is a problem only you can decide: it is said loudly and new entries are held
        until it is gone. The bot never opens or closes a position because of a mismatch."""
        if not self.live or not hasattr(self.broker, "recover"):
            return
        now = now_ist()
        self.last_recon = now
        try:
            rep = self.broker.recover(self.state.positions, startup=startup)
        except Exception as e:  # noqa: BLE001 - Zerodha can't be read just now: say so, try again next time
            self.recon = {**self.recon, "at": now.isoformat(timespec="seconds"), "ok": False,
                          "error": f"{type(e).__name__}: {e}"}
            if startup:
                self.note(f"could not compare the bot's positions with Zerodha at the start ({type(e).__name__}) "
                          f"- it will be tried again", logging.WARNING)
            return
        for sym in rep["drop"]:
            self.note(f"{sym}: no open MIS position on Kite - dropping it from state", logging.WARNING)
            self.state.positions.pop(sym, None)
        for sym, qty in rep["resize"].items():
            pos = self.state.positions[sym]
            self.note(f"{sym}: Zerodha holds {qty} shares, the bot had {pos['qty']} on record - corrected, and the "
                      f"stop-loss is placed again for {qty}", logging.WARNING)
            if pos.get("sl_order_id"):
                self.broker.cancel(pos["sl_order_id"])
            pos.update(qty=qty, sl_order_id=None)       # ensure_stop places a stop for the new size this round
        for text in rep["cancelled"]:
            self.note(f"cancelled a leftover order of the bot: {text}", logging.WARNING)
        for text in rep["problems"]:
            if text not in self.recon_said:
                self.recon_said.add(text)
                self.note(f"{T.WARN_ICON} THE BOT'S RECORD DOES NOT MATCH ZERODHA: {text}. New entries are on "
                          f"hold until this is resolved.", logging.ERROR)
        if not rep["problems"] and self.recon_hold:
            self.note("the bot's record matches Zerodha again: new entries are allowed")
        self.recon_hold = (f"the bot's record does not match Zerodha ({rep['problems'][0]})"
                           if rep["problems"] else None)
        self.recon = {"at": now.isoformat(timespec="seconds"), "ok": True, "startup": startup,
                      "problems": rep["problems"], "notes": rep["notes"], "cancelled": rep["cancelled"],
                      "dropped": rep["drop"], "resized": rep["resize"]}
        self.state.save()

    def drain_broker(self):
        """Put what the live broker had to say (order problems, late fills) in the log and the events list."""
        msgs = getattr(self.broker, "messages", None)
        if msgs:
            for level, text in list(msgs):
                self.note(text, level)
            msgs.clear()

    def close_position(self, sym, ref_price, reason, when):
        pos = self.state.positions[sym]
        qty, price = self.broker.exit(sym, pos, ref_price, reason)
        if qty <= 0:
            self.note(f"{T.WARN_ICON} {sym}: exit FAILED ({reason}) - check Kite", logging.ERROR)
            return
        keep = None
        if qty < pos["qty"]:
            # Part of the position is still open. It used to be forgotten here (the whole position was booked
            # as closed); now the rest stays on record, gets a new exchange stop and is managed until it is out.
            left = pos["qty"] - qty
            self.note(f"{sym}: only {qty}/{pos['qty']} closed - {left} shares are still open; the bot keeps "
                      f"managing them", logging.ERROR)
            keep = {**pos, "qty": left, "sl_order_id": None}
            pos["qty"] = qty
        row = self.state.record_exit(sym, price, when)
        row["reason"] = reason
        log_trade(row, self.mode)
        if keep:
            self.state.positions[sym] = keep
            self.state.save()
        icon = T.EXIT_WIN if row["net"] > 0 else T.EXIT_LOSS
        self.note(f"{icon} EXIT {row['direction']} {sym} x{qty} @ {price:.2f} ({T.reason_label(reason)}) "
                  f"net ₹{row['net']:.2f} ({row['r']:+.2f}R)")

    # -- one cycle
    def cycle(self):
        now = now_ist()
        today = now.date()
        if self.live and not self.balance_ok:      # retry reading the Zerodha balance, once a minute
            import time as _t
            if self.balance_tried is None or _t.monotonic() - self.balance_tried >= 60:
                self.sync_live_balance(now)
        self.state.new_day_if_needed(today)

        # stale positions from an earlier day (paper mode after a crash)
        for sym, p in list(self.state.positions.items()):
            if p["opened"][:10] < today.isoformat():
                self.close_position(sym, p.get("last_price", p["entry"]), "left over from earlier day", now)

        bars_by_sym = {}
        lags = []
        fetched = marketdata.fetch_many(C.WATCHLIST, C.INTERVAL_MIN, "10d", C.FETCH_WORKERS)
        watchdog.beat()                                 # the slow part of a round (price requests) is done
        feed_problems = {}                              # symbol -> what is wrong with today's candles
        for sym in C.WATCHLIST:
            got = fetched.get(sym)
            if isinstance(got, Exception) or got is None:
                self.rows[sym] = {"reason": f"data error: {got}"}
                continue
            bars, price, _ = got
            if self.rows.get(sym, {}).get("reason", "").startswith("data error"):
                # data is back: don't leave the old error on the dashboard until the next candle closes
                self.rows[sym] = self.good_rows.get(sym) or {
                    "reason": f"data OK again at {now:%H:%M} - waiting for the next candle"}
            closed = data_yahoo.closed_bars(bars, now, C.INTERVAL_MIN)
            if not closed:
                continue
            bars_by_sym[sym] = closed
            self.bars[sym] = closed
            self.prices[sym] = price
            probs = datacheck.problems(closed, today, C.INTERVAL_MIN)
            if probs:
                feed_problems[sym] = probs
                if (sym, probs[0]) not in self.feed_warned:      # said once per stock and problem
                    self.feed_warned.add((sym, probs[0]))
                    self.note(f"{T.WARN_ICON} {sym}: price data problem: {probs[0]} - no new entry in it "
                              f"while this lasts", logging.WARNING)
            if closed[-1]["t"].date() == today:
                lags.append((now - closed[-1]["t"]).total_seconds() / 60 - C.INTERVAL_MIN)
        self.data_lag_min = max(lags) if lags else None
        self.log_data_health(fetched, now)

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

        # REAL MONEY housekeeping before anything else: finish orders left open-ended (broker.settle), and
        # every few minutes compare the bot's record with Zerodha (reconcile).
        if self.live and hasattr(self.broker, "settle"):
            try:
                self.broker.settle()
            except Exception as e:  # noqa: BLE001 - never stops the round; positions keep their exchange stops
                self.note(f"could not finish checking open-ended orders ({type(e).__name__})", logging.WARNING)
            self.drain_broker()
            if self.last_recon is None or (now - self.last_recon).total_seconds() >= C.RECON_EVERY_SEC:
                self.reconcile(startup=False)

        # 1) manage open positions ("Close all positions now" on the dashboard closes them all, once)
        close_all = livemode.take_close_all(now)
        if close_all:
            self.note(f"Close all positions requested on the dashboard: closing {len(self.state.positions)}")
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
            if self.live and hasattr(self.broker, "ensure_stop"):
                # the position's stop-loss must exist at the exchange on every round, not only when it was placed
                stop_state, new_id = self.broker.ensure_stop(sym, pos)
                if stop_state == "replaced":
                    pos["sl_order_id"] = new_id
                    self.state.save()
                    self.note(f"{sym}: the exchange stop-loss was missing and has been placed again", logging.WARNING)
                elif stop_state == "missing":
                    self.note(f"{T.WARN_ICON} {sym}: no exchange stop-loss could be placed - closing the position",
                              logging.ERROR)
                    pos["sl_order_id"] = None
                    self.close_position(sym, px, "stop order missing", now)
                    continue
            if close_all:
                self.close_position(sym, px, "closed from dashboard", now)
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
        # Apply Now or a Paper | Real Money switch waiting: no new entries (open positions are still managed above)
        paused = os.path.exists(C.PAUSE_FILE) or self.restart_hold or self.switch is not None
        candidates, pickups = [], []
        for sym, closed in bars_by_sym.items():
            last = closed[-1]
            if last["t"].date() != today or self.last_bar.get(sym) == last["t"]:
                continue
            self.last_bar[sym] = last["t"]
            pickups.append((now - last["t"]).total_seconds() - C.INTERVAL_MIN * 60)   # seconds after its close
            ev = strategy.evaluate(closed, index_bars=index_bars.get(strategy.index_for(sym)))
            row = {k: ev[k] for k in ("or_high", "or_low", "close", "vwap", "vol_ratio", "reason")}
            row["bar"] = f"{last['t']:%H:%M}"
            self.rows[sym] = self.good_rows[sym] = row
            if ev["signal"]:
                candidates.append((sym, closed, ev, row))
        source = self.price_source()
        newest = max((b[-1]["t"] for b in bars_by_sym.values() if b[-1]["t"].date() == today), default=None)
        # Feed health for the dashboard: where prices come from, the newest finished candle, how long after its
        # close the bot got it, and what failed. Recording only.
        self.feed = {"source": source, "checked_at": now.isoformat(timespec="seconds"),
                     "newest_candle": f"{newest:%H:%M}" if newest else None,
                     "pickup_sec": round(max(pickups), 1) if pickups else self.feed.get("pickup_sec"),
                     "stocks_ok": len(bars_by_sym) - len(feed_problems), "stocks": len(C.WATCHLIST),
                     "problems": {s: p[0] for s, p in feed_problems.items()},
                     "errors": sorted(s for s in C.WATCHLIST if isinstance(fetched.get(s), Exception)
                                      or fetched.get(s) is None),
                     "live_needs_kite": bool(self.live and source != "kite")}
        # strongest breakout volume first, so scarce slots go to the best signals
        candidates.sort(key=lambda c: -(c[2]["vol_ratio"] or 0.0))
        for sym, closed, ev, row in candidates:
            last = closed[-1]
            direction, stop = ev["signal"], ev["stop"]
            ref = self.prices.get(sym, last["c"])
            mult = strategy.vix_multiplier(self.vix) if C.VIX_SIZING else 1.0
            # What the bot knows about this stock's prices right now. Kavach refuses the entry on delayed prices
            # in real money, on candles that failed validation, on a stale signal, or when the index filter
            # could not be checked (kavach.data_problem).
            data = {"live": self.live, "source": source, "problems": feed_problems.get(sym),
                    "signal_age_min": ((now - last["t"]).total_seconds() / 60) - C.INTERVAL_MIN,
                    "index_missing": bool(C.INDEX_FILTER) and not datacheck.has_bar(
                        index_bars.get(strategy.index_for(sym)), last["t"])}
            # Kavach decides every entry: time, pause, balance, data, account limits, price, sector, size
            decision = kavach.pre_trade(
                self.state, sym, direction, ref, stop, today, prices=self.prices, risk_mult=mult, now=now,
                square_off=square_off_time, paused=paused, hold=self.entry_hold(), balance_ok=self.balance_ok,
                balance_problem=self.balance_problem, data=data, signal_close=last["c"],
                bar_volume=last.get("v"), sector_of=strategy.sector_for)
            if not decision.ok:
                if decision.check == "paused" and decision.reason == "paused":
                    row["reason"] += " (paused)"
                elif decision.check != "time":          # after square-off: nothing to say, as before
                    row["reason"] += f" - skip: {decision.reason}"
                continue
            qty = decision.qty
            filled, fill_price, extra = self.broker.enter(sym, direction, qty, ref, stop)
            self.drain_broker()
            if filled <= 0:
                why = (extra or {}).get("reason")
                self.note(f"{sym}: entry not filled" + (f" - {why}" if why else ""), logging.WARNING)
                if why:
                    row["reason"] += f" - skip: {why}"
                continue
            target = strategy.target_for(fill_price, stop, direction)
            self.state.record_entry(sym, direction, filled, fill_price, stop, target, now,
                                    {**extra, "checked_upto": last["t"].isoformat(),
                                     **(ev["context"] or {})})
            self.note(f"{T.ENTER_ICON} ENTER {direction} {sym} x{filled} @ {fill_price:.2f} "
                      f"{T.name('stop')} (stop) {stop:.2f} "
                      + (f"{T.name('target')} (target) {target:.2f}" if target is not None
                         else "no fixed target (trailing)"))
        self.drain_broker()
        self.journal_round(now, candidates, square_off_time)
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
            "positions": positions, "rows": self.rows, "events": self.events, "feed": self.feed,
            # REAL MONEY: the last comparison with Zerodha, and why new entries are on hold (None = they aren't)
            "recon": self.recon if self.live else None, "entry_hold": self.entry_hold() if self.live else None,
            "build": self.build,
            # Paper | Real Money (livemode.py): a switch waiting, whether this bot's own environment has both live
            # settings, and whether real money is armed today. Read by the dashboard; never changes the mode.
            "switch": self.switch, "live_env": C.is_live(), "armed": livemode.arm_valid(now)[0],
            # REAL MONEY: the Zerodha balance trades are sized from (None in paper)
            "balance": ({"ok": self.balance_ok, "inr": self.state.d.get("balance_inr"),
                         "at": self.state.d.get("balance_at"), "problem": self.balance_problem}
                        if self.live else None),
        }
        # The status file only feeds the dashboard. If it can't be saved (another program holds it even after
        # safefile's retries, the disk is full, ...) that is a warning, said once, never a reason to stop: an
        # unsaved status used to end the bot outside market hours, with the error shown only in its window.
        try:
            tmp = os.path.join(C.DATA_DIR, "status.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, default=str, indent=1)
            safefile.replace(tmp, os.path.join(C.DATA_DIR, "status.json"))
        except OSError as e:
            if not self.status_save_failed:
                log.warning(f"could not save the dashboard status file ({type(e).__name__}: {e}) - "
                            "the bot keeps running and tries again next round")
            self.status_save_failed = True
        else:
            if self.status_save_failed:
                log.info("dashboard status file saved again")
            self.status_save_failed = False

    def _balance(self):
        import capital
        return capital.current_opening_balance(self.state.d)

    def check_restarted(self):
        """After an Apply-Now restart: check this start against what the previous bot had (restart.py).
        Any successful start is recorded, so "Go back to the previous version" knows where to go."""
        b = self.build or {}
        checks = restart.verify(self.mode, list(self.state.positions), self.state.d["trades_today"],
                                self.state.d["cash"], self._balance(), os.getpid(), b.get("version"),
                                disk_version=b.get("version"))
        if checks is not None:
            failed = [k for k, ok in checks.items() if not ok]
            if failed:
                self.note(f"Restart check FAILED: {', '.join(failed)} - look at the dashboard", logging.ERROR)
                return
            self.note(f"Restarted to apply the changes ({b.get('version')}) - all checks passed")
        if b.get("commit"):
            restart.record_good_start(b.get("version"), b.get("commit"))

    def apply_if_requested(self, now):
        """Apply Now (restart.py): restart only when safe; first check the new code in a separate process;
        save the journal and state; exit with restart.EXIT_RESTART (run_bot.ps1 starts the bot again)."""
        req, bad = restart.take_request()
        if bad:
            self.note(f"Restart request refused: {bad}", logging.WARNING)
        if not req:
            self.restart_hold, self.restart_noted = False, None
            return None
        today = now.date()
        import data_kite
        ok, why = restart.is_safe(self.live, len(self.state.positions), now, at(C.MARKET_OPEN, today),
                                  at(C.MARKET_CLOSE, today), 0.0 if self.live else data_kite.candle_hold(now))
        if not ok:
            self.restart_hold = not self.live
            if self.restart_noted != why:
                hold = "; no new entries meanwhile" if self.restart_hold else ""
                self.note(f"Restart pending - waiting for safe conditions: {why}{hold}")
                restart.set_stage("waiting", reason=why + hold, nonce=req["nonce"])
                self.restart_noted = why
            return None
        self.restart_hold = False
        restart.set_stage("checking", reason="checking the new code in a separate process", nonce=req["nonce"])
        import preflight
        res = preflight.run_separately()
        if not res.get("ok"):
            try:
                os.remove(restart.request_path())
            except OSError:
                pass
            errs = "; ".join(res.get("errors", [])[:3])
            restart.set_stage("failed", reason=f"the new code can't start, so the bot keeps running the old code: {errs}",
                              errors=res.get("errors", []))
            restart.log("apply now", "new code failed the check", errs)
            self.note(f"Changes NOT applied: the new code can't start ({errs}). Still running the old code.",
                      logging.ERROR)
            return None
        restart.set_stage("stopping", reason="saving the journal and state", nonce=req["nonce"])
        if self.journal is not None:
            self.journal_warn(self.journal.save())
        self.state.save()
        self.note(f"Restarting to apply the changes ({why})")
        self.write_status(now)
        restart.begin(req, self.mode, list(self.state.positions), self.state.d["trades_today"], self.state.d["cash"],
                      self._balance(), os.getpid(), (self.build or {}).get("version"), now)
        return restart.EXIT_RESTART

    def switch_mode_if_requested(self, now):
        """Paper | Real Money (livemode.py). The mode is fixed while the bot runs, so a switch is a restart.
        To paper: no new real-money trades at once; the open ones are managed as usual (stop orders, trailing stop,
        square-off close); the restart waits until all of them have closed. To real money: only with both Windows
        settings in this bot's own environment AND today's arming; it waits for no open paper position and holds
        new paper entries meanwhile. Returns restart.EXIT_RESTART when it is time (run_bot.ps1 starts the bot again
        with the same settings, and livemode.bot_mode decides the new mode). After the close it never restarts:
        the day ends in the mode it traded, and the next start decides the mode."""
        today = now.date()
        if now >= at(C.MARKET_CLOSE, today):
            return None
        if self.live:
            if not livemode.paper_requested():
                self.switch = self.switch_noted = None
                return None
            self.switch, target = "to_paper", "paper"
            n = len(self.state.positions)
            if n:
                if self.switch_noted != n:
                    self.note(f"Switch to PAPER requested: no new real-money trades; managing {n} open position"
                              f"{'s' if n > 1 else ''} until {'they close' if n > 1 else 'it closes'}")
                    self.switch_noted = n
                return None
        else:
            livemode.clear_paper_request()      # already paper: nothing to wait for
            if not (C.is_live() and livemode.arm_valid(now)[0]):
                self.switch = self.switch_noted = None
                return None
            self.switch, target = "to_live", "live"
            import data_kite
            ok, why = restart.is_safe(False, len(self.state.positions), now, at(C.MARKET_OPEN, today),
                                      at(C.MARKET_CLOSE, today), data_kite.candle_hold(now))
            if not ok:
                if self.switch_noted != why:
                    self.note(f"Real money armed: switching at the next safe moment ({why}); no new paper entries "
                              "meanwhile")
                    self.switch_noted = why
                return None
        label = "REAL MONEY" if target == "live" else "PAPER"
        if self.journal is not None:
            self.journal_warn(self.journal.save())
        self.state.save()
        self.note(f"Switching to {label}: restarting")
        livemode.log("switch", f"restarting in {label}", f"from {self.mode}")
        self.write_status(now)
        restart.begin(None, self.mode, list(self.state.positions), self.state.d["trades_today"], self.state.d["cash"],
                      self._balance(), os.getpid(), (self.build or {}).get("version"), now, to_mode=target)
        return restart.EXIT_RESTART

    def check_changes_at_close(self, now):
        """Apply After Market Close: the changes load at the next start anyway; check them now, so a problem
        shows this evening instead of at 09:00 tomorrow."""
        if not restart.after_close_requested():
            return
        import preflight
        res = preflight.run_separately()
        try:
            os.remove(restart.after_close_path())
        except OSError:
            pass
        if res.get("ok"):
            restart.set_stage("checked", reason=f"the changes ({res.get('version')}) passed the check at the close "
                                                 "and load at the next start")
            restart.log("apply after close", "check passed", str(res.get("version")))
            self.note("Changes checked at the close: they load at the next start")
        else:
            errs = "; ".join(res.get("errors", [])[:3])
            restart.set_stage("failed", reason=f"the changes failed the check at the close (the next start would "
                                               f"fail): {errs}", errors=res.get("errors", []))
            restart.log("apply after close", "CHECK FAILED", errs)
            self.note(f"Changes checked at the close: they would NOT start ({errs})", logging.ERROR)

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
        self.save_reports(now)
        self.backup_data(now)

    def backup_data(self, now):
        """Last thing after the close: zip the data folder (journal, reports, logs, trades, state)."""
        try:
            r = backup.backup(now.date())
            self.note(f"data backup: {os.path.basename(r['path'])} ({r['files']} files, {r['bytes'] / 1e6:.1f} MB) in "
                      f"{os.path.dirname(r['path'])}" + (f"; removed {len(r['removed'])} old" if r["removed"] else ""))
        except Exception as e:  # noqa: BLE001 - a backup problem never stops the bot
            self.note(f"{T.WARN_ICON} data backup failed ({e!r})", logging.WARNING)

    def save_reports(self, now):
        """After the close on the last trading day of a week or month: the saved weekly/monthly report."""
        try:
            for period, key in reports.due(now.date()):
                path = reports.write(self.mode, period, key)
                if path:
                    self.note(f"{T.SCROLL} {period}ly report saved: "
                              f"{os.path.join('data', 'reports', os.path.basename(path))}")
        except Exception as e:  # noqa: BLE001 - a report problem never stops the bot
            self.note(f"{T.WARN_ICON} weekly/monthly report not saved ({e!r})", logging.WARNING)

    def run(self):
        if not holidays.year_listed(now_ist().year):
            self.note(f"nse_holidays.json has no {now_ist().year} list - add it from the NSE circular "
                      "(using the NIFTY-data fallback meanwhile)", logging.WARNING)
        print(T.banner(f"{T.mode_label(self.mode)} mode"), flush=True)
        self.note(f"{T.BOW} {T.APP} started in {T.mode_label(self.mode)} mode, watching {len(C.WATCHLIST)} stocks")
        if C.DRY_RUN is False and C.CONFIRM_LIVE != "YES":
            self.note("DRY_RUN=false but CONFIRM_LIVE is not YES - staying in PAPER mode", logging.WARNING)
        if self.live_note:
            self.note(self.live_note, logging.WARNING)
            livemode.log("bot start", "PAPER (not armed today)")
        elif self.live:
            livemode.log("bot start", "REAL MONEY (settings on and armed today)")
        self.apply_paper_capital()
        self.write_status(now_ist())
        self.check_restarted()
        watchdog.bot_started(now_ist(), os.getpid())    # any start ends "stopped on purpose today"
        for e in watchdog.last_events(now_ist()):       # why this start happened, if the watchdog caused it
            self.note(f"Watchdog at {e['at'][11:19]}: {e['text']}", logging.WARNING)
        waiting_login_logged = False
        while True:
            watchdog.beat()                             # alive and going round: see watchdog.check (hang detection)
            if os.path.exists(C.STOP_FILE):
                self.note("STOP file found - Ayodhya ORB shutting down (open positions stay protected by exchange stops in live mode)")
                self.journal_finish(now_ist(), "stopped")
                os.remove(C.STOP_FILE)
                watchdog.mark_stopped("Stop button or STOP file")   # the watchdog leaves it stopped today
                break
            if self.launcher_gone():
                # Stopping the "Kite ORB Bot" task (or killing run_bot.ps1) ends only the launcher; v1.8.0
                # kept trading on its own after that. Now it stops the same way as the STOP file.
                self.note("The launcher (run_bot.ps1 / the scheduled task) was stopped - Ayodhya ORB shutting down "
                          "(open positions stay protected by exchange stops in live mode)")
                self.journal_finish(now_ist(), "stopped")
                watchdog.mark_stopped("the launcher or the scheduled task was stopped")
                break
            rc = self.apply_if_requested(now_ist()) or self.switch_mode_if_requested(now_ist())
            if rc:
                return rc
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
                self.journal_finish(now, "complete")
                self.end_of_day(now)
                self.check_changes_at_close(now)
                self.write_status(now)
                break
            # A trading day: start today's counters now (trades today, day P&L, the daily loss limit's base),
            # not only at the first round after 09:15, so the dashboard never shows yesterday's numbers as
            # today's before the open. Same reset as cycle() does; it runs once a day.
            self.state.new_day_if_needed(today)
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
            self.nap(C.POLL_SEC)

    def nap(self, seconds):
        """Sleep between checks, but wake within a second when asked to stop (STOP file or launcher gone),
        instead of up to a minute later. Nothing else changes: the next check runs as before."""
        end = time.monotonic() + seconds
        beat_at = time.monotonic()
        while True:
            left = end - time.monotonic()
            if left <= 0 or os.path.exists(C.STOP_FILE) or self.launcher_gone():
                return
            if time.monotonic() - beat_at >= 15:        # waiting between rounds is healthy: keep the heartbeat fresh
                watchdog.beat()
                beat_at = time.monotonic()
            time.sleep(min(1.0, left))


SINGLE_INSTANCE_MUTEX = "Local\\AyodhyaORB_bot"   # run_bot.ps1 checks this same name
_instance_handle = None


def claim_single_instance(name=SINGLE_INSTANCE_MUTEX):
    """Hold a named Windows mutex while the bot runs, so a second bot can't start and the launcher
    can check "already running?" instantly. Windows releases it when the process ends, even after
    a crash. Returns False if another bot already holds it; True otherwise (and on other systems)."""
    global _instance_handle
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    handle = k32.CreateMutexW(None, False, name)
    if not handle:
        return True  # can't tell - don't block the start
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        return False
    _instance_handle = handle
    return True


def main():
    setup_logging()
    if not claim_single_instance():
        log.warning(f"{T.APP} is already running - not starting a second copy "
                    "(dashboard: http://127.0.0.1:5000)")
        return
    # The dashboard is its own program now (web.py, started by run_bot.ps1 or 3-DASHBOARD.bat), so it can
    # be updated and restarted without touching the trading bot, and its slow pages never share this process.
    if not dashboard_listening():
        log.info(f"Dashboard not running - double-click 3-DASHBOARD.bat to open it "
                 f"(http://{C.WEB_HOST}:{C.WEB_PORT}); the bot trades the same without it")
    enable_fault_log()
    b = None
    try:
        b = Bot()
        b.launcher_gone = watch_launcher(sys.argv)
        rc = b.run()
    except KeyboardInterrupt:
        # Ctrl+C in the bot's window is a deliberate stop: say so, and the watchdog leaves the bot stopped today
        log.warning(f"{T.APP} stopped with Ctrl+C (open positions stay protected by exchange stops in live mode)")
        watchdog.mark_stopped("Ctrl+C in the bot's window")
        return
    except Exception as e:  # noqa: BLE001 - the last line of defence: nothing may end the bot without a record
        # An error that reaches here ends the bot. It used to be shown only in the bot's window, which then
        # closed; now the full error goes to the log and to data/logs/crash_<time>.txt, and the exit code tells
        # run_bot.ps1 (which asks watchdog.py whether to start the bot again).
        path = watchdog.record_crash(e, mode=getattr(b, "mode", None),
                                     version=(getattr(b, "build", None) or {}).get("version"),
                                     positions=list(b.state.positions) if b is not None else None)
        log.critical(f"{T.APP} ENDED WITH AN ERROR: {type(e).__name__}: {e} "
                     f"(full details: {path or 'could not be saved'}; open positions stay protected by exchange "
                     f"stops in live mode)", exc_info=True)
        sys.exit(EXIT_CRASH)
    if rc:
        sys.exit(rc)   # restart.EXIT_RESTART: run_bot.ps1 starts the bot again


EXIT_CRASH = 1          # ended with an error (also what Python itself uses); 0 = ended normally, 3 = restart
_fault_file = None


def enable_fault_log():
    """A crash below Python (in a C library) leaves at least its stack in data/logs/fault.log."""
    global _fault_file
    try:
        import faulthandler
        if faulthandler.is_enabled():      # someone else (e.g. the test runner) already listens
            return
        os.makedirs(os.path.join(C.DATA_DIR, "logs"), exist_ok=True)
        _fault_file = open(os.path.join(C.DATA_DIR, "logs", "fault.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file)
    except (OSError, RuntimeError, ValueError):
        pass


def watch_launcher(argv):
    """`--launcher-pid N` (run_bot.ps1 passes its own process id): returns a function that says whether that
    process has ended. It holds a handle to the process, so a reused process id can't fool it. Without the
    option (bot started by hand), or off Windows, it always says no."""
    if "--launcher-pid" not in argv or os.name != "nt":
        return lambda: False
    try:
        pid = int(argv[argv.index("--launcher-pid") + 1])
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    except (ValueError, IndexError, OSError):
        return lambda: False
    if not handle:
        log.warning(f"--launcher-pid {argv[argv.index('--launcher-pid') + 1]}: no such process - not watching it")
        return lambda: False
    k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    return lambda: k32.WaitForSingleObject(handle, 0) == 0   # WAIT_OBJECT_0: the launcher has ended


def dashboard_listening(timeout=0.5):
    import socket
    try:
        with socket.create_connection((C.WEB_HOST, C.WEB_PORT), timeout=timeout):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    main()
