"""Pinned limits (pins.py, config.apply_pins): a limit you pin holds through restarts, updates and a lost Windows
setting, and a pin can only ever make the bot stricter."""

import json
import os

import pytest

import buildinfo
import config as C
import pins
import web


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """Built-in values, an empty data folder, and nothing pinned; everything is put back afterwards."""
    monkeypatch.setattr(C, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(C, "PINNED", {})
    for name, value in (("MAX_TRADES_PER_DAY", 4), ("COOLDOWN_DAYS", 2), ("RISK_PER_TRADE_PCT", 0.5),
                        ("MAX_OPEN_POSITIONS", 3), ("MAX_DAILY_LOSS_PCT", 2.0), ("LOSS_STREAK_LIMIT", 3),
                        ("MAX_POSITION_PCT", 25.0), ("MAX_EXPOSURE_PCT", 75.0)):
        monkeypatch.setattr(C, name, value)
    return tmp_path


def write(tmp, data):
    with open(os.path.join(str(tmp), C.PINS_FILE), "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def test_a_pin_holds_when_the_windows_setting_is_gone(fresh):
    # the settings are back at the built-in 4 and 2 (as after a lost Windows setting or on a new PC)
    write(fresh, {"MAX_TRADES_PER_DAY": 1, "COOLDOWN_DAYS": 3})
    got = C.apply_pins()
    assert (C.MAX_TRADES_PER_DAY, C.COOLDOWN_DAYS) == (1, 3)
    assert got["MAX_TRADES_PER_DAY"] == {"pin": 1, "setting": 4, "used": 1}
    assert got["COOLDOWN_DAYS"] == {"pin": 3, "setting": 2, "used": 3}


def test_a_pin_can_only_make_a_limit_stricter(fresh, monkeypatch):
    write(fresh, {"MAX_TRADES_PER_DAY": 10, "COOLDOWN_DAYS": 1, "RISK_PER_TRADE_PCT": 2.0, "MAX_DAILY_LOSS_PCT": 5})
    C.apply_pins()
    assert (C.MAX_TRADES_PER_DAY, C.COOLDOWN_DAYS, C.RISK_PER_TRADE_PCT, C.MAX_DAILY_LOSS_PCT) == (4, 2, 0.5, 2.0)
    # and a Windows setting stricter than the pin stays
    monkeypatch.setattr(C, "MAX_TRADES_PER_DAY", 1)
    write(fresh, {"MAX_TRADES_PER_DAY": 2})
    C.apply_pins()
    assert C.MAX_TRADES_PER_DAY == 1 and C.PINNED["MAX_TRADES_PER_DAY"]["used"] == 1


def test_a_bad_file_or_value_changes_nothing(fresh):
    for bad in ("{not json", json.dumps([1, 2]), json.dumps({"MAX_TRADES_PER_DAY": "many", "COOLDOWN_DAYS": -3,
                                                             "WATCHLIST": ["X"], "DRY_RUN": False, "CONFIRM_LIVE": "YES"})):
        write(fresh, bad)
        assert C.apply_pins() == {} and (C.MAX_TRADES_PER_DAY, C.COOLDOWN_DAYS) == (4, 2)
    os.remove(os.path.join(str(fresh), C.PINS_FILE))
    assert C.apply_pins() == {}
    # nothing but risk limits can be pinned: never the trading mode, the watchlist or a key
    assert not {"DRY_RUN", "CONFIRM_LIVE", "WATCHLIST", "KITE_API_KEY"} & set(C.PINNABLE)


def test_set_show_and_clear(fresh, capsys):
    assert pins.report() == ["No limit is pinned."]
    pins.set_pin("MAX_TRADES_PER_DAY", "1")
    pins.set_pin("COOLDOWN_DAYS", 3)
    saved = json.load(open(pins.path(), encoding="utf-8"))
    assert (saved["MAX_TRADES_PER_DAY"], saved["COOLDOWN_DAYS"]) == (1, 3) and "_changed" in saved
    assert pins.report() == ["MAX_TRADES_PER_DAY: pinned 1, normal setting 4 -> in force 1",
                             "COOLDOWN_DAYS: pinned 3, normal setting 2 -> in force 3"]
    for name, value in (("DRY_RUN", "false"), ("MAX_TRADES_PER_DAY", "0"), ("MAX_TRADES_PER_DAY", "lots")):
        with pytest.raises(ValueError):
            pins.set_pin(name, value)
    assert pins.main(["pins.py", "set", "CONFIRM_LIVE", "YES"]) == 1 and "Not pinned" in capsys.readouterr().out
    pins.clear_pin("COOLDOWN_DAYS")
    assert "COOLDOWN_DAYS" not in json.load(open(pins.path(), encoding="utf-8"))


def test_the_kavach_page_marks_a_pinned_limit(fresh, monkeypatch):
    monkeypatch.setattr(web, "read_status", lambda: {})
    write(fresh, {"MAX_TRADES_PER_DAY": 1})
    C.apply_pins()
    page = web.render_kavach()
    assert "pinned by you" in page and "survives updates, restarts and a lost Windows setting" in page
    assert page.count("pinned by you") == 1


def test_nothing_in_the_bot_or_dashboard_writes_the_pins():
    for name in ("bot.py", "web.py", "broker.py", "config.py", "kavach.py", "risk.py"):
        code = open(os.path.join(os.path.dirname(C.__file__), name), encoding="utf-8").read()
        assert "set_pin(" not in code and "import pins" not in code, name
    assert "pins.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}
