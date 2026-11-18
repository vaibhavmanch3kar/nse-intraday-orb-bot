#!/usr/bin/env python3
"""
Watchdog: keeps the bot and the dashboard running on a trading day, and keeps a record of why they stopped.

  Crash capture     an error that ends the bot is saved in full to data/logs/crash_<time>.txt (bot.main).
  After a crash     run_bot.ps1 asks `watchdog.py after-exit <exit code>`: RESTART on a trading day between
                    09:00 and the close, at most MAX_RESTARTS times a day; never after a deliberate stop.
  Five-minute check the "Kite ORB Watchdog" task runs `watchdog.py check` every 5 minutes: it starts the
                    dashboard or the bot if either is missing, and ends a bot that has stopped responding
                    (no heartbeat for HANG_SEC) so that it starts again.
  Stopped on purpose the Stop button / STOP file / stopping the task leave a mark for the day: the watchdog
                    never starts the bot again until someone starts it by hand (or the next day's 9:00 start).

It never trades, never changes the mode or any setting and never touches balances or positions: a bot it
starts is the same `run_bot.ps1` start as always (paper unless real money was armed today, and the bot
re-checks its positions against Zerodha when it starts).

    .venv\\Scripts\\python.exe watchdog.py status          # today's record
    .venv\\Scripts\\python.exe watchdog.py check           # what the 5-minute task runs
"""

import datetime as dt
import json
import os
import sys
import traceback

import config as C
import holidays
import safefile

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_RESTARTS = 5            # automatic starts per day (crash restarts + watchdog starts together)
HANG_SEC = 300              # no heartbeat for this long while the bot process exists = not responding
WATCH_FROM = "09:00"        # the watchdog acts from here to MARKET_CLOSE on trading days
MUTEX = "Local\\AyodhyaORB_bot"
KEEP_EVENTS = 60
ALERT_KINDS = {"crash": "dn", "hang": "dn", "gave_up": "dn", "restart": "warn", "dashboard": "warn"}


def now_ist():
    return dt.datetime.now(IST)


def state_path():
    return os.path.join(C.DATA_DIR, "watchdog.json")


def heartbeat_path():
    return os.path.join(C.DATA_DIR, "heartbeat.json")


def logs_dir():
    return os.path.join(C.DATA_DIR, "logs")


def log(text, now=None):
    """One line in data/logs/watchdog.log. Never raises."""
    try:
        os.makedirs(logs_dir(), exist_ok=True)
        with open(os.path.join(logs_dir(), "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(f"{(now or now_ist()):%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def load(now=None):
    """Today's record: {day, restarts, stopped, events}. A new day starts empty."""
    now = now or now_ist()
    day = now.date().isoformat()
    try:
        with open(state_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("day") == day:
            d.setdefault("restarts", 0)
            d.setdefault("stopped", None)
            d.setdefault("events", [])
            return d
    except (OSError, ValueError):
        pass
    return {"day": day, "restarts": 0, "stopped": None, "events": []}


def _save(d):
    try:
        safefile.write_json(state_path(), d, indent=1)
    except OSError as e:
        log(f"could not save watchdog.json ({type(e).__name__}: {e})")


def add_event(d, kind, text, now, **extra):
    ev = {"at": now.isoformat(timespec="seconds"), "kind": kind, "text": text, **extra}
    d["events"] = (d.get("events") or [])[-(KEEP_EVENTS - 1):] + [ev]
    log(f"{kind}: {text}", now)
    return ev


# ---------------------------------------------------------------- stopped on purpose
def mark_stopped(why, now=None):
    """The bot was stopped deliberately today (Stop button, STOP file, the task was stopped)."""
    now = now or now_ist()
    d = load(now)
    d["stopped"] = why
    add_event(d, "stopped", f"stopped on purpose ({why}): the watchdog will not start the bot again today", now)
    _save(d)


def bot_started(now=None, pid=None):
    """Called by the bot at every start: a start by anyone ends 'stopped on purpose'. Returns today's record."""
    now = now or now_ist()
    d = load(now)
    if d.get("stopped"):
        d["stopped"] = None
        add_event(d, "started", "bot started again after a deliberate stop", now)
    _save(d)
    beat(now, pid)
    return d


def stopped_on_purpose(now=None):
    return bool(load(now).get("stopped"))


# ---------------------------------------------------------------- heartbeat (hang detection)
def beat(now=None, pid=None):
    """The bot is alive and going round its loop. A plain small write: nobody else keeps this file open."""
    try:
        os.makedirs(C.DATA_DIR, exist_ok=True)
        with open(heartbeat_path(), "w", encoding="utf-8") as f:
            json.dump({"at": (now or now_ist()).isoformat(timespec="seconds"), "pid": pid or os.getpid()}, f)
    except OSError:
        pass


def heartbeat(now=None):
    """(seconds since the last beat, pid) or (None, None) if there is no usable heartbeat."""
    now = now or now_ist()
    try:
        with open(heartbeat_path(), encoding="utf-8") as f:
            h = json.load(f)
        return (now - dt.datetime.fromisoformat(h["at"])).total_seconds(), int(h["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


# ---------------------------------------------------------------- crash capture
def record_crash(exc, now=None, mode=None, version=None, positions=None):
    """Save the full error of a bot that is about to end. Returns the crash file's path (or None). Never raises."""
    now = now or now_ist()
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        one = f"{type(exc).__name__}: {exc}".strip()[:300]
        name = f"crash_{now:%Y-%m-%d_%H%M%S}.txt"
        os.makedirs(logs_dir(), exist_ok=True)
        path = os.path.join(logs_dir(), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Ayodhya ORB ended with an error at {now:%Y-%m-%d %H:%M:%S} IST\n"
                    f"mode: {mode}   version: {version}   open positions: {', '.join(positions or []) or 'none'}\n"
                    f"error: {one}\n\n{tb}")
        d = load(now)
        add_event(d, "crash", one, now, file=name, mode=mode)
        _save(d)
        return path
    except Exception:  # noqa: BLE001 - recording a crash must never raise a second one
        return None


# ---------------------------------------------------------------- when the watchdog may act
def _at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), IST)


def in_session(now=None):
    """A trading day, from WATCH_FROM to the close: the only time the watchdog starts or ends anything."""
    now = now or now_ist()
    day = now.date()
    return holidays.is_trading_day(day) and _at(day, WATCH_FROM) <= now <= _at(day, C.MARKET_CLOSE)


def after_exit(code, now=None):
    """The bot process ended with `code` (not the restart code 3, which run_bot.ps1 handles itself).
    Returns ("RESTART" | "STOP", reason) and records it."""
    now = now or now_ist()
    d = load(now)
    if code == 0:
        log("bot ended normally (exit code 0)", now)
        return "STOP", "ended normally"
    if d.get("stopped"):
        add_event(d, "exit", f"bot ended (exit code {code}) after a deliberate stop: not starting it again", now)
        _save(d)
        return "STOP", "stopped on purpose today"
    if not in_session(now):
        add_event(d, "exit", f"bot ended unexpectedly (exit code {code}) outside market hours: not starting it", now)
        _save(d)
        return "STOP", "outside market hours"
    if d["restarts"] >= MAX_RESTARTS:
        add_event(d, "gave_up", f"bot ended unexpectedly again (exit code {code}) after {MAX_RESTARTS} automatic "
                                f"starts today: NOT starting it again. Look at data\\logs for the reason.", now)
        _save(d)
        return "STOP", f"already started {MAX_RESTARTS} times today"
    d["restarts"] += 1
    add_event(d, "restart", f"bot ended unexpectedly (exit code {code}); starting it again "
                            f"({d['restarts']} of {MAX_RESTARTS} today)", now)
    _save(d)
    return "RESTART", f"{d['restarts']} of {MAX_RESTARTS}"


def check(now=None, probe=None):
    """The 5-minute check. `probe` has bot_running(), dashboard_listening(), start_bot(), start_dashboard(),
    kill(pid). Returns the list of things it did (also recorded)."""
    now = now or now_ist()
    probe = probe or RealProbe()
    done = []
    if not in_session(now):
        return done
    d = load(now)
    if not probe.dashboard_listening():
        probe.start_dashboard()
        add_event(d, "dashboard", "the dashboard was not running: started it", now)
        done.append("started dashboard")
    if d.get("stopped"):
        _save(d)
        return done
    if not probe.bot_running():
        if d["restarts"] >= MAX_RESTARTS:
            if not any(e["kind"] == "gave_up" for e in d["events"]):
                add_event(d, "gave_up", f"the bot is not running and was already started {MAX_RESTARTS} times "
                                        f"today: NOT starting it again. Look at data\\logs for the reason.", now)
        else:
            d["restarts"] += 1
            probe.start_bot()
            add_event(d, "restart", f"the bot was not running: started it "
                                    f"({d['restarts']} of {MAX_RESTARTS} today)", now)
            done.append("started bot")
    else:
        age, pid = heartbeat(now)
        trading = now >= _at(now.date(), C.MARKET_OPEN) + dt.timedelta(seconds=HANG_SEC)
        if age is not None and pid and age > HANG_SEC and trading:
            probe.kill(pid)
            add_event(d, "hang", f"the bot had not responded for {age / 60:.0f} minutes: ended it so that it "
                                 f"starts again (open positions keep their exchange stop-loss)", now)
            done.append(f"ended hung bot {pid}")
    _save(d)
    return done


def notices(now=None):
    """Today's watchdog events worth a popup on the dashboard: [{key, tone, title, body, at}]."""
    d = load(now)
    titles = {"crash": "The bot stopped with an error", "hang": "The bot stopped responding",
              "gave_up": "The bot is NOT being restarted", "restart": "The bot was started again",
              "dashboard": "The dashboard was started again"}
    out = []
    for i, e in enumerate(d.get("events") or []):
        if e.get("kind") in ALERT_KINDS:
            body = e["text"] + (f" Details: data\\logs\\{e['file']}" if e.get("file") else "")
            out.append({"key": f"{d['day']}-{e['at'][11:19]}-{e['kind']}-{i}", "tone": ALERT_KINDS[e["kind"]],
                        "title": titles[e["kind"]], "body": body, "at": e["at"]})
    return out


def last_events(now=None, kinds=("crash", "hang", "restart"), within_sec=180):
    """Recent events for the bot to repeat in its own log and events list when it starts."""
    now = now or now_ist()
    out = []
    for e in load(now).get("events") or []:
        try:
            age = (now - dt.datetime.fromisoformat(e["at"])).total_seconds()
        except (KeyError, ValueError):
            continue
        if e.get("kind") in kinds and 0 <= age <= within_sec:
            out.append(e)
    return out


# ---------------------------------------------------------------- the real PC (Windows)
class RealProbe:
    def bot_running(self):
        if os.name != "nt":
            return False
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenMutexW.restype = ctypes.c_void_p
        k32.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
        h = k32.OpenMutexW(0x00100000, False, MUTEX)        # SYNCHRONIZE: only to see that it exists
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        return bool(h)

    def dashboard_listening(self, timeout=1.0):
        import socket
        try:
            with socket.create_connection((C.WEB_HOST, C.WEB_PORT), timeout=timeout):
                return True
        except OSError:
            return False

    def _spawn(self, args):
        import subprocess
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                                   # SW_SHOWMINNOACTIVE: minimized, doesn't take focus
        subprocess.Popen(args, cwd=ROOT, startupinfo=si, creationflags=subprocess.CREATE_NEW_CONSOLE,
                         close_fds=True)

    def start_bot(self):
        self._spawn(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     os.path.join(ROOT, "run_bot.ps1"), "-NoBrowser"])

    def start_dashboard(self):
        py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
        self._spawn([py if os.path.exists(py) else sys.executable, "web.py", "--no-browser"])

    def kill(self, pid):
        import subprocess
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "after-exit":
        try:
            code = int(argv[2])
        except (IndexError, ValueError):
            code = 1
        verdict, why = after_exit(code)
        print(f"{verdict} {why}")
    elif cmd == "check":
        try:
            done = check()
        except Exception as e:  # noqa: BLE001 - the check runs unattended: record, never show a dialog
            log(f"check failed: {type(e).__name__}: {e}")
            return 1
        if done:
            print("; ".join(done))
    else:
        d = load()
        print(f"{d['day']}: automatic starts {d['restarts']} of {MAX_RESTARTS}; "
              f"stopped on purpose: {d['stopped'] or 'no'}; in session now: {in_session()}")
        for e in d["events"]:
            print(f"  {e['at'][11:19]} {e['kind']}: {e['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
