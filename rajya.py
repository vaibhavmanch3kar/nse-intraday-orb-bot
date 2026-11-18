"""
Rajya: the portfolio view. One read-only picture of everything in the account: cash, today's intraday positions
(the bot's sleeve) and delivery holdings (your own sleeve), with allocation, concentration, volatility,
correlation, drawdown, the risk budget against Kavach's limits, what-if stress arithmetic and a rebalancing
simulation.

What it is not:
  - It never places, changes or cancels an order. From Zerodha it can make exactly three read calls: margins,
    positions (through accounts.py) and holdings (HoldingsOnly below). The rebalancing table is arithmetic on a
    rule YOU choose; nothing happens unless you place those orders yourself in Kite.
  - It is not advice and names nothing to buy. It measures what is already in the account.
  - Its history figures (volatility, worst day, correlation) describe the past year of prices. They are not
    forecasts, and real falls can be larger than any of them.
  - Showing the Paper or the Zerodha account here never changes the mode the bot trades in.

    .venv\\Scripts\\python.exe rajya.py [paper|zerodha]
"""

import datetime as dt
import json
import math
import os
import sys

import accounts
import config as C
import costs
import freshness
import jobs
import journal
import safefile
import strategy

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STALE_MIN = 16
MIN_DAYS = 20                 # fewer common days of prices than this: no volatility or correlation is shown
CORR_HIGH = 0.7               # two holdings moving together this closely count as one bet for diversification
HISTORY = "1y"
DELIVERY_STT_PCT = 0.1        # delivery trades pay STT on the buy and on the sell
DELIVERY_STAMP_PCT = 0.015
DP_CHARGE = 15.34             # depository + Zerodha charge per stock sold from holdings, GST included
MARKET_FALLS = (5, 10, 20)
SECTOR_NAMES = {"^NSEBANK": "Banks", "^CNXIT": "IT"}
SETTING_KEYS = ("MAX_POSITION_PCT", "MAX_EXPOSURE_PCT", "MAX_OPEN_POSITIONS", "MAX_DAILY_LOSS_PCT")
VIEWS = ("paper", "zerodha")


def path(view):
    return os.path.join(C.DATA_DIR, f"rajya_{view}.json")


class HoldingsOnly:
    """The one extra Zerodha call Rajya makes. It has a single method, so nothing else can be reached through it."""

    def __init__(self, kite):
        self.__kite = kite

    def holdings(self):
        return self.__kite.holdings()


# ---------------------------------------------------------------- what is in the account
def item(symbol, sleeve, qty, avg, price, stop=None, owner="bot"):
    """One position. qty is signed (negative = short); value is its size in rupees at the last price."""
    q = abs(int(qty))
    long = qty >= 0
    price = float(price if price else avg)
    return {"symbol": symbol, "sleeve": sleeve, "direction": "LONG" if long else "SHORT", "qty": q,
            "avg": float(avg), "price": price, "value": price * q, "exposure": price * q * (1 if long else -1),
            "pnl": (price - float(avg)) * q * (1 if long else -1), "stop": stop, "owner": owner}


def _read(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _bot_positions(mode):
    return ((_read(os.path.join(C.DATA_DIR, f"state_{mode}.json")) or {}).get("positions")) or {}


def gather_paper(now=None):
    d = _read(os.path.join(C.DATA_DIR, "state_paper.json")) or {}
    start = float(d.get("opening_balance", C.PAPER_START_INR))
    items = [item(s, "intraday", p["qty"] * (1 if p["direction"] == "LONG" else -1), p["entry"],
                  p.get("last_price", p["entry"]), p.get("stop")) for s, p in sorted((d.get("positions") or {}).items())]
    cash = float(d.get("cash", start))
    return {"view": "paper", "items": items, "cash_equity": cash + sum(i["pnl"] for i in items),
            "bot_equity": cash + sum(i["pnl"] for i in items),
            "day_pnl": float(d.get("day_pnl", 0.0)) + sum(i["pnl"] for i in items), "holdings_known": True,
            "warnings": [] if d else ["No paper trades yet: the account is at its starting balance."]}


def gather_zerodha(now=None, client=None):
    acct = accounts.zerodha_account(now, client)
    if acct.get("error"):
        return {"view": "zerodha", "error": acct["error"], "login_needed": bool(acct.get("login_needed")), "items": [],
                "cash_equity": 0.0, "bot_equity": 0.0, "day_pnl": 0.0, "holdings_known": False, "warnings": []}
    warnings = list(acct.get("warnings") or [])
    state = _read(os.path.join(C.DATA_DIR, "state_live.json")) or {}
    bot = state.get("positions") or {}
    items = []
    for p in acct.get("positions") or []:
        if not p.get("qty"):
            continue
        mine = bot.get(p["symbol"]) if p.get("product") == "MIS" else None
        items.append(item(p["symbol"], "intraday" if p.get("product") == "MIS" else "delivery", p["qty"], p["avg"],
                          p["price"], (mine or {}).get("stop"), "bot" if mine else "you"))
    known = True
    try:
        rows = HoldingsOnly(client if client is not None else accounts._kite_client()).holdings()
    except Exception as e:  # noqa: BLE001 - the page says so and shows the rest
        rows, known = [], False
        msg = str(e) if isinstance(e, accounts.LoginNeeded) else accounts.explain_kite_error(e)[0]
        warnings.append(f"Your delivery holdings could not be read ({msg}). The picture below covers cash and "
                        f"today's positions only.")
    for h in rows or []:
        qty = int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
        if qty:
            items.append(item(h.get("tradingsymbol"), "delivery", qty, h.get("average_price") or 0,
                              h.get("last_price") or h.get("close_price") or 0, None, "you"))
    payin = float(acct.get("payin") or 0.0)
    if payin:
        warnings.append(f"₹{payin:,.2f} added today is counted here. The bot sizes its trades from it only from the "
                        f"next trading day.")
    cash_equity = float(acct.get("equity") or 0.0) + payin
    # Kavach's limits are worked out on the balance the BOT trades from (it leaves out money added today), so
    # the risk budget uses that balance, not the larger whole.
    mine = sum(i["pnl"] for i in items if i["owner"] == "bot")
    bot_equity = float(state["cash"]) + mine if "cash" in state else cash_equity - payin
    return {"view": "zerodha", "items": items, "cash_equity": cash_equity, "bot_equity": bot_equity,
            "day_pnl": float(acct.get("pnl") or 0.0), "holdings_known": known, "warnings": warnings}


def gather(view, now=None, client=None):
    return gather_zerodha(now, client) if view == "zerodha" else gather_paper(now)


# ---------------------------------------------------------------- arithmetic
def mean(xs):
    return sum(xs) / len(xs)


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def daily_returns(closes):
    """[(day, close)] oldest first -> {day: fractional change from the day before}."""
    return {d: c / p - 1 for (_, p), (d, c) in zip(closes, closes[1:]) if p}


def correlation(a, b):
    """Pearson correlation of two {day: return} series on their common days (None with too few)."""
    days = sorted(set(a) & set(b))
    if len(days) < MIN_DAYS:
        return None
    x, y = [a[d] for d in days], [b[d] for d in days]
    sx, sy = stdev(x), stdev(y)
    if not sx or not sy:
        return None
    mx, my = mean(x), mean(y)
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / (len(x) - 1) / (sx * sy)


def beta(stock, index):
    """How much the stock moved for each 1% of the index over the common days (None with too few)."""
    days = sorted(set(stock) & set(index))
    if len(days) < MIN_DAYS:
        return None
    x, y = [index[d] for d in days], [stock[d] for d in days]
    vx = stdev(x) ** 2
    if not vx:
        return None
    mx, my = mean(x), mean(y)
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / (len(x) - 1) / vx


def worst_5pct(xs):
    """The loss that was exceeded on about 1 day in 20 (nearest rank; a positive number for a loss)."""
    s = sorted(xs)
    return -s[max(0, math.ceil(0.05 * len(s)) - 1)]


def sector_name(symbol):
    return SECTOR_NAMES.get(strategy.sector_for(symbol), "Other")


def delivery_charges(price, qty, side):
    """Estimated charges for one delivery order at Zerodha (no brokerage on delivery)."""
    turnover = price * qty
    exch = turnover * C.EXCH_TXN_PCT / 100
    sebi = turnover * C.SEBI_PCT / 100
    out = exch + sebi + (exch + sebi) * C.GST_PCT / 100 + turnover * DELIVERY_STT_PCT / 100
    return out + (turnover * DELIVERY_STAMP_PCT / 100 if side == "BUY" else DP_CHARGE)


# ---------------------------------------------------------------- the measures
def allocation(items, cash_equity):
    """Shares of the whole account: by instrument, by sector, and by sleeve. The whole = cash-side equity plus
    the value of delivery holdings; intraday positions are bought on that cash, so they are shown as exposure."""
    held = sum(i["value"] for i in items if i["sleeve"] == "delivery")
    intraday = sum(i["value"] for i in items if i["sleeve"] == "intraday")
    total = cash_equity + held
    free = cash_equity - intraday

    def pct(v):
        return v / total * 100 if total > 0 else 0.0

    rows = [{"symbol": i["symbol"], "sleeve": i["sleeve"], "direction": i["direction"], "sector": sector_name(i["symbol"]),
             "owner": i["owner"], "value": i["value"], "pct": pct(i["value"]), "pnl": i["pnl"], "qty": i["qty"],
             "price": i["price"], "avg": i["avg"]}
            for i in sorted(items, key=lambda i: -i["value"])]
    sectors = {}
    for r in rows:
        sectors[r["sector"]] = sectors.get(r["sector"], 0.0) + r["value"]
    return {"total": total, "cash_equity": cash_equity, "holdings_value": held, "intraday_value": intraday,
            "free_cash": free, "leveraged": free < 0, "rows": rows,
            "by_sector": [{"name": k, "value": v, "pct": pct(v)} for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])],
            "by_sleeve": [{"name": "Intraday (the bot's sleeve)", "value": intraday, "pct": pct(intraday)},
                          {"name": "Delivery holdings (your sleeve)", "value": held, "pct": pct(held)},
                          {"name": "Cash not in any position", "value": max(free, 0.0), "pct": pct(max(free, 0.0))}]}


def concentration(alloc):
    rows = alloc["rows"]
    invested = sum(r["value"] for r in rows)
    if not rows or invested <= 0:
        return {"positions": 0}
    w = [r["value"] / invested for r in rows]
    out = {"positions": len(rows), "largest": rows[0]["symbol"], "largest_pct": rows[0]["pct"],
           "top3_pct": sum(r["pct"] for r in rows[:3]), "effective": 1 / sum(x * x for x in w),
           "limit_pct": C.MAX_POSITION_PCT}
    out["over_limit"] = [r["symbol"] for r in rows if r["pct"] > C.MAX_POSITION_PCT + 1e-9]
    return out


def history_measures(items, total, closes, index=None):
    """From the past year's daily closes: each position's volatility and beta, the pairs that move together, and
    how THIS mix of positions would have moved day by day (today's sizes replayed over past prices)."""
    index = index or C.INDEX_DEFAULT
    rets = {s: daily_returns(c) for s, c in closes.items() if c}
    idx = rets.get(index, {})
    per, covered = [], []
    for i in items:
        r = rets.get(i["symbol"])
        if not r or len(r) < MIN_DAYS:
            per.append({"symbol": i["symbol"], "covered": False})
            continue
        covered.append(i)
        vals = list(r.values())
        sd = stdev(vals)
        per.append({"symbol": i["symbol"], "covered": True, "days": len(vals), "vol_day_pct": sd * 100,
                    "vol_year_pct": sd * math.sqrt(252) * 100, "beta": beta(r, idx),
                    "move_1in20": worst_5pct([v * i["exposure"] for v in vals])})
    pairs = []
    syms = sorted({i["symbol"] for i in covered})
    for n, a in enumerate(syms):
        for b in syms[n + 1:]:
            c = correlation(rets[a], rets[b])
            if c is not None:
                pairs.append({"a": a, "b": b, "corr": c, "high": c >= CORR_HIGH})
    pairs.sort(key=lambda p: -p["corr"])
    out = {"per": per, "pairs": pairs, "not_covered": [p["symbol"] for p in per if not p["covered"]],
           "avg_corr": mean([p["corr"] for p in pairs]) if pairs else None, "mix": None}
    if covered:
        days = sorted(set.intersection(*[set(rets[i["symbol"]]) for i in covered]))
        if len(days) >= MIN_DAYS:
            moves = [sum(i["exposure"] * rets[i["symbol"]][d] for i in covered) for d in days]
            worst = min(range(len(moves)), key=lambda k: moves[k])
            curve, v = [total], total
            for m in moves:
                v += m
                curve.append(v)
            dd_inr, dd_pct = journal.drawdown(curve)
            sd = stdev(moves)
            out["mix"] = {"days": len(days), "from": str(days[0]), "to": str(days[-1]), "vol_day": sd,
                          "vol_day_pct": sd / total * 100 if total > 0 else None,
                          "vol_year_pct": sd * math.sqrt(252) / total * 100 if total > 0 else None,
                          "worst_day": str(days[worst]), "worst_move": moves[worst], "loss_1in20": worst_5pct(moves),
                          "drawdown": dd_inr, "drawdown_pct": dd_pct, "left_out": out["not_covered"]}
    return out


def record(mode, today=None):
    """The bot's booked result after charges, from its trade log: totals, the largest fall from a high, and how
    far below its high it stands now."""
    trades = sorted(journal.trade_rows(mode), key=lambda t: t.get("closed") or "")
    nets = [journal._f(t.get("net")) for t in trades]
    curve, v = [0.0], 0.0
    for n in nets:
        v += n
        curve.append(v)
    peak, worst = 0.0, 0.0
    for x in curve:
        peak = max(peak, x)
        worst = max(worst, peak - x)
    day = str(today) if today else None
    todays = [t for t in trades if day and (t.get("closed") or "")[:10] == day]
    return {"trades": len(trades), "net": v, "gross": sum(journal._f(t.get("gross")) for t in trades),
            "charges": sum(journal._f(t.get("charges")) for t in trades), "drawdown": worst, "below_high": peak - v,
            "wins": sum(1 for n in nets if n > 0), "today_trades": len(todays),
            "today_net": sum(journal._f(t.get("net")) for t in todays),
            "first": (trades[0].get("closed") or "")[:10] if trades else None}


def risk_budget(items, cash_equity, day_pnl):
    """The intraday sleeve against Kavach's limits, and what stands unprotected. cash_equity here is the balance
    the bot trades from."""
    intraday = [i for i in items if i["sleeve"] == "intraday"]
    rows, at_risk = [], 0.0
    for i in items:
        if i["stop"]:
            r = max(0.0, (i["price"] - i["stop"]) * i["qty"] * (1 if i["direction"] == "LONG" else -1))
            at_risk += r
        else:
            r = None
        rows.append({"symbol": i["symbol"], "sleeve": i["sleeve"], "stop": i["stop"], "risk": r, "value": i["value"]})
    exposure = sum(i["value"] for i in intraday)
    limit = cash_equity * C.MAX_DAILY_LOSS_PCT / 100
    exit_charges = sum(costs.order_charges(i["price"], i["qty"], "SELL" if i["direction"] == "LONG" else "BUY")
                       for i in intraday)
    return {"rows": rows, "at_risk": at_risk, "at_risk_pct": at_risk / cash_equity * 100 if cash_equity > 0 else None,
            "day_pnl": day_pnl, "day_limit": limit, "day_left": limit + min(day_pnl, 0.0),
            "worst_today": min(day_pnl, 0.0) - at_risk - exit_charges, "exit_charges": exit_charges,
            "exposure": exposure, "exposure_pct": exposure / cash_equity * 100 if cash_equity > 0 else None,
            "exposure_limit_pct": C.MAX_EXPOSURE_PCT, "open": len(intraday), "open_limit": C.MAX_OPEN_POSITIONS,
            "no_stop": [r["symbol"] for r in rows if r["risk"] is None]}


def stress(items, total, hist, budget):
    """What-if arithmetic. Each line is one stated assumption applied to today's positions; none is a forecast."""
    if not items:
        return []
    betas = {p["symbol"]: p.get("beta") for p in hist["per"] if p.get("covered")}
    out = []

    def add(name, what, loss):
        out.append({"name": name, "what": what, "loss": loss, "pct": loss / total * 100 if total > 0 else None})

    if any(i["stop"] for i in items):
        add("Every stop is hit", "Each position with a stop closes exactly at its stop, plus exit charges. A gap "
            "through the stop loses more than this.", budget["at_risk"] + budget["exit_charges"])
    assumed = sorted(i["symbol"] for i in items if betas.get(i["symbol"]) is None)
    for x in MARKET_FALLS:
        loss = sum(i["exposure"] * (betas.get(i["symbol"]) if betas.get(i["symbol"]) is not None else 1.0) * x / 100
                   for i in items)
        add(f"NIFTY falls {x}%", "Each position moves by its own past sensitivity to NIFTY, stops ignored"
            + (f" ({', '.join(assumed)}: no price history, assumed to move 1 for 1)." if assumed else "."), loss)
    if items:
        big = max(items, key=lambda i: i["value"])
        add(f"{big['symbol']} alone falls 20%" if big["direction"] == "LONG" else f"{big['symbol']} alone rises 20%",
            "The largest position moves 20% against you; everything else stays where it is.", big["value"] * 0.2)
    mix = hist.get("mix")
    if mix:
        left = (f" Left out, no price history: {', '.join(mix['left_out'])}." if mix.get("left_out") else "")
        add(f"The worst day of the past year again ({mix['worst_day']})",
            f"Today's positions on the worst of the last {mix['days']} trading days for this mix.{left}",
            -mix["worst_move"])
    return out


def rebalance(alloc, rule="cap", cap_pct=None):
    """A SIMULATION of bringing the delivery sleeve to a rule you choose. Nothing is ordered.
    rule 'cap': sell just enough of any holding above cap_pct of the account (the money stays as cash);
    rule 'equal': move every holding to the same value, buying and selling among the holdings already owned."""
    cap_pct = float(C.MAX_POSITION_PCT if cap_pct is None else cap_pct)
    total = alloc["total"]
    held = [r for r in alloc["rows"] if r["sleeve"] == "delivery" and r["direction"] == "LONG" and r.get("price")]
    trades = []
    target_each = sum(r["value"] for r in held) / len(held) if held else 0.0
    for r in held:
        price, qty = r["price"], r["qty"]
        if rule == "equal":
            n = int((target_each - r["value"]) / price)              # whole shares, never past the target
        else:
            over = r["value"] - cap_pct / 100 * total
            n = -min(qty, math.ceil(over / price - 1e-9)) if over > 0 else 0
        if not n:
            continue
        side = "BUY" if n > 0 else "SELL"
        value = abs(n) * price
        after = r["value"] + n * price
        trades.append({"symbol": r["symbol"], "action": side, "qty": abs(n), "price": price, "value": value,
                       "charges": delivery_charges(price, abs(n), side), "before_pct": r["pct"],
                       "after_pct": after / total * 100 if total > 0 else 0.0})
    sold = sum(t["value"] for t in trades if t["action"] == "SELL")
    bought = sum(t["value"] for t in trades if t["action"] == "BUY")
    charges = sum(t["charges"] for t in trades)
    return {"rule": rule, "cap_pct": cap_pct, "trades": trades, "sold": sold, "bought": bought, "charges": charges,
            "cash_change": sold - bought - charges, "holdings": len(held), "executes": False}


def measure(g, closes, now=None):
    now = now or dt.datetime.now(IST)
    items = g["items"]
    alloc = allocation(items, g["cash_equity"])
    hist = history_measures(items, alloc["total"], closes)
    budget = risk_budget(items, g.get("bot_equity", g["cash_equity"]), g["day_pnl"])
    budget["base"] = g.get("bot_equity", g["cash_equity"])
    mode = "live" if g["view"] == "zerodha" else "paper"
    return {"view": g["view"], "warnings": g["warnings"], "holdings_known": g["holdings_known"], "allocation": alloc,
            "concentration": concentration(alloc), "history": hist, "budget": budget,
            "stress": stress(items, alloc["total"], hist, budget), "record": record(mode, now.date()),
            "settings": {k: getattr(C, k) for k in SETTING_KEYS}}


# ---------------------------------------------------------------- the build (a background job on the dashboard)
def fetch_closes(symbols, job=jobs.NO_JOB, now=None):
    """{symbol: [(day, close)]} of finished daily candles for the past year (missing symbols are left out)."""
    import marketdata
    now = now or dt.datetime.now(IST)
    got = marketdata.fetch_many(symbols, range_=HISTORY, workers=C.FETCH_WORKERS, interval="1d",
                                on_done=lambda s: job.advance(f"daily prices: {s}"), stop=job.over_time)
    close_t = dt.datetime.combine(now.date(), dt.time(15, 30), tzinfo=IST)
    out = {}
    for s, g in got.items():
        if isinstance(g, Exception) or not g[0]:
            continue
        bars = g[0]
        if bars[-1]["t"].date() == now.date() and now < close_t:      # today's daily candle is still forming
            bars = bars[:-1]
        out[s] = [(str(b["t"].date()), b["c"]) for b in bars]
    return out


def build(view="paper", job=jobs.NO_JOB, now=None, client=None, fetch=None):
    now = now or dt.datetime.now(IST)
    view = view if view in VIEWS else "paper"
    g = gather(view, now, client)
    if g.get("error"):
        res = {"view": view, "error": g["error"], "login_needed": g.get("login_needed", False)}
    else:
        symbols = sorted({i["symbol"] for i in g["items"]})
        job.plan(len(symbols) + 1, "daily prices")
        closes = (fetch or fetch_closes)(symbols + [C.INDEX_DEFAULT], job) if symbols else {}
        res = measure(g, closes, now)
    res["built"] = now.isoformat(timespec="seconds")
    job.stop_if_cancelled()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    safefile.write_json(path(view), res, indent=1, default=str)
    return res


def latest(view):
    return _read(path(view if view in VIEWS else "paper"))


def status(c, now=None):
    now = now or dt.datetime.now(IST)
    if not c:
        return {"stale": True, "why": "nothing saved yet"}
    built = freshness.built_at(c)
    if built is None:
        return {"stale": True, "why": "the saved result has no build time"}
    changed = freshness.changed_settings(c.get("settings"), SETTING_KEYS) if c.get("settings") else []
    if changed:
        return {"stale": True, "why": f"settings changed ({', '.join(changed)})"}
    age = (now - built).total_seconds() / 60
    if freshness.in_session(now.date(), now) and age > STALE_MIN:
        return {"stale": True, "why": f"prices have moved since {built:%H:%M}"}
    if built.date() < now.date():
        return {"stale": True, "why": "built on an earlier day"}
    return {"stale": False, "why": ""}


def main():
    view = sys.argv[1] if len(sys.argv) > 1 else "paper"
    res = build(view)
    if res.get("error"):
        print(res["error"])
        return 1
    a = res["allocation"]
    print(f"{view}: whole account ₹{a['total']:,.2f} (cash side ₹{a['cash_equity']:,.2f}, holdings ₹{a['holdings_value']:,.2f})")
    for r in a["rows"]:
        print(f"  {r['symbol']:12s} {r['sleeve']:9s} ₹{r['value']:>12,.2f} {r['pct']:6.1f}%")
    for s in res["stress"]:
        print(f"  what if: {s['name']:45s} ₹{s['loss']:>10,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
