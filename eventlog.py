"""
Structured logs with secrets removed.

  - Every log line of the bot and the dashboard is also written as one JSON object per line to
    data/logs/events_<date>.jsonl ({"ts", "level", "program", "logger", "pid", "msg", "exc"}), so the day can be
    searched and filtered by a program instead of read by eye. The plain-text bot_<date>.log stays as it is.
  - Before anything is written, to either file or to the console, secrets are removed: the Kite API key and
    secret, the proxy user and password, today's access token (whatever text they appear in, tracebacks
    included), and anything that looks like one (access_token=..., user:password@host, an Authorization header).

install() is called once by each program. It never raises: logging extras must not be able to stop the bot.
"""

import datetime as dt
import json
import logging
import os
import re
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HIDDEN = "[hidden]"
SECRET_NAMES = ("KITE_API_KEY", "KITE_API_SECRET", "TRUEIP_USER", "TRUEIP_PASS")
MIN_LEN = 6                   # shorter values are not replaced (they would blank ordinary words and numbers)
TOKEN_RECHECK_SEC = 60
PATTERNS = (
    (re.compile(r"(?i)\b(access_token|api_secret|api_key|enctoken|request_token|public_token|refresh_token|password"
                r"|passwd|checksum)(\"?'?\s*[=:]\s*\"?'?)([^\s'\"&,;)}\]]+)"), r"\1\2" + HIDDEN),
    (re.compile(r"(?i)(authorization\"?'?\s*[:=]\s*\"?'?(?:token|bearer|basic)\s+)([^\s'\",;]+)"), r"\1" + HIDDEN),
    (re.compile(r"(://)([^\s:/@]+):([^\s@/]+)@"), r"\1" + HIDDEN + ":" + HIDDEN + "@"),
)
_token = {"at": 0.0, "value": None}


def _todays_token():
    now = time.monotonic()
    if now - _token["at"] > TOKEN_RECHECK_SEC or _token["at"] == 0.0:
        _token["at"] = now
        try:
            import kite_proxy
            _token["value"] = kite_proxy.load_token()
        except BaseException:  # noqa: BLE001 - no Kite settings, no login, anything: there is simply no token
            _token["value"] = None
    return _token["value"]


def secret_values():
    """The secret values this process knows right now, longest first."""
    vals = {os.environ.get(n) for n in SECRET_NAMES} | {_todays_token()}
    return sorted((v for v in vals if v and len(v) >= MIN_LEN), key=len, reverse=True)


def redact(text, values=None):
    """`text` with every known secret value and everything shaped like one replaced by [hidden]."""
    if not isinstance(text, str) or not text:
        return text
    for v in (secret_values() if values is None else values):
        if v in text:
            text = text.replace(v, HIDDEN)
    for rx, repl in PATTERNS:
        text = rx.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's own formatter: same line, secrets removed (the traceback is part of the line)."""

    def __init__(self, inner=None):
        super().__init__()
        self.inner = inner or logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record):
        return redact(self.inner.format(record))


class JsonLinesHandler(logging.Handler):
    """One JSON object per log record in <folder>/events_<India date>.jsonl."""

    def __init__(self, folder, program):
        super().__init__()
        self.folder, self.program, self.day, self.stream = folder, program, None, None

    def emit(self, record):
        try:
            when = dt.datetime.fromtimestamp(record.created, IST)
            day = when.date().isoformat()
            if day != self.day:
                self.close_stream()
                os.makedirs(self.folder, exist_ok=True)
                self.stream = open(os.path.join(self.folder, f"events_{day}.jsonl"), "a", encoding="utf-8")
                self.day = day
            row = {"ts": when.isoformat(timespec="milliseconds"), "level": record.levelname, "program": self.program,
                   "logger": record.name, "pid": record.process, "msg": redact(record.getMessage())}
            if record.exc_info:
                row["exc"] = redact(logging.Formatter().formatException(record.exc_info))
            self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.stream.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close_stream(self):
        if self.stream:
            self.stream.close()
        self.stream, self.day = None, None

    def close(self):
        self.close_stream()
        super().close()


def install(program, folder, root=None):
    """Make every existing handler remove secrets, and add the JSON-lines file. Safe to call twice.
    Returns True when it is in place."""
    try:
        root = root or logging.getLogger()
        for h in root.handlers:
            if not isinstance(h, JsonLinesHandler) and not isinstance(h.formatter, RedactingFormatter):
                h.setFormatter(RedactingFormatter(h.formatter))
        if not any(isinstance(h, JsonLinesHandler) for h in root.handlers):
            root.addHandler(JsonLinesHandler(folder, program))
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        return True
    except Exception:  # noqa: BLE001
        return False


def read(day, folder, limit=200, level=None, program=None):
    """The newest `limit` records of a day, oldest first (for the dashboard and for searching by hand)."""
    out = []
    try:
        with open(os.path.join(folder, f"events_{day}.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if (level is None or r.get("level") == level) and (program is None or r.get("program") == program):
                    out.append(r)
    except OSError:
        pass
    return out[-limit:]
