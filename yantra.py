#!/usr/bin/env python3
"""
Yantrashala: the strategy lab. It tests ideas; it never trades and never changes the bot.

    .venv\\Scripts\\python.exe yantra.py list                      # the strategies and how many variants each has
    .venv\\Scripts\\python.exe yantra.py test orb_retest           # develop-period test + walk-forward, saved
    .venv\\Scripts\\python.exe yantra.py test all
    .venv\\Scripts\\python.exe yantra.py register orb_retest wait=2 # freeze one variant for the forward test
    .venv\\Scripts\\python.exe yantra.py forward                   # registered variants on data AFTER registration
    .venv\\Scripts\\python.exe yantra.py show                      # the saved experiments

How an idea is judged (decided before any result was seen, the same for every idea):

  1. DEVELOP period only (DEV_START..DEV_END of the candle archive). Every variant is run by the one backtest
     engine: next-candle entry with slippage, gap-aware stops, Kavach's checks and sizes, Zerodha's charges.
  2. WALK-FORWARD inside that period: for each month, the variant that did best on the 3 months before it is the
     one "traded" that month. Only those out-of-sample months are judged. A strategy with one variant is simply
     judged month by month.
  3. PASS needs all of: at least MIN_TRADES walk-forward trades; the 95% range of the average result per trade
     (in R) entirely above zero; profit factor above PASS_PF; and at least PASS_MONTHS of the months positive.
     Anything else is FAIL. Thirty trades is a checkpoint, not proof.
  4. The older second half of the archive was already looked at in earlier studies (the audit, the video tests),
     so it is NOT untouched and is never used to pass anything. The only untouched data is the FUTURE: a passing
     variant is registered (frozen with a fingerprint of its code) and judged again only on candles recorded
     after that day. Real money comes after that, never from a backtest alone.
  5. Every run is saved with its settings, data range and code fingerprint, and the number of variants tried is
     counted and shown: the more that are tried, the more likely the best one is luck.
"""

import csv
import datetime as dt
import hashlib
import json
import math
import os
import sys
import tempfile

import archive
import backtest
import config as C
import safefile
import yantra_strategies as Y

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DEV_START, DEV_END = dt.date(2025, 11, 24), dt.date(2026, 6, 25)      # the develop period (as in the audit)
USED_END = dt.date(2026, 11, 17)        # everything up to here has been looked at before: never "untouched"
TRAIN_MONTHS = 3
MIN_TRAIN_TRADES = 15
MIN_TRADES = 30
PASS_PF = 1.1
PASS_MONTHS = 0.6
WARMUP_SESSIONS = 12                     # candles before the period, for volume averages and EMAs
CODE_FILES = ("yantra_strategies.py", "strategy.py", "kavach.py", "risk.py", "costs.py", "backtest.py")
ROOT = os.path.dirname(os.path.abspath(__file__))


def lab_dir():
    return os.path.join(C.DATA_DIR, "yantra")


def fingerprint():
    """A short hash of the code that decides a result: if it differs, the result is of other rules."""
    h = hashlib.sha256()
    for name in CODE_FILES:
        with open(os.path.join(ROOT, name), "rb") as f:
            h.update(f.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def settings():
    keys = ("WATCHLIST", "PAPER_START_INR", "RISK_PER_TRADE_PCT", "MAX_POSITION_PCT", "MAX_EXPOSURE_PCT",
            "MAX_OPEN_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_DAILY_LOSS_PCT", "LOSS_STREAK_LIMIT", "COOLDOWN_DAYS",
            "SLIPPAGE_PCT", "TRAIL_EMA", "TIME_STOP_BARS", "TIME_STOP_MIN_R", "TARGET_R", "INDEX_FILTER",
            "LAST_ENTRY", "SQUARE_OFF", "VOLUME_MULT", "MIN_OR_PCT", "MAX_OR_PCT", "MAX_PER_SECTOR",
            "MAX_BAR_VOLUME_PCT", "ALLOW_SHORT")
    return {k: getattr(C, k) for k in keys}


# ---------------------------------------------------------------- data
_DATA = {}


def load():
    """(stocks, indexes) from the candle archive, read once."""
    if not _DATA:
        allb = archive.load()
        _DATA["stocks"] = {s: b for s, b in allb.items() if s in C.WATCHLIST}
        _DATA["indexes"] = {s: b for s, b in allb.items() if s.startswith("^")}
    return _DATA["stocks"], _DATA["indexes"]


def window(bars, start, end):
    """Bars from WARMUP_SESSIONS sessions before `start` to `end` (the warm-up is not traded: see run)."""
    days = sorted({b["t"].date() for b in bars})
    first = [d for d in days if d >= start]
    if not first:
        return []
    k = max(0, days.index(first[0]) - WARMUP_SESSIONS)
    lo = days[k]
    return [b for b in bars if lo <= b["t"].date() <= end]


# ---------------------------------------------------------------- one run
def run(strat, start, end, data=None):
    """Trades of `strat` between start and end (inclusive), as rows with closed, symbol, direction, net, r, ..."""
    stocks, indexes = data or load()
    sub = {s: window(b, start, end) for s, b in stocks.items()}
    sub = {s: b for s, b in sub.items() if b}
    idx = {s: window(b, start, end) for s, b in indexes.items()}
    strat._feat = {}
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="yantra_")
    os.close(fd)
    try:
        backtest.simulate(sub, C.PAPER_START_INR, False, idx, {}, trades_path=path, strat=strat)
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    out = []
    for r in rows:
        day = dt.date.fromisoformat(str(r.get("closed") or "")[:10])
        if start <= day <= end:
            out.append({"day": day, "symbol": r["symbol"], "direction": r["direction"], "net": float(r["net"]),
                        "r": float(r["r"]), "reason": r.get("reason", "")})
    return out


def measure(trades, seed=7):
    """The figures every result is judged by. The 95% range of the average R comes from 2,000 resamples of the
    trades (bootstrap), with a fixed seed so a rerun gives the same numbers."""
    n = len(trades)
    out = {"trades": n}
    if not n:
        return out
    rs = [t["r"] for t in trades]
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    loss = -sum(x for x in nets if x <= 0)
    mean = sum(rs) / n
    import random
    rng = random.Random(seed)
    boots = sorted(sum(rng.choices(rs, k=n)) / n for _ in range(2000)) if n > 1 else [mean]
    months = {}
    for t in trades:
        months.setdefault(t["day"].strftime("%Y-%m"), []).append(t["net"])
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x["day"]):
        eq += t["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    out.update(win_rate=len(wins) / n, avg_r=mean, r_low=boots[int(0.025 * len(boots))],
               r_high=boots[min(len(boots) - 1, int(0.975 * len(boots)))], net=sum(nets),
               profit_factor=(sum(wins) / loss) if loss > 0 else None, max_drawdown=dd,
               months=len(months), months_positive=sum(1 for v in months.values() if sum(v) > 0),
               by_month={m: round(sum(v), 2) for m, v in sorted(months.items())},
               sd_r=math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0)
    return out


def verdict(m):
    """PASS / FAIL and the reasons, by the rule fixed at the top of this file."""
    why = []
    if m.get("trades", 0) < MIN_TRADES:
        why.append(f"only {m.get('trades', 0)} walk-forward trades (needs {MIN_TRADES})")
    if m.get("trades"):
        if m["r_low"] <= 0:
            why.append(f"the average result per trade could be zero or negative (95% range {m['r_low']:+.2f}R to "
                       f"{m['r_high']:+.2f}R)")
        pf = m.get("profit_factor")
        if pf is None or pf <= PASS_PF:
            why.append(f"profit factor {pf:.2f} (needs above {PASS_PF})" if pf is not None else "no losing trades to measure against")
        if m["months"] and m["months_positive"] / m["months"] < PASS_MONTHS:
            why.append(f"positive in {m['months_positive']} of {m['months']} months (needs {PASS_MONTHS:.0%})")
    return ("PASS" if not why else "FAIL"), why


# ---------------------------------------------------------------- walk-forward
def month_starts(start, end):
    out, d = [], dt.date(start.year, start.month, 1)
    while d <= end:
        out.append(d)
        d = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def walk_forward(runs, start, end):
    """runs: {label: trades over the whole period}. For each month after the first TRAIN_MONTHS, pick the label
    with the best average R over the TRAIN_MONTHS before it (at least MIN_TRAIN_TRADES trades there) and take
    THAT label's trades in the month. Returns (stitched trades, [{month, chosen, train_avg_r, trades}])."""
    ms = month_starts(start, end)
    stitched, picks = [], []
    for k in range(TRAIN_MONTHS, len(ms)):
        lo, hi = ms[k - TRAIN_MONTHS], ms[k]
        nxt = ms[k + 1] if k + 1 < len(ms) else end + dt.timedelta(days=1)
        best = None
        for label, tr in runs.items():
            train = [t["r"] for t in tr if lo <= t["day"] < hi]
            if len(train) >= MIN_TRAIN_TRADES:
                avg = sum(train) / len(train)
                if best is None or avg > best[1]:
                    best = (label, avg)
        if best is None:
            picks.append({"month": hi.strftime("%Y-%m"), "chosen": None, "train_avg_r": None, "trades": 0})
            continue
        test = [t for t in runs[best[0]] if hi <= t["day"] < nxt]
        stitched += test
        picks.append({"month": hi.strftime("%Y-%m"), "chosen": best[0], "train_avg_r": round(best[1], 3),
                      "trades": len(test)})
    return stitched, picks


# ---------------------------------------------------------------- experiments (saved, reproducible)
def test(name, data=None, now=None, save=True):
    """Run every variant of a strategy on the develop period, walk it forward, judge it, save the experiment."""
    now = now or dt.datetime.now(IST)
    vs = Y.variants(name)
    runs = {v.label(): run(v, DEV_START, DEV_END, data) for v in vs}
    stitched, picks = walk_forward(runs, DEV_START, DEV_END)
    wf = measure(stitched)
    v, why = verdict(wf)
    exp = {"id": f"{now:%Y%m%d-%H%M%S}-{name}", "at": now.isoformat(timespec="seconds"), "strategy": name,
           "what": Y.WHAT.get(name, ""), "period": [DEV_START.isoformat(), DEV_END.isoformat()],
           "variants_tried": len(vs), "code": fingerprint(), "settings": settings(),
           "in_sample": {label: measure(tr) for label, tr in runs.items()},
           "walk_forward": wf, "picks": picks, "verdict": v, "why": why}
    if save:
        os.makedirs(os.path.join(lab_dir(), "experiments"), exist_ok=True)
        safefile.write_json(os.path.join(lab_dir(), "experiments", exp["id"] + ".json"), exp, indent=1, default=str)
    return exp


def experiments():
    """Saved experiments, newest first."""
    folder = os.path.join(lab_dir(), "experiments")
    out = []
    try:
        names = sorted(os.listdir(folder), reverse=True)
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            try:
                with open(os.path.join(folder, n), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
    return out


def tests_so_far():
    """How many variants have been tried in all saved experiments (the multiple-testing count)."""
    seen = {}
    for e in experiments():
        seen[(e["strategy"], e.get("code"))] = e.get("variants_tried", 0)
    return sum(seen.values())


# ---------------------------------------------------------------- the forward test: the only untouched data
def register_path():
    return os.path.join(lab_dir(), "registered.json")


def registered():
    try:
        with open(register_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def register(name, params, now=None):
    """Freeze one variant today. It is judged only on candles recorded after this day."""
    now = now or dt.datetime.now(IST)
    cls = Y.STRATEGIES[name]
    if params not in cls.grid:
        raise ValueError(f"{name}: {params} is not one of its tested variants {cls.grid}")
    reg = registered()
    label = cls(**params).label()
    if any(r["label"] == label and r["code"] == fingerprint() for r in reg):
        raise ValueError(f"{label} is already registered with this code")
    reg.append({"label": label, "strategy": name, "params": params, "code": fingerprint(),
                "registered": now.date().isoformat(), "settings": settings()})
    os.makedirs(lab_dir(), exist_ok=True)
    safefile.write_json(register_path(), reg, indent=1, default=str)
    return reg[-1]


def forward(data=None, today=None):
    """Each registered variant on candles AFTER its registration day. A variant whose code changed since then is
    not measured (the frozen rules no longer exist)."""
    stocks, indexes = data or load()
    last = max((b["t"].date() for bars in stocks.values() for b in bars), default=None)
    out = []
    for r in registered():
        start = dt.date.fromisoformat(r["registered"]) + dt.timedelta(days=1)
        row = {**r, "from": start.isoformat(), "to": last.isoformat() if last else None}
        if r["code"] != fingerprint():
            row.update(status="code changed since registration: not measured", result=None)
        elif last is None or last < start:
            row.update(status="no candles after the registration day yet", result=None)
        else:
            strat = Y.STRATEGIES[r["strategy"]](**r["params"])
            m = measure(run(strat, start, last, (stocks, indexes)))
            v, why = verdict(m)
            row.update(status="measured", result=m, verdict=v, why=why)
        out.append(row)
    return out


# ---------------------------------------------------------------- command line
def _line(label, m):
    if not m.get("trades"):
        return f"  {label:44s} no trades"
    pf = f"{m['profit_factor']:.2f}" if m.get("profit_factor") is not None else "  - "
    return (f"  {label:44s} {m['trades']:4d} trades  win {m['win_rate']:4.0%}  avg {m['avg_r']:+.2f}R "
            f"({m['r_low']:+.2f} to {m['r_high']:+.2f})  PF {pf}  net ₹{m['net']:>9,.0f}  "
            f"months + {m['months_positive']}/{m['months']}")


def show(exp):
    print(f"\n{exp['strategy']}: {exp['what']}   [{exp['period'][0]} .. {exp['period'][1]}, code {exp['code']}]")
    for label, m in exp["in_sample"].items():
        print(_line(label + " (whole period)", m))
    print(_line("WALK-FORWARD (judged)", exp["walk_forward"]))
    print(f"  VERDICT: {exp['verdict']}" + ("" if not exp["why"] else " - " + "; ".join(exp["why"])))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for name, cls in Y.STRATEGIES.items():
            print(f"  {name:18s} {len(cls.grid)} variant(s)  {Y.WHAT.get(name, '')}")
    elif cmd == "test":
        names = list(Y.STRATEGIES) if (len(argv) < 3 or argv[2] == "all") else argv[2:]
        for name in names:
            show(test(name))
        print(f"\nVariants tried so far, all experiments: {tests_so_far()}. With that many tries, the best one "
              f"looking good is expected by luck alone; only the forward test can confirm it.")
    elif cmd == "show":
        for e in experiments():
            show(e)
    elif cmd == "register":
        params = {}
        for a in argv[3:]:
            k, v = a.split("=", 1)
            params[k] = float(v) if "." in v else int(v)
        r = register(argv[2], params)
        print(f"registered {r['label']} on {r['registered']} (code {r['code']}); judged only on later candles")
    elif cmd == "forward":
        for r in forward():
            print(f"\n{r['label']} registered {r['registered']}: {r['status']}")
            if r.get("result"):
                print(_line(f"forward {r['from']} .. {r['to']}", r["result"]))
                print(f"  VERDICT: {r['verdict']}" + ("" if not r["why"] else " - " + "; ".join(r["why"])))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
