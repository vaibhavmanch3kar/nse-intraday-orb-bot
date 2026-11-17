"""
Free NSE candles from Yahoo Finance's chart endpoint (no API key).

A Kite "Personal" app has no historical or quote APIs, so prices come from here.
Yahoo's NSE data can lag the exchange; every fetch reports how stale it is.
"""

import datetime as dt
import time

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0"}

_session = requests.Session()
_session.headers.update(HEADERS)
_session.trust_env = False  # never route market data through a system proxy


class DataError(Exception):
    pass


def yahoo_symbol(symbol: str) -> str:
    """NSE stock 'SBIN' -> 'SBIN.NS'; indices like '^NSEI' stay as they are."""
    return symbol if symbol.startswith("^") or "." in symbol else f"{symbol}.NS"


def fetch(symbol: str, interval_min: int = 15, range_: str = "10d", retries: int = 3, interval=None):
    """Return (bars, last_price, last_price_time) for an NSE symbol or index (^NSEI).
    interval='1d' fetches daily bars instead of interval_min minutes.

    bars: oldest-first list of dicts {t, o, h, l, c, v}; t = bar START (IST, aware).
    Bars with missing values are dropped. The still-forming bar is NOT removed here
    (see closed_bars), because the caller decides what "now" is.
    """
    params = {"interval": interval or f"{interval_min}m", "range": range_}
    last_err = None
    for attempt in range(retries):
        try:
            r = _session.get(URL.format(sym=yahoo_symbol(symbol)), params=params, timeout=15)
            if r.status_code == 429:
                raise DataError("rate limited (429)")
            r.raise_for_status()
            res = r.json()["chart"]["result"]
            if not res:
                raise DataError("empty result")
            return _parse(res[0])
        except (requests.RequestException, KeyError, ValueError, DataError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise DataError(f"{symbol}: {last_err}")


def _parse(res):
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        bars.append({"t": dt.datetime.fromtimestamp(t, IST), "o": float(o), "h": float(h),
                     "l": float(l), "c": float(c), "v": float(v or 0)})
    bars.sort(key=lambda b: b["t"])
    price = meta.get("regularMarketPrice")
    ptime = meta.get("regularMarketTime")
    return (bars,
            float(price) if price is not None else (bars[-1]["c"] if bars else None),
            dt.datetime.fromtimestamp(ptime, IST) if ptime else None)


SKIPPED = "skipped: the page's time limit was reached"


def fetch_many(symbols, interval_min=15, range_="10d", workers=6, interval=None, on_done=None, stop=None):
    """Fetch several symbols in parallel. Returns {symbol: (bars, price, ptime) or DataError}.
    Dashboard jobs only: on_done(symbol) after each symbol (progress); stop() -> True skips the symbols
    not started yet (time limit). The bot calls this without them, exactly as before."""
    from concurrent.futures import ThreadPoolExecutor

    def one(sym):
        if stop and stop():
            return sym, DataError(f"{sym}: {SKIPPED}")
        try:
            return sym, fetch(sym, interval_min, range_, interval=interval)
        except DataError as e:
            return sym, e
        finally:
            if on_done:
                on_done(sym)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return dict(ex.map(one, symbols))


def vix_by_day(range_="6mo"):
    """{date: India VIX close} from daily bars."""
    bars, _, _ = fetch("^INDIAVIX", range_=range_, interval="1d")
    return {b["t"].date(): b["c"] for b in bars}


def closed_bars(bars, now, interval_min=15):
    """Drop bars that haven't finished yet at `now`."""
    step = dt.timedelta(minutes=interval_min)
    return [b for b in bars if b["t"] + step <= now]
