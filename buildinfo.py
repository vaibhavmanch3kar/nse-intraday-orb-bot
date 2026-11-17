"""
Which code and settings are running, and is an update waiting?

The bot records a snapshot when it starts (in status.json); the dashboard records its own. Comparing them
with the files on disk tells the dashboard what a restart would change:

- dashboard-only files (never imported by the bot): restarting the dashboard applies them; the bot is
  not touched. Theme files apply by themselves (uitheme.maybe_reload).
- bot files (anything bot.py imports, found by reading the import statements) and settings: active at
  the bot's next start.

Only hashes are recorded, never file contents or setting values, and secret settings (Kite keys, proxy
details) are left out completely.
"""

import ast
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DATA_FILES = ("nse_holidays.json",)          # data files the bot reads
_SECRET_RE = re.compile(r"KEY|SECRET|TOKEN|PASS|USER|HOST|PORT|EGRESS", re.I)


def _sha(data):
    return hashlib.sha256(data).hexdigest()[:12]


def files_fp(root=ROOT):
    """{file: short hash} for the program files: *.py in the main folder and the bot's data files."""
    out = {}
    for name in sorted(os.listdir(root)):
        if name.endswith(".py") or name in DATA_FILES:
            try:
                with open(os.path.join(root, name), "rb") as f:
                    out[name] = _sha(f.read())
            except OSError:
                pass
    return out


_IMPORTS_CACHE = {}   # (path, size, mtime) -> imported names: a file is read again only after it changed


def _imports(path):
    """Module names imported anywhere in a file (top level or inside functions)."""
    try:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
        if key in _IMPORTS_CACHE:
            return _IMPORTS_CACHE[key]
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    _IMPORTS_CACHE[key] = names
    return names


def program_files(entry, root=ROOT):
    """The main-folder .py files `entry` (e.g. 'bot.py') can load, following imports."""
    local = {n[:-3] for n in os.listdir(root) if n.endswith(".py")}
    seen, todo = set(), [entry[:-3] if entry.endswith(".py") else entry]
    while todo:
        m = todo.pop()
        if m in seen or m not in local:
            continue
        seen.add(m)
        todo.extend(_imports(os.path.join(root, m + ".py")) & local)
    return {m + ".py" for m in seen}


def bot_files(root=ROOT):
    return program_files("bot.py", root) | set(DATA_FILES)


def dashboard_files(root=ROOT):
    return program_files("web.py", root)


def setting_names():
    """The setting names whose value is recorded as a hash: every UPPERCASE setting in config.py except
    secrets (Kite keys, proxy details), which are never recorded."""
    import config as C
    secret = set(getattr(C, "_SECRET_VARS", ()))
    return sorted(k for k, v in vars(C).items()
                  if k.isupper() and not k.startswith("_") and k not in secret and not _SECRET_RE.search(k)
                  and isinstance(v, (str, int, float, bool, list, tuple, dict, type(None))))


def settings_fp():
    import config as C
    return {k: _sha(json.dumps(getattr(C, k), sort_keys=True, default=str).encode()) for k in setting_names()}


def version(root=ROOT):
    """'v1.9.0', 'v1.8.0-8-g9cc9a2e' (8 commits after v1.8.0) or the commit; '-dirty' if files were edited."""
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=root, capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:   # no git: the newest version in CHANGELOG.md
        with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as f:
            m = re.search(r"^## (v\d+\.\d+\.\d+)", f.read(), re.M)
            return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def snapshot(program, with_settings=True):
    """What this program is running now: version, file hashes and (for the bot) settings hashes."""
    snap = {"program": program, "version": version(), "files": files_fp(), "pid": os.getpid(),
            "started": dt.datetime.now(IST).isoformat(timespec="seconds")}
    if with_settings:
        snap["settings"] = settings_fp()
    return snap


def settings_on_next_start(timeout=20):
    """The settings hashes a bot started NOW would use: config.py evaluated in a fresh Python with the
    saved Windows user settings (a running program doesn't see settings changed after it started).
    None if that can't be worked out."""
    env = dict(os.environ)
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    env[name] = str(value)
                    i += 1
        except OSError:
            pass
    try:
        out = subprocess.run([sys.executable, "-B", "-c", "import json, buildinfo; print(json.dumps(buildinfo.settings_fp()))"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        return json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def pending(bot_snap, dash_snap, disk=None, settings_now=None, root=ROOT):
    """What is on disk but not running yet:
    {"bot_files": [...], "settings": [...], "dashboard_files": [...]} (empty lists = nothing waiting)."""
    disk = disk if disk is not None else files_fp(root)
    out = {"bot_files": [], "settings": [], "dashboard_files": []}
    bot_run = (bot_snap or {}).get("files") or {}
    if bot_run and bot_run != disk:      # (the usual case, nothing changed, needs no import reading)
        out["bot_files"] = sorted(f for f in bot_files(root) if disk.get(f) != bot_run.get(f))
    dash_run = (dash_snap or {}).get("files") or {}
    if dash_run and dash_run != disk:
        dash_only = dashboard_files(root) - bot_files(root)
        out["dashboard_files"] = sorted(f for f in dash_only if disk.get(f) != dash_run.get(f))
    bot_set = (bot_snap or {}).get("settings") or {}
    if bot_set and settings_now:
        out["settings"] = sorted(k for k in set(bot_set) | set(settings_now) if bot_set.get(k) != settings_now.get(k))
    return out
