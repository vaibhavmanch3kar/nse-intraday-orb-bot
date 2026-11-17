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
import jobs
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
    state = (f"<span class='tag warn'>→ {_inr(float(pending['amount']))[:-3]} at next start</span>" if pending
             else "<span class='tag warn'>paper only</span>")
    return f"""<details class="card" id="capital"{' open' if pending else ''}><summary>Opening Balance · Paper Account
<span class="end"><b class="mono">{_inr(current)[:-3]}</b>{state}</span></summary>
<form method="post" action="/paper-capital" onsubmit="return confirmCapital(this)" style="display:inline">
<div class="opts">{radios}</div>
<input type="hidden" name="confirm" value="">
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
        late = lag is not None and lag > 20
        tip = f" title='{html.escape(ds.get('reason') or '')}'" if ds.get("reason") else ""
        delay = "" if lag is None else f" · {lag:.0f} min behind"
        data_chip = (f"<span class='chip {'amber' if late or ds.get('reason') else 'ok'}'{tip}><i></i>"
                     f"{src} data{delay}</span>")
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
                 + _kpi("Available funds", _inr(a["available"]), f"margin used {_inr(a['margin_used'])}")
                 + _kpi("Today's P&amp;L", _signed(a["pnl"]), f"booked {_inr(a['realised'], True)} · open {_inr(a['unrealised'], True)}",
                        _cls(a["pnl"]))
                 + _kpi("Open P&amp;L", _signed(a["unrealised"]), f"{n_open} open position{'s' if n_open != 1 else ''}",
                        _cls(a["unrealised"]))
                 + _kpi("Invested", _inr(a["invested"]), "open positions at average price")
                 + _kpi("Margin used", _inr(a["margin_used"]), "Zerodha's figure"))
        warn = "".join(f"<div class='warn small warnline'>⚠ {html.escape(w)}</div>" for w in a.get("warnings", []))
        note = (f"<div class='muted small warnline'>From Zerodha at {_clock(a.get('fetched_at'))} IST · read-only · "
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


def live_watchlist(s):
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
    return (f"<section class='card flush' id='watchlist'><div class='hd'><span class='theme'>{T.term('watchlist')}</span> <span class='end'><span class='tag'>last closed 15-min bar</span></span></div>"
            f"<table><tr><th>Stock</th><th class='r'>Close</th><th class='r hide-s'>Opening range</th><th class='r hide-s'>VWAP</th>"
            f"<th class='r hide-s'>Vol ×</th><th class='hide-s'>Bar</th><th>Status</th></tr>{body}</table></section>")


def live_events(s):
    items = []
    for e in s.get("events", []):
        t, _, msg = e.partition(" ")
        items.append(f"<li><time>{html.escape(t)}</time><span>{html.escape(msg)}</span></li>")
    body = "".join(items) or "<li class='muted'>No events yet</li>"
    return (f"<details class='card' id='events' open><summary><span class='theme'>{T.term('events')}</span>"
            f"</summary><ul class='list'>{body}</ul></details>")


def live_notice(s, running):
    if running:
        return ""
    when = (s.get("time") or "")
    return (f"<div class='banner'><b>Bot is not running</b> ({T.name('not_running')}: markets closed, weekend, or stopped). "
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
            f"onsubmit=\"return confirm('{ask}')\"><button class='ghost'>{label}</button></form>"
            f"<form method='post' action='/stop' style='display:inline' "
            f"onsubmit=\"return confirm('Stop the bot? In live mode open positions keep their exchange stop-loss.')\">"
            f"<button class='red'>■ Stop bot</button></form>"
            f"<div class='muted small'>Both ask before acting. Open positions keep their stop.</div></section>")


def live_limits(s):
    cooldown = s.get("cooldown_until")
    lag = s.get("data_lag_min")
    lag_txt = "-" if lag is None else f"{lag:.0f} min"
    until = f" · <span class='warn'>until {html.escape(str(cooldown))}</span>" if cooldown else ""
    return (f"<details class='card' id='limits'><summary>Data &amp; limits</summary><dl class='facts'>"
            f"<dt>Loss streak</dt><dd>{_fmt(s.get('loss_streak'))} <span class='muted'>· {T.name('cooldown')} after "
            f"{C.LOSS_STREAK_LIMIT}</span>{until}</dd>"
            f"<dt>Price data</dt><dd>{data_source_text(s)}</dd>"
            f"<dt>Data delay</dt><dd>{lag_txt}</dd>"
            f"<dt>Per trade</dt><dd>{C.RISK_PER_TRADE_PCT:g}% risk · at most {C.MAX_OPEN_POSITIONS} positions, "
            f"{C.MAX_TRADES_PER_DAY} trades a day</dd>"
            f"<dt>{T.name('daily_loss')}</dt><dd>stop new trades at −{C.MAX_DAILY_LOSS_PCT:g}% in a day (booked + open)</dd>"
            f"<dt>Exits</dt><dd>{T.term('stop')} · {T.term('trail')} · {T.term('time_stop')} · "
            f"{T.term('square_off')} at {C.SQUARE_OFF}</dd>"
            f"</dl></details>")


def live_login(ok, login_msg):
    url = login_url()
    login_btn = (f"<a class='btn' href='{html.escape(url)}'>Log in to Zerodha</a>" if url and not ok else "")
    return (f"<section class='card'><div class='hd'><span class='theme'>{T.term('login')}</span></div>"
            f"<div class='{'up' if ok else 'warn'}'>{html.escape(login_msg)}</div>{login_btn}"
            f"<div class='muted small'>{T.term('paper')} mode doesn't need a login, except for {T.name('kite_data')} (Kite) "
            f"price data. {T.term('live')} mode needs one every day (tokens expire ~06:00 IST).</div></section>")


LIVE_REFRESH_SEC = ACCOUNT_REFRESH_SEC


def live_regions(view, s):
    """The parts of the Live page that change while the bot runs. /api/live sends the same parts and the
    page swaps them in place every few seconds (no reload; the opening-balance form is never touched)."""
    running = bot_running(s)
    ok, login_msg = login_status()
    return {"status": _status_chips(s, running, view, ok, s.get("data_lag_min")),
            "notice": live_notice(s, running), "acct": live_kpis(view, s), "equity": live_equity(view, s),
            "zpositions": zerodha_positions() if view == "zerodha" else "",
            "positions": live_positions(s, view), "watchlist": live_watchlist(s),
            "controls": live_controls(s, running), "login": live_login(ok, login_msg),
            "events": live_events(s), "limits": live_limits(s)}


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
{view_banner(view)}
<div class="banner" id="offline" role="status" hidden>⚠ Can't reach the dashboard server: the figures below may be old. It keeps trying.</div>
{R('notice')}
{note}
{R('acct')}
<div class="grid" style="margin-top:16px">
<div>{R('equity')}{R('zpositions')}{R('positions')}{R('watchlist')}</div>
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


jobs.register("review", _review_job, REVIEW_LIMIT_SEC)
jobs.register("scanner", _scan_job, SCAN_LIMIT_SEC)


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
        text = f"updated {_hm(built)}, {_ago(built)}"
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
    at_risk = total * C.PAPER_START_INR * C.RISK_PER_TRADE_PCT / 100
    body = f"""
<div class="page-head"><h1>{T.LAMP} {T.term('review')} <span class="tag">{r['day']}</span></h1>
<span class="sub">The day replayed through the strategy rules, one stock at a time (portfolio limits not applied) ·
built {built} from 15-min data</span></div>{note}
<div class="chips"><span class="muted small">Day:</span><span class="days">{days}</span>
<form method="get" action="/review" style="display:inline;margin:0"><input type="hidden" name="day" value="{r['day']}">
<input type="hidden" name="refresh" value="1"><button class="ghost small">↻ Refresh</button></form></div>
<div class="kgrid k4">{_kpi("Signals", f"{r.get('signals', 0)}")}{_kpi("Winners", f"{r.get('wins', 0)}")}
{_kpi("Total", f"{total:+.2f}R", "after slippage and charges", _cls(total))}
{_kpi("At 0.5% risk on ₹1L", _signed(at_risk), "about", _cls(at_risk))}</div>{progress_note}
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
    for s, x in sorted(hits, key=lambda kv: (-len(kv[1]["setups"]), kv[0])):
        trend = ("<span class='up'>above 200 DMA</span>" if x.get("above_200dma") else
                 "<span class='dn'>below 200 DMA</span>" if x.get("above_200dma") is False else "-")
        plan = (f"stop {_fmt(x['stop'])} · target {_fmt(x['target'])} · qty {x['qty']}"
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
    risk_rs = C.PAPER_START_INR * C.RISK_PER_TRADE_PCT / 100
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
Plan: stop = close − 1.5 × ATR(14), target = 2R, qty risks {C.RISK_PER_TRADE_PCT}% of ₹{C.PAPER_START_INR:,.0f}
(capped at {C.MAX_POSITION_PCT:.0f}% of capital). The TWS interview's rule: prefer stocks in an uptrend (above the 200 DMA,
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
            + mode_chips(s) + view_banner(view) + trade_kpis(st, label, view) + note
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
.tpicks{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;border:0;margin:0 0 12px;padding:0}
.tpick{display:flex;flex-direction:column;gap:8px;padding:12px;border:1px solid var(--border);border-radius:var(--radius);
 background:var(--surface-2);cursor:pointer}
.tpick:has(input:checked){border-color:var(--accent);box-shadow:inset 0 0 0 2px var(--accent)}
.tpick .tn{display:flex;align-items:center;gap:8px;font-weight:700} .tpick .td{color:var(--muted);font-size:12.5px}
.tpick input{width:18px;height:18px;margin:0;accent-color:var(--accent)}
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


def render_settings(saved=False):
    """Settings: the dashboard theme (look only, saved in this browser)."""
    cur = T.theme_id()
    if not T._ui:
        body = (f"<div class='page-head'><h1>{T.GEAR} {T.ui('settings_page')}</h1></div>"
                "<div class='banner'>Themes could not be loaded, so the dashboard uses its built-in look.</div>")
        return page(T.ui("settings_page"), "/settings", body)
    previews = "".join(T._ui.preview_css(t, f".pv-{t.id}") for t in T._ui.THEMES.values())
    picks = []
    for tid in T._ui.ids():
        t = T._ui.THEMES[tid]
        checked = (" checked" + (" autofocus" if saved else "")) if tid == cur else ""  # keyboard focus returns here
        in_use = " <span class='tag'>in use</span>" if tid == cur else ""
        picks.append(f"<label class='tpick'><span class='tn'><input type='radio' name='ui_theme' value='{tid}'{checked}>"
                     f"{html.escape(t.name)}{in_use}</span>"
                     f"<span class='td'>{html.escape(t.description)}</span>{_theme_preview(t)}</label>")
    note = ("<div class='banner' role='status'>Theme saved for this browser. The bot, its settings and your "
            "accounts are unchanged.</div>" if saved else "")
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
f.addEventListener('change',function(e){{if(e.target&&e.target.name==='ui_theme')f.submit();}});}})();</script>"""
    return page(T.ui("settings_page"), "/settings", body)


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
            self._send(render_settings(saved="saved" in q))
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
        elif u.path == "/scanner":
            if q.get("refresh", ["0"])[0] == "1":
                jobs.start("scanner")
                self._redirect("/scanner")
                return
            self._send(render_scanner())
        elif u.path == "/api/jobs":
            # read-only: progress of the Review and Scanner background builds
            self._send(json.dumps(jobs.snapshot_all()), ctype="application/json; charset=utf-8")
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
    # The dashboard is its own program: it never trades. The bot (bot.py) runs separately and keeps
    # trading when this window is closed or restarted (for example to load a dashboard update).
    print(T.banner(f"{T.name('live_page')} dashboard (the bot runs separately; this only shows it)"))
    print(f"  Dashboard: http://{C.WEB_HOST}:{C.WEB_PORT}   (Ctrl+C or closing this window stops the dashboard only)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://{C.WEB_HOST}:{C.WEB_PORT}")
    try:
        serve()
    except KeyboardInterrupt:
        pass
    except OSError as e:   # port 5000 taken: another dashboard (or an older bot that still serves it)
        print(f"  Not started: http://{C.WEB_HOST}:{C.WEB_PORT} is already in use ({e}).")
