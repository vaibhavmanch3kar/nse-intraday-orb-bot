"""The "Static-IP proxy" line of System health: the dashboard checks the proxy in the background while the bot
trades real money, keeps only ok / not ok, the time taken and the kind of error, and never blocks a page."""

import datetime as dt
import json
import os
import time

import buildinfo
import health
import proxycheck

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 11, 20, 10, 0, tzinfo=IST)            # a Friday, market open
SECRET_PASS = "pa55-word-for-test"
WHITELISTED = "203.0.113.7"                                    # documentation address, not a real one


class Answer:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status


def fake_net(monkeypatch, egress=WHITELISTED, kite_status=200, fail=None):
    """Stand-ins for the proxy settings and the two requests; returns the list of URLs asked for."""
    import kite_proxy
    import requests
    asked = []
    monkeypatch.setattr(kite_proxy, "proxies", lambda: {"https": f"http://user:{SECRET_PASS}@proxy.invalid:8080"})
    monkeypatch.setattr(kite_proxy, "env", lambda name, default=None: WHITELISTED)

    def get(url, proxies=None, timeout=None):
        asked.append((url, bool(proxies), timeout))
        if fail:
            raise fail
        return Answer(egress) if url == proxycheck.ECHO_URL else Answer("", kite_status)
    monkeypatch.setattr(requests, "get", get)
    return asked


# ---------------------------------------------------------------- one check
def test_a_working_proxy(monkeypatch):
    asked = fake_net(monkeypatch)
    r = proxycheck.probe()
    assert r["ok"] is True and r["egress_ok"] is True and r["error"] is None and r["ms"] >= 0
    # both requests go through the proxy, with a time limit: the echo service, then Kite's public address
    assert [u for u, _, _ in asked] == [proxycheck.ECHO_URL, proxycheck.KITE_URL]
    assert all(p and t == proxycheck.TIMEOUT_SEC for _, p, t in asked)


def test_traffic_from_another_address_is_caught(monkeypatch):
    fake_net(monkeypatch, egress="198.51.100.9")
    r = proxycheck.probe()
    assert r["ok"] is True and r["egress_ok"] is False


def test_a_failure_keeps_the_kind_of_error_only(monkeypatch):
    import requests
    fake_net(monkeypatch, fail=requests.exceptions.ProxyError(
        f"Unable to connect to proxy http://user:{SECRET_PASS}@proxy.invalid:8080 (10054)"))
    r = proxycheck.probe()
    assert r == {"ok": False, "egress_ok": None, "ms": r["ms"], "error": "ProxyError"}
    assert SECRET_PASS not in json.dumps(r) and "proxy.invalid" not in json.dumps(r)


def test_kite_down_behind_a_working_proxy(monkeypatch):
    fake_net(monkeypatch, kite_status=503)
    r = proxycheck.probe()
    assert r["ok"] is False and r["egress_ok"] is True and r["error"] == "Kite answered HTTP 503"


def test_missing_settings_are_a_failure_not_a_crash(monkeypatch):
    import kite_proxy

    def missing():
        raise SystemExit("[x] Missing environment variable TRUEIP_HOST")
    monkeypatch.setattr(kite_proxy, "proxies", missing)
    assert proxycheck.probe()["error"] == "SystemExit"


# ---------------------------------------------------------------- when to check, and what is kept
def at(sec):
    return NOW + dt.timedelta(seconds=sec)


def test_failures_in_a_row_are_counted_and_cleared():
    assert proxycheck.due(NOW)                                         # nothing checked yet
    proxycheck.record({"ok": True, "egress_ok": True, "ms": 800, "error": None}, now=NOW)
    assert not proxycheck.due(at(proxycheck.CHECK_EVERY_SEC - 1)) and proxycheck.due(at(proxycheck.CHECK_EVERY_SEC))
    proxycheck.record({"ok": False, "egress_ok": None, "ms": 10, "error": "ProxyError"}, now=at(300))
    proxycheck.record({"ok": False, "egress_ok": None, "ms": 10, "error": "ProxyError"}, now=at(360))
    got = proxycheck.latest()
    assert got["fails"] == 2 and got["failing_since"] == at(300).isoformat(timespec="seconds")
    assert not proxycheck.due(at(360 + proxycheck.RETRY_SEC - 1)) and proxycheck.due(at(360 + proxycheck.RETRY_SEC))
    proxycheck.record({"ok": True, "egress_ok": True, "ms": 800, "error": None}, now=at(420))
    assert proxycheck.latest()["fails"] == 0 and proxycheck.latest()["failing_since"] is None


def test_a_check_runs_in_the_background_and_never_twice_at_once(monkeypatch):
    monkeypatch.setattr(proxycheck, "ENABLED", True)
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return {"ok": True, "egress_ok": True, "ms": 300, "error": None}
    monkeypatch.setattr(proxycheck, "probe", slow)
    t0 = time.monotonic()
    assert proxycheck.ensure() is True
    assert time.monotonic() - t0 < 0.2                                 # the page did not wait for it
    assert proxycheck.ensure() is False                                # one is already running
    for _ in range(50):
        if proxycheck.latest()["last"]:
            break
        time.sleep(0.05)
    assert proxycheck.latest()["last"]["ok"] is True and len(calls) == 1
    assert proxycheck.ensure() is False                                # not due again for five minutes


def test_the_test_suite_never_checks_for_real():
    assert proxycheck.ENABLED is False and proxycheck.ensure() is False and proxycheck.latest()["last"] is None


# ---------------------------------------------------------------- the line on the health card
def status(mode="live", **over):
    s = {"time": (NOW - dt.timedelta(seconds=20)).isoformat(), "mode": mode, "equity": 10000.0, "day_pnl": 0.0,
         "feed": {"source": "kite", "newest_candle": "09:45", "stocks_ok": 12, "stocks": 12, "problems": {},
                  "errors": []},
         "recon": {"at": (NOW - dt.timedelta(seconds=60)).isoformat(), "ok": True, "problems": []}}
    s.update(over)
    return s


def line(proxy, mode="live"):
    rows = health.checks(NOW, status(mode), login=True, disk_free_gb=40.0, store="protected", proxy=proxy)
    r = next(r for r in rows if r["key"] == "proxy")
    return r["state"], r["detail"]


def checked(ago, ok=True, egress_ok=True, error=None, fails=0, since=None):
    return {"last": {"ok": ok, "egress_ok": egress_ok, "ms": 912, "error": error,
                     "at": (NOW - dt.timedelta(seconds=ago)).isoformat(timespec="seconds")},
            "fails": fails, "failing_since": since and (NOW - dt.timedelta(seconds=since)).isoformat()}


def test_health_line_states():
    assert line(checked(70)) == ("ok", "working, leaves from the whitelisted address; checked 1 minute ago (912 ms)")
    state, detail = line(checked(30, ok=False, egress_ok=None, error="ProxyError", fails=1, since=30))
    assert state == "warn" and "the last check failed (ProxyError)" in detail
    state, detail = line(checked(30, ok=False, egress_ok=None, error="ProxyError", fails=3, since=200))
    assert state == "bad" and detail.startswith("NOT reachable for 3 minutes (ProxyError)")
    state, detail = line(checked(30, egress_ok=False))
    assert state == "bad" and "does NOT leave from the whitelisted address" in detail
    state, detail = line(checked(3 * proxycheck.CHECK_EVERY_SEC + 60))
    assert state == "warn" and detail.startswith("not checked for")
    assert line({"last": None, "fails": 0, "failing_since": None}) == ("off", "being checked")
    state, detail = line(checked(70), mode="paper")
    assert state == "off" and "only checked while trading real money" in detail


def test_the_card_starts_a_check_only_while_trading_real_money(monkeypatch):
    started = []
    monkeypatch.setattr(proxycheck, "ensure", lambda now=None: started.append(now) or False)
    health.checks(NOW, status("paper"), login=True, disk_free_gb=40.0)
    assert started == []
    health.checks(NOW, status("live"), login=True, disk_free_gb=40.0)
    assert len(started) == 1


def test_a_broken_proxy_check_never_breaks_the_card(monkeypatch):
    def boom(now=None):
        raise RuntimeError("x")
    monkeypatch.setattr(proxycheck, "ensure", boom)
    rows = health.checks(NOW, status("live"), login=True, disk_free_gb=40.0)
    assert next(r for r in rows if r["key"] == "proxy")["state"] == "off"


def test_proxycheck_is_not_part_of_the_bot_and_orders_nothing():
    assert "proxycheck.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}
    code = open(proxycheck.__file__, encoding="utf-8").read()
    for word in ("import broker", "place_order", "cancel_order", "modify_order", "print(r", "secretbox.get"):
        assert word not in code, word
