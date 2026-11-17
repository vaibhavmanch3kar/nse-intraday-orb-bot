"""REAL MONEY trades are sized from the actual Zerodha balance, never from the bot's own made-up record.

Before this fix a first real-money start sized trades from the default ₹1,00,000 whatever was in Zerodha.
Now: a new real-money record starts at ₹0; the bot reads the balance (read-only) before any trade, uses the smaller
of today's opening cash and the cash available now (collateral excluded), and opens no new trades if it can't."""

import datetime as dt
import json
import os
import time

import pytest

import bot
import config as C
import data_kite
import data_yahoo
import livemode
import web
from conftest import IST, bar, history

SECRET = "tok-SECRET-0123456789"


class FakeKite:
    def __init__(self, opening=5000.0, live=5000.0, collateral=50000.0, error=None):
        self.avail = {"opening_balance": opening, "live_balance": live, "collateral": collateral, "cash": opening}
        self.error, self.calls = error, 0

    def margins(self, segment=None):
        self.calls += 1
        assert segment == "equity"
        if self.error:
            raise self.error
        return {"available": dict(self.avail), "net": self.avail["live_balance"] + self.avail["collateral"]}


def entry_day():
    hist, day = history()
    return hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
                   bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
                   bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000)], day


@pytest.fixture
def market(monkeypatch):
    bars, day = entry_day()
    ref = {"bars": bars, "now": dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST), "day": day}
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (ref["bars"], ref["bars"][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: ref["now"])
    monkeypatch.setattr(livemode, "_now", lambda: ref["now"])
    monkeypatch.setattr(data_kite, "candle_hold", lambda now=None: 0.0)
    return ref


@pytest.fixture
def live(market, monkeypatch):
    """A REAL MONEY bot (both settings + today's arming). PaperBroker stands in for Zerodha: no order is sent."""
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    livemode.arm(login_at=market["now"])
    b = bot.Bot()
    assert b.live
    return b


def test_new_real_money_record_starts_at_zero_and_trades_nothing_before_the_balance(live):
    assert live.state.d["cash"] == 0.0 and not live.balance_ok
    live.cycle()
    assert not live.state.positions and "balance not read" in live.rows["SBIN"]["reason"]


def test_trades_are_sized_from_the_real_balance(live, market):
    live.kite = FakeKite(opening=5000.0, live=5000.0)
    live.sync_live_balance(market["now"])
    assert live.balance_ok and live.state.d["cash"] == 5000.0
    live.cycle()
    pos = live.state.positions["SBIN"]
    notional, risk = pos["entry"] * pos["qty"], abs(pos["entry"] - pos["stop"]) * pos["qty"]
    assert notional <= 5000.0 * C.MAX_POSITION_PCT / 100                   # ≤ ₹1,250.00, not ₹25,000
    assert risk <= 5000.0 * C.RISK_PER_TRADE_PCT / 100                     # ≤ ₹25.00, not ₹500
    assert live.state.d["day_start_equity"] == 5000.0                      # daily loss limit ₹100.00
    old_qty, _ = __import__("risk").size_position(100000.0, pos["entry"], pos["stop"], "LONG")
    assert pos["qty"] < old_qty / 10                                       # the bug sized ~20x bigger


def test_uses_the_smaller_figure_and_never_collateral(live, market):
    live.kite = FakeKite(opening=6000.0, live=5000.0, collateral=90000.0)
    live.sync_live_balance(market["now"])
    assert live.state.d["cash"] == 5000.0
    live.kite = FakeKite(opening=3000.0, live=5000.0, collateral=90000.0)
    live.sync_live_balance(market["now"])
    assert live.state.d["cash"] == 3000.0


@pytest.mark.parametrize("kite, why", [
    (FakeKite(error=OSError(f"proxy said {SECRET}")), "could not read your Zerodha balance (OSError)"),
    (FakeKite(opening=0.0, live=0.0), "Zerodha balance is ₹0.00"),
    (FakeKite(opening=-50.0, live=-50.0), "Zerodha balance is ₹-50.00"),
])
def test_no_balance_means_no_new_trades(live, market, kite, why):
    live.kite = kite
    live.sync_live_balance(market["now"])
    assert not live.balance_ok and live.balance_problem == why and live.state.d["cash"] == 0.0
    live.cycle()
    assert not live.state.positions and why in live.rows["SBIN"]["reason"]
    live.write_status(market["now"])
    s = json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))
    assert s["balance"]["ok"] is False and SECRET not in json.dumps(s)
    s["time"] = dt.datetime.now(IST).isoformat()
    notice = web.live_notice(s, True)
    assert "REAL MONEY: no new trades" in notice and SECRET not in notice
    log = open(os.path.join(C.DATA_DIR, "logs", "mode_changes.log"), encoding="utf-8").read()
    assert "balance: NOT USABLE" in log and SECRET not in log


def test_retries_once_a_minute_then_trades(live, market):
    live.kite = FakeKite(error=OSError("down"))
    live.sync_live_balance(market["now"])
    live.kite = FakeKite()
    live.cycle()                                       # under a minute later: no new read yet
    assert live.kite.calls == 0 and not live.state.positions
    live.balance_tried = time.monotonic() - 61
    market["now"] = market["now"].replace(minute=5)
    live.last_bar.clear()
    live.cycle()
    assert live.kite.calls == 1 and live.balance_ok and "SBIN" in live.state.positions


def test_restart_with_open_positions_keeps_the_record(live, market):
    live.kite = FakeKite()
    live.sync_live_balance(market["now"])
    live.cycle()
    assert "SBIN" in live.state.positions
    nb = bot.Bot()                                     # restart mid-trade
    nb.kite = FakeKite(opening=5000.0, live=3700.0)    # Zerodha's figure now has margin blocked
    nb.sync_live_balance(market["now"])
    assert nb.kite.calls == 0 and nb.balance_ok and nb.state.d["cash"] == 5000.0


def test_open_positions_without_todays_read_block_new_trades(live, market):
    live.state.d["positions"]["XYZ"] = {"direction": "LONG", "qty": 1, "entry": 10.0, "stop": 9.0,
                                        "target": None, "opened": market["now"].isoformat()}
    live.kite = FakeKite()
    live.sync_live_balance(market["now"])
    assert not live.balance_ok and "not read before them" in live.balance_problem


def test_paper_never_reads_zerodha(market):
    b = bot.Bot()
    assert not b.live and b.balance_ok and b.state.d["cash"] == C.PAPER_START_INR
    b.kite = FakeKite(error=AssertionError("paper must not call Zerodha"))
    b.sync_live_balance(market["now"])
    b.cycle()
    assert b.kite.calls == 0 and "SBIN" in b.state.positions
    b.write_status(market["now"])
    assert json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))["balance"] is None


def test_dashboard_shows_the_balance(live, market):
    live.kite = FakeKite()
    live.sync_live_balance(market["now"])
    live.write_status(market["now"])
    s = json.load(open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8"))
    notice = web.balance_notice(s)
    assert "Balance from Zerodha: <b>₹5,000.00</b>" in notice and "(read 10:00)" in notice
    assert web.balance_notice({"mode": "paper", "balance": None}) == ""
