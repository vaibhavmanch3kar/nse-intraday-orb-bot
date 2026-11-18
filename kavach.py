"""
Kavach: the one risk engine. Every new position, from any strategy, in paper, real money or a backtest, is
decided here, by the same checks in the same order. It is deterministic: the same inputs always give the same
answer, and it never looks at anything it was not given. It never places an order; it answers "may this trade be
taken, with how many shares, and if not, which rule stopped it".

    d = kavach.pre_trade(state, "SBIN", "LONG", entry=1110.5, stop=1104.0, today=day, prices=prices, ...)
    if d.ok: broker.enter(..., d.qty, ...)   else: show d.reason

The checks, in order (the first one that fails stops the trade):
   1. time           not after the square-off time
   2. paused         Pause, an Apply-Now restart or a Paper | Real Money switch waiting; or on hold because an
                     order's fate is unknown or the bot's positions don't match Zerodha's
   3. balance        real money: today's Zerodha balance was read
   4. data           real money needs Kite prices (never delayed Yahoo); the candles passed validation;
                     the signal is not older than MAX_SIGNAL_AGE_MIN; the index filter could be checked
   5. account        cool-down after losses, daily loss limit (booked + open), trades a day, open positions,
                     already in this stock, already traded it today (so no averaging down, no second entry)
   6. price          the price has not run more than MAX_CHASE_R past the signal, and is not at the stop
   7. sector         at most MAX_PER_SECTOR open positions in one sector and direction (0 = off)
   8. size           shares from the risk budget, the per-stock limit, the total exposure limit and charges
   9. liquidity      at most MAX_BAR_VOLUME_PCT of the signal candle's volume (0 = off)

Checks 4, 7 and 9 are new in Ayodhya ORB 2.0; the others are the rules the bot always had, moved here unchanged.
A backtest gives no data-health input (its candles are complete by construction), so check 4 does not apply there.
"""

from dataclasses import dataclass, field

import config as C
import risk

CHECKS = ("time", "paused", "balance", "data", "account", "price", "sector", "size", "liquidity")


@dataclass
class Decision:
    ok: bool
    qty: int = 0
    reason: str = ""            # plain words, for the watchlist row and the journal ("" when ok)
    check: str = ""             # which of CHECKS stopped it ("" when ok)
    checks: list = field(default_factory=list)   # [(check, passed, detail)] in the order they ran

    def __bool__(self):
        return self.ok


def _stop(d, check, reason):
    d.checks.append((check, False, reason))
    d.ok, d.qty, d.reason, d.check = False, 0, reason, check
    return d


def data_problem(data):
    """The first data reason not to trade, or None. `data` is what the bot knows about its prices this round:
    {live, source, problems: [text], signal_age_min, index_missing}. None (a backtest) means no data checks."""
    if not data:
        return None
    if data.get("live") and (data.get("source") or "").lower() != "kite":
        return (f"real-money entries need Kite prices; the bot is on {data.get('source') or 'no'} data "
                f"(delayed) right now")
    if data.get("problems"):
        return f"price data problem: {data['problems'][0]}"
    age = data.get("signal_age_min")
    if C.MAX_SIGNAL_AGE_MIN > 0 and age is not None and age > C.MAX_SIGNAL_AGE_MIN:
        return (f"the signal candle closed {age:.0f} min ago, older than the {C.MAX_SIGNAL_AGE_MIN:g} min "
                f"limit (stale data)")
    if data.get("index_missing") and C.INDEX_FILTER:
        return "index data is missing, so the index filter can't be checked"
    return None


def sector_count(state, symbol, direction, sector_of):
    sec = sector_of(symbol)
    return sec, sum(1 for s, p in state.positions.items()
                    if p.get("direction") == direction and sector_of(s) == sec)


def pre_trade(state, symbol, direction, entry, stop, today, *, prices=None, equity=None, risk_mult=1.0,
              now=None, square_off=None, paused=False, hold=None, balance_ok=True, balance_problem=None,
              data=None, signal_close=None, bar_volume=None, sector_of=None):
    """Decide one new position. `state` is the account (risk.State); `entry` is the price it would be bought or
    sold at; `prices` are the latest prices (for the daily loss limit and the account's value); `equity`
    overrides the account value used for sizing (a backtest sizes from booked equity)."""
    d = Decision(True)

    def passed(check, detail="ok"):
        d.checks.append((check, True, detail))

    # 1. time
    if square_off is not None and now is not None and now >= square_off:
        return _stop(d, "time", "after the square-off time")
    passed("time")
    # 2. paused
    if paused:
        return _stop(d, "paused", "paused")
    if hold:                                  # the broker's state is unclear, or positions don't match Zerodha
        return _stop(d, "paused", hold)
    passed("paused")
    # 3. balance (real money)
    if not balance_ok:
        return _stop(d, "balance", balance_problem or "Zerodha balance not read yet")
    passed("balance")
    # 4. data
    why = data_problem(data)
    if why:
        return _stop(d, "data", why)
    passed("data", "not applicable" if not data else "ok")
    # 5. account limits
    ok, why = state.can_enter(symbol, today, prices)
    if not ok:
        return _stop(d, "account", why)
    passed("account")
    # 6. price
    if signal_close is not None:
        risk_ps = abs(signal_close - stop)
        chased = (entry - signal_close) if direction == "LONG" else (signal_close - entry)
        if chased > C.MAX_CHASE_R * risk_ps:
            return _stop(d, "price", f"price ran {chased / risk_ps:.1f}R past signal")
    if (direction == "LONG" and entry <= stop) or (direction == "SHORT" and entry >= stop):
        return _stop(d, "price", "price already at stop")
    passed("price")
    # 7. sector
    if C.MAX_PER_SECTOR > 0 and sector_of is not None:
        sec, n = sector_count(state, symbol, direction, sector_of)
        if n >= C.MAX_PER_SECTOR:
            return _stop(d, "sector", f"already {n} {direction} position{'s' if n != 1 else ''} in the same "
                                      f"sector ({sec}); limit {C.MAX_PER_SECTOR}")
    passed("sector", "off" if C.MAX_PER_SECTOR <= 0 else "ok")
    # 8. size
    eq = state.equity(prices) if equity is None else equity
    qty, why = risk.size_position(eq, entry, stop, direction, state.open_notional(), risk_mult)
    if qty <= 0:
        return _stop(d, "size", why)
    passed("size", f"{qty} shares")
    # 9. liquidity
    if C.MAX_BAR_VOLUME_PCT > 0 and bar_volume:
        most = int(bar_volume * C.MAX_BAR_VOLUME_PCT / 100)
        if most < C.MIN_QTY:
            return _stop(d, "liquidity", f"too little volume: the signal candle traded {bar_volume:,.0f} shares")
        if qty > most:
            passed("liquidity", f"cut from {qty} to {most} shares ({C.MAX_BAR_VOLUME_PCT:g}% of the candle's volume)")
            qty = most
        else:
            passed("liquidity")
    else:
        passed("liquidity", "off" if C.MAX_BAR_VOLUME_PCT <= 0 else "no volume given")
    d.qty = qty
    return d


# What each limit is, its value now and its built-in default: for the Kavach page and for checking that the
# settings in force are the intended ones. Nothing here changes a setting.
LIMITS = (
    ("RISK_PER_TRADE_PCT", 0.5, "% of the account a single trade may lose, charges included"),
    ("MAX_POSITION_PCT", 25.0, "% of the account in one stock"),
    ("MAX_EXPOSURE_PCT", 75.0, "% of the account in all open positions together (no leverage)"),
    ("MAX_OPEN_POSITIONS", 3, "open positions at one time"),
    ("MAX_TRADES_PER_DAY", 4, "new trades in a day"),
    ("MAX_DAILY_LOSS_PCT", 2.0, "% loss in a day (booked + open) after which no new trade opens"),
    ("LOSS_STREAK_LIMIT", 3, "losing trades in a row before the cool-down"),
    ("COOLDOWN_DAYS", 2, "trading days off after that"),
    ("MAX_CHASE_R", 0.3, "R the price may run past the signal before the entry is skipped"),
    ("MAX_SIGNAL_AGE_MIN", 5.0, "minutes after its candle closed that a signal may still be traded"),
    ("MAX_PER_SECTOR", 0, "open positions in one sector and direction (0 = no limit)"),
    ("MAX_BAR_VOLUME_PCT", 2.0, "% of the signal candle's volume one order may be (0 = no limit)"),
)


def limits():
    """[{name, value, default, changed, what}] for every limit Kavach enforces."""
    out = []
    for name, default, what in LIMITS:
        value = getattr(C, name)
        out.append({"name": name, "value": value, "default": default, "changed": value != default, "what": what})
    return out


def looser_than_default():
    """Limits set looser than the built-in default (a larger number is looser for all of them except the
    cool-down length, where a smaller number is). [] is the expected answer."""
    out = []
    for x in limits():
        if x["name"] in ("MAX_PER_SECTOR", "MAX_BAR_VOLUME_PCT"):
            loose = x["default"] > 0 and (x["value"] <= 0 or x["value"] > x["default"])
        elif x["name"] == "COOLDOWN_DAYS":
            loose = x["value"] < x["default"]
        else:
            loose = x["value"] > x["default"]
        if loose:
            out.append(x)
    return out
