"""
Pins: risk limits you want to hold whatever else happens.

A pinned limit is saved in data\\owner_limits.json. Every program of the bot reads it at its start (config.py)
and uses the STRICTER of your pin and the normal setting (a Windows user setting, or the built-in value). So:
  - a pin can never loosen a limit;
  - it survives restarts, updates and deployments (the data folder is never touched by them);
  - if the Windows setting is lost or changed to something looser, the pin still holds, and if this file is
    lost, the Windows setting still holds.
A change takes effect at the next start of the bot (and of the dashboard, for what it shows).

    .venv\\Scripts\\python.exe pins.py                        # show the pins and what is in force
    .venv\\Scripts\\python.exe pins.py set MAX_TRADES_PER_DAY 1
    .venv\\Scripts\\python.exe pins.py clear MAX_TRADES_PER_DAY

Only you run this. Nothing in the bot or the dashboard writes this file.
"""

import datetime as dt
import json
import os
import sys

import config as C


def path():
    return os.path.join(C.DATA_DIR, C.PINS_FILE)


def load():
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(pins):
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pins, f, indent=1)
    os.replace(tmp, path())


def set_pin(name, value):
    """Pin one limit. Returns the saved pins. Raises ValueError for an unknown name or a value that is not a
    positive number."""
    if name not in C.PINNABLE:
        raise ValueError(f"{name} cannot be pinned. Limits that can: {', '.join(C.PINNABLE)}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number")
    if v <= 0:
        raise ValueError("a pin must be above zero")
    pins = load()
    pins[name] = int(v) if v == int(v) else v
    pins["_changed"] = dt.datetime.now().isoformat(timespec="seconds")
    save(pins)
    return pins


def clear_pin(name):
    pins = load()
    if pins.pop(name, None) is not None:
        pins["_changed"] = dt.datetime.now().isoformat(timespec="seconds")
        save(pins)
    return pins


def report():
    """Plain lines: each pin, the normal setting, and what a program started now would use."""
    pins = {k: v for k, v in load().items() if k in C.PINNABLE}
    if not pins:
        return ["No limit is pinned."]
    out = []
    now = C.apply_pins()
    for name, pin in pins.items():
        got = now.get(name)
        if got:
            out.append(f"{name}: pinned {got['pin']:g}, normal setting {got['setting']:g} -> in force {got['used']:g}")
        else:
            out.append(f"{name}: pinned {pin!r} is not a usable value - ignored")
    return out


def main(argv):
    if len(argv) >= 4 and argv[1] == "set":
        try:
            set_pin(argv[2], argv[3])
        except ValueError as e:
            print(f"Not pinned: {e}")
            return 1
    elif len(argv) >= 3 and argv[1] == "clear":
        clear_pin(argv[2])
    elif len(argv) > 1 and argv[1] not in ("show",):
        print(__doc__)
        return 1
    print("\n".join(report()))
    print(f"File: {path()}. A change takes effect at the next start of the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
