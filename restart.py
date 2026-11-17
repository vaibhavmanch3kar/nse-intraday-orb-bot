"""
"Apply Changes & Restart Bot": load code or settings that are already in the folder (it never pulls or merges).

Two ways, both chosen on the dashboard's Settings page and confirmed first:

- Apply After Market Close (the usual choice): nothing is restarted. The bot already ends at 15:35 and the
  09:00 start runs the new code. At the close the bot runs a trial check of the new code (preflight.py) so
  a problem shows that evening, not the next morning.
- Apply Now (when safe): the bot restarts itself, but only when it is safe:
    PAPER: no open position, not within 90 s after a 15-minute candle close. While waiting it takes no new
           entries (like Pause), so an open position can close and the restart follow.
    LIVE (real money): only before 09:15 or after the close, and with no open position.
  Otherwise the page says "Restart Pending - Waiting for Safe Conditions" and why.
  The bot then: checks the new code in a separate process (if that fails, the old bot keeps running and the
  error is shown) -> saves its journal and state -> exits with EXIT_RESTART -> run_bot.ps1 starts it again with
  the same environment -> the new bot checks itself within 90 s: one copy, new process, still the same mode,
  same open positions, trades today, cash and opening balance.

Requests are small files: data/restart_request.json (Apply Now) and data/apply_after_close.json, holding ONLY
a time and a random nonce; any other field makes the request invalid (it is refused and logged). They can't
carry a mode: nothing here can switch between PAPER and LIVE. Every request is logged in
data/logs/restart_requests.log. Balances, trade history, journals and settings are never touched.
"""

import datetime as dt
import json
import os
import re
import secrets

import config as C

EXIT_RESTART = 3
COME_BACK_SEC = 90
CANDLE_HOLD_SEC = 90
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
REQUEST_KEYS = {"requested_at", "nonce"}
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
ACTIVE_STAGES = ("waiting", "checking", "stopping", "restarting")


def _p(name):
    return os.path.join(C.DATA_DIR, name)


def request_path():
    return _p("restart_request.json")


def after_close_path():
    return _p("apply_after_close.json")


def state_path():
    return _p("restart_state.json")


def good_path():
    return _p("last_good_start.json")


def _now():
    return dt.datetime.now(IST)


def _write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


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
    """One line per request or outcome in data/logs/restart_requests.log."""
    try:
        os.makedirs(_p("logs"), exist_ok=True)
        with open(_p(os.path.join("logs", "restart_requests.log")), "a", encoding="utf-8") as f:
            f.write(f"{_now():%Y-%m-%d %H:%M:%S} {action}: {result}{(' - ' + detail) if detail else ''}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- requests (dashboard side)
def _new_request():
    return {"requested_at": _now().isoformat(timespec="seconds"), "nonce": secrets.token_hex(16)}


def busy():
    """A restart request or an Apply-Now restart in progress (a second request is refused: 409)."""
    st = state() or {}
    return os.path.exists(request_path()) or st.get("stage") in ACTIVE_STAGES


def request_now():
    """Apply Now (when safe). Returns (ok, message); ok False = refused (one is already pending)."""
    if busy():
        log("apply now", "refused", "a restart is already pending")
        return False, "A restart is already pending."
    req = _new_request()
    _write(request_path(), req)
    _write(state_path(), {"stage": "waiting", "at": req["requested_at"], "nonce": req["nonce"],
                          "reason": "waiting for the bot to pick up the request"})
    log("apply now", "requested", req["nonce"][:8])
    return True, "Restart requested: the bot restarts as soon as it is safe."


def request_after_close():
    if os.path.exists(after_close_path()):
        log("apply after close", "refused", "already requested")
        return False, "Already set to apply after the close."
    req = _new_request()
    _write(after_close_path(), req)
    log("apply after close", "requested", req["nonce"][:8])
    return True, "The changes apply at the next start; the bot checks them at today's close."


def cancel():
    a, b = _remove(request_path()), _remove(after_close_path())
    st = state() or {}
    if st.get("stage") == "waiting":
        _write(state_path(), dict(st, stage="cancelled", at=_now().isoformat(timespec="seconds")))
    log("cancel", "done" if (a or b) else "nothing pending")
    return a or b


def pending_request():
    return _read(request_path())


def after_close_requested():
    return _read(after_close_path())


def state():
    return _read(state_path())


def valid(req):
    """(ok, why): a request file must hold exactly a time and a nonce, nothing else."""
    if not isinstance(req, dict):
        return False, "not a request"
    if set(req) != REQUEST_KEYS:
        return False, f"unexpected fields ({', '.join(sorted(set(req) ^ REQUEST_KEYS))})"
    if not isinstance(req.get("nonce"), str) or not _NONCE_RE.match(req["nonce"]):
        return False, "bad nonce"
    try:
        dt.datetime.fromisoformat(req["requested_at"])
    except (TypeError, ValueError):
        return False, "bad time"
    return True, ""


# ---------------------------------------------------------------- safety (bot side)
def is_safe(live, open_positions, now, market_open, market_close, candle_hold=0.0):
    """(ok, why) for Apply Now. Pending orders: the bot keeps none between its checks (entries are filled or
    cancelled inside a check), so with no open position there is no order to look after either."""
    if open_positions:
        return False, f"{open_positions} open position{'s' if open_positions > 1 else ''}"
    if live:
        if market_open <= now < market_close:
            return False, "real money: only before 09:15 or after the close"
        return True, "real money, market closed, no open position"
    if candle_hold > 0:
        return False, f"the bot is fetching the new candle ({int(candle_hold)} s)"
    return True, "no open position" if now >= market_open else "before the market opens"


def set_stage(stage, **kw):
    st = state() or {}
    st.update(stage=stage, at=_now().isoformat(timespec="seconds"), **kw)
    _write(state_path(), st)
    return st


def take_request():
    """The bot reads the Apply-Now request: (req, None), (None, None) if none, or (None, why) if invalid (the
    file is then removed, the refusal shown and logged)."""
    raw = _read(request_path())
    if raw is None:
        if os.path.exists(request_path()):
            _remove(request_path())
            set_stage("failed", reason="request refused: unreadable file")
            log("apply now", "refused by the bot", "unreadable file")
            return None, "unreadable file"
        return None, None
    ok, why = valid(raw)
    if not ok:
        _remove(request_path())
        set_stage("failed", reason=f"request refused: {why}")
        log("apply now", "refused by the bot", why)
        return None, why
    return raw, None


def begin(req, mode, positions, trades_today, cash, balance, pid, version, now=None):
    """Record what the bot had, just before it exits to restart."""
    _write(state_path(), {"stage": "restarting", "at": (now or _now()).isoformat(timespec="seconds"),
                          "nonce": req.get("nonce") if req else None,
                          "before": {"mode": mode, "positions": sorted(positions), "trades_today": trades_today,
                                     "cash": round(float(cash), 2), "balance": round(float(balance), 2),
                                     "pid": pid, "version": version}})
    _remove(request_path())
    log("apply now", "restarting", f"version before {version}")


def verify(mode, positions, trades_today, cash, balance, pid, version, disk_version=None, now=None):
    """Called by the new bot right after its first status write. Returns the checks, or None if this start
    was not a restart."""
    st = state()
    if not st or st.get("stage") != "restarting":
        return None
    now = now or _now()
    before = st.get("before") or {}
    try:
        took = (now - dt.datetime.fromisoformat(st["at"])).total_seconds()
    except (KeyError, ValueError):
        took = None
    checks = {
        "one copy running": True,                       # this bot holds the single-instance lock
        "new process": pid != before.get("pid"),
        f"back within {COME_BACK_SEC} s": took is not None and took <= COME_BACK_SEC,
        "running the code in the folder": disk_version is None or version == disk_version,
        "same mode": mode == before.get("mode"),
        "same open positions": sorted(positions) == before.get("positions"),
        "same trades today": trades_today == before.get("trades_today"),
        "same cash": round(float(cash), 2) == before.get("cash"),
        "same opening balance": round(float(balance), 2) == before.get("balance"),
    }
    ok = all(checks.values())
    _write(state_path(), {"stage": "running" if ok else "failed", "ok": ok, "checks": checks,
                          "at": now.isoformat(timespec="seconds"), "took_sec": took, "before": before,
                          "version": version,
                          "reason": "" if ok else "checks failed: " + ", ".join(k for k, v in checks.items() if not v)})
    log("apply now", "verified" if ok else "CHECKS FAILED", f"version {version}, {took} s")
    return checks


def record_good_start(version, commit, now=None):
    """At each successful start: remember the version, so "Go back to the previous version" knows where to go."""
    g = _read(good_path()) or {"starts": []}
    starts = [s for s in g.get("starts", []) if s.get("commit") != commit]
    starts.append({"version": version, "commit": commit, "at": (now or _now()).isoformat(timespec="seconds")})
    _write(good_path(), {"starts": starts[-10:]})


def previous_good(current_commit):
    """The last version that started successfully before the one now in the folder, or None."""
    starts = (_read(good_path()) or {}).get("starts", [])
    for s in reversed(starts):
        if s.get("commit") and s.get("commit") != current_commit:
            return s
    return None


# ---------------------------------------------------------------- dashboard banner
def problem(status_time=None, now=None):
    """A red-banner message if the last Apply-Now went wrong (or the bot never came back), else None."""
    st = state()
    if not st:
        return None
    now = now or _now()
    stage = st.get("stage")
    if stage == "restarting":
        try:
            at = dt.datetime.fromisoformat(st["at"])
        except (KeyError, ValueError):
            return None
        back = status_time is not None and status_time > at
        if not back and (now - at).total_seconds() > COME_BACK_SEC:
            return (f"The bot stopped at {at:%H:%M} to restart and has not come back within {COME_BACK_SEC} s.")
        return None
    if stage == "failed":
        return st.get("reason") or "the restart failed"
    return None
