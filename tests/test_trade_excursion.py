"""The trade log's best_price / worst_price (and best_r / worst_r): how far each trade went, for and against,
while it was open. Record-keeping only: the columns never change an entry, an exit or a size."""

import csv
import datetime as dt
import os

import bot
import config as C
from conftest import IST, bar, history


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- the two helpers
def test_a_long_remembers_its_highest_and_lowest_price():
    pos = {"direction": "LONG"}
    bot.track_excursion(pos, 100.0)
    bot.track_excursion(pos, 101.5, 99.5)
    bot.track_excursion(pos, 100.8)
    assert pos["best_price"] == 101.5 and pos["worst_price"] == 99.5


def test_a_short_remembers_its_lowest_price_as_best():
    pos = {"direction": "SHORT"}
    bot.track_excursion(pos, 100.0)
    bot.track_excursion(pos, 101.5, 98.0)
    assert pos["best_price"] == 98.0 and pos["worst_price"] == 101.5


def test_a_missing_price_changes_nothing():
    pos = {"direction": "LONG", "best_price": 101.0, "worst_price": 99.0}
    bot.track_excursion(pos, None)
    bot.track_excursion(pos, 0)
    bot.track_excursion(pos)
    assert pos == {"direction": "LONG", "best_price": 101.0, "worst_price": 99.0}


def test_the_closed_row_includes_the_exit_price_and_both_in_r():
    row = bot.with_excursion({"direction": "LONG", "entry": 100.0, "stop": 98.0, "exit": 97.5,
                              "best_price": 103.0, "worst_price": 99.0})
    assert row["best_price"] == 103.0 and row["worst_price"] == 97.5          # the exit was the worst price
    assert row["best_r"] == 1.5 and row["worst_r"] == -1.25
    short = bot.with_excursion({"direction": "SHORT", "entry": 100.0, "stop": 102.0, "exit": 99.0,
                                "best_price": 97.0, "worst_price": 101.0})
    assert short["best_r"] == 1.5 and short["worst_r"] == -0.5


def test_a_position_that_was_never_tracked_keeps_the_columns_empty():
    row = {"direction": "LONG", "entry": 100.0, "stop": 98.0, "exit": 101.0}
    assert bot.with_excursion(row) == row
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    bot.log_trade({**row, "symbol": "SBIN", "qty": 1, "net": 1.0, "reason": "time stop"}, "paper")
    r = read(path)[0]
    assert r["best_price"] == "" and r["worst_price"] == "" and r["best_r"] == "" and r["worst_r"] == ""


def test_the_columns_are_the_last_four_of_the_trade_log():
    assert bot.TRADE_COLS[-4:] == ["best_price", "worst_price", "best_r", "worst_r"] == bot.EXCURSION_COLS


# ---------------------------------------------------------------- through the real paper loop
def paper_bot(monkeypatch, bars_ref, clock):
    import data_yahoo
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (bars_ref[0], bars_ref[0][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: clock[0])
    return bot.Bot()


def breakout_day(day):
    """09:15 range 100-101, 09:30 inside, 09:45 LONG breakout (stop at the range low, 100.0)."""
    return [bar(day, "09:15", 100.2, 101.0, 100.0, 100.5, 5000),
            bar(day, "09:30", 100.5, 100.6, 100.4, 100.5, 1000),
            bar(day, "09:45", 100.5, 101.3, 100.5, 101.2, 3000)]


def test_a_paper_trade_logs_how_far_it_went_before_the_stop(monkeypatch):
    hist, day = history()
    bars_ref = [hist + breakout_day(day)]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    b = paper_bot(monkeypatch, bars_ref, clock)
    b.cycle()
    pos = b.state.positions["SBIN"]
    entry = pos["entry"]
    assert pos["best_price"] == pos["worst_price"] == entry                    # tracked from the fill

    # the next candle runs up to 102.5 and dips to 101.0 without touching the stop or the target
    bars_ref[0] = bars_ref[0] + [bar(day, "10:00", 101.2, 102.5, 101.0, 102.0, 2000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()
    pos = b.state.positions["SBIN"]
    assert pos["best_price"] == 102.5 and pos["worst_price"] == 101.0
    stop, qty = pos["stop"], pos["qty"]

    # then a candle that spikes to 103.0 and falls through the stop to 99.0: the stop is taken first, and what
    # that candle did (its 103.0 high, its 99.0 low) is not counted beyond the exit price
    bars_ref[0] = bars_ref[0] + [bar(day, "10:15", 102.0, 103.0, 99.0, 99.5, 2000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 30, 30), tzinfo=IST)
    b.cycle()
    assert "SBIN" not in b.state.positions
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    with open(path, encoding="utf-8") as f:
        assert f.readline().strip().split(",") == bot.TRADE_COLS
    r = read(path)[0]
    exit_price = float(r["exit"])                                             # the stop level less paper slippage
    assert r["reason"] == "stop" and abs(exit_price - stop * (1 - C.SLIPPAGE_PCT / 100)) < 0.006
    assert float(r["best_price"]) == 102.5 and float(r["worst_price"]) == exit_price
    risk = entry - stop
    assert abs(float(r["best_r"]) - (102.5 - entry) / risk) < 0.006
    assert abs(float(r["worst_r"]) - (exit_price - entry) / risk) < 0.006 and float(r["worst_r"]) <= -1.0
    assert float(r["stop"]) == stop and int(r["qty"]) == qty                   # nothing else about the trade moved


def test_tracking_survives_a_restart(monkeypatch):
    hist, day = history()
    bars_ref = [hist + breakout_day(day)]
    clock = [dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST)]
    b = paper_bot(monkeypatch, bars_ref, clock)
    b.cycle()
    bars_ref[0] = bars_ref[0] + [bar(day, "10:00", 101.2, 102.5, 101.0, 102.0, 2000)]
    clock[0] = dt.datetime.combine(day, dt.time(10, 15, 30), tzinfo=IST)
    b.cycle()
    again = bot.Bot()                                                          # the bot after a restart
    pos = again.state.positions["SBIN"]
    assert pos["best_price"] == 102.5 and pos["worst_price"] == 101.0


def test_an_old_log_gets_the_new_columns_without_losing_rows():
    old_cols = bot.TRADE_COLS[:-4]
    path = os.path.join(C.DATA_DIR, "trades_live.csv")
    old = {c: "" for c in old_cols}
    old.update(closed="2026-11-18 13:15:49+05:30", symbol="KOTAKBANK", direction="LONG", qty="2", entry="500.95",
               exit="499.5", stop="494.1", net="-3.96", r="-0.29", reason="time stop")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=old_cols)
        w.writeheader()
        w.writerow(old)
    bot.log_trade({"closed": "2026-11-20 10:30:00+05:30", "symbol": "SBIN", "direction": "LONG", "qty": 3,
                   "entry": 100.0, "exit": 101.0, "stop": 99.0, "net": 2.5, "reason": "time stop",
                   "best_price": 101.8, "worst_price": 99.6}, "live")
    rows = read(path)
    assert len(rows) == 2 and {k: rows[0][k] for k in old_cols} == old and rows[0]["best_price"] == ""
    assert float(rows[1]["best_price"]) == 101.8 and float(rows[1]["best_r"]) == 1.8 and float(rows[1]["worst_r"]) == -0.4


# ---------------------------------------------------------------- record-keeping can never stop the loop
def test_bad_values_never_raise_and_the_trade_is_still_logged():
    pos = {"direction": "LONG", "best_price": 101.0, "worst_price": 99.0}
    bot.track_excursion(pos, "not a price")
    bot.track_excursion({}, 100.0)                                             # no direction on record
    assert pos == {"direction": "LONG", "best_price": 101.0, "worst_price": 99.0}
    broken = {"direction": "LONG", "entry": 100.0, "exit": 101.0, "best_price": 101.5, "worst_price": 99.5,
              "symbol": "SBIN", "qty": 1, "net": 1.0, "reason": "time stop"}   # no stop on record
    assert bot.with_excursion(broken) == broken
    bot.log_trade(broken, "paper")
    r = read(os.path.join(C.DATA_DIR, "trades_paper.csv"))[0]
    assert r["symbol"] == "SBIN" and r["net"] == "1.0" and r["best_r"] == ""
