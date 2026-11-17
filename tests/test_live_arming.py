"""Real money needs BOTH Windows settings (DRY_RUN=false, CONFIRM_LIVE=YES) AND today's arming on the dashboard.
Without the arming the bot trades paper, whoever starts it: the 9:00 task, a restart, or by hand."""

import datetime as dt
import json
import os

import pytest

import bot
import config as C
import livemode
from conftest import IST

NOW = dt.datetime(2026, 11, 17, 8, 55, tzinfo=IST)


def write_arm(**over):
    arm = {"date": NOW.date().isoformat(), "armed_at": NOW.isoformat(timespec="seconds"),
           "login_at": (NOW - dt.timedelta(minutes=3)).isoformat(timespec="seconds"), "nonce": "a" * 32}
    arm.update(over)
    with open(livemode.arm_path(), "w", encoding="utf-8") as f:
        json.dump(arm, f)


@pytest.fixture
def env_live(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    monkeypatch.setattr(bot, "now_ist", lambda: NOW)
    monkeypatch.setattr(livemode, "_now", lambda: NOW)


def test_settings_off_is_paper_without_a_note():
    assert livemode.bot_mode(False, NOW) == (False, None)
    write_arm()
    assert livemode.bot_mode(False, NOW) == (False, None)   # arming alone never makes it live


def test_settings_on_but_not_armed_is_paper_and_says_why():
    live, note = livemode.bot_mode(True, NOW)
    assert not live and "not armed today" in note and "PAPER" in note


def test_settings_on_and_armed_today_is_live():
    write_arm()
    assert livemode.bot_mode(True, NOW) == (True, None)


@pytest.mark.parametrize("over, why", [
    ({"date": "2026-11-16", "armed_at": "2026-11-16T15:00:00+05:30"}, "not today"),
    ({"mode": "live"}, "unexpected fields"),          # nothing else can ride along in the file
    ({"nonce": "not-hex"}, "bad code"),
    ({"armed_at": "yesterday"}, "bad time"),
    ({"armed_at": (NOW + dt.timedelta(hours=2)).isoformat()}, "future"),
])
def test_bad_or_old_arming_is_paper(over, why):
    write_arm(**over)
    live, note = livemode.bot_mode(True, NOW)
    assert not live and why in note


def test_unreadable_arming_is_paper():
    with open(livemode.arm_path(), "w") as f:
        f.write("{not json")
    assert livemode.bot_mode(True, NOW)[0] is False


def test_bot_with_live_settings_but_no_arming_trades_paper(env_live):
    b = bot.Bot()
    assert (b.live, b.mode) == (False, "paper") and b.state.path.endswith("state_paper.json")
    assert isinstance(b.broker, bot.PaperBroker) and b.ensure_live_broker() and b.kite is None


def test_bot_with_live_settings_and_todays_arming_is_live(env_live):
    write_arm()
    b = bot.Bot()
    assert (b.live, b.mode) == (True, "live") and b.state.path.endswith("state_live.json")


def test_yesterdays_arming_does_not_carry_over(env_live, monkeypatch):
    write_arm()
    tomorrow = NOW + dt.timedelta(days=1)
    monkeypatch.setattr(livemode, "_now", lambda: tomorrow)
    assert bot.Bot().live is False


def test_disarm_and_log(env_live):
    write_arm()
    assert livemode.disarm("test") and not os.path.exists(livemode.arm_path())
    assert not livemode.disarm("again")
    log = open(os.path.join(C.DATA_DIR, "logs", "mode_changes.log"), encoding="utf-8").read()
    assert "disarmed - test" in log
