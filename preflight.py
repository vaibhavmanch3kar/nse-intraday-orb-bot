#!/usr/bin/env python3
"""
Can the code in this folder start? Run in a separate Python before the bot restarts to load changes (and at the
close when changes are set to apply at the next start), so a broken edit never replaces a working bot.

  1. compiles every .py file in the folder (nothing is written);
  2. imports the bot's modules (bot.py and everything it imports) in this fresh Python;
  3. imports the dashboard (web.py): a problem there is reported but doesn't block a bot restart.

Prints one JSON line {"ok", "errors", "dashboard_errors", "version"} and exits 0 if the bot can start, 1 if not.
Never trades, never touches data files.

    .venv\\Scripts\\python.exe -B preflight.py
"""

import importlib
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))


def check(root=ROOT):
    sys.path.insert(0, root)
    errors, dash_errors = [], []
    for name in sorted(os.listdir(root)):
        if name.endswith(".py"):
            try:
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    compile(f.read(), name, "exec")
            except (SyntaxError, ValueError, UnicodeDecodeError) as e:
                errors.append(f"{name}: {type(e).__name__}: {e}")
    if not errors:
        import buildinfo
        for f in sorted(buildinfo.bot_files(root)):
            if f.endswith(".py"):
                try:
                    importlib.import_module(f[:-3])
                except BaseException as e:  # noqa: BLE001 - report anything, including SystemExit
                    last = traceback.format_exception_only(type(e), e)[-1].strip()
                    errors.append(f"{f}: {last}")
        try:
            importlib.import_module("web")
        except BaseException as e:  # noqa: BLE001
            dash_errors.append(f"web.py: {traceback.format_exception_only(type(e), e)[-1].strip()}")
    try:
        import buildinfo
        version = buildinfo.version(root)
    except Exception:  # noqa: BLE001
        version = "unknown"
    return {"ok": not errors, "errors": errors, "dashboard_errors": dash_errors, "version": version}


def run_separately(timeout=120):
    """Run the check in a fresh Python (as the bot does before restarting). Returns the result dict."""
    import subprocess
    try:
        out = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "preflight.py")], cwd=ROOT,
                             capture_output=True, text=True, timeout=timeout)
        lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
        if lines:
            return json.loads(lines[-1])
        return {"ok": False, "errors": [f"the check could not run ({(out.stderr or '').strip()[-300:]})"],
                "dashboard_errors": [], "version": "unknown"}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"ok": False, "errors": [f"the check could not run ({e})"], "dashboard_errors": [], "version": "unknown"}


if __name__ == "__main__":
    res = check()
    print(json.dumps(res), flush=True)
    sys.exit(0 if res["ok"] else 1)
