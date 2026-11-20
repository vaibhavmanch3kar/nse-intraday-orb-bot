"""Phase 4 on the dashboard: the Yantrashala results page (read-only), and a Cancel button for Review and Scanner
builds that stops the build and saves nothing of it."""

import datetime as dt
import os
import threading
import time

import pytest

import jobs
import safefile
import web
import yantra

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def m(trades=40, avg=-0.2, lo=-0.3, hi=-0.1, pf=0.4, net=-3000.0, up=1, months=5):
    return {"trades": trades, "win_rate": 0.25, "avg_r": avg, "r_low": lo, "r_high": hi, "profit_factor": pf,
            "net": net, "months": months, "months_positive": up}


def save(exp):
    safefile.write_json(os.path.join(yantra.lab_dir(), "experiments", exp["id"] + ".json"), exp)


def experiment(name, verdict, why, at="2026-11-18T14:00:00+05:30", n=2, wf=None):
    return {"id": f"{at[:10].replace('-', '')}-{at[11:19].replace(':', '')}-{name}", "at": at, "strategy": name,
            "what": "an idea", "period": ["2025-11-24", "2026-06-25"], "variants_tried": n, "code": "abc123def456",
            "in_sample": {f"{name}(x=1)": m(), f"{name}(x=2)": m(trades=0)}, "walk_forward": wf or m(),
            "verdict": verdict, "why": why}


# ---------------------------------------------------------------- the lab page
def test_empty_lab_page_says_how_to_run_it():
    page = web.render_yantrashala()
    assert "No experiment has been run yet" in page and "yantra.py test all" in page
    assert "Nothing is registered" in page and "How an idea is judged" in page


def test_lab_page_shows_each_idea_its_verdict_and_the_counts():
    save(experiment("orb_retest", "FAIL", ["profit factor 0.27 (needs above 1.1)"]))
    save(experiment("orb_retest", "FAIL", ["an older run"], at="2026-11-17T10:00:00+05:30"))
    save(experiment("lucky", "PASS", [], n=4, wf=m(avg=0.3, lo=0.05, hi=0.5, pf=1.6, net=4000.0, up=4)))
    page = web.render_yantrashala()
    assert page.index("lucky") < page.index("orb_retest")                  # a pass is listed first
    assert "PASS: forward-test candidate" in page and ">FAIL<" in page
    assert "profit factor 0.27 (needs above 1.1)" in page and "an older run" not in page    # newest run per idea
    assert "Walk-forward (the part that is judged)" in page and web.pnl_r(-0.2) in page and "(-0.30 to -0.10)" in page
    assert "With 6 variants tried, the best one looking good is expected by luck alone." in page
    assert "no trades" in page and "<form" not in page.split("<main", 1)[1]   # read-only: no buttons


def test_lab_page_lists_the_forward_register(monkeypatch):
    safefile.write_json(yantra.register_path(), [{"label": "orb_regime(index_min=0.4)", "strategy": "orb_regime",
                                                  "params": {"index_min": 0.4}, "code": "abc", "registered": "2026-11-18"}])
    assert "not measured yet: run yantra.py forward" in web.render_yantrashala()
    safefile.write_json(os.path.join(yantra.lab_dir(), "forward.json"), {"rows": [
        {"label": "orb_regime(index_min=0.4)", "status": "measured", "result": {"trades": 12}, "verdict": "FAIL"}]})
    page = web.render_yantrashala()
    assert "measured · 12 trades · FAIL" in page and "2026-11-18" in page


def test_live_page_links_to_the_lab_and_the_route_exists():
    assert "/yantrashala" in web.live_limits({})
    src = open(web.__file__, encoding="utf-8").read()
    assert 'u.path == "/yantrashala"' in src


# ---------------------------------------------------------------- cancelling a build
@pytest.fixture
def slow(monkeypatch):
    """A job that works in small steps until told to stop, then 'saves'."""
    monkeypatch.setattr(jobs, "ENABLED", True)
    saved, started = [], threading.Event()

    def work(target, job):
        job.plan(100, "working")
        started.set()
        for _ in range(400):
            if job.over_time():
                break
            job.advance()
            time.sleep(0.01)
        job.stop_if_cancelled()
        saved.append(target)

    return jobs.Job("slowtest", work, limit_sec=60), saved, started


def test_cancel_stops_the_build_and_nothing_is_saved(slow):
    job, saved, started = slow
    assert job.cancel() is False                             # nothing running: nothing to cancel
    job.start("a")
    job.start("b")                                           # queued behind it
    started.wait(5)
    assert job.cancel() is True
    job.wait(5)
    snap = job.snapshot()
    assert snap["state"] == "cancelled" and snap["error"] == "" and saved == []
    assert snap["queued"] is None and job.failed_recently(60)      # the queued build is dropped; no auto-restart
    job.start("c")                                           # asking again works, and the old cancel is forgotten
    started.wait(5)
    assert job.cancelled() is False
    job.cancel()
    job.wait(5)


def test_a_build_that_is_not_cancelled_still_saves(monkeypatch):
    monkeypatch.setattr(jobs, "ENABLED", True)
    saved = []

    def work(target, job):
        job.check()
        job.stop_if_cancelled()
        saved.append(target)

    job = jobs.Job("quick", work, limit_sec=60)
    job.start("x")
    job.wait(5)
    assert saved == ["x"] and job.snapshot()["state"] == "completed"
    assert jobs.NO_JOB.cancelled() is False and jobs.NO_JOB.stop_if_cancelled() is None


def test_strip_has_a_cancel_button_while_running_and_says_cancelled_after(monkeypatch):
    class Running:
        def snapshot(self):
            return {"state": "running", "done": 3, "total": 50, "elapsed_sec": 4.0, "step": "daily candles: ITC",
                    "finished": None, "error": ""}

    monkeypatch.setattr(web.jobs, "get", lambda name: Running())
    strip = web.job_strip("scanner", {"built": "2026-11-18T10:00:14+05:30"}, {"stale": False}, "/scanner?refresh=1", "stocks")
    assert "action='/jobs/cancel'" in strip and "name='job' value='scanner'" in strip and "✕ Cancel" in strip
    assert "name='csrf'" in strip or "csrf" in strip

    class Stopped:
        def snapshot(self):
            return {"state": "cancelled", "done": 3, "total": 50, "elapsed_sec": 4.0, "step": "", "finished": "x", "error": ""}

    monkeypatch.setattr(web.jobs, "get", lambda name: Stopped())
    strip = web.job_strip("scanner", {"built": "2026-11-18T10:00:14+05:30"}, {"stale": False}, "/scanner?refresh=1", "stocks")
    assert "Cancelled" in strip and "nothing of it was saved" in strip and "Refresh" in strip


def test_review_and_scanner_check_for_cancel_before_saving():
    root = os.path.dirname(web.__file__)
    for name in ("review.py", "scanner.py"):
        src = open(os.path.join(root, name), encoding="utf-8").read()
        assert src.index("job.stop_if_cancelled()") < src.rindex("write_json("), name
