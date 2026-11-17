"""
"Restart the bot when safe": how the dashboard asks, when the bot agrees, and how the restart is checked.

1. The dashboard writes data/RESTART_REQUEST (after you confirm). It never stops the bot itself.
2. The bot looks at the request on every loop and restarts only when it is safe:
     - PAPER mode only. In LIVE mode a restart is never automatic: the request is dropped and the
       dashboard says to restart by hand when flat;
     - before the market opens (09:15), or with no open position.
   Otherwise it waits (and says so) until it is safe. It then records what it had (mode, positions,
   trades today) in data/restart_state.json and exits with EXIT_RESTART.
3. run_bot.ps1 sees EXIT_RESTART and starts the bot again (at most 3 times in a row).
4. The new bot checks: one copy running (it holds the single-instance lock), status written, back within
   60 s, same mode (still PAPER), same open positions, same trade count. The result is in
   restart_state.json and on the dashboard (a red banner if anything is off, or if the bot never came back).
"""

import datetime as dt
import json
import os

import config as C

EXIT_RESTART = 3
COME_BACK_SEC = 60
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def request_path():
    return os.path.join(C.DATA_DIR, "RESTART_REQUEST")


def state_path():
    return os.path.join(C.DATA_DIR, "restart_state.json")


def _now():
    return dt.datetime.now(IST)


def _write(path, obj):
    os.makedirs(C.DATA_DIR, exist_ok=True)
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


# ---------------------------------------------------------------- dashboard side
def request(reason="load an update"):
    _write(request_path(), {"requested_at": _now().isoformat(timespec="seconds"), "reason": reason})


def cancel():
    try:
        os.remove(request_path())
        return True
    except OSError:
        return False


def pending_request():
    return _read(request_path())


def state():
    return _read(state_path())


def problem(status_time=None, now=None):
    """A red-banner message if the last restart went wrong (or the bot never came back), else None."""
    st = state()
    if not st:
        return None
    now = now or _now()
    if st.get("phase") == "restarting":
        try:
            at = dt.datetime.fromisoformat(st["at"])
        except (KeyError, ValueError):
            return None
        back = status_time is not None and status_time > at
        if not back and (now - at).total_seconds() > COME_BACK_SEC:
            return (f"The bot stopped at {at:%H:%M} to restart and has not come back within {COME_BACK_SEC} s. "
                    "Start it again with 2-START-BOT.bat and check its window for errors.")
        return None
    if st.get("phase") == "verified" and not st.get("ok"):
        failed = [k for k, v in (st.get("checks") or {}).items() if not v]
        return f"The bot restarted at {st.get('at', '')[11:16]} but these checks failed: {', '.join(failed)}."
    return None


# ---------------------------------------------------------------- bot side
def is_safe(live, open_positions, now, market_open):
    """(ok, why). PAPER only; before the open or with no open position."""
    if live:
        return False, "live mode is never restarted automatically"
    if now < market_open:
        return True, "before the market opens"
    if not open_positions:
        return True, "no open position"
    return False, f"waiting: {open_positions} open position(s)"


def begin(mode, positions, trades_today, pid, version, now=None):
    """Record what the bot had, just before it exits to restart."""
    _write(state_path(), {"phase": "restarting", "at": (now or _now()).isoformat(timespec="seconds"),
                          "before": {"mode": mode, "positions": sorted(positions), "trades_today": trades_today,
                                     "pid": pid, "version": version}})
    try:
        os.remove(request_path())
    except OSError:
        pass


def verify(mode, positions, trades_today, pid, now=None):
    """Called by the new bot right after its first status write. Returns the checks (or None if this start
    was not a restart)."""
    st = state()
    if not st or st.get("phase") != "restarting":
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
        "still PAPER": mode == before.get("mode") == "paper",
        "same open positions": sorted(positions) == before.get("positions"),
        "same trades today": trades_today == before.get("trades_today"),
    }
    _write(state_path(), {"phase": "verified", "ok": all(checks.values()), "checks": checks,
                          "at": now.isoformat(timespec="seconds"), "took_sec": took, "before": before})
    return checks
