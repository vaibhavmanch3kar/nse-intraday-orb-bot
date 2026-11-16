"""Daily backup of the data folder (Item 5)."""

import datetime as dt
import os
import zipfile

import archive
import backup
import bot
import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def make_data():
    files = {"trades_paper.csv": "closed,net\n", "state_paper.json": "{}", "journal/journal_paper.csv": "date\n",
             "logs/bot_2026-11-16.log": "x\n", "candles/2026-11-16.csv": "t\n", "status.json.tmp": "half",
             "STOP": "", "PAUSE": "", "kite_token_copy.json": "secret"}
    for rel, text in files.items():
        p = os.path.join(C.DATA_DIR, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


def test_default_folder_is_next_to_the_project(monkeypatch):
    monkeypatch.setattr(C, "BACKUP_DIR", "")
    monkeypatch.setattr(C, "DATA_DIR", os.path.join("D:\\", "Trading", "KiteConnect", "kite-trader", "data"))
    assert backup.backup_dir() == os.path.join("D:\\", "Trading", "KiteConnect", "backups", "data")


def test_zip_has_the_data_but_no_secrets_or_switches():
    make_data()
    r = backup.backup(dt.date(2026, 11, 16))
    assert r["path"].endswith("data_2026-11-16.zip") and r["files"] == 5
    names = set(zipfile.ZipFile(r["path"]).namelist())
    assert names == {f"data/{n}" for n in ("trades_paper.csv", "state_paper.json", "journal/journal_paper.csv",
                                           "logs/bot_2026-11-16.log", "candles/2026-11-16.csv")}
    assert not any("_backups" in n for n in names)          # the backup folder is never zipped into itself


def test_keeps_only_the_newest(monkeypatch):
    make_data()
    other = os.path.join(backup.backup_dir(), "notes.zip")
    os.makedirs(backup.backup_dir(), exist_ok=True)
    open(other, "w").close()
    for d in range(1, 6):
        r = backup.backup(dt.date(2026, 11, d), keep=3)
    assert [n for n, _ in backup.backups()] == ["data_2026-11-05.zip", "data_2026-11-04.zip", "data_2026-11-03.zip"]
    assert r["removed"] == ["data_2026-11-02.zip"]
    assert os.path.exists(other)                             # never touches other files


def test_bot_backs_up_after_the_close_and_never_stops(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(archive, "fetch_and_save", lambda *a, **k: {"days": [], "source": "kite", "failed": []})
    make_data()
    b = bot.Bot()
    b.end_of_day(dt.datetime(2026, 11, 18, 15, 36, tzinfo=IST))
    assert os.path.exists(os.path.join(backup.backup_dir(), "data_2026-11-18.zip"))
    assert any("data backup: data_2026-11-18.zip" in e for e in b.events)

    def full(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(backup, "backup", full)
    b.end_of_day(dt.datetime(2026, 11, 18, 15, 40, tzinfo=IST))   # must not raise
    assert any("data backup failed" in e for e in b.events)
