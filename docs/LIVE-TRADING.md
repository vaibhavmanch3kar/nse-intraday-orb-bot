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
   **static-IP proxy** (for example TrueIP). Get one, and add its IP on the developer site under
   **Profile → IP Whitelist** (account-wide, covers all your apps).

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

Real money needs **three things, every day**: both live switches, today's Zerodha login, and the day's
**arming** on the dashboard. Without the arming the bot trades paper, however it was started: by hand, by a
restart, or by the scheduled task (`4-SCHEDULE.bat`). The arming lasts only until midnight.

**Once, before the first live day,** set these yourself in Windows (Start > "Edit environment variables for
your account" > New). The dashboard can never set them:
- `KITE_ACCOUNT_ACTIVE` = `YES`, after Zerodha has reactivated your account (it was dormant).
- `KITE_ORDER_PERMISSION` = `YES`, after you subscribed to the paid Kite Connect plan, whose app can place
  orders. Zerodha has no read-only way to check either of these, so you confirm them.
- Optional: `LIVE_MIN_FUNDS_INR` (default 10000), the Zerodha balance needed before you can arm.

**Every trading day:**
1. Start the bot with **both** live switches on. For one window only:
   ```powershell
   $env:DRY_RUN = "false"; $env:CONFIRM_LIVE = "YES"
   powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
   ```
   Or save both in Windows so the 9:00 task has them; it still trades paper on any day you don't arm.
   Start small, for example with `$env:WATCHLIST = "SBIN,INFY"` and `$env:RISK_PER_TRADE_PCT = "0.25"`.
2. On the dashboard, click **Log in to Zerodha** and log in (Zerodha's own two-step login). The day's token is
   saved automatically and expires around 6:00 AM.
3. Within 10 minutes of that login, open **Settings > Trading mode**:
   - Click **Check readiness**. It checks, read-only: the two switches, your account and order-permission
     confirmations, that Kite traffic leaves from the whitelisted static IP, today's login and your funds.
     If anything fails the panel shows **Live Trading Locked**, what's missing and how to fix it.
   - When everything passes it shows your risk per trade and daily loss limit in rupees. Type `REAL MONEY` and
     click **Arm REAL MONEY for today**.
   - The bot switches at the next safe moment: with no open paper position, and not while it fetches a new
     candle. Until then it opens no new paper trades. The header shows **REAL MONEY** once it has switched.

**Back to paper** (Settings > Trading mode > **Switch back to Paper**): the arming is removed at once and the bot
opens no new real-money trades. It keeps managing the open ones (exchange stop orders, trailing stop, 15:10
close) and switches to paper when all of them have closed. The header says "→ PAPER when 2 close" meanwhile.
**Close all positions now** is a separate button with its own confirmation, for when you want out at once.

**Log out of Zerodha** (Live page) is refused while the bot holds real-money positions, because it needs the
login to manage and close them. Every mode change, arming and refusal is logged in `data/logs/mode_changes.log`.

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
