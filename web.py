#!/usr/bin/env python3
"""
Local dashboard + one-click Zerodha login on http://127.0.0.1:5000 (this PC only).

The Kite app's redirect URL must be  http://127.0.0.1:5000/kite/callback
After you log in, Zerodha redirects here, the request_token is exchanged for today's
access token through the static-IP proxy, and the token is saved to .kite_token.json.

Runs inside bot.py automatically. To log in without the bot:  .venv\\Scripts\\python.exe web.py
"""

import hmac
import html
import json
import logging
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

import config as C
import jobs
import theme as T
import webui

try:   # what this dashboard runs (version and file hashes), to tell when a dashboard update is waiting
    import buildinfo
    DASH_BUILD = buildinfo.snapshot("dashboard", with_settings=False)
except Exception:  # noqa: BLE001 - never stops the dashboard
    buildinfo, DASH_BUILD = None, None

STATUS_FILE = os.path.join(C.DATA_DIR, "status.json")
log = logging.getLogger("ayodhya")

# ---------------------------------------------------------------- request safety
# Every button that changes something (pause, stop, opening balance, Zerodha logout) must come from this
# dashboard's own page: the browser's Origin (or Referer) must be exactly this dashboard, the Host header must
# be this dashboard (so another website can't pose as it via DNS tricks), and the form must carry CSRF_TOKEN,
# a secret made fresh every time the dashboard starts and only ever written into its own pages.
CSRF_TOKEN = secrets.token_urlsafe(32)
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def csrf_field():
    return f"<input type='hidden' name='csrf' value='{CSRF_TOKEN}'>"


def host_ok(host, port):
    """The Host header names this dashboard (127.0.0.1 or localhost, on its own port)."""
    try:
        u = urlsplit("http://" + (host or ""))
        return u.hostname in LOCAL_HOSTS and (u.port or 80) == port
    except ValueError:
        return False


def origin_ok(origin, port):
    """The page that sent a form is exactly this dashboard (not a look-alike such as 127.0.0.1:5000.example)."""
    try:
        u = urlsplit(origin or "")
        return u.scheme == "http" and u.hostname in LOCAL_HOSTS and (u.port or 80) == port
    except ValueError:
        return False


def csrf_ok(form):
    return hmac.compare_digest((form.get("csrf") or [""])[0].encode(), CSRF_TOKEN.encode())


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
    """Wrap a page body in the dashboard frame (header with the bot's real trading mode, page tabs, account
    switch, theme switch)."""
    s = read_status()
    switch = view_switch(active, view, s) if view and active in VIEW_PAGES else ""
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return webui.shell(title, active, body, view=view, switch=switch, extra_head=meta, mode=mode_badge(s))


def mode_badge(s):
    """The mode the bot is ACTUALLY trading in, from the running bot's own status (never from the Paper | Zerodha
    view switch). Shown in every page header, in plain words in every theme, never by colour alone."""
    s = s or {}
    mode = s.get("mode")
    if not bot_running(s):
        last = f" · last run: {'REAL MONEY' if mode == 'live' else 'PAPER'}" if mode else ""
        return (f"<span class='mbadge off' title='The bot is not running{last}'>"
                f"<span aria-hidden='true'>○</span><b>Bot not running</b><span class='long'>{last}</span></span>")
    live = mode == "live"
    word, sub = ("REAL MONEY", " · Zerodha") if live else ("PAPER", " · virtual money")
    paused = " · new entries paused" if s.get("paused") else ""
    import livemode
    full = livemode.switch_message(s)
    sw = (f"<span class='mswitch' title='{html.escape(full)}'> · {html.escape(livemode.switch_message(s, short=True))}"
          f"<span class='sr'> ({html.escape(full)})</span></span>" if full else "")
    icon = "⏸" if paused else ("●" if live else "◐")
    return (f"<span class='mbadge {'live' if live else 'paper'}' title='The bot is trading {word}{sub}{paused}. "
            f"Set in its own settings; the account view does not change it.'>"
            f"<span aria-hidden='true'>{icon}</span><span class='lbl'>Bot trading:</span> <b>{word}</b>"
            f"<span class='long'>{sub}{paused}</span>{sw}</span>")


def mode_chips(status, extra=""):
    """The mode the bot trades in (set only in its own settings), next to the account switch."""
    mode = T.mode_label((status or {}).get("mode") or ("live" if C.is_live() else "paper"))
    live = " live" if "LIVE" in mode else ""
    return (f"<div class='chips'><span class='chip mode{live}' title='The mode the bot trades in (set in its own settings)'>"
            f"Bot trading: {html.escape(mode)}</span>{extra}"
            f"<span class='muted small'>The Paper | Zerodha switch only changes what you see.</span></div>")


STATUS_POLL_SEC = 1.0     # the dashboard looks at status.json at most this often, however many pages poll
_STATUS = {"key": None, "text": None, "checked": None}


def read_status():
    """The bot's last saved status.

    On Windows, while any other program is touching a file (reading it, or even asking for its details), a
    program replacing that file is refused ("Access is denied"). That is how the bot's save of status.json
    failed on 18 Nov 2026 with dashboard pages polling it. The bot now retries a refused save (safefile.py),
    which is the real fix; this side simply touches the file as little as possible: at most once every
    STATUS_POLL_SEC it asks whether the file changed, and it opens it only when it did (about once a minute)."""
    import time
    now = time.monotonic()
    fresh = (_STATUS["text"] is not None and _STATUS["key"] and _STATUS["key"][0] == STATUS_FILE
             and _STATUS["checked"] is not None and now - _STATUS["checked"] < STATUS_POLL_SEC)
    try:
        if not fresh:
            st = os.stat(STATUS_FILE)
            # a replaced file is a new file (st_ino changes) even if its time and size happen to match
            key = (STATUS_FILE, st.st_mtime_ns, st.st_size, st.st_ino)
            if STATUS_POLL_SEC <= 0 or _STATUS["key"] != key:
                with open(STATUS_FILE, encoding="utf-8") as f:
                    text = f.read()
                json.loads(text)                   # a half-written file is not kept
                _STATUS.update(key=key, text=text)
            _STATUS["checked"] = now
        return json.loads(_STATUS["text"])         # a fresh copy each time: callers may change it
    except (OSError, ValueError, TypeError):
        _STATUS.update(key=None, text=None, checked=None)
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


def data_source_text(s):
    """'Pushpaka · Kite (real-time)' / 'Yahoo (free, may lag)', with Kite's fallback reason if any."""
    ds = s.get("data_source") or {}
    using = ds.get("using")
    if not using:
        return "-"
    label = f"{T.name('kite_data')} · Kite (real-time)" if using == "kite" else "Yahoo (free, may lag)"
    why = f"<br><span class='warn small'>Kite unavailable: {html.escape(ds['reason'])}</span>" if ds.get("reason") else ""
    return f"<b class='{'up' if using == 'kite' else ''}'>{label}</b>{why}"


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
    return (f"<details class='card' id='calendar'><summary><span class='theme'>{T.term('calendar')}</span>"
            f"<span class='end'><span class='tag'>{state}</span></span></summary>"
            f"<div class='small'>Today: <b>{state}</b> · next trading day <b>{nxt:%a %d %b}</b><br>"
            f"<span class='muted'>Upcoming holidays: {up}</span>{warn}</div></details>")


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


def view_banner(view, s=None):
    """Which account the page shows, and, when that is not the account the bot is trading right now, a plain
    line saying so. The view only changes what is shown; it never changes the trading mode."""
    bar = ("<div class='viewbar zerodha'>LIVE · REAL ZERODHA ACCOUNT · real money · read-only view</div>"
           if view == "zerodha" else "<div class='viewbar paper'>PAPER ACCOUNT · virtual money</div>")
    s = s or {}
    if not bot_running(s):
        return bar
    live = s.get("mode") == "live"
    if live and view == "paper":
        return bar + ("<div class='banner' role='status' style='border-color:var(--dn)'><b>You are looking at the "
                      "PAPER account, but the bot is trading REAL MONEY on Zerodha right now.</b> These paper "
                      "figures are not today's trading. <a href='?view=zerodha'>Show the Zerodha account</a> "
                      "(switching views never changes the trading mode).</div>")
    if not live and view == "zerodha":
        return bar + ("<div class='banner' role='status'><b>You are looking at your real Zerodha account, but the "
                      "bot is trading PAPER (virtual money) right now.</b> It places no real orders. "
                      "<a href='?view=paper'>Show the Paper account</a> (switching views never changes the "
                      "trading mode).</div>")
    return bar


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
         ("Added today", f"<b>{_inr(acct.get('payin') or 0)}</b>"),
         ("Bot sizes trades from", f"<b>{_inr(acct.get('sizing') if acct.get('sizing') is not None else acct['opening'])}</b>"),
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
    return (f"<h2>{title}</h2><div class='kpis'>{kpis}</div>{warn}{logout_form(read_status())}"
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
              f"<input type='hidden' name='confirm' value=''>{csrf_field()}<button class='red'>Cancel pending change</button></form>"
              if pending else "")
    state = (f"<span class='tag warn'>→ {_inr(float(pending['amount']))[:-3]} at next start</span>" if pending
             else "<span class='tag warn'>paper only</span>")
    return f"""<details class="card" id="capital"{' open' if pending else ''}><summary>Opening Balance · Paper Account
<span class="end"><b class="mono">{_inr(current)[:-3]}</b>{state}</span></summary>
<form method="post" action="/paper-capital" onsubmit="return confirmCapital(this)" style="display:inline">
<div class="opts">{radios}</div>
<input type="hidden" name="confirm" value="">{csrf_field()}
<button>Save opening balance</button></form> {cancel}
{capital_lines()}
<div class="muted small" style="margin-top:6px">Now <b>{_inr(current)}</b>. A new amount takes effect when the bot next starts
(normally 9:00), before any trade; if paper positions are open it waits. Past trades and P&amp;L are kept, and
position sizes follow the new equity (0.5% risk per trade). Paper only: it never touches your Zerodha account or
live trading. From {_inr(float(capital.MIN_INR))[:-3]} to {_inr(float(capital.MAX_INR))[:-3]}, whole rupees.</div></details>
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
    return f"<span class='tag {cls}' style='margin-left:4px'{t}>{html.escape(text)}</span>"


def _signed(v, nd=2):
    """▲ ₹1,234.50 / ▼ ₹1,234.50 / ₹0.00 - the arrow means the sign is never told by colour alone."""
    if v is None:
        return "-"
    arrow = "▲ " if v > 0 else "▼ " if v < 0 else ""
    return f"{arrow}₹{abs(v):,.{nd}f}"


def _cls(v):
    return "" if not v else ("up" if v > 0 else "dn")


def _held(m):
    return "-" if m is None else (f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min")


def position_row(p):
    """One open position as a card: P&L in ₹ and R, where the price sits between the stop and the
    target (or +2R when the trade has no fixed target), the EMA trail, the time-stop check, time in trade."""
    long_ = p.get("direction") == "LONG"
    r = p.get("r_unreal")
    entry, price, stop = p.get("entry"), p.get("price"), p.get("stop")
    stop_d = p.get("stop_dist_pct")
    badges = ""
    if stop_d is not None and stop_d < 0.3:
        badges += _badge("NEAR STOP", "dn")
    trail = "-"
    if p.get("trail_ema") is not None:
        td = p.get("trail_dist_pct")
        trail = f"EMA {_fmt(p['trail_ema'])}" + ("" if td is None else f" <span class='muted'>({td:+.2f}%)</span>")
        if td is not None and td < 0:
            badges += _badge("PAST TRAIL", "dn", "Price is on the wrong side of the EMA: if the 15-min bar closes here, the bot exits")
        elif td is not None and td < 0.2:
            badges += _badge("NEAR TRAIL", "warn", "Within 0.2% of the EMA trail")
        else:
            badges += _badge("TRAIL", "warn", "Exit when a 15-min close crosses the EMA")
    tc = p.get("time_check")
    if tc == "passed":
        time_txt = "check passed" + _badge("PASSED", "up")
    elif tc == "pending":
        need = C.TIME_STOP_MIN_R
        ok_now = r is not None and r >= need
        time_txt = (f"check at {html.escape(str(p.get('time_check_at')))} <span class='muted'>· needs ≥ {need:+.1f}R</span>"
                    + _badge("ON TRACK" if ok_now else "AT RISK", "up" if ok_now else "warn",
                             "Exits at the time-stop check unless the trade is at least this far in profit"))
    else:
        time_txt = "-"
    # the rail: stop on the left, target (or +2R) on the right, entry tick and a dot for the price
    rail = ""
    try:
        risk = abs(entry - stop)
        tgt = p.get("target") or (entry + 2 * risk if long_ else entry - 2 * risk)
        lo, hi = (stop, tgt) if long_ else (tgt, stop)
        span = (hi - lo) or 1
        pos = lambda v: max(0.0, min(100.0, (v - lo) / span * 100))  # noqa: E731
        left_lbl, right_lbl = (stop, tgt) if long_ else (tgt, stop)
        tgt_name = f"{T.name('target')} · target" if p.get("target") else "+2R"
        rail = (f"<div class='rail-t'>{T.term('stop')} → {tgt_name}</div>"
                f"<div class='rail' role='img' aria-label='price {_fmt(price)} between stop {_fmt(stop)} and {html.escape(tgt_name)} {_fmt(tgt)}'>"
                f"<span class='entry' style='left:{pos(entry):.1f}%'></span><span class='dot' style='left:{pos(price):.1f}%'></span></div>"
                f"<div class='rail-l'><span class='{'dn' if long_ else 'up'}'>{_fmt(left_lbl)}</span>"
                f"<span>entry {_fmt(entry)}</span><span class='{'up' if long_ else 'dn'}'>{_fmt(right_lbl)}</span></div>")
    except (TypeError, ValueError):
        pass
    unreal = p.get("unrealised")
    return (f"<div class='pos'>"
            f"<div class='sym'><b>{_fmt(p.get('symbol'))}</b><span class='side {'long' if long_ else 'short'}'>"
            f"{html.escape(str(p.get('direction')))}</span>{badges}"
            f"<span class='lbl'>× {p.get('qty')} · in trade {_held(p.get('held_min'))}</span></div>"
            f"<div><span class='lbl'>Entry → price</span>{_fmt(entry)} → <b>{_fmt(price)}</b></div>"
            f"<div><span class='lbl'>Open P&amp;L</span><b class='{_cls(unreal)}'>{_signed(unreal)}"
            f"{'' if r is None else f' · {r:+.2f}R'}</b></div>"
            f"<div class='railbox'>{rail}</div>"
            f"<div class='small'><span class='lbl'>{T.term('time_stop')}</span>{time_txt}"
            f"<span class='lbl' style='margin-top:4px'>{T.term('trail')} · {trail}</span>"
            f"<span class='lbl'>{T.term('stop')} {_fmt(stop)}"
            f"{'' if stop_d is None else f' · {stop_d:.2f}% away'}</span></div></div>")


LIVE_CSS = """
.kwrap{background:none;border:0;box-shadow:none;padding:0;overflow:visible;border-radius:var(--radius)}
.card.acct.kwrap{outline-offset:5px;margin:5px 5px 21px}
.kwrap .warnline{padding:0 2px}
.kwrap .kgrid{margin:0}
.kwrap .warnline{margin-top:8px}
.pos{display:grid;grid-template-columns:1.1fr 1fr 1fr 1.7fr 1.1fr;gap:12px;align-items:start;padding:12px 16px;border-bottom:1px solid var(--border)}
.pos:last-child{border-bottom:0}
.pos .sym b{font-size:15px}
.side{font-size:11px;font-weight:700;padding:1px 6px;border-radius:5px;margin-left:6px}
.side.long{background:var(--up-bg);color:var(--up)} .side.short{background:var(--dn-bg);color:var(--dn)}
.lbl{display:block;font-size:11.5px;color:var(--muted)}
.rail-t{font-size:11.5px;color:var(--muted)}
.rail{position:relative;height:8px;border-radius:99px;margin:10px 0 4px;
 background:linear-gradient(90deg,var(--dn-bg),var(--surface-2) 40%,var(--up-bg))}
.rail .dot{position:absolute;top:50%;width:14px;height:14px;border-radius:50%;background:var(--text);border:3px solid var(--surface);
 transform:translate(-50%,-50%);box-shadow:0 0 0 1px var(--border)}
.rail .entry{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--muted)}
.rail-l{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);gap:6px}
.empty{padding:14px 16px;color:var(--muted)}
.list{list-style:none;margin:0;padding:0}
.list li{padding:7px 0;border-bottom:1px dashed var(--border);font-size:13px;display:flex;gap:10px}
.list li:last-child{border-bottom:0}
.list time{color:var(--muted);font-size:12px;min-width:60px;flex:none}
.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px} .facts dt{color:var(--muted)} .facts dd{margin:0}
.spark svg{width:100%;height:auto;display:block}
@media (max-width:760px){
  .pos{grid-template-columns:1fr 1fr;row-gap:8px;padding:12px 14px}
  .pos .railbox{grid-column:1/-1}
}
"""


def _status_chips(s, running, view, ok_login, lag):
    """Market, bot, mode, data and Zerodha login at a glance."""
    import datetime as dt
    import accounts
    import holidays
    now = dt.datetime.now(accounts.IST)
    if accounts.market_open(now):
        market = f"<span class='chip ok'><i class='pulse'></i>Market open · {now:%H:%M}</span>"
    else:
        today = now.date()
        why = ("weekend" if today.weekday() >= 5 else holidays.holiday_name(today) or
               ("closed for the day" if now.time() > dt.time(15, 30) else "opens 9:15"))
        market = f"<span class='chip'><i></i>Market closed · {html.escape(why)}</span>"
    paused = s.get("paused")
    if running and paused:
        bot_chip = f"<span class='chip amber'><i></i>Bot {T.name('pause')} · PAUSED</span>"
    elif running:
        bot_chip = f"<span class='chip ok'><i></i>{T.name('running')} · bot RUNNING</span>"
    else:
        bot_chip = f"<span class='chip bad'><i></i>{T.name('not_running')} · bot NOT RUNNING</span>"
    mode = T.mode_label(s.get("mode") or ("live" if C.is_live() else "paper"))
    mode_chip = (f"<span class='chip mode{' live' if 'LIVE' in mode else ''}' "
                 f"title='The mode the bot trades in (set in its own settings)'>Bot trading: {html.escape(mode)}</span>")
    ds = s.get("data_source") or {}
    src = "Kite" if ds.get("using") == "kite" else ("Yahoo" if ds.get("using") else None)
    data_chip = ""
    if src:
        # Prices arrive as 15-minute candles, so the newest finished candle is normally 0-16 minutes old. That
        # age used to be shown as "N min behind", which read like a broken feed. Now: the candle's time, and a
        # warning only when a candle is really late (watch_timing).
        w = watch_timing(s)
        late = w["state"] == "late" or (lag is not None and lag > 20)
        why = ds.get("reason") or CANDLE_AGE_TIP
        tip = f" title='{html.escape(why)}'"
        delay = " · candle LATE" if late else (f" · last candle {w['last']}" if w["last"] else "")
        data_chip = (f"<span class='chip {'amber' if late or ds.get('reason') else 'ok'}'{tip}><i></i>"
                     f"{src} data{html.escape(delay)}</span>")
    login_chip = (f"<span class='chip {'ok' if ok_login else ''}'><i></i>Zerodha: "
                  f"{'logged in' if ok_login else 'not logged in'}</span>")
    when = (s.get("time") or "")[11:19]
    upd = f"<span class='updated' id='updated'>Bot status {html.escape(when) if when else '-'} IST</span>"
    return f"<div class='chips' id='status'>{market}{bot_chip}{mode_chip}{data_chip}{login_chip}{upd}</div>"


def _meter(used_pct):
    cls = "high" if used_pct >= 75 else "mid" if used_pct >= 40 else ""
    return (f"<div class='meter {cls}' role='meter' aria-valuemin='0' aria-valuemax='100' aria-valuenow='{used_pct:.0f}'>"
            f"<span style='width:{max(2.0, min(100.0, used_pct)):.0f}%'></span></div>")


def _kpi(label, value, sub="", cls=""):
    return (f"<div class='kpi'><label>{label}</label><div class='v {cls}'>{value}</div>"
            + (f"<div class='s'>{sub}</div>" if sub else "") + "</div>")


def _limits_kpis(s, equity=None, unreal=0.0):
    """Trades used today and how much of the daily loss limit is used (the bot's own counters)."""
    trades, cap = int(s.get("trades_today") or 0), C.MAX_TRADES_PER_DAY
    out = _kpi("Trades today", f"{trades} <small>of {cap}</small>", _meter(trades / cap * 100) if cap else "")
    day = float(s.get("day_pnl") or 0.0) + float(unreal or 0.0)
    eq = equity if equity is not None else s.get("equity")
    if eq:
        start = float(eq) - day
        loss_pct = max(0.0, -day) / start * 100 if start else 0.0
        out += _kpi(f"{T.name('daily_loss')} · daily loss limit",
                    f"{loss_pct:.2f}% <small>of {C.MAX_DAILY_LOSS_PCT:g}%</small>",
                    _meter(loss_pct / C.MAX_DAILY_LOSS_PCT * 100))
    else:
        out += _kpi(f"{T.name('daily_loss')} · daily loss limit", f"- <small>of {C.MAX_DAILY_LOSS_PCT:g}%</small>")
    return out


def funds_sub(a):
    """Under 'Available funds': margin used, and money added today if any."""
    sub = f"margin used {_inr(a['margin_used'])}"
    if a.get("payin"):
        sub += f" · includes {_inr(a['payin'])} added today"
    return sub


def funds_note(a):
    """Why 'Account value' and 'Available funds' differ, and which figure the bot sizes trades from."""
    parts = []
    if a.get("payin"):
        parts.append(f"Available funds include <b>{_inr(a['payin'])} added today</b>; account value is this "
                     f"morning's cash plus today's P&amp;L, so it doesn't.")
    if a.get("collateral"):
        parts.append(f"Available funds include {_inr(a['collateral'])} of margin from pledged shares, "
                     f"which the bot does not use.")
    if a.get("sizing") is not None:
        parts.append(f"The bot sizes real-money trades from <b>{_inr(a['sizing'])}</b> (the smaller of this "
                     f"morning's cash and the cash now"
                     + ("; money added today counts from tomorrow" if a.get("payin") else "") + ").")
    return f"<div class='small warnline'>{' '.join(parts)}</div>" if parts else ""


def live_kpis(view, s):
    """The key figures for the account in view. Paper: from the paper files only. Zerodha: read-only
    from Kite's balance and positions; a login or error message instead when it can't be read."""
    import accounts
    if view == "zerodha":
        a = accounts.zerodha_account()
        if a.get("error"):
            return (f"<section class='card acct zerodha' id='acct'>{zerodha_card(a)}</section>")
        n_open = sum(1 for r in a["positions"] if r["qty"])
        cells = (_kpi("Account value", _inr(a["equity"]), f"opening {_inr(a['opening'])}")
                 + _kpi("Available funds", _inr(a["available"]), funds_sub(a))
                 + _kpi("Today's P&amp;L", _signed(a["pnl"]), f"booked {_inr(a['realised'], True)} · open {_inr(a['unrealised'], True)}",
                        _cls(a["pnl"]))
                 + _kpi("Open P&amp;L", _signed(a["unrealised"]), f"{n_open} open position{'s' if n_open != 1 else ''}",
                        _cls(a["unrealised"]))
                 + _kpi("Invested", _inr(a["invested"]), "open positions at average price")
                 + _kpi("Margin used", _inr(a["margin_used"]), "Zerodha's figure"))
        warn = "".join(f"<div class='warn small warnline'>⚠ {html.escape(w)}</div>" for w in a.get("warnings", []))
        note = (funds_note(a) +
                f"<div class='muted small warnline'>From Zerodha at {_clock(a.get('fetched_at'))} IST · read-only · "
                f"Zerodha's P&amp;L is before charges · delivery holdings not included</div>")
        return f"<section class='card acct zerodha kwrap' id='acct'><div class='kgrid'>{cells}</div>{warn}{note}</section>"
    a = accounts.paper_account()
    if a.get("error"):
        return f"<section class='card acct paper' id='acct'>{paper_card(a, s)}</section>"
    n = len(a["positions"])
    today = a["realised_today"] + a["unrealised"]
    rsum = sum(p.get("r_unreal") or 0 for p in s.get("positions", [])) if s.get("mode") == "paper" else None
    cells = (_kpi("Total equity", _inr(a["equity"]), f"<span class='{_cls(a['total_pnl'])}'>{_signed(a['total_pnl'])}</span> "
                  f"since the {_inr(a['start'])} start")
             + _kpi("Available funds", _inr(a["available"]),
                    f"{_inr(a['invested'])} in {n} position{'s' if n != 1 else ''}")
             + _kpi("Today's P&amp;L", _signed(today), f"booked {_inr(a['realised_today'], True)} · open {_inr(a['unrealised'], True)}",
                    _cls(today))
             + _kpi("Open P&amp;L", _signed(a["unrealised"]),
                    (f"{rsum:+.2f}R · " if rsum and n else "") + "before exit charges", _cls(a["unrealised"])))
    cells += (_limits_kpis(s, a["equity"], a["unrealised"]) if s.get("mode", "paper") == "paper" else
              _kpi("Realised since start", _signed(a["realised"]), f"charges {_inr(a['charges'])}", _cls(a["realised"]))
              + _kpi("Charges paid", _inr(a["charges"]), "Zerodha charges, simulated"))
    warnings = list(a.get("warnings", []))
    lag = s.get("data_lag_min")
    if s.get("mode") == "paper" and lag is not None and lag > 20:
        warnings.append(f"Prices are about {lag:.0f} min behind the market "
                        f"({'Kite' if (s.get('data_source') or {}).get('using') == 'kite' else 'Yahoo'} data).")
    warn = "".join(f"<div class='warn small warnline'>⚠ {html.escape(w)}</div>" for w in warnings)
    note = (f"<div class='muted small warnline'>Paper account (virtual money) · from the bot's last round at "
            f"{_clock(a.get('as_of'))} IST · realised since start {_inr(a['realised'], True)} · charges {_inr(a['charges'])} · "
            f"available = equity − invested</div>")
    return f"<section class='card acct paper kwrap' id='acct'><div class='kgrid'>{cells}</div>{warn}{note}</section>"


def today_equity(mode):
    """Today's equity samples from the daily journal: [(HH:MM:SS, equity)], or []."""
    import datetime as dt
    import accounts
    import journal
    d = journal.load_detail(mode, dt.datetime.now(accounts.IST).date().isoformat()) or {}
    return [(e[0], float(e[1])) for e in d.get("equity", []) if len(e) >= 2], d.get("start_equity")


def live_equity(view, s=None):
    mode = "live" if view == "zerodha" else "paper"
    pts, start = today_equity(mode)
    if len(pts) < 2 and (s or {}).get("mode", "paper") != mode:
        return ""   # the bot isn't trading this account: nothing to chart
    import charts
    import chartjs
    body = (chartjs.container({"type": "line", "pts": [[t[:5], round(v, 2)] for t, v in pts],
                               "base": start if start is not None else pts[0][1]},
                              charts.intraday(pts, start), "Equity during the day")
            if len(pts) >= 2 else
            webui.empty(T.ui("empty_equity")))
    return (f"<section class='card flush' id='equity'><div class='hd'>Today's equity "
            f"<span class='tag'>{html.escape(T.mode_label(mode))}</span></div><div class='bd spark'>{body}</div></section>")


def live_positions(s, view):
    mode = s.get("mode", "paper")
    if view == "zerodha" and mode != "live":
        return ""   # the bot's positions are paper positions: they belong to the Paper view
    if view == "paper" and mode == "live":
        return ""
    cards = "".join(position_row(p) for p in s.get("positions", [])) or webui.empty(T.ui("empty_positions"))
    return (f"<section class='card flush' id='positions'><div class='hd'><span class='theme'>{T.term('positions')}</span> <span class='end'><span class='tag'>held by the bot · "
            f"{html.escape(T.mode_label(mode))}</span></span></div>{cards}</section>")


def zerodha_positions():
    import accounts
    a = accounts.zerodha_account()
    if a.get("error"):
        return ""
    rows = "".join(
        f"<tr><td class='first' data-label='Position'><b>{_fmt(r['symbol'])}</b> <span class='muted'>{_fmt(r['exchange'])}</span></td>"
        f"<td data-label='Product'>{_fmt(r['product'])}</td>"
        f"<td data-label='Qty' class='r {'up' if r['qty'] > 0 else 'dn' if r['qty'] < 0 else 'muted'}'>{r['qty'] if r['qty'] else 'closed'}</td>"
        f"<td data-label='Avg price' class='r'>{_fmt(r['avg'])}</td><td data-label='Last price' class='r'>{_fmt(r['price'])}</td>"
        f"<td data-label='Realised' class='r'>{_pl(r['realised'])}</td><td data-label='Unrealised' class='r'>{_pl(r['unrealised'])}</td></tr>"
        for r in a.get("positions", [])) or "<tr><td colspan=7 class='muted'>No positions today</td></tr>"
    return (f"<section class='card flush' id='zpositions'><div class='hd'>Zerodha positions today "
            f"<span class='end'><span class='tag dn'>real money · read-only</span></span></div>"
            f"<table class='rt'><tr><th>Position</th><th>Product</th><th class='r'>Qty</th><th class='r'>Avg price</th>"
            f"<th class='r'>Last price</th><th class='r'>Realised</th><th class='r'>Unrealised</th></tr>{rows}</table></section>")


def too_dear(s):
    """(per-stock limit in rupees, [stocks whose single share costs more than it]) for the bot's own account."""
    eq = s.get("equity")
    if not eq:
        return 0.0, []
    limit = float(eq) * C.MAX_POSITION_PCT / 100
    return limit, sorted(sym for sym, r in (s.get("rows") or {}).items()
                         if isinstance(r.get("close"), (int, float)) and r["close"] > limit)


def live_signals(s, now=None):
    """Why the bot has or hasn't traded today: every signal so far and what happened to it (from the bot's own
    journal for the account it trades), what the watchlist is waiting for, and which stocks this account
    can't buy at all. Read-only."""
    import datetime as dt
    import journal
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    mode = "live" if s.get("mode") == "live" else "paper"
    detail = journal.load_detail(mode, now.date().isoformat()) or {}
    sigs = detail.get("signals") or []
    taken = [x for x in sigs if x.get("outcome") == "taken"]
    rows = "".join(
        f"<tr><td class='first' data-label='Time'>{html.escape(str(x.get('bar') or x.get('time') or '-'))}</td>"
        f"<td data-label='Signal'><b>{html.escape(str(x.get('symbol')))}</b> {html.escape(str(x.get('direction') or ''))}</td>"
        f"<td data-label='What happened'><span class='st {'sig' if x.get('outcome') == 'taken' else 'dn'}'>"
        f"{'traded' if x.get('outcome') == 'taken' else 'skipped'}</span></td>"
        f"<td data-label='Why' style='white-space:normal'>{html.escape(str(x.get('reason') or '-'))}</td></tr>"
        for x in sigs)
    waiting = {}
    for r in (s.get("rows") or {}).values():
        full = r.get("reason") or ""
        why = full.split(" - skip")[0]
        key = ("signal skipped (see above)" if " - skip" in full else
               "volume too low" if why.startswith("volume") else "range too wide or narrow" if why.startswith("range")
               else why or "no data yet")
        waiting[key] = waiting.get(key, 0) + 1
    wait_txt = " · ".join(f"{n} {html.escape(k)}" for k, n in sorted(waiting.items(), key=lambda kv: -kv[1]))
    limit, dear = too_dear(s)
    dear_txt = (f"<div class='warn small warnline'>⚠ With {_inr(s.get('equity'))} in the account, one share of "
                f"<b>{html.escape(', '.join(dear))}</b> costs more than the {C.MAX_POSITION_PCT:g}% per-stock limit "
                f"({_inr(limit)}), so their signals are skipped. That limit is a risk rule, not a fault.</div>"
                if dear else "")
    if sigs:
        head = f"{len(sigs)} signal{'s' if len(sigs) != 1 else ''} · {len(taken)} traded · {len(sigs) - len(taken)} skipped"
        table = (f"<table class='rt'><tr><th>Candle</th><th>Signal</th><th>What happened</th><th>Why</th></tr>"
                 f"{rows}</table>")
    else:
        head = "no signal yet"
        table = ("<div class='muted' style='padding:10px 14px'>No breakout signal so far today. A signal needs a "
                 "15-minute candle to close outside the opening range, with enough volume, before "
                 f"{html.escape(str(C.LAST_ENTRY))}.</div>")
    now_line = (f"<div class='muted small warnline'>Watchlist right now: {wait_txt}</div>" if wait_txt else "")
    return (f"<section class='card flush' id='signals'><div class='hd'>Today's signals <span class='end'>"
            f"<span class='tag'>{html.escape(head)}</span></span></div>{table}{now_line}{dear_txt}</section>")


CANDLE_PICKUP_MIN = 1    # the bot looks every POLL_SEC (60 s) and Kite has a closed candle within seconds
CANDLE_LATE_MIN = 4      # this many minutes past the expected time, the page says "No updates"


def watch_timing(s, now=None):
    """When the watchlist last changed and when it should change next. It changes once per 15-minute candle:
    the 09:15 candle closes at 09:30, so the first update is about 09:31, then about every 15 minutes.
    Returns {state: before_open|waiting|late|closed|stopped, last, text, key}; `key` names the overdue
    update, so a page shows one "No updates" popup per missed candle, not one per refresh."""
    import datetime as dt
    import holidays
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    tz = now.tzinfo
    step = dt.timedelta(minutes=C.INTERVAL_MIN)
    pick = dt.timedelta(minutes=CANDLE_PICKUP_MIN)

    def at(day, hhmm):
        h, m = map(int, hhmm.split(":"))
        return dt.datetime.combine(day, dt.time(h, m), tz)

    day = now.date()
    open_t, close_t = at(day, C.MARKET_OPEN), at(day, C.MARKET_CLOSE)
    try:
        status_at = dt.datetime.fromisoformat(s["time"])
        status_day = status_at.date()
        running = (now - status_at).total_seconds() < max(180, 3 * C.POLL_SEC)   # same rule as bot_running()
    except (KeyError, ValueError, TypeError):
        status_day, running = None, False
    bars = sorted(r.get("bar") for r in (s.get("rows") or {}).values() if r.get("bar"))
    last = bars[-1] if bars and status_day == day else None          # 'HH:MM' strings sort in time order
    out = {"last": last, "key": ""}
    if not holidays.is_trading_day(day) or now >= close_t:
        nd = holidays.next_trading_day(day)
        first = at(nd, C.MARKET_OPEN) + step + pick
        out.update(state="closed", text=f"Market closed. Next update about {first:%H:%M} on {first:%a %d %b}, "
                                        f"when the first 15-minute candle closes.")
        return out
    if now < open_t:
        first = open_t + step + pick
        out.update(state="before_open", text=f"Market opens at {open_t:%H:%M}. First update about {first:%H:%M}, "
                                             f"when the {open_t:%H:%M} candle closes.")
        return out
    if not running:
        out.update(state="stopped", key=f"{day}-stopped",
                   text="No updates: the bot is not running, so the watchlist isn't changing. "
                        "Start it (2-START-BOT.bat) to resume.")
        return out
    nxt = (at(day, last) + step) if last else open_t                  # the candle we are waiting for
    closes = nxt + step
    expected = closes + pick
    mins = max(0, int((expected - now).total_seconds() // 60))
    if now <= expected + dt.timedelta(minutes=CANDLE_LATE_MIN):
        when = "in under a minute" if mins == 0 else f"in {mins} min"
        lead = f"Last update: the {last} candle. " if last else "No candles yet today. "
        out.update(state="waiting", text=f"{lead}Next update about {expected:%H:%M} ({when}), "
                                         f"when the {nxt:%H:%M} candle closes at {closes:%H:%M}.")
        return out
    late = int((now - expected).total_seconds() // 60)
    ds = (s.get("data_source") or {}).get("using") or "price data"
    out.update(state="late", key=f"{day}-{nxt:%H%M}",
               text=f"No updates: the {nxt:%H:%M} candle closed at {closes:%H:%M} but hasn't reached the bot "
                    f"({late} min late). The bot retries every minute; check the internet and {ds.capitalize()} data.")
    return out


def live_watchlist(s, now=None):
    rows = []
    for sym, r in sorted(s.get("rows", {}).items()):
        reason = r.get("reason") or ""
        low = reason.lower()
        signal = ("long breakout" in low or "short breakout" in low) and "skip" not in low
        cls = ("sig" if signal else "dn" if low.startswith("data error") or "skip" in low else
               "in" if "inside" in low else "")
        rows.append(
            f"<tr><td class='first' data-label='Stock'><b>{_fmt(sym)}</b></td>"
            f"<td data-label='Close' class='r'>{_fmt(r.get('close'))}</td>"
            f"<td data-label='Opening range' class='r hide-s'>{_fmt(r.get('or_low'))} – {_fmt(r.get('or_high'))}</td>"
            f"<td data-label='VWAP' class='r hide-s'>{_fmt(r.get('vwap'))}</td>"
            f"<td data-label='Vol ×' class='r hide-s'>{_fmt(r.get('vol_ratio'), 1)}</td>"
            f"<td data-label='Bar' class='hide-s'>{_fmt(r.get('bar'))}</td>"
            f"<td data-label='Status' style='white-space:normal'><span class='st {cls}'>{_fmt(T.decorate(reason) or None)}</span></td></tr>")
    body = "".join(rows) or "<tr><td colspan=7 class='muted'>Waiting for market data</td></tr>"
    w = watch_timing(s, now)
    tag = f"last candle {w['last']}" if w["last"] else "last closed 15-min bar"
    late = f" data-late='{html.escape(w['key'])}'" if w["state"] in ("late", "stopped") else ""
    bar = (f"<div class='wtime {w['state']}' role='status'{late}>"
           f"{'⚠ ' if w['state'] in ('late', 'stopped') else '⏱ '}{html.escape(w['text'])}</div>")
    return (f"<section class='card flush' id='watchlist'><div class='hd'><span class='theme'>{T.term('watchlist')}</span> <span class='end'><span class='tag'>{html.escape(tag)}</span></span></div>"
            f"{bar}<table><tr><th>Stock</th><th class='r'>Close</th><th class='r hide-s'>Opening range</th><th class='r hide-s'>VWAP</th>"
            f"<th class='r hide-s'>Vol ×</th><th class='hide-s'>Bar</th><th>Status</th></tr>{body}</table></section>")


def live_events(s):
    items = []
    for e in s.get("events", []):
        t, _, msg = e.partition(" ")
        items.append(f"<li><time>{html.escape(t)}</time><span>{html.escape(msg)}</span></li>")
    body = "".join(items) or "<li class='muted'>No events yet</li>"
    return (f"<details class='card' id='events' open><summary><span class='theme'>{T.term('events')}</span>"
            f"</summary><ul class='list'>{body}</ul></details>")


def balance_notice(s):
    """REAL MONEY: the Zerodha balance the bot sizes trades from, or why it opens no new trades."""
    b = (s or {}).get("balance") if (s or {}).get("mode") == "live" else None
    if not b:
        return ""
    if b.get("ok") and b.get("inr") is not None:
        at = (b.get("at") or "")[11:16]
        return (f"<div class='banner' role='status'>Balance from Zerodha: <b>₹{b['inr']:,.2f}</b>"
                f"{' (read ' + html.escape(at) + ')' if at else ''}. Trades are sized from it.</div>")
    why = b.get("problem") or "Zerodha balance not read yet"
    return (f"<div class='banner' role='alert'><b>REAL MONEY: no new trades.</b> {html.escape(why)}. "
            f"Open positions are still managed.</div>")


def watchdog_notices():
    """Today's crashes and automatic restarts for the popups and the Live page. Never raises."""
    try:
        import watchdog
        return watchdog.notices()
    except Exception:  # noqa: BLE001 - a problem reading the record must not break a page
        return []


def watchdog_banner():
    """What the watchdog did today, newest first (at most 4 lines): shown on the Live page all day."""
    items = [n for n in watchdog_notices() if n.get("tone") in ("dn", "warn")][-4:][::-1]
    if not items:
        return ""
    rows = "".join(f"<li><b>{html.escape(n['at'][11:16])}</b> {html.escape(n['title'])}: "
                   f"{html.escape(n['body'])}</li>" for n in items)
    bad = any(n["tone"] == "dn" for n in items)
    return (f"<div class='banner wd' role='status' style='border-color:var(--{'dn' if bad else 'warn'})'>"
            f"<b>Watchdog today</b><ul style='margin:4px 0 0 18px;padding:0'>{rows}</ul></div>")


def live_notice(s, running):
    if running:
        return watchdog_banner() + balance_notice(s)
    when = (s.get("time") or "")
    return (watchdog_banner() +
            f"<div class='banner'><b>Bot is not running</b> ({T.name('not_running')}: markets closed, weekend, or stopped). "
            f"Showing the last saved state{' from ' + html.escape(when[:16].replace('T', ' ')) if when else ''}. "
            f"See <a href='/review'>{T.term('review')}</a> for how the last trading day played out, "
            f"or <a href='/trades'>{T.term('trades')}</a> for history.</div>")


def live_controls(s, running):
    """Pause/Resume and Stop (both ask first). Shown only while the bot runs."""
    if not running:
        return ""
    paused = s.get("paused")
    ask = "Resume new entries?" if paused else "Pause new entries? Open positions keep being managed."
    label = "▶ Resume new entries" if paused else f"⏸ {T.name('pause')} · Pause new entries"
    return (f"<section class='card'><div class='hd'><span class='theme'>{T.term('controls')}</span></div>"
            f"<form method='post' action='/{'resume' if paused else 'pause'}' style='display:inline' "
            f"onsubmit=\"return confirm('{ask}')\">{csrf_field()}<button class='ghost'>{label}</button></form>"
            f"<form method='post' action='/stop' style='display:inline' "
            f"onsubmit=\"return confirm('Stop the bot? In live mode open positions keep their exchange stop-loss.')\">"
            f"{csrf_field()}<button class='red'>■ Stop bot</button></form>"
            f"<div class='muted small'>Both ask before acting. Open positions keep their stop.</div></section>")


CANDLE_AGE_TIP = ("Prices arrive as 15-minute candles. The newest finished candle is normally up to 16 minutes "
                  "old: that is its age, not a delay in the data.")


def candle_age_text(s, now=None):
    """'finished 9 min ago · on time' / '... · LATE: ...' for the Data & limits card."""
    lag = s.get("data_lag_min")
    if lag is None:
        return "-"
    w = watch_timing(s, now)
    if w["state"] == "late" or lag > 20:
        return (f"finished {lag:.0f} min ago · <span class='warn'>LATE: a newer candle should have arrived</span>")
    return (f"finished {lag:.0f} min ago · <span class='up'>on time</span> "
            f"<span class='muted'>(a new candle every {C.INTERVAL_MIN} min; up to {C.INTERVAL_MIN + 1} min old is normal)</span>")


def feed_line(s):
    """One line on the price feed's health for the Data & limits card (the bot records it each round)."""
    f = s.get("feed") or {}
    if not f:
        return "-"
    src = "Kite" if f.get("source") == "kite" else (str(f.get("source") or "none").capitalize())
    pick = f.get("pickup_sec")
    got = f" · picked up {pick:.0f} s after it closed" if isinstance(pick, (int, float)) else ""
    bad = len(f.get("problems") or {}) + len(f.get("errors") or [])
    health = (f"<span class='up'>{f.get('stocks_ok', 0)} of {f.get('stocks', 0)} stocks OK</span>" if not bad else
              f"<span class='warn'>{f.get('stocks_ok', 0)} of {f.get('stocks', 0)} stocks OK · {bad} with a problem</span>")
    block = (" · <b class='dn'>real-money entries blocked: not on Kite prices</b>" if f.get("live_needs_kite") else "")
    return f"{html.escape(src)} · candle {html.escape(str(f.get('newest_candle') or '-'))}{got} · {health}{block}"


def live_limits(s):
    cooldown = s.get("cooldown_until")
    lag_txt = candle_age_text(s)
    until = f" · <span class='warn'>until {html.escape(str(cooldown))}</span>" if cooldown else ""
    return (f"<details class='card' id='limits'><summary>Data &amp; limits</summary><dl class='facts'>"
            f"<dt>Loss streak</dt><dd>{_fmt(s.get('loss_streak'))} <span class='muted'>· {T.name('cooldown')} after "
            f"{C.LOSS_STREAK_LIMIT}</span>{until}</dd>"
            f"<dt>Price data</dt><dd>{data_source_text(s)}</dd>"
            f"<dt>Newest candle</dt><dd>{lag_txt}</dd>"
            f"<dt>Price feed</dt><dd>{feed_line(s)}</dd>"
            f"<dt>Per trade</dt><dd>{C.RISK_PER_TRADE_PCT:g}% risk · at most {C.MAX_OPEN_POSITIONS} positions, "
            f"{C.MAX_TRADES_PER_DAY} trades a day</dd>"
            f"<dt>{T.name('daily_loss')}</dt><dd>stop new trades at −{C.MAX_DAILY_LOSS_PCT:g}% in a day (booked + open)</dd>"
            f"<dt>Exits</dt><dd>{T.term('stop')} · {T.term('trail')} · {T.term('time_stop')} · "
            f"{T.term('square_off')} at {C.SQUARE_OFF}</dd>"
            f"</dl><div class='small' style='padding:0 14px 12px'><a href='/kavach'>Kavach · every risk check "
            f"and limit →</a> · <a href='/chanakya'>Chanakya · what speaks for and against each stock →</a> · "
            f"<a href='/rajya'>Rajya · the whole account, measured →</a> · "
            f"<a href='/yantrashala'>Yantrashala · strategy lab →</a></div></details>")


def live_positions_held(s):
    """Real-money positions the RUNNING bot holds (0 in paper mode or when the bot isn't running)."""
    return len(s.get("positions") or []) if (s.get("mode") == "live" and bot_running(s)) else 0


def logout_form(s):
    """'Log out of Zerodha' (asks first). Disabled while the bot holds real-money positions: it needs the login
    to manage and close them."""
    held = live_positions_held(s or {})
    if held:
        return (f"<div class='logout'><button type='button' class='red wide' disabled aria-disabled='true'>"
                f"⏏ Log out of Zerodha</button><div class='muted small'>Not available now: the bot holds {held} "
                f"real-money position(s) and needs the login to manage and close them. Log out after they close."
                f"</div></div>")
    live_note = ("\\n\\nThe bot is trading REAL MONEY: new entries will be paused until you log in again."
                 if (s or {}).get("mode") == "live" and bot_running(s or {}) else "")
    ask = ("Log out of Zerodha?\\n\\nThis ends today\\'s Zerodha session for this dashboard and the bot, and deletes "
           "the login saved on this PC. Price data switches to Yahoo until you log in again." + live_note)
    return (f"<form method='post' action='/zerodha/logout' class='logout' "
            f"onsubmit=\"if(!confirm('{ask}'))return false;this.confirm.value='yes'\">"
            f"<input type='hidden' name='confirm' value=''>{csrf_field()}"
            f"<button class='red wide'>⏏ Log out of Zerodha</button></form>")


def zerodha_logout():
    """Log out of Zerodha from the dashboard: end the session at Zerodha (when it can be reached) and delete the
    login saved on this PC (.kite_token.json). Refused while the bot holds real-money positions. If the bot trades
    real money (and is flat), new entries are paused too. The token is never shown or logged."""
    s = read_status()
    held = live_positions_held(s)
    if held:
        return (f"Not logged out: the bot holds {held} real-money position(s) and needs the Zerodha login to "
                "manage and close them. Log out after they close.")
    try:
        kp = _kite_proxy()
    except SystemExit:
        return "Nothing to log out: the Kite settings are missing on this PC."
    path = kp.TOKEN_FILE
    if not path.exists():
        return "You are not logged in to Zerodha on this PC."
    tok = kp.load_token()           # None if the saved login already expired (06:00)
    ended, why = False, ""
    if tok:
        try:
            kp.make_kite(tok).invalidate_access_token(tok)
            ended = True
        except (Exception, SystemExit) as e:  # network, proxy or Kite error: still delete the saved login
            why = type(e).__name__
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        return (f"Logout incomplete: the saved login on this PC could not be deleted ({type(e).__name__}). "
                f"Close other programs using it and try again.")
    live = s.get("mode") == "live" and bot_running(s)
    if live:
        os.makedirs(C.DATA_DIR, exist_ok=True)
        open(C.PAUSE_FILE, "w").close()
    log.info("Zerodha: logged out from the dashboard (session ended at Zerodha: %s)", "yes" if ended else "no")
    if ended:
        msg = "Logged out of Zerodha: Zerodha ended the session and the login saved on this PC is deleted."
    elif tok:
        msg = (f"Logged out on this PC: the saved login is deleted. Zerodha could not be reached ({why}), so the "
               "session there ends by itself at about 06:00.")
    else:
        msg = "Logged out: the saved login had already expired and is now deleted."
    if live:
        msg += " The bot trades real money, so new entries are paused until you log in again."
    return msg + " Price data uses Yahoo until you log in again."


def live_login(ok, login_msg, s=None):
    url = login_url()
    login_btn = (f"<a class='btn' href='{html.escape(url)}'>Log in to Zerodha</a>" if url and not ok else "")
    logout = logout_form(s) if ok else ""
    return (f"<section class='card'><div class='hd'><span class='theme'>{T.term('login')}</span></div>"
            f"<div class='{'up' if ok else 'warn'}'>{html.escape(login_msg)}</div>{login_btn}{logout}"
            f"<div class='muted small'>{T.term('paper')} mode doesn't need a login, except for {T.name('kite_data')} (Kite) "
            f"price data. {T.term('live')} mode needs one every day (tokens expire ~06:00 IST).</div></section>")


# ---------------------------------------------------------------- Paper | Real Money (livemode.py)
_READY = {"at": None, "checks": None}     # last readiness check (it calls Zerodha, so only when asked)


def _short_err(e):
    """An error's kind only: messages can carry a proxy address or account details."""
    return f"could not check ({type(e).__name__})"


def readiness_probe():
    """Read-only answers for livemode.readiness(): saved Windows settings, the running bot's own settings, today's
    login, Zerodha profile and funds (read-only calls), and that Kite traffic leaves from the whitelisted IP. Never
    places an order and never shows a token, an address or a password."""
    import livemode
    s = read_status()
    probe = {"settings": {n: livemode.windows_setting(n) for n in
                          ("DRY_RUN", "CONFIRM_LIVE", "KITE_ACCOUNT_ACTIVE", "KITE_ORDER_PERMISSION")},
             "bot_env_live": bool(s.get("live_env")) and bot_running(s)}
    try:
        kp = _kite_proxy()
    except (Exception, SystemExit) as e:
        probe.update(account_err=_short_err(e), egress_err="Kite settings missing: run setup_kite_proxy.ps1",
                     funds_err=_short_err(e))
        return probe
    try:
        import requests
        egress = requests.get("https://api64.ipify.org", proxies=kp.proxies(), timeout=10).text.strip()
        want = kp.env("EXPECTED_EGRESS_IP")
        probe["egress_ok"] = bool(egress) and egress == want
        if not probe["egress_ok"]:
            probe["egress_err"] = "Kite traffic does NOT leave from the whitelisted IP"
    except (Exception, SystemExit) as e:
        probe["egress_err"] = f"proxy check failed ({type(e).__name__})"
    tok = None
    try:
        tok = kp.load_token()
    except (Exception, SystemExit):
        pass
    probe["logged_in"] = bool(tok)
    if not tok:
        probe.update(account_err="log in to Zerodha first", funds_err="log in to Zerodha first")
        return probe
    try:
        kite = kp.make_kite(tok)
    except (Exception, SystemExit) as e:
        probe.update(account_err=_short_err(e), funds_err=_short_err(e))
        return probe
    try:
        kite.profile()
        probe["account_ok"] = True
    except (Exception, SystemExit) as e:
        probe["account_err"] = f"Zerodha refused ({type(e).__name__})"
    try:
        m = kite.margins("equity") or {}
        bal = (m.get("available") or {}).get("live_balance", m.get("net"))
        probe["funds"] = float(bal)
    except (Exception, SystemExit) as e:
        probe["funds_err"] = _short_err(e)
    return probe


def check_readiness():
    import livemode
    import datetime as dt
    probe = readiness_probe()
    checks = livemode.readiness(probe)
    _READY.update(at=dt.datetime.now(livemode.IST), checks=checks, funds=probe.get("funds"))
    livemode.log("readiness check", "all passed" if livemode.all_ready(checks) else "LOCKED",
                 ", ".join(n for n, ok, _, _ in checks if not ok))
    return checks


def login_age_min():
    """Minutes since today's Zerodha login (None if not logged in). Reads only the time, never the token."""
    import livemode
    import datetime as dt
    try:
        kp = _kite_proxy()
        if not kp.load_token():
            return None
        created = dt.datetime.fromisoformat(json.loads(kp.TOKEN_FILE.read_text())["created"])
        return (dt.datetime.now(livemode.IST) - created).total_seconds() / 60, created
    except (Exception, SystemExit):
        return None


def arm_real_money(form):
    """Arm REAL MONEY for today. Only with: the typed words, every readiness check passing right now, a Zerodha
    login from the last livemode.LOGIN_FRESH_MIN minutes, and the bot not already trading real money. Writes only
    today's arming file; it can't set DRY_RUN / CONFIRM_LIVE, and the bot still needs both of them."""
    import livemode
    words = (form.get("words") or [""])[0].strip()
    if words != livemode.CONFIRM_WORDS:
        livemode.log("arm real money", "refused", "confirmation words not typed")
        return False, f"Not armed: type {livemode.CONFIRM_WORDS} exactly to confirm."
    s = read_status()
    if s.get("mode") == "live" and bot_running(s):
        return False, "The bot is already trading REAL MONEY."
    age = login_age_min()
    if age is None or age[0] > livemode.LOGIN_FRESH_MIN or age[0] < -1:
        livemode.log("arm real money", "refused", "no Zerodha login in the last 10 minutes")
        return False, (f"Not armed: the switch needs a Zerodha login from the last {livemode.LOGIN_FRESH_MIN} minutes "
                       "(Zerodha's own two-step login). Log out, log in again, then arm.")
    checks = check_readiness()
    if not livemode.all_ready(checks):
        missing = ", ".join(n for n, ok, _, _ in checks if not ok)
        livemode.log("arm real money", "refused", f"locked: {missing}")
        return False, f"Not armed: Live Trading is locked ({missing})."
    livemode.arm(login_at=age[1])
    running = bot_running(s)
    if running and s.get("live_env"):
        return True, ("REAL MONEY armed for today. The bot switches at the next safe moment (no open paper "
                      "position); until then it opens no new paper trades.")
    if running:
        return True, ("REAL MONEY armed for today, but the running bot was started without DRY_RUN=false and "
                      "CONFIRM_LIVE=YES, so it stays in PAPER. Real money starts only if it starts again today "
                      "with both settings.")
    return True, "REAL MONEY armed for today: the next start today trades real money. The arming ends at midnight."


def switch_to_paper():
    """Back to paper: the arming goes at once; a live bot opens no new trades and switches when its positions
    have closed (it keeps managing them)."""
    import livemode
    s = read_status()
    livemode.request_paper()
    if s.get("mode") == "live" and bot_running(s):
        n = live_positions_held(s)
        if n:
            return (f"Switching to PAPER: no new real-money trades from now. The bot keeps managing its {n} open "
                    f"position(s) (stop orders, trailing stop, square-off close) and switches when they have closed.")
        return "Switching to PAPER: the bot restarts in paper within a minute."
    return "Real money is not armed any more: the bot trades PAPER."


def close_all_positions(form):
    import livemode
    if (form.get("confirm") or [""])[0] != "yes":
        return "Nothing closed: closing all positions needs your confirmation."
    s = read_status()
    if not bot_running(s):
        return "Nothing closed: the bot is not running."
    n = len(s.get("positions") or [])
    if not n:
        return "There are no open positions to close."
    livemode.request_close_all()
    kind = "real-money" if s.get("mode") == "live" else "paper"
    return f"Closing all {n} {kind} position(s) at the market: the bot does it at its next check (within a minute)."


def trading_mode_card(s):
    """Settings > Trading mode: Paper | Real Money. Shows what the bot actually trades, the audit warning, the
    readiness checks (Live Trading Locked when any fails), the arming form, and Switch back to Paper."""
    import livemode
    s = s or {}
    running = bot_running(s)
    live = s.get("mode") == "live" and running
    armed = livemode.arm_valid()[0]
    out = [f"<section class='card' id='mode'><div class='hd'>Trading mode: Paper | Real Money</div>",
           f"<p>Now: {mode_badge(s)}</p>",
           f"<div class='banner' role='note'>⚠ {html.escape(livemode.AUDIT_VERDICT)}</div>"]
    if live or armed:
        sw = livemode.switch_message(s) if running else ""
        what = ("The bot is trading <b>REAL MONEY</b> today." if live else
                "<b>REAL MONEY is armed for today.</b> " + ("The bot switches at the next safe moment."
                                                           if running and s.get("live_env") else
                                                           "It applies only to a start today with both live settings."))
        out.append(f"<p>{what}</p>")
        if sw:
            out.append(f"<p class='warn'>{html.escape(sw)}</p>")
        if s.get("switch") != "to_paper":
            ask = ("Switch back to PAPER?\\n\\nNo new real-money trades from now. Open positions are still managed "
                   "(stop orders, trailing stop, square-off close); the bot switches when they have closed.")
            out.append(f"<form method='post' action='/mode/paper' onsubmit=\"return confirm('{ask}')\">{csrf_field()}"
                       f"<button class='wide'>◐ Switch back to Paper</button></form>")
        n = len(s.get("positions") or []) if live else 0
        if n:
            ask = (f"Close ALL {n} real-money position(s) now at the market price?\\n\\nThis sells/buys at once at "
                   f"whatever the price is. It cannot be undone.")
            out.append(f"<form method='post' action='/positions/close-all' "
                       f"onsubmit=\"if(!confirm('{ask}'))return false;this.confirm.value='yes'\">"
                       f"<input type='hidden' name='confirm' value=''>{csrf_field()}"
                       f"<button class='red wide'>✖ Close all positions now</button></form>"
                       f"<p class='muted small'>Separate from the switch: use it only if you want out at once.</p>")
        out.append("</section>")
        return "".join(out)
    checks, at = _READY["checks"], _READY["at"]
    if checks is None:
        out.append("<p>The bot trades <b>PAPER</b>. Real money stays locked until every check below passes. "
                   "Checking asks Zerodha for your profile and funds (read only: nothing is ever ordered).</p>")
    else:
        rows = "".join(f"<li class='{'up' if ok else 'dn'}'>{'✔' if ok else '✖'} <b>{html.escape(n)}</b>: "
                       f"{html.escape(d)}{'' if ok else '<br><span class=muted>' + html.escape(fix) + '</span>'}</li>"
                       for n, ok, d, fix in checks)
        ready = livemode.all_ready(checks)
        head = ("<p class='up'><b>All checks passed.</b></p>" if ready else
                "<p class='dn'><b>🔒 Live Trading Locked.</b> What's missing and how to fix it:</p>")
        out.append(f"{head}<ul class='checks'>{rows}</ul><p class='muted small'>Checked at {at:%H:%M:%S}.</p>")
    out.append(f"<form method='post' action='/mode/check'>{csrf_field()}<button>Check readiness</button></form>")
    if checks is not None and livemode.all_ready(checks):
        funds = next((d for n, _, d, _ in checks if n == "Enough funds"), "")
        probe_funds = _READY.get("funds")
        risk = (f"Risk per trade: {C.RISK_PER_TRADE_PCT}% of equity" +
                (f" = about ₹{probe_funds * C.RISK_PER_TRADE_PCT / 100:,.0f}" if probe_funds else "") +
                f". Daily loss limit: {C.MAX_DAILY_LOSS_PCT}% of equity" +
                (f" = about ₹{probe_funds * C.MAX_DAILY_LOSS_PCT / 100:,.0f}" if probe_funds else "") +
                f". Up to {C.MAX_TRADES_PER_DAY} trades a day. Funds: {funds}.")
        age = login_age_min()
        fresh = age is not None and age[0] <= livemode.LOGIN_FRESH_MIN
        out.append(f"<div class='banner' role='note'>{html.escape(risk)}</div>")
        if not fresh:
            out.append(f"<p class='warn'>Arming needs a Zerodha login from the last {livemode.LOGIN_FRESH_MIN} minutes. "
                       "Log out of Zerodha and log in again on the Live page, then come back.</p>")
        out.append(f"<form method='post' action='/mode/real'>{csrf_field()}"
                   f"<label>Type <b>{livemode.CONFIRM_WORDS}</b> to arm real money for today only: "
                   f"<input name='words' autocomplete='off' spellcheck='false' required></label> "
                   f"<button class='red'{'' if fresh else ' disabled'}>● Arm REAL MONEY for today</button></form>"
                   f"<p class='muted small'>It takes effect only at a safe moment (no open paper position) and only "
                   f"if the bot runs with DRY_RUN=false and CONFIRM_LIVE=YES. It ends at midnight. Every change is "
                   f"logged in data/logs/mode_changes.log.</p>")
    out.append("</section>")
    return "".join(out)


LIVE_REFRESH_SEC = ACCOUNT_REFRESH_SEC


def live_regions(view, s):
    """The parts of the Live page that change while the bot runs. /api/live sends the same parts and the
    page swaps them in place every few seconds (no reload; the opening-balance form is never touched)."""
    running = bot_running(s)
    ok, login_msg = login_status()
    return {"status": _status_chips(s, running, view, ok, s.get("data_lag_min")),
            "notice": live_notice(s, running), "acct": live_kpis(view, s), "equity": live_equity(view, s),
            "zpositions": zerodha_positions() if view == "zerodha" else "",
            "positions": live_positions(s, view), "signals": live_signals(s), "watchlist": live_watchlist(s),
            "controls": live_controls(s, running), "login": live_login(ok, login_msg, s),
            "events": live_events(s), "limits": live_limits(s), "modebadge": mode_badge(s)}


LIVE_JS = """
(function(){
  var view=document.body.getAttribute('data-view')||'paper', every=SEC*1000, busy=false, failed=false;
  var off=document.getElementById('offline');
  function openState(box){var m={};box.querySelectorAll('details[id]').forEach(function(d){m[d.id]=d.open});return m}
  function restore(box,m){box.querySelectorAll('details[id]').forEach(function(d){if(d.id in m)d.open=m[d.id]})}
  function stamp(){var u=document.getElementById('updated'); if(!u)return;
    u.textContent='Updated '+new Date().toTimeString().slice(0,8)+' · every SEC s'+(failed?' · OFFLINE':'');}
  function tick(){
    if(busy||document.hidden)return; busy=true;
    fetch('/api/live?view='+encodeURIComponent(view),{cache:'no-store'})
     .then(function(r){if(!r.ok)throw new Error(r.status);return r.json()})
     .then(function(d){
        failed=false; if(off)off.hidden=true;
        Object.keys(d.regions||{}).forEach(function(k){
          var box=document.querySelector('[data-region="'+k+'"]'); if(!box)return;
          var h=d.regions[k]; if(box._html===h)return;
          var m=openState(box); box.innerHTML=h; box._html=h; restore(box,m);
        });
        stamp(); document.dispatchEvent(new CustomEvent('regionsupdated'));
     })
     .catch(function(){failed=true; if(off)off.hidden=false; stamp();})
     .then(function(){busy=false});
  }
  document.querySelectorAll('[data-region]').forEach(function(b){b._html=b.innerHTML});
  setInterval(tick,every);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)tick()});
  stamp();
})();
""".replace("SEC", str(LIVE_REFRESH_SEC))


def render(msg="", q=None):
    s = read_status()
    view = current_view(q or {}, s)
    reg = live_regions(view, s)
    capital = capital_card() if view == "paper" else ""

    def R(k):
        return f"<div data-region='{k}'>{reg[k]}</div>"
    note = (f'<div class="banner" style="border-color:var(--up)" role="status">{html.escape(msg)}</div>' if msg else "")
    body = f"""<style>{LIVE_CSS}</style>
<div class="page-head"><h1>{T.BOW} {T.name('live_page')}</h1><span class="sub">{T.plain('live_page')} · updates in place every {LIVE_REFRESH_SEC} s</span></div>
{R('status')}
{view_banner(view, s)}
<div class="banner" id="offline" role="status" hidden>⚠ Can't reach the dashboard server: the figures below may be old. It keeps trying.</div>
{R('notice')}
{note}
{update_banner(s)}
{R('acct')}
<div class="grid" style="margin-top:16px">
<div>{R('equity')}{R('zpositions')}{R('positions')}{R('signals')}{R('watchlist')}</div>
<aside>{R('controls')}{capital}{R('login')}{R('events')}{market_calendar()}{R('limits')}</aside>
</div>
<script>{LIVE_JS}</script>"""
    return page(T.plain("live_page"), "/", body, view=view)


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
                f"style='fill:var(--accent)' opacity='0.18'/>")
    col = "var(--up)" if closes[-1] >= closes[0] else "var(--dn)"
    return (f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' role='img' aria-label='closes, "
            f"{'up' if closes[-1] >= closes[0] else 'down'} {abs(closes[-1] / closes[0] - 1) * 100:.1f}%'>"
            f"{band}<polyline points='{pts}' fill='none' style='stroke:{col}' stroke-width='1.5'/></svg>")


# ---------------------------------------------------------------- background builds (Review, Scanner)
REVIEW_LIMIT_SEC = 300
SCAN_LIMIT_SEC = 300


def _review_job(target, job):
    import review
    res = review.build(target, refresh=True, job=job)
    if not res.get("day"):
        raise RuntimeError(res.get("error") or "no price data")


def _scan_job(target, job):
    import scanner
    res = scanner.scan(job=job)
    if not res.get("rows") and res.get("errors"):
        raise RuntimeError(f"no stock could be downloaded ({len(res['errors'])} errors)")


def _chanakya_job(target, job):
    import chanakya
    res = chanakya.build(job=job)
    if not res.get("rows") and res.get("errors"):
        raise RuntimeError(f"no stock could be downloaded ({len(res['errors'])} errors)")


jobs.register("review", _review_job, REVIEW_LIMIT_SEC)
jobs.register("scanner", _scan_job, SCAN_LIMIT_SEC)
def _rajya_job(target, job):
    import rajya
    rajya.build(target or "paper", job=job)


jobs.register("chanakya", _chanakya_job, REVIEW_LIMIT_SEC)
jobs.register("rajya", _rajya_job, REVIEW_LIMIT_SEC)


def auto_start(name, target=None):
    """Start a background build because the saved result is missing or out of date - but not again within
    AUTO_RETRY_SEC of a failed build (a Retry click always starts one)."""
    j = jobs.get(name)
    if j and j.failed_recently(AUTO_RETRY_SEC):
        return "held"
    return jobs.start(name, target)


AUTO_RETRY_SEC = 300

_JOB_POLL_JS = """<script>(function(){var el=document.querySelector('.jobstrip[data-running]');if(!el)return;
var name=el.getAttribute('data-job'),unit=el.getAttribute('data-unit'),tail=el.getAttribute('data-tail')||'';
function mmss(s){s=Math.floor(s||0);var m=Math.floor(s/60),r=s%60;return m+':'+(r<10?'0':'')+r;}
function tick(){if(document.hidden){setTimeout(tick,3000);return;}
fetch('/api/jobs',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){var s=j[name];
if(!s){return;}if(s.state!=='running'){location.reload();return;}
el.querySelector('.js-text').textContent=s.done+' of '+(s.total||'?')+' '+unit+' \u00b7 '+mmss(s.elapsed_sec)+
(s.step?' \u00b7 '+s.step:'')+tail;setTimeout(tick,2000);}).catch(function(){setTimeout(tick,5000);});}
setTimeout(tick,2000);})();</script>"""


def _hm(iso):
    try:
        import datetime as _dt
        t = _dt.datetime.fromisoformat(iso)
        return t.strftime("%d %b %H:%M") if t.date() != _dt.datetime.now(t.tzinfo).date() else t.strftime("%H:%M")
    except (TypeError, ValueError):
        return "-"


def _ago(iso):
    try:
        import datetime as _dt
        t = _dt.datetime.fromisoformat(iso)
        m = int((_dt.datetime.now(t.tzinfo) - t).total_seconds() // 60)
    except (TypeError, ValueError):
        return ""
    return "just now" if m < 1 else f"{m} min ago" if m < 120 else f"{m // 60} h ago" if m < 48 * 60 else f"{m // 1440} days ago"


def _mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def job_strip(name, result, fresh, refresh_url, unit):
    """The status strip at the top of the Review and Scanner pages: Loading / Running (progress, elapsed),
    Completed (last update), Failed (reason, Retry), Data stale (why, Refresh), Partial (skipped stocks).
    Words and an icon, not colour alone; polls /api/jobs only while a build runs."""
    j = jobs.get(name)
    s = j.snapshot() if j else {"state": "idle"}
    built = (result or {}).get("built")
    saved = f"showing the saved result from {_hm(built)}" if built else ""
    button, running, tail = "", False, ""

    def btn(label):
        return (f"<a class='btn ghost small' href='{html.escape(refresh_url)}' "
                f"aria-label='{label} now'>↻ {label}</a>")
    if s["state"] == "running":
        running = True
        state, icon, cls = ("Running", "⏳", "run") if result else ("Loading", "⏳", "run")
        tail = f" · {saved}" if saved else ""
        text = (f"{s['done']} of {s['total'] or '?'} {unit} · {_mmss(s['elapsed_sec'])}"
                + (f" · {s['step']}" if s.get("step") else "") + tail)
        button = (f"<form method='post' action='/jobs/cancel' style='margin:0'>"
                  f"<input type='hidden' name='job' value='{html.escape(name)}'>{csrf_field()}"
                  f"<button class='ghost small' aria-label='Cancel this build'>✕ Cancel</button></form>")
    elif s["state"] == "cancelled":
        state, icon, cls = "Cancelled", "⚠", "warn"
        text = "the build was cancelled; nothing of it was saved" + (f" · {saved}" if saved else "")
        button = btn("Refresh")
    elif s["state"] == "failed":
        state, icon, cls = "Failed", "✕", "bad"
        text = f"the last update failed: {s['error']}" + (f" · {saved}" if saved else "")
        button = btn("Retry")
    elif not result:
        state, icon, cls = "No data yet", "⚠", "warn"
        text = "nothing saved yet"
        button = btn("Refresh")
    elif fresh.get("stale"):
        state, icon, cls = "Data stale", "⚠", "warn"
        text = f"{fresh.get('why') or 'out of date'} · {saved}"
        button = btn("Refresh")
    elif result.get("partial"):
        sk = result["partial"].get("skipped", [])
        state, icon, cls = "Partial", "⚠", "warn"
        text = (f"{len(sk)} stock(s) skipped at the time limit ({html.escape(', '.join(sk[:6]))}"
                f"{'…' if len(sk) > 6 else ''}) · updated {_hm(built)}, {_ago(built)}")
        button = btn("Retry")
    else:
        state, icon, cls = "Completed", "✓", "ok"
        took = (f" · took {_mmss(s['elapsed_sec'])} for {s['done']} {unit}"
                if s.get("elapsed_sec") and s.get("finished") else "")
        text = f"updated {_hm(built)}, {_ago(built)}{took}"
    data = (f" data-running='1' data-job='{name}' data-unit='{html.escape(unit)}' data-tail='{html.escape(tail)}'"
            if running else "")
    return (f"<div class='jobstrip {cls}' role='status' aria-live='polite'{data}>"
            f"<span class='js-state'><span aria-hidden='true'>{icon}</span> <b>{state}</b></span>"
            f"<span class='js-text'>{html.escape(text) if state != 'Partial' else text}</span>{button}</div>"
            + (_JOB_POLL_JS if running else ""))


def render_review(day=None, refresh=False):
    """Draws straight away from the saved Review; builds in the background when it is missing,
    out of date or a refresh was asked for. Never waits for price downloads."""
    import review
    h1 = f"<h1>{T.LAMP} {T.term('review')}</h1>"
    r = review.cached(day) if day else review.latest()
    if r and not r.get("day"):
        r = None
    fresh = review.status(r, latest=not day)
    if refresh:
        jobs.start("review", day)
    elif fresh["stale"]:
        auto_start("review", day)
    refresh_url = "/review?" + (f"day={day}&" if day else "") + "refresh=1"
    note = job_strip("review", r, fresh, refresh_url, "steps")
    if not r:
        return page("Session review", "/review", f"{h1}{note}<p class='muted'>The review appears here when the "
                    "build has finished. The page updates by itself.</p>")
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
            if tm.get("error"):
                outcome += (f"<br><span class='warn'>⚠ {T.term('time_machine')}: no 5-min data "
                            f"({html.escape(str(tm['error']))})</span>")
            if tm.get("history_text"):
                outcome += f"<br><span class='muted'>📜 {html.escape(tm['history_text'])}</span>"
            r_cell = f"<b class='{'up' if res > 0 else 'dn'}'>{res:+.2f}R</b>"
        else:
            outcome = f"<span class='muted'>{html.escape(x.get('note') or '-')}</span>"
            r_cell = "-"
        rows.append(f"<tr><td class='first' data-label='Stock'><b>{html.escape(sym)}</b></td>"
                    f"<td data-label='Day'>{sparkline(x.get('closes', []), x.get('or_high'), x.get('or_low'))}</td>"
                    f"<td data-label='Opening range' class='r'>{_fmt(x.get('or_low'))} – {_fmt(x.get('or_high'))}</td>"
                    f"<td data-label='Day %' class='r {'up' if (chg or 0) >= 0 else 'dn'}'>"
                    f"{'-' if chg is None else ('▲ ' if chg >= 0 else '▼ ') + f'{chg:+.2f}%'}</td>"
                    f"<td data-label='Signal → outcome' class='block' style='white-space:normal;text-align:left'>{outcome}</td>"
                    f"<td data-label='Result' class='r'>{r_cell}</td></tr>")
    progress_note = (f"<div class='banner' role='note'><b>Session in progress</b>: as of "
                     f"{html.escape(str(fresh['as_of'] or '-'))}, updated after each 15-minute candle. "
                     "Not final until the market closes.</div>" if fresh["in_progress"] else "")
    st = r.get("settings", {})
    settings = " · ".join(f"{k}={v}" for k, v in st.items())
    total = r.get("total_r", 0.0)
    built = html.escape(str(r.get('built', ''))[:16].replace('T', ' '))
    cap, cap_label = sizing_capital()
    at_risk = total * cap * C.RISK_PER_TRADE_PCT / 100
    body = f"""
<div class="page-head"><h1>{T.LAMP} {T.term('review')} <span class="tag">{r['day']}</span></h1>
<span class="sub">The day replayed through the strategy rules, one stock at a time (portfolio limits not applied) ·
built {built} from 15-min data</span></div>{note}
<div class="chips"><span class="muted small">Day:</span><span class="days">{days}</span>
<form method="get" action="/review" style="display:inline;margin:0"><input type="hidden" name="day" value="{r['day']}">
<input type="hidden" name="refresh" value="1"><button class="ghost small">↻ Refresh</button></form></div>
<div class="kgrid k4">{_kpi("Signals", f"{r.get('signals', 0)}")}{_kpi("Winners", f"{r.get('wins', 0)}")}
{_kpi("Total", f"{total:+.2f}R", "after slippage and charges", _cls(total))}
{_kpi(f"At {C.RISK_PER_TRADE_PCT:g}% risk on {_inr(cap)}", _signed(at_risk),
      f"about · {html.escape(cap_label)} · less when a position limit applies", _cls(at_risk))}</div>{progress_note}
{review_funnel(r)}
<section class="card flush"><div class="hd">Stocks <span class="end"><span class="tag">chart = 15-min closes ·
{T.ui('band_colour')} band = opening range</span></span></div><table class="rt">
<tr><th>Stock</th><th>Day</th><th class="r">Opening range</th><th class="r">Day %</th><th>Signal → outcome</th>
<th class="r">Result</th></tr>{''.join(rows)}</table></section>
<details class="card"><summary>How to read this page</summary><div class="small muted">Rules: {html.escape(settings)}.
R = profit or loss as a multiple of the risk (entry to stop), after 0.05% slippage and Zerodha charges.<br>
{T.SEER} {T.term('time_machine')}: the trade replayed 10,000 times using this stock's own 5-minute moves from the same
time of day (±10 min) over the last 60 days, shuffled. It assumes no edge, so it's a baseline: a good setup
should beat these odds. The replays also inherit the stock's recent intraday drift.
The verdict compares target-first vs stop-first at 1R with break-even (50%): within 5 points is a coin flip.<br>
{T.SCROLL} History is the real record: the bot's same rules (same direction) run on each earlier session.</div></details>"""
    return page("Session review", "/review", body)


def review_funnel(r):
    """One line under the Review's figures: of N stocks, how many gave a signal and why the others did not
    (taken from each stock's own note), so 'only 3 signals' explains itself."""
    rows = r.get("rows") or {}
    if not rows:
        return ""
    groups, signals = {}, 0
    for x in rows.values():
        if x.get("signal_time") or x.get("direction"):
            signals += 1
            continue
        note = (x.get("note") or "").strip()
        why = note.split(" ", 1)[1] if note[:2].isdigit() and " " in note else note     # drop the leading HH:MM
        low = why.lower()
        key = ("volume too low" if low.startswith("volume") else
               "opening range too wide or too narrow" if low.startswith("range") else
               "breakout came after the last entry time" if "after" in low else
               "never closed outside the opening range" if "inside" in low or not low else
               "price on the wrong side of VWAP" if "vwap" in low else
               "index moving the other way" if "index" in low or "nifty" in low else why)
        groups[key] = groups.get(key, 0) + 1
    parts = [f"<b>{signals}</b> gave a signal"] + [f"{n} {html.escape(k)}" for k, n in
                                                     sorted(groups.items(), key=lambda kv: -kv[1])]
    return (f"<div class='muted small' role='note' style='margin:0 0 14px'>Of {len(rows)} stocks: "
            f"{' · '.join(parts)}. Each stock's own reason is in the table below.</div>")


KAVACH_CHECKS = (
    ("time", "Not after the square-off time."),
    ("paused", "Not while Pause, an Apply-Now restart or a Paper | Real Money switch is waiting."),
    ("balance", "Real money: today's Zerodha balance has been read."),
    ("data", "Real money trades only on Kite prices, never delayed Yahoo. The stock's candles passed validation, "
             "the signal is not stale, and the index filter could be checked."),
    ("account", "Cool-down after losses, daily loss limit (booked + open), trades a day, open positions, and one "
                "trade per stock a day: so no averaging down and no second entry."),
    ("price", "The price has not run past the signal, and is not already at the stop."),
    ("sector", "Not too many open positions in one sector and direction (when switched on)."),
    ("size", "Shares from the risk budget, the per-stock limit, the total exposure limit and charges. "
             "If not even one share fits, the trade is skipped."),
    ("liquidity", "The order is a small share of the signal candle's volume."),
)


def kavach_zerodha_card(s):
    """REAL MONEY: the bot's last comparison of its own record with Zerodha, and whether new entries are held."""
    if s.get("mode") != "live":
        return ""
    r = s.get("recon") or {}
    hold = s.get("entry_hold")
    if not r:
        body = "<div class='muted'>Not compared yet: the bot does it at every start and every few minutes.</div>"
    elif not r.get("ok"):
        body = (f"<div class='warn'>⚠ Zerodha could not be read at {html.escape(str(r.get('at') or '')[11:19])} "
                f"({html.escape(str(r.get('error') or ''))}). The bot tries again.</div>")
    else:
        lines = [f"<li class='dn'><b>{html.escape(p)}</b></li>" for p in r.get("problems") or []]
        lines += [f"<li>Dropped {html.escape(x)}: Zerodha no longer holds it.</li>" for x in r.get("dropped") or []]
        lines += [f"<li>{html.escape(k)} corrected to {v} shares.</li>" for k, v in (r.get("resized") or {}).items()]
        lines += [f"<li>Cancelled a leftover order: {html.escape(x)}</li>" for x in r.get("cancelled") or []]
        lines += [f"<li class='muted'>{html.escape(x)}</li>" for x in r.get("notes") or []]
        ok = "<span class='up'>matches Zerodha</span>" if not r.get("problems") else "<b class='dn'>DOES NOT MATCH</b>"
        body = (f"<div>The bot's record {ok} · checked {html.escape(str(r.get('at') or '')[11:19])}"
                f"{' (at the start)' if r.get('startup') else ''}</div>"
                f"<ul style='margin:6px 0 0 18px;padding:0'>{''.join(lines)}</ul>")
    held = (f"<div class='banner' role='alert' style='border-color:var(--dn);margin-top:8px'><b>New entries are on "
            f"hold:</b> {html.escape(hold)}. Open positions are still managed and keep their exchange stops.</div>"
            if hold else "")
    return (f"<section class='card' id='zmatch'><div class='hd'>Bot's record against Zerodha</div>{body}{held}"
            f"<div class='muted small' style='margin-top:6px'>The bot corrects only what is safe (a position Zerodha "
            f"no longer holds, a leftover working order, a missing stop). It never opens or closes a position "
            f"because of a mismatch.</div></section>")


ORDER_ROLE_WORDS = {"E": "entry", "S": "stop-loss", "X": "exit", "F": "flatten"}


def kavach_orders_card(s, now=None):
    """Today's order record (orders.py): every real order the bot asked for and what became of it. Read-only."""
    import datetime as dt
    import orders
    if s.get("mode") != "live":
        return ""
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    latest = orders.Journal("live", now=lambda: now).latest()
    rows = []
    for tag, o in sorted(latest.items(), key=lambda kv: str(kv[1].get("at") or ""), reverse=True)[:40]:
        st = str(o.get("status") or "")
        cls = ("sig" if st == "COMPLETE" else "dn" if st in ("REJECTED", "UNCERTAIN", "NOT SENT") else
               "in" if st in ("TRIGGER PENDING", "OPEN") else "")
        fill = (f"{int(o.get('filled') or 0)} @ {_fmt(o.get('avg'))}" if o.get("filled") else "-")
        rows.append(
            f"<tr><td class='first' data-label='Time'>{html.escape(str(o.get('at') or '')[11:19])}</td>"
            f"<td data-label='Order'><b>{html.escape(str(o.get('symbol') or '?'))}</b> "
            f"{html.escape(str(o.get('side') or ''))} {html.escape(str(o.get('qty') or ''))} "
            f"<span class='muted'>{html.escape(ORDER_ROLE_WORDS.get(str(o.get('role')), 'order'))}</span></td>"
            f"<td data-label='Status'><span class='st {cls}'>{html.escape(st)}</span></td>"
            f"<td data-label='Filled' class='r'>{fill}</td>"
            f"<td data-label='Note' style='white-space:normal' class='muted small'>"
            f"{html.escape(str(o.get('note') or o.get('order_id') or ''))}</td></tr>")
    table = (f"<table class='rt'><tr><th>Last change</th><th>Order</th><th>Status</th><th class='r'>Filled</th>"
             f"<th>Zerodha order / note</th></tr>{''.join(rows)}</table>" if rows else
             "<div class='muted' style='padding:10px 14px'>No real order has been asked for today.</div>")
    return (f"<section class='card flush' id='orders'><div class='hd'>Order record today <span class='end'>"
            f"<span class='tag'>{len(latest)} order{'s' if len(latest) != 1 else ''}</span></span></div>{table}"
            f"<div class='muted small' style='padding:8px 14px 12px'>Each order is written down before it is sent "
            f"and after every change (data\\orders). Filled quantities come from Zerodha's order status, never "
            f"from the order id alone.</div></section>")


def kavach_feed_card(s):
    f = s.get("feed") or {}
    if not f:
        return ("<section class='card'><div class='hd'>Price feed</div><div class='muted'>The bot records feed "
                "health each round from its next start (version 2.0). Nothing recorded yet.</div></section>")
    rows = "".join(f"<tr><td class='first'><b>{html.escape(sym)}</b></td><td style='white-space:normal'>"
                   f"<span class='st dn'>{html.escape(why)}</span></td></tr>"
                   for sym, why in sorted((f.get("problems") or {}).items()))
    rows += "".join(f"<tr><td class='first'><b>{html.escape(sym)}</b></td><td><span class='st dn'>no data this "
                    f"round</span></td></tr>" for sym in f.get("errors") or [])
    table = (f"<table class='rt'><tr><th>Stock</th><th>Problem (no new entry while it lasts)</th></tr>{rows}</table>"
             if rows else "<div class='muted' style='padding:10px 14px'>No data problems this round.</div>")
    return (f"<section class='card flush' id='feed'><div class='hd'>Price feed <span class='end'><span class='tag'>"
            f"checked {html.escape(str(f.get('checked_at') or '')[11:19])}</span></span></div>"
            f"<div style='padding:10px 14px'>{feed_line(s)}</div>{table}</section>")


def render_kavach():
    """Kavach: the limits in force, how much of today's limits is used, the checks every entry passes, what was
    refused today and why, and the price feed's health. Read-only: nothing here changes a limit."""
    import kavach
    s = read_status()
    lim = kavach.limits()
    loose = {x["name"] for x in kavach.looser_than_default()}
    rows = "".join(
        f"<tr><td class='first' data-label='Limit' style='white-space:normal'><b>{html.escape(x['what'])}</b>"
        f"<div class='muted small'>{x['name']}</div></td>"
        f"<td data-label='Now' class='r'><b>{x['value']:g}</b></td>"
        f"<td data-label='Built-in' class='r'>{x['default']:g}</td>"
        f"<td data-label='Status'>" + (
            "<span class='st dn'>LOOSER than built-in</span>" if x["name"] in loose else
            "<span class='st'>changed (stricter)</span>" if x["changed"] else "<span class='st in'>as built</span>")
        + "</td></tr>" for x in lim)
    warn = (f"<div class='banner' role='alert' style='border-color:var(--dn)'><b>{len(loose)} limit(s) are set looser "
            f"than the built-in value:</b> {html.escape(', '.join(sorted(loose)))}. They come from Windows settings "
            f"(environment variables); remove them there to go back to the built-in values.</div>" if loose else "")
    eq = s.get("equity")
    notional = sum(float(p.get("entry") or 0) * float(p.get("qty") or 0) for p in s.get("positions") or [])
    n_pos = len(s.get("positions") or [])
    streak = int(s.get("loss_streak") or 0)
    usage = (_limits_kpis(s)
             + _kpi("Open positions", f"{n_pos} <small>of {C.MAX_OPEN_POSITIONS}</small>",
                    _meter(n_pos / C.MAX_OPEN_POSITIONS * 100) if C.MAX_OPEN_POSITIONS else "")
             + _kpi("In the market", (f"{notional / float(eq) * 100:.0f}% <small>of {C.MAX_EXPOSURE_PCT:g}%</small>"
                                      if eq else "-"),
                    _meter(notional / float(eq) * 100 / C.MAX_EXPOSURE_PCT * 100) if eq else "")
             + _kpi("Losses in a row", f"{streak} <small>of {C.LOSS_STREAK_LIMIT}</small>",
                    _meter(streak / C.LOSS_STREAK_LIMIT * 100) if C.LOSS_STREAK_LIMIT else ""))
    if eq:
        usage += _kpi("Risk per trade now", _inr(float(eq) * C.RISK_PER_TRADE_PCT / 100),
                      f"{C.RISK_PER_TRADE_PCT:g}% of {_inr(eq)}")
    checks = "".join(f"<li><b>{i}. {name}</b>: {html.escape(what)}</li>" for i, (name, what) in enumerate(KAVACH_CHECKS, 1))
    cooldown = s.get("cooldown_until")
    cool = (f"<div class='banner' role='status'><b>Cool-down:</b> no new trades until {html.escape(str(cooldown))} "
            f"after {C.LOSS_STREAK_LIMIT} losses in a row.</div>" if cooldown else "")
    body = f"""
<div class="page-head"><h1>🛡 Kavach <span class="tag">risk controls</span></h1>
<span class="sub">One set of checks decides every new position, in paper, real money and backtests alike ·
read-only</span></div>
{mode_chips(s)}{warn}{cool}
<div class="kgrid">{usage}</div>
{live_signals(s)}
{kavach_feed_card(s)}
{kavach_zerodha_card(s)}
{kavach_orders_card(s)}
<section class="card flush"><div class="hd">Limits in force</div>
<table class="rt"><tr><th>Limit</th><th class="r">Now</th><th class="r">Built-in</th><th>Status</th></tr>{rows}</table>
<div class="muted small" style="padding:8px 14px 12px">A limit can be changed only in the bot's own settings, never
from this page. "Looser" means a larger risk than the built-in value.</div></section>
<details class="card" open><summary>The nine checks every entry passes, in order</summary>
<ol style="margin:6px 0 0 4px;padding-left:18px;line-height:1.6;list-style:none">{checks}</ol>
<div class="muted small" style="margin-top:8px">The first check that fails stops the trade and names the rule in
"Today's signals". A stop-loss limits the planned loss; it cannot guarantee the price it fills at.</div></details>"""
    return page("Kavach", "/", body)


CHECK_WORDS = {"for": ("agrees", "sig"), "against": ("disagrees", "dn"), "neutral": ("says nothing", ""),
               "n/a": ("not available", "")}


def _chanakya_details(a):
    """One stock's full picture: every check with its measured value, the plan, Kavach's answer, the record."""
    rows = "".join(
        f"<tr><td class='first' data-label='Check'><b>{html.escape(c['name'])}</b></td>"
        f"<td data-label='Says'><span class='st {CHECK_WORDS[c['state']][1]}'>{CHECK_WORDS[c['state']][0]}</span></td>"
        f"<td data-label='Measured' style='white-space:normal'>{html.escape(str(c['value']))}</td>"
        f"<td data-label='What it means' style='white-space:normal' class='muted small'>{html.escape(c['why'])}</td></tr>"
        for c in a["checks"])
    p = a.get("plan") or {}
    plan = ""
    if p:
        bits = [f"entry about <b>{_fmt(p['entry'])}</b>", f"stop <b>{_fmt(p['stop'])}</b> ({_fmt(p['risk_per_share'])} a share)",
                f"2R reference {_fmt(p['reference_2r'])}"]
        if p.get("qty"):
            bits += [f"<b>{p['qty']}</b> share(s) = {_inr(p['value'])}", f"at risk {_inr(p['risk'])}",
                     f"charges {_inr(p['charges'])} + slippage {_inr(p['slippage'])}"]
        plan = (f"<div class='small' style='padding:8px 14px'><b>The plan the bot would use:</b> {' · '.join(bits)}. "
                f"Target: {html.escape(str(p.get('target')))}. A stop limits the planned loss; it cannot guarantee "
                f"the price it fills at.</div>")
    return (f"<details class='card flush' id='c-{html.escape(a['symbol'])}'><summary><b>{html.escape(a['symbol'])}</b> · "
            f"{html.escape(a['setup'])} · {a['score']} of {a['of']} agree, {a['against']} disagree</summary>"
            f"<div style='padding:8px 14px'>{html.escape(a['summary'])}</div>{plan}"
            f"<table class='rt'><tr><th>Check</th><th>Says</th><th>Measured</th><th>What it means</th></tr>{rows}</table>"
            f"</details>")


def render_chanakya():
    """Chanakya: for every watchlist stock, what speaks for and against a trade right now, from measured facts.
    Read-only. It draws from the saved result and rebuilds in the background; it never touches the broker."""
    import chanakya
    r = chanakya.latest()
    fresh = chanakya.status(r)
    if fresh["stale"]:
        auto_start("chanakya")
    note = job_strip("chanakya", r, fresh, "/chanakya?refresh=1", "steps")
    head = (f"<div class='page-head'><h1>🧭 Chanakya <span class='tag'>research assistant</span></h1>"
            f"<span class='sub'>What speaks for and against each stock right now, from measured facts · read-only"
            f"</span></div>"
            f"<div class='banner' role='note'><b>How to read this.</b> The score is a count of checks that agree, "
            f"not a probability and not advice. Chanakya cannot place an order and cannot overrule Kavach. "
            f"The past record is shown with its sample size, and says so when it proves nothing.</div>")
    if not r:
        return page("Chanakya", "/", head + note + "<p class='muted'>The first assessment appears here when it has "
                    "been built. The page updates by itself.</p>")
    rec = (r.get("record") or {}).get("ALL") or {}
    period = (r.get("record") or {}).get("period") or ["", ""]
    record = ""
    if rec.get("trades"):
        losing = rec["r_high"] < 0
        record = (f"<div class='banner' role='note' style='border-color:var(--{'dn' if losing else 'warn'})'>"
                  f"<b>The record of the bot's rules:</b> {rec['trades']} trades from {html.escape(str(period[0]))} to "
                  f"{html.escape(str(period[1]))}, average {rec['avg_r']:+.2f}R a trade after charges "
                  f"(range {rec['r_low']:+.2f} to {rec['r_high']:+.2f}), net {_inr(rec['net'], True)} on ₹1,00,000. "
                  + ("<b>Over that year these rules lost money.</b> A high count of agreeing checks below does not "
                     "change that." if losing else "") + "</div>")
    elif (r.get("record") or {}).get("error"):
        record = (f"<div class='banner' role='note'>The past record could not be worked out "
                  f"({html.escape(str(r['record']['error']))}).</div>")
    rows = []
    for a in r.get("rows", []):
        k = a.get("kavach") or {}
        h = a.get("history") or {}
        kav = ("-" if not k else f"<span class='st sig'>allowed, {k['qty']}</span>" if k.get("ok") else
               f"<span class='st dn'>refused</span> <span class='muted small'>{html.escape(str(k.get('reason')))}</span>")
        past = (f"{h['trades']} trades, {h['avg_r']:+.2f}R <span class='muted small'>({h['r_low']:+.2f} to "
                f"{h['r_high']:+.2f})</span>" if h.get("trades") else "<span class='muted'>none</span>")
        score = (f"<b>{a['score']}</b> of {a['of']} agree · {a['against']} disagree" if a.get("direction") else
                 "<span class='muted'>no setup</span>")
        rows.append(
            f"<tr><td class='first' data-label='Stock'><a href='#c-{html.escape(a['symbol'])}'><b>"
            f"{html.escape(a['symbol'])}</b></a></td>"
            f"<td data-label='Now' style='white-space:normal'>{html.escape(a['setup'])}</td>"
            f"<td data-label='Checks'>{score}</td>"
            f"<td data-label='Kavach' style='white-space:normal'>{kav}</td>"
            f"<td data-label='Past record'>{past}</td>"
            f"<td data-label=\"Bot's rule\" style='white-space:normal' class='muted small'>"
            f"{html.escape(str(a.get('bot_rule') or ''))}</td></tr>")
    errs = r.get("errors") or {}
    err = (f"<div class='banner' role='note'>No data for {len(errs)} stock(s): "
           f"{html.escape(', '.join(sorted(errs)))}</div>" if errs else "")
    details = "".join(_chanakya_details(a) for a in r.get("rows", []) if a.get("direction"))
    quiet = [a for a in r.get("rows", []) if not a.get("direction")]
    quiet_txt = (f"<section class='card'><div class='hd'>No setup right now</div><ul class='small' "
                 f"style='margin:0 0 0 18px;padding:0'>"
                 + "".join(f"<li>{html.escape(a['summary'] or a['symbol'] + ': ' + a['setup'])}</li>" for a in quiet)
                 + "</ul></section>" if quiet else "")
    body = f"""{head}{note}{record}{err}
<section class="card flush"><div class="hd">Ranking <span class="muted small">most agreeing checks first ·
as of {html.escape(str(r.get('as_of') or '')[11:16])} · account {_inr(r.get('equity'))} ({html.escape(str(r.get('mode')))})</span>
<span class="end"><form method="get" action="/chanakya" style="margin:0"><input type="hidden" name="refresh" value="1">
<button class="ghost small">↻ Rebuild</button></form></span></div>
<table class="rt"><tr><th>Stock</th><th>Now</th><th>Checks</th><th>Kavach</th><th>Past record (this stock and direction)</th>
<th>Bot's rule on this candle</th></tr>{''.join(rows)}</table></section>
{details}{quiet_txt}"""
    return page("Chanakya", "/", body)


def _pct(v, nd=1, signed=False):
    return "-" if v is None else f"{v:+.{nd}f}%" if signed else f"{v:.{nd}f}%"


def _rajya_rebalance(alloc, view, q):
    """The rebalancing SIMULATION: arithmetic on a rule the user picks. The form is a plain GET; nothing is ordered."""
    import rajya
    rule = "equal" if (q.get("rule") or ["cap"])[0] == "equal" else "cap"
    try:
        cap = min(100.0, max(1.0, float((q.get("cap") or [C.MAX_POSITION_PCT])[0])))
    except ValueError:
        cap = float(C.MAX_POSITION_PCT)
    sim = rajya.rebalance(alloc, rule, cap)
    form = (f"<form method='get' action='/rajya' class='small' style='padding:8px 14px;display:flex;gap:10px;"
            f"flex-wrap:wrap;align-items:center'><input type='hidden' name='view' value='{view}'>"
            f"<label>Rule <select name='rule'><option value='cap'{' selected' if rule == 'cap' else ''}>No holding "
            f"above a share of the account</option><option value='equal'{' selected' if rule == 'equal' else ''}>"
            f"Every holding the same size</option></select></label>"
            f"<label>Share limit % <input type='number' name='cap' min='1' max='100' step='1' value='{cap:g}' "
            f"style='width:5em'></label><button class='ghost small'>Work it out</button></form>")
    note = ("<div class='banner' role='note' style='margin:8px 14px'><b>Simulation only.</b> Rajya cannot place "
            "orders. These rows are arithmetic on the rule you chose, not advice; nothing changes in your account "
            "unless you decide to and place the orders yourself in Kite. Selling can create a tax bill, which is "
            "not estimated here.</div>")
    if not sim["holdings"]:
        table = ("<p class='muted small' style='padding:0 14px 12px'>There are no delivery holdings to rebalance. "
                 "The bot's intraday positions are closed the same day and are left out.</p>")
    elif not sim["trades"]:
        table = (f"<p class='small' style='padding:0 14px 12px'>Nothing to change: "
                 + (f"no holding is above {cap:g}% of the account." if rule == "cap" else
                    "the holdings are already as equal as whole shares allow.") + "</p>")
    else:
        rows = "".join(
            f"<tr><td class='first' data-label='Stock'><b>{html.escape(t['symbol'])}</b></td>"
            f"<td data-label='Would'>{'sell' if t['action'] == 'SELL' else 'buy'} {t['qty']}</td>"
            f"<td data-label='Value' class='r'>{_inr(t['value'])}</td>"
            f"<td data-label='Share before' class='r'>{_pct(t['before_pct'])}</td>"
            f"<td data-label='Share after' class='r'>{_pct(t['after_pct'])}</td>"
            f"<td data-label='Est. charges' class='r'>{_inr(t['charges'])}</td></tr>" for t in sim["trades"])
        table = (f"<table class='rt'><tr><th>Stock</th><th>Would</th><th class='r'>Value</th><th class='r'>Share "
                 f"before</th><th class='r'>Share after</th><th class='r'>Est. charges</th></tr>{rows}</table>"
                 f"<div class='small' style='padding:8px 14px 12px'>Sold {_inr(sim['sold'])} · bought "
                 f"{_inr(sim['bought'])} · estimated charges {_inr(sim['charges'])} · cash would change by "
                 f"{_inr(sim['cash_change'], True)}. Prices are the last ones read; real fills differ.</div>")
    return (f"<section class='card flush' id='rebalance'><div class='hd'>Rebalancing simulation <span class='muted "
            f"small'>delivery holdings only · nothing is ordered</span></div>{note}{form}{table}</section>")


def render_rajya(q=None):
    """Rajya: the whole account measured (allocation, concentration, risk budget, past movement, correlation,
    what-if arithmetic, rebalancing simulation). Read-only: the view only chooses which account is shown."""
    import rajya
    q = q or {}
    s = read_status()
    view = current_view(q, s)
    r = rajya.latest(view)
    fresh = rajya.status(r)
    if fresh["stale"]:
        auto_start("rajya", view)
    note = job_strip("rajya", r, fresh, f"/rajya?view={view}&refresh=1", "steps")
    head = (f"<div class='page-head'><h1>🏛 Rajya <span class='tag'>portfolio</span></h1>"
            f"<span class='sub'>Everything in the account, measured · read-only</span></div>"
            f"{view_switch('/rajya', view, s)}{view_banner(view, s)}"
            f"<div class='banner' role='note'><b>How to read this.</b> Rajya measures what is already in the "
            f"account. It is not advice, names nothing to buy and cannot place an order. Figures from past prices "
            f"describe the past year; they are not forecasts, and real falls can be larger.</div>")
    if not r:
        return page("Rajya", "/", head + note + "<p class='muted'>The first picture appears here when it has been "
                    "built. The page updates by itself.</p>")
    if r.get("error"):
        url = login_url() if r.get("login_needed") else ""
        login = f" <a class='btn' href='{html.escape(url)}'>Log in to Zerodha</a>" if url else ""
        return page("Rajya", "/", head + note + f"<div class='banner' role='alert'><b>The account could not be read."
                    f"</b> {html.escape(str(r['error']))}{login}</div>")
    a, c, b, h, rec = r["allocation"], r["concentration"], r["budget"], r["history"], r["record"]
    warn = "".join(f"<div class='banner' role='status'>{html.escape(w)}</div>" for w in r.get("warnings") or [])
    kp = (_kpi("Whole account", _inr(a["total"]), "cash side + delivery holdings")
          + _kpi("Cash side", _inr(a["cash_equity"]), "cash with today's intraday result")
          + _kpi("Delivery holdings", _inr(a["holdings_value"]),
                 "your own sleeve" if r.get("holdings_known") else "could not be read")
          + _kpi("In intraday positions", _inr(a["intraday_value"]), "the bot's sleeve")
          + _kpi("Today so far", _pl(b["day_pnl"]), "Zerodha's figure, before charges" if view == "zerodha" else
                 "booked today after charges, plus open positions")
          + _kpi("Booked by the bot", _pl(rec["net"]),
                 f"{rec['trades']} trade(s) after {_inr(rec['charges'])} charges" if rec["trades"] else "no closed trade yet"))
    lev = ("<div class='banner' role='status'>Intraday positions are larger than the cash behind them: the "
           "difference is intraday margin (borrowed for the day).</div>" if a.get("leveraged") else "")
    rows = "".join(
        f"<tr><td class='first' data-label='Stock'><b>{html.escape(str(x['symbol']))}</b> "
        f"<span class='muted small'>{x['direction'].lower()}</span></td>"
        f"<td data-label='Sleeve'>{'intraday · ' + ('the bot' if x['owner'] == 'bot' else 'not the bot') if x['sleeve'] == 'intraday' else 'delivery · yours'}</td>"
        f"<td data-label='Sector'>{html.escape(x['sector'])}</td><td data-label='Qty' class='r'>{x['qty']}</td>"
        f"<td data-label='Value' class='r'>{_inr(x['value'])}</td>"
        f"<td data-label='Share of account' class='r'>{_pct(x['pct'])}</td>"
        f"<td data-label='P&amp;L' class='r'>{_pl(x['pnl'])}</td></tr>" for x in a["rows"])
    alloc = (f"<table class='rt'><tr><th>Stock</th><th>Sleeve</th><th>Sector</th><th class='r'>Qty</th>"
             f"<th class='r'>Value</th><th class='r'>Share of account</th><th class='r'>P&amp;L</th></tr>{rows}</table>"
             if rows else "<p class='muted small' style='padding:8px 14px'>Nothing is held right now: the whole "
             "account is cash.</p>")

    def split(title, parts):
        cells = "".join(f"<tr><td class='first' data-label='{title}'>{html.escape(p['name'])}</td>"
                        f"<td data-label='Value' class='r'>{_inr(p['value'])}</td>"
                        f"<td data-label='Share' class='r'>{_pct(p['pct'])}</td></tr>" for p in parts)
        return f"<table class='rt'><tr><th>{title}</th><th class='r'>Value</th><th class='r'>Share</th></tr>{cells}</table>"

    if c.get("positions"):
        over = (f" <b class='dn'>Above the {c['limit_pct']:g}% the bot allows itself per stock: "
                f"{html.escape(', '.join(c['over_limit']))}.</b>" if c["over_limit"] else "")
        conc = (f"Largest position <b>{html.escape(str(c['largest']))}</b> is {_pct(c['largest_pct'])} of the account; "
                f"the largest three are {_pct(c['top3_pct'])}. By size, the {c['positions']} position(s) count as "
                f"<b>{c['effective']:.1f}</b> equal-sized one(s).{over}")
    else:
        conc = "No position, so nothing is concentrated."
    stops = "".join(
        f"<tr><td class='first' data-label='Stock'><b>{html.escape(str(x['symbol']))}</b></td>"
        f"<td data-label='Stop' class='r'>{_fmt(x['stop']) if x['stop'] else '<span class=\'st dn\'>no stop</span>'}</td>"
        f"<td data-label='At risk to the stop' class='r'>{_inr(x['risk']) if x['risk'] is not None else 'whole value ' + _inr(x['value'])}</td></tr>"
        for x in b["rows"])
    budget = (
        "<div class='kgrid'>"
        + _kpi("At risk to stops", _inr(b["at_risk"]), f"{_pct(b['at_risk_pct'], 2)} of the bot's {_inr(b.get('base'))}")
        + _kpi("Daily loss limit", f"{_inr(b['day_left'])} <small>left of {_inr(b['day_limit'])}</small>",
               _meter((b["day_limit"] - b["day_left"]) / b["day_limit"] * 100) if b["day_limit"] else "")
        + _kpi("In the market", f"{_pct(b['exposure_pct'], 0)} <small>of {b['exposure_limit_pct']:g}%</small>",
               _meter((b["exposure_pct"] or 0) / b["exposure_limit_pct"] * 100) if b["exposure_limit_pct"] else "")
        + _kpi("Open intraday positions", f"{b['open']} <small>of {b['open_limit']}</small>")
        + _kpi("If every stop is hit today", _pl(b["worst_today"]), "today's result, stops and exit charges")
        + "</div>"
        + (f"<table class='rt'><tr><th>Stock</th><th class='r'>Stop</th><th class='r'>At risk to the stop</th></tr>"
           f"{stops}</table>" if stops else "")
        + (f"<div class='banner' role='status' style='margin:8px 14px'><b>No stop is known for: "
           f"{html.escape(', '.join(b['no_stop']))}.</b> Rajya only knows the bot's own stops; a holding without "
           f"one is exposed for its whole value.</div>" if b["no_stop"] else "")
        + "<div class='muted small' style='padding:8px 14px 12px'>Limits are Kavach's and are worked out on the "
          "balance the bot trades from. A stop limits the planned loss; a gap can fill beyond it.</div>")
    per = "".join(
        f"<tr><td class='first' data-label='Stock'><b>{html.escape(str(p['symbol']))}</b></td>"
        + (f"<td data-label='Usual daily move' class='r'>{_pct(p['vol_day_pct'], 2)}</td>"
           f"<td data-label='Over a year' class='r'>{_pct(p['vol_year_pct'], 0)}</td>"
           f"<td data-label='Moves with NIFTY' class='r'>{'-' if p['beta'] is None else format(p['beta'], '.2f') + 'x'}</td>"
           f"<td data-label='1 day in 20 lost more than' class='r'>{_inr(p['move_1in20'])}</td>"
           if p["covered"] else "<td colspan='4' class='muted'>no price history found</td>") + "</tr>"
        for p in h["per"])
    mix = h.get("mix")
    mix_txt = (f"<div class='small' style='padding:8px 14px 12px'><b>Today's positions, replayed over "
               f"{mix['days']} past trading days</b> ({mix['from']} to {mix['to']}): a usual day moved the account by "
               f"{_inr(mix['vol_day'])} ({_pct(mix['vol_day_pct'], 2)}); on 1 day in 20 it lost more than "
               f"{_inr(mix['loss_1in20'])}; the worst day ({mix['worst_day']}) was {_inr(mix['worst_move'], True)}; "
               f"held unchanged, the largest fall from a high was {_inr(mix['drawdown'])} "
               f"({_pct(mix['drawdown_pct'], 1)}). Past movement, not a forecast."
               + (f" <b>Left out, no price history: {html.escape(', '.join(mix['left_out']))}.</b>"
                  if mix.get("left_out") else "") + "</div>" if mix else
               "<p class='muted small' style='padding:8px 14px 12px'>Not enough price history to replay this mix.</p>")
    moves = (f"<table class='rt'><tr><th>Stock</th><th class='r'>Usual daily move</th><th class='r'>Over a year</th>"
             f"<th class='r'>Moves with NIFTY</th><th class='r'>1 day in 20 lost more than</th></tr>{per}</table>{mix_txt}"
             if per else "<p class='muted small' style='padding:8px 14px 12px'>Nothing is held, so there is nothing "
             "to measure.</p>")
    if h["pairs"]:
        prs = "".join(
            f"<tr><td class='first' data-label='Pair'><b>{html.escape(p['a'])}</b> and <b>{html.escape(p['b'])}</b></td>"
            f"<td data-label='Moved together' class='r'>{p['corr']:+.2f}</td>"
            f"<td data-label='Reading'>{'<span class=\'st dn\'>close to one bet</span>' if p['high'] else '<span class=\'st in\'>separate enough</span>'}</td></tr>"
            for p in h["pairs"][:15])
        corr = (f"<table class='rt'><tr><th>Pair</th><th class='r'>Moved together</th><th>Reading</th></tr>{prs}</table>"
                f"<div class='muted small' style='padding:8px 14px 12px'>+1 means the two always moved together, 0 "
                f"not at all. At {rajya.CORR_HIGH:g} or more, two positions spread the risk much less than their "
                f"count suggests. Average of all pairs: {h['avg_corr']:+.2f}.</div>")
    else:
        corr = ("<p class='muted small' style='padding:8px 14px 12px'>Correlation needs at least two positions with "
                "price history.</p>")
    st_rows = "".join(
        f"<tr><td class='first' data-label='What if' style='white-space:normal'><b>{html.escape(x['name'])}</b>"
        f"<div class='muted small'>{html.escape(x['what'])}</div></td>"
        f"<td data-label='Result' class='r'>{_pl(-x['loss'])}</td>"
        f"<td data-label='Of the account' class='r'>{_pct(-x['pct'] if x['pct'] is not None else None, 2, True)}</td></tr>"
        for x in r["stress"])
    stress = (f"<table class='rt'><tr><th>What if</th><th class='r'>Result</th><th class='r'>Of the account</th></tr>"
              f"{st_rows}</table><div class='muted small' style='padding:8px 14px 12px'>Each row is one stated "
              f"assumption applied to today's positions. None is a forecast, and none is a limit on what can happen."
              f"</div>" if st_rows else "<p class='muted small' style='padding:8px 14px 12px'>Nothing is held, so "
              "no scenario changes the account.</p>")
    record = (f"{rec['trades']} closed trade(s) since {html.escape(str(rec['first']))}: {_inr(rec['gross'], True)} "
              f"before charges, {_inr(rec['charges'])} charges, <b>{_inr(rec['net'], True)}</b> after. Largest fall "
              f"from a high: {_inr(rec['drawdown'])}; now {_inr(rec['below_high'])} below its high. Today: "
              f"{rec['today_trades']} closed, {_inr(rec['today_net'], True)}." if rec["trades"] else
              "No closed trade in this account's log yet, so there is no booked result or drawdown to show.")
    body = f"""{head}{note}{warn}
<div class="kgrid">{kp}</div>{lev}
<section class="card flush"><div class="hd">Where the money is <span class="muted small">as of
{html.escape(str(r.get('built') or '')[11:16])}</span><span class="end"><a class="btn ghost small"
href="/rajya?view={view}&amp;refresh=1">↻ Rebuild</a></span></div>{alloc}
{split('Sleeve', a['by_sleeve'])}{split('Sector', a['by_sector']) if a['by_sector'] else ''}
<div class="small" style="padding:8px 14px 12px"><b>Concentration.</b> {conc}</div></section>
<section class="card flush"><div class="hd">Risk budget <span class="muted small">the bot's sleeve against
Kavach's limits</span></div>{budget}</section>
<section class="card flush"><div class="hd">How these positions have moved <span class="muted small">daily
closes, past year</span></div>{moves}</section>
<section class="card flush"><div class="hd">Do they move together?</div>{corr}</section>
<section class="card flush"><div class="hd">What-if arithmetic</div>{stress}</section>
<section class="card"><div class="hd">Booked result and drawdown <span class="muted small">after charges, from
the trade log</span></div><div class="small">{record}</div></section>
{_rajya_rebalance(a, view, q)}"""
    return page("Rajya", "/", body)


def _lab_row(label, m, judged=False):
    if not m or not m.get("trades"):
        return (f"<tr><td class='first' data-label='Run'>{html.escape(label)}</td>"
                f"<td colspan='6' class='muted'>no trades</td></tr>")
    pf = f"{m['profit_factor']:.2f}" if m.get("profit_factor") is not None else "-"
    return (f"<tr{' class=judged' if judged else ''}><td class='first' data-label='Run' style='white-space:normal'>"
            f"{'<b>' if judged else ''}{html.escape(label)}{'</b>' if judged else ''}</td>"
            f"<td data-label='Trades' class='r'>{m['trades']}</td>"
            f"<td data-label='Wins' class='r'>{m['win_rate']:.0%}</td>"
            f"<td data-label='Average per trade' class='r {_cls(m['avg_r'])}'>{m['avg_r']:+.2f}R "
            f"<span class='muted'>({m['r_low']:+.2f} to {m['r_high']:+.2f})</span></td>"
            f"<td data-label='Profit factor' class='r'>{pf}</td>"
            f"<td data-label='Net' class='r'>{_pl(m['net'])}</td>"
            f"<td data-label='Months up' class='r'>{m['months_positive']} of {m['months']}</td></tr>")


def render_yantrashala():
    """Yantrashala, the strategy lab: the saved experiments (yantra.py), newest per strategy, with the rule they
    were judged by. Read-only: this page runs nothing and changes nothing; the lab never trades."""
    import yantra
    exps = yantra.experiments()
    latest = {}
    for e in exps:                                   # newest first: keep the first seen per strategy
        latest.setdefault(e["strategy"], e)
    cards = []
    for name, e in sorted(latest.items(), key=lambda kv: (kv[1]["verdict"] != "PASS", kv[0])):
        rows = "".join(_lab_row(label + " · whole develop period", m) for label, m in e["in_sample"].items())
        rows += _lab_row("Walk-forward (the part that is judged)", e["walk_forward"], judged=True)
        ok = e["verdict"] == "PASS"
        why = "".join(f"<li>{html.escape(w)}</li>" for w in e.get("why") or [])
        cards.append(
            f"<section class='card flush'><div class='hd'>{html.escape(name)} <span class='muted small'>"
            f"{html.escape(e.get('what') or '')}</span><span class='end'><span class='tag {'up' if ok else 'dn'}'>"
            f"{'PASS: forward-test candidate' if ok else 'FAIL'}</span></span></div>"
            f"<table class='rt'><tr><th>Run</th><th class='r'>Trades</th><th class='r'>Wins</th>"
            f"<th class='r'>Average per trade (95% range)</th><th class='r'>Profit factor</th><th class='r'>Net</th>"
            f"<th class='r'>Months up</th></tr>{rows}</table>"
            + (f"<ul class='small' style='margin:8px 0 10px 30px;padding:0'>{why}</ul>" if why else "")
            + f"<div class='muted small' style='padding:0 14px 12px'>{e['variants_tried']} variant(s) tried · "
              f"{html.escape(e['period'][0])} to {html.escape(e['period'][1])} · run "
              f"{html.escape(str(e.get('at') or '')[:16].replace('T', ' '))} · code {html.escape(str(e.get('code')))}"
              f"</div></section>")
    reg = yantra.registered()
    try:
        with open(os.path.join(yantra.lab_dir(), "forward.json"), encoding="utf-8") as f:
            fwd = json.load(f)
    except (OSError, ValueError):
        fwd = {}
    frows = {r["label"]: r for r in fwd.get("rows") or []}
    reg_rows = "".join(
        f"<tr><td class='first'><b>{html.escape(r['label'])}</b></td><td>{html.escape(r['registered'])}</td>"
        f"<td style='white-space:normal'>{html.escape((frows.get(r['label']) or {}).get('status') or 'not measured yet: run yantra.py forward')}"
        + (f" · {(frows[r['label']]['result'] or {}).get('trades', 0)} trades · "
           f"{html.escape(str(frows[r['label']].get('verdict')))}" if (frows.get(r['label']) or {}).get('result') else "")
        + "</td></tr>" for r in reg)
    reg_card = (f"<section class='card flush'><div class='hd'>Forward test <span class='muted small'>the only "
                f"untouched data: candles recorded after a variant was frozen</span></div>"
                + (f"<table class='rt'><tr><th>Variant</th><th>Frozen on</th><th>Result on later candles</th></tr>"
                   f"{reg_rows}</table>" if reg else
                   "<div class='muted' style='padding:10px 14px'>Nothing is registered. Only a variant that passes "
                   "the walk-forward test is worth freezing for a forward test; none has passed.</div>")
                + "</section>")
    n_pass = sum(1 for e in latest.values() if e["verdict"] == "PASS")
    tried = yantra.tests_so_far()
    head = (f"<div class='kgrid k4'>{_kpi('Ideas tested', str(len(latest)))}{_kpi('Variants tried', str(tried))}"
            f"{_kpi('Passed walk-forward', str(n_pass), cls='up' if n_pass else '')}"
            f"{_kpi('In forward test', str(len(reg)))}</div>")
    empty = ("" if cards else "<section class='card'><div class='muted'>No experiment has been run yet. Run "
             "<code>.venv\\Scripts\\python.exe yantra.py test all</code> in the bot's folder.</div></section>")
    body = f"""
<div class="page-head"><h1>⚗ Yantrashala <span class="tag">strategy lab</span></h1>
<span class="sub">Ideas under test. Nothing here is traded, and a backtest alone never puts a strategy into the bot ·
read-only</span></div>
<div class="banner" role="note"><b>How an idea is judged</b> (fixed before any result was seen): tested on the
develop period only, walked forward month by month, and it passes only with at least {yantra.MIN_TRADES} walk-forward
trades, an average result per trade whose whole 95% range is above zero, a profit factor above {yantra.PASS_PF} and
{yantra.PASS_MONTHS:.0%} of months positive. A pass is only a ticket to the forward test on future data.
{f'With {tried} variants tried, the best one looking good is expected by luck alone.' if tried else ''}</div>
{head}{empty}{''.join(cards)}{reg_card}"""
    return page("Yantrashala", "/", body)


def sizing_capital(s=None):
    """(rupees, plain label): the capital that plans on the Scanner and Review pages are sized from. It is the
    account the bot trades: in real money the Zerodha balance the bot read (what it sizes its own trades from),
    otherwise the paper opening balance. It used to be a fixed Rs 1,00,000 whatever the account held."""
    s = read_status() if s is None else s
    bal = s.get("balance") or {}
    if s.get("mode") == "live" and bal.get("inr"):
        return float(bal["inr"]), "your Zerodha balance, as the bot read it"
    import capital
    return float(capital.current_opening_balance()), "the paper opening balance"


def plan_qty(close, stop, cap):
    """Shares for a long plan: the risk rule, capped by the per-stock limit (the Scanner's own rule)."""
    if not close or not stop or close <= stop:
        return 0
    return int(min(cap * C.RISK_PER_TRADE_PCT / 100 // (close - stop), cap * C.MAX_POSITION_PCT / 100 // close))


def render_scanner(refresh=False):
    """Draws straight away from the saved scan; scans in the background when it is missing, out of date
    or a re-scan was asked for. Never waits for price downloads."""
    import scanner
    r = scanner.latest()
    fresh = scanner.status(r)
    if refresh:
        jobs.start("scanner")
    elif fresh["stale"]:
        auto_start("scanner")
    note = job_strip("scanner", r, fresh, "/scanner?refresh=1", "stocks")
    if not r:
        return page("Swing scanner", "/scanner", f"<h1>{T.FLAG} {T.term('scanner')}</h1>{note}<p class='muted'>"
                    "The scan appears here when it has finished. The page updates by itself.</p>")
    real_errors = {s: e for s, e in (r.get("errors") or {}).items() if "skipped" not in str(e)}
    err_note = ""
    if real_errors:
        shown = ", ".join(f"{s} ({e.split(': ', 1)[-1]})" for s, e in sorted(real_errors.items())[:5])
        more = f" and {len(real_errors) - 5} more" if len(real_errors) > 5 else ""
        err_note = (f"<div class='banner' role='note'><b>Data errors for {len(real_errors)} of "
                    f"{len(real_errors) + len(r.get('rows', {}))} stocks</b>, left out of this scan: "
                    f"{html.escape(shown + more)}</div>")
    counts = "".join(_kpi(html.escape(k), f"{v}") for k, v in r.get("counts", {}).items())
    rows = []
    hits = [(s, x) for s, x in r.get("rows", {}).items() if x["setups"]]
    cap, cap_label = sizing_capital()
    for s, x in sorted(hits, key=lambda kv: (-len(kv[1]["setups"]), kv[0])):
        trend = ("<span class='up'>above 200 DMA</span>" if x.get("above_200dma") else
                 "<span class='dn'>below 200 DMA</span>" if x.get("above_200dma") is False else "-")
        qty = plan_qty(x.get("close"), x.get("stop"), cap)
        plan = (f"stop {_fmt(x['stop'])} · target {_fmt(x['target'])} · "
                + (f"qty {qty}" if qty else "<span class='warn'>qty 0: 1 share doesn't fit the limits</span>")
                if x.get("stop") else "-")
        rows.append(
            f"<tr><td class='first' data-label='Stock'><b>{html.escape(s)}</b></td>"
            f"<td data-label='60 days'>{sparkline(x.get('closes', []), None, None)}</td>"
            f"<td data-label='Setup' style='white-space:normal'><span class='st sig'>{html.escape(', '.join(x['setups']))}</span></td>"
            f"<td data-label='Close' class='r'><span>{_fmt(x['close'])} <span class='{'up' if x['change_pct'] >= 0 else 'dn'}'>"
            f"{'▲' if x['change_pct'] >= 0 else '▼'} {x['change_pct']:+.2f}%</span></span></td>"
            f"<td data-label='RSI' class='r'>{_fmt(x.get('rsi'), 0)}</td>"
            f"<td data-label='Trend'><span>{trend} <span class='muted'>· {x['from_52w_high_pct']:+.1f}% vs 52w high</span></span></td>"
            f"<td data-label='Plan' class='block' style='white-space:normal'>{plan}</td></tr>")
    table = "".join(rows) or "<tr><td colspan=7 class='muted'>No setups on the last session.</td></tr>"
    risk_rs = cap * C.RISK_PER_TRADE_PCT / 100
    built = html.escape(str(r.get('built', ''))[:16].replace('T', ' '))
    errors = (' Data errors: ' + html.escape(', '.join(r['errors']))) if r.get('errors') else ''
    body = f"""
<div class="page-head"><h1>{T.FLAG} {T.term('scanner')} <span class="tag">{html.escape(str(r.get('day')))}</span></h1>
<span class="sub">Multi-day (CNC) setups on the last closed daily candle · built {built}</span></div>
<div class="banner"><b>Research only</b> - {html.escape(T.ui('scanner_scout'), quote=False)}, the bot does not trade these. Check the chart yourself before acting.</div>
{note}{err_note}<div class="kgrid k4">{counts}</div>
<section class="card flush"><div class="hd">Stocks with a setup <span class="end">
<form method="get" action="/scanner" style="margin:0"><input type="hidden" name="refresh" value="1">
<button class="ghost small">↻ Re-scan</button></form></span></div>
<table class="rt"><tr><th>Stock</th><th>60 days</th><th>Setup</th><th class="r">Close</th><th class="r">RSI</th><th>Trend</th>
<th>Plan (₹{risk_rs:,.0f} risk)</th></tr>{table}</table></section>
<details class="card"><summary>How the setups and the plan work</summary><div class="small muted">
Setups: 20-day high breakout · bull flag (≥8% pole, tight shallow flag) · 20-day MA reclaim ·
oversold (RSI&lt;30) · oversold bounce (RSI&lt;30 in last 3 days, close above yesterday's high).
Plan: stop = close − 1.5 × ATR(14), target = 2R, qty risks {C.RISK_PER_TRADE_PCT}% of {_inr(cap)}
({html.escape(cap_label)}; capped at {C.MAX_POSITION_PCT:.0f}% of it per stock, so {_inr(cap * C.MAX_POSITION_PCT / 100)}). The TWS interview's rule: prefer stocks in an uptrend (above the 200 DMA,
near highs).{errors}</div></details>"""
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


def _day_label(iso_day):
    """'2026-11-16' -> '16 Nov'"""
    import datetime as dt
    try:
        return dt.date.fromisoformat(iso_day).strftime("%d %b")
    except ValueError:
        return iso_day or "-"


def trade_stats(rows, start_cash):
    """Numbers for one trade list: equity after each trade, max drawdown, wins, net, average R, charges."""
    nets = [_num(r.get("net")) for r in rows]
    equity, eq = [], start_cash
    for v in nets:
        eq += v
        equity.append(eq)
    peak, max_dd = start_cash, 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100 if peak else 0)
    n = len(rows)
    return {"n": n, "nets": nets, "equity": equity, "max_dd": max_dd, "net": sum(nets),
            "wins": sum(1 for v in nets if v > 0), "avg_r": sum(_num(r.get("r")) for r in rows) / n if n else None,
            "charges": sum(_num(r.get("charges")) for r in rows)}


def trade_kpis(st, label, frame=""):
    n, net = st["n"], st["net"]
    cells = (_kpi(f"{html.escape(label)} trades", f"{n}")
             + _kpi("Win rate", f"{(st['wins'] / n * 100 if n else 0):.0f}%", f"{st['wins']} won · {n - st['wins']} lost")
             + _kpi("Net P&amp;L", _signed(net), "after charges", _cls(net))
             + _kpi("Max drawdown", f"{st['max_dd']:.2f}%", "from the highest equity", "dn" if st["max_dd"] else "")
             + _kpi("Average", "-" if st["avg_r"] is None else f"{st['avg_r']:+.2f}R", "per trade",
                    _cls(st["avg_r"] or 0))
             + _kpi("Charges", _inr(st["charges"]), "Zerodha charges"))
    cls = f"card acct {frame} kwrap" if frame else "kwrap card"
    return f"<section class='{cls}'><div class='kgrid'>{cells}</div></section>"


def trade_charts(rows, st, label, start_cash):
    import charts
    import chartjs
    tips = [f"{r.get('symbol', '')} {r.get('direction', '')} {r.get('closed', '')[:10]} ({_num(r.get('r')):+.2f}R)"
            for r in rows]
    days = [(r.get("closed") or "")[:10] for r in rows]
    eq_pts = [[_day_label(d), round(v, 2), d, t] for d, v, t in zip(days, st["equity"], tips)]
    bar_pts = [[f"#{i + 1} {_day_label(d)}", round(v, 2), t] for i, (d, v, t) in enumerate(zip(days, st["nets"], tips))]
    eq_chart = chartjs.container({"type": "line", "pts": eq_pts, "base": start_cash, "from0": True, "dd": True,
                                  "ranges": True, "firstLabel": "start"},
                                 charts.equity_curve(st["equity"], start_cash, tips),
                                 f"{label}: equity after each closed trade")
    bar_chart = chartjs.container({"type": "bars", "pts": bar_pts, "h": 170},
                                  charts.pnl_bars(st["nets"], tips), f"{label}: profit or loss per trade")
    return (f"<section class='card'><h2>{html.escape(label)}: equity after each closed trade "
            f"<span class='muted small'>(red area = drawdown from the peak)</span></h2>{eq_chart}"
            f"<h2 style='margin-top:14px'>Profit or loss per trade <span class='muted small'>"
            f"(hover, tap or use the arrow keys for details)</span></h2>{bar_chart}</section>")


def trade_breakdowns(rows):
    return (f"<section class='card'><div class='cols'>{breakdown_table(rows, 'reason', 'By exit reason')}"
            f"{breakdown_table(rows, 'direction', 'Long vs short')}"
            f"{breakdown_table(rows, 'symbol', 'By stock')}</div></section>")


def trade_log(rows, src, label, limit=200):
    """Every trade, newest first, with filters (stock, side, result) and a chart link. On a phone each row
    becomes a small card."""
    idx_rows = list(enumerate(rows))[-limit:][::-1]
    body = []
    for i, r in idx_rows:
        net = _num(r.get("net"))
        side = r.get("direction", "")
        body.append(
            f"<tr data-sym='{html.escape(r.get('symbol', ''))}' data-side='{html.escape(side)}' "
            f"data-res='{'won' if net > 0 else 'lost'}'>"
            f"<td class='first' data-label='Trade'><b>{html.escape(r.get('symbol', ''))}</b> "
            f"<span class='side {'long' if side == 'LONG' else 'short'}'>{html.escape(side)}</span>"
            f"<span class='lbl'>{html.escape(r.get('closed', '')[:16])}</span></td>"
            f"<td data-label='Qty' class='r'>{html.escape(r.get('qty', ''))}</td>"
            f"<td data-label='Entry → exit' class='r'>{html.escape(r.get('entry', ''))} → {html.escape(r.get('exit', ''))}</td>"
            f"<td data-label='Exit reason'>{html.escape(T.reason_label(r.get('reason', '')))}</td>"
            f"<td data-label='Charges' class='r'>₹{_num(r.get('charges')):,.2f}</td>"
            f"<td data-label='Net' class='r'><b class='{_cls(net)}'>{_signed(net)}</b></td>"
            f"<td data-label='R' class='r'>{_num(r.get('r')):+.2f}R</td>"
            f"<td data-label='Chart'><a class='sig' href='/journey?src={src}&amp;i={i}' "
            f"title='{T.name('journey')}: the trade on its chart'>{T.PATH} chart</a></td></tr>")
    syms = sorted({r.get("symbol", "") for _, r in idx_rows if r.get("symbol")})
    opts = "".join(f"<option value='{html.escape(x)}'>{html.escape(x)}</option>" for x in syms)
    more = f" (newest {limit} of {len(rows)})" if len(rows) > limit else ""
    filters = (f"<div class='filters' role='search' aria-label='Filter trades'>"
               f"<label>Stock <select data-f='sym'><option value=''>All</option>{opts}</select></label>"
               f"<label>Side <select data-f='side'><option value=''>All</option><option>LONG</option><option>SHORT</option></select></label>"
               f"<label>Result <select data-f='res'><option value=''>All</option><option value='won'>Won</option>"
               f"<option value='lost'>Lost</option></select></label>"
               f"<span class='muted small' data-count>{len(idx_rows)} trades{more}</span></div>")
    return (f"<section class='card tlog'><h2>{html.escape(label)} trade log <span class='muted small'>newest first</span></h2>"
            f"{filters}<table class='rt'><tr><th>Trade</th><th class='r'>Qty</th><th class='r'>Entry → exit</th>"
            f"<th>Exit reason</th><th class='r'>Charges</th><th class='r'>Net</th><th class='r'>R</th><th></th></tr>"
            f"{''.join(body)}</table></section>")


def trade_section(rows, src, label, start_cash, show_log=True):
    """Key figures, charts, breakdowns and the trade log for one trade list (used for the backtest)."""
    st = trade_stats(rows, start_cash)
    out = trade_kpis(st, label)
    if not rows:
        return out
    out += trade_charts(rows, st, label, start_cash) + trade_breakdowns(rows)
    return out + (trade_log(rows, src, label, limit=500) if show_log else "")


def daily_pnl_card(mode):
    """Net P&L per trading day from the daily journal, as bars."""
    import chartjs
    import charts
    import journal
    days = journal.days(mode)
    if not days:
        return ""
    pts = [[_day_label(r["date"]), round(journal._f(r.get("net_pnl")), 2),
            f"{int(journal._f(r.get('trades')))} trade(s) · {r.get('status') or ''}"] for r in days]
    chart = chartjs.container({"type": "bars", "pts": pts, "h": 160}, charts.pnl_bars([p[1] for p in pts]),
                              "Net profit or loss per trading day")
    return (f"<section class='card'><h2>Net P&amp;L per trading day <span class='muted small'>from the daily journal"
            f"</span></h2>{chart}</section>")


TRADES_CSS = """
.filters{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:0 0 10px}
.filters label{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}
.tlog tr[hidden]{display:none!important}
.panel[hidden]{display:none}
.side{font-size:11px;font-weight:700;padding:1px 6px;border-radius:5px;margin-left:4px}
.side.long{background:var(--up-bg);color:var(--up)} .side.short{background:var(--dn-bg);color:var(--dn)}
.lbl{display:block;font-size:11.5px;color:var(--muted)}
.kwrap{background:none;border:0;box-shadow:none;padding:0;overflow:visible;border-radius:var(--radius)}
.card.acct.kwrap{outline-offset:5px;margin:5px 5px 21px} .kwrap .kgrid{margin:0}
"""

TRADES_JS = """
(function(){
  // tabs: Overview | Journal | Trade log | Backtest (all panels stay visible without the script)
  var tabs=[].slice.call(document.querySelectorAll('.ptabs [data-tab]'));
  function pick(id,push){var ok=tabs.some(function(t){return t.getAttribute('data-tab')===id}); if(!ok)id=tabs[0]&&tabs[0].getAttribute('data-tab');
    tabs.forEach(function(t){var on=t.getAttribute('data-tab')===id;t.classList.toggle('on',on);t.setAttribute('aria-selected',on)});
    document.querySelectorAll('.panel').forEach(function(p){p.hidden=p.id!==id});
    if(push)try{history.replaceState(null,'','#'+id)}catch(e){}
    document.dispatchEvent(new CustomEvent('regionsupdated'));}
  tabs.forEach(function(t){t.addEventListener('click',function(ev){ev.preventDefault();pick(t.getAttribute('data-tab'),true)})});
  var asked=(location.hash||'').slice(1); try{asked=asked||new URLSearchParams(location.search).get('tab')||''}catch(e){}
  if(tabs.length){pick(asked||tabs[0].getAttribute('data-tab'),false);
    // a link like /trades#journal opens that tab with the page at the top (the tabs stay in view)
    if(location.hash)setTimeout(function(){window.scrollTo(0,0)},0);}
  // trade log filters
  document.querySelectorAll('.tlog').forEach(function(card){
    var sels=[].slice.call(card.querySelectorAll('select[data-f]')), count=card.querySelector('[data-count]');
    function apply(){var n=0, rows=card.querySelectorAll('tr[data-sym]');
      rows.forEach(function(tr){var show=sels.every(function(s){return !s.value||tr.getAttribute('data-'+s.getAttribute('data-f'))===s.value});
        tr.hidden=!show; if(show)n++});
      if(count)count.textContent=n+' of '+rows.length+' trades';}
    sels.forEach(function(s){s.addEventListener('change',apply)});
  });
})();
"""


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
    head = (f"<div class='card' id='jcard'><h2>{T.SCROLL} Daily journal · {html.escape(T.name(mode))}</h2>"
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
    rows = trade_rows(mode)
    label = f"{T.name(mode)} · {VIEWS[view]}"
    start, note = C.PAPER_START_INR, ""
    if mode == "paper":
        import capital
        start = capital.current_opening_balance()
        if capital.load().get("history"):
            note = (f"<div class='muted small' style='margin:-8px 0 14px'>The equity curve and drawdown use the current "
                    f"opening balance ({_inr(start)}). {capital_lines()}</div>")
    st = trade_stats(rows, start)
    empty = ("" if rows else
             webui.empty(T.ui("empty_trades"), "card muted", "section")
             if mode == "paper" else
             "<section class='card muted'>No real-money trades: the bot has only traded on paper. This list shows "
             "trades the bot places in live mode (data\\trades_live.csv), not your whole Zerodha tradebook.</section>")
    overview = (empty + (trade_charts(rows, st, label, start) if rows else "") + daily_pnl_card(mode)
                + (trade_breakdowns(rows) if rows else "") + edge_card(rows, f"{mode.title()} trading"))
    panels = [("overview", "Overview", overview),
              ("journal", "Journal", journal_card(mode, view, (q or {}).get("period", ["day"])[0])),
              ("log", "Trade log", trade_log(rows, mode, label) if rows else empty)]
    bt = trade_rows("backtest")
    if bt:
        first, last = bt[0].get("opened", "")[:10], max(r.get("closed", "")[:10] for r in bt)
        panels.append(("backtest", "Backtest",
                       f"<div class='page-head'><h1>{T.FIRE} {T.name('backtest')} · Last backtest "
                       f"<span class='tag'>{html.escape(first)} → {html.escape(last)}</span></h1>"
                       f"<span class='sub'>Run <code>.venv\\Scripts\\python.exe backtest.py</code> to update. "
                       f"Full list: data\\backtest_trades.csv</span></div>"
                       + trade_section(bt, "backtest", "Backtest", C.PAPER_START_INR)
                       + edge_card(bt, "Backtest", compact=True)))
    tabs = "".join(f"<a href='#{pid}' data-tab='{pid}' role='tab'>{name}</a>" for pid, name, _ in panels)
    body = (f"<style>{TRADES_CSS}</style>"
            f"<div class='page-head'><h1>{T.SCROLL} {T.term('trades')}</h1>"
            f"<span class='sub'>{html.escape(T.ui('trades_sub'), quote=False)}</span></div>"
            + mode_chips(s) + view_banner(view, s) + trade_kpis(st, label, view) + note
            + f"<nav class='ptabs' role='tablist' aria-label='Trades sections'>{tabs}</nav>"
            + "".join(f"<section class='panel' id='{pid}' role='tabpanel' aria-label='{name}'>{html_}</section>"
                      for pid, name, html_ in panels)
            + f"<script>{TRADES_JS}</script>")
    return page("Trades", "/trades", body, view=view)


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
        import chartjs
        band = (orng["low"], orng["high"]) if orng else None
        chart = chartjs.container(charts.journey_data(today, trade, ema, band),
                                  charts.journey(today, trade, ema, band), f"{sym} 15-minute candles with the trade")
    net, rr = _num(r.get("net")), _num(r.get("r"))
    held = f"{int((closed - opened).total_seconds() // 60)} min" if closed else "-"
    side = r.get('direction', '')
    back = f"/trades?view={'zerodha' if src == 'live' else 'paper'}#{'backtest' if src == 'backtest' else 'log'}"
    trail = f'EMA{C.TRAIL_EMA} trail' if C.TRAIL_EMA > 0 else 'no trail'
    body = f"""
<div class="page-head"><h1>{T.PATH} {T.name('journey')}: {html.escape(sym)}
<span class="tag {'up' if side == 'LONG' else 'dn'}">{html.escape(side)}</span>
<span class="tag">{opened:%a %d %b %Y}</span> <span class="tag">{html.escape(src)}</span></h1>
<span class="sub"><a href="{back}">← back to {T.term('trades')}</a></span></div>
<div class="kgrid">{_kpi("Result", f"{_signed(net)}", f"{rr:+.2f}R", _cls(net))}
{_kpi("Entry", _fmt(_num(r.get('entry'))), f"at {opened:%H:%M}")}
{_kpi("Exit", _fmt(_num(r.get('exit'))), f"at {closed.strftime('%H:%M') if closed else '-'}")}
{_kpi("Exit reason", html.escape(T.reason_label(r.get('reason', '-'))))}
{_kpi("Held", held, f"qty {html.escape(r.get('qty', '-'))}")}
{_kpi("Charges", f"₹{_num(r.get('charges')):,.2f}")}</div>
<section class="card"><h2>15-minute candles <span class="muted small">{T.ui('band_colour')} band = opening range · red dashed =
{T.term('stop')} · grey dotted = entry · {T.ui('trail_colour')} = {trail} · ▲▼ = entry and exit</span></h2>{chart}</section>
<div class="muted small">Hover or tap a candle (or use the arrow keys) for its prices. The chart uses today's price
data, so it can differ slightly from what the bot saw live if a bar has since been corrected.</div>"""
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


SETTINGS_CSS = """
ul.checks{list-style:none;margin:8px 0;padding:0;display:grid;gap:8px}
ul.checks li{padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface-2)}
ul.checks li span.muted{font-size:12.5px}
#mode form{margin:10px 0}
#mode input[name=words]{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 8px;font:inherit;min-height:36px;max-width:100%}
.tpicks{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;border:0;margin:0 0 12px;padding:0}
.tpick{display:flex;flex-direction:column;gap:8px;padding:12px;border:1px solid var(--border);border-radius:var(--radius);
 background:var(--surface-2);cursor:pointer}
.tpick:has(input:checked){border-color:var(--accent);box-shadow:inset 0 0 0 2px var(--accent)}
.tpick .tn{display:flex;align-items:center;gap:8px;font-weight:700} .tpick .td{color:var(--muted);font-size:12.5px}
.tpick input{width:18px;height:18px;margin:0;accent-color:var(--accent)}
.tgroup{grid-column:1/-1;margin:8px 0 -4px;font:600 13px var(--serif);color:var(--gold);letter-spacing:.3px}
.pv{display:block;border-radius:var(--radius);overflow:hidden;background:var(--bg);color:var(--text);
 border:var(--card-border-width) solid var(--border);font:13px/1.4 var(--font)}
.pv-top{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--border)}
.pv-top b{font:600 14px var(--serif);color:var(--gold)}
.pv-logo{width:26px;height:26px;display:grid;place-items:center;border-radius:7px;font-size:15px;flex:none}
.pv-logo svg{width:26px;height:26px;display:block}
.pv-body{display:flex;flex-wrap:wrap;gap:8px;padding:10px}
.pv-kpi{background:var(--surface);border:var(--card-border-width) solid var(--border);border-radius:var(--radius);
 padding:6px 9px;box-shadow:var(--shadow)}
.pv-kpi small{display:block;color:var(--muted);font-size:11px} .pv-kpi b{font-size:15px;font-variant-numeric:tabular-nums}
.pv-up{color:var(--up)} .pv-dn{color:var(--dn)}
.pv-chip{align-self:center;border:1px solid var(--gold);color:var(--gold);border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700}
.pv-btn{align-self:center;background:var(--btn-bg);color:var(--accent-ink);
 border-radius:var(--btn-radius);padding:5px 10px;font-weight:650;font-size:12px}
.pv-name{font:600 12px var(--serif);color:var(--gold)}
"""


def _theme_preview(t):
    """A small live preview tile drawn with theme t's own tokens and art (sample figures, clearly marked)."""
    logo = t.avatar or html.escape(t.strings["icons"].get("bow", ""))
    terms = t.strings["terms"]
    return (f"<span class='pv pv-{t.id}' aria-hidden='true'>"
            f"<span class='pv-top'><span class='pv-logo'>{logo}</span><b>{T.APP}</b>"
            f"<span class='pv-name' style='margin-left:auto'>{html.escape(terms['live_page'])} · Live</span></span>"
            f"<span class='pv-body'>"
            f"<span class='pv-kpi'><small>Today's P&amp;L (sample)</small><b class='pv-up'>▲ ₹207.51</b></span>"
            f"<span class='pv-kpi'><small>Open P&amp;L (sample)</small><b class='pv-dn'>▼ −₹42.10</b></span>"
            f"<span class='pv-chip'>{html.escape(terms['paper'])} · PAPER</span>"
            f"<span class='pv-btn'>⏸ {html.escape(terms['pause'])} · Pause new entries</span>"
            f"</span></span>")


def render_settings(saved=False, msg=""):
    """Settings: the dashboard theme (look only, saved in this browser), the trading mode, waiting updates."""
    cur = T.theme_id()
    if not T._ui:
        body = (f"<div class='page-head'><h1>{T.GEAR} {T.ui('settings_page')}</h1></div>"
                "<div class='banner'>Themes could not be loaded, so the dashboard uses its built-in look.</div>")
        return page(T.ui("settings_page"), "/settings", body)
    previews = "".join(T._ui.preview_css(t, f".pv-{t.id}") for t in T._ui.THEMES.values())
    picks, last_group = [], None
    for tid in T._ui.ids():
        t = T._ui.THEMES[tid]
        grp = T._ui.group(tid)
        if grp and grp != last_group:
            picks.append(f"<p class='tgroup'>{html.escape(grp)}</p>")
        last_group = grp
        checked = (" checked" + (" autofocus" if saved else "")) if tid == cur else ""  # keyboard focus returns here
        in_use = " <span class='tag'>in use</span>" if tid == cur else ""
        picks.append(f"<label class='tpick'><span class='tn'><input type='radio' name='ui_theme' value='{tid}'{checked}>"
                     f"{html.escape(t.name)}{in_use}</span>"
                     f"<span class='td'>{html.escape(t.description)}</span>{_theme_preview(t)}</label>")
    note = ("<div class='banner' role='status'>Theme saved for this browser. The bot, its settings and your "
            "accounts are unchanged.</div>" if saved else "")
    if msg:
        note += f"<div class='banner' role='status'>{html.escape(msg)}</div>"
    body = f"""<style>{SETTINGS_CSS}{previews}</style>
<div class="page-head"><h1>{T.GEAR} {html.escape(T.ui('settings_page'))}</h1><span class="sub">Dashboard look · saved in this browser only</span></div>
{note}
<section class="card" id="theme"><div class="hd">Theme</div>
<p class="muted" style="margin-top:0">{html.escape(T.ui('settings_theme_intro'))}</p>
<form method="get" action="/settings" id="themeForm">
<fieldset class="tpicks"><legend class="sr">Dashboard theme</legend>{"".join(picks)}</fieldset>
<button type="submit">Apply theme</button>
<span class="muted small">Applies straight away, no restart. Choosing a theme also applies it.</span>
</form>
<p>Light or dark: use the ☀ / ☾ button at the top right. It works with every theme.</p>
<p class="muted small">Stored as a cookie named <code>{T._ui.COOKIE}</code> in this browser only. A theme changes colours,
pictures and the extra names shown next to the plain terms. It never changes the bot, its settings, the trading mode
({T.term('paper')} / {T.term('live')}), orders or your accounts. Prices, P&amp;L, stops, errors and confirmations read the
same in every theme.</p>
</section>
<script>(function(){{var f=document.getElementById('themeForm');if(!f)return;
f.addEventListener('change',function(e){{if(e.target&&e.target.name==='ui_theme')f.submit();}});}})();</script>
{trading_mode_card(read_status())}
{updates_section(read_status())}"""
    return page(T.ui("settings_page"), "/settings", body)


# ---------------------------------------------------------------- version, waiting updates, restart when safe
_pending_cache = {"t": 0.0, "key": None, "val": None}
_settings_cache = {"t": 0.0, "val": None}


def pending_updates(s, with_settings=False):
    """What is on disk but not running yet (buildinfo.pending), cached for 30 s. Settings are only worked
    out on the Settings page (it starts a small Python to read them), cached for 2 minutes."""
    import time as _t
    if not buildinfo:
        return {"bot_files": [], "settings": [], "dashboard_files": []}
    settings_now = None
    if with_settings:
        if _t.monotonic() - _settings_cache["t"] > 120 or _settings_cache["val"] is None:
            _settings_cache.update(t=_t.monotonic(), val=buildinfo.settings_on_next_start())
        settings_now = _settings_cache["val"]
    bot_build = (s or {}).get("build")
    key = (json.dumps(bot_build, sort_keys=True) if bot_build else "", with_settings)
    if _t.monotonic() - _pending_cache["t"] > 30 or _pending_cache["key"] != key:
        _pending_cache.update(t=_t.monotonic(), key=key,
                              val=buildinfo.pending(bot_build, DASH_BUILD, settings_now=settings_now))
    return _pending_cache["val"]


def next_start_text(now=None):
    """When the scheduled task next starts the bot (09:00 on the next trading day)."""
    import datetime as _dt
    import holidays
    now = now or _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))
    d = now.date()
    if not (holidays.is_trading_day(d) and now.time() < _dt.time(9, 0)):
        d = holidays.next_trading_day(d)
    return "today at 09:00" if d == now.date() else f"{d:%a %d %b} at 09:00"


def _short(c):
    return (c or "")[:7]


def _bot_running(s):
    t = _dt_parse((s or {}).get("time"))
    return t is not None and (_now_ist() - t).total_seconds() < 300


def _post_btn(action, label, ask, cls="ghost"):
    """A small form button that asks first and carries the page's confirmation and CSRF_TOKEN."""
    return (f"<form method='post' action='{action}' style='display:inline' "
            f"onsubmit=\"if(!confirm('{ask}'))return false;this.confirm.value='yes'\">"
            f"<input type='hidden' name='confirm' value=''>{csrf_field()}<button class='{cls} small'>{label}</button></form>")


_RESTART_POLL_JS = """<script>(function(){var el=document.querySelector('[data-restart-stage]');if(!el)return;
var stage=el.getAttribute('data-restart-stage');
function tick(){if(document.hidden){setTimeout(tick,3000);return;}
fetch('/api/restart',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
if((j.stage||'')!==stage){location.reload();return;}
var t=el.querySelector('.js-text');if(t&&j.reason){t.textContent=j.reason;}setTimeout(tick,2000);})
.catch(function(){setTimeout(tick,5000);});}
setTimeout(tick,2000);})();</script>"""

_STAGE_LABEL = {"waiting": ("⏸", "Restart Pending – Waiting for Safe Conditions"),
                "checking": ("⏳", "Applying changes: checking the new code"),
                "stopping": ("⏳", "Stopping: saving the journal and state"),
                "restarting": ("⏳", "Restarting")}


def restart_strip(s):
    """Where an Apply-Changes restart stands: the live stages (waiting, checking, stopping, restarting), the
    result, or a red failure banner with the options (Try again / Go back / Leave it stopped)."""
    import restart
    st = restart.state() or {}
    stage = st.get("stage")
    bad = restart.problem(_dt_parse((s or {}).get("time")))
    if bad:
        return restart_failure(bad, s)
    if stage in _STAGE_LABEL:
        icon, label = _STAGE_LABEL[stage]
        cancel = (_post_btn("/bot/restart-cancel", "Cancel", "Cancel the pending restart?") if stage == "waiting" else "")
        return (f"<div class='jobstrip {'warn' if stage == 'waiting' else 'run'}' role='status' aria-live='polite' "
                f"data-restart-stage='{stage}'><span class='js-state'><span aria-hidden='true'>{icon}</span> "
                f"<b>{html.escape(label)}</b></span><span class='js-text'>{html.escape(st.get('reason') or '')}</span>"
                f"{cancel}</div>" + _RESTART_POLL_JS)
    if stage == "running":
        at = _dt_parse(st.get("at"))
        if at and (_now_ist() - at).total_seconds() < 3600:
            b = (s or {}).get("build") or {}
            return (f"<div class='jobstrip ok' role='status'><span class='js-state'><span aria-hidden='true'>✓</span> "
                    f"<b>Changes applied</b></span><span class='js-text'>Running {html.escape(str(b.get('version') or st.get('version')))}"
                    f" ({_short(b.get('commit'))}) since {at:%H:%M}; all checks passed (back in "
                    f"{int(st.get('took_sec') or 0)} s).</span></div>")
    if stage == "checked":
        return (f"<div class='jobstrip ok' role='status'><span class='js-state'><span aria-hidden='true'>✓</span> "
                f"<b>Checked at the close</b></span><span class='js-text'>{html.escape(st.get('reason') or '')}</span></div>")
    return ""


def restart_failure(reason, s):
    import restart
    running = _bot_running(s)
    cur = buildinfo.commit() if buildinfo else None
    prev = restart.previous_good(cur)
    opts = []
    if running:
        opts.append(_post_btn("/bot/apply-now", "↻ Try again", "Try the restart again (when safe)?"))
    if prev:
        opts.append(_post_btn("/bot/go-back", f"↩ Go back to {html.escape(str(prev.get('version')))}",
                              f"Switch the folder back to {prev.get('version')} ({_short(prev.get('commit'))}), the last "
                              "version that started successfully, and restart the bot when safe? Balances, trade "
                              "history, journals and settings are not touched."))
    opts.append(_post_btn("/bot/restart-dismiss", "Leave it as it is", "Hide this message and leave the bot as it is?"))
    steps = ("<details><summary class='small'>Steps to look into it</summary><ol class='small'>"
             "<li>Read the reason above and <code>data\\logs\\restart_requests.log</code> and today's "
             "<code>data\\logs\\bot_&lt;date&gt;.log</code>.</li>"
             "<li>If the bot is stopped, start it by hand with <b>2-START-BOT.bat</b> (it runs the code in the folder).</li>"
             "<li>Balances, trade history, journals and settings were not touched.</li></ol></details>")
    return (f"<div class='jobstrip bad' role='alert'><span class='js-state'><span aria-hidden='true'>✕</span> "
            f"<b>Restart problem</b></span><span class='js-text'>{html.escape(reason)}</span>"
            f"<span>{' '.join(opts)}</span>{steps}</div>")


def update_banner(s):
    """Live page: where an Apply-Changes restart stands (or a red failure banner), else one line when
    changes are pending (details and buttons on the Settings page)."""
    try:
        strip = restart_strip(s)
    except Exception:  # noqa: BLE001 - never breaks the Live page
        strip = ""
    if strip:
        return strip
    p = pending_updates(s)
    parts = []
    if p["dashboard_files"]:
        parts.append("dashboard only (applies when the dashboard restarts; the bot keeps trading)")
    if p["bot_files"]:
        parts.append(f"bot code (needs a bot restart; otherwise active at the next start, {next_start_text()})")
    if not parts:
        return ""
    return (f"<div class='banner' role='status'><b>Changes pending:</b> {html.escape('; '.join(parts))}. "
            f"<a href='/settings#updates'>Apply changes</a></div>")


def _dt_parse(v):
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def updates_section(s):
    """Settings page, "Changes Pending": what runs, what is waiting in the folder (dashboard/theme only, or bot
    code and settings), and the Apply buttons. The buttons restart with the code already in the folder: they
    never pull or merge anything, and they can't change PAPER/LIVE."""
    import restart
    s = s or {}
    b = s.get("build") or {}
    running = _bot_running(s)
    live = (s.get("mode") == "live")
    rows = []
    if b and running:
        rows.append(f"<li><b>Bot</b>: running {html.escape(str(b.get('version')))} ({_short(b.get('commit'))})"
                    f"{' with edited files' if b.get('dirty') else ''} since "
                    f"{html.escape(str(b.get('started', ''))[11:16])} · {T.mode_label(s.get('mode') or 'paper')}</li>")
    else:
        rows.append("<li><b>Bot</b>: not running" + (f" (last ran {html.escape(str(b.get('version')))})" if b else "")
                    + "</li>")
    if DASH_BUILD:
        rows.append(f"<li><b>Dashboard</b>: {html.escape(str(DASH_BUILD.get('version')))} "
                    f"({_short(DASH_BUILD.get('commit'))}) since {html.escape(str(DASH_BUILD.get('started', ''))[11:16])}</li>")
    if buildinfo:
        rows.append(f"<li><b>In the folder</b>: {html.escape(buildinfo.version())} ({_short(buildinfo.commit())})</li>")
    p = pending_updates(s, with_settings=True)
    items = []
    if p["dashboard_files"]:
        items.append(f"<li><b>Dashboard / theme only</b> ({html.escape(', '.join(p['dashboard_files']))}): applies "
                     "when the dashboard restarts, any time; the bot is not touched. "
                     + _post_btn("/dashboard/restart", "↻ Restart the dashboard now",
                                 "Restart the dashboard to apply the changes? The bot keeps trading; this page reloads.")
                     + "</li>")
    if p["bot_files"] or p["settings"]:
        what = ", ".join(p["bot_files"] + [f"setting {k}" for k in p["settings"]])
        line = f"<li><b>Bot code or settings</b> ({html.escape(what)}): needs a bot restart. "
        st = restart.state() or {}
        if restart.after_close_requested():
            line += (f"<b>Set to apply after the market close</b>: checked at 15:35, active at the next start "
                     f"({next_start_text()}). " + _post_btn("/bot/restart-cancel", "Cancel", "Cancel applying after the close?"))
        elif restart.busy() or st.get("stage") in _STAGE_LABEL:
            line += "A restart is in progress (see above)."
        elif not running:
            line += f"The bot isn't running: it starts with these changes ({next_start_text()})."
        else:
            line += ("<br>" + _post_btn("/bot/apply-after-close", "Apply After Market Close (recommended)",
                                        "Apply the changes at the next start? The bot finishes today as usual and "
                                        "checks the new code at the close.", cls="")
                     + " " + _post_btn("/bot/apply-now", "Apply Now (when safe)",
                                       "Restart the bot to apply the changes as soon as it is safe? "
                                       + ("Real money: only before 09:15 or after the close, with no open position."
                                          if live else "PAPER: with no open position and not right after a candle "
                                          "close; no new entries while it waits.")))
            line += ("<div class='muted small'>Apply Now checks the new code in a separate process first; if it "
                     "can't start, the bot keeps running the old code. After the restart the bot checks itself: "
                     "one copy, same mode, same positions, trades, cash and balance.</div>")
        items.append(line + "</li>")
    if not items:
        items.append("<li>No changes pending: everything in the folder is running.</li>")
    return (f"<section class='card' id='updates'><div class='hd'>Changes Pending</div>{restart_strip(s)}"
            f"<ul style='margin:0 0 8px;padding-left:18px'>{''.join(rows)}</ul>"
            f"<ul style='margin:0;padding-left:18px'>{''.join(items)}</ul>"
            "<p class='muted small'>Theme files apply by themselves. Only hashes are compared, never secret "
            "settings. Nothing here pulls or merges code, or changes PAPER/LIVE. Every request is logged in "
            "data\\logs\\restart_requests.log.</p></section>")


def go_back(s):
    """Switch the folder back to the last version that started successfully (git, detached), then ask the bot
    to restart when safe. Refused if program files were edited and not committed (they would be lost)."""
    import subprocess
    import restart
    if not buildinfo:
        return False, "Version information isn't available."
    cur = buildinfo.commit()
    prev = restart.previous_good(cur)
    if not prev:
        return False, "No earlier version that started successfully is recorded."
    if buildinfo.dirty():
        return False, ("Program files in the folder were edited and not committed; going back would lose them. "
                       "Commit them or go back by hand.")
    branch = buildinfo._git("rev-parse", "--abbrev-ref", "HEAD") or "main"
    root = os.path.dirname(os.path.abspath(__file__))
    out = subprocess.run(["git", "switch", "--detach", prev["commit"]], cwd=root, capture_output=True, text=True,
                         timeout=60)
    if out.returncode:
        restart.log("go back", "git failed", (out.stderr or "").strip()[-200:])
        return False, "git could not switch: " + (out.stderr or "").strip()[-200:]
    restart.log("go back", "switched", f"{_short(cur)} -> {_short(prev['commit'])} ({prev.get('version')})")
    msg = f"The folder is back on {prev.get('version')} ({_short(prev['commit'])}). "
    if _bot_running(s):
        restart.request_now()
        msg += "The bot restarts with it when safe. "
    msg += f"To return to the newer code later: git switch {branch}"
    return True, msg


def _now_ist():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))


_SERVER = None


def restart_dashboard():
    """Start a fresh dashboard program and stop this one (to load a dashboard update). The bot is a
    separate program and is not touched."""
    import subprocess
    import threading
    import time as _t

    def go():
        _t.sleep(0.5)                       # let the "restarting" page reach the browser
        args = [sys.executable, os.path.abspath(__file__), "--no-browser", "--wait-port"]
        kw = {}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 7              # minimized, without taking focus
            kw = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": si}
        log = os.path.join(C.DATA_DIR, "logs", "dashboard_restart.log")
        try:
            # start the new dashboard FIRST (it waits for the port): once this server stops, the main
            # thread ends and Python exits, so nothing after shutdown() is sure to run
            new = subprocess.Popen(args, cwd=os.path.dirname(os.path.abspath(__file__)), **kw)
            note = f"started the new dashboard (process {new.pid})"
        except OSError as e:
            note = f"could NOT start a new dashboard ({e}); this one keeps running"
        try:
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{_now_ist():%Y-%m-%d %H:%M:%S} dashboard {os.getpid()}: {note}\n")
        except OSError:
            pass
        if "NOT" in note:
            return
        if _SERVER:
            _SERVER.shutdown()
            _SERVER.server_close()          # frees port 5000 for the new dashboard
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()


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
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # the browser left before the page arrived (tab closed, clicked elsewhere): nothing to do

    def _redirect(self, to="/"):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        """Every page is drawn in the dashboard theme chosen in this browser (cookie; Default if none).
        The theme only changes the look; the request handling below is the same for every theme.
        Edited theme files are picked up here, without restarting the dashboard."""
        if not host_ok(self.headers.get("Host"), self.server.server_address[1]):
            self._send("forbidden: open the dashboard at http://127.0.0.1:%d" % C.WEB_PORT, 403, "text/plain")
            return
        if T._ui:
            try:
                T._ui.maybe_reload()
            except Exception:  # never let a theme check break a page
                pass
        tok = T._ui.activate(T._ui.from_cookie(self.headers.get("Cookie"))) if T._ui else None
        try:
            self._get()
        finally:
            if tok is not None:
                T._ui.reset(tok)

    def _settings(self, q):
        """GET /settings shows the page; /settings?ui_theme=<id> saves the choice in a browser cookie
        (look only: never the bot's settings, mode or accounts) and shows the page again."""
        want = q.get("ui_theme", [None])[0]
        if want is None or not T._ui:
            self._send(render_settings(saved="saved" in q, msg=q.get("msg", [""])[0]))
            return
        tid = want if want in T._ui.THEMES else T._ui.DEFAULT
        self.send_response(303)
        self.send_header("Set-Cookie", f"{T._ui.COOKIE}={tid}; Path=/; Max-Age=31536000; SameSite=Lax")
        self.send_header("Location", "/settings?saved=1")
        self.end_headers()

    def _get(self):
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
            if q.get("refresh", ["0"])[0] == "1":   # start the rebuild, then show the page without ?refresh
                jobs.start("review", day)              # (so reloading the page doesn't start another one)
                self._redirect("/review" + (f"?day={day}" if day else ""))
                return
            self._send(render_review(day))
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
        elif u.path == "/settings":
            self._settings(q)
        elif u.path == "/kavach":
            self._send(render_kavach())
        elif u.path == "/yantrashala":
            self._send(render_yantrashala())
        elif u.path == "/chanakya":
            if q.get("refresh", ["0"])[0] == "1":
                jobs.start("chanakya")
                self._redirect("/chanakya")
                return
            self._send(render_chanakya())
        elif u.path == "/rajya":
            # read-only: the view only chooses which account is SHOWN; it never changes the trading mode
            view = current_view(q, read_status())
            if q.get("refresh", ["0"])[0] == "1":
                jobs.start("rajya", view)
                self._redirect(f"/rajya?view={view}")
                return
            self._send(render_rajya(q))
        elif u.path == "/scanner":
            if q.get("refresh", ["0"])[0] == "1":
                jobs.start("scanner")
                self._redirect("/scanner")
                return
            self._send(render_scanner())
        elif u.path == "/api/zerodha/events":
            # read-only: Zerodha position-change alerts for the popups on every page (accounts.zerodha_events)
            import accounts
            try:
                since = int(q.get("since", ["-1"])[0])
            except ValueError:
                since = -1
            out = accounts.zerodha_events(since)
            w = watch_timing(read_status())       # "No updates" warning for the popups (watchlist timing)
            out["watch"] = {"state": w["state"], "key": w["key"], "text": w["text"]}
            out["notices"] = watchdog_notices()   # crashes and automatic restarts today (watchdog.py)
            self._send(json.dumps(out), ctype="application/json; charset=utf-8")
        elif u.path == "/api/jobs":
            # read-only: progress of the Review and Scanner background builds
            self._send(json.dumps(jobs.snapshot_all()), ctype="application/json; charset=utf-8")
        elif u.path == "/api/restart":
            # read-only: where an Apply-Changes restart stands (for the live stage display)
            import restart
            st = restart.state() or {}
            self._send(json.dumps({"stage": st.get("stage"), "reason": st.get("reason"), "at": st.get("at")}),
                       ctype="application/json; charset=utf-8")
        elif u.path == "/api/version":
            # read-only: which version the dashboard and the bot run (no file lists, no settings)
            keep = ("program", "version", "commit", "dirty", "started", "pid")
            bot_b = read_status().get("build") or {}
            self._send(json.dumps({"dashboard": {k: (DASH_BUILD or {}).get(k) for k in keep},
                                   "bot": {k: bot_b.get(k) for k in keep}}),
                       ctype="application/json; charset=utf-8")
        elif u.path == "/api/live":
            # read-only: the Live page's changing parts as JSON, for in-place updates
            s = read_status()
            view = current_view(q, s)
            body = json.dumps({"view": view, "regions": live_regions(view, s)}, ensure_ascii=False)
            self._send(body, ctype="application/json; charset=utf-8")
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

    def _paper_capital(self, form):
        """Save or cancel a PAPER opening-balance change, after the confirmation the page adds when you
        click OK. Writes only data/paper_capital.json."""
        from urllib.parse import quote
        import capital
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

    def _restart(self, form):
        """Apply-Changes requests from the Settings page (do_POST has already checked Host, Origin and
        CSRF_TOKEN), after the confirmation the page adds when you click OK. The bot is never stopped from
        here: it restarts itself when safe. A second request while one is pending is refused (409)."""
        import restart
        if form.get("confirm", [""])[0] != "yes":
            self._redirect("/settings#updates")    # nothing done without the confirmation
            return
        if self.path == "/dashboard/restart":
            restart.log("restart dashboard", "requested")
            self._send(page("Restarting", "/settings",
                            "<meta http-equiv='refresh' content='5;url=/settings#updates'>"
                            "<div class='banner' role='status'>Restarting the dashboard to load the update. "
                            "The bot keeps trading. This page reloads in a few seconds.</div>"))
            restart_dashboard()
            return
        ok, msg = True, ""
        if self.path == "/bot/apply-now":
            ok, msg = restart.request_now()
        elif self.path == "/bot/apply-after-close":
            ok, msg = restart.request_after_close()
        elif self.path == "/bot/restart-cancel":
            restart.cancel()
        elif self.path == "/bot/restart-dismiss":
            restart.set_stage("dismissed")
            restart.log("restart problem", "left as it is")
        elif self.path == "/bot/go-back":
            ok, msg = go_back(read_status())
        if not ok:
            code = 409 if "pending" in msg or "Already" in msg else 400
            self._send(page("Not done", "/settings", f"<div class='banner' role='alert'>{html.escape(msg)} "
                            "<a href='/settings#updates'>Back</a></div>"), code)
            return
        if msg and self.path == "/bot/go-back":
            self._send(page("Going back", "/settings", f"<div class='banner' role='status'>{html.escape(msg)} "
                            "<a href='/settings#updates'>Back</a></div>"))
            return
        self._redirect("/settings#updates")

    def do_POST(self):
        # Other websites can't read the dashboard, but a page could try to send it a form: only forms from the
        # dashboard's own pages are accepted (exact Origin/Referer, Host, and the per-start CSRF_TOKEN).
        port = self.server.server_address[1]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 10_000:
            self.close_connection = True
            self._send("too large", 413, "text/plain")
            return
        # read the form before any answer: replying with unread data makes Windows reset the connection,
        # and the browser would show a network error instead of the reason
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not host_ok(self.headers.get("Host"), port) or not origin_ok(origin, port):
            self._send("forbidden: use the buttons on the dashboard page", 403, "text/plain")
            return
        if not csrf_ok(form):
            self._send("forbidden: this page is out of date (the dashboard restarted). Reload it and try again.",
                       403, "text/plain")
            return
        os.makedirs(C.DATA_DIR, exist_ok=True)
        if self.path in ("/paper-capital", "/paper-capital/cancel"):
            self._paper_capital(form)
            return
        if self.path == "/jobs/cancel":
            # stop a running Review or Scanner build (nothing of it is saved); never touches the bot
            name = form.get("job", [""])[0]
            if name in ("review", "scanner", "chanakya", "rajya"):
                job = jobs.get(name)
                if job:
                    job.cancel()
            self._redirect({"scanner": "/scanner", "chanakya": "/chanakya", "rajya": "/rajya"}.get(name, "/review"))
            return
        if self.path in ("/bot/apply-now", "/bot/apply-after-close", "/bot/restart-cancel", "/bot/restart-dismiss",
                         "/bot/go-back", "/dashboard/restart"):
            self._restart(form)
            return
        if self.path in ("/mode/check", "/mode/real", "/mode/paper", "/positions/close-all"):
            from urllib.parse import quote
            if self.path == "/mode/check":
                import livemode
                checks = check_readiness()
                msg = ("All live-trading checks passed." if livemode.all_ready(checks) else
                       "Live Trading is locked: see what's missing below.")
            elif self.path == "/mode/real":
                msg = arm_real_money(form)[1]
            elif self.path == "/mode/paper":
                msg = switch_to_paper()
            else:
                msg = close_all_positions(form)
            self._redirect("/settings?msg=" + quote(msg) + "#mode")
            return
        if self.path == "/zerodha/logout":
            from urllib.parse import quote
            msg = (zerodha_logout() if form.get("confirm", [""])[0] == "yes"
                   else "Not logged out: the logout needs your confirmation.")
            self._redirect("/?msg=" + quote(msg))
            return
        if self.path == "/pause":
            open(C.PAUSE_FILE, "w").close()
        elif self.path == "/resume":
            if os.path.exists(C.PAUSE_FILE):
                os.remove(C.PAUSE_FILE)
        elif self.path == "/stop":
            open(C.STOP_FILE, "w").close()
        self._redirect("/")


def serve(wait_port=0):
    """Serve the dashboard. wait_port: seconds to keep trying while port 5000 is still held by the
    dashboard this one replaces (restart_dashboard)."""
    global _SERVER
    import time as _t
    deadline = _t.monotonic() + wait_port
    while True:
        try:
            srv = ThreadingHTTPServer((C.WEB_HOST, C.WEB_PORT), Handler)
            break
        except OSError:
            if _t.monotonic() >= deadline:
                raise
            _t.sleep(0.5)
    _SERVER = srv
    srv.serve_forever()


if __name__ == "__main__":
    # The dashboard is its own program: it never trades. The bot (bot.py) runs separately and keeps
    # trading when this window is closed or restarted (for example to load a dashboard update).
    print(T.banner(f"{T.name('live_page')} dashboard (the bot runs separately; this only shows it)"))
    print(f"  Dashboard: http://{C.WEB_HOST}:{C.WEB_PORT}   (Ctrl+C or closing this window stops the dashboard only)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://{C.WEB_HOST}:{C.WEB_PORT}")
    try:
        serve(wait_port=20 if "--wait-port" in sys.argv else 0)
    except KeyboardInterrupt:
        pass
    except OSError as e:   # port 5000 taken: another dashboard (or an older bot that still serves it)
        print(f"  Not started: http://{C.WEB_HOST}:{C.WEB_PORT} is already in use ({e}).")
