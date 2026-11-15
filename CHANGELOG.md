# Versions

[← back to README](README.md)

Every version is on the [Releases page](https://github.com/vaibhavmanch3kar/nse-intraday-orb-bot/releases),
with a ZIP you can download. To go back to an older version, download that version's ZIP.
Your settings (Windows user environment variables) and your `data` folder are not part of the ZIP, so keep them.

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
