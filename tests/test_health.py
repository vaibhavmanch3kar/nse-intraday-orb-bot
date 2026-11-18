"""Health: one list of read-only checks; a check that turns bad (or recovers) gets one alert line and one popup;
the card and /api/health show the same list and carry no secret."""

import datetime as dt
import json
import os

import pytest

import buildinfo
import config as C
import health
import watchdog
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 11, 18, 11, 0, tzinfo=IST)            # a Wednesday, market open
NIGHT = dt.datetime(2026, 11, 18, 20, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    health.reset()
    monkeypatch.setattr(health, "logged_in", lambda: True)
    monkeypatch.setattr(C, "LAST_ENTRY", "12:00")
    yield
    health.reset()


def good(now=NOW, **over):
    s = {"time": (now - dt.timedelta(seconds=20)).isoformat(), "mode": "live", "equity": 5000.0, "day_pnl": -10.0,
         "feed": {"source": "kite", "newest_candle": "10:45", "stocks_ok": 12, "stocks": 12, "problems": {}, "errors": []},
         "recon": {"at": (now - dt.timedelta(seconds=60)).isoformat(), "ok": True, "problems": []},
         "entry_hold": None, "paused": False}
    s.update(over)
    return s


def beat(tmp, now=NOW, ago=5):
    with open(os.path.join(str(tmp), "heartbeat.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": (now - dt.timedelta(seconds=ago)).isoformat(), "pid": 4242}, fh)


def states(rows):
    return {r["key"]: r["state"] for r in rows}


def run(tmp, now=NOW, **over):
    return states(health.checks(now, good(now, **over), login=over.pop("login", True) if "login" in over else True,
                                disk_free_gb=40.0))


# ---------------------------------------------------------------- the checks
def test_everything_working_reads_ok(isolated):
    beat(isolated)
    rows = health.checks(NOW, good(), login=True, disk_free_gb=40.0)
    st = states(rows)
    assert [r["key"] for r in rows] == ["bot", "heartbeat", "watchdog", "feed", "login", "recon", "entries",
                                        "day_loss", "crashes", "backup", "disk"]
    assert {k: v for k, v in st.items() if k != "backup"} == {k: "ok" for k in st if k != "backup"}
    assert st["backup"] == "warn"                                 # no backup has been made in this empty folder
    assert "REAL MONEY" in rows[0]["detail"] and health.overall(rows) == "warn"


def test_a_bot_that_is_down_in_market_hours_is_bad_but_a_deliberate_stop_is_not(isolated):
    old = (NOW - dt.timedelta(minutes=20)).isoformat()
    st = states(health.checks(NOW, good(time=old), login=True, disk_free_gb=40.0))
    assert st["bot"] == "bad" and st["heartbeat"] == "off" and st["feed"] == "off" and st["entries"] == "off"
    watchdog.mark_stopped("Stop button", NOW)
    assert states(health.checks(NOW, good(time=old), login=True, disk_free_gb=40.0))["bot"] == "warn"
    closed = states(health.checks(NIGHT, good(NIGHT, time=old), login=True, disk_free_gb=40.0))
    assert closed["bot"] == "off"                                 # after the close a stopped bot is normal


def test_a_stuck_bot_shows_on_the_heartbeat(isolated):
    beat(isolated, ago=health.BEAT_WARN_SEC + 30)
    assert states(health.checks(NOW, good(), login=True, disk_free_gb=40.0))["heartbeat"] == "warn"
    beat(isolated, ago=watchdog.HANG_SEC + 30)
    assert states(health.checks(NOW, good(), login=True, disk_free_gb=40.0))["heartbeat"] == "bad"


def test_feed_login_books_and_holds(isolated):
    beat(isolated)

    def st(login=True, **over):
        return states(health.checks(NOW, good(**over), login=login, disk_free_gb=40.0))

    feed = dict(good()["feed"])
    assert st(feed=dict(feed, live_needs_kite=True))["feed"] == "bad"
    assert st(feed=dict(feed, stocks_ok=10, problems={"ITC": "gap"}))["feed"] == "warn"
    assert st(login=False)["login"] == "bad" and st(login=False, mode="paper")["login"] in ("warn", "off")
    assert st(recon={"at": NOW.isoformat(), "ok": False, "problems": ["SBIN: bot 5, Zerodha 0"]})["recon"] == "bad"
    late = (NOW - dt.timedelta(seconds=4 * C.RECON_EVERY_SEC)).isoformat()
    assert st(recon={"at": late, "ok": True, "problems": []})["recon"] == "warn"
    assert st(mode="paper")["recon"] == "off"
    assert st(entry_hold="positions do not match Zerodha")["entries"] == "bad"
    assert st(paused=True)["entries"] == "warn" and st(cooldown_until="2026-11-20")["entries"] == "warn"
    after = dt.datetime(2026, 11, 18, 13, 0, tzinfo=IST)
    beat(isolated, after)
    assert states(health.checks(after, good(after), login=True, disk_free_gb=40.0))["entries"] == "off"


def test_daily_loss_crashes_backup_and_disk(isolated, monkeypatch):
    beat(isolated)

    def st(disk=40.0, **over):
        return states(health.checks(NOW, good(**over), login=True, disk_free_gb=disk))

    limit = 5000.0 * C.MAX_DAILY_LOSS_PCT / 100
    assert st(day_pnl=-0.5 * limit)["day_loss"] == "ok" and st(day_pnl=-0.8 * limit, equity=5000.0 - 0.8 * limit)["day_loss"] == "warn"
    assert st(day_pnl=-1.1 * limit, equity=5000.0 - 1.1 * limit)["day_loss"] == "bad" and st(equity=None)["day_loss"] == "off"
    assert st(disk=0.4)["disk"] == "warn"
    os.makedirs(watchdog.logs_dir(), exist_ok=True)
    open(os.path.join(watchdog.logs_dir(), "crash_2026-11-18_101500.txt"), "w").close()
    open(os.path.join(watchdog.logs_dir(), "crash_2026-11-17_101500.txt"), "w").close()
    rows = health.checks(NOW, good(), login=True, disk_free_gb=40.0)
    crash = next(r for r in rows if r["key"] == "crashes")
    assert crash["state"] == "warn" and crash["detail"].startswith("1 today")
    monkeypatch.setattr(health.backup, "backups", lambda: [("data_2026-11-17.zip", 10)])
    os.makedirs(health.backup.backup_dir(), exist_ok=True)
    p = os.path.join(health.backup.backup_dir(), "data_2026-11-17.zip")
    open(p, "w").close()
    stamp = (NOW - dt.timedelta(days=1)).timestamp()
    os.utime(p, (stamp, stamp))
    back = next(r for r in health.checks(NOW, good(), login=True, disk_free_gb=40.0) if r["key"] == "backup")
    assert back["state"] == "ok" and back["detail"] == "made yesterday"
    stamp = (NOW - dt.timedelta(days=9)).timestamp()
    os.utime(p, (stamp, stamp))
    assert next(r for r in health.checks(NOW, good(), login=True, disk_free_gb=40.0) if r["key"] == "backup")["state"] == "warn"


def test_watchdog_record_is_reported(isolated):
    beat(isolated)
    d = watchdog.load(NOW)
    d["restarts"] = 2
    watchdog._save(d)
    assert states(health.checks(NOW, good(), login=True, disk_free_gb=40.0))["watchdog"] == "warn"
    watchdog.add_event(d, "gave_up", "not starting it again", NOW)
    watchdog._save(d)
    assert states(health.checks(NOW, good(), login=True, disk_free_gb=40.0))["watchdog"] == "bad"


# ---------------------------------------------------------------- alerts
def row(key, state, detail="x"):
    return {"key": key, "name": key.title(), "state": state, "detail": detail}


def test_one_alert_line_per_change_and_a_recovery_line(isolated):
    assert health.observe(NOW, [row("feed", "ok"), row("bot", "off")]) == []
    assert not os.path.exists(health.alerts_path(NOW.date()))                # nothing wrong: nothing written
    t1 = NOW + dt.timedelta(minutes=1)
    first = health.observe(t1, [row("feed", "bad", "no Kite prices"), row("bot", "off")])
    assert [(a["key"], a["state"], a["was"]) for a in first] == [("feed", "bad", "ok")]
    assert health.observe(t1 + dt.timedelta(minutes=1), [row("feed", "bad"), row("bot", "ok")]) == []    # no repeat
    back = health.observe(t1 + dt.timedelta(minutes=2), [row("feed", "ok"), row("bot", "ok")])
    assert [(a["key"], a["state"], a["was"]) for a in back] == [("feed", "ok", "bad")]
    worse = health.observe(t1 + dt.timedelta(minutes=3), [row("feed", "warn"), row("bot", "ok")])
    assert worse[0]["state"] == "warn"
    assert [a["state"] for a in health.alerts(NOW.date())] == ["bad", "ok", "warn"]
    n = health.notices(NOW)
    assert [x["tone"] for x in n] == ["dn", "up", "warn"] and len({x["key"] for x in n}) == 3
    assert n[0]["title"] == "Feed: NOT working" and n[0]["body"] == "no Kite prices" and n[1]["title"] == "Feed: working again"
    assert sorted(os.listdir(str(isolated))) == ["logs"]                      # only the alert history was written


def test_many_open_pages_do_not_recheck_more_often_than_the_interval(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(health, "checks", lambda now=None, *a, **k: calls.append(now) or [row("feed", "ok")])
    for sec in (0, 3, 6, 9, health.OBSERVE_EVERY_SEC + 1):
        health.observe(NOW + dt.timedelta(seconds=sec))
    assert len(calls) == 2


def test_a_broken_check_never_breaks_the_page(isolated, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(health, "checks", boom)
    assert health.observe(NOW) == []


# ---------------------------------------------------------------- the card and the address
def test_card_is_closed_when_all_is_well_and_open_with_the_reason_when_not(isolated, monkeypatch):
    monkeypatch.setattr(health, "checks", lambda *a, **k: [row("feed", "ok", "12 of 12 stocks"), row("bot", "off", "closed")])
    card = web.health_card({})
    assert "<details class='card flush' id='health'>" in card and "✓ all working" in card and "No alert today." in card
    monkeypatch.setattr(health, "checks", lambda *a, **k: [row("feed", "bad", "no Kite prices"), row("disk", "warn", "low")])
    card = web.health_card({})
    assert "id='health' open>" in card and "✕ 1 NOT working" in card and "1 need a look" in card
    assert "no Kite prices" in card and "Alerts today" in card and "✕ NOT working" in card and "! needs a look" in card
    assert "health" in web.live_regions("paper", {}) and "data-region='health'" in web.render()


def test_report_has_states_and_sentences_only(isolated, monkeypatch):
    monkeypatch.setenv("KITE_API_SECRET", "s3cr3t-value-for-test")
    beat(isolated)
    rep = health.report(NOW, good())
    assert set(rep) == {"at", "overall", "checks", "bad", "warn"} and rep["overall"] in ("ok", "warn", "bad")
    text = json.dumps(rep)
    assert "s3cr3t" not in text and "access_token" not in text and all(set(c) == {"key", "name", "state", "detail"} for c in rep["checks"])
    src = open(web.__file__, encoding="utf-8").read()
    assert 'u.path == "/api/health"' in src and "watchdog_notices() + health.notices()" in src


def test_health_is_read_only_and_not_part_of_the_bot():
    code = open(health.__file__, encoding="utf-8").read()
    for word in ("import broker", "kiteconnect", "place_order", "start_bot", "kill(", "os.remove", "mark_stopped"):
        assert word not in code, word
    assert "health.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}
