"""
When is a saved Review or Scan out of date? Saved results follow the market data, not a clock:

- A daily candle is final SETTLE_MIN after the close (15:30 + 15 min). A scan stays valid until the next
  trading day's daily candle is final, so the Scanner downloads once a day instead of every 30 minutes.
- A past session never changes: its Review is kept for good.
- Today's session, while the market is open, is "in progress": its Review is out of date as soon as the
  next 15-minute candle has closed (plus CANDLE_LAG_MIN for the data source to have it).
- Any result built with different settings is out of date.

Pure functions of (saved result, now): no files, no network.
"""

import datetime as dt

import config as C
import holidays

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SETTLE_MIN = 15        # minutes after MARKET_CLOSE when the day's candles are final
CANDLE_LAG_MIN = 2     # minutes after a 15-min candle closes before the data source is expected to have it
PARTIAL_RETRY_MIN = 10 # a result cut short by the time limit is retried after this long


def _at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def settle_time(day):
    return _at(day, C.MARKET_CLOSE) + dt.timedelta(minutes=SETTLE_MIN)


def is_trading_day(day):
    try:
        return holidays.is_trading_day(day)
    except Exception:  # no holiday list: weekdays only
        return day.weekday() < 5


def last_candle_close(now):
    """The most recent 15-min candle close today that the data source should already have, or None
    (not a trading day, or before the first candle has closed)."""
    now = now.astimezone(IST)
    if not is_trading_day(now.date()):
        return None
    t, end = _at(now.date(), C.MARKET_OPEN), _at(now.date(), C.MARKET_CLOSE)
    last = None
    while True:
        t += dt.timedelta(minutes=C.INTERVAL_MIN)
        if t > end or t + dt.timedelta(minutes=CANDLE_LAG_MIN) > now:
            return last
        last = t


def settled_since(since, now):
    """The latest trading day whose candles became final after `since` and by `now`, or None."""
    d = now.astimezone(IST).date()
    while d >= since.astimezone(IST).date():
        if is_trading_day(d):
            s = settle_time(d)
            if since < s <= now:
                return d
        d -= dt.timedelta(days=1)
    return None


def in_session(day, now):
    """True while `day` is today's trading session and its candles are not final yet."""
    return day == now.astimezone(IST).date() and is_trading_day(day) and now < settle_time(day)


def built_at(c):
    try:
        b = dt.datetime.fromisoformat(c["built"])
        return b if b.tzinfo else b.replace(tzinfo=IST)
    except (KeyError, TypeError, ValueError):
        return None


def changed_settings(saved, keys):
    """Names of the settings in `keys` whose current value differs from `saved` ({} if none saved)."""
    if not isinstance(saved, dict):
        return list(keys)
    return [k for k in keys if k in saved and saved[k] != _jsonish(getattr(C, k, None))]


def _jsonish(v):
    return list(v) if isinstance(v, tuple) else v
