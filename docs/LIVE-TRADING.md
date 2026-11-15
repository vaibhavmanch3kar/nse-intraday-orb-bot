# Real-money trading with Zerodha (advanced)

[← back to README](../README.md)

> ⚠️ **Don't do this until you have paper-traded for several weeks and understand the results.**
> The strategy lost money in testing. Real orders can lose real money, including more than the
> stop if the price gaps. You are responsible for every order.

The bot stays in **paper mode** unless you do *all* of the steps below. If any one is missing, it
trades pretend money.

## What you need

1. **A Zerodha account with the NSE equity segment active.** If it's dormant (no trades for 2 years),
   reactivate it from Zerodha first.
2. **A Kite Connect developer app** at https://developers.kite.trade/apps:
   - **Personal** (free) is enough to place orders. This bot gets its prices from Yahoo, so it doesn't need Kite's paid data.
   - **Connect** (₹500/month = 500 credits for 30 days) adds real-time prices and history. See
     [KITE-CONNECT-PLAN.md](KITE-CONNECT-PLAN.md) for adding credits, step by step with screenshots.
   - **Zerodha Client ID**: the account holder's user ID (e.g. `AB1234`).
   - **Redirect URL**: `http://127.0.0.1:5000/kite/callback` (exactly this).
   - **Postback URL**: leave empty.
   - After creating it, note the **API key** and **API secret**. Never share them or put them in a file.
3. **A static IP.** SEBI rules require API orders to come from an IP registered on your Kite app.
   Home internet usually has a changing IP, so this project sends Kite traffic through a
   **static-IP proxy** (for example TrueIP). Get one, and register its IP in your Kite app's settings.

## One-time setup

First do the normal paper setup (`1-SETUP.bat`) from the README. Then run this in a **normal**
PowerShell window (not "Run as administrator"), inside the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_kite_proxy.ps1
```

It asks for:
- the proxy IP, port and username;
- the static IP whitelisted on Kite (press Enter to use the proxy IP);
- the proxy password, Kite API key and API secret (typed hidden; right-click pastes).

They are saved in your **Windows user environment variables**, not in any file. Then it checks
that traffic really leaves from the static IP, and walks you through the first login.
Run it again with `-ResetSecrets` to change anything.

## Every trading day

1. Open PowerShell in the project folder and switch on **both** live switches (they last only for this window):
   ```powershell
   $env:DRY_RUN = "false"; $env:CONFIRM_LIVE = "YES"
   powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
   ```
2. On the dashboard, click **Log in to Zerodha** and log in. Zerodha sends you back to the dashboard and
   the day's token is saved automatically. Tokens expire around 6:00 AM, so log in every day.
3. Start small, for example with `$env:WATCHLIST = "SBIN,INFY"` and `$env:RISK_PER_TRADE_PCT = "0.25"`
   set before starting.

The scheduled task (`4-SCHEDULE.bat`) **always runs in paper mode**, because it doesn't set the switches.

## What the bot does with real orders

- **Entry:** a marketable LIMIT order (±0.2%). It is cancelled after 30 seconds if not filled; partial fills are kept.
- **Stop:** an SL order at the exchange for the filled quantity, so the stop works even if your PC or the bot crashes.
  If the stop order is rejected, the position is closed immediately.
- **Exit (trail, time stop, square-off):** cancel the SL, then exit with limit orders (5 retries, wider each time).
- On startup it ignores positions Kite doesn't show as open MIS. It never touches holdings it didn't open.
- Prices come from Yahoo and can lag. In a fast market, fills can be worse than the backtest assumes.

## Safety checklist

- [ ] Weeks of paper trading reviewed on the Trades and Session review pages
- [ ] Zerodha account active, NSE segment enabled
- [ ] Kite app redirect URL is `http://127.0.0.1:5000/kite/callback`, static IP registered
- [ ] `setup_kite_proxy.ps1` check passed (egress IP matches)
- [ ] Small watchlist and small risk per trade for the first weeks
- [ ] PC stays on and awake 9:00–15:35, and you can watch it
