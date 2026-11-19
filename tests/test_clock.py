"""The PC's clock is compared with Zerodha's; a clock that is wrong holds new entries (review item, 19 Nov 2026).

The bot decides which candle has closed, the last-entry time and the square-off from the PC's clock. Two minutes
fast and a candle that is still forming is traded as if it had closed."""

import datetime as dt

import bot
import clockcheck
import config as C
from conftest import IST, bar, history


def test_skew_is_pc_time_minus_server_time():
    assert clockcheck.skew_seconds(lambda: (1000.0, 1125.0)) == 125.0          # the PC is 125 s ahead
    assert clockcheck.skew_seconds(lambda: (1000.0, 940.0)) == -60.0           # ... or a minute behind
    assert clockcheck.skew_seconds(lambda: None) is None                       # Zerodha could not be asked


def test_server_time_comes_from_the_date_header(monkeypatch):
    class Reply:
        headers = {"Date": "Thu, 19 Nov 2026 09:43:57 GMT"}
    monkeypatch.setattr(clockcheck._session, "head", lambda url, **kw: Reply())
    server, local = clockcheck.server_epoch()
    assert server == dt.datetime(2026, 11, 19, 9, 43, 57, tzinfo=dt.timezone.utc).timestamp() and local > 0
    monkeypatch.setattr(clockcheck._session, "head", lambda url, **kw: (_ for _ in ()).throw(OSError("offline")))
    assert clockcheck.server_epoch() is None


def breakout_day(day):
    return [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
            bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
            bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)]


def paper_bot(monkeypatch, skew):
    import data_yahoo
    hist, day = history()
    bars = hist + breakout_day(day)
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(C, "CLOCK_CHECK_SEC", 300)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars, bars[-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])
    asked = []

    def probe():
        asked.append(clock[0])
        return skew[0]
    monkeypatch.setattr(clockcheck, "skew_seconds", probe)
    return bot.Bot(), clock, asked


def test_a_wrong_clock_holds_new_entries_and_says_why(monkeypatch):
    skew = [125.0]
    b, clock, asked = paper_bot(monkeypatch, skew)
    b.cycle()
    assert b.state.positions == {}                                             # the breakout is NOT taken
    assert "125 seconds ahead of Zerodha's" in b.entry_hold()
    assert "skip: this PC's clock is 125 seconds ahead" in b.rows["SBIN"]["reason"]
    assert sum("No new entry is taken until it is right" in e for e in b.events) == 1
    clock[0] += dt.timedelta(seconds=60)
    b.cycle()
    assert len(asked) == 1                                                     # asked every 5 minutes, not every round
    assert sum("No new entry is taken until it is right" in e for e in b.events) == 1      # and said once


def test_entries_resume_when_the_clock_is_right_again(monkeypatch):
    skew = [-90.0]
    b, clock, asked = paper_bot(monkeypatch, skew)
    b.cycle()
    assert "90 seconds behind Zerodha's" in b.entry_hold()
    skew[0] = 0.4
    clock[0] += dt.timedelta(seconds=300)
    b.cycle()
    assert b.entry_hold() is None and any("agrees with Zerodha's again" in e for e in b.events)


def test_a_right_clock_changes_nothing(monkeypatch):
    b, clock, asked = paper_bot(monkeypatch, [1.2])
    b.cycle()
    assert "SBIN" in b.state.positions and b.entry_hold() is None and b.clock_skew == 1.2


def test_when_zerodha_cannot_be_asked_nothing_is_held(monkeypatch):
    b, clock, asked = paper_bot(monkeypatch, [None])
    b.cycle()
    assert "SBIN" in b.state.positions and b.entry_hold() is None


def test_a_wrong_clock_does_not_stop_positions_being_managed(monkeypatch):
    skew = [0.0]
    b, clock, asked = paper_bot(monkeypatch, skew)
    b.cycle()
    assert "SBIN" in b.state.positions
    skew[0] = 400.0
    clock[0] = clock[0].replace(hour=15, minute=11)                            # past the square-off
    b.cycle()
    assert b.clock_hold and "SBIN" not in b.state.positions                    # still squared off


def test_the_check_is_off_in_the_test_suite_and_in_the_replay():
    assert C.CLOCK_CHECK_SEC == 0                                              # set by conftest for every test
    src = open(bot.__file__.replace("bot.py", "paper_replay.py"), encoding="utf-8").read()
    assert 'os.environ["CLOCK_CHECK_SEC"] = "0"' in src
