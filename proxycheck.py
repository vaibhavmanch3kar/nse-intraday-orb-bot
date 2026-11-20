"""
Proxycheck: is the static-IP proxy working right now? For the "Static-IP proxy" line of the dashboard's System
health card (health.py).

Every real-money order and every Kite request goes through the static-IP proxy, because Zerodha accepts orders
only from the whitelisted address. While the bot trades real money the dashboard checks the proxy in the
background, every CHECK_EVERY_SEC, and after a failed check again after RETRY_SEC:
  1. one request through the proxy to an IP-echo service: the traffic must leave from EXPECTED_EGRESS_IP, the
     address whitelisted at Zerodha;
  2. one request through the proxy to Kite's public API address: no login, nothing about the account.

Only the result is kept: whether it worked, how long it took and the kind of error. Never the address, the proxy
login or anything else from an answer. A check never blocks a page: the page shows the last result.
The bot does not load this file; nothing here sends or cancels an order.

    .venv\\Scripts\\python.exe proxycheck.py        # one check now, printed
"""

import datetime as dt
import threading
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CHECK_EVERY_SEC = 300         # while it works
RETRY_SEC = 60                # after a failed check
TIMEOUT_SEC = 10              # per request
ECHO_URL = "https://api64.ipify.org"
KITE_URL = "https://api.kite.trade/"
ENABLED = True                # the test suite turns this off: tests never reach the network

_lock = threading.Lock()
_state = {"last": None, "fails": 0, "failing_since": None, "running": False}


def now_ist():
    return dt.datetime.now(IST)


def probe():
    """One check: {"ok", "egress_ok", "ms", "error"}. ok = Kite's API answered through the proxy; egress_ok = the
    traffic left from the whitelisted address (None when that could not be seen). Never raises."""
    t0 = time.monotonic()

    def ms():
        return int((time.monotonic() - t0) * 1000)
    try:
        import requests

        import config  # noqa: F401 - loads the saved Kite and proxy settings, as the dashboard does
        import kite_proxy as kp
        px = kp.proxies()
        want = kp.env("EXPECTED_EGRESS_IP")
        got = requests.get(ECHO_URL, proxies=px, timeout=TIMEOUT_SEC).text.strip()
        egress_ok = bool(got) and got == want
        r = requests.get(KITE_URL, proxies=px, timeout=TIMEOUT_SEC)
        if r.status_code >= 500:
            return {"ok": False, "egress_ok": egress_ok, "ms": ms(), "error": f"Kite answered HTTP {r.status_code}"}
        return {"ok": True, "egress_ok": egress_ok, "ms": ms(), "error": None}
    except (Exception, SystemExit) as e:  # noqa: BLE001 - missing settings make kite_proxy exit: also a failure
        # the kind of error only: a message can carry the proxy's address or login
        return {"ok": False, "egress_ok": None, "ms": ms(), "error": type(e).__name__}


def record(result, now=None):
    """Keep one check's result, and count failures in a row (from when they started)."""
    now = now or now_ist()
    with _lock:
        _state["last"] = {**result, "at": now.isoformat(timespec="seconds")}
        if result.get("ok"):
            _state.update(fails=0, failing_since=None)
        else:
            _state["fails"] += 1
            _state["failing_since"] = _state["failing_since"] or _state["last"]["at"]


def latest():
    """{"last": {...} or None, "fails": n, "failing_since": iso or None}."""
    with _lock:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _state.items() if k != "running"}


def due(now=None):
    """Whether a new check should start now."""
    now = now or now_ist()
    with _lock:
        last, fails = _state["last"], _state["fails"]
    if last is None:
        return True
    age = (now - dt.datetime.fromisoformat(last["at"])).total_seconds()
    return age >= (RETRY_SEC if fails else CHECK_EVERY_SEC)


def ensure(now=None):
    """Start a background check when one is due and none is running. Returns at once."""
    if not ENABLED or not due(now):
        return False
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True

    def run():
        try:
            record(probe())
        finally:
            with _lock:
                _state["running"] = False
    threading.Thread(target=run, name="proxycheck", daemon=True).start()
    return True


def reset():
    """Forget every result (tests)."""
    with _lock:
        _state.update(last=None, fails=0, failing_since=None, running=False)


def main():
    import config  # noqa: F401 - loads the saved Kite and proxy settings, as the dashboard does
    r = probe()
    print(f"proxy {'working' if r['ok'] else 'NOT working'}; whitelisted address: "
          f"{'yes' if r['egress_ok'] else 'NO' if r['egress_ok'] is False else 'unknown'}; {r['ms']} ms"
          + (f"; error {r['error']}" if r["error"] else ""))


if __name__ == "__main__":
    main()
