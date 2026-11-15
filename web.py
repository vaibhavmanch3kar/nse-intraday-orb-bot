#!/usr/bin/env python3
"""
Local dashboard + one-click Zerodha login on http://127.0.0.1:5000 (this PC only).

The Kite app's redirect URL must be  http://127.0.0.1:5000/kite/callback
After you log in, Zerodha redirects here, the request_token is exchanged for today's
access token through the static-IP proxy, and the token is saved to .kite_token.json.

Runs inside bot.py automatically. To log in without the bot:  .venv\\Scripts\\python.exe web.py
"""

import html
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config as C

STATUS_FILE = os.path.join(C.DATA_DIR, "status.json")


def _kite_proxy():
    import kite_proxy  # imported lazily: it needs the proxy/Kite env vars
    return kite_proxy


def login_status():
    try:
        kp = _kite_proxy()
        if kp.load_token():
            data = json.loads(kp.TOKEN_FILE.read_text())
            return True, f"Logged in as {data.get('user_id')} (token from {data.get('created', '')[:16]})"
    except SystemExit:
        return False, "Kite settings missing - run setup_kite_proxy.ps1"
    except Exception as e:
        return False, f"token check failed: {e}"
    return False, "Not logged in today"


def login_url():
    try:
        return _kite_proxy().make_kite().login_url()
    except SystemExit:
        return None


def exchange_token(request_token):
    kp = _kite_proxy()
    kite = kp.make_kite()
    data = kite.generate_session(request_token, api_secret=kp.env("KITE_API_SECRET"))
    kp.save_token(data["access_token"], data.get("user_id"))
    return data.get("user_id")


def _fmt(v, nd=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return html.escape(str(v))


CSS = """
:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--muted:#94a3b8;--up:#22c55e;--dn:#ef4444;--acc:#38bdf8;--warn:#f59e0b;--line:#334155}
body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border-radius:10px;padding:14px 16px;margin:12px 0;overflow-x:auto}
h1{font-size:20px;margin:4px 0} h2{font-size:15px;margin:0 0 8px;color:var(--muted)}
.kpis{display:flex;flex-wrap:wrap;gap:18px} .kpis div b{display:block;font-size:18px}
table{border-collapse:collapse;width:100%} td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.up{color:var(--up)} .dn{color:var(--dn)} .muted{color:var(--muted)} .warn{color:var(--warn)} .sig{color:var(--acc);font-weight:600}
.btn,button{background:var(--acc);color:#0f172a;border:0;border-radius:6px;padding:8px 14px;font-weight:600;text-decoration:none;cursor:pointer;display:inline-block;margin:4px 6px 4px 0}
button.red{background:var(--dn);color:#fff} .tag{padding:2px 8px;border-radius:999px;background:var(--line);font-size:12px}
ul{margin:0;padding-left:18px} .msg{color:var(--up)}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 2px} nav a{color:var(--fg);text-decoration:none;padding:6px 12px;border-radius:6px;background:var(--card)}
nav a.on{background:var(--acc);color:#0f172a;font-weight:600}
.banner{background:#422006;color:#fde68a;border-radius:8px;padding:10px 14px;margin:10px 0}
.tm{color:#c4b5fd;font-size:13px}
.days a{color:var(--acc);margin-right:10px;text-decoration:none} .days a.on{color:var(--fg);font-weight:700;text-decoration:underline}
"""


def page(title, active, body, refresh=None):
    tabs = [("/", "Live"), ("/review", "Session review"), ("/scanner", "Swing scanner"), ("/trades", "Trades")]
    nav = "".join(f"<a href='{u}' class='{'on' if u == active else ''}'>{t}</a>" for u, t in tabs)
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1">{meta}<title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body><main><nav>{nav}</nav>{body}</main></body></html>')


def read_status():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def bot_running(s):
    """The bot rewrites status.json at least every POLL_SEC while it runs."""
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(s["time"])
    except (KeyError, ValueError, TypeError):
        return False
    return (dt.datetime.now(t.tzinfo) - t).total_seconds() < max(180, 3 * C.POLL_SEC)


def market_calendar():
    """One line: is NSE open today, next trading day, upcoming holidays."""
    import datetime as dt
    import holidays
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
    name = holidays.holiday_name(today)
    if today.weekday() >= 5:
        state = "Weekend"
    elif name:
        state = f"NSE holiday: {html.escape(name)}"
    else:
        state = "Trading day"
    nxt = holidays.next_trading_day(today)
    up = " · ".join(f"{d:%a %d %b} {html.escape(n)}" for d, n in holidays.upcoming(today, 3)) or "none listed"
    warn = ("" if holidays.year_listed(today.year) else
            f" <span class='warn'>nse_holidays.json has no {today.year} list - add it.</span>")
    return (f"<div class='card'><h2>NSE calendar</h2>Today: <b>{state}</b> · next trading day "
            f"<b>{nxt:%a %d %b}</b><br><span class='muted'>Upcoming holidays: {up}</span>{warn}</div>")


def render(msg=""):
    s = read_status()
    running = bot_running(s)
    ok, login_msg = login_status()
    url = login_url()
    mode = s.get("mode", "paper" if not C.is_live() else "live").upper()
    lag = s.get("data_lag_min")
    lag_txt = "-" if lag is None else f"{lag:.0f} min"
    lag_cls = "warn" if (lag or 0) > 20 else ""
    paused = s.get("paused")

    pos_rows = "".join(
        f"<tr><td>{_fmt(p['symbol'])}</td><td class='{'up' if p['direction']=='LONG' else 'dn'}'>{p['direction']}</td>"
        f"<td>{p['qty']}</td><td>{_fmt(p['entry'])}</td><td>{_fmt(p['price'])}</td><td>{_fmt(p['stop'])}</td>"
        f"<td>{_fmt(p['target'])}</td><td class='{'up' if p['unrealised']>=0 else 'dn'}'>{_fmt(p['unrealised'])}</td></tr>"
        for p in s.get("positions", [])) or "<tr><td colspan=8 class='muted'>No open positions</td></tr>"

    sig_rows = "".join(
        f"<tr><td>{_fmt(sym)}</td><td>{_fmt(r.get('bar'))}</td><td>{_fmt(r.get('or_low'))} – {_fmt(r.get('or_high'))}</td>"
        f"<td>{_fmt(r.get('close'))}</td><td>{_fmt(r.get('vwap'))}</td><td>{_fmt(r.get('vol_ratio'), 1)}</td>"
        f"<td class='{'sig' if 'breakout' in (r.get('reason') or '') else ''}'>{_fmt(r.get('reason'))}</td></tr>"
        for sym, r in sorted(s.get("rows", {}).items())) or "<tr><td colspan=7 class='muted'>Waiting for market data</td></tr>"

    events = "".join(f"<li>{html.escape(e)}</li>" for e in s.get("events", [])) or "<li class='muted'>-</li>"
    login_btn = (f"<a class='btn' href='{html.escape(url)}'>Log in to Zerodha</a>" if url and not ok else "")
    when = (s.get("time") or "")
    banner = "" if running else (
        f"<div class='banner'><b>Bot is not running</b> (markets closed, weekend, or stopped). "
        f"Showing the last saved state{' from ' + html.escape(when[:16].replace('T', ' ')) if when else ''}. "
        f"See <a href='/review' style='color:#fde68a'>Session review</a> for how the last trading day played out, "
        f"or <a href='/trades' style='color:#fde68a'>Trades</a> for history.</div>")
    calendar = market_calendar()
    controls = (f"""<div class="card"><h2>Controls</h2>
<form method="post" action="/{'resume' if paused else 'pause'}" style="display:inline"><button>{'Resume new entries' if paused else 'Pause new entries'}</button></form>
<form method="post" action="/stop" style="display:inline" onsubmit="return confirm('Stop the bot? In live mode open positions keep their exchange stop-loss.')"><button class="red">Stop bot</button></form></div>"""
                if running else "")
    return page("ORB Bot", "/", f"""
<h1>ORB Bot <span class="tag">{mode}</span> <span class="tag {'up' if running else 'warn'}">{'RUNNING' if running else 'NOT RUNNING'}</span> {'<span class="tag warn">PAUSED</span>' if paused else ''}</h1>
<div class="muted">Updated {_fmt(when[11:19])} IST · auto-refreshes every 20 s</div>
{banner}
{calendar}
{f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
<div class="card"><h2>Zerodha login</h2>
<div class="{'up' if ok else 'warn'}">{html.escape(login_msg)}</div>{login_btn}
<div class="muted">Paper mode doesn't need a login. Live mode needs one every day (tokens expire ~06:00 IST).</div></div>
<div class="card kpis">
<div>Equity<b>₹{_fmt(s.get('equity'))}</b></div><div>Today P&amp;L<b class="{'up' if (s.get('day_pnl') or 0)>=0 else 'dn'}">₹{_fmt(s.get('day_pnl'))}</b></div>
<div>Total P&amp;L<b>₹{_fmt(s.get('realized_pnl'))}</b></div><div>Charges paid<b>₹{_fmt(s.get('charges_paid'))}</b></div>
<div>Trades today<b>{_fmt(s.get('trades_today'))}</b></div><div>Loss streak<b>{_fmt(s.get('loss_streak'))}</b></div>
<div>Data delay<b class="{lag_cls}">{lag_txt}</b></div></div>
<div class="card"><h2>Open positions</h2><table><tr><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Price</th><th>Stop</th><th>Target</th><th>Unrealised ₹</th></tr>{pos_rows}</table></div>
<div class="card"><h2>Watchlist (last closed 15-min bar)</h2><table><tr><th>Stock</th><th>Bar</th><th>Opening range</th><th>Close</th><th>VWAP</th><th>Vol ×</th><th>Status</th></tr>{sig_rows}</table></div>
{controls}
<div class="card"><h2>Recent events</h2><ul>{events}</ul></div>""", refresh=20)


def sparkline(closes, or_hi, or_lo, w=160, h=36):
    """Tiny inline chart of the day's 15-min closes with the opening range band."""
    if not closes:
        return ""
    vals = closes + [v for v in (or_hi, or_lo) if v]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1

    def y(v):
        return h - 3 - (v - lo) / span * (h - 6)
    step = w / max(1, len(closes) - 1)
    pts = " ".join(f"{i * step:.1f},{y(c):.1f}" for i, c in enumerate(closes))
    band = ""
    if or_hi and or_lo:
        band = (f"<rect x='0' y='{y(or_hi):.1f}' width='{w}' height='{max(1, y(or_lo) - y(or_hi)):.1f}' "
                f"fill='#38bdf8' opacity='0.15'/>")
    col = "#22c55e" if closes[-1] >= closes[0] else "#ef4444"
    return (f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' role='img' aria-label='intraday closes'>"
            f"{band}<polyline points='{pts}' fill='none' stroke='{col}' stroke-width='1.5'/></svg>")


def render_review(day=None, refresh=False):
    import review
    try:
        r = review.build(day, refresh=refresh) if (day or refresh) else review.latest()
    except Exception as e:
        return page("Session review", "/review", f"<h1>Session review</h1><div class='banner'>Could not build the review: {html.escape(str(e))}</div>")
    if not r.get("day"):
        return page("Session review", "/review", "<h1>Session review</h1><div class='banner'>No data from Yahoo right now - try Refresh later.</div>")
    days = "".join(f"<a class='{'on' if d == r['day'] else ''}' href='/review?day={d}'>{d[5:]}</a>"
                   for d in r.get("days", [])[-10:])
    rows = []
    for sym, x in sorted(r["rows"].items()):
        chg = x.get("day_change_pct")
        if x.get("direction"):
            res = x["r"]
            outcome = (f"<span class='{'up' if x['direction'] == 'LONG' else 'dn'}'>{x['direction']}</span> "
                       f"{x['signal_time']} → entry {_fmt(x['entry'])}, stop {_fmt(x['stop'])}, target {_fmt(x['target'])}"
                       f"<br><span class='muted'>exit {html.escape(str(x['exit_reason']))} {x['exit_time']} @ {_fmt(x['exit'])}</span>")
            tm = x.get("time_machine") or {}
            if tm.get("text"):
                outcome += (f"<br><span class='tm'>⏱ Time Machine ({tm.get('n', 0):,} replays, seed {tm.get('seed', '-')}): "
                            f"{html.escape(tm['text'])}</span>")
            if tm.get("verdict"):
                v = tm["verdict"]
                cls = "dn" if "stop" in v.split(":")[0] else "up" if "target" in v.split(":")[0] else "warn"
                outcome += f"<br><b class='{cls}'>{html.escape(v)}</b>"
            if tm.get("history_text"):
                outcome += f"<br><span class='muted'>📜 {html.escape(tm['history_text'])}</span>"
            r_cell = f"<b class='{'up' if res > 0 else 'dn'}'>{res:+.2f}R</b>"
        else:
            outcome = f"<span class='muted'>{html.escape(x.get('note') or '-')}</span>"
            r_cell = "-"
        rows.append(f"<tr><td><b>{html.escape(sym)}</b></td><td>{sparkline(x.get('closes', []), x.get('or_high'), x.get('or_low'))}</td>"
                    f"<td>{_fmt(x.get('or_low'))} – {_fmt(x.get('or_high'))}</td>"
                    f"<td class='{'up' if (chg or 0) >= 0 else 'dn'}'>{'-' if chg is None else f'{chg:+.2f}%'}</td>"
                    f"<td style='white-space:normal'>{outcome}</td><td>{r_cell}</td></tr>")
    st = r.get("settings", {})
    settings = " · ".join(f"{k}={v}" for k, v in st.items())
    total = r.get("total_r", 0.0)
    body = f"""
<h1>Session review <span class="tag">{r['day']}</span></h1>
<div class="muted">Replays the day through the strategy rules, one stock at a time (portfolio limits not applied).
Built {html.escape(str(r.get('built', ''))[:16].replace('T', ' '))} from Yahoo 15-min data.</div>
<div class="card days">Day: {days} <form method="get" action="/review" style="display:inline">
<input type="hidden" name="day" value="{r['day']}"><input type="hidden" name="refresh" value="1"><button>Refresh</button></form></div>
<div class="card kpis"><div>Signals<b>{r.get('signals', 0)}</b></div><div>Winners<b>{r.get('wins', 0)}</b></div>
<div>Total<b class="{'up' if total > 0 else 'dn'}">{total:+.2f}R</b></div>
<div>At 0.5% risk on ₹1L<b class="{'up' if total > 0 else 'dn'}">≈ ₹{total * C.PAPER_START_INR * C.RISK_PER_TRADE_PCT / 100:+,.0f}</b></div></div>
<div class="card"><h2>Stocks — chart shows 15-min closes, blue band = opening range</h2><table>
<tr><th>Stock</th><th>Day</th><th>Opening range</th><th>Day %</th><th>Signal → outcome</th><th>Result</th></tr>{''.join(rows)}</table></div>
<div class="card muted">Rules: {html.escape(settings)}. R = profit or loss as a multiple of the risk
(entry to stop), after 0.05% slippage and Zerodha charges.<br>
⏱ Time Machine: the trade replayed 10,000 times using this stock's own 5-minute moves from the same
time of day (±10 min) over the last 60 days, shuffled. It assumes no edge, so it's a baseline: a good setup
should beat these odds. The replays also inherit the stock's recent intraday drift.
The verdict compares target-first vs stop-first at 1R with break-even (50%): within 5 points is a coin flip.
📜 History is the real record: the bot's same rules (same direction) run on each earlier session.</div>"""
    return page("Session review", "/review", body)


def render_scanner(refresh=False):
    import scanner
    try:
        r = scanner.scan() if refresh else scanner.latest()
    except Exception as e:
        return page("Swing scanner", "/scanner", f"<h1>Swing scanner</h1><div class='banner'>Scan failed: {html.escape(str(e))}</div>")
    counts = "".join(f"<div>{html.escape(k)}<b>{v}</b></div>" for k, v in r.get("counts", {}).items())
    rows = []
    hits = [(s, x) for s, x in r.get("rows", {}).items() if x["setups"]]
    for s, x in sorted(hits, key=lambda kv: (-len(kv[1]["setups"]), kv[0])):
        trend = ("<span class='up'>above 200 DMA</span>" if x.get("above_200dma") else
                 "<span class='dn'>below 200 DMA</span>" if x.get("above_200dma") is False else "-")
        plan = (f"stop {_fmt(x['stop'])} · target {_fmt(x['target'])} · qty {x['qty']}"
                if x.get("stop") else "-")
        rows.append(
            f"<tr><td><b>{html.escape(s)}</b></td><td>{sparkline(x.get('closes', []), None, None)}</td>"
            f"<td class='sig' style='white-space:normal'>{html.escape(', '.join(x['setups']))}</td>"
            f"<td>{_fmt(x['close'])}<br><span class='{'up' if x['change_pct'] >= 0 else 'dn'}'>{x['change_pct']:+.2f}%</span></td>"
            f"<td>{_fmt(x.get('rsi'), 0)}</td><td>{trend}<br><span class='muted'>{x['from_52w_high_pct']:+.1f}% vs 52w high</span></td>"
            f"<td style='white-space:normal'>{plan}</td></tr>")
    table = "".join(rows) or "<tr><td colspan=7 class='muted'>No setups on the last session.</td></tr>"
    risk_rs = C.PAPER_START_INR * C.RISK_PER_TRADE_PCT / 100
    body = f"""
<h1>Swing scanner <span class="tag">{html.escape(str(r.get('day')))}</span></h1>
<div class="banner">Research only - the bot does not trade these. They are multi-day (CNC) setups on daily candles;
check the chart yourself before acting. Built {html.escape(str(r.get('built', ''))[:16].replace('T', ' '))}.</div>
<div class="card kpis">{counts}</div>
<form method="get" action="/scanner"><input type="hidden" name="refresh" value="1"><button>Re-scan</button></form>
<div class="card"><h2>Stocks with a setup on the last closed daily candle (60-day chart)</h2><table>
<tr><th>Stock</th><th>60 days</th><th>Setup</th><th>Close</th><th>RSI</th><th>Trend</th><th>Plan (₹{risk_rs:,.0f} risk)</th></tr>{table}</table></div>
<div class="card muted">Setups: 20-day high breakout · bull flag (≥8% pole, tight shallow flag) · 20-day MA reclaim ·
oversold (RSI&lt;30) · oversold bounce (RSI&lt;30 in last 3 days, close above yesterday's high).
Plan: stop = close − 1.5 × ATR(14), target = 2R, qty risks {C.RISK_PER_TRADE_PCT}% of ₹{C.PAPER_START_INR:,.0f}
(capped at {C.MAX_POSITION_PCT:.0f}% of capital). The TWS interview's rule: prefer stocks in an uptrend (above the 200 DMA,
near highs).{(' Data errors: ' + html.escape(', '.join(r['errors']))) if r.get('errors') else ''}</div>"""
    return page("Swing scanner", "/scanner", body)


def _read_csv(path):
    import csv
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def render_trades():
    parts = ["<h1>Trades</h1>"]
    for mode in ("paper", "live"):
        rows = _read_csv(os.path.join(C.DATA_DIR, f"trades_{mode}.csv"))
        if not rows and mode == "live":
            continue
        net = sum(_num(r.get("net")) for r in rows)
        wins = sum(1 for r in rows if _num(r.get("net")) > 0)
        body = "".join(
            f"<tr><td>{html.escape(r.get('closed', '')[:16])}</td><td>{html.escape(r.get('symbol', ''))}</td>"
            f"<td class='{'up' if r.get('direction') == 'LONG' else 'dn'}'>{html.escape(r.get('direction', ''))}</td>"
            f"<td>{html.escape(r.get('qty', ''))}</td><td>{html.escape(r.get('entry', ''))}</td><td>{html.escape(r.get('exit', ''))}</td>"
            f"<td>{html.escape(r.get('reason', ''))}</td><td>{html.escape(r.get('charges', ''))}</td>"
            f"<td class='{'up' if _num(r.get('net')) > 0 else 'dn'}'>{_num(r.get('net')):,.2f}</td><td>{_num(r.get('r')):+.2f}</td></tr>"
            for r in reversed(rows[-200:])) or "<tr><td colspan=10 class='muted'>No trades yet - the bot records them here once it trades on a market day.</td></tr>"
        parts.append(f"""<div class="card kpis"><div>{mode.title()} trades<b>{len(rows)}</b></div>
<div>Win rate<b>{(wins / len(rows) * 100 if rows else 0):.0f}%</b></div><div>Net P&amp;L<b class="{'up' if net >= 0 else 'dn'}">₹{net:,.2f}</b></div></div>
<div class="card"><h2>{mode.title()} trade log (newest first)</h2><table><tr><th>Closed</th><th>Stock</th><th>Side</th><th>Qty</th>
<th>Entry</th><th>Exit</th><th>Reason</th><th>Charges ₹</th><th>Net ₹</th><th>R</th></tr>{body}</table></div>""")
    bt = _read_csv(os.path.join(C.DATA_DIR, "backtest_trades.csv"))
    if bt:
        net = sum(_num(r.get("net")) for r in bt)
        wins = sum(1 for r in bt if _num(r.get("net")) > 0)
        first, last = bt[0].get("opened", "")[:10], bt[-1].get("closed", "")[:10]
        parts.append(f"""<div class="card"><h2>Last backtest ({html.escape(first)} → {html.escape(last)})</h2>
<div class="kpis"><div>Trades<b>{len(bt)}</b></div><div>Win rate<b>{wins / len(bt) * 100:.0f}%</b></div>
<div>Net P&amp;L<b class="{'up' if net >= 0 else 'dn'}">₹{net:,.0f}</b></div></div>
<div class="muted">Run <code>.venv\\Scripts\\python.exe backtest.py</code> to update. Full list: data\\backtest_trades.csv</div></div>""")
    return page("Trades", "/trades", "".join(parts))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console clean
        pass

    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to="/"):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(render(q.get("msg", [""])[0]))
        elif u.path == "/review":
            day = q.get("day", [None])[0]
            if day:
                try:
                    import datetime as dt
                    dt.date.fromisoformat(day)
                except ValueError:
                    day = None
            self._send(render_review(day, refresh=q.get("refresh", ["0"])[0] == "1"))
        elif u.path == "/trades":
            self._send(render_trades())
        elif u.path == "/scanner":
            self._send(render_scanner(refresh=q.get("refresh", ["0"])[0] == "1"))
        elif u.path == "/api/status":
            try:
                with open(STATUS_FILE, encoding="utf-8") as f:
                    self._send(f.read(), ctype="application/json")
            except OSError:
                self._send("{}", ctype="application/json")
        elif u.path == "/kite/callback":
            if q.get("status", [""])[0] != "success" or not q.get("request_token"):
                self._send(render("Login was not successful - try again."), 400)
                return
            try:
                user = exchange_token(q["request_token"][0])
                self._redirect(f"/?msg=Logged+in+as+{user}.+Token+saved+for+today.")
            except Exception as e:
                self._send(render(f"Token exchange failed: {e}"), 500)
        else:
            self._send("not found", 404, "text/plain")

    def do_POST(self):
        # Browsers on other sites can't read the response, but they can POST: only accept same-origin.
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if origin and not origin.startswith(f"http://{C.WEB_HOST}:{C.WEB_PORT}"):
            self._send("forbidden", 403, "text/plain")
            return
        os.makedirs(C.DATA_DIR, exist_ok=True)
        if self.path == "/pause":
            open(C.PAUSE_FILE, "w").close()
        elif self.path == "/resume":
            if os.path.exists(C.PAUSE_FILE):
                os.remove(C.PAUSE_FILE)
        elif self.path == "/stop":
            open(C.STOP_FILE, "w").close()
        self._redirect("/")


def serve():
    srv = ThreadingHTTPServer((C.WEB_HOST, C.WEB_PORT), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    print(f"Dashboard: http://{C.WEB_HOST}:{C.WEB_PORT}   (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://{C.WEB_HOST}:{C.WEB_PORT}")
    try:
        serve()
    except KeyboardInterrupt:
        pass
