"""Secret protection: the day's Zerodha login is stored protected by Windows; an older plain file is still read and
is then protected; a file that cannot be read means "log in again", never a crash; the API keys can be moved into
a protected store and back, one at a time, never losing one; and no value is ever printed."""

import datetime as dt
import json
import os
import sys

import pytest

import config as C
import kite_proxy
import secretbox
import secure_secrets

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TOKEN = "made-up-access-token-0123456789"
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows data protection")


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    p = tmp_path / ".kite_token.json"
    monkeypatch.setattr(kite_proxy, "TOKEN_FILE", p)
    return p


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "store.json")


class Settings:
    def __init__(self, **values):
        self.values, self.fail_delete, self.fail_set = dict(values), set(), set()

    def get(self, name):
        return self.values.get(name) or None

    def set(self, name, value):
        if name in self.fail_set:
            raise OSError("denied")
        self.values[name] = value

    def delete(self, name):
        if name in self.fail_delete:
            raise OSError("denied")
        del self.values[name]


# ---------------------------------------------------------------- protecting a value
@windows
def test_a_protected_value_reads_back_and_shows_nothing_of_itself():
    p = secretbox.protect(TOKEN)
    assert p.startswith("dpapi:") and TOKEN not in p and secretbox.unprotect(p) == TOKEN
    assert secretbox.protect(TOKEN) != p                      # never the same bytes twice
    assert secretbox.is_protected(p) and not secretbox.is_protected(TOKEN)


@windows
def test_a_damaged_or_foreign_value_is_refused_not_guessed():
    p = secretbox.protect(TOKEN)
    with pytest.raises(secretbox.Unavailable):
        secretbox.unprotect(p[:-12] + "AAAAAAAAAAA=")
    with pytest.raises(secretbox.Unavailable):
        secretbox.unprotect("dpapi:not base64 !!")
    with pytest.raises(ValueError):
        secretbox.unprotect(TOKEN)


def test_without_windows_protection_it_says_so(monkeypatch):
    monkeypatch.setattr(secretbox, "available", lambda: False)
    with pytest.raises(secretbox.Unavailable):
        secretbox.protect("x")


# ---------------------------------------------------------------- the day's login
@windows
def test_login_is_saved_protected_and_read_back(token_file):
    kite_proxy.save_token(TOKEN, "AB1234")
    raw = token_file.read_text()
    data = json.loads(raw)
    assert TOKEN not in raw and "access_token" not in data and data["enc"].startswith("dpapi:")
    assert data["user_id"] == "AB1234" and dt.datetime.fromisoformat(data["created"])      # still plain: not secret
    assert kite_proxy.load_token() == TOKEN and kite_proxy.token_storage() == "protected"
    assert not token_file.with_name(token_file.name + ".tmp").exists()


@windows
def test_an_older_plain_file_is_read_and_then_protected(token_file):
    created = dt.datetime.now(IST).isoformat()
    token_file.write_text(json.dumps({"access_token": TOKEN, "user_id": "AB1234", "created": created}))
    assert kite_proxy.token_storage() == "plain"
    assert kite_proxy.load_token() == TOKEN
    raw = token_file.read_text()
    assert TOKEN not in raw and json.loads(raw)["created"] == created and kite_proxy.token_storage() == "protected"
    assert kite_proxy.load_token() == TOKEN


def test_yesterdays_login_and_unreadable_files_mean_log_in_again(token_file):
    assert kite_proxy.load_token() is None and kite_proxy.token_storage() == "none"
    old = (dt.datetime.now(IST) - dt.timedelta(days=2)).isoformat()
    token_file.write_text(json.dumps({"access_token": TOKEN, "user_id": "AB1234", "created": old}))
    assert kite_proxy.load_token() is None and TOKEN in token_file.read_text()             # an expired file is left alone
    now = dt.datetime.now(IST).isoformat()
    for text in ("not json", json.dumps({"enc": "dpapi:AAAA", "created": now}), json.dumps({"created": now}),
                 json.dumps({"enc": 5, "created": now}), json.dumps(["a"])):
        token_file.write_text(text)
        assert kite_proxy.load_token() is None, text


def test_where_windows_cannot_protect_the_login_still_works_and_is_reported(token_file, monkeypatch):
    def refuse(text):
        raise secretbox.Unavailable("no")

    monkeypatch.setattr(secretbox, "protect", refuse)
    kite_proxy.save_token(TOKEN, "AB1234")
    assert kite_proxy.load_token() == TOKEN and kite_proxy.token_storage() == "plain"


# ---------------------------------------------------------------- the key store and the loader
@windows
def test_store_keeps_names_readable_and_values_protected(store):
    secretbox.put("KITE_API_KEY", "made-up-key", store)
    secretbox.put("TRUEIP_PASS", "made-up-pass", store)
    text = open(store, encoding="utf-8").read()
    assert "made-up" not in text and secretbox.names(store) == ["KITE_API_KEY", "TRUEIP_PASS"]
    assert secretbox.get("KITE_API_KEY", store) == "made-up-key" and secretbox.get("NOPE", store) is None
    assert secretbox.remove("TRUEIP_PASS", store) and secretbox.names(store) == ["KITE_API_KEY"]
    open(store, "w").write("broken")
    assert secretbox.names(store) == [] and secretbox.get("KITE_API_KEY", store) is None


@windows
def test_loader_fills_only_a_secret_that_windows_does_not_have(store, monkeypatch):
    secretbox.put("KITE_API_KEY", "from-store", store)
    secretbox.put("KITE_API_SECRET", "from-store-2", store)
    secretbox.put("WATCHLIST", "not-a-secret-name", store)
    monkeypatch.setattr(secretbox, "STORE_FILE", store)
    monkeypatch.setenv("KITE_API_KEY", "from-windows")
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.delenv("WATCHLIST", raising=False)
    C.load_secret_store()
    assert os.environ["KITE_API_KEY"] == "from-windows" and os.environ["KITE_API_SECRET"] == "from-store-2"
    assert "WATCHLIST" not in os.environ                      # the store can only supply the known secret names


def test_a_broken_store_never_stops_the_program(monkeypatch):
    monkeypatch.setattr(secretbox, "names", lambda path=None: (_ for _ in ()).throw(RuntimeError("boom")))
    C.load_secret_store()


# ---------------------------------------------------------------- moving the keys
@windows
def test_move_stores_checks_and_only_then_removes_and_restore_puts_it_back(store):
    s = Settings(KITE_API_KEY="made-up-key", KITE_API_SECRET="made-up-secret", TRUEIP_PASS="made-up-pass", OTHER="x")
    assert dict(secure_secrets.status(s, store))["KITE_API_KEY"] == "Windows settings (plain text)"
    assert dict(secure_secrets.status(s, store))["TRUEIP_USER"] == "not set"
    res = dict(secure_secrets.move(s, store))
    assert res["KITE_API_KEY"] == "moved to the protected store" and res["TRUEIP_USER"] == "nothing to move"
    assert s.values == {"OTHER": "x"} and secretbox.get("KITE_API_SECRET", store) == "made-up-secret"
    assert dict(secure_secrets.status(s, store))["KITE_API_KEY"] == "protected store"
    back = dict(secure_secrets.restore(s, store))
    assert back["KITE_API_KEY"] == "back in the Windows settings" and secretbox.names(store) == []
    assert s.values == {"OTHER": "x", "KITE_API_KEY": "made-up-key", "KITE_API_SECRET": "made-up-secret",
                        "TRUEIP_PASS": "made-up-pass"}


@windows
def test_a_failure_never_loses_a_key(store, monkeypatch):
    s = Settings(KITE_API_KEY="made-up-key", KITE_API_SECRET="made-up-secret")
    s.fail_delete.add("KITE_API_KEY")
    res = dict(secure_secrets.move(s, store))
    assert "could not be removed" in res["KITE_API_KEY"] and s.values["KITE_API_KEY"] == "made-up-key"
    assert "both" in dict(secure_secrets.status(s, store))["KITE_API_KEY"]
    real_put = secretbox.put

    def refuse(name, value, path=None):
        raise secretbox.Unavailable("no")

    s2 = Settings(TRUEIP_PASS="made-up-pass")
    monkeypatch.setattr(secretbox, "put", refuse)
    res = dict(secure_secrets.move(s2, store))
    assert res["TRUEIP_PASS"].startswith("NOT moved") and s2.values == {"TRUEIP_PASS": "made-up-pass"}
    monkeypatch.setattr(secretbox, "put", real_put)
    s3 = Settings()
    s3.fail_set.add("KITE_API_SECRET")
    res = dict(secure_secrets.restore(s3, store))             # the secret moved above is still in the store
    assert res["KITE_API_SECRET"].startswith("NOT restored") and secretbox.get("KITE_API_SECRET", store) == "made-up-secret"


@windows
def test_status_and_results_never_show_a_value(store, capsys):
    s = Settings(KITE_API_KEY="made-up-key", KITE_API_SECRET="made-up-secret", TRUEIP_USER="made-up-user", TRUEIP_PASS="made-up-pass")
    texts = [secure_secrets.status(s, store), secure_secrets.move(s, store), secure_secrets.status(s, store),
             secure_secrets.restore(s, store)]
    assert "made-up" not in json.dumps(texts) and "made-up" not in capsys.readouterr().out


def test_nothing_moves_without_typing_the_word(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(secure_secrets, "move", lambda *a, **k: called.append("move") or [])
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert secure_secrets.main(["x", "move"]) == 1 and called == [] and "Nothing was changed." in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt="": "MOVE")
    assert secure_secrets.main(["x", "move"]) == 0 and called == ["move"]


def test_secret_files_are_never_committed():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ignore = open(os.path.join(root, ".gitignore"), encoding="utf-8").read().split()
    for name in (".kite_token.json", ".secrets.dpapi.json", ".env"):
        assert name in ignore, name
    for mod in (secretbox, secure_secrets):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "import logging" not in src                     # nothing here writes to a log
        for line in src.splitlines():
            if "print(" in line:
                assert "value" not in line and "token" not in line.replace("token_storage", ""), line
