#!/usr/bin/env python3
"""
Market data from Zerodha Kite Connect (needs the paid "Connect" plan on your Kite app).

Same functions as data_yahoo.py (fetch, fetch_many, vix_by_day), so the rest of the bot doesn't care
where prices come from:
  - real-time last prices (one batched LTP call for the whole watchlist);
  - historical candles from Kite, fetched in chunks so backtests can go back much further than
    Yahoo's ~60 days (Kite allows 200 days per call for 15-minute candles, 100 for 5-minute);
  - indices too: ^NSEI -> NIFTY 50, ^NSEBANK -> NIFTY BANK, ^CNXIT -> NIFTY IT, ^INDIAVIX -> INDIA VIX.

Market data goes DIRECT to Kite (not through the static-IP proxy): SEBI's static-IP rule is for
orders, and the proxy would add ~1 s to every call. It uses today's login token (.kite_token.json).

    .venv\\Scripts\\python.exe data_kite.py check     # after upgrading: is Kite data working?
"""

import datetime as dt
import json
import os
import re
import threading
import time
from collections import deque

import config as C
from data_yahoo import IST, DataError

INDEX_NAMES = {"^NSEI": "NIFTY 50", "^NSEBANK": "NIFTY BANK", "^CNXIT": "NIFTY IT", "^INDIAVIX": "INDIA VIX"}
INTERVALS = {1: "minute", 3: "3minute", 5: "5minute", 10: "10minute", 15: "15minute", 30: "30minute",
             60: "60minute"}
# Kite's documented maximum days per historical request, by interval
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100, "15minute": 200,
            "30minute": 200, "60minute": 400, "day": 2000}
HIST_PER_SEC = 3            # Kite allows 3 historical requests per second

_kite = None
_kite_token = None
_tokens = None              # tradingsymbol -> instrument_token
_lock = threading.Lock()
_calls = deque()


class PlanError(DataError):
    """The Kite app has no market-data permission (still on the free Personal plan)."""


# ---------------------------------------------------------------- client
def _client():
    """A Kite client WITHOUT the proxy, with today's token. Raises DataError if not logged in."""
    global _kite, _kite_token
    import kite_proxy  # reads the token file and env vars
    try:
        tok = kite_proxy.load_token()
        key = os.environ.get("KITE_API_KEY")
    except SystemExit as e:  # kite_proxy exits on missing settings
        raise DataError(f"Kite settings missing: {e}")
    if not key:
        raise DataError("KITE_API_KEY is not set - run setup_kite_proxy.ps1")
    if not tok:
        raise DataError("no Kite login today - click 'Log in to Zerodha' on the dashboard")
    if _kite is None or _kite_token != tok:
        from kiteconnect import KiteConnect
        _kite = KiteConnect(api_key=key, timeout=10)
        _kite.set_access_token(tok)
        _kite_token = tok
    return _kite


def _call(fn, *args, historical=False, **kwargs):
    """Run a Kite call, pacing historical requests to 3 per second and translating errors."""
    from kiteconnect import exceptions as kex
    if historical:
        with _lock:
            now = time.monotonic()
            while _calls and now - _calls[0] > 1.0:
                _calls.popleft()
            if len(_calls) >= HIST_PER_SEC:
                time.sleep(max(0.0, 1.0 - (now - _calls[0])) + 0.02)
            _calls.append(time.monotonic())
    try:
        return fn(*args, **kwargs)
    except kex.PermissionException as e:
        raise PlanError(f"Kite says no permission for market data ({e}). Is your app on the paid Connect plan?")
    except kex.TokenException as e:
        raise DataError(f"Kite login expired ({e}) - log in again from the dashboard")
    except kex.KiteException as e:
        raise DataError(f"Kite error: {e}")
    except Exception as e:  # network etc.
        raise DataError(f"Kite request failed: {e}")


# ---------------------------------------------------------------- instruments
def _cache_path():
    return os.path.join(C.DATA_DIR, "kite_instruments_NSE.json")


def instrument_tokens():
    """{tradingsymbol: instrument_token} for NSE, cached in data/ once per day."""
    global _tokens
    today = dt.datetime.now(IST).date().isoformat()
    if _tokens and _tokens.get("_day") == today:
        return _tokens
    p = _cache_path()
    try:
        with open(p, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("_day") == today:
            _tokens = cached
            return _tokens
    except (OSError, ValueError):
        pass
    rows = _call(_client().instruments, "NSE")
    toks = {r["tradingsymbol"]: int(r["instrument_token"]) for r in rows}
    toks["_day"] = today
    os.makedirs(C.DATA_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(toks, f)
    _tokens = toks
    return toks


def kite_symbol(symbol):
    """Our symbol -> Kite tradingsymbol ('SBIN' stays, '^NSEI' -> 'NIFTY 50')."""
    return INDEX_NAMES.get(symbol, symbol)


def token_for(symbol):
    toks = instrument_tokens()
    ks = kite_symbol(symbol)
    if ks not in toks:
        raise DataError(f"{symbol}: not found in Kite's NSE instrument list")
    return toks[ks]


# ---------------------------------------------------------------- candles
def parse_range(range_):
    """'10d' -> 10, '2mo' -> 60, '1y' -> 365 (calendar days)."""
    m = re.fullmatch(r"(\d+)(d|mo|y)", str(range_).strip())
    if not m:
        raise DataError(f"bad range {range_!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 1, "mo": 30, "y": 365}[unit]


def _candles(token, interval, days, now):
    """All candles for the last `days` calendar days, fetched in Kite-sized chunks (oldest first)."""
    step = MAX_DAYS[interval]
    end = now
    start = now - dt.timedelta(days=days)
    out, frm = [], start
    while frm < end:
        to = min(end, frm + dt.timedelta(days=step))
        rows = _call(_client().historical_data, token, frm.replace(tzinfo=None), to.replace(tzinfo=None),
                     interval, historical=True)
        out.extend(rows or [])
        frm = to + dt.timedelta(seconds=1)
    bars, seen = [], set()
    for r in out:
        t = r["date"]
        if isinstance(t, str):
            t = dt.datetime.fromisoformat(t)
        t = t.astimezone(IST) if t.tzinfo else t.replace(tzinfo=IST)
        if t in seen:
            continue
        seen.add(t)
        bars.append({"t": t, "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
                     "c": float(r["close"]), "v": float(r.get("volume") or 0)})
    bars.sort(key=lambda b: b["t"])
    return bars


def fetch(symbol, interval_min=15, range_="10d", retries=2, interval=None):
    """Same contract as data_yahoo.fetch: (bars oldest-first incl. the forming bar, last price, time)."""
    kint = "day" if interval == "1d" else INTERVALS.get(int(interval_min))
    if not kint:
        raise DataError(f"Kite has no {interval_min}-minute candles")
    now = dt.datetime.now(IST)
    last_err = None
    for attempt in range(retries):
        try:
            bars = _candles(token_for(symbol), kint, parse_range(range_), now)
            return bars, (bars[-1]["c"] if bars else None), now
        except PlanError:
            raise
        except DataError as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise DataError(f"{symbol}: {last_err}")


def fetch_many(symbols, interval_min=15, range_="10d", workers=3, interval=None):
    """Fetch several symbols (paced to Kite's limit), then overwrite each last price with a single
    batched real-time LTP call. Returns {symbol: (bars, price, time) or DataError}."""
    out = {}
    for s in symbols:
        try:
            out[s] = fetch(s, interval_min, range_, interval=interval)
        except PlanError:
            raise
        except DataError as e:
            out[s] = e
    ok = [s for s in symbols if not isinstance(out.get(s), Exception)]
    if ok:
        try:
            keys = {f"NSE:{kite_symbol(s)}": s for s in ok}
            ltp = _call(_client().ltp, list(keys))
            now = dt.datetime.now(IST)
            for k, v in (ltp or {}).items():
                s = keys.get(k)
                if s and v.get("last_price"):
                    bars, _, _ = out[s]
                    out[s] = (bars, float(v["last_price"]), now)
        except DataError:
            pass  # candles are still fine; price falls back to the last candle close
    return out


def vix_by_day(range_="6mo"):
    bars, _, _ = fetch("^INDIAVIX", range_=range_, interval="1d")
    return {b["t"].date(): b["c"] for b in bars}


# ---------------------------------------------------------------- check
def check():
    """Plain-language check after upgrading the Kite app."""
    print("Checking Kite Connect market data ...")
    try:
        _client()
    except DataError as e:
        print(f"[x] {e}")
        return 1
    try:
        ltp = _call(_client().ltp, ["NSE:SBIN", "NSE:NIFTY 50"])
        print(f"[ok] Live prices: SBIN {ltp['NSE:SBIN']['last_price']}, NIFTY 50 {ltp['NSE:NIFTY 50']['last_price']}")
        bars, _, _ = fetch("SBIN", 15, "5d")
        print(f"[ok] 15-min candles: {len(bars)} for SBIN, last {bars[-1]['t']:%a %d %b %H:%M}" if bars
              else "[!] 15-min candles: none returned (market holiday?)")
        long_bars, _, _ = fetch("SBIN", 15, "180d")
        days = len({b['t'].date() for b in long_bars})
        print(f"[ok] History: {days} trading days of 15-min candles available (Yahoo gives ~60)")
    except PlanError as e:
        print(f"[x] {e}")
        print("    Upgrade the app to 'Connect' at https://developers.kite.trade/apps, then log in again.")
        return 1
    except DataError as e:
        print(f"[x] {e}")
        return 1
    print("[ok] Kite data works.")
    if (C.DATA_SOURCE or "").lower() == "kite":
        print("[ok] The bot is set to use Kite data (DATA_SOURCE = kite). Nothing else to do.")
    else:
        print("     To use it, run once in PowerShell, then restart the bot:")
        print('     [Environment]::SetEnvironmentVariable("DATA_SOURCE", "kite", "User")')
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(check() if (len(sys.argv) < 2 or sys.argv[1] == "check") else 2)
