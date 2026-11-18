# Versions

[← back to README](README.md)

Every version is on the [Releases page](https://github.com/vaibhavmanch3kar/nse-intraday-orb-bot/releases),
with a ZIP you can download. To go back to an older version, download that version's ZIP.
Your settings (Windows user environment variables) and your `data` folder are not part of the ZIP, so keep them.

## v2.0 (18 Nov 2026, branch `release/ayodhya-orb-2.0`): safety, research and readiness

Step-by-step guide with screenshots: [docs/GUIDE-2.0.md](docs/GUIDE-2.0.md).
What the evidence shows: [docs/PRODUCTION-READINESS.md](docs/PRODUCTION-READINESS.md).

**The trading rules are unchanged except for one counting rule** (the cool-down, below). On the one-year candle
archive to 17 Nov 2026 the backtest was 333 trades, −16.46% before that change and is 334 trades, −16.18% after
it. The rules still lose money after costs and no idea tested in the lab has passed: paper trading stays the
default, and real money stays off unless you set it up yourself ([docs/LIVE-TRADING.md](docs/LIVE-TRADING.md)).
843 offline tests pass.

### Reliability

- **Watchdog.** A second scheduled task checks every 5 minutes in market hours and starts the bot or the
  dashboard again if either has stopped (at most five times a day). An error that ends the bot is saved in full
  to `data\logs\crash_<time>.txt`; a bot that stops responding for 10 minutes is ended and started again.
  **Stop** is remembered: the watchdog leaves a bot you stopped alone.
- **Fixed:** the bot could end silently when Windows refused to replace a file another program was reading.
  Every save now waits out a busy file.
- **Fixed:** the day's counters were started only at the first round after the open; they now start before it.
- **Popups** on every page when a Zerodha position changes, when the watchlist stops updating, and for every
  automatic restart.

### Kavach: one risk engine

- Every new position, in paper, real money and backtests, is decided by the same nine checks in the same order:
  time, pause, balance, data, account limits, price, sector, size, liquidity. A refused signal names the check
  and the reason.
- **Real money never trades on bad prices:** delayed or fallback (Yahoo) prices, candles that fail validation, a
  signal older than 5 minutes, or a missing index candle while the index filter is on.
- New optional limits: positions per sector, and order size against the candle's volume.
- **Square-off moved from 15:10 to 15:00**, clear of Zerodha's own 3:12 PM square-off for F&O stocks.
- **Changed:** positions that close on the same candle are counted **losses first** for the "three losses in a
  row" cool-down. Before, the count depended on which exit happened to be recorded first, so the bot and the
  backtest could disagree about a cool-down.
- **Fixed:** on a day money is added to the account, a restart no longer wipes the day's booked loss out of the
  balance trades are sized from. A gain is never added, so the figure can only be the same or smaller.

### Execution safety (real money)

- Every order is written to an append-only record before it is sent and carries its own tag. A lost reply is
  looked up by that tag instead of sending the order again. An order ID is not treated as a fill.
- Partial fills, rejections, timeouts and late fills are handled; the stop-loss order is verified when placed
  and checked again on every round.
- The bot's record is compared with Zerodha at every start and every 5 minutes. A mismatch holds new entries;
  it never opens or closes a position on its own.
- A margin check before each entry; if it cannot be made, no order is sent.

### Research and portfolio

- **Yantrashala (strategy lab):** every idea runs through the one backtest engine with no view of future
  candles, is judged by a rule fixed in advance (walk-forward, a range that chance allows, profit factor, share
  of positive months), and a passing idea is registered and judged again only on later data. Six ideas tested;
  none passed.
- **Chanakya (research assistant):** for each watchlist stock, measured checks for and against a trade, the
  plan with size and charges, Kavach's answer and the past record with its sample size. A count, not a
  probability. No AI model, no orders.
- **Rajya (portfolio):** allocation, concentration, risk budget, past movement, correlation, what-if arithmetic,
  booked result and drawdown, and a rebalancing simulation that orders nothing. Reads Zerodha only.
- **One-year backtest page** under Review: runs the present rules over the whole archive from the dashboard in
  about ten seconds.

### Dashboard

- **All eight pages in the navigation**: Live, Trades, Review, Scanner, Chanakya, Kavach, Rajya, Yantrashala.
  The row scrolls on narrow screens and on phones.
- **System health** card on the Live page with twelve checks, an alert history and `/api/health`.
- **Start** button when the bot is not running. It asks first, says which mode the bot would start in and cannot
  change the mode.
- **Readiness** page (`/readiness`): three verdicts from evidence, with the tests behind every requirement.
- Review and Scanner builds can be cancelled; a cancelled build saves nothing.
- Clearer wording: a candle's age is no longer shown as a data delay; money added today is explained; a day
  without trades says why; the page says so when the account in view is not the one the bot trades.

### Security

- **Today's Zerodha login is stored protected by Windows** (readable only by your Windows user on this PC).
  An older login file is still read and is then protected. Versions before 2.0 cannot read the protected file:
  after going back to one, log in again.
- **`secure_secrets.py`** can move the API key, API secret and proxy login out of the plain Windows settings
  into the same protected store, and back. It never shows a value and never loses a key on a failure.
- **Logs without secrets:** API keys, the proxy login and the access token are removed before anything is
  written, tracebacks included. Every log line is also saved as JSON in `data\logs\events_<date>.jsonl`.
- Tests prove from the code that only one module can send or cancel an order, that only the bot loads it, and
  that the dashboard and all analysis pages cannot reach it.

### Readiness

- **`paper_replay.py`** drives the real trading loop through every archived day on virtual money and compares
  it with the backtest. On one year: 328 trades in each, all 328 identical.
- **`readiness.py`** maps 25 requirements to the tests that prove them and writes the report. Platform: pass.
  Strategy: fail (no proven edge). Operations: open items listed.

## v1.9.0 (17 Nov 2026): Paper | Real Money control, Zerodha logout, fast Review and Scanner, six Indian themes

The strategy, risk limits, entry and exit rules and the backtest are unchanged: on frozen data the backtest is
identical to v1.8.0 (84 trades, −3.03%, PF 0.47). Live trading stays off unless you set it up yourself
([docs/LIVE-TRADING.md](docs/LIVE-TRADING.md)).

### Real-money trades sized from your real Zerodha balance

- **Fixed:** in REAL MONEY mode the bot sized trades from its own record, which on a first live start was the
  default ₹1,00,000 whatever was in Zerodha (with ₹5,000 in Zerodha that meant about ₹500 risk per trade and
  a ₹2,000 daily loss limit). Now a new real-money record starts at ₹0 and the bot reads the balance from Zerodha
  (read-only) before any trade: the smaller of today's opening cash and the cash available now, without
  pledged shares. If it can't, or the balance is zero, it opens no new trades and says why. A restart with
  positions open keeps the record. Shown on the Live page as "Balance from Zerodha". Paper and the backtest
  are unchanged.

### Six India-inspired dashboard themes

Look only: colours, names, pictures and page icons. Trading, the bot, its settings, the mode, orders and data are
unchanged; the frozen backtest is identical (84 trades, −3.03%, PF 0.47).

- **Six new themes** under Settings > Indian heritage: **Kurukshetra** (Mahabharata), **Swarajya** (Maratha forts),
  **Takshashila** (Arthashastra), **Antariksh** (mission control), **Vijayanagara** (Hampi bazaar) and
  **Chola Samudra** (sea trade). Each has its own palette (light and dark), shapes, header scene, mascot, eight
  page icons, themed names next to the plain terms, and an Indian-script brand line.
- Settings groups the nine themes: Original, Indian heritage, Game styles. Default, Playground and Sandbox are
  unchanged.
- Every theme passes the theme tests (all names present, WCAG AA contrast in both modes, under 100 KB, no
  animation, nothing loaded from the internet, the same numbers on every page) and fits a 375 px phone screen.

### Zerodha logout, real trading mode on every page, Paper | Real Money control

The strategy, risk limits, entry and exit rules, orders and the backtest are unchanged (frozen backtest identical:
84 trades, −3.03%, PF 0.47). Live trading stays off unless you set it up as below.

- **Real money needs today's arming too.** Before, `DRY_RUN=false` and `CONFIRM_LIVE=YES` alone made the bot
  trade real money, so if they were ever saved in Windows the 9:00 task would have started live. Now it also
  needs the day's arming on the dashboard; without it the bot trades paper, however it was started.
- **Paper | Real Money on Settings > Trading mode.** Read-only readiness checks (switches, account active and order
  permission that you confirm in Windows, static-IP proxy, today's login, funds), shown as **Live Trading Locked**
  with the fixes when any fails, plus the strategy audit's NO-GO warning. Arming needs every check, a Zerodha
  login from the last 10 minutes and the typed words `REAL MONEY`; it applies at a safe moment and ends at
  midnight. **Switch back to Paper** stops new real-money trades at once and switches when the open ones have
  closed (they are still managed). **Close all positions now** is separate, with its own confirmation.
  See [docs/LIVE-TRADING.md](docs/LIVE-TRADING.md).
- **Log out of Zerodha** button (Live page): asks first, ends the session at Zerodha, deletes the login saved on
  this PC, never shows or logs the token; refused while the bot holds real-money positions.
- **The real trading mode on every page**, in the header, in plain words in every theme and on a phone:
  PAPER · virtual money, REAL MONEY · Zerodha, or Bot not running, plus any switch that is waiting.
- **Buttons accept only the dashboard's own page:** exact Origin/Host checks and a secret code made at each
  dashboard start (other websites and programs can't press them).

### Fast Review and Scanner pages, safe updates and restarts

The strategy, risk limits, entry and exit rules, orders and the backtest are unchanged: on frozen data every
Review, History check and backtest trade is identical to v1.8.0 (84 trades, −3.03%, PF 0.47). Full report:
[docs/design/jobs/REPORT.md](docs/design/jobs/REPORT.md).

- **Review (Chitrakoot) and Scanner (Hanuman's Watch) open at once.** They show the saved result straight away
  and build a new one in the background when needed (v1.8.0 made you wait 20–35 s while it downloaded from Kite,
  and often the browser gave up).
  - A **status strip** says Completed / Running (progress, time) / Failed (why, Retry) / Data stale (why,
    Refresh) / Partial, in words and an icon. The page updates itself when a build finishes.
  - One build at a time per page; a second click joins it. A 5-minute time limit, with skipped stocks listed.
  - Scanner data errors and failed Time Machine downloads are now shown instead of hidden.
- **Saved results follow the data.** The Scanner refreshes once a day after the close (not every 30 minutes).
  Past Reviews are final. Today's Review is marked **in progress** and refreshed after each 15-minute candle
  (v1.8.0 could show a half-built morning copy all day). Results built with other settings are refreshed.
- **History check 3× faster**, with identical results.
- **The bot comes first for Kite data.** Page downloads use at most 2 of Kite's 3 requests a second, wait while
  the bot fetches and for 90 s after each candle close, and never hold a lock while waiting.
- **The dashboard is its own program.** `run_bot.ps1` starts it in a minimized window; it keeps running when the
  bot stops and can be restarted without touching the bot. The Kite request budget is shared by both through a
  small locked file. Edited theme files show without a restart.
- **Changes Pending and Apply Changes & Restart Bot** on the Settings page: what the bot and the dashboard run
  (version, commit, edited or not), what is waiting in the folder, split into dashboard/theme only
  (**Restart the dashboard now**) and bot code or settings. **Apply After Market Close** (recommended; the new
  code is checked at the close) or **Apply Now (when safe)**: PAPER with no open position and not right after a
  candle close, no new entries while waiting; real money only before 9:15 or after the close. The new code is
  checked in a separate process first, the stages show live, and the bot checks itself within 90 s (same mode,
  positions, trades, cash, balance). Failures: Try again / Go back to the previous version / Leave it as it is.
  Requests hold only a time and a nonce (anything else is refused), duplicates get 409, every request is logged,
  and nothing can switch PAPER/LIVE. Secret settings are never recorded. Same request checks (Host, Origin,
  per-start token) as every other dashboard button.
- **Stopping really stops.** Stopping the scheduled task now stops the bot too (it used to keep running), a stop
  request takes effect within a second, and the new `7-STOP-BOT.bat` confirms the bot has stopped.
- **Tests:** 330 pass (v1.8.0 had 248), 1 skipped while the real bot runs on the PC. They include a v1.8.0
  reference replay, two real programs sharing the Kite budget, a real dashboard restarting itself and a broken
  code change caught before a restart.

## v1.8.0 (17 Nov 2026): dashboard themes: Default, Playground and Sandbox

Display only. The strategy, risk limits, bot logic, orders, settings, the trading mode and account data are
unchanged. Default looks exactly as before: pixel-identical screenshots of every page in dark and light, apart
from the new Settings button.

- **Settings page** (`/settings`, and a gear button at the top right): choose **Default**, **Playground** or
  **Sandbox**. Each choice has a small live preview drawn in that theme.
  - The choice is a browser cookie (`ayodhya-ui-theme`) and applies on the next page with no restart.
  - An unknown or missing choice falls back to Default.
  - Light/dark (☀/☾) works with every theme.
- **`themes/` folder.** One self-contained folder per theme, plus `registry.json`:
  - `theme.json`, `tokens.css` (colours for dark and light, shapes, fonts), `strings.json` (themed names, icons,
    short texts) and `assets/` (original SVG avatar, header scene, icons and a small skin stylesheet).
  - The loader is `uitheme.py`. How to add or edit a theme is in `themes/README.md`.
- **Playground** is bright and rounded, with a robot helper (Bolt). **Sandbox** is square and pixel-style,
  with a block robot (Cubit) and a pixel landscape.
  - The art is original. No game's names, logos, characters, textures or assets are used.
- **Finance wording stays plain in every theme.** Themed names only ever sit beside the plain term
  ("Quest Log · Trades", "Practice · PAPER", "Real Money · LIVE").
  - Prices, P&L signs and arrows, status words, stops, errors, banners and confirmations read the same in every
    theme. A test renders every page in all three themes and compares the numbers, signs, currency, times and
    status words.
- **Accessibility and size:**
  - Every new theme meets WCAG AA (4.5:1) for text in dark and light.
  - Keyboard: radio buttons, focus returns to your choice after saving.
  - No animation is added, and reduced motion is respected. No new network requests.
  - Decorative assets (tokens.css plus assets/): Default 1.7 KB, Playground 6.6 KB, Sandbox 9.7 KB.
- **Tests:** `tests/test_themes.py` adds 44 tests, for 248 in total.

## v1.7.0 (16 Nov 2026): redesigned dashboard: light and dark, desktop and phone

A display-only redesign. The strategy, risk limits, bot logic, balances and live-trading safety are
unchanged, and the backtest gives the same trades. See the README section "Look and feel".

- **Themes and frame:**
  - **Themes:** dark (night sky, saffron and gold) and light (ivory and saffron). The dashboard follows
    your PC's or phone's setting; the ☀/☾ button switches, and the browser remembers.
  - **Header:** compact, with the bow logo, ॥ अयोध्या ॥ and the ribbon, plus page tabs and the
    Paper Account | Zerodha Account switch.
  - **Phone:** a bottom bar with Live, Trades, Review and Scanner.
- **Live page:**
  - A **status bar**: market, bot, the mode it trades in, data delay, Zerodha login.
  - **Key figures**: equity, available funds, today's P&L, open P&L, trades used of 4, daily loss limit used.
  - **Today's equity** chart.
  - **Position cards**, showing where the price sits between the stop and the target.
  - The watchlist, and a side column with controls, opening balance, login, events, calendar and limits.
  - **Pause now asks before acting**, like Stop.
- **No more page reloads.** The Live page updates its figures in place every 10 seconds from a new
  read-only address (`/api/live`). It pauses while the tab is hidden and warns when it is offline. The
  opening-balance form is never wiped.
- **Interactive charts** (no outside libraries). They draw at the screen's real size, and hover, tap or
  the arrow keys show exact values. Equity curves have 1W/1M/3M/All buttons. The candle chart of each
  trade works the same way.
- **Trades page:**
  - Key figures at the top.
  - Tabs: Overview, Journal (Daily/Weekly/Monthly), Trade log and Backtest.
  - The trade log can be filtered by stock, side and result.
  - Net P&L per trading day, from the journal.
  - On a phone, each trade is a card.
- **Review, Scanner and trade chart pages** use the new look, with phone cards and the explanations
  folded away.
- **Accessibility and speed:**
  - Every text colour meets WCAG AA in both themes.
  - Skip link, keyboard focus, readable charts and "reduce motion".
  - Profit and loss always carry ▲▼ or +/−, not only colour.
  - Pages are small (Live about 50 KB).
- **Safety, tested:** looking at any page or data address writes no file and changes no setting, and the
  new address can't change anything.
- New README screenshots (sample figures), including phone and light theme. 204 tests pass (20 new).

## v1.6.0 (16 Nov 2026): daily journal, weekly and monthly reports, daily logs and backups

Recording only: the strategy and risk rules never read any of this, and a problem writing it is a warning in
the log, never a reason to stop trading. Paper and live are kept separate. See the README section
"Daily journal, weekly and monthly reports".

- **Daily journal.** `data\journal\journal_paper.csv` has one row per trading day:
  - equity at the start and end, net P&L and %, charges;
  - trades, wins, total R, best and worst trade;
  - the day's worst drawdown;
  - signals taken and skipped, with the reasons;
  - data outages and when the bot ran.

  `data\journal\paper\<date>.json` holds that day in detail: every signal and what happened to it, outages,
  bot starts, and equity every minute. The bot saves the day every round. After the close the day is marked
  **complete**, a STOP file marks it **stopped HH:MM**, and a day the bot didn't finish shows as
  **incomplete**. Days with trades but no journal are **rebuilt** from the trade log.
- **Daily | Weekly | Monthly** card on the Trades page. It shows trades, win rate, net ₹ and return, charges,
  average R, best and worst day, worst drawdown, skipped signals and outage minutes, with a **Download CSV**
  button. It follows the Paper Account | Zerodha Account view.
- **Saved reports:** `data\reports\paper_week_2026-W47.html` and `paper_month_2026-11.html`, written after
  the close on the last trading day of each week or month, or at the next start if the bot missed the close.
  Each file has a summary, equity at each close, a day-by-day table, the trades and the skip reasons. They
  print cleanly to PDF, and the Trades page links to them. To make one any time, run
  `reports.py --week 2026-W47` or `--month 2026-11`.
- **One log file per day:** `data\logs\bot_<date>.log`. The old `data\bot.log` is split by date at the first
  start, and the original is kept as `data\logs\bot_until_<date>.log`.
- **Daily backup:** after the close, `data\` is zipped to `KiteConnect\backups\data\data_<date>.zip` and the
  newest 30 are kept.
  - **To protect against a disk failure,** set `BACKUP_DIR` to a OneDrive or USB folder. `BACKUP_KEEP`
    changes how many backups are kept.
  - **To back up by hand,** run `backup.py`.
- Strategy, risk limits and backtest are unchanged: the same trades on the same data before and after each
  change. 184 tests pass (28 new).

## v1.5.0 (16 Nov 2026): choose the paper account's opening balance

The paper account no longer has to start at ₹1,00,000. See the README section "Paper opening balance".

- **Opening Balance · Paper Account** card on the Live page (Paper Account view): ₹10,000, ₹25,000, ₹50,000,
  ₹1,00,000, ₹5,00,000 or a custom amount (whole rupees, ₹10,000 to ₹1 crore). Saving asks for confirmation;
  the dashboard only accepts the change from its own page with that confirmation.
- The choice is saved permanently (`data\paper_capital.json`) as **pending**, and the paper bot applies it at
  its next start, before any trade. It waits while paper positions are open, and you can cancel it until then.
- Cash moves by the difference between the old and new opening balance; past trades, realised P&L and charges
  are never changed. Each change is recorded and shown on the account card; the Trades page's equity curve
  uses the opening balance in force.
- **Paper only:** live trading and the Zerodha account never read this setting. Backtest and advisor stay at
  ₹1,00,000. Strategy and risk rules are unchanged; trade sizes follow equity as before.
- 156 tests pass (14 new). Backtest unchanged (same 82 trades on the same data).

## v1.4.0 (16 Nov 2026): Paper Account | Zerodha Account dashboard views

A **View: Paper Account | Zerodha Account** switch on the Live and Trades pages. It only changes what the
dashboard shows: the bot's trading mode is still set only by its own settings, live trading is still off,
and switching never places an order. See the README section "Paper Account | Zerodha Account".

- **Paper Account card:** starting balance, total equity, available funds, invested amount, realised P&L
  (since the start and today), unrealised P&L, total P&L and charges, from the paper files only.
- **Zerodha Account card (read-only):** opening balance, account value, available funds, margin used,
  invested amount, today's realised/unrealised/total P&L and each position, from Kite's balance and
  positions calls only. A wrapper blocks every other Kite call, so the view can't trade. Real balances are
  kept in memory for 15 seconds and never written to a file or a log. Needs the day's Zerodha login.
- **Clear marking:** gold "PAPER ACCOUNT · virtual money" or red "LIVE · REAL ZERODHA ACCOUNT" bar and
  frame; the header always shows the mode the bot trades in; opening the Zerodha view asks for confirmation.
- **Kept separate:** each view reads only its own account's data, and the Trades page shows only that
  account's trades.
- **Refresh and warnings:** the card updates in place every 10 seconds and shows when it was last updated.
  It warns about stale data, prices more than 20 minutes behind, login problems (with the login button),
  network or Kite errors, and failed refreshes, which keep the last good values with their time.
- Strategy, risk limits and backtest are unchanged (same 82 trades on the same data). 142 tests pass
  (19 new), including checks that viewing either account changes no file, setting or environment variable.

## v1.3.1 (16 Nov 2026): internet drops and faster start-up

Fixes after the first scheduled paper-trading day, when the PC's Wi-Fi dropped several times between
9:20 and 9:45. Strategy, risk limits and backtest are unchanged (same 82 trades on the same data).

- **Dashboard rows recover after an internet drop.** A stock's row used to keep showing "data error" until
  the next 15-minute candle closed, even after prices were loading again, so a working bot looked stuck.
  Now it shows the last result again, or "data OK again at HH:MM - waiting for the next candle".
- **Outages are written to `bot.log`.** One warning when price data starts failing, with the reason in plain
  words (for example "can't look up the data server's address (internet or DNS down?)"), and one line when all
  stocks are back, with the start time and how long it lasted. Both also show in the dashboard's events.
- **Faster start:** the "is the bot already running?" check in `run_bot.ps1` used a Windows process query
  that took 1.5 to 3 minutes. The bot now holds a Windows mutex while it runs, checked in under a second, and a
  second copy refuses to start. The scheduled task now runs at normal priority (Task Scheduler's default is
  "below normal"). Start-up went from 2.5-6 minutes to 17 seconds.
  - **To get the normal priority**, run `register_task.ps1` once more (it updates the existing task).
- 123 tests pass (7 new).

## v1.3.0 (16 Nov 2026): learning advisor, Phases 1-3 (Guru Vashishtha)

The advisor only reports. It never changes a setting, and it can't trade. Later phases (proposals tested
on unseen data, which you approve) are not part of this version. See the README section
"Learning advisor".

- **Phase 1:** every trade now saves the conditions at its signal bar: signal time, opening range width,
  volume ratio, distance from VWAP, index change and opening gap. These are six new columns at the end of
  `backtest_trades.csv` and `trades_paper.csv` / `trades_live.csv`. An older paper or live log gets the new
  header automatically, and its old rows are kept. Recording only: the backtest gives exactly the same
  84 trades and numbers as before. 101 tests pass.
- **Phase 2:** candle archive. `archive.py` saves every finished trading day's 15-minute candles for the
  watchlist and its indexes to `data/candles/` (one file per day). The bot saves the day automatically after
  the close, while that day's Kite login still works. Kite data is preferred and is never replaced by Yahoo
  data. `archive.py --status` shows what is saved. The first run saved 247 trading days (17 Nov 2025 to
  13 Nov 2026) from Kite. Trading and the backtest are unchanged (same 84 trades). 110 tests pass.
  - Note: from 3 Aug 2026 Kite has no 15:15 candle for the stocks (NSE's closing auction session); the
    indexes still have it. The archive also keeps two Sunday special sessions (1 Feb Budget day and
    8 Nov Diwali Muhurat, 4 candles), which the weekly report will leave out.
- **Phase 3:** the weekly report, **Guru Vashishtha · Advisor**. `advisor.py report` backtests the current
  settings on the candle archive and shows the trades by entry time, direction, range width, breakout volume,
  weekday, stock and exit reason, each with trade count, win rate, average R, net ₹ and a 95% range. A rule
  check replays the same data with one rule switched off at a time. Paper and live trades are reported
  separately. It is read-only: it writes only to `data/advisor/` and never touches `backtest_trades.csv` or
  any setting. A full year takes about 10 minutes (`--no-rules` is faster). `backtest.py` gives the same
  84 trades as before. 116 tests pass.
  - First report (17 Nov 2025 to 13 Nov 2026, 331 trades): −16.7%, max drawdown 16.7%, profit factor 0.27,
    average −0.21R (95% range −0.26R to −0.16R, so clearly losing), and a loss in every one of the 13 months.
    Each filter reduced the loss; the VWAP filter never changed a trade.

## v1.2.0 (15 Nov 2026): Ramayana edition

- The bot is now **Ayodhya ORB**, themed on the Ramayana across the dashboard, console windows, log
  messages, launcher titles and setup scripts. Every themed name sits next to the plain term
  (e.g. **Lakshman Rekha · stop**, **Rajkosh · Trades**). See the glossary in the README.
- New dashboard look: a saffron and gold palette on a night-sky background, a serif title, a torana-style border,
  a bow icon in the browser tab, the Devanagari heading ॥ अयोध्या ॥, and footprints (👣) for the trade-chart (Yatra) page.
  Profit stays green and loss stays red.
- Log lines: 🏹 ENTER, 🏆 EXIT (win), 🛡 EXIT (loss), with themed exit reasons.
- All names are in one file, `theme.py`. Trade files, status data and the strategy and risk rules are **unchanged**.
- The Windows scheduled task keeps its name ("Kite ORB Bot"), so nothing needs re-registering.
- New README screenshots (no personal data). 96 tests pass, including 5 new theme tests.

## v1.1.1 (15 Nov 2026): Kite check fixes

- `6-CHECK-KITE-DATA.bat` now says whether the bot already uses Kite data, and otherwise shows the one
  PowerShell line that switches it on. It no longer tells you to edit `config.py`.
- Self-tests always use their own fake prices. With `DATA_SOURCE=kite` saved, 8 tests used to reach real
  Kite data and fail. 91 tests pass.

## v1.1.0 (15 Nov 2026): switching to a new Kite app

- **New `set_kite_keys.ps1`:** changes only the Kite API key and secret, for example after creating a paid
  Connect app. Input is hidden, and the values are saved in your Windows user environment variables, never in a file.
  Your proxy settings stay as they are, and the old app's login token is deleted.
- **The bot always uses the latest saved key.** A PowerShell window opened before a key change used to keep
  the old key. Now the saved Kite and proxy settings always win.
- **`DATA_SOURCE` can be saved for your Windows user** (`[Environment]::SetEnvironmentVariable("DATA_SOURCE", "kite", "User")`).
  A value set in the current window still overrides it.
- **Docs:** the static IP is registered once for the whole developer account (**Profile → IP Whitelist**), not for each app.
- No change to the strategy, the risk limits or the backtest. 91 tests pass.

## v1.0.0 (15 Nov 2026): first public version

- Opening range breakout bot for 12 large NSE stocks, intraday (MIS), **paper trading by default**.
  - Index filter, 1-hour time stop, 21 EMA trailing stop, square-off at 15:10.
  - Full Zerodha charges.
- Risk limits:
  - 0.5% risk per trade, at most 3 positions and 4 trades a day.
  - The 2% daily loss limit counts booked and open losses.
  - After 3 losses in a row, the bot takes 2 days off.
- Local dashboard at http://127.0.0.1:5000. Pages:
  - Live, with an equity curve and richer positions.
  - Session review, with the Time Machine.
  - Trades, with P&L charts, breakdowns, a 30-trade edge check and per-trade charts.
  - Swing scanner.
- Price data:
  - Yahoo by default.
  - Optional real-time Kite Connect data (paid plan, 500 credits a month), falling back to Yahoo automatically.
- NSE holiday calendar, a Windows Task Scheduler start at 09:00, and a backtest marked to market with fill tests.
- Live trading only with two deliberate switches, a static-IP proxy and the daily Zerodha login.
