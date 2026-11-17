"""Kite's 3-requests-a-second limit is shared fairly: the bot always comes first, dashboard jobs give way."""

import datetime as dt
import threading
import time

import data_kite
import jobs
import marketdata
import review
from conftest import IST


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def pacer(hold=lambda: 0.0):
    c = FakeClock()
    return data_kite.Pacer(clock=c, sleep=c.sleep, hold=hold), c


def test_never_more_than_three_a_second():
    p, _ = pacer()
    slots = [p.wait_turn() for _ in range(12)]
    assert slots[:3] == [0.0, 0.0, 0.0]
    assert all(slots[i + 3] - slots[i] > 1.0 for i in range(len(slots) - 3))


def test_dashboard_jobs_leave_room_for_the_bot():
    p, _ = pacer()
    slots = [p.wait_turn(background=True) for _ in range(8)]
    assert all(slots[i + 2] - slots[i] > 1.0 for i in range(len(slots) - 2))   # at most 2 a second


def test_jobs_wait_while_the_bot_is_fetching():
    p, c = pacer()
    p.wait_turn()                          # the bot fetches at t=0
    slot = p.wait_turn(background=True)
    assert slot >= data_kite.BOT_BUSY_SEC  # the job waited until the bot was done


def test_jobs_wait_out_the_candle_close_window():
    c = FakeClock()
    p = data_kite.Pacer(clock=c, sleep=c.sleep, hold=lambda: max(0.0, 5.0 - c.t))
    assert p.wait_turn(background=True) >= 5.0
    c2 = FakeClock()
    p2 = data_kite.Pacer(clock=c2, sleep=c2.sleep, hold=lambda: 99.0)
    assert p2.wait_turn() == 0.0           # the bot itself is never held


def test_a_waiting_job_never_blocks_the_bot():
    """v1.8.0 slept while holding the lock, so the bot queued behind a scan."""
    hold = {"s": 30.0}
    p = data_kite.Pacer(hold=lambda: hold["s"])
    done = threading.Event()
    t = threading.Thread(target=lambda: (p.wait_turn(background=True), done.set()), daemon=True)
    t.start()
    time.sleep(0.1)
    t0 = time.perf_counter()
    p.wait_turn()                          # the bot
    assert time.perf_counter() - t0 < 0.2
    assert not done.is_set()
    hold["s"] = 0.0
    p._bot_busy_until = 0.0
    assert done.wait(3)


def at(hhmm, day=dt.date(2026, 11, 18)):
    h, m, *s = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m, s[0] if s else 0), tzinfo=IST)


def test_candle_close_window_times():
    assert data_kite.candle_hold(at("09:20")) == 0                 # no candle has closed yet
    assert data_kite.candle_hold(at("09:30:10")) == 80
    assert data_kite.candle_hold(at("09:31:31")) == 0
    assert data_kite.candle_hold(at("15:30:30")) == 60             # the last candle
    assert data_kite.candle_hold(at("15:40")) == 0
    assert data_kite.candle_hold(at("10:00:00", dt.date(2026, 11, 21))) == 0   # Saturday


def test_dashboard_job_mark_reaches_the_download_threads(monkeypatch):
    seen = []

    def fake_fetch(sym, *a, **k):
        seen.append(jobs.in_background())
        return [], None, None

    monkeypatch.setattr(marketdata, "fetch", fake_fetch)
    with jobs.background(True):
        review.fetch_all(["A", "B", "C", "D", "E"])
    assert seen == [True] * 5
    seen.clear()
    review.fetch_all(["A", "B"])           # the bot's own path: not a dashboard job
    assert seen == [False, False]
    assert not jobs.in_background()
