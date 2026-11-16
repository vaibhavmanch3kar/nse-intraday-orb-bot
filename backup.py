#!/usr/bin/env python3
"""
Daily backup of the data folder (trades, journal, reports, logs, state, candle archive).

The bot runs it after the close: data/ -> <backups>/data_YYYY-MM-DD.zip, keeping the newest
BACKUP_KEEP (30) and deleting only older files named data_YYYY-MM-DD.zip in that folder.

    .venv\\Scripts\\python.exe backup.py          # back up now
    .venv\\Scripts\\python.exe backup.py --list   # show the backups

The default folder (KiteConnect\\backups\\data) is on the same disk as the bot. To survive a disk
failure, set BACKUP_DIR to a OneDrive or USB folder:
    [Environment]::SetEnvironmentVariable("BACKUP_DIR", "D:\\OneDrive\\kite-backups", "User")

Never included: the Kite login (it isn't in data/), temporary files, the STOP/PAUSE switches and
anything with 'token' in its name. To restore, unzip a backup into an empty data folder while the
bot is stopped.
"""

import datetime as dt
import os
import re
import zipfile

import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAME_RE = re.compile(r"^data_\d{4}-\d{2}-\d{2}\.zip$")


def backup_dir():
    if C.BACKUP_DIR:
        return os.path.abspath(C.BACKUP_DIR)
    project = os.path.dirname(os.path.abspath(C.DATA_DIR))
    return os.path.join(os.path.dirname(project), "backups", "data")


def _skip(name):
    low = name.lower()
    return low.endswith(".tmp") or "token" in low or low in ("stop", "pause", ".env")


def backups():
    """[(name, bytes)] newest first."""
    folder = backup_dir()
    try:
        names = sorted((n for n in os.listdir(folder) if NAME_RE.match(n)), reverse=True)
    except OSError:
        return []
    return [(n, os.path.getsize(os.path.join(folder, n))) for n in names]


def backup(day=None, keep=None):
    """Zip the data folder. Returns {path, files, bytes, removed}."""
    day = day or dt.datetime.now(IST).date()
    keep = C.BACKUP_KEEP if keep is None else keep
    data, folder = os.path.abspath(C.DATA_DIR), backup_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"data_{day}.zip")
    tmp = path + ".tmp"
    count = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(data):
            # never zip the backup folder into itself (if someone points BACKUP_DIR inside data/)
            dirs[:] = sorted(d for d in dirs if os.path.abspath(os.path.join(root, d)) != folder)
            for f in sorted(files):
                if _skip(f):
                    continue
                full = os.path.join(root, f)
                try:
                    z.write(full, os.path.join("data", os.path.relpath(full, data)))
                    count += 1
                except OSError:   # a file being rewritten right now: skip it, the rest still counts
                    pass
    os.replace(tmp, path)
    removed = []
    if keep > 0:
        for name, _ in backups()[keep:]:
            os.remove(os.path.join(folder, name))
            removed.append(name)
    return {"path": path, "files": count, "bytes": os.path.getsize(path), "removed": removed}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Back up the data folder (read-only for trading)")
    ap.add_argument("--list", action="store_true", help="show the backups")
    a = ap.parse_args()
    if a.list:
        print(backup_dir())
        for name, size in backups():
            print(f"  {name}  {size / 1e6:.1f} MB")
        return
    r = backup()
    print(f"{r['path']}: {r['files']} files, {r['bytes'] / 1e6:.1f} MB"
          + (f"; removed {', '.join(r['removed'])}" if r["removed"] else ""))


if __name__ == "__main__":
    main()
