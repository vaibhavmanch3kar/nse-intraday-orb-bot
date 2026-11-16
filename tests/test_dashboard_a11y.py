"""Item 8: readable colours in both themes, accessible page structure, small pages, and looking at any
page or data address never writes a file or changes a setting."""

import csv
import json
import os
import re
import threading
import urllib.error
import urllib.request

import pytest

import accounts
import config as C
import theme as T
import web


def _lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_colours_meet_wcag_aa(theme):
    p = T.PALETTES[theme]
    for fg in ("text", "muted", "up", "dn", "gold", "accent", "warn", "info"):
        for bg in ("bg", "surface", "surface-2"):
            assert contrast(p[fg], p[bg]) >= 4.5, (theme, fg, bg, round(contrast(p[fg], p[bg]), 2))
    assert contrast(p["trail"], p["surface"]) >= 3.0                      # chart line (graphics: 3:1)
    assert contrast(p["accent-ink"], p["accent"]) >= 4.5                  # text on the main buttons
    assert contrast(p["banner-fg"], p["banner-bg"]) >= 4.5


REVIEW = {"day": "2026-11-16", "days": ["2026-11-13", "2026-11-16"], "built": "2026-11-16T16:00", "signals": 1, "wins": 1,
          "total_r": 0.65, "settings": {"TARGET_R": 0},
          "rows": {"ITC": {"closes": [284.5, 290.1, 295.6], "or_low": 284.0, "or_high": 286.75, "day_change_pct": 3.78,
                           "direction": "LONG", "r": 0.65, "signal_time": "09:30", "entry": 290.85, "stop": 284.0,
                           "target": None, "exit_reason": "square-off", "exit_time": "15:00", "exit": 295.6},
                   "SBIN": {"closes": [1090, 1089], "or_low": 1083.9, "or_high": 1093.5, "day_change_pct": -0.1,
                            "note": "inside range"}}}
SCAN = {"day": "2026-11-16", "built": "2026-11-16T16:00", "counts": {"20-day high breakout": 1}, "errors": [],
        "rows": {"TCS": {"setups": ["20-day high breakout"], "closes": [3300, 3350, 3410], "close": 3410.0,
                         "change_pct": 1.2, "rsi": 64, "above_200dma": True, "from_52w_high_pct": -3.3,
                         "stop": 3330.0, "target": 3570.0, "qty": 6}}}


@pytest.fixture
def server(monkeypatch):
    import review
    import scanner
    monkeypatch.setattr(review, "latest", lambda *a, **k: REVIEW)        # no price fetching in tests
    monkeypatch.setattr(scanner, "latest", lambda *a, **k: SCAN)
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=x&v=3")

    def no_kite():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", no_kite)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        return e.code, ""


def sample_trades(n=82):
    with open(os.path.join(C.DATA_DIR, "backtest_trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["opened", "closed", "symbol", "direction", "qty", "entry", "exit",
                                          "reason", "charges", "net", "r"])
        w.writeheader()
        for i in range(n):
            w.writerow({"opened": f"2026-09-{1 + i % 28:02d} 09:45:00+05:30", "closed": f"2026-09-{1 + i % 28:02d} 11:00:00+05:30",
                        "symbol": ["ITC", "SBIN", "TCS"][i % 3], "direction": "LONG" if i % 2 else "SHORT", "qty": "10",
                        "entry": "100", "exit": "101", "reason": "time stop", "charges": "20", "net": str(i % 7 - 3),
                        "r": "0.1"})


PAGES = ["/", "/?view=zerodha", "/trades", "/trades?view=zerodha", "/review", "/scanner", "/journey?src=backtest&i=0"]


def test_page_structure_is_accessible(server):
    sample_trades(5)
    for path in PAGES:
        code, body = get(server + path)
        assert code == 200, path
        assert "<html lang='en'>" in body and "name='viewport'" in body, path
        assert body.count("<h1") >= 1 and "Skip to content" in body and "id='main'" in body, path
        assert "aria-current='page'" in body and "aria-label='Switch theme'" in body, path
        assert "prefers-reduced-motion" in body and ":focus-visible" in body, path
        for box in re.findall(r"<div class='chart[^>]*>", body):
            assert "role='img'" in box and "aria-label=" in box and "tabindex='0'" in box, path


def test_pages_stay_small(server):
    sample_trades(82)
    budgets = {"/": 120_000, "/trades": 350_000, "/review": 120_000, "/scanner": 120_000, "/api/live": 60_000}
    for path, limit in budgets.items():
        _, body = get(server + path)
        assert len(body.encode("utf-8")) < limit, (path, len(body))


def snapshot():
    out = {}
    for root, _, files in os.walk(C.DATA_DIR):
        for name in files:
            p = os.path.join(root, name)
            out[p] = open(p, "rb").read()
    return out


def test_looking_never_changes_anything(server):
    sample_trades(5)
    with open(os.path.join(C.DATA_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"mode": "paper", "time": "2026-11-16T10:00:00+05:30"}, f)
    before = snapshot()
    flags = (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live(), C.PAPER_START_INR, C.MAX_DAILY_LOSS_PCT, C.RISK_PER_TRADE_PCT)
    for path in PAGES + ["/api/live", "/api/live?view=zerodha", "/api/account?view=paper", "/api/status",
                         "/journal.csv?view=paper&period=week", "/trades?view=paper&period=month"]:
        get(server + path)
    assert snapshot() == before
    assert (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live(), C.PAPER_START_INR, C.MAX_DAILY_LOSS_PCT,
            C.RISK_PER_TRADE_PCT) == flags


def test_no_new_ways_to_change_things(server):
    """The data address is read-only: posting to it does nothing (the only actions are the old ones)."""
    before = snapshot()
    req = urllib.request.Request(server + "/api/live", data=b"view=zerodha", method="POST",
                                 headers={"Origin": server})
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError:
        pass
    assert snapshot() == before
