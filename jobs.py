"""
Background jobs for the dashboard's slow pages (Review and Scanner).

A page never waits for a build any more: it shows the last good result straight away and, when that is
missing, out of date or you asked for a refresh, starts the build here in the background.

- One job per kind ("review", "scanner"). A second request while one runs joins it instead of starting a
  copy; a request for a different Review day is queued and runs next (the latest request wins).
- Each job has a time limit. Past it, no new downloads are started; what was fetched is still used and the
  result says which stocks were skipped.
- State for the status strip and /api/jobs: idle / running / completed / failed, progress (n of N),
  start time, elapsed time, last success and last error.

DISPLAY ONLY: jobs build research pages (Review, Scanner). They never place orders or change the bot.
"""

import datetime as dt
import threading
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ENABLED = True   # tests switch background jobs off unless they test them

_bg = threading.local()


def in_background():
    """True inside a dashboard job (its data requests give way to the bot's: see data_kite.Pacer)."""
    return getattr(_bg, "on", False)


class background:
    """`with jobs.background(flag):` marks this thread's work as a dashboard job (or not). Worker threads
    a job starts (download pools) use it to keep the mark."""
    def __init__(self, on=True):
        self.on = on

    def __enter__(self):
        self.prev, _bg.on = in_background(), self.on

    def __exit__(self, *exc):
        _bg.on = self.prev


class TimeLimit(Exception):
    """Raised inside a job once its time limit has passed."""


class Cancelled(Exception):
    """Raised inside a job after Cancel was pressed: nothing of this build is saved."""


class Job:
    def __init__(self, name, fn, limit_sec):
        self.name, self.fn, self.limit_sec = name, fn, limit_sec
        self._lock = threading.Lock()
        self.state = "idle"
        self.target = None          # what is being built (a Review day, or None for the latest)
        self.queued = None          # (target,) to run after this one
        self.done = self.total = 0
        self.step = ""
        self.started = self.finished = self.last_ok = None
        self.error = ""
        self.skipped = 0
        self._t0 = None
        self._thread = None
        self._cancelled = False

    # ------------------------------------------------------------------ control
    def start(self, target=None):
        """Start a build of `target`. Returns 'started', 'joined' (same build already running),
        'queued' (another target is running; this one runs next) or 'off' (jobs disabled)."""
        if not ENABLED:
            return "off"
        with self._lock:
            if self.state == "running":
                if target == self.target:
                    return "joined"
                self.queued = (target,)
                return "queued"
            self._begin(target)
            return "started"

    def _begin(self, target):  # caller holds the lock
        self.state, self.target, self.error, self.step = "running", target, "", "starting"
        self.done = self.total = self.skipped = 0
        self._cancelled = False
        self.started, self.finished, self._t0 = dt.datetime.now(IST), None, time.monotonic()
        self._thread = threading.Thread(target=self._run, args=(target,), name=f"job-{self.name}", daemon=True)
        self._thread.start()

    def _run(self, target):
        try:
            with background(True):
                self.fn(target, self)
            ok, err = True, ""
        except Cancelled:
            ok, err = None, ""
        except Exception as e:  # a failed build must never take the dashboard down
            ok, err = False, f"{type(e).__name__}: {e}" if not str(e) else str(e)
        with self._lock:
            if self._cancelled:
                ok, err = None, ""        # Cancel was pressed: whatever the work did, this build does not count
            self.finished = dt.datetime.now(IST)
            self.state = "completed" if ok else ("cancelled" if ok is None else "failed")
            self.error = err
            if ok:
                self.last_ok = self.finished
            nxt, self.queued = self.queued, None
            if nxt is not None:
                self._begin(nxt[0])

    def cancel(self):
        """Stop the running build: the work sees it at its next check, saves nothing, and nothing queued runs.
        Returns True if a build was running."""
        with self._lock:
            if self.state != "running":
                return False
            self._cancelled, self.queued, self.step = True, None, "cancelling"
            return True

    def cancelled(self):
        return self._cancelled

    def stop_if_cancelled(self):
        """Called by the work just before it saves its result."""
        if self._cancelled:
            raise Cancelled("cancelled")

    def failed_recently(self, sec):
        """True if the last build failed or was cancelled less than `sec` seconds ago (pages then don't start one
        by themselves, so a data outage can't cause a rebuild on every visit, and a cancelled build does not
        start again the moment the page reloads; Retry / Refresh still work)."""
        with self._lock:
            return (self.state in ("failed", "cancelled") and self.finished is not None
                    and (dt.datetime.now(IST) - self.finished).total_seconds() < sec)

    def wait(self, timeout=None):
        """Wait for the current build (tests and command-line use)."""
        t = self._thread
        if t:
            t.join(timeout)

    # ------------------------------------------------------------------ called by the work
    def elapsed(self):
        return time.monotonic() - self._t0 if self._t0 is not None and self.state == "running" else (
            (self.finished - self.started).total_seconds() if self.finished and self.started else 0.0)

    def over_time(self):
        """True once the time limit has passed, or Cancel was pressed: either way the work should stop now."""
        return self._cancelled or (self._t0 is not None and time.monotonic() - self._t0 > self.limit_sec)

    def plan(self, total, step=""):
        with self._lock:
            self.total, self.step = total, step or self.step

    def advance(self, step="", n=1):
        with self._lock:
            self.done += n
            if step:
                self.step = step

    def note_skipped(self, n=1):
        with self._lock:
            self.skipped += n

    def check(self):
        """Raise TimeLimit once the time limit has passed (for work that cannot be partly kept)."""
        self.stop_if_cancelled()
        if self.over_time():
            raise TimeLimit(f"stopped after the {self.limit_sec // 60}-minute time limit")

    # ------------------------------------------------------------------ for pages
    def snapshot(self):
        with self._lock:
            return {
                "name": self.name, "state": self.state, "target": self.target,
                "queued": self.queued[0] if self.queued else None, "done": self.done, "total": self.total,
                "step": self.step, "skipped": self.skipped, "limit_sec": self.limit_sec,
                "started": self.started.isoformat(timespec="seconds") if self.started else None,
                "finished": self.finished.isoformat(timespec="seconds") if self.finished else None,
                "elapsed_sec": round(self.elapsed(), 1),
                "last_ok": self.last_ok.isoformat(timespec="seconds") if self.last_ok else None,
                "error": self.error,
            }


class NoJob:
    """Stands in when no job object is passed: progress calls do nothing, nothing is ever over time."""
    def plan(self, *a, **k): pass
    def advance(self, *a, **k): pass
    def note_skipped(self, *a, **k): pass
    def check(self): pass
    def over_time(self): return False
    def cancelled(self): return False
    def stop_if_cancelled(self): pass


NO_JOB = NoJob()
_JOBS = {}


def register(name, fn, limit_sec):
    """Define a job kind once (the dashboard does this at start-up). fn(target, job) does the work."""
    if name not in _JOBS:
        _JOBS[name] = Job(name, fn, limit_sec)
    return _JOBS[name]


def get(name):
    return _JOBS.get(name)


def start(name, target=None):
    j = _JOBS.get(name)
    return j.start(target) if j else "off"


def snapshot_all():
    return {n: j.snapshot() for n, j in _JOBS.items()}
