"""
Is this PC's clock right?

Everything the bot does is timed by the PC's clock: which 15-minute candle has closed, the last-entry time, the
15:00 square-off. A clock a couple of minutes fast makes the bot treat a candle that is still forming as
closed and trade on it; a clock many minutes slow lets Zerodha's own square-off come before the bot's.

skew_seconds() compares the clock with Zerodha's server time, read from the Date header of its API host (one
small request, no login, nothing about the account). The header is in whole seconds and the request takes a
moment, so the answer is good to about two seconds: more than enough to catch a clock that is minutes off.
"""

import email.utils
import time

import requests

URL = "https://api.kite.trade"

_session = requests.Session()
_session.trust_env = False          # straight to Zerodha, like market data: never through a system proxy


def server_epoch(timeout=5):
    """(Zerodha's server time, this PC's time at the same moment), both as seconds since 1970, or None if it
    can't be read."""
    try:
        before = time.time()
        r = _session.head(URL, timeout=timeout, allow_redirects=False)
        after = time.time()
        server = email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp()
    except Exception:  # noqa: BLE001 - no network, no header, a header that can't be read: simply not known
        return None
    return server, (before + after) / 2


def skew_seconds(probe=server_epoch):
    """How many seconds this PC's clock is AHEAD of Zerodha's (negative: behind), or None if not known."""
    got = probe()
    if not got:
        return None
    server, local = got
    return local - server
