"""Kavach's two optional limits (2.0): at most MAX_PER_SECTOR open positions in one sector and direction, and
one order at most MAX_BAR_VOLUME_PCT of the signal candle's volume. Both only ever make the bot stricter."""

import datetime as dt

import pytest

import config as C
import kavach
import risk
import strategy

DAY = dt.date(2026, 11, 18)


@pytest.fixture
def st(tmp_path):
    s = risk.State(str(tmp_path / "state.json"), 100000.0)
    s.new_day_if_needed(DAY)
    return s


def ask(st, symbol="SBIN", direction="LONG", entry=1000.0, stop=990.0, **kw):
    return kavach.pre_trade(st, symbol, direction, entry, stop, DAY, sector_of=strategy.sector_for, **kw)


def test_sector_limit_is_off_by_default(st):
    assert C.MAX_PER_SECTOR == 0
    st.positions["HDFCBANK"] = {"direction": "LONG", "qty": 5, "entry": 800.0}
    st.positions["ICICIBANK"] = {"direction": "LONG", "qty": 5, "entry": 1600.0}
    d = ask(st, "SBIN")
    assert d.ok and ("sector", True, "off") in d.checks


def test_sector_limit_counts_same_sector_and_direction_only(st, monkeypatch):
    monkeypatch.setattr(C, "MAX_PER_SECTOR", 1)
    banks = [s for s in ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK")
             if strategy.sector_for(s) == strategy.sector_for("SBIN")]
    assert len(banks) >= 3                                       # the watchlist's banks share one sector index
    st.positions[banks[0]] = {"direction": "LONG", "qty": 5, "entry": 800.0}
    other = "SBIN" if banks[0] != "SBIN" else banks[1]
    d = ask(st, other, "LONG")
    assert not d.ok and d.check == "sector" and "already 1 LONG position in the same sector" in d.reason
    assert ask(st, other, "SHORT", entry=1000.0, stop=1010.0).ok           # the other direction is a hedge, allowed
    it = next(s for s in C.WATCHLIST if strategy.sector_for(s) != strategy.sector_for("SBIN"))
    assert ask(st, it, "LONG").ok                                          # another sector is allowed
    st.positions["RELIANCE"] = {"direction": "LONG", "qty": 1, "entry": 1300.0}
    assert strategy.sector_for("RELIANCE") == "RELIANCE" != strategy.sector_for("ITC")
    assert ask(st, "ITC", "LONG", entry=300.0, stop=297.0).ok              # sharing the default index is not a sector
    monkeypatch.setattr(C, "MAX_PER_SECTOR", 2)
    assert ask(st, other, "LONG").ok


def test_order_is_cut_to_a_share_of_the_candles_volume(st, monkeypatch):
    monkeypatch.setattr(C, "MAX_BAR_VOLUME_PCT", 2.0)
    full = ask(st).qty                                           # 25 shares by the per-stock limit
    assert ask(st, bar_volume=100000).qty == full                # 2% of 1,00,000 = 2,000: no effect
    d = ask(st, bar_volume=500)                                  # 2% of 500 = 10 shares
    assert d.ok and d.qty == 10 and "cut from 25 to 10 shares" in d.checks[-1][2]
    d = ask(st, bar_volume=20)                                   # 2% of 20 = 0 shares
    assert not d.ok and d.check == "liquidity" and "too little volume" in d.reason
    assert ask(st, bar_volume=None).qty == full                  # no volume known (an index): not applied
    monkeypatch.setattr(C, "MAX_BAR_VOLUME_PCT", 0.0)
    assert ask(st, bar_volume=20).qty == full                    # 0 switches it off


def test_optional_limits_never_raise_a_quantity(st, monkeypatch):
    import random
    rng = random.Random(5)
    for _ in range(500):
        entry = rng.uniform(100, 3000)
        stop = entry * 0.99
        monkeypatch.setattr(C, "MAX_BAR_VOLUME_PCT", 0.0)
        base = ask(st, entry=entry, stop=stop).qty
        monkeypatch.setattr(C, "MAX_BAR_VOLUME_PCT", rng.choice([0.5, 2.0, 10.0]))
        assert ask(st, entry=entry, stop=stop, bar_volume=rng.choice([50, 5000, 500000])).qty <= base
