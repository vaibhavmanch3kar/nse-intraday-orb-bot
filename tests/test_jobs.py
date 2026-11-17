"""Background builds for the Review and Scanner pages: pages answer at once, one build at a time,
time limits, failures shown, and a browser leaving early is harmless."""

import http.client
import json
import threading
import time
from urllib.parse import urlparse

import pytest

import config as C
import data_yahoo
import jobs
import review
import scanner
import web
from conftest import bar, history
from test_review import day_with_breakout


@pytest.fixture
def jobs_on(monkeypatch):
    monkeypatch.setattr(jobs, "ENABLED", True)
    made = []

    def fresh(name, fn, limit):   # a new Job per test, so state never leaks between tests
        j = jobs.Job(name, fn, limit)
        made.append(j)
        monkeypatch.setitem(jobs._JOBS, name, j)
        return j
    yield fresh
    for j in made:
        j.wait(10)


def test_single_flight_join_queue_and_last_success(jobs_on):
    gate, ran = threading.Event(), []

    def work(target, job):
        ran.append(target)
        job.plan(2)
        job.advance("one")
        gate.wait(5)
        job.advance("two")

    j = jobs_on("t", work, 60)
    assert j.start("A") == "started"
    time.sleep(0.05)
    assert j.start("A") == "joined"            # same build already running: no second copy
    assert j.start("B") == "queued"
    assert j.start("C") == "queued"            # the latest request wins
    s = j.snapshot()
    assert s["state"] == "running" and s["done"] == 1 and s["total"] == 2 and s["queued"] == "C"
    gate.set()
    for _ in range(100):
        if ran == ["A", "C"] and j.snapshot()["state"] == "completed":
            break
        time.sleep(0.05)
    assert ran == ["A", "C"]
    s = j.snapshot()
    assert s["state"] == "completed" and s["last_ok"] and s["error"] == "" and s["queued"] is None


def test_failure_is_recorded_and_disabled_jobs_do_nothing(jobs_on, monkeypatch):
    def boom(target, job):
        raise RuntimeError("Kite login expired")

    j = jobs_on("t", boom, 60)
    j.start()
    j.wait(5)
    s = j.snapshot()
    assert s["state"] == "failed" and "Kite login expired" in s["error"] and s["last_ok"] is None
    monkeypatch.setattr(jobs, "ENABLED", False)
    assert j.start() == "off"


def test_time_limit_skips_the_rest_and_marks_the_scan_partial(jobs_on, monkeypatch):
    calls = []

    def slow_fetch(sym, interval_min=15, range_="10d", interval=None, **k):
        calls.append(sym)
        time.sleep(0.3)
        raise data_yahoo.DataError(f"{sym}: no data")

    monkeypatch.setattr(data_yahoo, "fetch", slow_fetch)
    j = jobs_on("scan-limit", lambda t, job: None, 0.2)   # 0.2 s limit
    j._t0 = time.monotonic()
    syms = [f"S{i}" for i in range(12)]
    res = scanner.scan(syms, job=j)
    skipped = [s for s, e in res["errors"].items() if data_yahoo.SKIPPED in e]
    assert skipped and len(calls) < len(syms)              # some never started
    assert res["partial"]["skipped"] == sorted(skipped) and j.skipped == len(skipped)


def test_review_page_answers_at_once_and_builds_in_background(jobs_on, monkeypatch):
    hist, day = history()
    bars = hist + day_with_breakout(day, [bar(day, "10:15", 101.4, 104.0, 101.3, 103.8, 2000)])
    gate = threading.Event()

    def blocked_fetch(s, i, r, **kw):
        gate.wait(10)
        return bars, bars[-1]["c"], None

    monkeypatch.setattr(C, "WATCHLIST", ["SBIN", "INFY"])
    monkeypatch.setattr(data_yahoo, "fetch", blocked_fetch)
    jobs_on("review", web._review_job, 60)
    t = time.perf_counter()
    page = web.render_review()
    assert time.perf_counter() - t < 1.0                    # did not wait for the downloads
    assert "<b>Loading</b>" in page and "data-running='1'" in page and "/api/jobs" in page
    assert jobs.get("review").snapshot()["state"] == "running"
    web.render_review()                                     # a second visit joins, no second build
    assert jobs.get("review").start(None) == "joined"
    gate.set()
    jobs.get("review").wait(10)
    assert jobs.get("review").snapshot()["state"] == "completed"
    page = web.render_review()
    assert "SBIN" in page and "<b>Completed</b>" in page and "data-running" not in page


def test_scanner_page_shows_saved_scan_while_rescanning(jobs_on, monkeypatch):
    old = {"day": "2026-11-13", "built": "2026-11-14T21:00:00+05:30", "errors": {}, "rows": {},
           "counts": {k: 0 for k in scanner.SETUPS}}
    review.write_json(scanner._path(), old)
    gate = threading.Event()
    monkeypatch.setattr(data_yahoo, "fetch_many", lambda *a, **k: (gate.wait(10), {})[1])
    jobs_on("scanner", web._scan_job, 60)
    page = web.render_scanner()                             # the saved scan is old: rescan starts
    assert "2026-11-13" in page and "<b>Running</b>" in page and "showing the saved result from" in page
    gate.set()
    jobs.get("scanner").wait(10)


def test_refresh_redirects_and_api_jobs_is_read_only(jobs_on, monkeypatch):
    jobs_on("review", lambda t, job: None, 60)
    jobs_on("scanner", lambda t, job: None, 60)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        host, port = srv.server_address
        for path, to in (("/review?day=2026-11-13&refresh=1", "/review?day=2026-11-13"),
                         ("/scanner?refresh=1", "/scanner")):
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", path)
            r = c.getresponse()
            assert r.status == 303 and urlparse(r.getheader("Location")).path + (
                "?" + urlparse(r.getheader("Location")).query if urlparse(r.getheader("Location")).query else "") == to
        c = http.client.HTTPConnection(host, port, timeout=5)
        c.request("GET", "/api/jobs")
        r = c.getresponse()
        body = json.loads(r.read())
        assert r.status == 200 and set(body) >= {"review", "scanner"}
        assert {"state", "done", "total", "elapsed_sec", "last_ok", "error"} <= set(body["review"])
    finally:
        srv.shutdown()


def test_browser_leaving_early_is_not_an_error():
    class Gone:
        def write(self, data):
            raise ConnectionAbortedError(10053, "aborted")

    h = web.Handler.__new__(web.Handler)
    h.wfile = Gone()
    h.send_response = h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h._send("<p>page</p>")          # must not raise (it used to print a traceback in the bot window)


# ---------------------------------------------------------------- the status strip
def saved_review(**extra):
    import datetime as dt
    now = dt.datetime.now(jobs.IST)
    return {"day": "2026-11-13", "days": ["2026-11-13"], "built": now.isoformat(timespec="seconds"),
            "rows": {}, "signals": 0, "wins": 0, "total_r": 0.0,
            "settings": {k: getattr(C, k) for k in review.SETTING_KEYS}, **extra}


def test_strip_states_completed_stale_failed_and_partial(jobs_on, monkeypatch):
    ok = saved_review()
    strip = web.job_strip("review", ok, review.status(ok), "/review?refresh=1", "steps")
    assert "<b>Completed</b>" in strip and "updated" in strip and "just now" in strip and "✓" in strip
    assert "role='status'" in strip and "Retry" not in strip

    old = saved_review(built="2026-11-13T09:40:00+05:30")
    strip = web.job_strip("review", old, {"stale": True, "why": "the 09:45 candle has closed since"},
                          "/review?refresh=1", "steps")
    assert "<b>Data stale</b>" in strip and "09:45 candle" in strip and "↻ Refresh" in strip

    part = saved_review(partial={"skipped": ["SBIN", "TCS"], "why": "time limit"})
    strip = web.job_strip("review", part, review.status(part), "/review?refresh=1", "steps")
    assert "<b>Partial</b>" in strip and "2 stock(s) skipped" in strip and "SBIN, TCS" in strip

    j = jobs_on("review", lambda t, job: (_ for _ in ()).throw(RuntimeError("Kite login expired")), 60)
    j.start()
    j.wait(5)
    strip = web.job_strip("review", ok, review.status(ok), "/review?refresh=1", "steps")
    assert "<b>Failed</b>" in strip and "Kite login expired" in strip and "↻ Retry" in strip
    assert "href='/review?refresh=1'" in strip


def test_no_automatic_retry_loop_after_a_failure(jobs_on):
    runs = []

    def fails(target, job):
        runs.append(1)
        raise RuntimeError("network down")

    j = jobs_on("review", fails, 60)
    j.start()
    j.wait(5)
    assert web.auto_start("review") == "held"          # visiting the page again doesn't retry by itself
    assert jobs.start("review") == "started"           # ...but Retry does
    j.wait(5)
    assert len(runs) == 2


def test_time_machine_and_scanner_data_errors_are_shown(monkeypatch):
    r = saved_review(rows={"SBIN": {"direction": "LONG", "signal_time": "09:45", "entry": 100.0, "stop": 99.0,
                                    "target": 102.0, "exit": 101.0, "exit_time": "10:30", "exit_reason": "target",
                                    "r": 0.9, "closes": [100, 101], "day_change_pct": 1.0,
                                    "time_machine": {"n": 0, "error": "SBIN: timed out"}}})
    review.write_json(review._cache_path("2026-11-13"), r)
    page = web.render_review("2026-11-13")
    assert "no 5-min data (SBIN: timed out)" in page
    scan = {"day": "2026-11-13", "built": saved_review()["built"], "rows": {},
            "errors": {"TCS": "TCS: rate limited (429)", "INFY": "INFY: " + data_yahoo.SKIPPED},
            "counts": {k: 0 for k in scanner.SETUPS},
            "settings": {k: getattr(C, k) for k in scanner.SETTING_KEYS}}
    monkeypatch.setattr(scanner, "latest", lambda: scan)
    page = web.render_scanner()
    assert "Data errors for 1 of 1 stocks" in page and "TCS (rate limited (429))" in page


def test_strip_works_in_every_theme_and_has_no_animation():
    import theme as T
    ok = saved_review()
    for tid in T._ui.THEMES:
        tok = T._ui.activate(tid)
        try:
            page = web.page("Session review", "/review",
                            web.job_strip("review", ok, review.status(ok), "/review?refresh=1", "steps"))
        finally:
            T._ui.reset(tok)
        assert ".jobstrip{" in page and "<b>Completed</b>" in page, tid
    css = web.webui.css() if hasattr(web.webui, "css") else ""
    block = css[css.find(".jobstrip{"):css.find(".banner{")]
    assert "animation" not in block and "transition" not in block
