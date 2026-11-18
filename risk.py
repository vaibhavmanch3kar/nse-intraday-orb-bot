"""Position sizing, risk gates and persistent bot state."""

import datetime as dt
import json
import math
import os

import config as C
import safefile
import costs

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def size_position(equity, entry, stop, direction, open_notional=0.0, risk_mult=1.0):
    """Shares such that (stop loss + all charges) <= RISK_PER_TRADE_PCT x risk_mult of equity,
    within the per-stock and total exposure caps. Returns (qty, reason)."""
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0:
        return 0, "stop equals entry"
    budget = equity * C.RISK_PER_TRADE_PCT / 100 * risk_mult
    by_risk = int(budget // per_share_risk)
    cap_one = equity * C.MAX_POSITION_PCT / 100
    cap_all = max(0.0, equity * C.MAX_EXPOSURE_PCT / 100 - open_notional)
    by_one, by_all = int(cap_one // entry), int(cap_all // entry)
    qty = min(by_risk, by_one, by_all)
    if qty < C.MIN_QTY:
        # Say WHICH limit stops the trade. All three used to be reported as "risk budget too small for 1 share",
        # which was wrong whenever the share price, not the risk, was the reason.
        if by_risk < C.MIN_QTY:
            return 0, (f"1 share would risk ₹{per_share_risk:,.2f}, more than the ₹{budget:,.2f} risk budget "
                       f"({C.RISK_PER_TRADE_PCT * risk_mult:g}% of ₹{equity:,.0f})")
        if by_one < C.MIN_QTY:
            return 0, (f"1 share costs ₹{entry:,.2f}, more than the {C.MAX_POSITION_PCT:g}% per-stock limit "
                       f"(₹{cap_one:,.0f} of ₹{equity:,.0f})")
        return 0, (f"1 share costs ₹{entry:,.2f}, more than the ₹{cap_all:,.0f} left under the "
                   f"{C.MAX_EXPOSURE_PCT:g}% total exposure limit")
    # charges are partly fixed (₹20 brokerage cap), so step down until risk + charges fit
    while qty >= C.MIN_QTY:
        loss = per_share_risk * qty + costs.round_trip(entry, stop, qty, direction)
        if loss <= budget:
            return qty, "ok"
        qty = min(qty - 1, int(qty * budget / loss))
    return 0, f"risk budget too small for 1 share (₹{budget:,.2f} doesn't cover the stop loss plus charges)"


class State:
    """Cash, P&L, streaks, today's counters and open positions. Saved after every change."""

    def __init__(self, path, start_cash):
        self.path = path
        self.d = {
            "cash": start_cash, "realized_pnl": 0.0, "charges_paid": 0.0,
            "day": None, "day_start_equity": start_cash, "day_pnl": 0.0,
            "trades_today": 0, "traded_symbols": [], "loss_streak": 0,
            "cooldown_until": None, "positions": {},
        }
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.d.update(json.load(f))

    # -- persistence
    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=2, default=str)
        safefile.replace(tmp, self.path)

    # -- accessors
    @property
    def positions(self):
        return self.d["positions"]

    def equity(self, prices=None):
        """Cash + unrealised P&L of open positions at `prices` (entry price if unknown).
        Positions are tracked as margin-free notional: cash is not reduced on entry."""
        eq = self.d["cash"]
        for sym, p in self.positions.items():
            px = (prices or {}).get(sym, p["entry"])
            eq += costs.gross_pnl(p["entry"], px, p["qty"], p["direction"])
        return eq

    def open_notional(self):
        return sum(p["entry"] * p["qty"] for p in self.positions.values())

    # -- day roll
    def new_day_if_needed(self, today: dt.date):
        if self.d["day"] != today.isoformat():
            self.d.update(day=today.isoformat(), day_start_equity=self.equity(),
                          day_pnl=0.0, trades_today=0, traded_symbols=[])
            self.save()

    # -- gates
    def open_pnl(self, prices=None):
        """Profit/loss of open positions at `prices` (0 for any position without a price)."""
        if not prices:
            return 0.0
        return self.equity({s: prices[s] for s in self.positions if s in prices}) - self.d["cash"]

    def can_enter(self, symbol, today: dt.date, prices=None):
        """Entry gates. `prices` (symbol -> latest price) lets the daily loss limit count losses
        in open positions too, not only booked ones."""
        if self.d["cooldown_until"] and today.isoformat() < self.d["cooldown_until"]:
            return False, f"cooldown until {self.d['cooldown_until']} after {C.LOSS_STREAK_LIMIT} losses"
        day_loss = self.d["day_pnl"] + self.open_pnl(prices)
        if day_loss <= -self.d["day_start_equity"] * C.MAX_DAILY_LOSS_PCT / 100:
            return False, f"daily loss limit {C.MAX_DAILY_LOSS_PCT}% hit (booked + open)"
        if self.d["trades_today"] >= C.MAX_TRADES_PER_DAY:
            return False, f"{C.MAX_TRADES_PER_DAY} trades today"
        if len(self.positions) >= C.MAX_OPEN_POSITIONS:
            return False, f"{C.MAX_OPEN_POSITIONS} positions open"
        if symbol in self.positions:
            return False, "already in position"
        if symbol in self.d["traded_symbols"]:
            return False, "already traded today"
        return True, "ok"

    # -- bookkeeping
    def record_entry(self, symbol, direction, qty, entry, stop, target, when, extra=None):
        self.positions[symbol] = {"direction": direction, "qty": qty, "entry": entry,
                                  "stop": stop, "target": target, "opened": str(when),
                                  **(extra or {})}
        self.d["trades_today"] += 1
        self.d["traded_symbols"].append(symbol)
        self.save()

    def record_exit(self, symbol, exit_price, when):
        p = self.positions.pop(symbol)
        gross = costs.gross_pnl(p["entry"], exit_price, p["qty"], p["direction"])
        charges = costs.round_trip(p["entry"], exit_price, p["qty"], p["direction"])
        net = gross - charges
        self.d["cash"] += net
        self.d["realized_pnl"] += net
        self.d["charges_paid"] += charges
        self.d["day_pnl"] += net
        if net < 0:
            self.d["loss_streak"] += 1
            if self.d["loss_streak"] >= C.LOSS_STREAK_LIMIT:
                until = dt.date.fromisoformat(self.d["day"] or str(dt.date.today())) + dt.timedelta(days=C.COOLDOWN_DAYS + 1)
                self.d["cooldown_until"] = until.isoformat()
                self.d["loss_streak"] = 0
        else:
            self.d["loss_streak"] = 0
        self.save()
        risk = abs(p["entry"] - p["stop"]) * p["qty"]
        return {**p, "symbol": symbol, "exit": exit_price, "closed": str(when), "gross": gross,
                "charges": charges, "net": net, "r": net / risk if risk else 0.0}


def round_tick(price, direction_up: bool):
    """Round to the 0.05 tick, away from the market for marketable limits."""
    n = price / C.TICK
    return round((math.ceil(n) if direction_up else math.floor(n)) * C.TICK, 2)
