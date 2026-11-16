#!/usr/bin/env python3
"""
Saved weekly and monthly reports: one self-contained HTML file per week or month, from the daily journal.

    data/reports/paper_week_2026-W47.html     (live_... for live trading)
    data/reports/paper_month_2026-11.html

The bot writes them after the close on the last trading day of each week and month. By hand:

    .venv\\Scripts\\python.exe reports.py --week 2026-W47
    .venv\\Scripts\\python.exe reports.py --month 2026-11 [--mode live]
    .venv\\Scripts\\python.exe reports.py --list

Each file opens in any browser and prints cleanly (Ctrl+P, "Save as PDF"). Read-only for trading:
they only read the journal and the trade log, and write only to data/reports/.
"""

import datetime as dt
import html
import os
import re

import config as C
import holidays
import journal

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAME_RE = re.compile(r"^(paper|live)_(week_\d{4}-W\d{2}|month_\d{4}-\d{2})\.html$")
UP, DN, MUTED, LINE, ACC = "#15803d", "#b91c1c", "#6b6258", "#ddd3c4", "#c2410c"


def reports_dir():
    return os.path.join(C.DATA_DIR, "reports")


def file_name(mode, period, key):
    return f"{mode}_{period}_{key}.html"


def saved(mode=None):
    """Saved report file names, newest first (optionally only one mode's)."""
    try:
        names = [n for n in os.listdir(reports_dir()) if NAME_RE.match(n)]
    except OSError:
        return []
    if mode:
        names = [n for n in names if n.startswith(mode + "_")]
    return sorted(names, key=_period_end, reverse=True)


def _period_end(name):
    """The last day a saved report covers (to list them newest first; a month after its last week)."""
    _, period, key = name[:-5].split("_", 2)
    if period == "week":
        y, w = key.split("-W")
        return (dt.date.fromisocalendar(int(y), int(w), 7), 0)
    y, m = map(int, key.split("-"))
    first_next = dt.date(y + m // 12, m % 12 + 1, 1)
    return (first_next - dt.timedelta(days=1), 1)


def due(today):
    """What to write after today's close: the week if the next trading day is in another ISO week,
    the month if it is in another month."""
    nxt = holidays.next_trading_day(today)
    out = []
    if nxt.isocalendar()[:2] != today.isocalendar()[:2]:
        out.append(("week", journal.period_key(today.isoformat(), "week")))
    if (nxt.year, nxt.month) != (today.year, today.month):
        out.append(("month", journal.period_key(today.isoformat(), "month")))
    return out


def catch_up(mode, today):
    """Reports the bot should have written after the last trading day's close but didn't (it
    stopped early or crashed). Called at the first round of a new day; returns the paths written."""
    prev = today - dt.timedelta(days=1)
    for _ in range(15):
        if holidays.is_trading_day(prev):
            break
        prev -= dt.timedelta(days=1)
    out = []
    for period, key in due(prev):
        if not os.path.exists(os.path.join(reports_dir(), file_name(mode, period, key))):
            path = write(mode, period, key, today)
            if path:
                out.append(path)
    return out


def _inr(v, sign=False):
    if v is None:
        return "–"
    s = f"{'-' if v < 0 else '+' if sign and v > 0 else ''}₹{abs(v):,.2f}"
    cls = "up" if v > 0 else "dn" if v < 0 else ""
    return f"<span class='{cls}'>{s}</span>" if sign else s


def _opt(v, fmt):
    return "–" if v is None else fmt.format(v)


def equity_svg(points, w=760, h=200):
    """Equity at the start and after each day's close; red area = below the running high."""
    if len(points) < 2:
        return ""
    vals = [p[1] for p in points]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or max(abs(hi) * 0.002, 1)
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 70, 10, 10, h - 24
    x = lambda i: left + i * (w - left - right) / (len(points) - 1)  # noqa: E731
    y = lambda v: bottom - (v - lo) / (hi - lo) * (bottom - top)      # noqa: E731
    peak, top_pts, bot_pts = vals[0], [], []
    for i, v in enumerate(vals):
        peak = max(peak, v)
        top_pts.append(f"{x(i):.1f},{y(peak):.1f}")
        bot_pts.append(f"{x(i):.1f},{y(v):.1f}")
    col = UP if vals[-1] >= vals[0] else DN
    parts = [f"<line x1='{left}' x2='{w - right}' y1='{y(vals[0]):.1f}' y2='{y(vals[0]):.1f}' stroke='{MUTED}' "
             f"stroke-dasharray='4 4'/>",
             f"<polygon points='{' '.join(top_pts + bot_pts[::-1])}' fill='{DN}' opacity='0.15'/>",
             f"<polyline points='{' '.join(bot_pts)}' fill='none' stroke='{col}' stroke-width='2'/>"]
    for v in (max(vals), min(vals)):
        parts.append(f"<text x='{left - 6}' y='{y(v) + 4:.1f}' font-size='11' fill='{MUTED}' text-anchor='end'>"
                     f"₹{v:,.0f}</text>")
    for i, (label, v) in enumerate(points):
        parts.append(f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='3' fill='{col}'><title>{html.escape(label)}: "
                     f"₹{v:,.2f}</title></circle>")
    parts.append(f"<text x='{left}' y='{h - 6}' font-size='11' fill='{MUTED}'>{html.escape(points[0][0])}</text>"
                 f"<text x='{w - right}' y='{h - 6}' font-size='11' fill='{MUTED}' text-anchor='end'>"
                 f"{html.escape(points[-1][0])}</text>")
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' aria-label='equity by day'>"
            f"{''.join(parts)}</svg>")


CSS = f"""
body{{font-family:Segoe UI,Arial,sans-serif;color:#2b2420;background:#fffdf8;max-width:980px;margin:24px auto;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:22px 0 8px;color:{ACC}}}
.muted{{color:{MUTED};font-size:13px}} .up{{color:{UP}}} .dn{{color:{DN}}}
.kpis{{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}}
.kpis div{{border:1px solid {LINE};border-radius:8px;padding:8px 12px;min-width:120px;font-size:12px;color:{MUTED}}}
.kpis b{{display:block;font-size:18px;color:#2b2420;margin-top:2px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid {LINE};padding:5px 6px;text-align:left}}
th{{background:#f6efe3}} .warnbox{{border:1px solid #f0c27b;background:#fff6e6;border-radius:8px;padding:8px 12px;font-size:13px}}
@media print{{body{{margin:0;max-width:none}} a{{color:inherit;text-decoration:none}}}}
"""


def build(mode, period, key, today=None):
    """The report as HTML text, or None if the journal has no day in that week/month."""
    rows = [r for r in journal.days(mode, today) if journal.period_key(r["date"], period) == key]
    if not rows:
        return None
    s = journal.summarise(rows, period)[0]
    all_trades = journal.trade_rows(mode)
    dates = set(s["dates"])
    trades = [t for t in all_trades if (t.get("closed") or "")[:10] in dates]
    who = "Paper account (virtual money)" if mode == "paper" else "LIVE · real Zerodha account"
    what = f"Week {s['label']}" if period == "week" else s["label"]
    made = dt.datetime.now(IST).strftime("%d %b %Y %H:%M")

    odd = [r for r in rows if r.get("status") != "complete"]
    warn = ""
    if odd:
        items = "".join(f"<li>{html.escape(journal.period_label(r['date'], 'day'))}: {html.escape(r.get('status') or '')}"
                        f"{(' - ' + html.escape(r['note'])) if r.get('note') else ''}</li>" for r in odd)
        warn = (f"<div class='warnbox'><b>{len(odd)} of {len(rows)} day(s) not fully recorded</b> - the bot didn't run "
                f"to the close, or the day was rebuilt from the trade log:<ul>{items}</ul></div>")

    kpis = (f"<div class='kpis'><div>Net P&amp;L<b>{_inr(s['net'], True)}</b></div>"
            f"<div>Return<b class='{'up' if s['net'] > 0 else 'dn' if s['net'] < 0 else ''}'>{s['return_pct']:+.2f}%</b></div>"
            f"<div>Equity<b>{_inr(s['start_equity'])} → {_inr(s['end_equity'])}</b></div>"
            f"<div>Trading days<b>{s['days']} ({s['full_days']} full)</b></div>"
            f"<div>Trades<b>{s['trades']} ({s['wins']} won)</b></div>"
            f"<div>Win rate<b>{_opt(s['win_rate'], '{:.0f}%')}</b></div>"
            f"<div>Average R<b>{_opt(s['avg_r'], '{:+.2f}R')}</b></div>"
            f"<div>Charges<b>{_inr(s['charges'])}</b></div>"
            f"<div>Best / worst day<b>{_inr(s['best_day'], True)} / {_inr(s['worst_day'], True)}</b></div>"
            f"<div>Worst drawdown<b>{s['max_drawdown_pct']:.2f}%{'' if s['drawdown_known'] else '*'}</b></div>"
            f"<div>Skipped signals<b>{_opt(s['skipped'], '{:.0f}')}</b></div>"
            f"<div>Data outages<b>{_opt(s['outage_min'], '{:.0f} min')}</b></div></div>")

    points = [("start", s["start_equity"])] + [(r["date"][5:], journal._f(r.get("end_equity"))) for r in rows]
    chart = f"<h2>Equity at each day's close</h2>{equity_svg(points)}" if len(rows) > 1 else ""

    day_rows = "".join(
        f"<tr><td>{html.escape(journal.period_label(r['date'], 'day'))}</td><td>{html.escape(r.get('status') or '')}</td>"
        f"<td>{int(journal._f(r.get('trades')))}</td><td>{int(journal._f(r.get('wins')))}</td>"
        f"<td>{_inr(journal._opt(r.get('net_pnl')), True)}</td>"
        f"<td>{_opt(journal._opt(r.get('return_pct')), '{:+.2f}%')}</td>"
        f"<td>{_opt(journal._opt(r.get('max_drawdown_pct')), '{:.2f}%')}</td>"
        f"<td>{_opt(journal._opt(r.get('signals')), '{:.0f}')} / {_opt(journal._opt(r.get('skipped')), '{:.0f}')}</td>"
        f"<td>{_opt(journal._opt(r.get('outage_min')), '{:.0f} min')}</td>"
        f"<td>{html.escape(r.get('bot_start') or '?')}–{html.escape(r.get('bot_stop') or '?')}</td></tr>"
        for r in rows)
    days_tbl = ("<h2>Day by day</h2><table><tr><th>Day</th><th>Status</th><th>Trades</th><th>Won</th><th>Net</th>"
                "<th>Return</th><th>Worst drawdown</th><th>Signals / skipped</th><th>Data outages</th><th>Bot ran</th></tr>"
                f"{day_rows}</table>")

    if trades:
        trs = "".join(
            f"<tr><td>{html.escape((t.get('closed') or '')[:16])}</td><td>{html.escape(t.get('symbol') or '')}</td>"
            f"<td>{html.escape(t.get('direction') or '')}</td><td>{html.escape(t.get('qty') or '')}</td>"
            f"<td>{html.escape(t.get('entry') or '')}</td><td>{html.escape(t.get('exit') or '')}</td>"
            f"<td>{html.escape(t.get('reason') or '')}</td><td>{_inr(journal._f(t.get('charges')))}</td>"
            f"<td>{_inr(journal._f(t.get('net')), True)}</td><td>{journal._f(t.get('r')):+.2f}</td></tr>"
            for t in trades)
        trades_tbl = ("<h2>Trades</h2><table><tr><th>Closed</th><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th>"
                      f"<th>Exit</th><th>Exit reason</th><th>Charges</th><th>Net</th><th>R</th></tr>{trs}</table>")
    else:
        trades_tbl = "<h2>Trades</h2><div class='muted'>No trades in this period.</div>"

    skips = (f"<h2>Why signals were skipped</h2><div>{html.escape(s['skip_reasons'])}</div>"
             if s["skip_reasons"] else "")
    outages = []
    for d in s["dates"]:
        det = journal.load_detail(mode, d) or {}
        for o in det.get("outages", []):
            outages.append(f"<li>{html.escape(journal.period_label(d, 'day'))}: {html.escape(o.get('start') or '?')[:5]}"
                           f"–{html.escape(o.get('end') or '?')[:5]}</li>")
    out_html = f"<h2>Price-data outages</h2><ul>{''.join(outages)}</ul>" if outages else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ayodhya ORB · {html.escape(mode)} · {html.escape(what)}</title><style>{CSS}</style></head><body>
<h1>🏹 Ayodhya ORB · {html.escape(what)}</h1>
<div class="muted">{html.escape(who)} · {s['days']} trading day(s): {html.escape(s['dates'][0])} to {html.escape(s['dates'][-1])}
· made {made} IST from data\\journal\\journal_{html.escape(mode)}.csv and data\\trades_{html.escape(mode)}.csv</div>
{warn}{kpis}{chart}{days_tbl}{trades_tbl}{skips}{out_html}
<p class="muted">Net P&amp;L is after Zerodha charges and slippage. Worst drawdown = the biggest fall from a high, inside a
day (checked every round) or from one day's close to a later one; * = some days unknown (rebuilt days).
R = profit or loss as a multiple of the trade's planned risk. This report doesn't change any setting and can't trade.</p>
</body></html>"""


def write(mode, period, key, today=None):
    """Write the report; returns its path, or None if there is nothing to report."""
    text = build(mode, period, key, today)
    if text is None:
        return None
    path = os.path.join(reports_dir(), file_name(mode, period, key))
    journal._write_atomic(path, text)
    return path


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Weekly / monthly report from the daily journal (read-only for trading)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--week", help="ISO week, e.g. 2026-W47")
    g.add_argument("--month", help="month, e.g. 2026-11")
    g.add_argument("--list", action="store_true", help="list saved reports")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    a = ap.parse_args()
    if a.list:
        for n in saved():
            print(os.path.join(reports_dir(), n))
        return
    period, key = ("week", a.week) if a.week else ("month", a.month)
    ok = re.fullmatch(r"\d{4}-W\d{2}", key) if period == "week" else re.fullmatch(r"\d{4}-\d{2}", key)
    if not ok:
        raise SystemExit(f"--{period} should look like {'2026-W47' if period == 'week' else '2026-11'}")
    path = write(a.mode, period, key)
    print(path or f"No {a.mode} journal days in {key} - nothing written.")


if __name__ == "__main__":
    main()
