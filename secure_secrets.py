"""
Where the secrets are kept, and a way to move them into the protected store.

    .venv\\Scripts\\python.exe secure_secrets.py status     # names and where each is kept (never a value)
    .venv\\Scripts\\python.exe secure_secrets.py move       # Windows user settings -> protected store
    .venv\\Scripts\\python.exe secure_secrets.py restore    # protected store -> Windows user settings

Today the Kite API key, API secret and the proxy user and password sit in the Windows user settings as plain
text. `move` stores each one protected by Windows (secretbox.py: readable only by this Windows user on this PC),
checks that it reads back the same, and only then removes it from the Windows settings. The bot and the dashboard
find it in the store from their next start (config.load_secret_store). `restore` puts everything back.

Run it with the bot stopped. Nothing here prints a value, and nothing is moved without typing MOVE.
A copy of the store on another PC, or under another Windows user, cannot be read: keep your keys written down
somewhere safe, as you would anyway.
"""

import sys

import secretbox

MOVE_NAMES = ("KITE_API_KEY", "KITE_API_SECRET", "TRUEIP_USER", "TRUEIP_PASS")


class WindowsSettings:
    """The current user's saved environment (HKCU\\Environment). Tests pass a dictionary-backed stand-in."""

    def get(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return str(winreg.QueryValueEx(k, name)[0]) or None
        except OSError:
            return None

    def set(self, name, value):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, name):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)


def status(settings=None, store_path=None):
    """[(name, where)] with where one of 'Windows settings (plain text)', 'protected store', 'both', 'not set',
    'protected store (NOT readable here)'."""
    settings = settings or WindowsSettings()
    stored = set(secretbox.names(store_path))
    out = []
    for name in MOVE_NAMES:
        plain = bool(settings.get(name))
        if name in stored:
            readable = secretbox.get(name, store_path) is not None
            where = ("both (the Windows setting is the one used)" if plain else
                     "protected store" if readable else "protected store (NOT readable by this user on this PC)")
        else:
            where = "Windows settings (plain text)" if plain else "not set"
        out.append((name, where))
    return out


def move(settings=None, store_path=None):
    """Move every secret found in the Windows settings into the store. Each one is stored and read back before
    its Windows setting is removed; a failure leaves that one exactly where it was. Returns [(name, result)]."""
    settings = settings or WindowsSettings()
    out = []
    for name in MOVE_NAMES:
        value = settings.get(name)
        if not value:
            out.append((name, "nothing to move"))
            continue
        try:
            secretbox.put(name, value, store_path)
            if secretbox.get(name, store_path) != value:
                raise secretbox.Unavailable("did not read back")
        except (secretbox.Unavailable, OSError) as e:
            out.append((name, f"NOT moved, left in the Windows settings ({type(e).__name__})"))
            continue
        try:
            settings.delete(name)
            out.append((name, "moved to the protected store"))
        except OSError:
            out.append((name, "stored, but the Windows setting could not be removed (it is still the one used)"))
    return out


def restore(settings=None, store_path=None):
    """Put every stored secret back into the Windows settings, then remove it from the store."""
    settings = settings or WindowsSettings()
    out = []
    for name in secretbox.names(store_path):
        if name not in MOVE_NAMES:
            continue
        value = secretbox.get(name, store_path)
        if value is None:
            out.append((name, "NOT restored: this user on this PC cannot read it; enter it again with the setup script"))
            continue
        try:
            settings.set(name, value)
            if settings.get(name) != value:
                raise OSError("did not read back")
        except OSError:
            out.append((name, "NOT restored: the Windows setting could not be written; it stays in the store"))
            continue
        secretbox.remove(name, store_path)
        out.append((name, "back in the Windows settings"))
    return out


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        for name, where in status():
            print(f"  {name:18s} {where}")
        try:
            import kite_proxy
            print(f"  {'today' + chr(39) + 's login':18s} {kite_proxy.token_storage()}")
        except BaseException:  # noqa: BLE001 - missing Kite settings make kite_proxy exit
            pass
        return 0
    if cmd in ("move", "restore"):
        if not secretbox.available():
            print("Windows data protection is only available on Windows.")
            return 1
        word = cmd.upper()
        if "--yes" not in argv and input(f"Stop the bot first. Type {word} to go on: ").strip() != word:
            print("Nothing was changed.")
            return 1
        for name, result in (move() if cmd == "move" else restore()):
            print(f"  {name:18s} {result}")
        print("Start the bot and the dashboard again so that they read the keys from their new place.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
