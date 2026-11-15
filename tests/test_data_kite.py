"""Kite Connect market data and the Yahoo/Kite switch, against a fake Kite client (no network)."""

import datetime as dt

import pytest

import config as C
import data_kite
import data_yahoo
import marketdata

IST = data_yahoo.IST


class FakeKite:
    def __init__(self, permission=True):
        self.permission = permission
        self.hist_calls = []

    def instruments(self, exchange=None):
        return [{"tradingsymbol": "SBIN", "instrument_token": 779521},
                {"tradingsymbol": "NIFTY 50", "instrument_token": 256265},
                {"tradingsymbol": "INDIA VIX", "instrument_token": 264969}]

    def historical_data(self, token, frm, to, interval, continuous=False, oi=False):
        from kiteconnect import exceptions as kex
        if not self.permission:
            raise kex.PermissionException("Insufficient permission for that call.")
        assert (to - frm).days <= data_kite.MAX_DAYS[interval]           # never ask for too much at once
        self.hist_calls.append((token, frm, to, interval))
        out, d = [], frm.date()
        while d <= to.date():
            if d.weekday() < 5:
                for hm in ((9, 15), (9, 30)) if interval != "day" else ((0, 0),):
                    t = dt.datetime.combine(d, dt.time(*hm), tzinfo=IST)
                    if frm.replace(tzinfo=IST) <= t <= to.replace(tzinfo=IST) + dt.timedelta(minutes=15):
                        out.append({"date": t, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10})
            d += dt.timedelta(days=1)
        return out

    def ltp(self, instruments):
        from kiteconnect import exceptions as kex
        if not self.permission:
            raise kex.PermissionException("Insufficient permission for that call.")
        return {k: {"last_price": 123.45} for k in instruments}


@pytest.fixture
def fake(monkeypatch):
    k = FakeKite()
    monkeypatch.setattr(data_kite, "_client", lambda: k)
    monkeypatch.setattr(data_kite, "_tokens", None)
    monkeypatch.setattr(data_kite, "HIST_PER_SEC", 1000)   # no pacing delays in tests
    return k


def test_parse_range_and_symbols():
    assert data_kite.parse_range("10d") == 10 and data_kite.parse_range("2mo") == 60
    assert data_kite.parse_range("1y") == 365
    with pytest.raises(data_yahoo.DataError):
        data_kite.parse_range("abc")
    assert data_kite.kite_symbol("^NSEI") == "NIFTY 50" and data_kite.kite_symbol("SBIN") == "SBIN"


def test_fetch_chunks_long_ranges_and_dedupes(fake):
    bars, price, _ = data_kite.fetch("SBIN", 15, "450d")
    assert len(fake.hist_calls) == 3                       # 450 days / 200 per call
    times = [b["t"] for b in bars]
    assert times == sorted(times) and len(times) == len(set(times))
    assert all(b["t"].tzinfo is not None for b in bars)
    assert price == bars[-1]["c"]
    assert fake.hist_calls[0][0] == 779521 and fake.hist_calls[0][3] == "15minute"


def test_index_and_daily(fake):
    data_kite.fetch("^NSEI", 15, "5d")
    assert fake.hist_calls[-1][0] == 256265
    vix = data_kite.vix_by_day("1mo")
    assert fake.hist_calls[-1][3] == "day" and vix
    with pytest.raises(data_yahoo.DataError):
        data_kite.fetch("NOTASTOCK", 15, "5d")


def test_fetch_many_uses_one_batched_ltp(fake, monkeypatch):
    calls = []
    orig = fake.ltp
    monkeypatch.setattr(fake, "ltp", lambda inst: calls.append(inst) or orig(inst))
    got = data_kite.fetch_many(["SBIN", "NOTASTOCK"], 15, "5d")
    assert got["SBIN"][1] == 123.45                       # live price, not the candle close
    assert isinstance(got["NOTASTOCK"], data_yahoo.DataError)
    assert calls == [["NSE:SBIN"]]


def test_switch_defaults_to_yahoo(monkeypatch):
    monkeypatch.setattr(C, "DATA_SOURCE", "yahoo")
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: (["y"], 1.0, None))
    assert marketdata.fetch("SBIN", 15, "5d")[0] == ["y"]
    assert marketdata.status()["using"] == "yahoo" and marketdata.max_history_days() == 60


def test_switch_uses_kite_when_logged_in(fake, monkeypatch):
    monkeypatch.setattr(C, "DATA_SOURCE", "kite")
    bars, price, _ = marketdata.fetch("SBIN", 15, "5d")
    assert bars and marketdata.status() == {"wanted": "kite", "using": "kite", "reason": ""}
    assert marketdata.max_history_days() == 365


def test_switch_falls_back_without_login(monkeypatch):
    monkeypatch.setattr(C, "DATA_SOURCE", "kite")

    def no_login():
        raise data_yahoo.DataError("no Kite login today")
    monkeypatch.setattr(data_kite, "_client", no_login)
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: (["y"], 1.0, None))
    assert marketdata.fetch("SBIN", 15, "5d")[0] == ["y"]
    st = marketdata.status()
    assert st["using"] == "yahoo" and "no Kite login" in st["reason"]


def test_switch_falls_back_when_plan_not_upgraded(monkeypatch):
    k = FakeKite(permission=False)
    monkeypatch.setattr(C, "DATA_SOURCE", "kite")
    monkeypatch.setattr(data_kite, "_client", lambda: k)
    monkeypatch.setattr(data_kite, "_tokens", None)
    monkeypatch.setattr(data_yahoo, "fetch", lambda *a, **k: (["y"], 1.0, None))
    assert marketdata.fetch("SBIN", 15, "5d")[0] == ["y"]
    assert "Connect plan" in marketdata.status()["reason"]


def test_dashboard_shows_data_source():
    import web
    assert "Kite (real-time)" in web.data_source_kpi({"data_source": {"using": "kite", "wanted": "kite"}})
    html = web.data_source_kpi({"data_source": {"using": "yahoo", "wanted": "kite", "reason": "no login"}})
    assert "Yahoo" in html and "no login" in html
    assert web.data_source_kpi({}) == ""
