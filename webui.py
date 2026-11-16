"""
The dashboard's look: colours for the dark and light themes, typography, spacing, the page frame
(header, page tabs, phone bottom navigation, theme switch) and shared components.

DISPLAY ONLY: nothing here reads or changes the bot's settings, files or trading mode.
Everything is inline (no web fonts, no CDN), so pages load fast and work without internet.
"""

import html

import theme as T

# page tabs in the order shown: path, TERMS key, icon
NAV = [("/", "live_page", T.BOW, "Live"), ("/trades", "trades", T.SCROLL, "Trades"),
       ("/review", "review", T.LAMP, "Review"), ("/scanner", "scanner", T.FLAG, "Scanner")]
VIEW_PAGES = ("/", "/trades")      # pages with the Paper Account | Zerodha Account switch
THEME_KEY = "ayodhya-theme"        # the browser remembers your light/dark choice (per browser, nothing else)


def _vars(p):
    return "".join(f"--{k}:{v};" for k, v in p.items())


def _tokens():
    dark, light = T.PALETTES["dark"], T.PALETTES["light"]
    alias = ("--card:var(--surface);--fg:var(--text);--line:var(--border);--acc:var(--accent);"
             "--radius:12px;--font:system-ui,'Segoe UI',Roboto,'Noto Sans',sans-serif;"
             "--serif:Georgia,'Palatino Linotype','Times New Roman',serif;")
    return (f":root{{{_vars(dark)}{alias}color-scheme:dark}}"
            f"@media (prefers-color-scheme:light){{:root:not([data-theme=dark]){{{_vars(light)}color-scheme:light}}}}"
            f":root[data-theme=light]{{{_vars(light)}color-scheme:light}}"
            f":root[data-theme=dark]{{{_vars(dark)}color-scheme:dark}}")


BASE_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 var(--font)}
a{color:var(--accent)} a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.skip{position:absolute;left:-999px;top:8px;z-index:20;background:var(--surface);color:var(--text);padding:8px 12px;border-radius:8px}
.skip:focus{left:8px}
.num,.kpi .v,td,.mono{font-variant-numeric:tabular-nums}
.up{color:var(--up)} .dn{color:var(--dn)} .muted{color:var(--muted)} .warn{color:var(--warn)} .info{color:var(--info)}
.sig{color:var(--accent);font-weight:600} .msg{color:var(--up)} .tm{color:var(--info);font-size:13px}
.small{font-size:12.5px} .nowrap{white-space:nowrap} .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
h1{font:600 22px var(--serif);color:var(--gold);margin:4px 0 2px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
h2{font:600 14px var(--font);margin:0 0 8px;color:var(--text)}
h2 .theme,.theme{font-family:var(--serif);color:var(--gold);font-weight:600}
ul{margin:0;padding-left:18px}
code{background:var(--surface-2);border:1px solid var(--border);border-radius:5px;padding:0 5px;font-size:12.5px}

/* header */
.top{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 90%,transparent);
 -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}
.top-in{max-width:1440px;margin:0 auto;display:flex;align-items:center;gap:18px;padding:9px 24px}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--text)} .brand:hover{text-decoration:none}
.logo{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-size:18px;flex:none;
 background:linear-gradient(135deg,var(--gold),var(--accent))}
.brand b{font:600 18px var(--serif);color:var(--gold);letter-spacing:.3px;display:block;line-height:1.1}
.brand small{color:var(--muted);font-size:11px;display:block}
.tabs{display:flex;gap:4px}
.tabs a{display:flex;flex-direction:column;padding:5px 12px;border-radius:9px;color:var(--muted);text-decoration:none;line-height:1.2}
.tabs a b{color:var(--text);font-weight:600;font-size:13px} .tabs a small{font-size:11px}
.tabs a:hover{background:var(--surface-2);text-decoration:none}
.tabs a.on{background:var(--surface-2);box-shadow:inset 0 -2px 0 var(--accent)} .tabs a.on b{color:var(--gold)}
.top .right{margin-left:auto;display:flex;align-items:center;gap:10px}
.ribbon{height:3px;opacity:.85;background:repeating-linear-gradient(90deg,var(--accent) 0 14px,#b91c1c 14px 28px,var(--gold) 28px 42px)}
.icon-btn{width:38px;height:38px;border-radius:9px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);
 cursor:pointer;font-size:16px;display:grid;place-items:center;padding:0}

/* Paper Account | Zerodha Account switch */
.seg{display:inline-flex;background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:3px;gap:2px}
.seg a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:7px;font-weight:600;font-size:13px;
 min-height:32px;display:flex;align-items:center;justify-content:center;white-space:nowrap}
.seg a:hover{color:var(--text);text-decoration:none}
.seg a.paper.on{background:linear-gradient(180deg,var(--gold),var(--accent));color:var(--accent-ink)}
.seg a.zerodha.on{background:var(--live);color:#fff}
.phone-seg{display:none}

/* layout */
.wrap{max-width:1440px;margin:0 auto;padding:14px 24px 32px}
.page-head{display:flex;flex-wrap:wrap;align-items:flex-end;gap:8px 16px;margin:2px 0 10px}
.page-head .sub{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}
.grid > div,.grid > aside{min-width:0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
footer{text-align:center;color:var(--muted);font-size:11.5px;margin:24px 0 4px}

/* cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);
 padding:14px 16px;margin:0 0 16px;overflow-x:auto}
.card.flush{padding:0}
.card > .hd,.card > summary{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:-14px -16px 12px;padding:11px 16px;
 border-bottom:1px solid var(--border);font-weight:600}
.card.flush > .hd,.card.flush > summary{margin:0}
.card > .hd .end,.card > summary .end{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.card .bd{padding:12px 16px}
details.card > summary{cursor:pointer;list-style:none;margin-bottom:0;border-bottom:0}
details.card[open] > summary{border-bottom:1px solid var(--border);margin-bottom:12px}
details.card.flush[open] > summary{margin-bottom:0}
details.card > summary::-webkit-details-marker{display:none}
details.card > summary::after{content:"▾";color:var(--muted);margin-left:auto}
details.card[open] > summary::after{content:"▴"}
details.card > summary .end{margin-left:auto} details.card > summary .end + *{margin-left:0}

/* chips, tags, banners */
.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;background:var(--surface);
 border:1px solid var(--border);font-size:12.5px;white-space:nowrap;color:var(--text)}
.chip i{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block;flex:none}
.chip.ok i{background:var(--up);box-shadow:0 0 0 3px var(--up-bg)} .chip.bad i{background:var(--dn);box-shadow:0 0 0 3px var(--dn-bg)}
.chip.amber i{background:var(--warn)} .chip.mode{border-color:var(--gold);color:var(--gold);font-weight:700}
.chip.mode.live{border-color:var(--live);color:var(--dn)}
.chips .updated{margin-left:auto;color:var(--muted);font-size:12px}
.pulse{animation:pulse 2s infinite} @keyframes pulse{50%{opacity:.35}}
.tag{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:99px;background:var(--surface-2);color:var(--muted);
 border:1px solid var(--border);font-weight:600;font-family:var(--font);white-space:nowrap;vertical-align:middle}
.tag.up{color:var(--up);background:var(--up-bg);border-color:transparent}
.tag.dn{color:var(--dn);background:var(--dn-bg);border-color:transparent}
.tag.warn{color:var(--warn);background:var(--warn-bg);border-color:transparent}
.banner{background:var(--banner-bg);color:var(--banner-fg);border:1px solid color-mix(in srgb,var(--warn) 45%,transparent);
 border-radius:10px;padding:10px 14px;margin:0 0 14px} .banner a{color:inherit;font-weight:600}
.viewbar{border-radius:10px;padding:8px 14px;font-weight:700;font-size:12.5px;letter-spacing:.6px;margin:0 0 14px}
.viewbar.paper{background:var(--warn-bg);color:var(--gold);border:1px solid color-mix(in srgb,var(--gold) 45%,transparent)}
.viewbar.zerodha{background:var(--dn-bg);color:var(--dn);border:2px solid var(--live)}
.card.acct.paper{outline:2px solid color-mix(in srgb,var(--gold) 55%,transparent);outline-offset:-1px}
.card.acct.zerodha{outline:2px solid var(--live);outline-offset:-1px}

/* key figures */
.kpis{display:flex;flex-wrap:wrap;gap:12px 22px}
.kpis > div{min-width:0} .kpis > div b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.kgrid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:0 0 16px}
.kgrid.k4{grid-template-columns:repeat(4,minmax(0,1fr))}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;box-shadow:var(--shadow);min-width:0}
.kpi label{display:block;color:var(--muted);font-size:12px}
.kpi .v{font-size:21px;font-weight:650;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .v small{font-size:13px;color:var(--muted);font-weight:500}
.kpi .s{font-size:12px;margin-top:2px;color:var(--muted)}
.meter{height:6px;border-radius:99px;background:var(--surface-2);margin-top:8px;overflow:hidden}
.meter span{display:block;height:100%;border-radius:99px;background:var(--up)}
.meter.mid span{background:var(--warn)} .meter.high span{background:var(--dn)}

/* tables */
table{border-collapse:collapse;width:100%}
th{font-size:12px;color:var(--muted);font-weight:600;text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap;vertical-align:top}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right}
table.wrap td{white-space:normal}
tbody tr:hover td,table tr:hover td{background:color-mix(in srgb,var(--surface-2) 55%,transparent)}
.st{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--surface-2);color:var(--muted);white-space:nowrap}
.st.sig{background:var(--warn-bg);color:var(--warn)} .st.in{color:var(--info)} .st.up{background:var(--up-bg);color:var(--up)}
.st.dn{background:var(--dn-bg);color:var(--dn)}

/* buttons and forms */
.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:40px;padding:8px 14px;border-radius:9px;
 border:0;cursor:pointer;font:650 14px var(--font);text-decoration:none;
 background:linear-gradient(180deg,var(--gold),var(--accent));color:var(--accent-ink);margin:4px 6px 4px 0}
.btn:hover,button:hover{filter:brightness(1.06);text-decoration:none}
button.ghost,.btn.ghost{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
button.red,.btn.red{background:var(--dn-bg);color:var(--dn);border:1px solid color-mix(in srgb,var(--dn) 45%,transparent)}
button.small,.btn.small{min-height:30px;padding:4px 10px;font-size:12.5px}
.opts{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 8px}
.opt{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border:1px solid var(--border);border-radius:9px;background:var(--surface-2);
 cursor:pointer;min-height:40px} .opt:has(input:checked){border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
input[type=number],input[type=search],select{background:var(--surface);color:var(--text);border:1px solid var(--border);
 border-radius:8px;padding:6px 8px;font:inherit;min-height:36px}

/* links rows like 'Day: 11-16 11-17' and tab bars */
.days a{color:var(--accent);margin-right:10px;text-decoration:none} .days a.on{color:var(--text);font-weight:700;text-decoration:underline}
.ptabs{display:flex;gap:4px;flex-wrap:wrap;margin:0 0 14px;border-bottom:1px solid var(--border)}
.ptabs a,.ptabs button{background:none;border:0;border-radius:0;color:var(--muted);padding:9px 14px;margin:0 0 -1px;min-height:40px;
 font:600 13.5px var(--font);text-decoration:none;cursor:pointer;border-bottom:2px solid transparent}
.ptabs a.on,.ptabs button.on{color:var(--gold);border-bottom-color:var(--accent)}
.ptabs a:hover,.ptabs button:hover{color:var(--text);filter:none;text-decoration:none}

/* phone bottom navigation */
.bottom{display:none}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}

/* ---------- tablet ---------- */
@media (max-width:1100px){
  .grid{grid-template-columns:1fr}
  .kgrid{grid-template-columns:repeat(3,minmax(0,1fr))}
  .tabs a small{display:none}
}
/* ---------- phone ---------- */
@media (max-width:760px){
  body{font-size:14.5px}
  .top-in{padding:8px 14px;gap:10px}
  .brand small,.tabs,.top .right .seg{display:none}
  .phone-seg{display:block;padding:10px 14px 0}
  .phone-seg .seg{display:flex;width:100%} .phone-seg .seg a{flex:1;min-height:42px}
  .wrap{padding:12px 14px calc(84px + env(safe-area-inset-bottom))}
  h1{font-size:19px}
  .chips{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin-left:-14px;margin-right:-14px;padding:0 14px 2px}
  .chips::-webkit-scrollbar{display:none}
  .chips .updated{display:none}
  .kgrid,.kgrid.k4{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .kpi{padding:10px 12px} .kpi .v{font-size:18px}
  .card{padding:12px 14px} .card > .hd,.card > summary{margin:-12px -14px 12px;padding:10px 14px}
  .card.flush > .hd,.card.flush > summary{margin:0}
  th,td{padding:7px 8px}
  .hide-s{display:none}
  /* tables marked .rt turn into one card per row */
  table.rt,table.rt tbody,table.rt tr,table.rt td{display:block;width:100%}
  table.rt tr:first-child:has(th),table.rt thead{display:none}
  table.rt tr{border-bottom:1px solid var(--border);padding:8px 0}
  table.rt tr:last-child{border-bottom:0}
  table.rt td{border:0;padding:3px 0;display:flex;justify-content:space-between;gap:12px;white-space:normal;text-align:right}
  table.rt td::before{content:attr(data-label);color:var(--muted);font-size:12px;text-align:left;flex:none;max-width:45%}
  table.rt td.first{font-size:15px;text-align:left} table.rt td.first::before{display:none}
  table.rt tr:hover td{background:none}
  .bottom{display:grid;grid-template-columns:repeat(4,1fr);position:fixed;left:0;right:0;bottom:0;z-index:12;
   background:color-mix(in srgb,var(--surface) 94%,transparent);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);
   border-top:1px solid var(--border);padding:6px 4px calc(6px + env(safe-area-inset-bottom))}
  .bottom a{display:flex;flex-direction:column;align-items:center;gap:2px;text-decoration:none;color:var(--muted);font-size:11px;
   min-height:48px;justify-content:center;border-radius:10px}
  .bottom a span{font-size:19px;line-height:1}
  .bottom a.on{color:var(--gold);background:var(--surface-2)}
  .bottom a:hover{text-decoration:none}
}
@media print{.top,.bottom,.phone-seg,.icon-btn{display:none} .card{box-shadow:none;break-inside:avoid}}
"""

# runs in <head> before the page is drawn, so there is no flash of the wrong theme
HEAD_JS = ("(function(){try{var t=localStorage.getItem('" + THEME_KEY + "');"
           "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t)}catch(e){}})();")

THEME_JS = """
(function(){
  var KEY='""" + THEME_KEY + """', root=document.documentElement, btn=document.getElementById('themeBtn');
  function current(){var t=root.getAttribute('data-theme');
    if(t)return t; return matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}
  function paint(){var t=current(); if(btn){btn.textContent=t==='light'?'☾':'☀';
    btn.setAttribute('aria-label',t==='light'?'Switch to the dark theme':'Switch to the light theme');
    btn.title=btn.getAttribute('aria-label');}
    document.dispatchEvent(new CustomEvent('themechange'));}
  if(btn)btn.addEventListener('click',function(){var t=current()==='light'?'dark':'light';
    root.setAttribute('data-theme',t); try{localStorage.setItem(KEY,t)}catch(e){} paint();});
  try{matchMedia('(prefers-color-scheme: light)').addEventListener('change',paint)}catch(e){}
  paint();
})();
"""


def css():
    return _tokens() + BASE_CSS


def nav_links(active, view, cls):
    """Page tabs (desktop) or bottom navigation (phone). The current account view carries over."""
    out = []
    for path, key, icon, short in NAV:
        href = path + (f"?view={view}" if view and path in VIEW_PAGES else "")
        on = " on" if path == active else ""
        cur = " aria-current='page'" if path == active else ""
        if cls == "tabs":
            out.append(f"<a class='{on.strip()}' href='{href}'{cur} title='{html.escape(T.term(key))}'><b>{short}</b>"
                       f"<small>{html.escape(T.name(key), quote=False)}</small></a>")
        else:
            out.append(f"<a class='{on.strip()}' href='{href}'{cur} title='{html.escape(T.term(key))}'>"
                       f"<span aria-hidden='true'>{icon}</span>{short}</a>")
    return "".join(out)


def shell(title, active, body, view=None, switch="", extra_head=""):
    """The whole page: header with brand, page tabs, account switch and theme switch; the body;
    footer; and the phone's bottom navigation."""
    head = (f"<header class='top'><div class='top-in'>"
            f"<a class='brand' href='/' aria-label='{T.APP} home'><span class='logo' aria-hidden='true'>{T.BOW}</span>"
            f"<span><b>{T.APP}</b><small>{T.DEVANAGARI} · NSE intraday ORB</small></span></a>"
            f"<nav class='tabs' aria-label='Pages'>{nav_links(active, view, 'tabs')}</nav>"
            f"<div class='right'>{switch}"
            f"<button type='button' class='icon-btn' id='themeBtn' aria-label='Switch theme'>☀</button></div>"
            f"</div><div class='ribbon'></div></header>")
    phone_switch = f"<div class='phone-seg'>{switch}</div>" if switch else ""
    foot = (f"<footer>{T.APP} · {T.DEVANAGARI} · {T.term('paper')} trading by default · "
            f"software only, not investment advice</footer>")
    bottom = f"<nav class='bottom' aria-label='Pages'>{nav_links(active, view, 'bottom')}</nav>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
            f"<meta name='theme-color' content='{T.PALETTES['dark']['bg']}'>"
            f"<title>{html.escape(title)} · {T.APP}</title>"
            f'<link rel="icon" href="{T.FAVICON}">'
            f"<script>{HEAD_JS}</script><style>{css()}</style>{extra_head}</head>"
            f"<body data-view='{html.escape(view or '')}'><a class='skip' href='#main'>Skip to content</a>{head}{phone_switch}"
            f"<main class='wrap' id='main'>{body}{foot}</main>{bottom}"
            f"<script>{THEME_JS}</script></body></html>")
