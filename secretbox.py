"""
Secret protection with Windows' own data protection (DPAPI). What is protected here can only be read back by the
same Windows user on the same PC: a copied file, a backup or a stolen disk shows only unreadable bytes. No
password is asked and no key is kept by this program; Windows holds the key with the user's sign-in.

Two things use it:
  - the Zerodha login of the day (kite_proxy.save_token / load_token): the access token is stored protected;
  - the secret store (.secrets.dpapi.json next to this file): API key, API secret and proxy details, for anyone
    who moves them out of the Windows user settings with secure_secrets.py. config.py reads the store only for a
    name that is not in the Windows settings.

What it does NOT protect against: a program already running as you on this PC can ask Windows for the same
values. It protects the values at rest, not a compromised session.

Nothing here prints, logs or returns a secret except to the caller that asked for it by name.
"""

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE_FILE = os.path.join(ROOT, ".secrets.dpapi.json")
ENTROPY = b"ayodhya-orb/secretbox/v1"      # ties a protected value to this program's use (not a secret)
PREFIX = "dpapi:"


class Unavailable(Exception):
    """Windows data protection is not usable here (not Windows, or Windows refused)."""


def available():
    return sys.platform == "win32"


def _blob_type():
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    return DATA_BLOB


def _call(fn_name, data):
    if not available():
        raise Unavailable("Windows data protection is only available on Windows")
    import ctypes
    DATA_BLOB = _blob_type()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    fn = getattr(crypt32, fn_name)
    fn.argtypes = (ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DATA_BLOB))
    fn.restype = ctypes.c_int
    buf_in = ctypes.create_string_buffer(data, len(data))
    buf_ent = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_ent = DATA_BLOB(len(ENTROPY), ctypes.cast(buf_ent, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN: never show a prompt (the bot runs unattended)
    if not fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0x1, ctypes.byref(blob_out)):
        raise Unavailable(f"Windows refused ({fn_name}, error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def protect(text):
    """A protected, printable form of `text` ('dpapi:...'). Raises Unavailable if Windows cannot do it."""
    return PREFIX + base64.b64encode(_call("CryptProtectData", text.encode("utf-8"))).decode("ascii")


def unprotect(value):
    """The text behind a protected value. Raises Unavailable if it was protected by another user or PC, or is
    damaged; raises ValueError if `value` is not a protected value at all."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise ValueError("not a protected value")
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
    except ValueError:
        raise Unavailable("the protected value is damaged")
    return _call("CryptUnprotectData", raw).decode("utf-8")


def is_protected(value):
    return isinstance(value, str) and value.startswith(PREFIX)


# ---------------------------------------------------------------- the secret store
def _load(path=None):
    try:
        with open(path or STORE_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d, path=None):
    path = path or STORE_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def names(path=None):
    """The names kept in the store (never the values)."""
    return sorted(k for k, v in _load(path).items() if is_protected(v))


def get(name, path=None):
    """The value stored under `name`, or None if it is not stored or cannot be read on this PC by this user."""
    v = _load(path).get(name)
    if not is_protected(v):
        return None
    try:
        return unprotect(v)
    except (Unavailable, ValueError, UnicodeDecodeError):
        return None


def put(name, value, path=None):
    """Store `value` under `name`, protected, and read it back to be sure. Raises Unavailable on failure."""
    d = _load(path)
    d[name] = protect(value)
    if unprotect(d[name]) != value:
        raise Unavailable("the value did not read back the same")
    _save(d, path)


def remove(name, path=None):
    d = _load(path)
    if name in d:
        del d[name]
        _save(d, path)
        return True
    return False
