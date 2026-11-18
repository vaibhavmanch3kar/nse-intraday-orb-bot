"""
Chanakya: the research assistant. For every stock on the watchlist it lays out, from measured facts only, what
speaks for a trade right now and what speaks against it, the plan the bot would use (entry, stop, size, charges),
what Kavach would decide, and how the same kind of signal has actually done in the past.

What it is not:
  - It never places, changes or cancels an order and has no access to the broker. It reads candles and the bot's
    own saved files, and writes one file of its own (data/chanakya_latest.json).
  - It gives no probabilities and no promises. A "score" here is a count: how many of the listed checks agree.
    The historical record is shown with its sample size and range, and says so plainly when it proves nothing.
  - It cannot overrule Kavach: Kavach's answer is shown as it is, and a refusal is a refusal.
  - It is deterministic: the same candles and the same account give the same page. No language model is used.

    .venv\\Scripts\\python.exe chanakya.py            # build once and print the ranking
"""

import datetime as dt
import json
import os
import sys

import config as C
import costs
import data_yahoo
import freshness
import jobs
import kavach
import risk
import safefile
import strategy
import yantra_strategies as Y

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STALE_MIN = 16                # during market hours the picture is rebuilt after each 15-minute candle
SETTING_KEYS = ("WATCHLIST", "RISK_PER_TRADE_PCT", "MAX_POSITION_PCT", "VOLUME_MULT", "MIN_OR_PCT", "MAX_OR_PCT",
                "INDEX_FILTER", "LAST_ENTRY", "MAX_PER_SECTOR")
FOR, AGAINST, NEUTRAL, NA = "for", "against", "neutral", "n/a"


def path():
    return os.path.join(C.DATA_DIR, "chanakya_latest.json")


# ---------------------------------------------------------------- small measurements
def rsi(closes, n=14):
    """Wilder's RSI of the last value (None with too little data)."""
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        gains += max(b - a, 0)
        losses += max(a - b, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        ag = (ag * (n - 1) + max(b - a, 0)) / n
        al = (al * (n - 1) + max(a - b, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def daily(bars):
    """[{day, o, h, l, c, v}] from 15-minute candles, oldest first."""
    out = []
    for b in bars:
        d = b["t"].date()
        if not out or out[-1]["day"] != d:
            out.append({"day": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
        else:
            x = out[-1]
            x["h"], x["l"], x["c"], x["v"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"], x["v"] + b["v"]
    return out


def day_change(bars):
    """Today's % change from the day's first open to the last close (None without today's candles)."""
    if not bars:
        return None
    day = bars[-1]["t"].date()
    first = next(b for b in bars if b["t"].date() == day)
    return (bars[-1]["c"] / first["o"] - 1) * 100


def check(name, state, value, why):
    return {"name": name, "state": state, "value": value, "why": why}


# ---------------------------------------------------------------- one stock
def assess(symbol, bars, index_bars, account, history=None, peers=None, now=None):
    """Everything Chanakya has to say about one stock at its newest finished candle.

    account: {"state": risk.State (read, never saved), "prices": {...}, "live": bool, "source": "kite"|"yahoo"}
    history: {(symbol, direction): figures, "ALL": figures} from past trades of the live rules (see history()).
    peers:   {symbol: today's % change} of the stocks in the same sector (sector strength)."""
    now = now or dt.datetime.now(IST)
    out = {"symbol": symbol, "as_of": None, "direction": None, "setup": "no data", "checks": [], "score": 0, "of": 0,
           "against": 0, "plan": None, "kavach": None, "history": None, "bot_rule": "", "summary": ""}
    if not bars:
        return out
    i = len(bars) - 1
    b = bars[i]
    f = Y.Features(bars, index_bars)
    out["as_of"] = (b["t"] + dt.timedelta(minutes=C.INTERVAL_MIN)).isoformat(timespec="minutes")
    out["close"] = b["c"]
    ev = strategy.evaluate(bars, i, index_bars)
    out["bot_rule"] = ev["reason"]
    if f.or_high[i] is None or b["t"] < f.or_end[i]:
        out["setup"] = "the opening range is still forming"
        out["summary"] = f"{symbol}: nothing to assess until the first candle of the day has closed."
        return out
    hi, lo, c = f.or_high[i], f.or_low[i], b["c"]
    out["or_high"], out["or_low"] = hi, lo
    d = "LONG" if c > hi else "SHORT" if c < lo else None
    out["direction"] = d
    if d is None:
        out["setup"] = "inside the opening range"
        gap_up, gap_dn = (hi / c - 1) * 100, (1 - lo / c) * 100
        out["summary"] = (f"{symbol} is inside its opening range ({lo:,.2f} to {hi:,.2f}); {gap_up:.2f}% below the "
                          f"top and {gap_dn:.2f}% above the bottom. No setup to assess.")
        return out
    sign = 1 if d == "LONG" else -1
    word = "above" if d == "LONG" else "below"
    fresh = ev["signal"] == d
    out["setup"] = f"{d}: {'breakout on this candle' if fresh else f'{word} the opening range'}"
    stop = lo if d == "LONG" else hi
    risk_ps = abs(c - stop)
    ch = out["checks"]

    # 1. the bot's own rule on this candle
    ch.append(check("Bot's rule on this candle", FOR if fresh else NEUTRAL, ev["reason"],
                    "The live rules signal a trade on this candle." if fresh else
                    "The live rules do not signal a trade on this candle (they need a fresh close beyond the range "
                    "that passes every filter)."))
    # 2. volume
    vr = f.vol_ratio[i]
    ch.append(check("Volume", NA if vr is None else FOR if vr >= C.VOLUME_MULT else AGAINST,
                    "-" if vr is None else f"{vr:.2f}x the usual for this time",
                    f"Needs {C.VOLUME_MULT:g}x or more to count as a real move."))
    # 3. VWAP
    vw = f.vwap[i]
    ch.append(check("VWAP", FOR if (c - vw) * sign > 0 else AGAINST, f"price {c:,.2f}, VWAP {vw:,.2f}",
                    f"A {d.lower()} wants the price {word} the day's average traded price."))
    # 4. short trend
    ch.append(check("Short trend (15-min)", FOR if (f.ema9[i] - f.ema21[i]) * sign > 0 else AGAINST,
                    f"EMA9 {f.ema9[i]:,.2f}, EMA21 {f.ema21[i]:,.2f}", "The faster average on the trade's side of the slower one."))
    # 5. momentum
    r = rsi([x["c"] for x in bars[-120:]])
    if r is None:
        ch.append(check("Momentum (RSI 14)", NA, "-", "Not enough candles."))
    else:
        state = FOR if (r - 50) * sign >= 5 else AGAINST if (r - 50) * sign <= -5 else NEUTRAL
        ch.append(check("Momentum (RSI 14)", state, f"{r:.0f}", "Above 55 supports a long, below 45 a short; 45-55 says nothing."))
    # 6. the stock's index
    ic = f.index_chg[i]
    ch.append(check("Index today", NA if ic is None else FOR if ic * sign > 0 else AGAINST,
                    "-" if ic is None else f"{strategy.index_for(symbol)} {ic:+.2f}%", "The stock's index moving the same way."))
    # 7. sector strength
    if peers:
        avg = sum(peers.values()) / len(peers)
        ch.append(check("Sector today", FOR if avg * sign > 0 else AGAINST,
                        f"{len(peers)} peer(s) average {avg:+.2f}%", "Other stocks of the same sector moving the same way."))
    else:
        ch.append(check("Sector today", NA, "-", "No other stock of this sector on the watchlist."))
    # 8. daily trend
    dd = daily(bars)
    prior = dd[:-1]
    if len(prior) >= 20:
        sma20 = sum(x["c"] for x in prior[-20:]) / 20
        ch.append(check("Daily trend", FOR if (c - sma20) * sign > 0 else AGAINST,
                        f"price {c:,.2f}, 20-day average {sma20:,.2f}", "The bigger picture pointing the same way."))
    else:
        ch.append(check("Daily trend", NA, "-", "Fewer than 20 earlier sessions of data."))
    # 9. room to run: the nearest known level in the trade's direction
    levels = []
    if prior:
        levels += [("yesterday's high", prior[-1]["h"]), ("yesterday's low", prior[-1]["l"])]
    if len(prior) >= 20:
        levels += [("20-day high", max(x["h"] for x in prior[-20:])), ("20-day low", min(x["l"] for x in prior[-20:]))]
    ahead = sorted(((lvl - c) * sign, name, lvl) for name, lvl in levels if (lvl - c) * sign > 0)
    if not ahead or risk_ps <= 0:
        ch.append(check("Room to run", FOR if risk_ps > 0 else NA, "no known level ahead" if risk_ps > 0 else "-",
                        "No earlier high or low stands between the price and open space."))
    else:
        dist, name, lvl = ahead[0]
        rr = dist / risk_ps
        ch.append(check("Room to run", FOR if rr >= 1 else AGAINST if rr < 0.5 else NEUTRAL,
                        f"{name} {lvl:,.2f} is {rr:.1f}R away",
                        "The nearest earlier high or low ahead of the trade; under 0.5R leaves little room."))
    # 10. the stop against the stock's own movement
    a = f.atr[i]
    width = (hi - lo) / lo * 100
    if a:
        k = risk_ps / a
        ok = C.MIN_OR_PCT <= width <= C.MAX_OR_PCT and 0.5 <= k <= 3
        ch.append(check("Stop distance", FOR if ok else AGAINST, f"{risk_ps:,.2f} = {k:.1f} ATR; range {width:.2f}% wide",
                        "A stop closer than half the usual candle move is noise; one beyond 3 is a different trade."))
    else:
        ch.append(check("Stop distance", NA, "-", "Not enough candles for ATR."))
    # 11. time of day
    late = b["t"].time() > strategy.hm(C.LAST_ENTRY)
    ch.append(check("Time of day", AGAINST if late else FOR, f"candle {b['t']:%H:%M}",
                    f"The bot takes no new trade on candles after {C.LAST_ENTRY}."))

    # the plan the bot would use, and Kavach's answer for THIS account right now
    st = account.get("state")
    plan = {"direction": d, "entry": c, "stop": stop, "risk_per_share": risk_ps,
            "reference_2r": c + 2 * risk_ps * sign, "target": "none: the bot trails with the 21-EMA and a time stop"}
    if st is not None and risk_ps > 0:
        data = {"live": bool(account.get("live")), "source": account.get("source"),
                "signal_age_min": (now - b["t"]).total_seconds() / 60 - C.INTERVAL_MIN} if account.get("check_data") else None
        dec = kavach.pre_trade(st, symbol, d, c, stop, now.date(), prices=account.get("prices") or {},
                               data=data, signal_close=c, bar_volume=b.get("v"), sector_of=strategy.sector_for)
        out["kavach"] = {"ok": dec.ok, "qty": dec.qty, "reason": dec.reason, "check": dec.check}
        qty = dec.qty
        if not dec.ok and dec.check not in ("size", "liquidity"):
            qty = risk.size_position(st.equity(account.get("prices") or {}), c, stop, d, st.open_notional())[0]
        if qty > 0:
            charges = costs.round_trip(c, stop, qty, d)
            slip = 2 * c * qty * C.SLIPPAGE_PCT / 100
            risk_rs = risk_ps * qty
            plan.update(qty=qty, value=c * qty, risk=risk_rs, charges=charges, slippage=slip,
                        cost_pct_of_risk=(charges + slip) / risk_rs * 100 if risk_rs else None)
            cp = plan["cost_pct_of_risk"]
            ch.append(check("Costs", FOR if cp <= 25 else AGAINST if cp > 40 else NEUTRAL,
                            f"charges ₹{charges:,.2f} + slippage ₹{slip:,.2f} = {cp:.0f}% of the ₹{risk_rs:,.2f} at risk",
                            "What the trade costs even if it works; over 40% of the risk is hard to earn back."))
        else:
            ch.append(check("Costs", NA, "-", "No quantity fits, so there is nothing to cost."))
        ch.append(check("Kavach (risk engine)", FOR if dec.ok else AGAINST,
                        f"allowed, {dec.qty} share(s)" if dec.ok else f"refused: {dec.reason}",
                        "The same checks the bot applies before any order, for this account right now."))
    out["plan"] = plan

    # the record of this kind of signal
    h = (history or {}).get(f"{symbol}|{d}")
    out["history"] = h
    if h and h.get("trades"):
        proven = h["trades"] >= 20 and h["r_low"] > 0
        bad = h["trades"] >= 20 and h["r_high"] < 0
        ch.append(check("Past record", FOR if proven else AGAINST if bad else NEUTRAL,
                        f"{h['trades']} past {d.lower()} trades in {symbol}: average {h['avg_r']:+.2f}R "
                        f"(range {h['r_low']:+.2f} to {h['r_high']:+.2f})",
                        "How the live rules actually did on this stock and direction, after charges. Under 20 trades, "
                        "or a range that includes zero, proves nothing either way."))
    else:
        ch.append(check("Past record", NA, "no past trades", "The live rules have not traded this stock in this direction in the archive."))

    counted = [x for x in ch if x["state"] != NA]
    out["score"] = sum(1 for x in counted if x["state"] == FOR)
    out["against"] = sum(1 for x in counted if x["state"] == AGAINST)
    out["of"] = len(counted)
    out["summary"] = summarise(out)
    return out


def summarise(a):
    """Two or three plain sentences from the facts above. No advice, no probabilities."""
    sym, d = a["symbol"], a["direction"]
    against = [x["name"].lower() for x in a["checks"] if x["state"] == AGAINST]
    parts = [f"{sym} is {'above' if d == 'LONG' else 'below'} its opening range: {a['score']} of {a['of']} checks "
             f"agree with a {d.lower()}, {a['against']} disagree"
             + (f" ({', '.join(against[:4])}{'…' if len(against) > 4 else ''})." if against else ".")]
    k = a.get("kavach")
    if k:
        parts.append(f"Kavach would allow {k['qty']} share(s)." if k["ok"] else f"Kavach would refuse it: {k['reason']}.")
    h = a.get("history")
    if h and h.get("trades"):
        if h["trades"] >= 20 and h["r_high"] < 0:
            parts.append(f"The record is against it: {h['trades']} past trades averaged {h['avg_r']:+.2f}R after charges.")
        elif h["trades"] >= 20 and h["r_low"] > 0:
            parts.append(f"The record supports it: {h['trades']} past trades averaged {h['avg_r']:+.2f}R after charges.")
        else:
            parts.append(f"The record proves nothing either way ({h['trades']} past trades, average {h['avg_r']:+.2f}R).")
    else:
        parts.append("There is no past record for this stock and direction.")
    return " ".join(parts)


def rank(rows):
    """Stocks with a setup first, most agreeing checks first, then fewest disagreeing; the rest alphabetically."""
    return sorted(rows, key=lambda a: (a["direction"] is None, -a["score"], a["against"], a["symbol"]))


# ---------------------------------------------------------------- the record of the live rules
def history(job=jobs.NO_JOB):
    """{"SYMBOL|DIRECTION": figures, "ALL": figures, "period": [...]} from running the live rules over the candle
    archive with the lab's engine (yantra.py). Kept in data/chanakya_history.json until the code or the archive's
    last day changes."""
    import yantra
    stocks, indexes = yantra.load()
    days = sorted({b["t"].date() for bars in stocks.values() for b in bars})
    if not days:
        return {}
    key = f"{yantra.fingerprint()}|{days[-1]}|{','.join(sorted(stocks))}"
    p = os.path.join(C.DATA_DIR, "chanakya_history.json")
    try:
        with open(p, encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("key") == key:
            return saved
    except (OSError, ValueError):
        pass
    job.advance("past record of the live rules", 0)
    trades = yantra.run(Y.Orb(), days[0], days[-1], (stocks, indexes))
    out = {"key": key, "period": [days[0].isoformat(), days[-1].isoformat()], "ALL": _figs(yantra.measure(trades))}
    groups = {}
    for t in trades:
        groups.setdefault(f"{t['symbol']}|{t['direction']}", []).append(t)
    for k, tr in groups.items():
        out[k] = _figs(yantra.measure(tr))
    os.makedirs(C.DATA_DIR, exist_ok=True)
    safefile.write_json(p, out, indent=1)
    return out


def _figs(m):
    return {k: m.get(k) for k in ("trades", "win_rate", "avg_r", "r_low", "r_high", "net", "profit_factor")}


# ---------------------------------------------------------------- the build (a background job on the dashboard)
def account_now(now=None):
    """The account Kavach is asked about: the bot's own saved state for the mode it trades in (read, never
    saved), and the newest prices the bot saw."""
    status = {}
    try:
        with open(os.path.join(C.DATA_DIR, "status.json"), encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        pass
    mode = "live" if status.get("mode") == "live" else "paper"
    st = risk.State(os.path.join(C.DATA_DIR, f"state_{mode}.json"), C.PAPER_START_INR)   # only read
    prices = {p["symbol"]: p.get("price") for p in status.get("positions") or [] if p.get("price")}
    return {"state": st, "prices": prices, "live": mode == "live", "mode": mode,
            "source": (status.get("data_source") or {}).get("using"), "check_data": False,
            "equity": st.equity(prices)}


def build(symbols=None, job=jobs.NO_JOB, now=None, fetch=None):
    """Assess every stock and save the ranking. `fetch` is only for tests."""
    import review
    now = now or dt.datetime.now(IST)
    symbols = list(symbols or C.WATCHLIST)
    indices = sorted({strategy.index_for(s) for s in symbols})
    job.plan(len(symbols) + len(indices) + 1, "15-min candles")
    fetched = (fetch or review.fetch_all)(symbols + indices, job=job)
    index_data = {ix: fetched.pop(ix)[0] for ix in indices if ix in fetched}
    try:
        hist = history(job)
    except Exception as e:  # noqa: BLE001 - the page still works without the record, and says so
        hist = {"error": f"{type(e).__name__}: {e}"}
    job.advance("past record")
    acct = account_now(now)
    changes = {s: day_change(b) for s, (b, err) in fetched.items() if b}
    rows, errors = [], {}
    for sym in symbols:
        bars, err = fetched.get(sym, ([], "no data"))
        if err or not bars:
            errors[sym] = str(err or "no candles")
            continue
        sector = strategy.sector_for(sym)
        peers = {p: v for p, v in changes.items() if p != sym and v is not None and strategy.sector_for(p) == sector}
        rows.append(assess(sym, bars, index_data.get(strategy.index_for(sym)), acct, hist, peers, now))
    res = {"built": now.isoformat(timespec="seconds"), "mode": acct["mode"], "equity": acct["equity"],
           "rows": rank(rows), "errors": errors, "record": {k: hist.get(k) for k in ("ALL", "period", "error")},
           "settings": {k: getattr(C, k) for k in SETTING_KEYS},
           "as_of": max((r["as_of"] for r in rows if r.get("as_of")), default=None)}
    job.stop_if_cancelled()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    safefile.write_json(path(), res, indent=1, default=str)
    return res


def latest():
    try:
        with open(path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def status(c, now=None):
    """{"stale", "why"}: in market hours the picture is rebuilt once a newer 15-minute candle has closed."""
    now = now or dt.datetime.now(IST)
    if not c:
        return {"stale": True, "why": "nothing saved yet"}
    built = freshness.built_at(c)
    if built is None:
        return {"stale": True, "why": "the saved result has no build time"}
    changed = freshness.changed_settings(c.get("settings"), SETTING_KEYS)
    if changed:
        return {"stale": True, "why": f"settings changed ({', '.join(changed)})"}
    age = (now - built).total_seconds() / 60
    if freshness.in_session(now.date(), now) and age > STALE_MIN:
        return {"stale": True, "why": f"a newer candle has closed since {built:%H:%M}"}
    if built.date() < now.date() and freshness.in_session(now.date(), now):
        return {"stale": True, "why": "built on an earlier day"}
    return {"stale": False, "why": ""}


def main():
    res = build()
    for a in res["rows"]:
        print(f"{a['symbol']:11s} {a['setup']:38s} {a['score']}/{a['of']} for, {a['against']} against")
        if a["direction"]:
            print("            " + a["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
