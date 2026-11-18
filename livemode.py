"""Paper | Real Money: when the bot may trade real money, and how the dashboard asks it to switch.

Real money needs ALL of these, every day:
  1. DRY_RUN=false and CONFIRM_LIVE=YES in the bot's own environment. You set them in Windows yourself; nothing
     in this program can set them (config.is_live() reads them, unchanged).
  2. Today's arming (data/live_arm.json). Only the dashboard writes it, and only after every readiness check
     passed, a Zerodha login in the last LOGIN_FRESH_MIN minutes and the typed words REAL MONEY. It holds a date, a
     time and a random code, nothing that can change a setting, and it expires at the end of the day.
Without the arming the bot trades PAPER whatever the settings say, including the 9:00 scheduled task.

Switching back to paper: the dashboard deletes the arming at once and leaves data/switch_to_paper.json. A live bot
then opens no new trades, keeps managing the open ones (stop orders, trailing stop, the 15:10 close) and restarts in
paper only when all of them have closed. "Close all positions now" (data/close_all.json) is a separate request.

Nothing here places an order or talks to Zerodha; readiness() only runs the read-only probes it is given.
"""

import datetime as dt
import json
import os
import re
import secrets

import config as C
import safefile

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CONFIRM_WORDS = "REAL MONEY"
LOGIN_FRESH_MIN = 10
ARM_KEYS = {"date", "armed_at", "login_at", "nonce"}
REQUEST_KEYS = {"requested_at", "nonce"}
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
# The strategy audit of 17 Nov 2026 (audit/AUDIT_REPORT.md): shown as a warning next to the Real Money switch.
AUDIT_VERDICT = ("Strategy audit, 17 Nov 2026: NO-GO. The full-year backtest loses money after costs; "
                 "the audit advises against trading this strategy with real money.")


def _p(name):
    return os.path.join(C.DATA_DIR, name)


def arm_path():
    return _p("live_arm.json")


def to_paper_path():
    return _p("switch_to_paper.json")


def close_all_path():
    return _p("close_all.json")


def _now():
    return dt.datetime.now(IST)


def _write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    safefile.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _remove(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def log(action, result, detail=""):
    """One line per request or outcome in data/logs/mode_changes.log (never a token or a secret)."""
    try:
        os.makedirs(_p("logs"), exist_ok=True)
        with open(_p(os.path.join("logs", "mode_changes.log")), "a", encoding="utf-8") as f:
            f.write(f"{_now():%Y-%m-%d %H:%M:%S} {action}: {result}{(' - ' + detail) if detail else ''}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- arming (today only)
def arm_valid(now=None):
    """(ok, why): is there a well-formed arming for today?"""
    now = now or _now()
    raw = _read(arm_path())
    if raw is None:
        return False, "not armed today" if not os.path.exists(arm_path()) else "unreadable arming file"
    if not isinstance(raw, dict) or set(raw) != ARM_KEYS:
        return False, "arming file has unexpected fields"
    if not isinstance(raw.get("nonce"), str) or not _NONCE_RE.match(raw["nonce"]):
        return False, "arming file has a bad code"
    try:
        armed = dt.datetime.fromisoformat(raw["armed_at"])
    except (TypeError, ValueError):
        return False, "arming file has a bad time"
    if raw.get("date") != now.date().isoformat() or armed.astimezone(IST).date() != now.date():
        return False, f"the arming was for {raw.get('date')}, not today"
    if armed > now + dt.timedelta(minutes=1):
        return False, "the arming time is in the future"
    return True, ""


def arm(login_at, now=None):
    """Written by the dashboard only after its own checks (see web.arm_real_money)."""
    now = now or _now()
    _remove(to_paper_path())
    _write(arm_path(), {"date": now.date().isoformat(), "armed_at": now.isoformat(timespec="seconds"),
                        "login_at": login_at.isoformat(timespec="seconds"), "nonce": secrets.token_hex(16)})
    log("real money", "ARMED for today", f"Zerodha login at {login_at:%H:%M}")


def disarm(why):
    if _remove(arm_path()):
        log("real money", "disarmed", why)
        return True
    return False


def bot_mode(env_live, now=None):
    """(live, note) for a starting bot. env_live is config.is_live() (both Windows settings). Real money only
    with both settings AND today's arming; otherwise paper, with a note saying why when the settings were on."""
    if not env_live:
        return False, None
    ok, why = arm_valid(now)
    if ok:
        return True, None
    return False, (f"DRY_RUN=false and CONFIRM_LIVE=YES are set, but real money was not armed today on the dashboard "
                   f"({why}) - trading PAPER")


# ---------------------------------------------------------------- switch back to paper / close all
def _new_request(now=None):
    return {"requested_at": (now or _now()).isoformat(timespec="seconds"), "nonce": secrets.token_hex(16)}


def _valid_request(raw):
    if not isinstance(raw, dict) or set(raw) != REQUEST_KEYS:
        return False
    if not isinstance(raw.get("nonce"), str) or not _NONCE_RE.match(raw["nonce"]):
        return False
    try:
        dt.datetime.fromisoformat(raw["requested_at"])
    except (TypeError, ValueError):
        return False
    return True


def request_paper(now=None):
    """Switch back to paper: the arming goes at once, so any later start is paper."""
    disarm("switch back to paper requested")
    _write(to_paper_path(), _new_request(now))
    log("switch to paper", "requested")


def paper_requested():
    raw = _read(to_paper_path())
    if raw is None:
        return False
    if not _valid_request(raw):
        _remove(to_paper_path())
        log("switch to paper", "refused", "malformed request file")
        return False
    return True


def clear_paper_request():
    _remove(to_paper_path())


def request_close_all(now=None):
    _write(close_all_path(), _new_request(now))
    log("close all positions", "requested")


CLOSE_ALL_FRESH_MIN = 10


def take_close_all(now=None):
    """The bot takes the request (once). True only for a well-formed request from the last CLOSE_ALL_FRESH_MIN
    minutes: an old one (made after the close, say) is dropped, so it can never close the next day's trades."""
    raw = _read(close_all_path())
    if raw is None:
        return False
    _remove(close_all_path())
    if not _valid_request(raw):
        log("close all positions", "refused", "malformed request file")
        return False
    age = ((now or _now()) - dt.datetime.fromisoformat(raw["requested_at"])).total_seconds()
    if not 0 <= age <= CLOSE_ALL_FRESH_MIN * 60 + 60:
        log("close all positions", "dropped", f"request too old ({int(age // 60)} min)")
        return False
    log("close all positions", "taken by the bot")
    return True


def switch_message(s, short=False):
    """A mode change waiting, from the bot's status, in plain words. short: the header badge's form (the full
    sentence is its tooltip and is on Settings > Trading mode)."""
    s = s or {}
    sw = s.get("switch")
    n = len(s.get("positions") or [])
    if sw == "to_paper":
        if short:
            return f"→ PAPER when {n} close{'s' if n == 1 else ''}" if n else "→ PAPER now"
        return (f"Switching to PAPER when {n} position{'s' if n != 1 else ''} close{'s' if n == 1 else ''}"
                if n else "Switching to PAPER now")
    if sw == "to_live":
        return "→ REAL MONEY soon" if short else "Switching to REAL MONEY at the next safe moment"
    return ""


# ---------------------------------------------------------------- readiness (read-only)
def windows_setting(name):
    """The value saved in Windows for this user (what the 9:00 task will start with), else this process's own."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return ""
    return os.environ.get(name, "")


def readiness(probe):
    """The Live Trading checks, in order. probe is a dict of read-only answers (see web.readiness_probe):
    settings (name -> value saved in Windows), logged_in, account_ok/err, egress_ok/err, funds/funds_err.
    Returns a list of (name, ok, detail, how_to_fix)."""
    st = probe.get("settings", {})
    on = lambda n, v: (st.get(n) or "").strip().lower() == v.lower()  # noqa: E731
    checks = []
    saved = on("DRY_RUN", "false") and on("CONFIRM_LIVE", "YES")
    live_set = saved or bool(probe.get("bot_env_live"))
    checks.append(("Live settings on", live_set,
                   ("DRY_RUN=false and CONFIRM_LIVE=YES are saved in Windows" if saved else
                    "DRY_RUN=false and CONFIRM_LIVE=YES are set for the running bot" if live_set else
                    "not set (the bot trades paper)"),
                   "Set them yourself in Windows: Start > 'Edit environment variables for your account' > New: "
                   "DRY_RUN = false and CONFIRM_LIVE = YES. The dashboard can never set them."))
    active = on("KITE_ACCOUNT_ACTIVE", "YES")
    checks.append(("Zerodha account active", active and probe.get("account_ok", False),
                   ("you confirmed it and Zerodha answers" if active and probe.get("account_ok") else
                    probe.get("account_err") or "not confirmed: the account was dormant"),
                   "Reactivate the account with Zerodha (re-KYC), then set KITE_ACCOUNT_ACTIVE = YES in Windows."))
    perm = on("KITE_ORDER_PERMISSION", "YES")
    checks.append(("Kite Connect app can place orders", perm,
                   "you confirmed the paid app has order permission" if perm else "not confirmed",
                   "Subscribe to the paid Kite Connect plan at developers.kite.trade, then set "
                   "KITE_ORDER_PERMISSION = YES in Windows. Zerodha has no read-only way to check this."))
    checks.append(("Static-IP proxy verified", bool(probe.get("egress_ok")),
                   "Kite traffic leaves from the whitelisted IP" if probe.get("egress_ok") else
                   probe.get("egress_err") or "not checked",
                   "Run setup_kite_proxy.ps1 and whitelist the proxy IP in the Kite developer console."))
    logged = bool(probe.get("logged_in"))
    checks.append(("Logged in to Zerodha today", logged, "yes" if logged else "no",
                   "Use 'Log in with Zerodha' on the dashboard."))
    funds, need = probe.get("funds"), C.LIVE_MIN_FUNDS_INR
    enough = isinstance(funds, (int, float)) and funds >= need
    checks.append(("Enough funds", enough,
                   (f"₹{funds:,.0f} available (needs ₹{need:,.0f})" if isinstance(funds, (int, float)) else
                    probe.get("funds_err") or "not checked"),
                   f"Add funds in Zerodha Console (at least ₹{need:,.0f}, the LIVE_MIN_FUNDS_INR setting)."))
    return checks


def all_ready(checks):
    return bool(checks) and all(ok for _, ok, _, _ in checks)
