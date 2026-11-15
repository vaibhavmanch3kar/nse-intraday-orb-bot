#!/usr/bin/env python3
"""
Guru Vashishtha · learning advisor. Phase 3: the weekly report.

    .venv\\Scripts\\python.exe advisor.py report              # whole candle archive
    .venv\\Scripts\\python.exe advisor.py report --days 120   # only the last 120 trading days
    .venv\\Scripts\\python.exe advisor.py report --no-rules   # skip the rule check (faster)

What the report does:
  1. Backtests the bot's CURRENT settings on the candle archive (data/candles, see archive.py),
     charges and slippage included. Sessions shorter than MIN_SESSION_BARS candles (e.g. Diwali
     Muhurat trading) are left out.
  2. Slices the trades by exit reason, entry time, direction, stock, opening-range width, breakout
     volume and weekday, each with trade count, win rate, average R, net ₹ and a 95% range.
  3. Rule check: replays the same data with one rule switched off at a time (index filter, time
     stop, EMA trail, VWAP filter, volume filter) to show what each rule did on this data.
  4. Summarises paper (and live) trades separately. Backtest and real results are never mixed.

What it does NOT do: change any setting, propose changes, or touch the bot. It only writes to
data/advisor/ (report_<date>.json and report_<date>_trades.csv), never to backtest_trades.csv.
Everything here describes the past on data the rules were already chosen with; a rule that
"hurt" here is not a recommendation to drop it.
"""

import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

import archive
import backtest
import config as C
import edge
import theme as T

MIN_SESSION_BARS = 20            # a normal NSE day has 25 fifteen-minute candles
MIN_SLICE_TRADES = edge.MIN_TRADES

STRATEGY_SETTINGS = ["WATCHLIST", "OR_MINUTES", "LAST_ENTRY", "SQUARE_OFF", "ALLOW_SHORT", "MIN_OR_PCT",
                     "MAX_OR_PCT", "VOLUME_MULT", "VOLUME_LOOKBACK_DAYS", "USE_VWAP_FILTER", "STOP_RULE",
                     "TARGET_R", "MAX_CHASE_R", "TRAIL_EMA", "TIME_STOP_BARS", "TIME_STOP_MIN_R",
                     "INDEX_FILTER", "VIX_SIZING", "SLIPPAGE_PCT", "RISK_PER_TRADE_PCT",
                     "MAX_OPEN_POSITIONS", "MAX_TRADES_PER_DAY", "MAX_DAILY_LOSS_PCT",
                     "LOSS_STREAK_LIMIT", "COOLDOWN_DAYS", "PAPER_START_INR"]

# rule check: (key, label, settings that switch the rule off, is it on right now?)
RULES = [
    ("index_filter", T.term("index_filter"), {"INDEX_FILTER": False}, lambda: bool(C.INDEX_FILTER)),
    ("time_stop", T.term("time_stop"), {"TIME_STOP_BARS": 0}, lambda: C.TIME_STOP_BARS > 0),
    ("trail", T.term("trail"), {"TRAIL_EMA": 0}, lambda: C.TRAIL_EMA > 0),
    ("vwap", "VWAP filter", {"USE_VWAP_FILTER": False}, lambda: bool(C.USE_VWAP_FILTER)),
    ("volume", "volume filter", {"VOLUME_MULT": 0.0}, lambda: C.VOLUME_MULT > 0),
]


def advisor_dir():
    return os.path.join(C.DATA_DIR, "advisor")


# ── data ────────────────────────────────────────────────────────────────────
def session_days(allb):
    """(kept days, skipped [(day, candles)]): a day counts as a session when any symbol has at
    least MIN_SESSION_BARS candles that day."""
    most = collections.Counter()
    for bars in allb.values():
        for d, n in collections.Counter(b["t"].date() for b in bars).items():
            most[d] = max(most[d], n)
    kept = sorted(d for d, n in most.items() if n >= MIN_SESSION_BARS)
    skipped = sorted((d, n) for d, n in most.items() if n < MIN_SESSION_BARS)
    return kept, skipped


def select(allb, days):
    """Split archive bars into (stocks, indexes) restricted to `days`."""
    keep = set(days)
    stocks = {s: [b for b in allb[s] if b["t"].date() in keep] for s in C.WATCHLIST if s in allb}
    indexes = {s: [b for b in bars if b["t"].date() in keep] for s, bars in allb.items()
               if s not in C.WATCHLIST}
    return stocks, indexes


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _run(job):
    """One backtest on the archive with some settings switched. Runs in a worker process (or
    in-process when workers=1); settings are restored afterwards either way."""
    name, overrides, data_dir, day_list, trades_path = job
    saved = {k: getattr(C, k) for k in overrides}
    saved_dir = C.DATA_DIR
    try:
        C.DATA_DIR = data_dir
        for k, v in overrides.items():
            setattr(C, k, v)
        allb = archive.load()
        stocks, indexes = select(allb, [dt.date.fromisoformat(d) for d in day_list])
        res = backtest.simulate(stocks, C.PAPER_START_INR, False, indexes, {}, trades_path=trades_path)
        return name, res, read_rows(trades_path)
    finally:
        C.DATA_DIR = saved_dir
        for k, v in saved.items():
            setattr(C, k, v)


def run_jobs(jobs, workers=None):
    if workers == 1 or len(jobs) == 1:
        return [_run(j) for j in jobs]
    n = min(len(jobs), workers or max(1, (os.cpu_count() or 2) - 1))
    with ProcessPoolExecutor(max_workers=n) as ex:
        return list(ex.map(_run, jobs))


# ── slices ──────────────────────────────────────────────────────────────────
def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def reason_group(r):
    low = (r.get("reason") or "").lower()
    if low.startswith("time stop"):
        return "time stop"
    if "trail" in low:
        return "EMA trail"
    if "stop" in low:
        return "stop"
    if low.startswith("target"):
        return "target"
    if low.startswith("square-off"):
        return "square-off"
    return low or "unknown"


WINDOWS = [("09:30", "10:00"), ("10:00", "10:30"), ("10:30", "11:00"), ("11:00", "11:30"), ("11:30", "12:15")]


def entry_window(r):
    sb = r.get("signal_bar") or ""
    if len(sb) != 5:
        return "unknown"
    for lo, hi in WINDOWS:
        if lo <= sb < hi:
            return f"{lo}-{hi}"
    return f"other ({sb})"


def band(value, edges, unit):
    """'<0.5%', '0.5-0.75%', ..., '≥1%' for value against sorted edges."""
    v = _num(value)
    if v is None:
        return "unknown"
    fmt = lambda x: f"{x:g}"
    if v < edges[0]:
        return f"<{fmt(edges[0])}{unit}"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"{fmt(lo)}-{fmt(hi)}{unit}"
    return f"≥{fmt(edges[-1])}{unit}"


def weekday(r):
    try:
        return dt.datetime.fromisoformat(r["opened"]).strftime("%a")
    except (KeyError, TypeError, ValueError):
        return "unknown"


WIDTH_EDGES, VOLUME_EDGES = [0.5, 0.75, 1.0], [1.5, 2.0, 3.0]

# (name, bucket function, fixed bucket order or None = by net ₹, at_entry: knowable when entering?)
SLICES = [
    ("entry window", entry_window, [f"{lo}-{hi}" for lo, hi in WINDOWS], True),
    ("direction", lambda r: r.get("direction") or "unknown", ["LONG", "SHORT"], True),
    ("range width", lambda r: band(r.get("or_width_pct"), WIDTH_EDGES, "%"),
     [band(x, WIDTH_EDGES, "%") for x in (0.1, 0.6, 0.8, 1.2)], True),
    ("breakout volume", lambda r: band(r.get("vol_ratio"), VOLUME_EDGES, "x"),
     [band(x, VOLUME_EDGES, "x") for x in (1.2, 1.7, 2.5, 4.0)], True),
    ("weekday", weekday, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], True),
    ("stock", lambda r: r.get("symbol") or "unknown", None, True),
    ("exit reason", reason_group, None, False),
]


def stats(rows):
    e = edge.edge_check(rows, min_trades=MIN_SLICE_TRADES)
    keep = ("n", "win_rate", "avg_r", "total_net", "r_low", "r_high", "verdict")
    return {k: e.get(k) for k in keep}


def slice_table(rows, keyfn, order=None):
    groups = collections.defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    out = [{"bucket": k, **stats(v)} for k, v in groups.items()]
    if order:
        pos = {b: i for i, b in enumerate(order)}
        out.sort(key=lambda x: (pos.get(x["bucket"], len(pos)), x["bucket"]))
    else:
        out.sort(key=lambda x: -(x["total_net"] or 0))
    return out


def analyse(rows):
    """Overall edge check plus every slice for a list of trade rows."""
    return {"trades": len(rows), "edge": edge.edge_check(rows),
            "slices": {name: {"at_entry": at_entry, "rows": slice_table(rows, fn, order)}
                       for name, fn, order, at_entry in SLICES} if rows else {}}


# ── report ──────────────────────────────────────────────────────────────────
def _clean(x):
    """JSON-safe: inf/nan -> None, floats rounded, dates as text."""
    if isinstance(x, float):
        return round(x, 4) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (dt.date, dt.datetime)):
        return x.isoformat()
    return x


def build_report(days=None, rules=True, workers=None, today=None):
    t0 = time.time()
    allb = archive.load()
    if not allb:
        raise RuntimeError("The candle archive is empty - run archive.py first")
    kept, skipped = session_days(allb)
    if days:
        kept = kept[-(days + C.VOLUME_LOOKBACK_DAYS):]   # warm-up sessions for the volume filter
    stamp = (today or dt.date.today()).isoformat()
    os.makedirs(advisor_dir(), exist_ok=True)
    base_path = os.path.join(advisor_dir(), f"report_{stamp}_trades.csv")
    data_dir = os.path.abspath(C.DATA_DIR)
    day_list = [d.isoformat() for d in kept]

    active = [r for r in RULES if r[3]()] if rules else []
    with tempfile.TemporaryDirectory() as tmp:
        jobs = [("baseline", {}, data_dir, day_list, base_path)]
        jobs += [(key, off, data_dir, day_list, os.path.join(tmp, f"{key}.csv")) for key, _, off, _ in active]
        results = {name: (res, rows) for name, res, rows in run_jobs(jobs, workers)}

    base_res, base_rows = results["baseline"]
    rule_rows = []
    for key, label, off, _ in active:
        res = results[key][0]
        rule_rows.append({"rule": key, "label": label, "switched_off_with": off,
                          "with_rule": base_res, "without_rule": res,
                          "effect_net": base_res["net_pnl"] - res["net_pnl"],
                          "effect_max_dd": base_res["max_dd_pct"] - res["max_dd_pct"]})
    inactive = [label for key, label, off, on in RULES if not on()] if rules else []

    real = {}
    for mode in ("paper", "live"):
        rows = read_rows(os.path.join(C.DATA_DIR, f"trades_{mode}.csv"))
        if rows or mode == "paper":
            real[mode] = analyse(rows)

    sources = collections.Counter()
    for d in kept:
        for slot in archive._read_day(d).values():
            sources[slot["source"]] += 1

    notes = [
        "Backtest of the bot's current settings on the candle archive, charges and slippage included. "
        "It describes the past; it is not a forecast.",
        "The rule check switches off one rule at a time on the same data. A rule that hurt here is not a "
        "recommendation to remove it: any change must be tested on data it hasn't seen and approved by you.",
        f"Slices with fewer than {MIN_SLICE_TRADES} trades are marked 'few trades': their numbers can easily be luck.",
        "Exit-reason slices describe how trades ended, which isn't known when entering.",
        "Backtest and paper/live results are kept separate and never added together.",
    ]
    if C.VIX_SIZING:
        notes.append("VIX sizing is on, but India VIX is not archived, so the report sizes every trade at full risk.")
    if skipped:
        notes.append("Short sessions left out: " + ", ".join(f"{d} ({n} candles)" for d, n in skipped))
    missing = [s for s in C.WATCHLIST if s not in allb]
    if missing:
        notes.append("Not in the archive (left out): " + ", ".join(missing))

    rep = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "archive": {"first": kept[0] if kept else None, "last": kept[-1] if kept else None,
                    "sessions": len(kept), "skipped": skipped, "symbol_days_by_source": dict(sources)},
        "settings": {k: getattr(C, k) for k in STRATEGY_SETTINGS},
        "backtest": {"summary": base_res, **analyse(base_rows), "trades_file": base_path},
        "rules": rule_rows, "rules_already_off": inactive,
        "real": real,
        "notes": notes,
        "seconds": round(time.time() - t0, 1),
    }
    rep = _clean(rep)
    path = os.path.join(advisor_dir(), f"report_{stamp}.json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, ensure_ascii=False)
    os.replace(tmp_path, path)
    rep["report_file"] = path
    return rep


# ── console ─────────────────────────────────────────────────────────────────
def _money(x):
    return "-" if x is None else f"₹{x:+,.0f}"


def _r(x):
    return "-" if x is None else f"{x:+.2f}R"


VERDICT = {"too_early": "few trades", "positive": "edge", "negative": "losing",
           "unproven": "unproven", "leaning_negative": "leaning losing"}


def print_slice(name, table):
    print(f"\n  By {name}")
    print(f"    {'':16}{'trades':>7}{'win%':>7}{'avg R':>9}{'net':>11}   95% range of avg R")
    for row in table:
        rng = ("-" if row["r_low"] is None else f"{row['r_low']:+.2f} to {row['r_high']:+.2f}")
        print(f"    {row['bucket'][:16]:16}{row['n']:>7}{(row['win_rate'] or 0) * 100:>6.0f}%"
              f"{_r(row['avg_r']):>9}{_money(row['total_net']):>11}   {rng}  {VERDICT.get(row['verdict'], '')}")


def print_section(title, sec):
    e = sec["edge"]
    print(f"\n{title}: {sec['trades']} trades")
    if not sec["trades"]:
        return
    rng = "" if e.get("r_low") is None else f", 95% range {e['r_low']:+.2f} to {e['r_high']:+.2f}R"
    print(f"  Average {_r(e.get('avg_r'))}{rng} -> {VERDICT.get(e['verdict'], e['verdict'])}")
    for name, s in sec["slices"].items():
        print_slice(name + ("" if s["at_entry"] else " (known only after exit)"), s["rows"])


def print_report(rep):
    print(T.banner(f"{T.term('advisor')} · Weekly report"))
    a, s = rep["archive"], rep["backtest"]["summary"]
    print(f"Candle archive {a['first']} -> {a['last']}: {a['sessions']} sessions "
          f"({', '.join(f'{k} {v}' for k, v in sorted(a['symbol_days_by_source'].items()))} symbol-days)")
    print(f"\n{T.FIRE} Backtest of current settings {s['start']} -> {s['end']}")
    pf = "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    print(f"  Return {s['return_pct']:+.2f}% ({_money(s['net_pnl'])})   max drawdown {s['max_dd_pct']:.2f}%   "
          f"profit factor {pf}")
    print(f"  Trades {s['trades']}   win rate {s['win_rate']:.1f}%   avg {_r(s['avg_r'])}   "
          f"charges ₹{s['charges']:,.0f}   buy & hold {s['buy_hold_pct']:+.2f}%")

    if rep["rules"] or rep["rules_already_off"]:
        print("\nRule check: same data, one rule switched off at a time")
        print(f"  {'rule':30}{'trades with/without':>21}{'net with':>12}{'net without':>13}{'rule effect':>13}{'max DD with/without':>22}")
        for r in rep["rules"]:
            w, wo = r["with_rule"], r["without_rule"]
            print(f"  {r['label'][:30]:30}{w['trades']:>10} / {wo['trades']:<8}{_money(w['net_pnl']):>12}"
                  f"{_money(wo['net_pnl']):>13}{_money(r['effect_net']):>13}"
                  f"{w['max_dd_pct']:>12.2f}% / {wo['max_dd_pct']:.2f}%")
        for label in rep["rules_already_off"]:
            print(f"  {label[:30]:30}  already off in your settings - not checked")
        print("  Rule effect = net with the rule minus net without it (positive = the rule helped on this data).")

    print_section(f"\n{T.SCROLL} Backtest trades", rep["backtest"])
    for mode, sec in rep["real"].items():
        print_section(f"\n{T.BOW} {T.mode_label(mode)} trades (real fills, kept separate)", sec)
    print("\nNotes")
    for n in rep["notes"]:
        print(f"  - {n}")
    print(f"\nReport: {rep['report_file']}")
    print(f"Trades: {rep['backtest']['trades_file']}")
    print(f"({rep['seconds']} s)")


def main():
    ap = argparse.ArgumentParser(description=f"{T.term('advisor')}: weekly report (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="backtest slices, rule check and paper results")
    rp.add_argument("--days", type=int, default=None, help="only the last N trading days of the archive")
    rp.add_argument("--no-rules", action="store_true", help="skip the rule check (much faster)")
    rp.add_argument("--workers", type=int, default=None, help="parallel backtests (default: CPUs - 1)")
    a = ap.parse_args()
    if a.cmd == "report":
        print_report(build_report(a.days, not a.no_rules, a.workers))


if __name__ == "__main__":
    main()
