#!/usr/bin/env python3
"""
Portfolio backtest of the ORB strategy on Yahoo 15-minute NSE candles (max ~60 days),
using the same strategy, sizing, risk gates and Zerodha charges as the live bot.

    .venv\\Scripts\\python.exe backtest.py                # 60 days, default watchlist
    .venv\\Scripts\\python.exe backtest.py --days 30 --symbols SBIN,INFY

Fills: entry at the NEXT bar's open (+ slippage) after the signal bar closes;
stop/target inside a bar fill at the level (stop first if both); square-off at the close
of the last bar ending by SQUARE_OFF.
"""

import argparse
import collections
import csv
import datetime as dt
import os
import tempfile

import config as C
import data_yahoo
import kavach
import marketdata
import risk
import strategy


def run(days=60, symbols=None, start_cash=None, verbose=True):
    symbols = symbols or C.WATCHLIST
    start_cash = start_cash or C.PAPER_START_INR
    data = {}
    now = dt.datetime.now(data_yahoo.IST)
    for s, got in marketdata.fetch_many(symbols, C.INTERVAL_MIN, f"{days}d", C.FETCH_WORKERS).items():
        if isinstance(got, Exception):
            print(f"[!] {s}: {got}")
            continue
        data[s] = data_yahoo.closed_bars(got[0], now, C.INTERVAL_MIN)
    index_data, vix = load_context(days, list(data))
    return simulate(data, start_cash, verbose, index_data, vix)


def load_context(days, symbols):
    """Index bars (for INDEX_FILTER) and India VIX by day (for VIX_SIZING). Always fetched,
    so the same data can be reused when comparing settings."""
    now = dt.datetime.now(data_yahoo.IST)
    index_data = {}
    for ix in {strategy.index_for(s) for s in symbols}:
        try:
            bars, _, _ = marketdata.fetch(ix, C.INTERVAL_MIN, f"{days}d")
            index_data[ix] = data_yahoo.closed_bars(bars, now, C.INTERVAL_MIN)
        except data_yahoo.DataError as e:
            print(f"[!] index {ix}: {e}")
    try:
        vix = marketdata.vix_by_day("6mo")
    except data_yahoo.DataError as e:
        print(f"[!] India VIX: {e}")
        vix = {}
    return index_data, vix


def prev_vix(vix, day):
    earlier = [d for d in vix if d < day]
    return vix[max(earlier)] if earlier else None


def simulate(data, start_cash, verbose=True, index_data=None, vix=None, trades_path=None):
    """trades_path: where to write the trade list (default data/backtest_trades.csv). The advisor
    passes its own path so its runs never overwrite your backtest results."""
    index_data, vix = index_data or {}, vix or {}
    tmp = tempfile.mkdtemp()
    st = risk.State(os.path.join(tmp, "bt_state.json"), start_cash)
    trades = []
    pending = {}     # symbol -> signal waiting for next bar open
    idx = {s: {b["t"]: i for i, b in enumerate(bars)} for s, bars in data.items()}
    times = sorted({b["t"] for bars in data.values() for b in bars})
    first_day = min((b["t"].date() for bars in data.values() for b in bars), default=None)
    sim_from = None
    if first_day:  # need volume history before trading: skip the first VOLUME_LOOKBACK_DAYS sessions
        days_sorted = sorted({t.date() for t in times})
        sim_from = days_sorted[min(len(days_sorted) - 1, C.VOLUME_LOOKBACK_DAYS)]
    equity_curve = []
    last_close = {}  # symbol -> latest bar close, to value open positions at market (not at entry)

    for t in times:
        day = t.date()
        if day < sim_from:
            continue
        st.new_day_if_needed(day)
        # 1) pending entries fill at this bar's open - strongest volume first, so when there are
        #    more signals than free slots the bot takes the best ones (not alphabetical order)
        for s, sig in sorted(pending.items(), key=lambda kv: -kv[1]["rank"]):
            i = idx[s].get(t)
            if i is None:
                continue
            bars = data[s]
            b = bars[i]
            pending.pop(s)
            if b["t"].date() == sig["day"] and s not in st.positions:
                d, stop = sig["direction"], sig["stop"]
                beyond = (d == "LONG" and b["o"] <= stop) or (d == "SHORT" and b["o"] >= stop)
                if not beyond:
                    slip = b["o"] * C.SLIPPAGE_PCT / 100
                    entry = b["o"] + slip if d == "LONG" else b["o"] - slip
                    mult = strategy.vix_multiplier(prev_vix(vix, day)) if C.VIX_SIZING else 1.0
                    # the same Kavach checks as the live bot (kavach.py). Open losses count toward the daily
                    # limit (prices=last_close); the size comes from booked equity, as before.
                    decision = kavach.pre_trade(st, s, d, entry, stop, day, prices=last_close, equity=st.equity(),
                                                risk_mult=mult, sector_of=strategy.index_for)
                    if decision.ok:
                        st.record_entry(s, d, decision.qty, entry, stop, strategy.target_for(entry, stop, d),
                                        b["t"], {"held": 0, **(sig["context"] or {})})
        for s, bars in data.items():
            i = idx[s].get(t)
            if i is None:
                continue
            b = bars[i]
            last_close[s] = b["c"]
            # 2) exits on this bar
            if s in st.positions:
                pos = st.positions[s]
                pos["held"] = pos.get("held", 0) + 1
                hit = strategy.manage_exit(pos, bars, i, pos["held"])
                if not hit and strategy.is_square_off_bar(b):
                    hit = ("square-off", b["c"])
                if hit:
                    reason, px = hit
                    slip = px * C.SLIPPAGE_PCT / 100 if reason != "target" else 0.0
                    px = px - slip if pos["direction"] == "LONG" else px + slip
                    row = st.record_exit(s, px, b["t"] + dt.timedelta(minutes=C.INTERVAL_MIN))
                    row["reason"] = reason
                    trades.append(row)
            # 3) signal on this closed bar -> enter at next bar open
            if s not in st.positions and not strategy.is_square_off_bar(b):
                ev = strategy.evaluate(bars, i, index_data.get(strategy.index_for(s)))
                if ev["signal"]:
                    pending[s] = {"direction": ev["signal"], "stop": ev["stop"], "day": day,
                                  "rank": ev["vol_ratio"] or 0.0, "context": ev["context"]}
        # mark open positions to this bar's close, so drawdowns inside open trades are counted
        equity_curve.append((t, st.equity(last_close)))

    return report(trades, st, start_cash, equity_curve, data, sim_from, verbose, trades_path)


def report(trades, st, start_cash, curve, data, sim_from, verbose, trades_path=None):
    end_eq = st.equity()
    peak, max_dd = start_cash, 0.0
    for _, e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)
    wins = [t for t in trades if t["net"] > 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in trades if t["net"] <= 0)
    pf = gross_win / gross_loss if gross_loss else float("inf")
    # equal-weight buy & hold over the same window
    bh = []
    for bars in data.values():
        b = [x for x in bars if sim_from and x["t"].date() >= sim_from]
        if b:
            bh.append(b[-1]["c"] / b[0]["o"] - 1)
    res = {
        "start": str(sim_from), "end": str(curve[-1][0].date()) if curve else "-",
        "trades": len(trades), "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "return_pct": (end_eq / start_cash - 1) * 100, "max_dd_pct": max_dd, "profit_factor": pf,
        "net_pnl": end_eq - start_cash, "charges": st.d["charges_paid"],
        "avg_r": sum(t["r"] for t in trades) / len(trades) if trades else 0.0,
        "buy_hold_pct": sum(bh) / len(bh) * 100 if bh else 0.0,
    }
    path = trades_path or os.path.join(C.DATA_DIR, "backtest_trades.csv")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ["opened", "closed", "symbol", "direction", "qty", "entry", "exit", "stop", "target",
            "gross", "charges", "net", "r", "reason"] + strategy.CONTEXT_COLS
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()})
    if verbose:
        import theme as T
        print("\n" + T.banner(f"{T.FIRE} {T.term('backtest')}"))
        print(f"ORB backtest  {res['start']} -> {res['end']}  ({len(data)} stocks, ₹{start_cash:,.0f})")
        print(f"  Return        {res['return_pct']:+.2f}%   (net ₹{res['net_pnl']:,.0f})")
        print(f"  Max drawdown  {res['max_dd_pct']:.2f}%")
        print(f"  Trades        {res['trades']}   win rate {res['win_rate']:.1f}%   avg {res['avg_r']:+.2f}R")
        print(f"  Profit factor {res['profit_factor']:.2f}")
        print(f"  Charges paid  ₹{res['charges']:,.0f}")
        print(f"  Buy & hold    {res['buy_hold_pct']:+.2f}% (equal weight, same window)")
        per = collections.defaultdict(float)
        reasons = collections.Counter()
        for t in trades:
            per[t["symbol"]] += t["net"]
            reasons[t["reason"]] += 1
        if per:
            print("  P&L by stock  " + ", ".join(f"{s} {v:+,.0f}" for s, v in sorted(per.items(), key=lambda x: -x[1])))
            print("  Exit reasons  " + ", ".join(f"{T.reason_label(r)} {n}" for r, n in reasons.most_common()))
        print(f"  Trade list    {path}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60,
                    help="Yahoo keeps ~60 days of 15m data; with DATA_SOURCE=kite up to 365")
    ap.add_argument("--symbols", default="", help="comma list, default = WATCHLIST")
    ap.add_argument("--cash", type=float, default=None)
    a = ap.parse_args()
    syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()] or None
    limit = marketdata.max_history_days()
    if a.days > limit:
        print(f"[i] {marketdata.status()['using'].title()} data goes back about {limit} days - using {limit}")
    run(min(a.days, limit), syms, a.cash)


if __name__ == "__main__":
    main()
