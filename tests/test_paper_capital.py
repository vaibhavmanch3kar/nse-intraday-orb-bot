"""Paper Account opening balance: validation, pending change, applied only by the paper bot at start-up,
past trades and P&L untouched, live state never affected."""

import datetime as dt
import json
import os

import pytest

import accounts
import bot
import capital
import config as C
import risk

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 11, 17, 9, 0, 5, tzinfo=IST)


def paper_state(**extra):
    s = risk.State(capital.paper_state_path(), C.PAPER_START_INR)
    s.d.update(extra)
    s.save()
    return s


def test_validate():
    assert capital.validate("50000") == 50000
    assert capital.validate("₹1,00,000") == 100000
    assert capital.validate(500000) == 500000
    for bad in ("abc", "", "9999", "100000001", "50000.50", "-20000"):
        with pytest.raises(ValueError):
            capital.validate(bad)
    assert set(capital.PRESETS) == {10_000, 25_000, 50_000, 100_000, 500_000}


def test_request_saves_pending_only():
    paper_state(cash=100500.0, realized_pnl=500.0)
    before = open(capital.paper_state_path(), encoding="utf-8").read()
    msg = capital.request(50000, now=NOW)
    assert "next start" in msg
    assert capital.load()["pending"]["amount"] == 50000
    assert open(capital.paper_state_path(), encoding="utf-8").read() == before   # money untouched until start
    assert "cancelled" in capital.request(100000)                                 # back to current = cancel
    assert capital.load()["pending"] is None
    assert "already" in capital.request(100000)                                   # nothing pending, same amount


def test_apply_moves_cash_by_the_difference_and_keeps_pnl():
    s = paper_state(cash=100500.0, realized_pnl=500.0, charges_paid=40.0, day_start_equity=100500.0)
    capital.request(50000, now=NOW)
    msg = capital.apply_pending(s, now=NOW)
    assert "₹1,00,000" not in msg and "₹100,000 to ₹50,000" in msg
    d = json.load(open(capital.paper_state_path(), encoding="utf-8"))
    assert d["cash"] == 50500.0 and d["day_start_equity"] == 50500.0 and d["opening_balance"] == 50000
    assert d["realized_pnl"] == 500.0 and d["charges_paid"] == 40.0            # recorded P&L unchanged
    st = capital.load()
    assert st["pending"] is None and st["history"][-1]["from"] == 100000 and st["history"][-1]["to"] == 50000
    a = accounts.paper_account(now=NOW)
    assert a["start"] == 50000 and a["equity"] == 50500.0 and a["total_pnl"] == 500.0


def test_applying_twice_cannot_move_cash_twice():
    s = paper_state(cash=100000.0)
    capital.request(25000, now=NOW)
    capital.apply_pending(s, now=NOW)
    # simulate a crash before the pending entry was cleared
    capital._write_json(capital.settings_path(), {"pending": {"amount": 25000, "requested_at": "x"}, "history": []})
    msg = capital.apply_pending(s, now=NOW)
    assert "already in force" in msg and s.d["cash"] == 25000.0


def test_waits_while_positions_are_open():
    s = paper_state(cash=100000.0, positions={"ITC": {"direction": "LONG", "qty": 1, "entry": 290.0, "stop": 284.0}})
    capital.request(50000, now=NOW)
    msg = capital.apply_pending(s, now=NOW)
    assert "waits" in msg and s.d["cash"] == 100000.0 and capital.load()["pending"]["amount"] == 50000


def test_trade_history_file_untouched():
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("closed,symbol,net\n2026-11-16 11:00,ITC,62.0\n")
    before = open(path, encoding="utf-8").read()
    s = paper_state(cash=100062.0, realized_pnl=62.0)
    capital.request(500000, now=NOW)
    capital.apply_pending(s, now=NOW)
    assert open(path, encoding="utf-8").read() == before


def test_paper_bot_applies_at_start(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", True)
    capital.request(25000, now=NOW)
    b = bot.Bot()
    b.apply_paper_capital()
    assert b.state.d["cash"] == 25000.0 and b.state.equity() == 25000.0
    assert any("opening balance changed" in e for e in b.events)


def test_live_bot_never_reads_the_paper_setting(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    capital.request(25000, now=NOW)
    b = bot.Bot()
    assert b.live and b.state.path.endswith("state_live.json")
    b.apply_paper_capital()
    assert b.state.d["cash"] == C.PAPER_START_INR                              # live state untouched
    assert capital.load()["pending"]["amount"] == 25000                         # still waiting for paper
    assert not os.path.exists(capital.paper_state_path())


def test_bad_settings_file_never_stops_the_bot(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", True)
    with open(capital.settings_path(), "w", encoding="utf-8") as f:
        f.write('{"pending": {"amount": "lots"}}')
    b = bot.Bot()
    b.apply_paper_capital()                                                     # must not raise
    assert b.state.d["cash"] == C.PAPER_START_INR
