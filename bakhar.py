"""
Bakhar's one-year backtest: the bot's present rules run over every day in the candle archive, by the same engine
the bot's backtests use (backtest.simulate: same fills, same Kavach checks, same charges). The dashboard runs it
as a background job; nothing here touches the bot, the broker or any trade file of the bot.

It writes two files of its own: data/trades_bakhar.csv (the trade list) and data/bakhar_latest.json (the summary).
Past results describe the past. They are not a forecast and not advice.

    .venv\\Scripts\\python.exe bakhar.py
"""

import datetime as dt
import json
import os
import sys

import archive
import backtest
import config as C
import freshness
import jobs
import journal
import safefile
import yantra
import yantra_strategies as Y

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAME = "bakhar"                      # journal.trade_rows(NAME) reads data/trades_bakhar.csv


def path():
    return os.path.join(C.DATA_DIR, "bakhar_latest.json")


def trades_path():
    return os.path.join(C.DATA_DIR, f"trades_{NAME}.csv")


def build(job=jobs.NO_JOB, now=None, data=None):
    """Run the year and save it. `data` ({symbol: bars}) is only for tests."""
    now = now or dt.datetime.now(IST)
    job.plan(3, "reading the candle archive")
    allb = data if data is not None else archive.load()
    stocks = {s: b for s, b in allb.items() if s in C.WATCHLIST and b}
    indexes = {s: b for s, b in allb.items() if s.startswith("^")}
    if not stocks:
        raise RuntimeError("the candle archive has no candles for the watchlist yet")
    days = sorted({b["t"].date() for bars in stocks.values() for b in bars})
    job.advance("running the bot's rules over every archived day")
    os.makedirs(C.DATA_DIR, exist_ok=True)
    tmp = trades_path() + ".new"
    res = backtest.simulate(stocks, C.PAPER_START_INR, False, indexes, {}, trades_path=tmp, strat=Y.Orb())
    job.advance("measuring")
    with open(tmp, encoding="utf-8", newline="") as fh:
        import csv
        rows = list(csv.DictReader(fh))
    m = yantra.measure([{"day": dt.date.fromisoformat(r["closed"][:10]), "net": journal._f(r.get("net")),
                         "r": journal._f(r.get("r"))} for r in rows])
    out = {"built": now.isoformat(timespec="seconds"), "code": yantra.fingerprint(), "settings": yantra.settings(),
           "archive": {"from": str(days[0]), "to": str(days[-1]), "days": len(days), "stocks": sorted(stocks)},
           "start_cash": C.PAPER_START_INR,
           "result": {k: v for k, v in res.items() if isinstance(v, (int, float, str)) or v is None},
           "range": {k: m.get(k) for k in ("avg_r", "r_low", "r_high", "months", "months_positive", "max_drawdown")}}
    job.advance("saving")
    try:
        job.stop_if_cancelled()                  # Cancel was pressed: keep the saved result as it was
    except BaseException:
        os.remove(tmp)
        raise
    safefile.replace(tmp, trades_path())
    safefile.write_json(path(), out, indent=1, default=str)
    return out


def latest():
    try:
        with open(path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def status(c, now=None):
    """Out of date when the rules' code or settings changed, or a newer trading day has been archived."""
    now = now or dt.datetime.now(IST)
    if not c:
        return {"stale": True, "why": "not run yet"}
    built = freshness.built_at(c)
    if built is None:
        return {"stale": True, "why": "the saved result has no build time"}
    if c.get("code") != yantra.fingerprint():
        return {"stale": True, "why": "the rules' code has changed since this run"}
    changed = freshness.changed_settings(c.get("settings"), tuple(yantra.settings()))
    if changed:
        return {"stale": True, "why": f"settings changed ({', '.join(changed)})"}
    day = freshness.settled_since(built, now)
    if day:
        return {"stale": True, "why": f"{day:%d %b} is now in the archive"}
    return {"stale": False, "why": ""}


def main():
    out = build()
    r = out["result"]
    print(f"{out['archive']['from']} .. {out['archive']['to']}: {r.get('trades')} trades, return {r.get('return_pct')}%, "
          f"profit factor {r.get('profit_factor')}, average {out['range']['avg_r']:+.2f}R "
          f"(range {out['range']['r_low']:+.2f} to {out['range']['r_high']:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
