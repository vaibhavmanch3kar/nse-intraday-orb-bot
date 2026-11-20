"""
Production readiness, from evidence. This file holds the checklist of what the 2.0 brief asked for, runs the
tests that prove each item, adds the paper replay, the present state of the running system and the strategy's
own record, and writes the result:

    data/readiness_latest.json            what the dashboard's Readiness page shows
    docs/PRODUCTION-READINESS.md          the same as a report to read

    .venv\\Scripts\\python.exe readiness.py run        # run the tests (about 4 minutes) and write both files
    .venv\\Scripts\\python.exe readiness.py show       # print the last saved result

Three separate verdicts, by rules fixed here in advance, because they are different questions:
  PLATFORM    Does the software do what it must, safely? (every requirement's tests pass, the trading loop agrees
              with the backtest, nothing is NOT working now, no risk limit is looser than built)
  STRATEGY    Is there evidence that the trading rules make money? (a rule set that passed the lab's walk-forward
              rule AND then held up on data it had never seen; a profitable-looking backtest alone never counts)
  OPERATIONS  What is still open for the owner to decide or do.

It reads and measures. It changes no setting, places nothing and never talks to the broker. It is a statement of
what the evidence shows, not advice on whether to trade.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import config as C
import safefile

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.abspath(__file__))
MIN_AGREEMENT_PCT = 95.0       # the trading loop and the backtest must take the same trades at least this often
PASS, FAIL, OPEN = "pass", "fail", "open"

# What the brief asked for, and the test files that prove it. A requirement passes when every test in its files
# passes and there is at least one.
REQUIREMENTS = [
    ("K1", "Capital protection (Kavach)", "One risk engine decides every new position, in paper, real money and backtests alike",
     ["test_kavach", "test_backtest_engine"]),
    ("K2", "Capital protection (Kavach)", "Risk per trade, open positions, trades per day, daily loss limit and the cool-down after losses are enforced",
     ["test_kavach_limits", "test_risk_costs", "test_cooldown_order"]),
    ("K3", "Capital protection (Kavach)", "No real-money entry on delayed, stale, missing or invalid prices",
     ["test_kavach_data", "test_data_outage"]),
    ("K4", "Capital protection (Kavach)", "Positions are sized from the real balance and skipped when not even one share is affordable",
     ["test_live_balance", "test_phase1_clarity"]),
    ("E1", "Execution", "Paper and real money are separate; real money needs both Windows settings and the day's arming",
     ["test_live_arming", "test_live_safety", "test_mode_switch"]),
    ("E2", "Execution", "Every order is recorded before it is sent, carries its own tag and is never sent twice; an order ID is not treated as a fill",
     ["test_execution_safety", "test_brokers"]),
    ("E3", "Execution", "The bot's record is compared with Zerodha at start and every few minutes; a mismatch holds new entries",
     ["test_execution_safety", "test_kavach_orders_page"]),
    ("E4", "Execution", "Every open position keeps an exchange stop-loss, checked on every round",
     ["test_execution_safety", "test_stop"]),
    ("E5", "Execution", "Only one module can send or cancel an order; the dashboard and the analysis pages cannot reach it",
     ["test_execution_isolation"]),
    ("E6", "Execution", "Positions are closed before the broker's own square-off",
     ["test_square_off", "test_paper_flow"]),
    ("D1", "Market data", "Candles are validated; delays and gaps are measured and shown",
     ["test_kavach_data", "test_watch_timing", "test_freshness"]),
    ("D2", "Market data", "Kite's request limits are respected and a data failure falls back safely",
     ["test_kite_pacing", "test_data_kite"]),
    ("D3", "Market data", "Scanner and Review build in the background with progress and Cancel",
     ["test_jobs", "test_lab_page_and_cancel"]),
    ("S1", "Strategy testing", "No strategy can see a future candle; the lab's copy of the live rules matches them candle by candle",
     ["test_yantra"]),
    ("S2", "Strategy testing", "Backtests include charges and slippage and give the same answer every time",
     ["test_backtest_engine", "test_risk_costs", "test_bakhar_backtest", "test_review_equivalence"]),
    ("S3", "Strategy testing", "Thirty trades is treated as an early checkpoint, not as proof",
     ["test_edge"]),
    ("A1", "Research and portfolio", "Chanakya shows measured evidence and Kavach's answer; no probabilities, no orders",
     ["test_chanakya"]),
    ("A2", "Research and portfolio", "Rajya reads the account only; rebalancing is a simulation",
     ["test_rajya", "test_accounts"]),
    ("U1", "Dashboard", "All eight pages; choosing an account view never changes the trading mode",
     ["test_nav_eight_pages", "test_mode_badge", "test_dashboard_live"]),
    ("U2", "Dashboard", "Start, Pause, Resume, Stop and Zerodha logout ask first and accept only the dashboard's own forms",
     ["test_start_button", "test_request_safety", "test_zerodha_logout"]),
    ("U3", "Dashboard", "Apply Changes checks the new code, restarts only when safe, verifies the result and can go back",
     ["test_restart"]),
    ("H1", "Security and self-healing", "The login is stored protected and no secret reaches a log",
     ["test_secrets", "test_eventlog"]),
    ("H2", "Security and self-healing", "Health checks and alerts",
     ["test_health", "test_proxycheck", "test_zerodha_alerts"]),
    ("H3", "Security and self-healing", "A crashed or stuck bot is detected, recorded and restarted; files are saved safely",
     ["test_watchdog", "test_safefile", "test_premarket_day"]),
    ("H4", "Security and self-healing", "Daily backups, the trade journal and the candle archive",
     ["test_backup", "test_journal", "test_archive"]),
]

# Rules that apply to trading through the Kite API. Checked against official sources on the date given; they
# change, so each line carries its date.
COMPLIANCE = [
    ("SEBI retail algo framework (in force since 1 Apr 2026)", "Below 10 orders a second needs no separate registration; the bot sends a handful a day.", "2026-11-18"),
    ("Static IP for API orders", "Orders go out through the fixed, whitelisted address configured for this PC.", "2026-11-18"),
    ("No market orders from an algo", "The bot sends LIMIT entries and exits and SL (stop-loss limit) protection only.", "2026-11-18"),
    ("Order tags", "Every bot order carries a tag of at most 20 letters and digits.", "2026-11-18"),
    ("Zerodha MIS square-off", "Zerodha closes MIS positions in F&O stocks at 3:12 PM for a fee; the bot closes at 3:00 PM.", "2026-11-18"),
    ("Closing session change on 7 Dec 2026", "From 7 Dec 2026 regular trading runs to 3:30 PM for all stocks and the closing auction moves to 3:30-3:40 PM (Zerodha update of 16 Oct 2026, after SEBI's October circular). The bot closes at 3:00 PM, so it is not affected. Zerodha has not yet published its MIS square-off time from that day: check its bulletin in the first week of December.", "2026-11-20"),
]


# Things the testing itself turned up: (date found, what, detail, how it was settled or None while it is open).
FINDINGS = [
    ("2026-11-18", "The cool-down count depended on the order in which same-candle exits were recorded",
     "When a winning and a losing position closed on the same candle, the 'three losses in a row' count depended on "
     "which was recorded first. The trading loop recorded them in the order they were opened, the backtest in "
     "another order. This was the cause of every difference in the first one-year paper replay (6 trades: twice in "
     "the year one of the two went into a cool-down and the other did not).",
     "Settled 2026-11-18 by the owner's decision: exits recorded at the same moment are counted losses first "
     "(risk.State._count_streak, tests/test_cooldown_order.py). One-year backtest before: 333 trades, -16.46%; "
     "after: 334 trades, -16.18%."),
]


def now_ist():
    return dt.datetime.now(IST)


def path():
    return os.path.join(C.DATA_DIR, "readiness_latest.json")


def report_path():
    return os.path.join(ROOT, "docs", "PRODUCTION-READINESS.md")


# ---------------------------------------------------------------- the tests
def run_tests(timeout=1500):
    """Run the whole test suite once and return {"file": {"passed", "failed", "skipped", "failures": [...]}}."""
    xml = os.path.join(tempfile.mkdtemp(prefix="readiness_"), "junit.xml")
    env = dict(os.environ, DRY_RUN="true")
    env.pop("CONFIRM_LIVE", None)
    proc = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    return parse_junit(xml), proc.returncode


def parse_junit(xml):
    out = {}
    for case in ET.parse(xml).getroot().iter("testcase"):
        name = (case.get("classname") or "").split(".")[-1]
        row = out.setdefault(name, {"passed": 0, "failed": 0, "skipped": 0, "failures": []})
        if case.find("failure") is not None or case.find("error") is not None:
            row["failed"] += 1
            row["failures"].append(case.get("name"))
        elif case.find("skipped") is not None:
            row["skipped"] += 1
        else:
            row["passed"] += 1
    return out


def requirements(tests):
    """Each requirement with its test counts and pass/fail."""
    out = []
    for rid, area, text, files in REQUIREMENTS:
        passed = sum(tests.get(f, {}).get("passed", 0) for f in files)
        failed = sum(tests.get(f, {}).get("failed", 0) for f in files)
        missing = [f for f in files if f not in tests]
        out.append({"id": rid, "area": area, "text": text, "files": files, "passed": passed, "failed": failed,
                    "missing": missing, "state": PASS if passed and not failed and not missing else FAIL})
    return out


# ---------------------------------------------------------------- other evidence (all read-only)
def _read(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def replay_evidence(source=None):
    """The last paper replay (paper_replay.py --out data/paper_replay.json), or None."""
    return _read(source or os.path.join(C.DATA_DIR, "paper_replay.json"))


def strategy_evidence():
    """What is known about the trading rules' results: the one-year backtest, every lab experiment, the forward
    test register, and the real and paper trade logs."""
    import edge
    import journal
    import yantra
    year = _read(os.path.join(C.DATA_DIR, "bakhar_latest.json"))
    newest = {}
    for e in yantra.experiments():
        newest.setdefault(e.get("strategy"), e)
    lab = [{"strategy": k, "verdict": e.get("verdict"), "trades": (e.get("walk_forward") or {}).get("trades"),
            "avg_r": (e.get("walk_forward") or {}).get("avg_r"), "variants": e.get("variants_tried")}
           for k, e in sorted(newest.items())]
    forward = _read(os.path.join(yantra.lab_dir(), "forward.json")) or {}
    logs = {}
    for mode in ("live", "paper"):
        rows = journal.trade_rows(mode)
        chk = edge.edge_check(rows)
        logs[mode] = {"trades": len(rows), "net": round(sum(journal._f(r.get("net")) for r in rows), 2),
                      "verdict": chk.get("verdict"), "needed": chk.get("min_trades")}
    return {"year": {"result": (year or {}).get("result"), "range": (year or {}).get("range"),
                     "archive": (year or {}).get("archive")} if year else None,
            "lab": lab, "lab_passed": [x["strategy"] for x in lab if x["verdict"] == "PASS"],
            "registered": len(yantra.registered()),
            "forward_passed": [r.get("label") for r in forward.get("rows") or [] if r.get("verdict") == "PASS"],
            "logs": logs}


def state_now(now=None):
    """The running system right now: health, risk limits against their built-in values, where the secrets are
    kept, the code version and whether it is saved outside this PC."""
    import health
    import kavach
    import secure_secrets
    out = {"health": None, "loose_limits": [], "secrets": [], "git": {}}
    try:
        rep = health.report(now)
        out["health"] = {"overall": rep["overall"], "bad": rep["bad"], "warn": rep["warn"]}
    except Exception as e:  # noqa: BLE001
        out["health"] = {"overall": "unknown", "bad": [], "warn": [], "error": type(e).__name__}
    try:
        out["loose_limits"] = [x["name"] for x in kavach.looser_than_default()]
        out["limits"] = {x["name"]: x["value"] for x in kavach.limits()}
    except Exception:  # noqa: BLE001
        pass
    try:
        out["secrets"] = [[n, w] for n, w in secure_secrets.status()]
    except Exception:  # noqa: BLE001
        pass
    try:
        import kite_proxy
        out["login_store"] = kite_proxy.token_storage()
    except BaseException:  # noqa: BLE001
        out["login_store"] = "none"

    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    ahead = git("rev-list", "--count", "@{upstream}..HEAD")
    out["git"] = {"version": git("describe", "--tags", "--always"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                  "unpushed": int(ahead) if ahead.isdigit() else None,
                  "uncommitted": len([x for x in git("status", "--porcelain").splitlines() if not x.startswith("??")])}
    return out


# ---------------------------------------------------------------- the verdicts
def verdicts(reqs, replay, strategy, state):
    """The three answers and, for each, the reasons. The rules are the ones in this file's first lines."""
    failed = [r["id"] for r in reqs if r["state"] != PASS]
    platform = []
    platform.append((PASS if not failed else FAIL,
                     f"{len(reqs) - len(failed)} of {len(reqs)} requirements have passing tests"
                     + (f"; failing: {', '.join(failed)}" if failed else "")))
    if not replay:
        platform.append((FAIL, "no paper replay has been run"))
    else:
        cmp_ = replay["compare"]
        ok = not replay.get("errors") and cmp_["agreement_pct"] >= MIN_AGREEMENT_PCT
        platform.append((PASS if ok else FAIL,
                         f"paper replay of {replay['days'][2]} days: the trading loop and the backtest took the same "
                         f"trade {cmp_['agreement_pct']:.1f}% of the time ({cmp_['same_trade']} shared, "
                         f"{cmp_['only_in_loop_n']} only in the loop, {cmp_['only_in_backtest_n']} only in the "
                         f"backtest), {len(replay.get('errors') or [])} errors"))
    h = state.get("health") or {}
    platform.append((PASS if h.get("overall") in ("ok", "warn") else FAIL,
                     "nothing is NOT working right now" if not h.get("bad") else f"NOT working now: {', '.join(h['bad'])}"))
    platform.append((PASS if not state.get("loose_limits") else FAIL,
                     "no risk limit is looser than its built-in value" if not state.get("loose_limits")
                     else f"looser than built: {', '.join(state['loose_limits'])}"))

    strat = []
    year = (strategy.get("year") or {})
    res, rng = year.get("result") or {}, year.get("range") or {}
    if res:
        strat.append((PASS if (rng.get("r_low") or -1) > 0 else FAIL,
                      f"one-year backtest of the present rules: {res.get('trades')} trades, {res.get('return_pct'):+.2f}%, "
                      f"average {rng.get('avg_r'):+.2f}R (range {rng.get('r_low'):+.2f} to {rng.get('r_high'):+.2f})"))
    else:
        strat.append((FAIL, "no one-year backtest result is saved"))
    strat.append((PASS if strategy.get("lab_passed") else FAIL,
                  f"lab: {len(strategy.get('lab') or [])} ideas tested by walk-forward, "
                  + (f"passed: {', '.join(strategy['lab_passed'])}" if strategy.get("lab_passed") else "none passed")))
    strat.append((PASS if strategy.get("forward_passed") else FAIL,
                  "forward test on unseen data: " + (", ".join(strategy["forward_passed"]) + " passed"
                                                     if strategy.get("forward_passed") else
                                                     f"nothing has passed ({strategy.get('registered', 0)} registered)")))
    live = (strategy.get("logs") or {}).get("live") or {}
    strat.append((OPEN, f"real-money record: {live.get('trades', 0)} closed trade(s), net ₹{live.get('net', 0):,.2f}; "
                        f"the checkpoint needs {live.get('needed')} trades and even then is not proof"))

    ops = []
    local = []          # items that describe this PC: shown on the dashboard, left out of the report file
    plain = [n for n, w in state.get("secrets") or [] if w.startswith("Windows settings")]
    if plain:
        ops.append((OPEN, f"{len(plain)} secret(s) are kept as plain text in the Windows settings ({', '.join(plain)}); "
                          f"secure_secrets.py move protects them"))
        local.append(ops[-1][1])
    g = state.get("git") or {}
    if g.get("unpushed"):
        ops.append((OPEN, f"{g['unpushed']} commits exist only on this PC (not pushed)"))
        local.append(ops[-1][1])
    if g.get("uncommitted"):
        ops.append((OPEN, f"{g['uncommitted']} changed file(s) are not committed"))
        local.append(ops[-1][1])
    for found, name, text, settled in FINDINGS:
        if not settled:
            ops.append((OPEN, f"Found {found}: {name}. {text}"))
    for name, text, checked in COMPLIANCE:
        if checked is None:
            ops.append((OPEN, f"{name}: {text}"))
    ops.append((OPEN, "alerts reach only the dashboard; nothing reaches a phone when the PC is unattended"))
    ops.append((OPEN, "prices are polled as 15-minute candles; Kite's live tick stream is not used"))

    def overall(rows, need_all=True):
        states = [s for s, _ in rows if s != OPEN]
        return PASS if states and all(s == PASS for s in states) else FAIL

    return {"platform": {"state": overall(platform), "reasons": platform},
            "strategy": {"state": overall(strat), "reasons": strat},
            "operations": {"state": OPEN if ops else PASS, "reasons": ops, "local": local}}


def build(tests=None, now=None, replay=None, write=True, code=0):
    now = now or now_ist()
    if tests is None:
        tests, code = run_tests()
    reqs = requirements(tests)
    replay = replay if replay is not None else replay_evidence()
    strategy = strategy_evidence()
    state = state_now(now)
    total = {k: sum(t[k] for t in tests.values()) for k in ("passed", "failed", "skipped")}
    res = {"built": now.isoformat(timespec="seconds"), "tests": {**total, "files": len(tests), "exit": code,
                                                               "failures": {f: t["failures"] for f, t in tests.items() if t["failures"]}},
           "requirements": reqs, "replay": replay, "strategy": strategy, "state": state,
           "compliance": [{"name": n, "text": t, "checked": c} for n, t, c in COMPLIANCE],
           "findings": [{"found": f, "name": n, "text": t, "settled": s} for f, n, t, s in FINDINGS],
           "verdicts": verdicts(reqs, replay, strategy, state)}
    if write:
        os.makedirs(C.DATA_DIR, exist_ok=True)
        safefile.write_json(path(), res, indent=1, default=str)
        os.makedirs(os.path.dirname(report_path()), exist_ok=True)
        safefile.write_text(report_path(), markdown(res))
    return res


def latest():
    return _read(path())


# ---------------------------------------------------------------- the report
WORDS = {PASS: "PASS", FAIL: "FAIL", OPEN: "OPEN"}
HEADLINES = {
    ("platform", PASS): "The software does what it must, by its tests, by a replay of the trading loop and by its state now.",
    ("platform", FAIL): "The software has NOT passed every check. See the reasons below before relying on it.",
    ("strategy", PASS): "A rule set passed the lab and then held up on data it had never seen.",
    ("strategy", FAIL): "There is NO evidence that the present trading rules make money. Every test so far says they lose after costs.",
    ("operations", OPEN): "Open items for the owner.",
    ("operations", PASS): "Nothing is open.",
}


PRIVATE_LIVE = ("not written to this file, because it is the owner's own account; it is on the dashboard's Readiness "
                "page. The checkpoint needs 30 real trades, and even then is not proof.")


def markdown(res):
    v = res["verdicts"]
    t = res["tests"]
    L = ["# Ayodhya ORB 2.0: production-readiness report", "",
         f"Built {res['built'][:16].replace('T', ' ')} by `readiness.py` from the evidence listed here. "
         f"Code version `{(res['state'].get('git') or {}).get('version')}`.", "",
         "This report states what the evidence shows. It is not advice on whether to trade.", "",
         "## The three verdicts", "", "| Question | Verdict | In one line |", "|---|---|---|"]
    names = {"platform": "PLATFORM: is the software safe and correct?", "strategy": "STRATEGY: do the trading rules have a proven edge?",
             "operations": "OPERATIONS: what is still open?"}
    for k in ("platform", "strategy", "operations"):
        L.append(f"| {names[k]} | **{WORDS[v[k]['state']]}** | {HEADLINES[(k, v[k]['state'])]} |")
    # The report file can be shared (the repository is public). Items that describe this one PC, such as where
    # its secrets are kept, stay on the dashboard's Readiness page and are only counted here. So does the owner's
    # real-money record (trades and result of a real account): it is personal, not evidence about the software.
    hidden = set(v["operations"].get("local") or [])
    for k in ("platform", "strategy", "operations"):
        L += ["", f"### {names[k].split(':')[0].title()}", ""]
        L += [f"- **{WORDS[s]}** " + ("real-money record: " + PRIVATE_LIVE if text.startswith("real-money record:") else text)
              for s, text in v[k]["reasons"] if text not in hidden]
        if k == "operations" and hidden:
            L.append(f"- **OPEN** {len(hidden)} more item(s) about this PC's own set-up are listed on the dashboard's "
                     f"Readiness page and are not written to this file.")
    L += ["", "## Tests", "",
          f"{t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped, in {t['files']} test files.", "",
          "| | Area | Requirement | Tests passed | Result |", "|---|---|---|---|---|"]
    for r in res["requirements"]:
        L.append(f"| {r['id']} | {r['area']} | {r['text']} | {r['passed']}"
                 + (f" ({r['failed']} failed)" if r["failed"] else "") + f" | {WORDS[r['state']]} |")
    rp = res.get("replay")
    L += ["", "## Paper replay of the trading loop", ""]
    if rp:
        c = rp["compare"]
        L += [f"The real trading loop (`bot.Bot.cycle`) was driven through {rp['days'][2]} archived trading days "
              f"({rp['days'][0]} to {rp['days'][1]}, {rp['rounds']} rounds) on virtual money, with a simulated clock, "
              f"and compared with the backtest of the same days.", "",
              "| | Trading loop | Backtest |", "|---|---|---|",
              f"| Trades | {c['loop_trades']} | {c['backtest_trades']} |",
              f"| Net result | ₹{c['loop_net']:,.2f} | ₹{c['backtest_net']:,.2f} |", "",
              f"- The same trade (day, stock, direction) in both: {c['same_trade']} ({c['agreement_pct']:.1f}% agreement).",
              f"- Of those, same quantity: {c['same_quantity']}; same exit reason: {c['same_exit_reason']}.",
              f"- Only in the loop: {c['only_in_loop_n']}. Only in the backtest: {c['only_in_backtest_n']}.",
              f"- Rounds that raised an error: {len(rp.get('errors') or [])}.",
              "- What a replay cannot show: real order fills, a broker outage, or delays in live prices."]
    else:
        L.append("Not run.")
    if res.get("findings"):
        L += ["", "## What the testing itself found", ""]
        for f in res["findings"]:
            L += [f"- **{f['name']}** (found {f['found']}). {f['text']}",
                  f"  - {'**' + f['settled'] + '**' if f['settled'] else '**Open: needs a decision.**'}"]
    s = res["strategy"]
    L += ["", "## What is known about the trading rules", ""]
    if s.get("lab"):
        L += ["| Idea tested in the lab | Walk-forward trades | Average R | Verdict |", "|---|---|---|---|"]
        L += [f"| {x['strategy']} | {x['trades']} | {x['avg_r']:+.2f} | {x['verdict']} |" if x.get("avg_r") is not None
              else f"| {x['strategy']} | {x['trades']} | - | {x['verdict']} |" for x in s["lab"]]
    L.append(f"- Real money trade log: {PRIVATE_LIVE}")
    g = s["logs"]["paper"]
    L.append(f"- Paper trade log: {g['trades']} closed trade(s), net ₹{g['net']:,.2f}.")
    L += ["", "## Rules for trading through the Kite API", "", "| Rule | How the bot stands | Checked |", "|---|---|---|"]
    L += [f"| {c['name']} | {c['text']} | {c['checked'] or '**not yet**'} |" for c in res["compliance"]]
    st = res["state"]
    L += ["", "## The running system when this was built", "",
          f"- Health: {(st.get('health') or {}).get('overall')}"
          + (f"; not working: {', '.join(st['health']['bad'])}" if (st.get('health') or {}).get('bad') else "")
          + (f"; needs a look: {', '.join(st['health']['warn'])}" if (st.get('health') or {}).get('warn') else "") + ".",
          f"- Zerodha login on disk: {st.get('login_store')}.",
          f"- Risk limits looser than built: {', '.join(st['loose_limits']) if st.get('loose_limits') else 'none'}.",
          "- Where this PC keeps its secrets is shown on the dashboard's Readiness page, not in this file."]
    L += ["", "## How to repeat this", "", "```",
          ".venv\\Scripts\\python.exe paper_replay.py --out data\\paper_replay.json",
          ".venv\\Scripts\\python.exe readiness.py run", "```", ""]
    return "\n".join(L)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "show"
    res = build() if cmd == "run" else latest()
    if not res:
        print("No saved result. Run: readiness.py run")
        return 2
    for k in ("platform", "strategy", "operations"):
        print(f"{k.upper():11s} {WORDS[res['verdicts'][k]['state']]}")
        for s, text in res["verdicts"][k]["reasons"]:
            print(f"   {WORDS[s]:5s} {text}")
    return 0 if res["verdicts"]["platform"]["state"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
