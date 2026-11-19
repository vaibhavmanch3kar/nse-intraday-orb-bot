"""
Settings for the Kite NSE intraday opening-range-breakout bot.

Every value can be overridden with an environment variable of the same name,
e.g.  $env:RISK_PER_TRADE_PCT = "0.25"
"""

import os
import sys

# Settings the setup scripts store in the Windows *user* environment.
# Secrets: the saved value always wins, so a terminal opened before a key change can't use an old key.
_SECRET_VARS = ("TRUEIP_HOST", "TRUEIP_PORT", "TRUEIP_USER", "TRUEIP_PASS",
                "EXPECTED_EGRESS_IP", "KITE_API_KEY", "KITE_API_SECRET")
# Preferences: only filled in when not set for this window, so `$env:DATA_SOURCE = ...` still overrides.
_PREF_VARS = ("DATA_SOURCE",)


def load_user_env():
    """Read the saved user settings from the registry (terminals don't see changes made after they opened)."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for name in _SECRET_VARS + _PREF_VARS:
                if name in _PREF_VARS and os.environ.get(name):
                    continue
                try:
                    os.environ[name] = winreg.QueryValueEx(k, name)[0]
                except FileNotFoundError:
                    pass
    except OSError:
        pass
    load_secret_store()


def load_secret_store():
    """A secret that is NOT in the Windows settings is taken from the protected store (secretbox.py), if it was
    moved there with secure_secrets.py. A value in the Windows settings always wins. Never raises."""
    try:
        import secretbox
        for name in secretbox.names():
            if name in _SECRET_VARS and not os.environ.get(name):
                value = secretbox.get(name)
                if value:
                    os.environ[name] = value
    except Exception:  # noqa: BLE001 - a store problem must look like "key not set", never stop the program
        pass


load_user_env()

# Windows consoles default to cp1252, which can't print ₹
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _env(name, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")
    if cast is list:
        return [x.strip().upper() for x in raw.split(",") if x.strip()]
    return cast(raw)


# ── Mode ───────────────────────────────────────────────────────────────────
# Paper trading is the default. Live trading needs BOTH DRY_RUN=false and CONFIRM_LIVE=YES, AND the real-money
# arming done on the dashboard that same day (livemode.py); without the arming the bot trades paper.
DRY_RUN = _env("DRY_RUN", True, bool)
CONFIRM_LIVE = _env("CONFIRM_LIVE", "")
PAPER_START_INR = _env("PAPER_START_INR", 100000.0, float)
# Readiness check only (Live Trading panel): the Zerodha equity balance needed before real money can be armed.
LIVE_MIN_FUNDS_INR = _env("LIVE_MIN_FUNDS_INR", 10000.0, float)

# ── Markets ────────────────────────────────────────────────────────────────
# Nifty 50 constituents (Nov 2026). Kite trading symbols = Yahoo symbols without ".NS".
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV",
    "BAJFINANCE", "BEL", "BHARTIARTL", "BSE", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT",
    "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS", "TECHM", "TITAN", "TMPV", "TRENT", "ULTRACEMCO",
]
# Stocks the bot TRADES: 12 liquid large caps. (Trading all 50 backtested worse: -7.9% vs -2.3%.)
# To trade the Nifty 50 instead: $env:WATCHLIST = (the NIFTY50 list, comma-separated)
WATCHLIST = _env("WATCHLIST", [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN",
    "AXISBANK", "KOTAKBANK", "LT", "ITC", "BHARTIARTL", "TATASTEEL",
], list)
# Stocks the swing SCANNER checks (research only).
SCAN_LIST = _env("SCAN_LIST", NIFTY50, list)
FETCH_WORKERS = _env("FETCH_WORKERS", 6, int)       # parallel Yahoo requests
INTERVAL_MIN = 15                                   # candle size used by the strategy

# ── Price data ─────────────────────────────────────────────────────────────
# "yahoo": free, no login, can lag a few minutes, ~60 days of history.
# "kite":  Kite Connect paid plan - real-time, long history; needs the daily Zerodha login.
#          Check it first: .venv\Scripts\python.exe data_kite.py check
#          Falls back to Yahoo automatically if Kite isn't usable that moment.
DATA_SOURCE = _env("DATA_SOURCE", "yahoo")

# ── Session times (IST, HH:MM) ─────────────────────────────────────────────
MARKET_OPEN = _env("MARKET_OPEN", "09:15")
OR_MINUTES = _env("OR_MINUTES", 15, int)            # opening range = first N minutes
LAST_ENTRY = _env("LAST_ENTRY", "12:00")            # no new entries on bars starting after this
SQUARE_OFF = _env("SQUARE_OFF", "15:00")            # close everything. Zerodha force-closes MIS in F&O stocks at
                                                    # 15:12 (Rs 50 + GST per order), so 15:10 left only 2 minutes
MARKET_CLOSE = _env("MARKET_CLOSE", "15:30")

# ── Strategy: opening range breakout ───────────────────────────────────────
ALLOW_SHORT = _env("ALLOW_SHORT", True, bool)       # MIS allows intraday shorts
MIN_OR_PCT = _env("MIN_OR_PCT", 0.25, float)        # skip if the range is too tight (noise)...
MAX_OR_PCT = _env("MAX_OR_PCT", 1.50, float)        # ...or too wide (stop too far)
VOLUME_MULT = _env("VOLUME_MULT", 1.2, float)       # breakout bar volume vs same-slot avg
VOLUME_LOOKBACK_DAYS = _env("VOLUME_LOOKBACK_DAYS", 5, int)
USE_VWAP_FILTER = _env("USE_VWAP_FILTER", True, bool)  # long above VWAP, short below
STOP_RULE = _env("STOP_RULE", "range")              # "range" = other side of the OR; "mid" = OR midpoint (Day Trader skill default)
TARGET_R = _env("TARGET_R", 0.0, float)             # 0 = no fixed target (trail instead); e.g. 2 = entry ± 2 × risk
MAX_CHASE_R = _env("MAX_CHASE_R", 0.3, float)       # skip if price already ran > 0.3R past the signal close
#   TARGET_R = 0 removes the fixed target (let winners run to the trail / square-off)

# ── Rules from TWS risk-management videos ──────────────────────────────────
# Default since 2026-11-14 (user's choice): index filter + 1h time stop + 21 EMA trail, no fixed target.
# Original rules: TARGET_R=2 TRAIL_EMA=0 TIME_STOP_BARS=0 INDEX_FILTER=false
TRAIL_EMA = _env("TRAIL_EMA", 21, int)              # exit when a 15m close crosses the EMA (0 = off)
TIME_STOP_BARS = _env("TIME_STOP_BARS", 4, int)     # 4 bars = 1 hour: exit if trade hasn't reached... (0 = off)
TIME_STOP_MIN_R = _env("TIME_STOP_MIN_R", 0.5, float)  # ...this much profit (in R) by then
INDEX_FILTER = _env("INDEX_FILTER", True, bool)     # long only if the stock's index is above its day open
INDEX_DEFAULT = "^NSEI"
SECTOR_INDEX = {                                    # correlation: banks follow Bank NIFTY, IT follows NIFTY IT
    "HDFCBANK": "^NSEBANK", "ICICIBANK": "^NSEBANK", "SBIN": "^NSEBANK", "AXISBANK": "^NSEBANK",
    "KOTAKBANK": "^NSEBANK", "INFY": "^CNXIT", "TCS": "^CNXIT", "HCLTECH": "^CNXIT", "TECHM": "^CNXIT",
}
VIX_SIZING = _env("VIX_SIZING", False, bool)        # scale risk by previous day's India VIX
VIX_BANDS = [(15.0, 1.0), (20.0, 0.75), (999.0, 0.5)]  # VIX below 15 -> full risk, 15-20 -> 75%, above 20 -> 50%

# ── Costs (Zerodha equity intraday; check zerodha.com/charges) ─────────────
BROKERAGE_PCT = _env("BROKERAGE_PCT", 0.03, float)  # per order, capped at ₹20
BROKERAGE_CAP = _env("BROKERAGE_CAP", 20.0, float)
STT_SELL_PCT = _env("STT_SELL_PCT", 0.025, float)
EXCH_TXN_PCT = _env("EXCH_TXN_PCT", 0.00297, float)  # NSE
SEBI_PCT = _env("SEBI_PCT", 0.0001, float)          # ₹10 per crore
STAMP_BUY_PCT = _env("STAMP_BUY_PCT", 0.003, float)
GST_PCT = _env("GST_PCT", 18.0, float)              # on brokerage + exchange + SEBI
SLIPPAGE_PCT = _env("SLIPPAGE_PCT", 0.05, float)    # assumed adverse fill per side

# ── Risk rules ─────────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT = _env("RISK_PER_TRADE_PCT", 0.5, float)   # % of equity lost if stop hits (costs incl.)
MAX_POSITION_PCT = _env("MAX_POSITION_PCT", 25.0, float)      # max notional per stock, % of equity
MAX_EXPOSURE_PCT = _env("MAX_EXPOSURE_PCT", 75.0, float)      # max notional all stocks (no leverage)
MAX_OPEN_POSITIONS = _env("MAX_OPEN_POSITIONS", 3, int)
# Kavach (kavach.py), added in 2.0. Each can only make the bot stricter than it was.
MAX_SIGNAL_AGE_MIN = _env("MAX_SIGNAL_AGE_MIN", 5.0, float)   # a signal older than this is stale: skip (0 = off)
MAX_PER_SECTOR = _env("MAX_PER_SECTOR", 0, int)               # open positions per sector and direction (0 = off)
MAX_BAR_VOLUME_PCT = _env("MAX_BAR_VOLUME_PCT", 2.0, float)   # one order <= this % of the signal candle's volume
MAX_TRADES_PER_DAY = _env("MAX_TRADES_PER_DAY", 4, int)
MAX_DAILY_LOSS_PCT = _env("MAX_DAILY_LOSS_PCT", 2.0, float)
LOSS_STREAK_LIMIT = _env("LOSS_STREAK_LIMIT", 3, int)
COOLDOWN_DAYS = _env("COOLDOWN_DAYS", 2, int)
MIN_QTY = 1

# ── Live order handling ────────────────────────────────────────────────────
LIMIT_BUFFER_PCT = _env("LIMIT_BUFFER_PCT", 0.20, float)      # marketable limit offset
ORDER_FILL_TIMEOUT_SEC = _env("ORDER_FILL_TIMEOUT_SEC", 30, int)
EXIT_RETRIES = _env("EXIT_RETRIES", 5, int)
CANCEL_WAIT_SEC = _env("CANCEL_WAIT_SEC", 10, int)            # wait this long for Zerodha to confirm a cancel
MARGIN_CHECK = _env("MARGIN_CHECK", True, bool)               # ask Zerodha for the margin before each entry
RECON_EVERY_SEC = _env("RECON_EVERY_SEC", 300, int)           # compare positions with Zerodha this often
# Zerodha refuses a price that is not a multiple of the stock's own price step (tick). NSE sets it per stock
# (0.01, 0.05, 0.10, 0.50, ...): real orders use each stock's step from Kite's instrument list (broker.TickSizes).
# When that list can't be read, no entry is sent, and stops and exits round to FALLBACK_TICK, which is valid for
# every stock with a step of 0.10 or less.
FALLBACK_TICK = 0.10

# The PC's clock is compared with Zerodha's server every CLOCK_CHECK_SEC seconds (0 = never). If it is more
# than CLOCK_MAX_SKEW_SEC off, no new entry is taken until it is right again (see clockcheck.py).
CLOCK_CHECK_SEC = _env("CLOCK_CHECK_SEC", 300, int)
CLOCK_MAX_SKEW_SEC = _env("CLOCK_MAX_SKEW_SEC", 30, int)

# ── Loop / files ───────────────────────────────────────────────────────────
POLL_SEC = _env("POLL_SEC", 60, int)
DATA_DIR = _env("DATA_DIR", "data")
STOP_FILE = _env("STOP_FILE", "STOP")
# Daily backup of the data folder after the close. Empty = KiteConnect\backups\data (next to the project)
# (same disk: point BACKUP_DIR at OneDrive / a USB drive for protection against a disk failure).
BACKUP_DIR = _env("BACKUP_DIR", "")
BACKUP_KEEP = _env("BACKUP_KEEP", 30, int)                    # newest daily backups kept
PAUSE_FILE = os.path.join(DATA_DIR, "PAUSE")
WEB_HOST = "127.0.0.1"                                        # local only, never expose
WEB_PORT = _env("WEB_PORT", 5000, int)                        # must match the Kite redirect URL


def is_live() -> bool:
    return (not DRY_RUN) and CONFIRM_LIVE == "YES"
