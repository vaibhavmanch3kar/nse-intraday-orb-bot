import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def bar(day, hhmm, o, h, l, c, v=1000):
    h_, m_ = map(int, hhmm.split(":"))
    return {"t": dt.datetime.combine(day, dt.time(h_, m_), tzinfo=IST), "o": o, "h": h, "l": l, "c": c, "v": v}


def flat_day(day, price=100.0, vol=1000):
    """A quiet session: 25 bars, all at `price`, constant volume."""
    out = []
    t = dt.datetime.combine(day, dt.time(9, 15))
    for _ in range(25):
        out.append(bar(day, t.strftime("%H:%M"), price, price + 0.2, price - 0.2, price, vol))
        t += dt.timedelta(minutes=15)
    return out


def history(days=5, start=dt.date(2026, 11, 2)):
    out, d = [], start
    while len({b["t"].date() for b in out}) < days:
        if d.weekday() < 5:
            out += flat_day(d)
        d += dt.timedelta(days=1)
    return out, d


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own data dir and default strategy settings."""
    monkeypatch.setattr(C, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(C, "PAUSE_FILE", str(tmp_path / "PAUSE"))
    monkeypatch.setattr(C, "STOP_FILE", str(tmp_path / "STOP"))
    for k, v in {"ALLOW_SHORT": True, "MIN_OR_PCT": 0.25, "MAX_OR_PCT": 1.5, "VOLUME_MULT": 1.2,
                 "USE_VWAP_FILTER": True, "TARGET_R": 2.0, "LAST_ENTRY": "12:00",
                 "SQUARE_OFF": "15:10", "SLIPPAGE_PCT": 0.05, "RISK_PER_TRADE_PCT": 0.5,
                 "MAX_POSITION_PCT": 25.0, "MAX_EXPOSURE_PCT": 75.0, "MAX_OPEN_POSITIONS": 3,
                 "MAX_TRADES_PER_DAY": 4, "MAX_DAILY_LOSS_PCT": 2.0, "LOSS_STREAK_LIMIT": 3,
                 "COOLDOWN_DAYS": 2, "ORDER_FILL_TIMEOUT_SEC": 2, "EXIT_RETRIES": 3,
                 # rule tests switch these on explicitly; core tests use the original rules
                 "INDEX_FILTER": False, "TRAIL_EMA": 0, "TIME_STOP_BARS": 0, "TIME_STOP_MIN_R": 0.5,
                 "VIX_SIZING": False}.items():
        monkeypatch.setattr(C, k, v)
    yield tmp_path
