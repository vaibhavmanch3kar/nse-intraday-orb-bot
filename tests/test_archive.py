"""Advisor Phase 2: the candle archive keeps finished days, prefers Kite, and never breaks the bot."""

import datetime as dt
import os

import archive
import bot
import config as C
import marketdata
from conftest import IST, bar, flat_day

D1, D2, D3 = dt.date(2026, 11, 11), dt.date(2026, 11, 12), dt.date(2026, 11, 13)


def at(day, h, m):
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def test_only_finished_sessions_are_kept():
    bars = flat_day(D2) + flat_day(D3)
    during = archive.complete_days(bars, at(D3, 14, 0))
    assert list(during) == [D2]                       # today is still trading
    after = archive.complete_days(bars, at(D3, 15, 35))
    assert list(after) == [D2, D3]


def test_save_and_load_round_trip():
    bars = flat_day(D1, price=100) + flat_day(D2, price=101)
    res = archive.save({"SBIN": bars, "^NSEI": flat_day(D2, price=25000)}, "yahoo", at(D3, 9, 0))
    assert res["days"] == [D1, D2] and res["bars"] == 25 * 3
    assert archive.days() == [D1, D2]
    got = archive.load()
    assert set(got) == {"SBIN", "^NSEI"}
    assert got["SBIN"] == bars                        # same values, same IST times, oldest first
    assert archive.load(["SBIN"], start=D2)["SBIN"] == bars[25:]
    assert not [f for f in os.listdir(archive.archive_dir()) if f.endswith(".tmp")]


def test_kite_is_never_replaced_by_yahoo():
    now = at(D3, 9, 0)
    archive.save({"SBIN": flat_day(D2, price=100)}, "kite", now)
    res = archive.save({"SBIN": flat_day(D2, price=999)}, "yahoo", now)
    assert res["kept"] == 1 and res["days"] == []
    assert archive.load()["SBIN"][0]["c"] == 100


def test_yahoo_day_is_upgraded_to_kite_and_fuller_day_wins():
    now = at(D3, 9, 0)
    archive.save({"SBIN": flat_day(D2, price=100)[:10]}, "yahoo", now)       # partial day
    archive.save({"SBIN": flat_day(D2, price=101)}, "yahoo", now)            # fuller -> replaces
    assert len(archive.load()["SBIN"]) == 25
    res = archive.save({"SBIN": flat_day(D2, price=101)[:20]}, "yahoo", now)  # shorter -> kept
    assert res["kept"] == 1 and len(archive.load()["SBIN"]) == 25
    archive.save({"SBIN": flat_day(D2, price=102)[:20]}, "kite", now)        # Kite always wins
    got = archive.load()["SBIN"]
    assert len(got) == 20 and got[0]["c"] == 102


def test_other_symbols_in_a_day_file_are_kept():
    now = at(D3, 9, 0)
    archive.save({"SBIN": flat_day(D2)}, "yahoo", now)
    archive.save({"INFY": flat_day(D2)}, "yahoo", now)
    assert set(archive.load()) == {"SBIN", "INFY"}


def test_archive_symbols_include_index_filter_indexes():
    syms = archive.archive_symbols(["SBIN", "HDFCBANK", "INFY"])
    assert syms[:3] == ["SBIN", "HDFCBANK", "INFY"]
    assert C.INDEX_DEFAULT in syms and len(syms) == len(set(syms))
    for s in ("SBIN", "HDFCBANK", "INFY"):
        assert archive.strategy.index_for(s) in syms


def test_fetch_and_save_reports_failures(monkeypatch):
    def fake_many(symbols, interval_min, range_, workers):
        return {s: (flat_day(D2) + flat_day(D3), 100.0, None) if s == "SBIN"
                else marketdata.DataError(f"{s}: down") for s in symbols}
    monkeypatch.setattr(marketdata, "fetch_many", fake_many)
    res = archive.fetch_and_save("5d", symbols=["SBIN", "INFY"], now=at(D3, 12, 0))
    assert res["days"] == [D2] and res["failed"] == ["INFY"] and res["source"] == "yahoo"


def test_bot_end_of_day_saves_and_never_raises(monkeypatch):
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(marketdata, "fetch_many",
                        lambda symbols, *a, **k: {s: (flat_day(D3), 100.0, None) for s in symbols})
    b = bot.Bot()
    notes = []
    monkeypatch.setattr(b, "note", lambda msg, level=None: notes.append(msg))
    b.end_of_day(at(D3, 15, 36))
    assert archive.days() == [D3] and "saved 1 day" in notes[-1]

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(marketdata, "fetch_many", boom)
    b.end_of_day(at(D3, 15, 40))                       # must not raise
    assert "candle archive failed" in notes[-1]


def test_archive_bars_feed_the_strategy_unchanged():
    """Bars loaded back from the archive give the same signal as the original bars."""
    import strategy
    from conftest import history
    hist, day = history()
    session = [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
               bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
               bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)]
    bars = hist + session
    archive.save({"SBIN": bars}, "kite", at(day, 15, 35))
    back = archive.load()["SBIN"]
    assert strategy.evaluate(back)["signal"] == strategy.evaluate(bars)["signal"] == "LONG"
