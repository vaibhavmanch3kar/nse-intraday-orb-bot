# Versions

[← back to README](README.md)

Every version is on the [Releases page](https://github.com/vaibhavmanch3kar/nse-intraday-orb-bot/releases),
with a ZIP you can download. To go back to an older version, download that version's ZIP.
Your settings (Windows user environment variables) and your `data` folder are not part of the ZIP, so keep them.

## Unreleased: learning advisor, Phases 1-3

- **Phase 1:** every trade now saves the conditions at its signal bar: signal time, opening range width,
  volume ratio, distance from VWAP, index change and opening gap. These are six new columns at the end of
  `backtest_trades.csv` and `trades_paper.csv` / `trades_live.csv`. An older paper or live log gets the new
  header automatically, and its old rows are kept. Recording only: the backtest gives exactly the same
  84 trades and numbers as before. 101 tests pass.

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
