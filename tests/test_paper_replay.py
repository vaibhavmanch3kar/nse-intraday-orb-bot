"""Paper replay: the real bot loop driven through archived candles with a simulated clock, on virtual money, then
compared with the backtest. It can only run on paper, in a folder of its own, from archived prices."""

import datetime as dt
import os

import pytest

import bot
import config as C
import marketdata
import paper_replay as R
from conftest import IST, bar
from test_yantra import market, rules  # noqa: F401  (rules: the live bot's present rules)


def day_bars(day, closes, open_=100.0):
    out, t, o = [], dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST), open_
    for c in closes:
        out.append(bar(day, t.strftime("%H:%M"), o, max(o, c) + 0.1, min(o, c) - 0.1, c))
        o, t = c, t + dt.timedelta(minutes=15)
    return out


# ---------------------------------------------------------------- the simulated price source
def test_feed_shows_only_what_had_happened_by_then():
    d1, d2 = dt.date(2026, 1, 5), dt.date(2026, 1, 6)
    bars = day_bars(d1, [101, 102, 103]) + day_bars(d2, [201, 202, 203, 204], open_=200.0)
    feed = R.Feed({"AAA": bars})
    feed.now = dt.datetime.combine(d2, dt.time(9, 45, 30), tzinfo=IST)      # 30 s after the 09:30 candle closed
    window, price, when = feed.at("AAA")
    assert [b["c"] for b in window] == [101, 102, 103, 201, 202, 203]       # the 09:45 candle has only begun...
    assert price == 202 and when == feed.now                                 # ...and its open is the price now
    assert all(b["t"] <= feed.now for b in window)
    import data_yahoo
    closed = data_yahoo.closed_bars(window, feed.now, 15)                    # what the bot treats as finished
    assert [b["c"] for b in closed] == [101, 102, 103, 201, 202]
    feed.now = dt.datetime.combine(d2, dt.time(15, 30, 30), tzinfo=IST)
    assert feed.at("AAA")[1] == 204                                          # after the last candle: its close
    feed.now = dt.datetime.combine(dt.date(2026, 1, 7), dt.time(10, 0), tzinfo=IST)
    assert feed.at("AAA") is None and isinstance(feed.fetch_many(["AAA", "ZZZ"])["ZZZ"], Exception)
    with pytest.raises(data_yahoo.DataError):
        feed.fetch("AAA")


def test_feed_window_is_the_last_sessions_only():
    bars, d = [], dt.date(2026, 1, 5)
    days = []
    while len(days) < 20:
        if d.weekday() < 5:
            bars += day_bars(d, [100, 101])
            days.append(d)
        d += dt.timedelta(days=1)
    feed = R.Feed({"AAA": bars})
    feed.now = dt.datetime.combine(days[-1], dt.time(15, 30, 30), tzinfo=IST)
    window = feed.at("AAA")[0]
    assert len({b["t"].date() for b in window}) == R.WINDOW_SESSIONS + 1 and window[-1]["t"].date() == days[-1]


def test_steps_run_from_the_first_close_to_the_last():
    s = list(R.steps(dt.date(2026, 1, 5)))
    assert (s[0].strftime("%H:%M:%S"), s[-1].strftime("%H:%M:%S"), len(s)) == ("09:30:30", "15:30:30", 25)


# ---------------------------------------------------------------- the replay itself
@pytest.fixture
def restore(monkeypatch):
    """replay() swaps the price source and the clock of the bot's modules: put them back after the test."""
    for mod, name in ((marketdata, "fetch_many"), (marketdata, "fetch"), (marketdata, "vix_by_day"), (bot, "now_ist")):
        monkeypatch.setattr(mod, name, getattr(mod, name))


def test_the_trading_loop_and_the_backtest_agree_on_the_same_candles(isolated, rules, restore, monkeypatch):  # noqa: F811
    monkeypatch.setattr(C, "WATCHLIST", ["AAA", "BBB"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(C, "SQUARE_OFF", "15:00")
    data = {"AAA": market(days=45, seed=3), "BBB": market(days=45, seed=5), "^NSEI": market(days=45, seed=9)}
    res = R.replay(data)
    assert res["errors"] == [] and res["days"][2] == 45 - R.WARMUP_SESSIONS and res["rounds"] == res["days"][2] * 25
    assert len(res["trades"]) >= 5
    bt, _ = R.backtest_same(data, dt.date.fromisoformat(res["days"][0]), dt.date.fromisoformat(res["days"][1]))
    c = R.compare(res["trades"], bt)
    assert c["agreement_pct"] >= 90 and c["same_exit_reason"] >= 0.8 * c["same_trade"]
    files = set(os.listdir(str(isolated)))
    assert "trades_paper.csv" in files and "state_paper.json" in files and "trades_live.csv" not in files
    assert not any(n.startswith("live_") or n == "orders" for n in files)      # no real-order record of any kind


def test_it_refuses_to_run_live_or_in_the_real_data_folder(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")
    with pytest.raises(SystemExit, match="live settings"):
        R.replay({"AAA": market(days=20, seed=3)})
    real = tmp_path / "data"
    real.mkdir()
    monkeypatch.setenv("REPLAY_SOURCE_DATA", str(real))
    with pytest.raises(SystemExit, match="must not be the real data folder"):
        R.main(["paper_replay.py", "--work", str(real)])


def test_the_environment_is_forced_to_paper_before_anything_is_imported(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "YES")
    monkeypatch.setenv("DATA_DIR", "somewhere-real")
    R._safe_environment(str(tmp_path))
    assert os.environ["DRY_RUN"] == "true" and "CONFIRM_LIVE" not in os.environ and os.environ["DATA_DIR"] == str(tmp_path)
    src = open(R.__file__, encoding="utf-8").read()
    assert src.index("_safe_environment(work)") < src.index("data = load_archive(real)")
    top = src[:src.index("def _safe_environment")]
    assert "import bot" not in top and "import config" not in top           # nothing of the bot loads before that


# ---------------------------------------------------------------- the comparison
def row(day, sym, direction, qty, net, reason):
    return {"opened": f"{day} 09:45:00+05:30", "symbol": sym, "direction": direction, "qty": str(qty), "net": str(net),
            "reason": reason}


def test_compare_counts_shared_and_one_sided_trades():
    loop = [row("2026-01-05", "A", "LONG", 10, -50.0, "stop"), row("2026-01-06", "B", "SHORT", 5, 20.0, "time stop (1h)"),
            row("2026-01-07", "C", "LONG", 1, 5.0, "square-off")]
    bt = [row("2026-01-05", "A", "LONG", 10, -51.0, "stop"), row("2026-01-06", "B", "SHORT", 6, 26.0, "time stop"),
          row("2026-01-08", "D", "LONG", 1, 1.0, "stop")]
    c = R.compare(loop, bt)
    assert (c["loop_trades"], c["backtest_trades"], c["same_trade"]) == (3, 3, 2)
    assert c["only_in_loop"] == [["2026-01-07", "C", "LONG"]] and c["only_in_backtest"] == [["2026-01-08", "D", "LONG"]]
    assert c["same_quantity"] == 1 and c["same_exit_reason"] == 2 and c["agreement_pct"] == 50.0
    assert c["loop_net"] == -25.0 and c["backtest_net"] == -24.0 and c["max_net_gap"] == 6.0
    assert R.compare([], [])["agreement_pct"] == 0.0
