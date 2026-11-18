"""
Saving a file safely on Windows.

Every file the bot keeps (status, positions, journal, arming, restart) is written to "<name>.tmp" and then moved
over the real file, so a crash never leaves a half-written file. On Windows that move fails with
"PermissionError: [WinError 5] Access is denied" whenever another program has the real file open at that
instant: the dashboard reading it, antivirus scanning it, a backup, Excel. The clash lasts milliseconds, so
`replace` simply tries again for about a second before giving up.

    safefile.replace(tmp, path)            # like os.replace, but waits out a busy file
    safefile.write_json(path, obj)         # tmp + replace in one call
"""

import json
import os
import time

TRIES = 25          # about 1.2 s in all: far longer than a read or a virus scan holds a small file
WAIT_SEC = 0.05


def replace(tmp, path, tries=TRIES, wait=WAIT_SEC, sleep=time.sleep):
    """os.replace(tmp, path); a busy file (PermissionError) is retried. Raises the last error if it stays busy."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            sleep(wait)


def write_text(path, text, newline=None):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    replace(tmp, path)


def write_json(path, obj, **dump_args):
    write_text(path, json.dumps(obj, **dump_args))
