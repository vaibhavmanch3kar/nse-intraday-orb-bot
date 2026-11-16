"""
Daily journal: one record per trading day, kept separately for paper and live trading.

    data/journal/journal_paper.csv          one row per trading day (journal_live.csv for live)
    data/journal/paper/2026-11-16.json      that day's detail: every signal and what happened to it,
                                            data outages, bot sessions and equity every round

RECORDING ONLY: the strategy and risk code never read these files, and the bot treats any journal
error as a warning, never as a reason to stop trading.

The bot updates the day every round (so a crash keeps everything up to the last round) and marks it
"complete" after the close. A day whose bot stopped early says so; a day whose bot died without
saying goodbye shows as "incomplete". Days with trades but no journal (before the journal existed,
or the bot never got to write it) are rebuilt from the trade log and the bot log, marked "rebuilt".
"""

import csv
import datetime as dt
import json
import os

import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

COLS = ["date", "status", "opening_balance", "start_equity", "end_equity", "net_pnl", "return_pct",
        "charges", "trades", "wins", "losses", "total_r", "best_trade", "worst_trade",
        "max_drawdown_inr", "max_drawdown_pct", "signals", "taken", "skipped", "skip_reasons",
        "outages", "outage_min", "bot_start", "bot_stop", "note"]

# full skip reason (from the dashboard row) -> short category for the CSV and the reports
SKIP_CATEGORIES = [
    ("daily loss", "daily loss limit"),
    ("trades today", "max trades a day"),
    ("positions open", "max open positions"),
    ("already in position", "already in position"),
    ("already traded today", "already traded today"),
    ("cooldown", "cool-down after losses"),
    ("price ran", "price ran past the signal"),
    ("already at stop", "price already at stop"),
    ("paused", "bot paused"),
    ("square-off", "after square-off time"),
    ("not filled", "entry not filled"),
]


def skip_category(reason):
    low = (reason or "").lower()
    for key, label in SKIP_CATEGORIES:
        if key in low:
            return label
    return "position size" if low else "not entered"


# -- paths

def journal_dir():
    return os.path.join(C.DATA_DIR, "journal")


def csv_path(mode):
    return os.path.join(journal_dir(), f"journal_{mode}.csv")


def detail_path(mode, day):
    return os.path.join(journal_dir(), mode, f"{day}.json")


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)   # raises PermissionError while Excel has the file open


def read_rows(mode):
    """The saved journal rows, oldest first ({} values as text, like any CSV)."""
    try:
        with open(csv_path(mode), encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def write_rows(mode, rows):
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in sorted(rows, key=lambda r: r["date"]):
        w.writerow({k: _cell(r.get(k)) for k in COLS})
    _write_atomic(csv_path(mode), buf.getvalue())


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 2)
    return v


def upsert_row(mode, row):
    rows = [r for r in read_rows(mode) if r.get("date") != row["date"]]
    rows.append(row)
    write_rows(mode, rows)


def load_detail(mode, day):
    try:
        with open(detail_path(mode, day), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# -- trades of a day (from the trade log, the record of truth)

def trade_rows(mode):
    try:
        with open(os.path.join(C.DATA_DIR, f"trades_{mode}.csv"), encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def trade_stats(trades):
    nets = [_f(t.get("net")) for t in trades]
    return {"trades": len(trades), "wins": sum(1 for v in nets if v > 0), "losses": sum(1 for v in nets if v <= 0),
            "total_r": sum(_f(t.get("r")) for t in trades), "charges": sum(_f(t.get("charges")) for t in trades),
            "realised": sum(nets),
            "best_trade": max(nets) if nets else None, "worst_trade": min(nets) if nets else None}


def trades_on(mode, day, all_trades=None):
    day = str(day)
    return [t for t in (all_trades if all_trades is not None else trade_rows(mode))
            if (t.get("closed") or "")[:10] == day]


# -- the bot's recorder

class DayJournal:
    """What the bot knows about one trading day. Created at the first round of the day; picks up the
    day's saved detail again after a restart, so counts add up across restarts."""

    def __init__(self, mode, day, started_at, state):
        self.mode, self.day = mode, str(day)
        d = load_detail(mode, self.day) or {}
        self.d = {"date": self.day, "mode": mode, "status": "running",
                  "opening_balance": d.get("opening_balance"), "start_equity": d.get("start_equity"),
                  "sessions": d.get("sessions", []), "signals": d.get("signals", []),
                  "outages": d.get("outages", []), "equity": d.get("equity", []), "note": d.get("note", "")}
        if "end_equity" in d:
            self.d["end_equity"] = d["end_equity"]
        if self.d["start_equity"] is None:
            self.d["start_equity"] = float(state.d.get("day_start_equity", 0.0))
        if mode == "paper":
            self.d["opening_balance"] = float(state.d.get("opening_balance", C.PAPER_START_INR))
        # an outage still open when the bot died ends where the bot was last seen
        for o in self.d["outages"]:
            if not o.get("end"):
                seen = self.d["sessions"][-1].get("last_seen") if self.d["sessions"] else None
                o["end"] = seen[11:19] if seen else o["start"]
        for s in self.d["sessions"]:
            if not s.get("stop"):
                s["stop"] = s.get("last_seen")   # the earlier run ended (or died) when it was last seen
        self.d["sessions"].append({"start": started_at.isoformat(timespec="seconds"),
                                   "last_seen": started_at.isoformat(timespec="seconds"), "stop": None})
        self._last_sample = None
        self.write_errors = 0

    # events
    def signal(self, now, sym, bar, direction, taken, reason=""):
        """One signal and what the bot did with it. A signal seen again after a restart (same stock
        and candle) is counted once; a 'taken' outcome wins over a 'skipped' one."""
        rec = {"time": now.strftime("%H:%M:%S"), "symbol": sym, "bar": bar, "direction": direction,
               "outcome": "taken" if taken else "skipped", "reason": "" if taken else reason,
               "category": "" if taken else skip_category(reason)}
        for i, old in enumerate(self.d["signals"]):
            if old["symbol"] == sym and old["bar"] == bar:
                if taken and old["outcome"] != "taken":
                    self.d["signals"][i] = rec
                return
        self.d["signals"].append(rec)

    def round(self, now, equity, positions, data_down_since):
        """Every round: equity sample (at most one a minute), outage start/end, last seen time."""
        stamp = now.strftime("%H:%M")
        if stamp != self._last_sample:
            self._last_sample = stamp
            self.d["equity"].append([now.strftime("%H:%M:%S"), round(equity, 2), positions])
        open_o = next((o for o in self.d["outages"] if not o.get("end")), None)
        if data_down_since is not None and open_o is None:
            self.d["outages"].append({"start": data_down_since.strftime("%H:%M:%S"), "end": None})
        elif data_down_since is None and open_o is not None:
            open_o["end"] = now.strftime("%H:%M:%S")
        self.d["sessions"][-1]["last_seen"] = now.isoformat(timespec="seconds")
        self.d["end_equity"] = round(equity, 2)

    def finish(self, now, equity, how):
        """how: 'complete' (after the close) or 'stopped' (STOP file before the close)."""
        self.round(now, equity, 0, None)
        for o in self.d["outages"]:
            o["end"] = o.get("end") or now.strftime("%H:%M:%S")
        self.d["sessions"][-1]["stop"] = now.isoformat(timespec="seconds")
        self.d["status"] = "complete" if how == "complete" else f"stopped {now:%H:%M}"

    # output
    def row(self):
        d, trades = self.d, trades_on(self.mode, self.day)
        ts = trade_stats(trades)
        start = float(d["start_equity"] or 0.0)
        end = float(d.get("end_equity", start))
        dd_inr, dd_pct = drawdown([start] + [e[1] for e in d["equity"]])
        skipped = [s for s in d["signals"] if s["outcome"] == "skipped"]
        reasons = {}
        for s in skipped:
            reasons[s["category"]] = reasons.get(s["category"], 0) + 1
        mins = sum(_minutes(o["start"], o["end"] or o["start"]) for o in d["outages"])
        first = d["sessions"][0]["start"][11:16] if d["sessions"] else ""
        last = d["sessions"][-1]
        stop = (last.get("stop") or last.get("last_seen") or "")[11:16]
        return {"date": self.day, "status": d["status"], "opening_balance": d.get("opening_balance"),
                "start_equity": start, "end_equity": end, "net_pnl": end - start,
                "return_pct": (end - start) / start * 100 if start else 0.0,
                "charges": ts["charges"], "trades": ts["trades"], "wins": ts["wins"], "losses": ts["losses"],
                "total_r": ts["total_r"], "best_trade": ts["best_trade"], "worst_trade": ts["worst_trade"],
                "max_drawdown_inr": dd_inr, "max_drawdown_pct": dd_pct,
                "signals": len(d["signals"]), "taken": len(d["signals"]) - len(skipped), "skipped": len(skipped),
                "skip_reasons": "; ".join(f"{k}: {v}" for k, v in sorted(reasons.items())),
                "outages": len(d["outages"]), "outage_min": round(mins),
                "bot_start": first, "bot_stop": stop,
                "note": f"{len(d['sessions'])} bot starts" if len(d["sessions"]) > 1 else ""}

    def save(self):
        """Detail JSON first (always), then the CSV row. Returns an error text or ''."""
        _write_atomic(detail_path(self.mode, self.day), json.dumps(self.d, indent=1, default=str))
        try:
            upsert_row(self.mode, self.row())
        except PermissionError:
            self.write_errors += 1
            return ("the journal CSV is open in another program (Excel?) - the day is still saved in "
                    f"{os.path.join('data', 'journal', self.mode, self.day + '.json')}; will retry")
        return ""


def drawdown(equities):
    """Largest fall from a running high: (₹, % of that high)."""
    peak, worst_inr, worst_pct = None, 0.0, 0.0
    for v in equities:
        peak = v if peak is None else max(peak, v)
        if peak and peak - v > worst_inr:
            worst_inr, worst_pct = peak - v, (peak - v) / peak * 100
    return worst_inr, worst_pct


def _minutes(a, b):
    try:
        t1, t2 = dt.datetime.strptime(a, "%H:%M:%S"), dt.datetime.strptime(b, "%H:%M:%S")
        return max(0.0, (t2 - t1).total_seconds() / 60)
    except (TypeError, ValueError):
        return 0.0


# -- days the journal doesn't have (rebuilt from the trade log and the bot log)

def _capital_history():
    try:
        import capital
        return capital.load().get("history") or []
    except Exception:  # noqa: BLE001
        return []


def _log_lines(day):
    """The bot's log lines for `day` (the per-day log file, or the old single bot.log)."""
    out = []
    for path in (os.path.join(C.DATA_DIR, "logs", f"bot_{day}.log"), os.path.join(C.DATA_DIR, "bot.log")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                out += [ln for ln in f if ln.startswith(str(day))]
        except OSError:
            pass
        if out:
            break
    return out


def rebuild_day(mode, day, all_trades=None):
    """A journal row for a day with trades but no journal, from the trade log and the bot log.
    Signals and the drawdown inside the day can't be recovered, so they stay blank."""
    day = str(day)
    all_trades = trade_rows(mode) if all_trades is None else all_trades
    trades = trades_on(mode, day, all_trades)
    ts = trade_stats(trades)
    # equity at the start of the day: first opening balance + every earlier trade + capital changes
    hist = _capital_history() if mode == "paper" else []
    opening = float(hist[0]["from"]) if hist else C.PAPER_START_INR
    for h in hist:
        if (h.get("applied_at") or "")[:10] <= day:
            opening += float(h["to"]) - float(h["from"])
    start = opening + sum(_f(t.get("net")) for t in all_trades if (t.get("closed") or "")[:10] < day)
    end = start + ts["realised"]
    lines = _log_lines(day) if mode in ("paper", "live") else []
    starts = [ln[11:16] for ln in lines if " started in " in ln and mode.upper() in ln]
    stops = [ln[11:16] for ln in lines if "STOP file found" in ln or "market closed" in ln]
    outages = sum(1 for ln in lines if "price data problem" in ln)
    return {"date": day, "status": "rebuilt", "opening_balance": opening if mode == "paper" else None,
            "start_equity": start, "end_equity": end, "net_pnl": end - start,
            "return_pct": (end - start) / start * 100 if start else 0.0,
            "charges": ts["charges"], "trades": ts["trades"], "wins": ts["wins"], "losses": ts["losses"],
            "total_r": ts["total_r"], "best_trade": ts["best_trade"], "worst_trade": ts["worst_trade"],
            "max_drawdown_inr": None, "max_drawdown_pct": None, "signals": None, "taken": ts["trades"],
            "skipped": None, "skip_reasons": "", "outages": outages or None, "outage_min": None,
            "bot_start": starts[0] if starts else "", "bot_stop": stops[-1] if stops else "",
            "note": "rebuilt from the trade log" + (f"; {len(starts)} bot starts" if len(starts) > 1 else "")}


def days(mode, today=None):
    """Every journal day, oldest first: the saved rows, plus rebuilt rows for days with trades but no
    journal (computed, not saved). A past day still marked 'running' is shown as 'incomplete'."""
    today = str(today or dt.datetime.now(IST).date())
    saved = {r["date"]: dict(r) for r in read_rows(mode)}
    for r in saved.values():
        if r.get("status") == "running" and r["date"] < today:
            r["status"] = "incomplete"
            r["note"] = ((r.get("note") or "") + f"; bot last seen {r.get('bot_stop') or '?'}").lstrip("; ")
    all_trades = trade_rows(mode)
    for day in sorted({(t.get("closed") or "")[:10] for t in all_trades} - set(saved) - {""}):
        saved[day] = rebuild_day(mode, day, all_trades)
    return [saved[k] for k in sorted(saved)]


def backfill(mode, today=None):
    """Save the rebuilt and 'incomplete' rows (the bot does this once a day at its first round).
    Returns the dates written."""
    rows = days(mode, today)
    saved = {r["date"]: r for r in read_rows(mode)}
    changed = [r["date"] for r in rows if r["date"] not in saved or saved[r["date"]].get("status") != r.get("status")]
    if changed:
        write_rows(mode, rows)
    return changed


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Daily journal (read-only for trading)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("show", help="print the journal")
    sp.add_argument("--mode", default="paper", choices=["paper", "live"])
    rb = sub.add_parser("rebuild", help="save rows for days with trades but no journal")
    rb.add_argument("--mode", default="paper", choices=["paper", "live"])
    a = ap.parse_args()
    if a.cmd == "rebuild":
        done = backfill(a.mode)
        print(f"saved {len(done)} day(s): {', '.join(done) or '-'}")
        return
    for r in days(a.mode):
        print(f"{r['date']}  {r['status']:<12} trades {r['trades']:>2}  net ₹{_f(r['net_pnl']):>10,.2f}  "
              f"equity ₹{_f(r['end_equity']):,.2f}")


if __name__ == "__main__":
    main()
