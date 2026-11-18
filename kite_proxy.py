#!/usr/bin/env python3
"""
kite_proxy.py - Kite Connect through a static-IP proxy (e.g. TrueIP).

Only Kite REST calls (login, orders, profile, quotes) go through the proxy.
Everything else in your bot, including KiteTicker, connects directly.

Commands:
    python kite_proxy.py            # all: check egress -> login if needed -> verify
    python kite_proxy.py check      # proxy + egress IP test only
    python kite_proxy.py login      # force a fresh daily login (access token)
    python kite_proxy.py verify     # profile / margins / LTP through the proxy
    python kite_proxy.py ticker     # KiteTicker websocket test (direct, no proxy)

Use from your bot:
    from kite_proxy import get_kite
    kite = get_kite()               # proxy-scoped KiteConnect with today's token

Required environment variables (setup_kite_proxy.ps1 sets these):
    TRUEIP_HOST, TRUEIP_PORT, TRUEIP_USER, TRUEIP_PASS, EXPECTED_EGRESS_IP,
    KITE_API_KEY, KITE_API_SECRET
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# Never route traffic through a process-wide proxy; the proxy is scoped to Kite only.
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_v, None)

import requests
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kex

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / ".kite_token.json"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NIFTY50_TOKEN = 256265


# ---------------------------------------------------------------- config
def env(name, default=None):
    val = os.environ.get(name, default)
    if not val:
        sys.exit(f"[x] Missing environment variable {name}. Run setup_kite_proxy.ps1 first "
                 f"(and open a new terminal afterwards).")
    return val


def proxies():
    host = env("TRUEIP_HOST")
    port = env("TRUEIP_PORT")
    user = quote(env("TRUEIP_USER"), safe="")
    pw = quote(env("TRUEIP_PASS"), safe="")  # URL-encode: @ : / # % would break the URL
    url = f"http://{user}:{pw}@{host}:{port}"
    return {"http": url, "https": url}


# ---------------------------------------------------------------- egress check
def check_egress():
    expected = env("EXPECTED_EGRESS_IP")
    px = proxies()

    try:
        direct = requests.get("https://api.ipify.org", timeout=10).text.strip()
    except requests.RequestException as e:
        direct = f"unavailable ({e.__class__.__name__})"
    print(f"[i] Home (direct) IP     : {direct}")

    t0 = time.perf_counter()
    try:
        r = requests.get("https://api64.ipify.org", proxies=px, timeout=15)
        r.raise_for_status()
    except requests.exceptions.ProxyError as e:
        if "407" in str(e):
            sys.exit("[x] 407 from proxy: wrong TRUEIP_USER / TRUEIP_PASS. "
                     "Re-run setup with -ResetSecrets.")
        sys.exit(f"[x] Proxy error (host/port wrong or port blocked?): {e}")
    except requests.RequestException as e:
        sys.exit(f"[x] Request via proxy failed: {e}")
    rtt_ms = (time.perf_counter() - t0) * 1000
    egress = r.text.strip()
    print(f"[i] Proxy egress IP      : {egress}   ({rtt_ms:.0f} ms round trip)")

    v6 = None
    try:
        r6 = requests.get("https://api6.ipify.org", proxies=px, timeout=10)
        if r6.ok and ":" in r6.text:
            v6 = r6.text.strip()
    except requests.RequestException:
        pass
    if v6:
        print(f"[!] Proxy ALSO has IPv6 egress: {v6}")
        print("    api.kite.trade is dual-stack, so orders may leave from this address.")
        print("    Whitelist it in Kite too (or ask your proxy provider which one Kite traffic uses).")
    else:
        print("[i] Proxy IPv6 egress    : none - Kite traffic will leave via IPv4")

    if egress == direct:
        sys.exit("[x] Egress equals your home IP - traffic is NOT going through the proxy.")
    if egress != expected:
        sys.exit(f"[x] Egress {egress} != EXPECTED_EGRESS_IP {expected}. "
                 f"Whitelist the right IP in Kite and update EXPECTED_EGRESS_IP.")
    print(f"[ok] Egress matches whitelisted IP {expected}")
    return egress, v6


# ---------------------------------------------------------------- token handling
def _last_token_reset(now):
    """Kite access tokens expire around 06:00 IST every day."""
    reset = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return reset if now >= reset else reset - dt.timedelta(days=1)


def _write_token_file(data):
    tmp = TOKEN_FILE.with_name(TOKEN_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, TOKEN_FILE)


def load_token():
    """Today's access token, or None (no login, yesterday's login, or a file this Windows user cannot read).
    The token is kept protected by Windows (secretbox.py). A file from before that, with the token in plain
    text, is still read, and is rewritten protected the first time it is read."""
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
        created = dt.datetime.fromisoformat(data["created"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError):
        return None
    if created < _last_token_reset(dt.datetime.now(IST)):
        return None
    import secretbox
    if "enc" in data:
        try:
            return secretbox.unprotect(data["enc"])
        except (secretbox.Unavailable, ValueError, UnicodeDecodeError):
            return None                      # protected by another user or PC, or damaged: log in again
    token = data.get("access_token")
    if token and secretbox.available():
        try:                                 # an older, plain file: protect it now; on any problem leave it as it is
            _write_token_file({"enc": secretbox.protect(token), "user_id": data.get("user_id"),
                               "created": data["created"]})
        except (secretbox.Unavailable, OSError):
            pass
    return token or None


def save_token(access_token, user_id):
    """Save today's login with the token protected by Windows. Only where Windows protection is not usable
    (not Windows, or Windows refuses) is it saved as before, in plain text; token_storage() says which."""
    import secretbox
    data = {"user_id": user_id, "created": dt.datetime.now(IST).isoformat()}
    try:
        data["enc"] = secretbox.protect(access_token)
    except secretbox.Unavailable:
        data["access_token"] = access_token
    _write_token_file(data)


def token_storage():
    """'protected', 'plain' or 'none': how the saved login is stored. The token itself is not read."""
    try:
        data = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        return "none"
    return "protected" if "enc" in data else "plain" if data.get("access_token") else "none"


# ---------------------------------------------------------------- kite client
def make_kite(access_token=None):
    kite = KiteConnect(api_key=env("KITE_API_KEY"), proxies=proxies(), timeout=10)
    if access_token:
        kite.set_access_token(access_token)
    return kite


def get_kite():
    """Proxy-scoped KiteConnect with today's access token. Import this from your bot."""
    tok = load_token()
    if not tok:
        raise RuntimeError("No valid Kite access token for today. Run: python kite_proxy.py login")
    return make_kite(tok)


def _explain(e):
    msg = str(e)
    hint = ""
    low = msg.lower()
    if "ip" in low and ("allow" in low or "whitelist" in low or "permitted" in low):
        hint = "  -> Egress IP is not whitelisted on your Kite app (developers.kite.trade/apps)."
    elif isinstance(e, kex.TokenException):
        hint = "  -> Token invalid/expired. Run: python kite_proxy.py login"
    elif "api_key" in low or "checksum" in low:
        hint = "  -> Check KITE_API_KEY / KITE_API_SECRET."
    return f"{e.__class__.__name__}: {msg}{hint}"


def login():
    kite = make_kite()
    url = kite.login_url()
    print("\n1) Log in to Zerodha in the browser window that opens (or open this URL):")
    print(f"   {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("2) After login you'll be sent to your app's redirect URL.")
    print("   Copy the FULL URL from the address bar (an error page is fine) and paste it here.")
    raw = input("   URL or request_token: ").strip()
    request_token = parse_qs(urlparse(raw).query).get("request_token", [raw])[0]
    if not request_token:
        sys.exit("[x] No request_token found.")

    try:
        data = kite.generate_session(request_token, api_secret=env("KITE_API_SECRET"))
    except kex.KiteException as e:
        sys.exit(f"[x] Login failed - {_explain(e)}")
    save_token(data["access_token"], data.get("user_id"))
    print(f"[ok] Logged in as {data.get('user_id')}. Token saved to {TOKEN_FILE.name} "
          f"(valid until ~06:00 IST).")
    return data["access_token"]


def verify():
    kite = get_kite()
    try:
        t0 = time.perf_counter()
        p = kite.profile()
        ms = (time.perf_counter() - t0) * 1000
        print(f"[ok] profile()  via proxy : {p['user_id']} / {p.get('user_name')}  ({ms:.0f} ms)")

        m = kite.margins("equity")
        bal = m.get("available", {}).get("live_balance", m.get("net"))
        print(f"[ok] margins()  via proxy : equity live balance = {bal}")
    except kex.KiteException as e:
        sys.exit(f"[x] {_explain(e)}")

    try:
        q = kite.ltp(["NSE:INFY"])
        print(f"[ok] ltp()      via proxy : NSE:INFY = {q['NSE:INFY']['last_price']}")
    except kex.KiteException as e:
        print(f"[!] ltp() failed (quote permission on your app?): {_explain(e)}")

    print("[ok] Kite REST through the static IP is working. Orders will leave from the whitelisted IP.")


# ---------------------------------------------------------------- ticker (direct)
def ticker_test(seconds=10):
    from kiteconnect import KiteTicker

    tok = load_token()
    if not tok:
        sys.exit("[x] No valid token. Run: python kite_proxy.py login")

    # Direct connection on purpose: the static-IP rule covers order APIs, not market data,
    # and ws.kite.trade has no IPv6 record anyway.
    kws = KiteTicker(env("KITE_API_KEY"), tok)
    state = {"connected": False, "ticks": 0, "error": None}

    def on_connect(ws, response):
        state["connected"] = True
        ws.subscribe([NIFTY50_TOKEN])
        ws.set_mode(ws.MODE_LTP, [NIFTY50_TOKEN])

    def on_ticks(ws, ticks):
        state["ticks"] += len(ticks)
        if state["ticks"] <= 3:
            for t in ticks:
                print(f"    tick: NIFTY 50 = {t.get('last_price')}")

    def on_error(ws, code, reason):
        state["error"] = f"{code} {reason}"

    def on_close(ws, code, reason):
        pass

    kws.on_connect, kws.on_ticks = on_connect, on_ticks
    kws.on_error, kws.on_close = on_error, on_close

    print(f"[i] Connecting KiteTicker directly for {seconds}s ...")
    kws.connect(threaded=True)
    time.sleep(seconds)
    try:
        kws.stop_retry()
        kws.close()
    except Exception:
        pass

    if state["connected"]:
        extra = "" if state["ticks"] else " (0 ticks - normal when market is closed)"
        print(f"[ok] KiteTicker connected, {state['ticks']} ticks received{extra}")
    else:
        print(f"[x] KiteTicker did not connect. Error: {state['error']}")
    sys.stdout.flush()
    os._exit(0 if state["connected"] else 1)  # Twisted reactor can't be restarted cleanly


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Kite Connect through a static-IP proxy")
    ap.add_argument("cmd", nargs="?", default="all",
                    choices=["all", "check", "login", "verify", "ticker"])
    ap.add_argument("--seconds", type=int, default=10, help="ticker test duration")
    a = ap.parse_args()

    if a.cmd == "check":
        check_egress()
    elif a.cmd == "login":
        check_egress()
        login()
    elif a.cmd == "verify":
        verify()
    elif a.cmd == "ticker":
        ticker_test(a.seconds)
    else:
        check_egress()
        if load_token():
            print("[i] Reusing today's access token.")
        else:
            login()
        verify()


if __name__ == "__main__":
    main()
