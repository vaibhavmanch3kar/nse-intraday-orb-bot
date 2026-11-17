# Review and Scanner speed, safe updates and restarts: report

Branch `feature/review-scanner-jobs`, from `main` at v1.8.0 (efc28dd). One commit per approved item. Nothing
here changes the strategy, the risk limits, entries, exits, orders or the backtest; LIVE mode was never
enabled, no order was placed, and the running bot was never stopped or restarted while this was built.

## 1. What was wrong (measured on 17 Nov, before any change)

| Finding | Evidence |
|---|---|
| The pages built their result **while the browser waited**. Once the saved copy was older than 15 min (Review) or 30 min (Scanner), the next visitor waited for a full rebuild. | `render_review` / `render_scanner` called `review.latest()` / `scanner.latest()`, which rebuilt in the request. |
| With Kite as the data source, downloads were **one at a time**, paced to 3 a second. | Review download: 15 symbols in **19.8 s**. Scanner: 50 symbols in **33.7 s**. (Yahoo: about 0.3 s per stock.) `data_kite.fetch_many` ignored `workers`. |
| The pages **shared Kite's 3-a-second budget with the bot**, in the same program, and the pacing slept while holding its lock. | `data_kite._call`: `time.sleep(...)` inside `with _lock`. The bot's candle fetch could queue behind a scan. |
| **Wasted and duplicate work.** The browser gave up, the build went on; reloads started more builds. | Five `ConnectionAbortedError` tracebacks in the bot window on 17 Nov. No single-flight guard. |
| Saved results **expired on a clock**, not when the data changed. | Daily candles change once a day, but the Scanner re-downloaded 50 stocks every 30 minutes (about 13 times a day). |
| **Today's Review could stay half-built.** | `build(day)` returned a saved copy of the day without checking its age; the day links used it. |
| **Errors were hidden.** | Scanner data errors only inside the folded help text; a failed Time Machine download showed nothing. |
| The History check re-read all 60 days for every bar. | `strategy.evaluate()` regroups the whole history: 1.5 ms per call; History 0.36–1.4 s per signal stock. |
| Stopping the scheduled task **did not stop the bot.** | On 17 Nov `Stop-ScheduledTask` ended only the `run_bot.ps1` window; both `bot.py` processes kept running. |
| Any dashboard change needed a **bot restart**. | The dashboard ran as a thread inside `bot.py`; settings and code are read once at start. A technical limit, not a safety rule. |

The safety rules that are deliberate were kept: LIVE needs both switches in the window that starts the bot,
the scheduled task always starts PAPER, the paper opening balance changes only at start with no open position,
and only one bot can run.

## 2. What changed (one commit each)

| # | Commit | Change |
|---|---|---|
| 1 | 791f63f, 53b033e | Equivalence tests with a copy of the v1.8.0 Review replay; benchmark tool `tools/bench_pages.py`; flaky theme test fixed (clock seconds). |
| 2 | 910f180 | Faster Review replay and History: the unchanged rules run on the shortest piece of bars that gives the same answer. `strategy.evaluate`, the bot and the backtest are untouched. |
| 3 | 188008d | `jobs.py`: background builds, one at a time per page (a second request joins), queued Review days, 5-minute limit (skipped stocks listed), `/api/jobs`, Refresh redirects, a browser leaving early is ignored, caches written atomically. |
| 4 | 512fd3d | `freshness.py`: Scanner valid until the next daily candle is final (15:45); past Reviews final; today's Review "in progress, as of HH:MM", refreshed after each candle; v1.8.0 half-built copies recognised; settings changes refresh; partial results retried after 10 min. |
| 5 | 0abdcfe | Kite pacing: slots booked outside the lock; page jobs use at most 2 of 3 requests a second, wait while the bot fetches (2 s) and for 90 s after each candle close. |
| 6 | 634d4a2 | Status strip (Loading / Running / Completed / Failed / Data stale / Partial; words and icon; polls only while running); no automatic retry loop after a failure; errors shown. |
| 7 | 9cc9a2e | Dashboard as its own program (`run_bot.ps1` starts it minimized); Kite budget shared between programs through a locked file; theme files reloaded when they change. |
| 8 | 43081ad | `buildinfo.py` and `restart.py`: running version, waiting updates (dashboard files / bot files / settings) and when they become active; Restart the dashboard now; a first "restart when safe" (replaced by the approved Apply Changes flow, section 10). |
| 9 | cc74db1 | Stopping the task stops the bot (`--launcher-pid`); stop requests take effect within a second; `7-STOP-BOT.bat`. |
| 10 | 376e746 | This report, screenshots, README, CHANGELOG; a faster first check for waiting updates. |
| + | b4687bd | Cherry-picked from `feature/zerodha-logout-mode` (698e38c): every button needs this dashboard's Host, exact Origin and a per-start CSRF token; the restart buttons use the same checks. |
| + | 7cbf373 | Approved addition to items 7–9: **Apply Changes & Restart Bot** (section 10). |
| + | this commit | Docs for the addition. |

## 3. Same results: before/after on frozen data

Inputs frozen once (the bot's 60 sessions of 15-min candles, 60 days of 5-min bars for the 12 stocks, 2 years of
daily bars for the 50 scanner stocks, India VIX; fetched from Yahoo, never Kite), clock fixed at
17 Nov 2026 18:00 IST. After every item the same harness recorded every Review replay (54 days × 12 stocks),
96 History checks, 6 full Review builds (Time Machine at its fixed seed, plus History), the Scanner and the
portfolio backtest, and compared them with v1.8.0.

**Result after every item: identical**, apart from one new field the Scanner now saves (`settings`, used to
notice settings changes). The backtest is unchanged throughout: **84 trades, return −3.03%, profit factor 0.47,
max drawdown 3.52%, net −₹3,031.66, charges ₹2,103.26.**

## 4. Speed

**Calculations** (frozen data, same machine; it was also running the bot, so timings vary by run):

| | v1.8.0 | now |
|---|---|---|
| Replay all 54 days × 12 stocks | 15.2 s | 2.1–3.4 s |
| 96 History checks | 72.5 s | 12.1–15.7 s |
| 6 full Review builds | 10.6 s | 2.5–3.9 s |
| One History check (per stock, median) | 0.36–0.59 s | 0.12–0.18 s |
| Replay one day, 12 stocks | 0.14–0.28 s | 0.03–0.045 s |

**Pages** (v1.8.0 and the branch side by side, each with its own copy of the data, saved results made out of
date, Yahoo data so the bot's Kite budget wasn't touched):

| | v1.8.0 | now |
|---|---|---|
| Review, saved copy out of date | 4.9–7.1 s (waits for the rebuild; ~20–25 s with Kite) | **0.10–0.17 s** (saved copy shown, rebuild in the background, done after 3.9 s) |
| Scanner, saved copy 19 h old | 1.4–3.5 s (re-scans; ~34 s with Kite) | **0.02–0.03 s** (still valid until today's 15:45) |
| Review / Scanner again | 0.005–0.023 s | 0.010–0.042 s |
| Live page data (`/api/live`) | 0.006–0.012 s | 0.007–0.011 s |
| First Live page after the dashboard starts | 0.83–0.88 s | 1.6–2.0 s (once per start: version check and the Kite library import; later loads 0.03 s, same as v1.8.0) |

With Kite, a background rebuild still takes about as long as before (Kite allows 3 requests a second, and pages now
use at most 2 and give way to the bot), but nobody waits for it: the page shows the saved result and the strip
shows the progress. Scanner downloads drop from about 13 a day to 1.

## 5. Responsiveness while the bot runs

- The Review and Scanner builds no longer run in the bot's program at all (item 7).
- Page downloads never take the bot's Kite slot: a test with two real programs shows at most 3 requests in any
  second, and a waiting page job never delays the bot (`tests/test_kite_pacing.py`).
- Pages answer while a build runs: in the A/B run the Review page answered again in 0.015–0.042 s while its own
  build was still running in the background; each request has its own thread.

## 6. Screenshots

`screens/`: Review and Scanner in Default, Playground and Sandbox; desktop (1366 px, PNG) and phone (375 px,
JPG); light mode. Dark mode, the Running state and the phone layout were also checked by hand in the browser.

| | Desktop | Phone |
|---|---|---|
| Default | [Review](screens/default-review-desktop.png) · [Scanner](screens/default-scanner-desktop.png) | [Review](screens/default-review-phone.jpg) · [Scanner](screens/default-scanner-phone.jpg) |
| Playground | [Review](screens/playground-review-desktop.png) · [Scanner](screens/playground-scanner-desktop.png) | [Review](screens/playground-review-phone.jpg) · [Scanner](screens/playground-scanner-phone.jpg) |
| Sandbox | [Review](screens/sandbox-review-desktop.png) · [Scanner](screens/sandbox-scanner-desktop.png) | [Review](screens/sandbox-review-phone.jpg) · [Scanner](screens/sandbox-scanner-phone.jpg) |

## 7. Tests

330 pass, 1 skipped (the stop-script test does nothing while the real bot is running on this PC). v1.8.0 had 248.
New: `test_review_equivalence`, `test_jobs`, `test_freshness`, `test_kite_pacing`, `test_separate_dashboard`,
`test_restart`, `test_stop`.

## 10. Apply Changes & Restart Bot (approved addition)

The Settings page section **Changes Pending** shows what the bot and the dashboard run (version, commit, edited
or not) and what is waiting in the folder, split into **dashboard / theme only** (Restart the dashboard now, any
time; the bot is not touched) and **bot code or settings** (needs a bot restart). Nothing pulls or merges code.

| | Apply After Market Close (recommended) | Apply Now (when safe) |
|---|---|---|
| When | the next start (09:00) | as soon as it is safe |
| PAPER | - | no open position, not within 90 s after a candle close; no new entries while waiting |
| Real money | - | only before 09:15 or after the close, with no open position |
| Check of the new code | at the close (15:35), in a separate Python | before stopping, in a separate Python: if it fails, the old bot keeps running |
| After | - | the new bot checks within 90 s: one copy, new process, code in the folder, same mode, positions, trades today, cash, opening balance |

- **Stages** (shown live, polled from `/api/restart` only while active): Restart Pending – Waiting for Safe
  Conditions (with the reason, e.g. "1 open position") → checking the new code → stopping (journal and state
  saved) → restarting (`run_bot.ps1` starts the bot again, same environment) → Changes applied: "Running vX
  (commit) since HH:MM".
- **Failure**: a red banner with the reason and **Try again**, **Go back to the previous version** (the last
  version that started successfully; recorded at every successful start; `git switch --detach`, refused if
  program files were edited and not committed; asks first) and **Leave it as it is**, with steps.
- **Safety**: requests are `data/restart_request.json` / `data/apply_after_close.json` holding only a time and a
  nonce; any other field is refused and logged, so nothing can switch PAPER/LIVE. A second request while one is
  pending gets 409. Every request and outcome is logged in `data/logs/restart_requests.log`. The buttons use the
  same Host / Origin / CSRF checks as every other dashboard button. Balances, trade history, journals and
  settings are never touched.
- **Tests** (`tests/test_restart.py`, 30): request format and refusals, safety rules for PAPER and real money,
  waiting with an open position (entries held), broken new code keeping the old bot running (and a real syntax
  error caught by `preflight.py`), the 90-second checks, Go back, 409, the page in all three themes, and a real
  dashboard restarting itself. Checked by hand at phone width.

## 8. Putting it live

- Merge after 15:35, when the bot has stopped by itself. Until then the bot keeps running v1.8.0 unchanged.
- The next 09:00 start (scheduled task) runs `run_bot.ps1`, which now starts the dashboard in its own minimized
  window and then the bot. The dashboard keeps running after 15:35.
- The first Scanner visit after the update re-scans once (the saved scan is from v1.8.0, which didn't record its
  settings).
- If you start the bot with `python bot.py` by hand, start the dashboard with `3-DASHBOARD.bat`; the bot logs a
  hint when no dashboard is running.

## 9. Limits

- "Go back to the previous version" leaves the folder on a detached commit; the message says how to return
  (`git switch <branch>`). It refuses when program files were edited and not committed.

- Apply Now relies on `run_bot.ps1` to start the bot again; a bot started by hand stays stopped, and the Live
  page shows a red banner after 90 s.
- The settings check runs a small Python on the Settings page (cached for 2 minutes); the Live page only compares
  files.
- A Kite outage still makes a rebuild fail after the time limit; the saved result stays on the page with the reason.
