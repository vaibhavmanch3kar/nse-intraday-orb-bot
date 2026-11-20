"""
Scoreboard: is the bot's trading doing better than simply holding the stocks?

For one account (paper or real money), over every completed trading day since that account's record began:
  the bot     its trades' net result each day (after all charges, from the trade log), as a share of the
              balance it started that day with, compounded day by day. Money added or withdrawn does not
              count as a result.
  holding     equal rupees in each watchlist stock, bought at the close of the day before the record began and
              simply held, less a one-time cost of buying (BUY_COST_PCT).
  the index   the price change of the Nifty 50 over the same days (an index pays no charges, and no dividends
              are counted anywhere here).

It reads the daily journal, the trade log and the candle archive. It contacts nothing, changes nothing and
advises nothing: it is a yardstick. A few days say very little; the line is useful only over months.

    .venv\\Scripts\\python.exe scoreboard.py          # print it for both accounts
"""

import datetime as dt
import sys

import archive
import config as C
import journal

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX = "^NSEI"
BUY_COST_PCT = 0.12          # delivery buy: STT 0.1% + stamp duty 0.015% + exchange and SEBI fees with GST
FEW_DAYS = 60                # fewer completed days than this is too few to judge anything


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def daily_closes(symbols, start, end, load=None):
    """{symbol: {date: last close of that day}} from the candle archive, for start..end (inclusive)."""
    bars = (load or archive.load)(symbols=list(symbols), start=start, end=end)
    out = {}
    for sym, rows in bars.items():
        days = {}
        for b in rows:
            days[b["t"].date()] = b["c"]              # rows are oldest first: the last one of a day stays
        out[sym] = days
    return out


def bot_days(mode, journal_rows=None, trades=None):
    """[(date, the day's starting balance, net result of the trades closed that day)], oldest first."""
    rows = journal.days(mode) if journal_rows is None else journal_rows
    trades = journal.trade_rows(mode) if trades is None else trades
    net = {}
    for t in trades:
        day = str(t.get("closed") or "")[:10]
        v = _f(t.get("net"))
        if day and v is not None:
            net[day] = net.get(day, 0.0) + v
    out = []
    for r in rows:
        try:
            day = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        out.append((day, _f(r.get("start_equity")), net.get(r["date"], 0.0)))
    return sorted(out)


def build(mode, today=None, journal_rows=None, trades=None, load=None, archived=None):
    """The scoreboard for one account, or {"reason": why there is none yet}. Never raises.
    `journal_rows`, `trades`, `load` and `archived` are only passed by tests."""
    try:
        return _build(mode, today, journal_rows, trades, load, archived)
    except Exception as e:  # noqa: BLE001 - a yardstick must never break the page it is shown on
        return {"reason": f"could not be worked out ({type(e).__name__})"}


def _build(mode, today, journal_rows, trades, load, archived):
    today = today or dt.datetime.now(IST).date()
    have = archive.days() if archived is None else archived
    days = bot_days(mode, journal_rows, trades)
    if not days:
        return {"reason": "this account has no trading days on record yet"}
    if not have:
        return {"reason": "no prices are archived yet (they are saved after each market close)"}
    first = days[0][0]
    before = [d for d in have if d < first]
    if not before:
        return {"reason": "no archived price from before this account's first day"}
    base_day, last_have = before[-1], have[-1]
    done = [x for x in days if x[0] <= last_have and x[0] < today]      # completed days with archived closes
    if not done:
        return {"reason": "the first completed day is not archived yet (prices are saved after the close)"}
    last = done[-1][0]
    closes = daily_closes(list(C.WATCHLIST) + [INDEX], base_day, last, load)
    span = [d for d in have if base_day <= d <= last]

    def growth(sym, day):
        c = closes.get(sym) or {}
        return c[day] / c[base_day] if c.get(base_day) and c.get(day) else None

    stocks = [s for s in C.WATCHLIST if growth(s, last) is not None]
    if not stocks or growth(INDEX, last) is None:
        return {"reason": "archived prices are missing for the watchlist or the index on these days"}
    bot = {d: (net / eq if eq and eq > 0 else 0.0) for d, eq, net in done}
    rows, acc = [], 1.0
    for d in span[1:]:
        acc *= 1 + bot.get(d, 0.0)                    # a market day the bot did not run: no result that day
        got = [growth(s, d) for s in stocks]
        got = [g for g in got if g is not None]
        idx = growth(INDEX, d)
        rows.append({"day": d.isoformat(), "bot_pct": (acc - 1) * 100,
                     "hold_pct": ((sum(got) / len(got)) * (1 - BUY_COST_PCT / 100) - 1) * 100 if got else None,
                     "index_pct": (idx - 1) * 100 if idx else None, "bot_ran": d in bot})
    end = rows[-1]
    capital = next((eq for _, eq, _ in done if eq and eq > 0), None)
    return {"mode": mode, "from": first.isoformat(), "to": last.isoformat(), "base_day": base_day.isoformat(),
            "market_days": len(rows), "bot_days": len(done), "stocks": len(stocks),
            "bot_pct": end["bot_pct"], "hold_pct": end["hold_pct"], "index_pct": end["index_pct"],
            "vs_hold_pts": end["bot_pct"] - end["hold_pct"], "vs_index_pts": end["bot_pct"] - end["index_pct"],
            "capital": capital, "few_days": len(rows) < FEW_DAYS, "buy_cost_pct": BUY_COST_PCT, "rows": rows}


def sentence(sb):
    """One plain line for the card and the command line."""
    if sb.get("reason"):
        return f"No scoreboard yet: {sb['reason']}."

    def gap(pts, what):
        return (f"{abs(pts):.2f} points {'ahead of' if pts > 0 else 'behind'} {what}" if abs(pts) >= 0.005
                else f"level with {what}")
    return (f"Over {sb['market_days']} market day(s) the bot is {gap(sb['vs_hold_pts'], 'holding the watchlist')} "
            f"and {gap(sb['vs_index_pts'], 'the Nifty 50')}.")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    for mode in ("paper", "live"):
        sb = build(mode)
        print(f"{mode}: {sentence(sb)}")
        if not sb.get("reason"):
            print(f"   {sb['from']} to {sb['to']}: bot {sb['bot_pct']:+.2f}%, holding the {sb['stocks']} watchlist "
                  f"stocks {sb['hold_pct']:+.2f}%, Nifty 50 {sb['index_pct']:+.2f}%")


if __name__ == "__main__":
    main()
