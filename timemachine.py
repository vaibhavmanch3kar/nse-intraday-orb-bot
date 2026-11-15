#!/usr/bin/env python3
"""
Time Machine: replay a trade thousands of times on the stock's own recent 5-minute moves.

For each replay, every 5-minute step from the entry time to SQUARE_OFF is filled with a real
5-minute bar of THIS stock taken at random from the same time of day over the last ~60 days
(morning bars are more volatile than afternoon bars, so time of day matters). Each bar's high,
low and close are applied relative to the previous close, and the first level touched wins.
If a stop and a target are touched inside the same bar, the stop is counted (conservative).

What it tells you: how often a target / the stop gets hit first if the stock just moves the way
it usually does at that time of day. It assumes NO edge (moves are shuffled), so it is a baseline:
a setup with a real edge should do better than these odds, a bad one worse.

    .venv\\Scripts\\python.exe timemachine.py TCS LONG 2439.8 2410.5 --at 09:45
"""

import argparse
import datetime as dt

import numpy as np

import config as C
import data_yahoo
import marketdata
import strategy

STEP_MIN = 5


SEED = 7
WINDOW = 2  # pool bars from the same time of day +/- 2 bars (+/- 10 min), as the Day Trader skill does


def slot_moves(bars5, window=WINDOW):
    """{'HH:MM': array[(rel_high, rel_low, rel_close)]} - log moves vs the previous close,
    within the same session (overnight gaps excluded), pooled over +/- `window` bars of the
    same time of day so each slot has ~5x more real bars to draw from."""
    exact = {}
    for prev, b in zip(bars5, bars5[1:]):
        if prev["t"].date() != b["t"].date() or prev["c"] <= 0:
            continue
        pc = prev["c"]
        exact.setdefault(f"{b['t']:%H:%M}", []).append(
            (np.log(b["h"] / pc), np.log(b["l"] / pc), np.log(b["c"] / pc)))
    out = {}
    for key in exact:
        t = dt.datetime.strptime(key, "%H:%M")
        pool = []
        for k in range(-window, window + 1):
            pool += exact.get(f"{t + dt.timedelta(minutes=STEP_MIN * k):%H:%M}", [])
        if len(pool) >= 5:
            out[key] = np.array(pool)
    return out


def replay(moves, direction, entry, stop, start, end, targets_r=(1.0, 2.0), n=10000, seed=SEED):
    """Simulate n paths from `start` (HH:MM, first bar after entry) to `end` (HH:MM).

    Returns {"n", "steps", "stop_first", "targets": {r: {"hit_first", "stop_first", "neither"}},
             "no_target": {"mean_r", "p_profit", "stop_hit"}} - fractions in 0..1.
    """
    rng = np.random.default_rng(seed)
    t0 = dt.datetime.strptime(start, "%H:%M")
    t1 = dt.datetime.strptime(end, "%H:%M")
    slots = []
    t = t0
    while t < t1:
        key = f"{t:%H:%M}"
        if key in moves:
            slots.append(key)
        t += dt.timedelta(minutes=STEP_MIN)
    risk = abs(entry - stop)
    sign = 1.0 if direction == "LONG" else -1.0
    if not slots or risk <= 0:
        return {"n": 0, "steps": 0}

    # log price relative to entry, for the high/low/close of every step
    hi = np.empty((n, len(slots)))
    lo = np.empty((n, len(slots)))
    cl = np.empty((n, len(slots)))
    level = np.zeros(n)
    for k, key in enumerate(slots):
        m = moves[key]
        pick = m[rng.integers(0, len(m), n)]
        hi[:, k] = level + pick[:, 0]
        lo[:, k] = level + pick[:, 1]
        level = level + pick[:, 2]
        cl[:, k] = level
    # in R units, oriented so that "up" is good for the trade
    to_r = lambda logp: sign * (entry * np.exp(logp) - entry) / risk  # noqa: E731
    best = to_r(hi if sign > 0 else lo)    # most favourable point of each bar
    worst = to_r(lo if sign > 0 else hi)   # least favourable point of each bar
    final = to_r(cl[:, -1])

    big = len(slots) + 1
    drift = float(sum(moves[k][:, 2].mean() for k in slots))  # average log move over the window
    stop_idx = np.where((worst <= -1.0).any(axis=1), (worst <= -1.0).argmax(axis=1), big)
    close_px = entry * np.exp(cl[:, -1])
    res = {"n": n, "seed": seed, "steps": len(slots), "minutes": len(slots) * STEP_MIN, "targets": {},
           "drift_pct": drift * 100,
           "close_median": float(np.median(close_px)),
           "close_p5": float(np.percentile(close_px, 5)), "close_p95": float(np.percentile(close_px, 95))}
    for tr in targets_r:
        hit = best >= tr
        tgt_idx = np.where(hit.any(axis=1), hit.argmax(axis=1), big)
        tgt_first = tgt_idx < stop_idx                     # same bar -> stop wins
        stop_first = (stop_idx <= tgt_idx) & (stop_idx < big)
        res["targets"][tr] = {"hit_first": float(tgt_first.mean()),
                              "stop_first": float(stop_first.mean()),
                              "neither": float(1 - tgt_first.mean() - stop_first.mean())}
    stopped = stop_idx < big
    r_no_target = np.where(stopped, -1.0, final)
    res["no_target"] = {"mean_r": float(r_no_target.mean()), "p_profit": float((r_no_target > 0).mean()),
                        "stop_hit": float(stopped.mean())}
    return res


def verdict(res, r=1.0):
    """One plain line, the Day Trader skill's way: among replays that hit the stop or the target,
    how often did the target come first, versus break-even 1 / (1 + R)?"""
    t = {float(k): v for k, v in (res.get("targets") or {}).items()}
    if not res.get("n") or r not in t:
        return "No verdict: not enough data."
    hit, stop = t[r]["hit_first"], t[r]["stop_first"]
    sided = hit + stop
    if sided < 0.05:
        return (f"No verdict: {1 - sided:.0%} of replays hit neither side by {C.SQUARE_OFF} "
                "(stop and target too far for the time left).")
    share, be = hit / sided, 1 / (1 + r)
    pts = (share - be) * 100
    if abs(pts) < 5:
        line = "Close to a coin flip" if r == 1.0 else "Close to break-even"
    elif abs(pts) < 15:
        line = "Leans toward the target" if pts > 0 else "Leans toward the stop"
    else:
        line = "Strongly toward the target" if pts > 0 else "Strongly toward the stop"
    out = (f"{line} at {r:g}R: target first {hit:.1%} vs stop first {stop:.1%} "
           f"({share:.0%} of decided replays; break-even is {be:.0%}).")
    if 1 - sided > 0.4:
        out += f" {1 - sided:.0%} hit neither by {C.SQUARE_OFF}, so the price then decides those."
    return out


def history(bars15, day, direction=None, index_bars=None):
    """The REAL record: run the bot's own rules (review.replay_day: same filters, stop, trail,
    time stop, square-off) on every earlier session in `bars15`. Optionally one direction only.
    Returns {"sessions", "fired", "wins", "losses", "avg_r", "by_reason", "direction"}."""
    import review
    day = dt.date.fromisoformat(str(day))
    days = sorted({b["t"].date() for b in bars15 if b["t"].date() < day})
    # the first VOLUME_LOOKBACK_DAYS sessions can't fire (no volume history), so they don't count
    days = days[C.VOLUME_LOOKBACK_DAYS:]
    rs, reasons = [], {}
    for d in days:
        r = review.replay_day(bars15, d, index_bars)
        if r.get("r") is None or (direction and r.get("direction") != direction):
            continue
        rs.append(r["r"])
        key = str(r.get("exit_reason", "")).split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    return {"sessions": len(days), "fired": len(rs), "wins": sum(1 for x in rs if x > 0),
            "losses": sum(1 for x in rs if x <= 0), "avg_r": (sum(rs) / len(rs)) if rs else None,
            "by_reason": reasons, "direction": direction}


def history_text(h):
    if not h or not h.get("sessions"):
        return "History: no earlier sessions to check."
    side = f"{h['direction'].lower()} " if h.get("direction") else ""
    if not h["fired"]:
        return f"History: the same {side}rule never fired in the previous {h['sessions']} sessions."
    reasons = ", ".join(f"{n} {k}" for k, n in sorted(h["by_reason"].items(), key=lambda kv: -kv[1]))
    return (f"History: the same {side}rule fired {h['fired']} times in the previous {h['sessions']} sessions: "
            f"{h['wins']} won, {h['losses']} lost, average {h['avg_r']:+.2f}R ({reasons}).")


def odds_for(symbol, direction, entry, stop, entry_time, n=10000, bars5=None):
    """Convenience: fetch 60 days of 5-min bars and replay from `entry_time` (HH:MM) to SQUARE_OFF."""
    if bars5 is None:
        bars5, _, _ = marketdata.fetch(symbol, STEP_MIN, "60d")
    moves = slot_moves(bars5)
    return replay(moves, direction, entry, stop, entry_time, C.SQUARE_OFF, n=n)


def summary(res):
    if not res.get("n"):
        return "not enough 5-min history"
    t = {float(k): v for k, v in res["targets"].items()}  # JSON round-trips turn keys into strings
    parts = [f"{r:g}R first {v['hit_first']:.0%}" for r, v in t.items()]
    nt = res["no_target"]
    return (f"{' · '.join(parts)} · stop first {t[min(t)]['stop_first']:.0%} (vs 1R) · "
            f"held to {C.SQUARE_OFF} without target: avg {nt['mean_r']:+.2f}R, profit {nt['p_profit']:.0%} · "
            f"price at {C.SQUARE_OFF}: median {res['close_median']:,.2f}, 90% between "
            f"{res['close_p5']:,.2f} and {res['close_p95']:,.2f}")


def main():
    ap = argparse.ArgumentParser(description="Replay a trade on the stock's own 5-minute moves")
    ap.add_argument("symbol")
    ap.add_argument("direction", choices=["LONG", "SHORT", "long", "short"])
    ap.add_argument("entry", type=float)
    ap.add_argument("stop", type=float)
    ap.add_argument("--at", default="09:45", help="entry time HH:MM (IST)")
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--date", default=None, help="session date YYYY-MM-DD for the history check (default: today)")
    a = ap.parse_args()
    d = a.direction.upper()
    res = odds_for(a.symbol.upper(), d, a.entry, a.stop, a.at, a.n)
    if not res.get("n"):
        print("Not enough 5-minute history for that time.")
        return
    import theme as T
    print(T.banner(f"{T.SEER} {T.term('time_machine')}"))
    print(f"Time Machine: {a.symbol.upper()} {d} entry {a.entry} stop {a.stop} from {a.at} to {C.SQUARE_OFF} "
          f"({res['minutes']} min, {res['n']:,} replays)")
    for r, v in res["targets"].items():
        print(f"  target {r:g}R: hit first {v['hit_first']:6.1%}   stop first {v['stop_first']:6.1%}   "
              f"neither by {C.SQUARE_OFF} {v['neither']:6.1%}")
    nt = res["no_target"]
    print(f"  no target (stop or square-off): average {nt['mean_r']:+.2f}R, ends in profit {nt['p_profit']:.1%}, "
          f"stop hit {nt['stop_hit']:.1%}")
    print(f"  Price at {C.SQUARE_OFF}: median {res['close_median']:,.2f}, 90% of replays between "
          f"{res['close_p5']:,.2f} and {res['close_p95']:,.2f}.")
    print(f"  The stock's usual move over this window in the last 60 days: {res['drift_pct']:+.2f}% "
          "(the replays inherit it).")
    day = a.date or dt.datetime.now(data_yahoo.IST).date().isoformat()
    bars15, _, _ = marketdata.fetch(a.symbol.upper(), C.INTERVAL_MIN, "60d")
    index_bars = None
    if C.INDEX_FILTER:
        index_bars, _, _ = marketdata.fetch(strategy.index_for(a.symbol.upper()), C.INTERVAL_MIN, "60d")
    print("  " + history_text(history(bars15, day, d, index_bars)))
    print("  " + verdict(res, 1.0))
    print(f"  Seed {res['seed']}: same data and seed give the same numbers. Built from the last 60 days; "
          "not a prediction - news and earnings can do things those days never did.")


if __name__ == "__main__":
    main()
