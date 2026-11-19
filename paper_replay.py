"""
Paper replay: the REAL bot loop (bot.Bot.cycle, the code that trades) driven through archived days with a
simulated clock and archived candles, on virtual money, and then compared with the backtest of the same days.

Why: a backtest is a second implementation of the rules. If the loop that actually trades and the backtest
disagree on the same candles, one of them is wrong, and that must be known before the loop handles money.

What it guarantees about itself:
  - PAPER only. It forces DRY_RUN on, removes the live confirmation, and stops if the bot still comes up live.
  - It works in a folder of its own (a new temporary one unless --work is given) and refuses the real data folder.
  - No network: prices come only from the candle archive. A real price request would raise.
  - It never talks to Zerodha and has no broker other than the paper one.

How a day is replayed: at 30 seconds after every 15-minute candle closes, the bot gets the candles finished by
then and, as "the price now", the open of the candle that has just begun (what the market was trading at at that
moment). That is the same moment and price at which the backtest fills.

    .venv\\Scripts\\python.exe paper_replay.py                    # every archived day
    .venv\\Scripts\\python.exe paper_replay.py --days 20           # the latest 20 days
    .venv\\Scripts\\python.exe paper_replay.py --out result.json
"""

import csv
import datetime as dt
import json
import os
import sys
import tempfile
import time

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WARMUP_SESSIONS = 12          # days of candles the bot is given before the first replayed day (as live: "10d")
WINDOW_SESSIONS = 12


def _safe_environment(work):
    """Force paper, a private folder and archive-only prices BEFORE the bot's modules are imported."""
    os.environ["DRY_RUN"] = "true"
    os.environ.pop("CONFIRM_LIVE", None)
    os.environ["DATA_SOURCE"] = "yahoo"
    os.environ["CLOCK_CHECK_SEC"] = "0"         # the replay runs on a simulated clock and never uses the network
    os.environ["DATA_DIR"] = work


def load_archive(real_data_dir):
    """{symbol: bars} from the real folder's candle archive (read only)."""
    import archive
    import config as C
    keep = C.DATA_DIR
    C.DATA_DIR = real_data_dir
    try:
        return archive.load()
    finally:
        C.DATA_DIR = keep


class Feed:
    """Archived candles as the bot's price source at a simulated moment."""

    def __init__(self, data):
        self.data = data
        self.now = None
        self.days = sorted({b["t"].date() for bars in data.values() for b in bars})
        self.by_day = {}                      # symbol -> {day: (first index, last index + 1)}
        for sym, bars in data.items():
            idx, start = {}, 0
            for i in range(1, len(bars) + 1):
                if i == len(bars) or bars[i]["t"].date() != bars[start]["t"].date():
                    idx[bars[start]["t"].date()] = (start, i)
                    start = i
            self.by_day[sym] = idx

    def at(self, sym):
        """(bars finished by now over the last sessions, the price now, its time) or None."""
        bars, idx = self.data.get(sym), self.by_day.get(sym, {})
        if not bars:
            return None
        today = self.now.date()
        pos = self.days.index(today) if today in self.days else None
        if pos is None:
            return None
        first_day = next((d for d in self.days[max(0, pos - WINDOW_SESSIONS):pos + 1] if d in idx), None)
        if first_day is None:
            return None
        lo = idx[first_day][0]
        hi = idx[today][1] if today in idx else lo
        step = dt.timedelta(minutes=15)
        end = lo
        for i in range(lo, hi):               # candles that have STARTED by now
            if bars[i]["t"] <= self.now:
                end = i + 1
        window = bars[lo:end]
        if not window:
            return None
        last = window[-1]
        price = last["o"] if last["t"] + step > self.now else last["c"]     # the candle just begun: its open
        return window, price, self.now

    def fetch_many(self, symbols, *a, **k):
        from data_yahoo import DataError
        out = {}
        for s in symbols:
            got = self.at(s)
            out[s] = got if got else DataError(f"{s}: no archived candles on {self.now.date()}")
        return out

    def fetch(self, sym, *a, **k):
        from data_yahoo import DataError
        got = self.at(sym)
        if not got:
            raise DataError(f"{sym}: no archived candles on {self.now.date()}")
        return got


def steps(day):
    """The moments the bot is asked to act on a day: 30 s after each candle close, 09:30:30 to 15:30:30."""
    t = dt.datetime.combine(day, dt.time(9, 30, 30), tzinfo=IST)
    end = dt.datetime.combine(day, dt.time(15, 30, 30), tzinfo=IST)
    while t <= end:
        yield t
        t += dt.timedelta(minutes=15)


def replay(data, days=None, progress=None):
    """Run the bot loop over the archive. Returns {"trades": [...], "days", "rounds", "seconds", "errors"}.
    Call only after _safe_environment(): the bot's modules are imported here."""
    import bot
    import config as C
    import marketdata
    if C.is_live():
        raise SystemExit("refusing to run: the live settings are on in this process")
    feed = Feed({s: b for s, b in data.items() if s in C.WATCHLIST or s.startswith("^")})
    marketdata.fetch_many, marketdata.fetch = feed.fetch_many, feed.fetch
    marketdata.vix_by_day = lambda *a, **k: {}
    bot.now_ist = lambda: feed.now
    all_days = feed.days[WARMUP_SESSIONS:]
    run_days = all_days[-days:] if days else all_days
    if not run_days:
        raise SystemExit("the candle archive is too short to replay")
    feed.now = dt.datetime.combine(run_days[0], dt.time(9, 0), tzinfo=IST)
    b = bot.Bot()
    if b.live or b.mode != "paper" or type(b.broker).__name__ != "PaperBroker":
        raise SystemExit("refusing to run: the bot did not come up in paper mode")
    started, rounds, errors = time.time(), 0, []
    for n, day in enumerate(run_days):
        for t in steps(day):
            feed.now = t
            try:
                b.cycle()
            except Exception as e:  # noqa: BLE001 - a round that raises is a finding, not a reason to stop
                errors.append({"at": t.isoformat(), "error": f"{type(e).__name__}: {e}"})
            rounds += 1
        if b.state.positions:
            errors.append({"at": str(day), "error": f"still holding {sorted(b.state.positions)} after the close"})
        if progress:
            progress(n + 1, len(run_days))
    trades = []
    path = os.path.join(C.DATA_DIR, "trades_paper.csv")
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            trades = list(csv.DictReader(fh))
    return {"trades": trades, "days": [str(run_days[0]), str(run_days[-1]), len(run_days)], "rounds": rounds,
            "seconds": round(time.time() - started, 1), "errors": errors,
            "equity_end": b.state.equity({}), "start_cash": C.PAPER_START_INR}


def backtest_same(data, first_day, last_day):
    """The backtest of exactly the replayed days, with the same warm-up candles before them."""
    import backtest
    import config as C
    days = sorted({b["t"].date() for bars in data.values() for b in bars})
    start = days[max(0, days.index(first_day) - C.VOLUME_LOOKBACK_DAYS)]
    cut = {s: [b for b in bars if start <= b["t"].date() <= last_day] for s, bars in data.items()}
    stocks = {s: b for s, b in cut.items() if s in C.WATCHLIST and b}
    indexes = {s: b for s, b in cut.items() if s.startswith("^")}
    out = os.path.join(C.DATA_DIR, "replay_backtest_trades.csv")
    res = backtest.simulate(stocks, C.PAPER_START_INR, False, indexes, {}, trades_path=out)
    with open(out, encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["opened"][:10] >= str(first_day)]
    return rows, res


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def compare(loop, bt):
    """How far the trading loop and the backtest agree on the same days."""
    def key(r):
        return (r["opened"][:10], r["symbol"], r["direction"])

    a, b = {key(r): r for r in loop}, {key(r): r for r in bt}
    both = sorted(set(a) & set(b))
    same_qty = sum(1 for k in both if a[k]["qty"] == b[k]["qty"])
    same_exit = sum(1 for k in both if (a[k]["reason"] or "").split(" (")[0] == (b[k]["reason"] or "").split(" (")[0])
    net_gap = [abs(_f(a[k]["net"]) - _f(b[k]["net"])) for k in both]
    return {"loop_trades": len(loop), "backtest_trades": len(bt), "same_trade": len(both),
            "only_in_loop": [list(k) for k in sorted(set(a) - set(b))][:25],
            "only_in_backtest": [list(k) for k in sorted(set(b) - set(a))][:25],
            "only_in_loop_n": len(set(a) - set(b)), "only_in_backtest_n": len(set(b) - set(a)),
            "same_quantity": same_qty, "same_exit_reason": same_exit,
            "loop_net": round(sum(_f(r["net"]) for r in loop), 2), "backtest_net": round(sum(_f(r["net"]) for r in bt), 2),
            "median_net_gap": round(sorted(net_gap)[len(net_gap) // 2], 2) if net_gap else None,
            "max_net_gap": round(max(net_gap), 2) if net_gap else None,
            "agreement_pct": round(len(both) / max(1, len(set(a) | set(b))) * 100, 1)}


def main(argv):
    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else None
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.abspath(os.environ.get("REPLAY_SOURCE_DATA") or os.path.join(here, "data"))
    work = os.path.abspath(argv[argv.index("--work") + 1]) if "--work" in argv else tempfile.mkdtemp(prefix="paper_replay_")
    if os.path.normcase(work) == os.path.normcase(real) or os.path.normcase(real).startswith(os.path.normcase(work) + os.sep):
        raise SystemExit("refusing to run: the work folder must not be the real data folder")
    os.makedirs(work, exist_ok=True)
    _safe_environment(work)
    data = load_archive(real)
    if not data:
        raise SystemExit(f"no candle archive in {real}")

    def progress(n, total):
        if n % 25 == 0 or n == total:
            print(f"  replayed {n} of {total} days", flush=True)

    res = replay(data, days, progress)
    first, last = dt.date.fromisoformat(res["days"][0]), dt.date.fromisoformat(res["days"][1])
    bt_rows, _ = backtest_same(data, first, last)
    cmp_ = compare(res["trades"], bt_rows)
    summary = {"built": dt.datetime.now(IST).isoformat(timespec="seconds"), "days": res["days"], "rounds": res["rounds"],
               "seconds": res["seconds"], "errors": res["errors"], "compare": cmp_, "work": work}
    print(json.dumps({k: v for k, v in summary.items() if k != "work"}, indent=1, default=str))
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1, default=str)
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
