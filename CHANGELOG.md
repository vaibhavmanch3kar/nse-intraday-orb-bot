# Versions

[← back to README](README.md)

Every version is on the [Releases page](https://github.com/vaibhavmanch3kar/nse-intraday-orb-bot/releases),
with a ZIP you can download. To go back to an older version, download that version's ZIP.
Your settings (Windows user environment variables) and your `data` folder are not part of the ZIP, so keep them.

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
