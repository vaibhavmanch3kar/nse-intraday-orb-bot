"""Readiness: three verdicts from evidence, by rules fixed in advance. The platform passes only when every
requirement's tests pass, the trading loop agrees with the backtest, nothing is broken now and no limit is loose;
the strategy passes only with a lab pass AND a pass on unseen data; a good-looking backtest alone never counts."""

import json
import os

import pytest

import readiness as R
import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def all_pass():
    return {f: {"passed": 3, "failed": 0, "skipped": 0, "failures": []} for _, _, _, files in R.REQUIREMENTS for f in files}


def replay(agreement=100.0, errors=()):
    return {"days": ["2025-12-03", "2026-11-17", 237], "rounds": 5925, "seconds": 300, "errors": list(errors),
            "compare": {"loop_trades": 330, "backtest_trades": 333, "same_trade": 328, "only_in_loop_n": 2,
                        "only_in_backtest_n": 5, "same_quantity": 300, "same_exit_reason": 320, "loop_net": -16000.0,
                        "backtest_net": -16458.0, "agreement_pct": agreement, "only_in_loop": [], "only_in_backtest": []}}


def strategy(r_low=-0.26, r_high=-0.16, lab_passed=(), forward=()):
    return {"year": {"result": {"trades": 333, "return_pct": -16.46}, "range": {"avg_r": -0.21, "r_low": r_low, "r_high": r_high}},
            "lab": [{"strategy": "orb", "verdict": "FAIL", "trades": 150, "avg_r": -0.21, "variants": 1}],
            "lab_passed": list(lab_passed), "registered": 0, "forward_passed": list(forward),
            "logs": {"live": {"trades": 1, "net": -3.96, "verdict": "too_early", "needed": 30},
                     "paper": {"trades": 3, "net": 118.0, "verdict": "too_early", "needed": 30}}}


def state(bad=(), loose=(), plain=("KITE_API_KEY",), unpushed=40):
    return {"health": {"overall": "bad" if bad else "ok", "bad": list(bad), "warn": []}, "loose_limits": list(loose),
            "secrets": [[n, "Windows settings (plain text)"] for n in plain] + [["TRUEIP_PASS", "protected store"]],
            "login_store": "protected", "git": {"version": "v1.9.0-45", "unpushed": unpushed, "uncommitted": 0}}


# ---------------------------------------------------------------- the checklist
def test_every_requirement_names_test_files_that_exist():
    ids = [r[0] for r in R.REQUIREMENTS]
    assert len(ids) == len(set(ids)) >= 24
    for rid, area, text, files in R.REQUIREMENTS:
        assert files and area and len(text) > 20
        for f in files:
            assert os.path.exists(os.path.join(ROOT, "tests", f + ".py")), (rid, f)


def test_a_requirement_passes_only_when_all_its_tests_pass_and_exist():
    tests = all_pass()
    assert all(r["state"] == "pass" for r in R.requirements(tests))
    tests["test_kavach"]["failed"], tests["test_kavach"]["failures"] = 1, ["test_x"]
    by = {r["id"]: r for r in R.requirements(tests)}
    assert by["K1"]["state"] == "fail" and by["K1"]["failed"] == 1 and by["K2"]["state"] == "pass"
    del tests["test_yantra"]
    by = {r["id"]: r for r in R.requirements(tests)}
    assert by["S1"]["state"] == "fail" and by["S1"]["missing"] == ["test_yantra"]      # a test that did not run proves nothing


def test_junit_results_are_counted_per_test_file(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text("""<testsuites><testsuite>
<testcase classname="tests.test_kavach" name="a"/><testcase classname="tests.test_kavach" name="b"><failure>x</failure></testcase>
<testcase classname="tests.test_kavach" name="c"><skipped/></testcase><testcase classname="tests.test_stop" name="d"><error>y</error></testcase>
</testsuite></testsuites>""", encoding="utf-8")
    assert R.parse_junit(str(xml)) == {"test_kavach": {"passed": 1, "failed": 1, "skipped": 1, "failures": ["b"]},
                                       "test_stop": {"passed": 0, "failed": 1, "skipped": 0, "failures": ["d"]}}


# ---------------------------------------------------------------- the verdicts
def verdict(tests=None, rp="default", st=None, sy=None):
    reqs = R.requirements(tests or all_pass())
    return R.verdicts(reqs, replay() if rp == "default" else rp, sy or strategy(), st or state())


def test_platform_passes_only_with_everything():
    assert verdict()["platform"]["state"] == "pass"
    bad = all_pass()
    bad["test_execution_safety"]["failed"] = 1
    v = verdict(tests=bad)
    assert v["platform"]["state"] == "fail" and "E2" in v["platform"]["reasons"][0][1]
    assert verdict(rp=None)["platform"]["state"] == "fail"                       # no replay: not proven
    assert verdict(rp=replay(agreement=80.0))["platform"]["state"] == "fail"
    assert verdict(rp=replay(errors=[{"at": "x", "error": "boom"}]))["platform"]["state"] == "fail"
    assert verdict(st=state(bad=["recon"]))["platform"]["state"] == "fail"
    assert verdict(st=state(loose=["MAX_DAILY_LOSS_PCT"]))["platform"]["state"] == "fail"


def test_strategy_needs_a_lab_pass_and_unseen_data_never_a_nice_backtest_alone():
    assert verdict()["strategy"]["state"] == "fail"
    nice = strategy(r_low=0.05, r_high=0.40)                                      # a backtest that looks profitable
    assert verdict(sy=nice)["strategy"]["state"] == "fail"
    assert verdict(sy=strategy(r_low=0.05, r_high=0.40, lab_passed=["orb_x"]))["strategy"]["state"] == "fail"
    assert verdict(sy=strategy(r_low=0.05, r_high=0.40, lab_passed=["orb_x"], forward=["orb_x(a=1)"]))["strategy"]["state"] == "pass"
    v = verdict()
    live = [text for s, text in v["strategy"]["reasons"] if s == "open"][0]
    assert "1 closed trade(s)" in live and "needs 30 trades and even then is not proof" in live
    assert v["strategy"]["state"] == "fail" and v["platform"]["state"] == "pass"   # the two never mask each other


def test_operations_lists_what_the_owner_still_has_to_decide(monkeypatch):
    text = " | ".join(t for _, t in verdict()["operations"]["reasons"])
    assert "1 secret(s) are kept as plain text" in text and "KITE_API_KEY" in text and "40 commits exist only on this PC" in text
    assert "phone" in text
    assert "7 Dec 2026" not in text                            # checked on 20 Nov 2026: no longer an open item
    # a rule that has not been checked yet is still listed as open
    monkeypatch.setattr(R, "COMPLIANCE", list(R.COMPLIANCE) + [("A new rule", "not yet checked", None)])
    assert "A new rule" in " | ".join(t for _, t in verdict()["operations"]["reasons"])
    assert "cool-down count" not in text                       # settled findings are not open items...
    assert all(len(f) == 4 for f in R.FINDINGS) and R.FINDINGS[0][3].startswith("Settled 2026-11-18")
    clean = " | ".join(t for _, t in verdict(st=state(plain=(), unpushed=0))["operations"]["reasons"])
    assert "plain text" not in clean and "commits" not in clean


# ---------------------------------------------------------------- the files and the page
def test_build_writes_the_result_and_the_report_and_changes_nothing_else(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(R, "report_path", lambda: str(tmp_path / "docs" / "PRODUCTION-READINESS.md"))
    monkeypatch.setattr(R, "state_now", lambda now=None: state())
    monkeypatch.setattr(R, "strategy_evidence", lambda: strategy())
    res = R.build(tests=all_pass(), replay=replay())
    assert sorted(os.listdir(str(isolated))) == ["docs", "readiness_latest.json"]      # the result and the report only
    assert R.latest()["verdicts"]["platform"]["state"] == "pass" and res["tests"]["failed"] == 0
    md = open(tmp_path / "docs" / "PRODUCTION-READINESS.md", encoding="utf-8").read()
    for text in ("# Ayodhya ORB 2.0: production-readiness report", "It is not advice on whether to trade.",
                 "| PLATFORM: is the software safe and correct? | **PASS** |", "| STRATEGY: do the trading rules have a proven edge? | **FAIL** |",
                 "There is NO evidence that the present trading rules make money.", "## Paper replay of the trading loop",
                 "| K1 | Capital protection (Kavach) |", "Closing session change on 7 Dec 2026", "regular trading runs to 3:30 PM",
                 "| 2026-11-20 |", "## How to repeat this",
                 "## What the testing itself found", "counted losses first"):     # ...but stay in the report
        assert text in md, text
    # the report file can be shared: what describes this one PC stays on the dashboard
    assert "plain text" not in md and "KITE_API_KEY" not in md and "only on this PC" not in md and "s3cr3t" not in md
    assert "2 more item(s) about this PC's own set-up are listed on the dashboard's Readiness page" in md
    assert len(res["verdicts"]["operations"]["local"]) == 2
    assert any("KITE_API_KEY" in t for _, t in R.latest()["verdicts"]["operations"]["reasons"])     # still on the page


def test_page_shows_the_three_verdicts_and_is_read_only(isolated, monkeypatch, tmp_path):
    first = web.render_readiness()
    assert "No result is saved yet" in first and "readiness.py run" in first
    monkeypatch.setattr(R, "report_path", lambda: str(tmp_path / "r.md"))
    monkeypatch.setattr(R, "state_now", lambda now=None: state())
    monkeypatch.setattr(R, "strategy_evidence", lambda: strategy())
    R.build(tests=all_pass(), replay=replay())
    page = web.render_readiness()
    for text in ("Platform", "Strategy", "Operations", "✓ PASS", "✕ FAIL", "• OPEN", "not advice on whether to trade",
                 "There is NO evidence that the present trading rules make money.", "Requirements and the tests that prove them",
                 "Paper replay of the trading loop", "the same trade in both", "Rules for trading through the Kite API"):
        assert text in page, text
    body = page.split("<main", 1)[1]
    assert "<form" not in body and "<button" not in body
    src = open(web.__file__, encoding="utf-8").read()
    assert 'u.path == "/readiness"' in src and "/readiness" in web.live_limits({})


def test_readiness_reads_and_measures_only():
    code = open(R.__file__, encoding="utf-8").read()
    for word in ("import broker", "import bot", "kiteconnect", "place_order", "os.environ[", "winreg", "CONFIRM_LIVE=\"YES\""):
        assert word not in code, word
    assert "env.pop(\"CONFIRM_LIVE\", None)" in code            # the test run it starts can never be live
