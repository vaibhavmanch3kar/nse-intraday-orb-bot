# Ayodhya ORB 2.0: production-readiness report

Built 2026-11-18 15:16 by `readiness.py` from the evidence listed here. Code version `v1.9.0-46-g744b38e`.

This report states what the evidence shows. It is not advice on whether to trade.

## The three verdicts

| Question | Verdict | In one line |
|---|---|---|
| PLATFORM: is the software safe and correct? | **PASS** | The software does what it must, by its tests, by a replay of the trading loop and by its state now. |
| STRATEGY: do the trading rules have a proven edge? | **FAIL** | There is NO evidence that the present trading rules make money. Every test so far says they lose after costs. |
| OPERATIONS: what is still open? | **OPEN** | Open items for the owner. |

### Platform

- **PASS** 25 of 25 requirements have passing tests
- **PASS** paper replay of 237 days: the trading loop and the backtest took the same trade 100.0% of the time (328 shared, 0 only in the loop, 0 only in the backtest), 0 errors
- **PASS** nothing is NOT working right now
- **PASS** no risk limit is looser than its built-in value

### Strategy

- **FAIL** one-year backtest of the present rules: 334 trades, -16.18%, average -0.20R (range -0.25 to -0.15)
- **FAIL** lab: 6 ideas tested by walk-forward, none passed
- **FAIL** forward test on unseen data: nothing has passed (0 registered)
- **OPEN** real-money record: 1 closed trade(s), net ₹-3.96; the checkpoint needs 30 trades and even then is not proof

### Operations

- **OPEN** 4 secret(s) are kept as plain text in the Windows settings (KITE_API_KEY, KITE_API_SECRET, TRUEIP_USER, TRUEIP_PASS); secure_secrets.py move protects them
- **OPEN** 46 commits exist only on this PC (not pushed)
- **OPEN** Closing session change on 7 Dec 2026: NOT yet verified: trading hours and MIS timings change that day and must be checked again before it.
- **OPEN** alerts reach only the dashboard; nothing reaches a phone when the PC is unattended
- **OPEN** prices are polled as 15-minute candles; Kite's live tick stream is not used

## Tests

837 passed, 0 failed, 2 skipped, in 71 test files.

| | Area | Requirement | Tests passed | Result |
|---|---|---|---|---|
| K1 | Capital protection (Kavach) | One risk engine decides every new position, in paper, real money and backtests alike | 22 | PASS |
| K2 | Capital protection (Kavach) | Risk per trade, open positions, trades per day, daily loss limit and the cool-down after losses are enforced | 43 | PASS |
| K3 | Capital protection (Kavach) | No real-money entry on delayed, stale, missing or invalid prices | 23 | PASS |
| K4 | Capital protection (Kavach) | Positions are sized from the real balance and skipped when not even one share is affordable | 27 | PASS |
| E1 | Execution | Paper and real money are separate; real money needs both Windows settings and the day's arming | 81 | PASS |
| E2 | Execution | Every order is recorded before it is sent, carries its own tag and is never sent twice; an order ID is not treated as a fill | 39 | PASS |
| E3 | Execution | The bot's record is compared with Zerodha at start and every few minutes; a mismatch holds new entries | 35 | PASS |
| E4 | Execution | Every open position keeps an exchange stop-loss, checked on every round | 35 | PASS |
| E5 | Execution | Only one module can send or cancel an order; the dashboard and the analysis pages cannot reach it | 31 | PASS |
| E6 | Execution | Positions are closed before the broker's own square-off | 7 | PASS |
| D1 | Market data | Candles are validated; delays and gaps are measured and shown | 37 | PASS |
| D2 | Market data | Kite's request limits are respected and a data failure falls back safely | 19 | PASS |
| D3 | Market data | Scanner and Review build in the background with progress and Cancel | 19 | PASS |
| S1 | Strategy testing | No strategy can see a future candle; the lab's copy of the live rules matches them candle by candle | 19 | PASS |
| S2 | Strategy testing | Backtests include charges and slippage and give the same answer every time | 27 | PASS |
| S3 | Strategy testing | Thirty trades is treated as an early checkpoint, not as proof | 4 | PASS |
| A1 | Research and portfolio | Chanakya shows measured evidence and Kavach's answer; no probabilities, no orders | 19 | PASS |
| A2 | Research and portfolio | Rajya reads the account only; rebalancing is a simulation | 40 | PASS |
| U1 | Dashboard | All eight pages; choosing an account view never changes the trading mode | 26 | PASS |
| U2 | Dashboard | Start, Pause, Resume, Stop and Zerodha logout ask first and accept only the dashboard's own forms | 21 | PASS |
| U3 | Dashboard | Apply Changes checks the new code, restarts only when safe, verifies the result and can go back | 30 | PASS |
| H1 | Security and self-healing | The login is stored protected and no secret reaches a log | 27 | PASS |
| H2 | Security and self-healing | Health checks and alerts | 26 | PASS |
| H3 | Security and self-healing | A crashed or stuck bot is detected, recorded and restarted; files are saved safely | 44 | PASS |
| H4 | Security and self-healing | Daily backups, the trade journal and the candle archive | 21 | PASS |

## Paper replay of the trading loop

The real trading loop (`bot.Bot.cycle`) was driven through 237 archived trading days (2025-12-03 to 2026-11-17, 5925 rounds) on virtual money, with a simulated clock, and compared with the backtest of the same days.

| | Trading loop | Backtest |
|---|---|---|
| Trades | 328 | 328 |
| Net result | ₹-15,773.21 | ₹-15,770.51 |

- The same trade (day, stock, direction) in both: 328 (100.0% agreement).
- Of those, same quantity: 313; same exit reason: 328.
- Only in the loop: 0. Only in the backtest: 0.
- Rounds that raised an error: 0.
- What a replay cannot show: real order fills, a broker outage, or delays in live prices.

## What the testing itself found

- **The cool-down count depended on the order in which same-candle exits were recorded** (found 2026-11-18). When a winning and a losing position closed on the same candle, the 'three losses in a row' count depended on which was recorded first. The trading loop recorded them in the order they were opened, the backtest in another order. This was the cause of every difference in the first one-year paper replay (6 trades: twice in the year one of the two went into a cool-down and the other did not).
  - **Settled 2026-11-18 by the owner's decision: exits recorded at the same moment are counted losses first (risk.State._count_streak, tests/test_cooldown_order.py). One-year backtest before: 333 trades, -16.46%; after: 334 trades, -16.18%.**

## What is known about the trading rules

| Idea tested in the lab | Walk-forward trades | Average R | Verdict |
|---|---|---|---|
| failed_breakout | 133 | -0.48 | FAIL |
| orb | 128 | -0.21 | FAIL |
| orb_atr | 70 | -0.35 | FAIL |
| orb_regime | 84 | -0.15 | FAIL |
| orb_retest | 68 | -0.65 | FAIL |
| vwap_pullback | 162 | -0.42 | FAIL |
- Real money trade log: 1 closed trade(s), net ₹-3.96.
- Paper trade log: 3 closed trade(s), net ₹118.15.

## Rules for trading through the Kite API

| Rule | How the bot stands | Checked |
|---|---|---|
| SEBI retail algo framework (in force since 1 Apr 2026) | Below 10 orders a second needs no separate registration; the bot sends a handful a day. | 2026-11-18 |
| Static IP for API orders | Orders go out through the fixed, whitelisted address configured for this PC. | 2026-11-18 |
| No market orders from an algo | The bot sends LIMIT entries and exits and SL (stop-loss limit) protection only. | 2026-11-18 |
| Order tags | Every bot order carries a tag of at most 20 letters and digits. | 2026-11-18 |
| Zerodha MIS square-off | Zerodha closes MIS positions in F&O stocks at 3:12 PM for a fee; the bot closes at 3:00 PM. | 2026-11-18 |
| Closing session change on 7 Dec 2026 | NOT yet verified: trading hours and MIS timings change that day and must be checked again before it. | **not yet** |

## The running system when this was built

- Health: ok.
- Zerodha login on disk: protected.
- Secrets: KITE_API_KEY: Windows settings (plain text); KITE_API_SECRET: Windows settings (plain text); TRUEIP_USER: Windows settings (plain text); TRUEIP_PASS: Windows settings (plain text).
- Risk limits looser than built: none.

## How to repeat this

```
.venv\Scripts\python.exe paper_replay.py --out data\paper_replay.json
.venv\Scripts\python.exe readiness.py run
```
