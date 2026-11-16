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
import theme as T
import webui

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


VIEW_PAGES = webui.VIEW_PAGES  # pages with the Paper Account | Zerodha Account switch


def page(title, active, body, refresh=None, view=None):
    """Wrap a page body in the dashboard frame (header, page tabs, account switch, theme switch)."""
    switch = view_switch(active, view, read_status()) if view and active in VIEW_PAGES else ""
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return webui.shell(title, active, body, view=view, switch=switch, extra_head=meta)


def mode_chips(status, extra=""):
    """The mode the bot trades in (set only in its own settings), next to the account switch."""
    mode = T.mode_label((status or {}).get("mode") or ("live" if C.is_live() else "paper"))
    live = " live" if "LIVE" in mode else ""
    return (f"<div class='chips'><span class='chip mode{live}' title='The mode the bot trades in (set in its own settings)'>"
            f"Bot trading: {html.escape(mode)}</span>{extra}"
            f"<span class='muted small'>The Paper | Zerodha switch only changes what you see.</span></div>")


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


def data_source_kpi(s):
    """Which price source the bot is really using (and why not Kite, if it fell back)."""
    ds = s.get("data_source") or {}
    using = ds.get("using")
    if not using:
        return ""
    label = f"{T.name('kite_data')} · Kite (real-time)" if using == "kite" else "Yahoo (free, may lag)"
    cls = "up" if using == "kite" else ("warn" if ds.get("wanted") == "kite" else "")
    tip = f" title='{html.escape(ds.get('reason') or '')}'" if ds.get("reason") else ""
    note = "<br><span class='muted' style='font-size:11px'>Kite unavailable, see tooltip</span>" if ds.get("reason") else ""
    return f"<div{tip}>Price data<b class='{cls}'>{label}</b>{note}</div>"


def market_calendar():
    """One line: is NSE open today, next trading day, upcoming holidays."""
    import datetime as dt
    import holidays
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
    name = holidays.holiday_name(today)
    if today.weekday() >= 5:
        state = "Weekend"
    elif name:
        state = f"{T.term('holiday')}: {html.escape(name)}"
    else:
        state = "Trading day"
    nxt = holidays.next_trading_day(today)
    up = " · ".join(f"{d:%a %d %b} {html.escape(n)}" for d, n in holidays.upcoming(today, 3)) or "none listed"
    warn = ("" if holidays.year_listed(today.year) else
            f" <span class='warn'>nse_holidays.json has no {today.year} list - add it.</span>")
    return (f"<div class='card'><h2>{T.term('calendar')}</h2>Today: <b>{state}</b> · next trading day "
            f"<b>{nxt:%a %d %b}</b><br><span class='muted'>Upcoming holidays: {up}</span>{warn}</div>")


def _inr(v, signed=False):
    """₹1,23,456.78 style (Western grouping, as elsewhere on the dashboard); '-' if missing."""
    if v is None:
        return "-"
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    return f"{sign}₹{abs(v):,.2f}"


def _pl(v):
    """Signed ₹ amount coloured green/red."""
    cls = "" if not v else ("up" if v > 0 else "dn")
    return f"<b class='{cls}'>{_inr(v, signed=True)}</b>"


def _clock(iso):
    return html.escape(iso[11:19]) if iso else "-"


ACCOUNT_REFRESH_SEC = 10


def paper_card(acct, status=None):
    """The Paper Account card (virtual money). Data comes from accounts.paper_account() only."""
    title = f"{T.name('paper')} · Paper Account <span class='tag'>virtual money</span>"
    if acct.get("error"):
        return f"<h2>{title}</h2><div class='warn'>⚠ {html.escape(acct['error'])}</div>"
    warnings = list(acct.get("warnings", []))
    s = status or {}
    lag = s.get("data_lag_min")
    if s.get("mode") == "paper" and lag is not None and lag > 20:
        warnings.append(f"Prices are about {lag:.0f} min behind the market "
                        f"({'Kite' if (s.get('data_source') or {}).get('using') == 'kite' else 'Yahoo'} data).")
    k = [("Starting balance", f"<b>{_inr(acct['start'])}</b>"),
         ("Total equity", f"<b>{_inr(acct['equity'])}</b>"),
         ("Available funds", f"<b>{_inr(acct['available'])}</b>"),
         ("Invested (open positions)", f"<b>{_inr(acct['invested'])}</b>"),
         ("Realised P&amp;L (since start)", _pl(acct["realised"])),
         ("Realised today", _pl(acct["realised_today"])),
         ("Unrealised P&amp;L", _pl(acct["unrealised"])),
         ("Total P&amp;L", _pl(acct["total_pnl"])),
         ("Charges paid", f"<b>{_inr(acct['charges'])}</b>")]
    kpis = "".join(f"<div>{label}{val}</div>" for label, val in k)
    warn = "".join(f"<div class='warn'>⚠ {html.escape(w)}</div>" for w in warnings)
    return (f"<h2>{title}</h2><div class='kpis'>{kpis}</div>{warn}{capital_lines()}"
            f"<div class='muted' style='margin-top:6px'>Updated {_clock(acct.get('as_of'))} IST (the bot's last round) · "
            f"checked {_clock(acct.get('now'))} · refreshes every {ACCOUNT_REFRESH_SEC} s · "
            f"unrealised P&amp;L is before exit charges · available = equity − invested (no leverage)</div>")


VIEWS = {"paper": "Paper Account", "zerodha": "Zerodha Account"}


def current_view(q, status):
    """Which account the page shows: ?view=paper|zerodha, else the account the bot trades with.
    Only changes what the page displays: the bot's mode is set in its own settings, never here."""
    v = (q.get("view") or [""])[0]
    if v in VIEWS:
        return v
    return "zerodha" if (status or {}).get("mode") == "live" else "paper"


def view_switch(path, view, status):
    bot_mode = T.mode_label((status or {}).get("mode") or ("live" if C.is_live() else "paper"))
    confirm = ("return confirm('This shows your REAL Zerodha account (read-only).\\n\\n"
               f"The bot keeps trading exactly as before ({bot_mode}). Switching views never changes "
               "the trading mode or places an order.\\n\\nShow the Zerodha Account?')")
    links = []
    for v, label in VIEWS.items():
        on = " on" if v == view else ""
        click = f' onclick="{confirm}"' if v == "zerodha" and view != "zerodha" else ""
        cur = " aria-current='true'" if v == view else ""
        links.append(f"<a class='{v}{on}' href='{path}?view={v}'{click}{cur}>{label}</a>")
    return (f"<div class='seg' role='group' aria-label='Account view (only changes what you see; "
            f"the bot is trading {html.escape(bot_mode)})'>{''.join(links)}</div>")


def view_banner(view):
    if view == "zerodha":
        return ("<div class='viewbar zerodha'>LIVE · REAL ZERODHA ACCOUNT · real money · read-only view</div>")
    return "<div class='viewbar paper'>PAPER ACCOUNT · virtual money</div>"


def zerodha_card(acct):
    """The real Zerodha account (read-only), from accounts.zerodha_account()."""
    import accounts
    title = f"{T.name('live')} · Zerodha Account <span class='tag dn'>real money · read-only</span>"
    if acct.get("error"):
        btn = ""
        if acct.get("login_needed"):
            url = login_url()
            btn = (f"<div><a class='btn' href='{html.escape(url)}'>Log in to Zerodha</a></div>" if url else
                   "<div class='muted'>Kite settings are missing, so the login button isn't available.</div>")
        return (f"<h2>{title}</h2><div class='warn'>⚠ {html.escape(acct['error'])}</div>{btn}"
                f"<div class='muted' style='margin-top:6px'>Checked {_clock(acct.get('now'))} IST · "
                f"tries again every {ACCOUNT_REFRESH_SEC} s</div>")
    k = [("Opening balance (start of day)", f"<b>{_inr(acct['opening'])}</b>"),
         ("Account value", f"<b>{_inr(acct['equity'])}</b>"),
         ("Available funds", f"<b>{_inr(acct['available'])}</b>"),
         ("Margin used", f"<b>{_inr(acct['margin_used'])}</b>"),
         ("Invested (open positions)", f"<b>{_inr(acct['invested'])}</b>"),
         ("Realised P&amp;L today", _pl(acct["realised"])),
         ("Unrealised P&amp;L", _pl(acct["unrealised"])),
         ("Total P&amp;L today", _pl(acct["pnl"]))]
    kpis = "".join(f"<div>{label}{val}</div>" for label, val in k)
    warn = "".join(f"<div class='warn'>⚠ {html.escape(w)}</div>" for w in acct.get("warnings", []))
    rows = "".join(
        f"<tr><td><b>{_fmt(r['symbol'])}</b> <span class='muted'>{_fmt(r['exchange'])}</span></td><td>{_fmt(r['product'])}</td>"
        f"<td class='{'up' if r['qty'] > 0 else 'dn' if r['qty'] < 0 else 'muted'}'>{r['qty'] if r['qty'] else 'closed'}</td>"
        f"<td>{_fmt(r['avg'])}</td><td>{_fmt(r['price'])}</td><td>{_pl(r['realised'])}</td><td>{_pl(r['unrealised'])}</td></tr>"
        for r in acct.get("positions", [])) or "<tr><td colspan=7 class='muted'>No positions today</td></tr>"
    return (f"<h2>{title}</h2><div class='kpis'>{kpis}</div>{warn}"
            f"<table style='margin-top:10px'><tr><th>Position</th><th>Product</th><th>Qty</th><th>Avg price</th>"
            f"<th>Last price</th><th>Realised</th><th>Unrealised</th></tr>{rows}</table>"
            f"<div class='muted' style='margin-top:6px'>From Zerodha at {_clock(acct.get('fetched_at'))} IST · "
            f"checked {_clock(acct.get('now'))} · refreshes every {ACCOUNT_REFRESH_SEC} s "
            f"(Zerodha is asked at most every {accounts.ZERODHA_CACHE_SEC} s) · account value = opening balance + "
            f"today's P&amp;L · Zerodha's P&amp;L is before charges · delivery holdings are not included</div>")


def capital_lines():
    """Pending opening-balance change and the last few applied changes (paper account only)."""
    import capital
    s = capital.load()
    out = ""
    p = s.get("pending")
    if p:
        out += (f"<div class='warn'>⏳ Opening balance changes to <b>{_inr(float(p['amount']))}</b> at the bot's next start "
                f"(requested {html.escape(str(p.get('requested_at', ''))[:16].replace('T', ' '))}).</div>")
    hist = s.get("history") or []
    if hist:
        items = " · ".join(f"{html.escape(str(h.get('applied_at', ''))[:10])}: {_inr(float(h['from']))} → {_inr(float(h['to']))}"
                           for h in hist[-3:])
        out += f"<div class='muted'>Opening balance changes: {items}</div>"
    return out


def capital_card():
    """Form to choose the PAPER opening balance. Kept outside the auto-refreshing account card, so a
    refresh never wipes what you're typing. The change applies at the bot's next start."""
    import capital
    current = capital.current_opening_balance()
    pending = capital.load().get("pending")
    chosen = float(pending["amount"]) if pending else current
    radios = "".join(
        f"<label class='opt'><input type='radio' name='amount' value='{v}'{' checked' if v == chosen else ''}> {_inr(float(v))[:-3]}</label>"
        for v in capital.PRESETS)
    custom_on = chosen not in capital.PRESETS
    radios += (f"<label class='opt'><input type='radio' name='amount' value='custom'{' checked' if custom_on else ''}> Custom: "
               f"₹<input type='number' name='custom_amount' min='{capital.MIN_INR}' max='{capital.MAX_INR}' step='1' "
               f"value='{int(chosen) if custom_on else ''}' placeholder='e.g. 75000' style='width:110px' "
               f"onfocus=\"this.form.amount.value='custom'\"></label>")
    cancel = ("<form method='post' action='/paper-capital/cancel' style='display:inline' "
              "onsubmit=\"if(!confirm('Cancel the pending opening balance change?'))return false;this.confirm.value='yes'\">"
              "<input type='hidden' name='confirm' value=''><button class='red'>Cancel pending change</button></form>"
              if pending else "")
    return f"""<div class="card" id="capital"><h2>Opening Balance · Paper Account <span class='tag'>virtual money</span></h2>
<form method="post" action="/paper-capital" onsubmit="return confirmCapital(this)" style="display:inline">
<div class="opts">{radios}</div>
<input type="hidden" name="confirm" value="">
<button>Save opening balance</button></form> {cancel}
<div class="muted" style="margin-top:6px">Now <b>{_inr(current)}</b>. A new amount takes effect when the bot next starts
(normally 9:00), before any trade; if paper positions are open it waits. Past trades and P&amp;L are kept, and
position sizes follow the new equity (0.5% risk per trade). Paper only: it never touches your Zerodha account or
live trading. From {_inr(float(capital.MIN_INR))[:-3]} to {_inr(float(capital.MAX_INR))[:-3]}, whole rupees.</div></div>
<script>function confirmCapital(f){{
 var v=f.querySelector('input[name=amount]:checked'); if(!v){{alert('Choose an amount.');return false}}
 var a=v.value==='custom'?Number(f.custom_amount.value):Number(v.value);
 if(!a||a<{capital.MIN_INR}||a>{capital.MAX_INR}||a!==Math.floor(a)){{alert('Enter whole rupees from {capital.MIN_INR:,} to {capital.MAX_INR:,}.');return false}}
 var m='Change the PAPER opening balance from \\u20b9{int(current):,} to \\u20b9'+a.toLocaleString('en-US')+'?\\n\\n'+
  'It takes effect at the bot\\'s next start (normally 9:00). Past trades and P&L are kept.\\n'+
  'Your real Zerodha account and live trading are not affected.';
 if(a<{capital.SMALL_INR})m+='\\n\\nBelow \\u20b9{capital.SMALL_INR:,} trades are small, and stocks priced above 25% of the balance are skipped.';
 if(!confirm(m))return false; f.confirm.value='yes'; return true}}</script>"""


def account_card_html(view, status=None):
    import accounts
    if view == "zerodha":
        return zerodha_card(accounts.zerodha_account())
    return paper_card(accounts.paper_account(), status)


def account_card(view, status=None):
    """The card plus a small script that refreshes it in place (no page reload)."""
    return (f"<div class='card acct {view}' id='acct'>{account_card_html(view, status)}</div>"
            f"<script>setInterval(function(){{fetch('/api/account?view={view}',{{cache:'no-store'}})"
            f".then(function(r){{if(!r.ok)throw 0;return r.text()}})"
            f".then(function(h){{document.getElementById('acct').innerHTML=h}})"
            f".catch(function(){{var e=document.getElementById('acct');if(e&&!e.querySelector('.offline'))"
            f"e.insertAdjacentHTML('afterbegin',\"<div class='warn offline'>⚠ Can't reach the dashboard server: "
            f"values below may be old.</div>\")}})}},{ACCOUNT_REFRESH_SEC * 1000});</script>")


def _badge(text, cls, tip=""):
    t = f" title='{html.escape(tip)}'" if tip else ""
    return f"<span class='tag {cls}' style='font-size:11px;margin-left:4px'{t}>{html.escape(text)}</span>"


def position_row(p):
    """One open position: P&L in ₹ and R, stop distance, EMA trail, time-stop check, time in trade."""
    long_ = p.get("direction") == "LONG"
    r = p.get("r_unreal")
    pnl_cls = "up" if (p.get("unrealised") or 0) >= 0 else "dn"
    stop_d = p.get("stop_dist_pct")
    stop_cell = f"{_fmt(p.get('stop'))}" + (f"<br><span class='muted'>{stop_d:.2f}% away</span>" if stop_d is not None else "")
    if stop_d is not None and stop_d < 0.3:
        stop_cell += _badge("NEAR STOP", "dn")
    if p.get("trail_ema") is not None:
        td = p.get("trail_dist_pct")
        trail_cell = (f"EMA {_fmt(p['trail_ema'])}<br><span class='muted'>"
                      f"{'' if td is None else f'{td:+.2f}% from price'}</span>")
        if td is not None and td < 0:
            trail_cell += _badge("PAST TRAIL", "dn", "Price is on the wrong side of the EMA: if the 15-min bar "
                                 "closes here, the bot exits")
        elif td is not None and td < 0.2:
            trail_cell += _badge("NEAR TRAIL", "warn", "Within 0.2% of the EMA trail")
        else:
            trail_cell += _badge("TRAIL", "warn", "Exit when a 15-min close crosses the EMA")
    else:
        trail_cell = f"target {_fmt(p.get('target'))}" if p.get("target") else "-"
    tc = p.get("time_check")
    if tc == "passed":
        time_cell = _badge("PASSED", "up")
    elif tc == "pending":
        need = C.TIME_STOP_MIN_R
        ok_now = r is not None and r >= need
        time_cell = (f"at {html.escape(str(p.get('time_check_at')))}<br><span class='muted'>needs ≥ {need:+.1f}R</span>"
                     + _badge("ON TRACK" if ok_now else "AT RISK", "up" if ok_now else "warn",
                              "Exits at the time-stop check unless the trade is at least this far in profit"))
    else:
        time_cell = "-"
    held = p.get("held_min")
    held_cell = "-" if held is None else (f"{held} min" if held < 60 else f"{held // 60} h {held % 60:02d} min")
    return (f"<tr><td><b>{_fmt(p.get('symbol'))}</b></td>"
            f"<td class='{'up' if long_ else 'dn'}'>{html.escape(str(p.get('direction')))} · {p.get('qty')}</td>"
            f"<td>{_fmt(p.get('entry'))} → {_fmt(p.get('price'))}</td>"
            f"<td class='{pnl_cls}'><b>₹{_fmt(p.get('unrealised'))}</b>"
            f"{'' if r is None else f'<br>{r:+.2f}R'}</td>"
            f"<td>{stop_cell}</td><td>{trail_cell}</td><td>{time_cell}</td><td>{held_cell}</td></tr>")


def render(msg="", q=None):
    s = read_status()
    view = current_view(q or {}, s)
    running = bot_running(s)
    ok, login_msg = login_status()
    url = login_url()
    mode = s.get("mode", "paper" if not C.is_live() else "live").upper()
    lag = s.get("data_lag_min")
    lag_txt = "-" if lag is None else f"{lag:.0f} min"
    lag_cls = "warn" if (lag or 0) > 20 else ""
    paused = s.get("paused")

    pos_rows = "".join(position_row(p) for p in s.get("positions", [])) or \
        "<tr><td colspan=8 class='muted'>No open positions</td></tr>"

    sig_rows = "".join(
        f"<tr><td>{_fmt(sym)}</td><td>{_fmt(r.get('bar'))}</td><td>{_fmt(r.get('or_low'))} – {_fmt(r.get('or_high'))}</td>"
        f"<td>{_fmt(r.get('close'))}</td><td>{_fmt(r.get('vwap'))}</td><td>{_fmt(r.get('vol_ratio'), 1)}</td>"
        f"<td class='{'sig' if 'breakout' in (r.get('reason') or '') else ''}'>{_fmt(T.decorate(r.get('reason')) or None)}</td></tr>"
        for sym, r in sorted(s.get("rows", {}).items())) or "<tr><td colspan=7 class='muted'>Waiting for market data</td></tr>"

    events = "".join(f"<li>{html.escape(e)}</li>" for e in s.get("events", [])) or "<li class='muted'>-</li>"
    login_btn = (f"<a class='btn' href='{html.escape(url)}'>Log in to Zerodha</a>" if url and not ok else "")
    when = (s.get("time") or "")
    banner = "" if running else (
        f"<div class='banner'><b>Bot is not running</b> ({T.name('not_running')}: markets closed, weekend, or stopped). "
        f"Showing the last saved state{' from ' + html.escape(when[:16].replace('T', ' ')) if when else ''}. "
        f"See <a href='/review' style='color:#fde68a'>{T.term('review')}</a> for how the last trading day played out, "
        f"or <a href='/trades' style='color:#fde68a'>{T.term('trades')}</a> for history.</div>")
    calendar = market_calendar()
    controls = (f"""<div class="card"><h2>{T.term('controls')}</h2>
<form method="post" action="/{'resume' if paused else 'pause'}" style="display:inline"><button>{'Resume new entries' if paused else T.name('pause') + ' · Pause new entries'}</button></form>
<form method="post" action="/stop" style="display:inline" onsubmit="return confirm('Stop the bot? In live mode open positions keep their exchange stop-loss.')"><button class="red">Stop bot</button></form></div>"""
                if running else "")
    cooldown = s.get("cooldown_until")
    streak_note = (f"<br><span class='warn' style='font-size:11px'>{T.name('cooldown')} until {html.escape(str(cooldown))}</span>"
                   if cooldown else f"<br><span class='muted' style='font-size:11px'>{T.name('cooldown')} after {C.LOSS_STREAK_LIMIT}</span>")
    run_tag = T.term("running") if running else T.term("not_running")
    return page(T.plain("live_page"), "/", f"""
<h1>{T.BOW} {T.name('live_page')} <span class="tag" title="The mode the bot trades in (set in its own settings)">Bot trading: {html.escape(T.mode_label(mode))}</span> <span class="tag {'up' if running else 'warn'}">{run_tag}</span> {f'<span class="tag warn">{T.name("pause").upper()} · PAUSED</span>' if paused else ''}</h1>
<div class="muted">{T.plain('live_page')} · bot status updated {_fmt(when[11:19])} IST · page auto-refreshes every 20 s</div>
{view_banner(view)}
{account_card(view, s)}
{capital_card() if view == "paper" else ""}
{banner}
{calendar}
{f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
<div class="card"><h2>{T.term('login')}</h2>
<div class="{'up' if ok else 'warn'}">{html.escape(login_msg)}</div>{login_btn}
<div class="muted">{T.term('paper')} mode doesn't need a login, except for {T.name('kite_data')} (Kite) price data.
{T.term('live')} mode needs one every day (tokens expire ~06:00 IST).</div></div>
<div class="card kpis">
<div>Trades today<b>{_fmt(s.get('trades_today'))}</b></div><div>Loss streak<b>{_fmt(s.get('loss_streak'))}</b>{streak_note}</div>
<div>Data delay<b class="{lag_cls}">{lag_txt}</b></div>{data_source_kpi(s)}</div>
<div class="card"><h2>{T.term('positions')} held by the bot <span class="tag">{html.escape(T.mode_label(mode))}</span></h2><table class="wrap"><tr><th>Stock</th><th>Side · Qty</th><th>Entry → Price</th><th>Open P&amp;L</th>
<th>{T.term('stop')}</th><th>{T.term('trail')}</th><th>{T.term('time_stop')}</th><th>In trade</th></tr>{pos_rows}</table></div>
<div class="card"><h2>{T.term('watchlist')} (last closed 15-min bar)</h2><table><tr><th>Stock</th><th>Bar</th><th>Opening range</th><th>Close</th><th>VWAP</th><th>Vol ×</th><th>Status</th></tr>{sig_rows}</table></div>
{controls}
<div class="card"><h2>{T.term('events')}</h2><ul>{events}</ul></div>""", refresh=20, view=view)


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
                f"fill='{T.COLORS['acc']}' opacity='0.18'/>")
    col = T.COLORS["up"] if closes[-1] >= closes[0] else T.COLORS["dn"]
    return (f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' role='img' aria-label='intraday closes'>"
            f"{band}<polyline points='{pts}' fill='none' stroke='{col}' stroke-width='1.5'/></svg>")


def render_review(day=None, refresh=False):
    import review
    h1 = f"<h1>{T.LAMP} {T.term('review')}</h1>"
    try:
        r = review.build(day, refresh=refresh) if (day or refresh) else review.latest()
    except Exception as e:
        return page("Session review", "/review", f"{h1}<div class='banner'>Could not build the review: {html.escape(str(e))}</div>")
    if not r.get("day"):
        return page("Session review", "/review", f"{h1}<div class='banner'>No price data right now - try Refresh later.</div>")
    days = "".join(f"<a class='{'on' if d == r['day'] else ''}' href='/review?day={d}'>{d[5:]}</a>"
                   for d in r.get("days", [])[-10:])
    rows = []
    for sym, x in sorted(r["rows"].items()):
        chg = x.get("day_change_pct")
        if x.get("direction"):
            res = x["r"]
            outcome = (f"<span class='{'up' if x['direction'] == 'LONG' else 'dn'}'>{x['direction']}</span> "
                       f"{x['signal_time']} → entry {_fmt(x['entry'])}, stop {_fmt(x['stop'])}, target {_fmt(x['target'])}"
                       f"<br><span class='muted'>exit {html.escape(T.reason_label(str(x['exit_reason'])))} {x['exit_time']} @ {_fmt(x['exit'])}</span>")
            tm = x.get("time_machine") or {}
            if tm.get("text"):
                outcome += (f"<br><span class='tm'>{T.SEER} {T.term('time_machine')} ({tm.get('n', 0):,} replays, seed {tm.get('seed', '-')}): "
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
<h1>{T.LAMP} {T.term('review')} <span class="tag">{r['day']}</span></h1>
<div class="muted">At Chitrakoot we look back calmly: the day replayed through the strategy rules, one stock at a time
(portfolio limits not applied). Built {html.escape(str(r.get('built', ''))[:16].replace('T', ' '))} from 15-min data.</div>
<div class="card days">Day: {days} <form method="get" action="/review" style="display:inline">
<input type="hidden" name="day" value="{r['day']}"><input type="hidden" name="refresh" value="1"><button>Refresh</button></form></div>
<div class="card kpis"><div>Signals<b>{r.get('signals', 0)}</b></div><div>Winners<b>{r.get('wins', 0)}</b></div>
<div>Total<b class="{'up' if total > 0 else 'dn'}">{total:+.2f}R</b></div>
<div>At 0.5% risk on ₹1L<b class="{'up' if total > 0 else 'dn'}">≈ ₹{total * C.PAPER_START_INR * C.RISK_PER_TRADE_PCT / 100:+,.0f}</b></div></div>
<div class="card"><h2>Stocks — chart shows 15-min closes, saffron band = opening range</h2><table>
<tr><th>Stock</th><th>Day</th><th>Opening range</th><th>Day %</th><th>Signal → outcome</th><th>Result</th></tr>{''.join(rows)}</table></div>
<div class="card muted">Rules: {html.escape(settings)}. R = profit or loss as a multiple of the risk
(entry to stop), after 0.05% slippage and Zerodha charges.<br>
{T.SEER} {T.term('time_machine')}: the trade replayed 10,000 times using this stock's own 5-minute moves from the same
time of day (±10 min) over the last 60 days, shuffled. It assumes no edge, so it's a baseline: a good setup
should beat these odds. The replays also inherit the stock's recent intraday drift.
The verdict compares target-first vs stop-first at 1R with break-even (50%): within 5 points is a coin flip.
{T.SCROLL} History is the real record: the bot's same rules (same direction) run on each earlier session.</div>"""
    return page("Session review", "/review", body)


def render_scanner(refresh=False):
    import scanner
    try:
        r = scanner.scan() if refresh else scanner.latest()
    except Exception as e:
        return page("Swing scanner", "/scanner", f"<h1>{T.FLAG} {T.term('scanner')}</h1><div class='banner'>Scan failed: {html.escape(str(e))}</div>")
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
<h1>{T.FLAG} {T.term('scanner')} <span class="tag">{html.escape(str(r.get('day')))}</span></h1>
<div class="banner">Research only - Hanuman scouts, the bot does not trade these. They are multi-day (CNC) setups on daily candles;
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


TRADE_FILES = {"paper": "trades_paper.csv", "live": "trades_live.csv", "backtest": "backtest_trades.csv"}


def trade_rows(src):
    return _read_csv(os.path.join(C.DATA_DIR, TRADE_FILES[src])) if src in TRADE_FILES else []


def breakdown_table(rows, key, title):
    """Count, win rate, net ₹ and average R per group (exit reason, side, stock)."""
    groups = {}
    for r in rows:
        k = (r.get(key) or "-").split(" (")[0]
        if key == "reason":
            k = T.reason_label(k)
        groups.setdefault(k, []).append(r)
    out = []
    for k, g in sorted(groups.items(), key=lambda kv: sum(_num(r.get("net")) for r in kv[1])):
        net = sum(_num(r.get("net")) for r in g)
        wins = sum(1 for r in g if _num(r.get("net")) > 0)
        avg_r = sum(_num(r.get("r")) for r in g) / len(g)
        out.append(f"<tr><td>{html.escape(k)}</td><td>{len(g)}</td><td>{wins / len(g):.0%}</td>"
                   f"<td class='{'up' if net > 0 else 'dn'}'>{'-' if net < 0 else ''}₹{abs(net):,.0f}</td>"
                   f"<td class='{'up' if avg_r > 0 else 'dn'}'>{avg_r:+.2f}R</td></tr>")
    return (f"<div style='flex:1 1 260px'><h2>{html.escape(title)}</h2><table><tr><th></th><th>Trades</th>"
            f"<th>Won</th><th>Net</th><th>Avg</th></tr>{''.join(out)}</table></div>")


def trade_section(rows, src, label, start_cash, show_log=True):
    """KPIs, equity curve, P&L bars, breakdowns and the trade log (with chart links) for one trade list."""
    import charts
    nets = [_num(r.get("net")) for r in rows]
    equity, eq = [], start_cash
    for v in nets:
        eq += v
        equity.append(eq)
    peak, max_dd = start_cash, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100 if peak else 0)
    net, wins = sum(nets), sum(1 for v in nets if v > 0)
    tips = [f"{r.get('symbol', '')} {r.get('direction', '')} {r.get('closed', '')[:10]} ({_num(r.get('r')):+.2f}R)"
            for r in rows]
    kpis = (f"<div class='card kpis'><div>{html.escape(label)} trades<b>{len(rows)}</b></div>"
            f"<div>Win rate<b>{(wins / len(rows) * 100 if rows else 0):.0f}%</b></div>"
            f"<div>Net P&amp;L<b class='{'up' if net >= 0 else 'dn'}'>₹{net:,.2f}</b></div>"
            f"<div>Max drawdown<b class='{'dn' if max_dd else ''}'>{max_dd:.2f}%</b></div></div>")
    if not rows:
        return kpis
    charts_html = (f"<div class='card'><h2>{html.escape(label)}: equity after each closed trade "
                   f"(red area = drawdown from the peak)</h2>{charts.equity_curve(equity, start_cash, tips)}"
                   f"<h2 style='margin-top:12px'>Profit or loss per trade (hover a bar for details)</h2>"
                   f"{charts.pnl_bars(nets, tips)}</div>"
                   f"<div class='card' style='display:flex;flex-wrap:wrap;gap:18px'>"
                   f"{breakdown_table(rows, 'reason', 'By exit reason')}"
                   f"{breakdown_table(rows, 'direction', 'Long vs short')}"
                   f"{breakdown_table(rows, 'symbol', 'By stock')}</div>")
    log = ""
    if show_log:
        idx_rows = list(enumerate(rows))[-200:][::-1]
        body = "".join(
            f"<tr><td>{html.escape(r.get('closed', '')[:16])}</td><td>{html.escape(r.get('symbol', ''))}</td>"
            f"<td class='{'up' if r.get('direction') == 'LONG' else 'dn'}'>{html.escape(r.get('direction', ''))}</td>"
            f"<td>{html.escape(r.get('qty', ''))}</td><td>{html.escape(r.get('entry', ''))}</td><td>{html.escape(r.get('exit', ''))}</td>"
            f"<td>{html.escape(T.reason_label(r.get('reason', '')))}</td><td>{html.escape(r.get('charges', ''))}</td>"
            f"<td class='{'up' if _num(r.get('net')) > 0 else 'dn'}'>{_num(r.get('net')):,.2f}</td><td>{_num(r.get('r')):+.2f}</td>"
            f"<td><a class='sig' href='/journey?src={src}&amp;i={i}' title='{T.name('journey')}: the trade on its chart'>{T.PATH} chart</a></td></tr>"
            for i, r in idx_rows)
        table = (f"<table><tr><th>Closed</th><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th>"
                 f"<th>Reason</th><th>Charges ₹</th><th>Net ₹</th><th>R</th><th></th></tr>{body}</table>")
        log = (f"<div class='card'><h2>{html.escape(label)} trade log (newest first)</h2>{table}</div>" if src != "backtest" else
               f"<div class='card'><details><summary class='sig' style='cursor:pointer'>Show all {len(rows)} backtest trades "
               f"(with charts)</summary>{table}</details></div>")
    return kpis + charts_html + log


def _money(v):
    if v is None:
        return "–"
    return f"<span class='{'up' if v > 0 else 'dn' if v < 0 else ''}'>{'-' if v < 0 else ''}₹{abs(v):,.0f}</span>"


def _maybe(v, fmt):
    return "–" if v is None else fmt.format(v)


def journal_card(mode, view, period):
    """Daily | Weekly | Monthly results from the daily journal (data/journal/journal_<mode>.csv)."""
    import journal
    period = period if period in journal.PERIODS else "day"
    rows = journal.days(mode)
    tabs = " ".join(f"<a class='{'on' if p == period else ''}' href='/trades?view={view}&amp;period={p}#journal'>{name}</a>"
                    for p, name in journal.PERIODS.items())
    dl = (f"<a class='sig' href='/journal.csv?view={view}&amp;period={period}' download>⬇ Download CSV</a>"
          if rows else "")
    head = (f"<div class='card' id='journal'><h2>{T.SCROLL} Daily journal · {html.escape(T.name(mode))}</h2>"
            f"<div class='days'>{tabs} {dl}</div>")
    if not rows:
        return (head + "<div class='muted'>No days yet. The bot writes one row per trading day to "
                f"data\\journal\\journal_{mode}.csv from its next start.</div></div>")
    status_cls = {"complete": "up", "rebuilt": "muted", "running": "warn"}
    if period == "day":
        body = []
        for r in reversed(rows):
            st = r.get("status") or ""
            sig = journal._opt(r.get("signals"))
            sig_txt = ("–" if sig is None else
                       f"{int(sig)} <span class='muted'>({int(journal._f(r.get('taken')))} taken, "
                       f"{int(journal._f(r.get('skipped')))} skipped)</span>")
            tip = html.escape(r.get("skip_reasons") or "")
            trades = int(journal._f(r.get("trades")))
            body.append(
                f"<tr><td>{html.escape(journal.period_label(r['date'], 'day'))}</td>"
                f"<td class='{status_cls.get(st, 'dn')}'>{html.escape(st)}</td>"
                f"<td>{trades}</td><td>{int(journal._f(r.get('wins')))}</td>"
                f"<td>{_money(journal._opt(r.get('net_pnl')))}</td>"
                f"<td>{_maybe(journal._opt(r.get('return_pct')), '{:+.2f}%')}</td>"
                f"<td>₹{journal._f(r.get('charges')):,.0f}</td>"
                f"<td>{_maybe(journal._f(r.get('total_r')) / trades if trades else None, '{:+.2f}R')}</td>"
                f"<td>{_maybe(journal._opt(r.get('max_drawdown_pct')), '{:.2f}%')}</td>"
                f"<td title='{tip}'>{sig_txt}</td>"
                f"<td>{_maybe(journal._opt(r.get('outage_min')), '{:.0f} min')}</td>"
                f"<td>{html.escape(r.get('bot_start') or '?')}–{html.escape(r.get('bot_stop') or '?')}</td>"
                f"<td class='muted' style='white-space:normal;min-width:150px'>{html.escape(r.get('note') or '')}</td></tr>")
        table = ("<table><tr><th>Day</th><th>Status</th><th>Trades</th><th>Won</th><th>Net</th><th>Return</th>"
                 "<th>Charges</th><th>Avg R</th><th>Worst drawdown</th><th>Signals</th><th>Data outages</th>"
                 f"<th>Bot ran</th><th>Note</th></tr>{''.join(body)}</table>")
    else:
        body = []
        for s in reversed(journal.summarise(rows, period)):
            odd = [st for st in s["statuses"] if st != "complete"]
            days_txt = f"{s['days']}" + (f" <span class='muted'>({s['full_days']} full)</span>" if odd else "")
            body.append(
                f"<tr><td>{html.escape(s['label'])}</td><td>{days_txt}</td><td>{s['trades']}</td>"
                f"<td>{_maybe(s['win_rate'], '{:.0f}%')}</td><td>{_money(s['net'])}</td>"
                f"<td>{s['return_pct']:+.2f}%</td><td>₹{s['charges']:,.0f}</td>"
                f"<td>{_maybe(s['avg_r'], '{:+.2f}R')}</td>"
                f"<td>{_money(s['best_day'])} / {_money(s['worst_day'])}</td>"
                f"<td>{journal.drawdown_text(s)}</td>"
                f"<td title='{html.escape(s['skip_reasons'])}'>{_maybe(s['skipped'], '{:.0f}')}</td>"
                f"<td>{_maybe(s['outage_min'], '{:.0f} min')}</td></tr>")
        table = ("<table><tr><th>" + ("Week" if period == "week" else "Month") + "</th><th>Days</th><th>Trades</th>"
                 "<th>Won</th><th>Net</th><th>Return</th><th>Charges</th><th>Avg R</th><th>Best / worst day</th>"
                 f"<th>Worst drawdown</th><th>Skipped signals</th><th>Data outages</th></tr>{''.join(body)}</table>")
    notes = ("<div class='muted' style='margin-top:6px'>Newest first. <b>complete</b> = the bot ran to the close; "
             "<b>stopped HH:MM</b> = stopped by the STOP file; <b>incomplete</b> = the bot stopped without saying "
             "goodbye (crash, PC off); <b>rebuilt</b> = from the trade log only, so signals, drawdown and outages "
             "are unknown (–). Worst drawdown = the biggest fall from a high, inside a day (checked every round) or "
             "from one day's close to a later one; * = some days unknown. Hover a signal count for the skip "
             f"reasons. Saved in data\\journal\\journal_{mode}.csv.</div>")
    import reports
    files = reports.saved(mode)
    saved = ""
    if files:
        links = " · ".join(f"<a class='sig' href='/reports/{html.escape(n)}' target='_blank'>"
                           f"{html.escape(n.split('_', 1)[1].replace('_', ' ').replace('.html', ''))}</a>"
                           for n in files[:12])
        saved = (f"<div style='margin-top:8px'>{T.SCROLL} Saved reports (data\\reports, open and print or save as PDF): "
                 f"{links}{' · …' if len(files) > 12 else ''}</div>")
    return head + table + notes + saved + "</div>"


def render_trades(q=None):
    s = read_status()
    view = current_view(q or {}, s)
    mode = "live" if view == "zerodha" else "paper"   # each view reads only its own trade file
    parts = [f"<h1>{T.SCROLL} {T.term('trades')}</h1>"
             f"<div class='muted'>The royal treasury's ledger: every closed trade, after Zerodha charges.</div>",
             mode_chips(s), view_banner(view)]
    rows = trade_rows(mode)
    label = f"{T.name(mode)} · {VIEWS[view]}"
    start, note = C.PAPER_START_INR, ""
    if mode == "paper":
        import capital
        start = capital.current_opening_balance()
        if capital.load().get("history"):
            note = (f"<div class='muted'>The equity curve and drawdown use the current opening balance "
                    f"({_inr(start)}). {capital_lines()}</div>")
    parts.append(f"<div class='card acct {view}' style='padding:4px 16px'>"
                 + trade_section(rows, mode, label, start) + note + "</div>")
    parts.append(journal_card(mode, view, (q or {}).get("period", ["day"])[0]))
    parts.append(edge_card(rows, f"{mode.title()} trading"))
    if not rows:
        parts.append("<div class='card muted'>No trades yet - the bot records them here once it trades on a market day.</div>"
                     if mode == "paper" else
                     "<div class='card muted'>No real-money trades: the bot has only traded on paper. This list shows "
                     "trades the bot places in live mode (data\\trades_live.csv), not your whole Zerodha tradebook.</div>")
    bt = trade_rows("backtest")
    if bt:
        first, last = bt[0].get("opened", "")[:10], max(r.get("closed", "")[:10] for r in bt)
        parts.append(f"<h1 style='margin-top:26px'>{T.FIRE} {T.name('backtest')} · Last backtest <span class='tag'>{html.escape(first)} → {html.escape(last)}</span></h1>"
                     f"<div class='muted'>Run <code>.venv\\Scripts\\python.exe backtest.py</code> to update. "
                     f"Full list: data\\backtest_trades.csv</div>")
        parts.append(trade_section(bt, "backtest", "Backtest", C.PAPER_START_INR))
        parts.append(edge_card(bt, "Backtest", compact=True))
    return page("Trades", "/trades", "".join(parts), view=view)


_BARS_CACHE = {}


def day_bars(symbol, day):
    """15-min bars of `symbol` for `day` plus all earlier bars (for the EMA), cached for 10 minutes."""
    import time
    import data_yahoo
    import marketdata
    hit = _BARS_CACHE.get(symbol)
    if not hit or time.time() - hit[0] > 600:
        bars, _, _ = marketdata.fetch(symbol, C.INTERVAL_MIN, "60d")
        import datetime as dt
        bars = data_yahoo.closed_bars(bars, dt.datetime.now(data_yahoo.IST), C.INTERVAL_MIN)
        _BARS_CACHE[symbol] = hit = (time.time(), bars)
    allbars = hit[1]
    upto = [b for b in allbars if b["t"].date() <= day]
    return upto, [b for b in upto if b["t"].date() == day]


def render_journey(src, i):
    """One trade drawn on its day's 15-min candles, with stop, entry/exit markers and the EMA trail."""
    import datetime as dt
    import charts
    import strategy
    rows = trade_rows(src)
    h1 = f"<h1>{T.PATH} {T.term('journey')}</h1>"
    if i is None or not 0 <= i < len(rows):
        return page("Trade chart", "/trades", f"{h1}<div class='banner'>Trade not found.</div>")
    r = rows[i]
    sym = r.get("symbol", "")

    def ts(v):
        try:
            return dt.datetime.fromisoformat(v)
        except (TypeError, ValueError):
            return None
    opened, closed = ts(r.get("opened")), ts(r.get("closed"))
    if not opened:
        return page("Trade chart", "/trades", f"{h1}<div class='banner'>This trade has no entry time.</div>")
    try:
        upto, today = day_bars(sym, opened.date())
    except Exception as e:  # data errors
        upto, today = [], []
        err = str(e)
    else:
        err = ""
    if not today:
        note = err or "Yahoo keeps only about 60 days of 15-minute data, so older trades can't be drawn."
        chart = f"<div class='banner'>No price data for {html.escape(sym)} on {opened:%d %b %Y}. {html.escape(note)}</div>"
    else:
        ema = None
        if C.TRAIL_EMA > 0:
            start = len(upto) - len(today)
            ema = [strategy.ema([b["c"] for b in upto[max(0, k - 4 * C.TRAIL_EMA): k + 1]], C.TRAIL_EMA)
                   for k in range(start, len(upto))]
        orng = strategy.opening_range(today)
        trade = {"direction": r.get("direction"), "entry": _num(r.get("entry")), "exit": _num(r.get("exit")),
                 "stop": _num(r.get("stop")), "target": _num(r.get("target")) or None,
                 "opened": opened, "closed": closed}
        chart = charts.journey(today, trade, ema, (orng["low"], orng["high"]) if orng else None)
    net, rr = _num(r.get("net")), _num(r.get("r"))
    held = f"{int((closed - opened).total_seconds() // 60)} min" if closed else "-"
    body = f"""
<h1>{T.PATH} {T.name('journey')}: {html.escape(sym)} <span class="tag {'up' if r.get('direction') == 'LONG' else 'dn'}">{html.escape(r.get('direction', ''))}</span>
<span class="tag">{opened:%a %d %b %Y}</span> <span class="tag">{html.escape(src)}</span></h1>
<div class="muted"><a class="sig" href="/trades">← back to {T.term('trades')}</a></div>
<div class="card kpis"><div>Result<b class="{'up' if net > 0 else 'dn'}">₹{net:,.2f} ({rr:+.2f}R)</b></div>
<div>Entry<b>{_fmt(_num(r.get('entry')))} at {opened:%H:%M}</b></div>
<div>Exit<b>{_fmt(_num(r.get('exit')))} at {closed.strftime('%H:%M') if closed else '-'}</b></div>
<div>Exit reason<b>{html.escape(T.reason_label(r.get('reason', '-')))}</b></div><div>Held<b>{held}</b></div>
<div>Qty<b>{html.escape(r.get('qty', '-'))}</b></div><div>Charges<b>₹{_num(r.get('charges')):,.2f}</b></div></div>
<div class="card"><h2>15-minute candles · saffron band = opening range · red dashed = {T.term('stop')} · grey dotted = entry ·
violet = {f'EMA{C.TRAIL_EMA} trail' if C.TRAIL_EMA > 0 else 'no trail'} · ▲▼ = entry and exit</h2>{chart}</div>
<div class="card muted">Hover a candle for its prices. The chart uses today's price data, so it can differ slightly
from what the bot saw live if a bar has since been corrected.</div>"""
    return page(f"{sym} trade", "/trades", body)


def edge_card(rows, label, compact=False):
    """The 30-trade edge check: progress bar until 30 trades, then a verdict on expectancy."""
    import edge
    c = edge.edge_check(rows)
    title, text = edge.message(c)
    cls = {"positive": "up", "unproven": "warn", "too_early": "muted"}.get(c["verdict"], "dn")
    n, need = c["n"], c["min_trades"]
    bar = ""
    if c["verdict"] == "too_early":
        pct = min(100, n / need * 100)
        bar = (f"<div style='background:var(--line);border-radius:6px;height:10px;margin:8px 0;max-width:420px'>"
               f"<div style='background:var(--acc);width:{pct:.0f}%;height:10px;border-radius:6px'></div></div>"
               f"<div class='muted'>{n} of {need} trades logged</div>")
    stats = ""
    if n:
        pf = c["profit_factor"]
        rng = "-" if n < 2 else f"{c['r_low']:+.2f} to {c['r_high']:+.2f}R"
        stats = (f"<div class='kpis' style='margin-top:10px'><div>Expectancy<b class='{'up' if c['avg_r'] > 0 else 'dn'}'>"
                 f"{c['avg_r']:+.2f}R</b></div><div>Per trade<b>₹{c['avg_net']:,.0f}</b></div>"
                 f"<div>Avg win / loss<b>₹{c['avg_win']:,.0f} / ₹{c['avg_loss']:,.0f}</b></div>"
                 f"<div>Profit factor<b>{'-' if pf is None else f'{pf:.2f}'}</b></div>"
                 f"<div>95% range<b>{rng}</b></div></div>")
    head = "" if compact else ("<div class='muted'>Rule from Trading With Sidhant: after 30 trades, check that the average "
                               "result per trade is positive. If it isn't, stop trading the setup.</div>")
    return (f"<div class='card'><h2>{T.FIRE} {T.name('edge')} · 30-trade edge check: {html.escape(label)}</h2>{head}"
            f"<div style='margin-top:8px'><b class='{cls}'>{html.escape(title)}</b> "
            f"<span>{html.escape(text)}</span></div>{bar}{stats}</div>")


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
            self._send(render(q.get("msg", [""])[0], q))
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
            self._send(render_trades(q))
        elif u.path == "/journal.csv":
            # read-only download of the journal table; each view reads only its own account's journal
            import journal
            view = q.get("view", ["paper"])[0]
            mode = "live" if view == "zerodha" else "paper"
            period = q.get("period", ["day"])[0]
            period = period if period in journal.PERIODS else "day"
            data = journal.summary_csv(mode, period).encode("utf-8-sig")   # BOM: Excel shows ₹ correctly
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="journal_{mode}_{period}.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif u.path.startswith("/reports/"):
            import reports
            name = u.path[len("/reports/"):]
            path = os.path.join(reports.reports_dir(), name)
            if not reports.NAME_RE.match(name) or not os.path.isfile(path):
                self._send("not found", 404, "text/plain")
                return
            with open(path, encoding="utf-8") as f:
                self._send(f.read())
        elif u.path == "/journey":
            try:
                idx = int(q.get("i", [""])[0])
            except ValueError:
                idx = None
            self._send(render_journey(q.get("src", ["paper"])[0], idx))
        elif u.path == "/scanner":
            self._send(render_scanner(refresh=q.get("refresh", ["0"])[0] == "1"))
        elif u.path == "/api/account":
            # read-only: renders the account card; never changes the bot's mode or settings
            self._send(account_card_html(q.get("view", ["paper"])[0], read_status()))
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
                self._redirect(f"/?msg=Setu+crossed:+logged+in+as+{user}.+Token+saved+for+today.")
            except Exception as e:
                self._send(render(f"Token exchange failed: {e}"), 500)
        else:
            self._send("not found", 404, "text/plain")

    def _paper_capital(self, origin):
        """Save or cancel a PAPER opening-balance change. Needs the dashboard's own page (Origin/Referer)
        and the confirmation the page adds after you click OK. Writes only data/paper_capital.json."""
        from urllib.parse import quote
        import capital
        if not origin:
            self._send("forbidden: open the dashboard page to change the opening balance", 403, "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10_000:
            self._send("too large", 413, "text/plain")
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if form.get("confirm", [""])[0] != "yes":
            msg = "Not changed: the change needs your confirmation."
        elif self.path == "/paper-capital/cancel":
            msg = capital.cancel()
        else:
            amount = form.get("amount", [""])[0]
            if amount == "custom":
                amount = form.get("custom_amount", [""])[0]
            try:
                msg = capital.request(amount)
            except ValueError as e:
                msg = f"Not changed: {e}"
        self._redirect("/?view=paper&msg=" + quote(msg))

    def do_POST(self):
        # Browsers on other sites can't read the response, but they can POST: only accept same-origin.
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if origin and not origin.startswith(f"http://{C.WEB_HOST}:{C.WEB_PORT}"):
            self._send("forbidden", 403, "text/plain")
            return
        os.makedirs(C.DATA_DIR, exist_ok=True)
        if self.path in ("/paper-capital", "/paper-capital/cancel"):
            self._paper_capital(origin)
            return
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
    print(T.banner(f"{T.name('live_page')} dashboard only (no trading)"))
    print(f"  Dashboard: http://{C.WEB_HOST}:{C.WEB_PORT}   (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://{C.WEB_HOST}:{C.WEB_PORT}")
    try:
        serve()
    except KeyboardInterrupt:
        pass
