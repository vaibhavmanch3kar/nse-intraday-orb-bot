"""The bot's own square-off is 15:00 by default: Zerodha force-closes intraday positions in F&O stocks at 15:12
(Rs 50 + GST per order), and 15:10 left the bot only two minutes. The backtest already exits on the candle that
ends at 15:00, so nothing changes there."""

import datetime as dt
import os

import config as C
import strategy
from conftest import IST

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZERODHA_FORCED_CLOSE = dt.time(15, 12)


def default_square_off():
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    return src.split('SQUARE_OFF = _env("SQUARE_OFF", "', 1)[1].split('"', 1)[0]


def test_default_leaves_at_least_ten_minutes_before_zerodhas_forced_close():
    h, m = map(int, default_square_off().split(":"))
    assert (h, m) == (15, 0)
    gap = dt.datetime.combine(dt.date.today(), ZERODHA_FORCED_CLOSE) - dt.datetime.combine(dt.date.today(), dt.time(h, m))
    assert gap >= dt.timedelta(minutes=10)


def bar_at(hhmm):
    h, m = map(int, hhmm.split(":"))
    return {"t": dt.datetime.combine(dt.date(2026, 11, 18), dt.time(h, m), tzinfo=IST)}


def test_backtest_exit_candle_is_the_same_for_1500_and_1510(monkeypatch):
    for setting in ("15:00", "15:10"):
        monkeypatch.setattr(C, "SQUARE_OFF", setting)
        hits = [t for t in ("14:15", "14:30", "14:45", "15:00", "15:15") if strategy.is_square_off_bar(bar_at(t))]
        assert hits == ["14:45"], (setting, hits)             # the candle that ends at 15:00
