"""Dashboard themes (Default, Playground, Sandbox): look only.

Checks: the registry and folders; every key in every theme (with fallback to Default); Default equals the
original built-in values; the same data renders the same numbers, signs, currency, times and status words in
every theme; WCAG AA contrast; size budget; no animation and no network references; the Settings cookie;
and that themes never touch the bot's files or settings."""

import csv
import html
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.request

import pytest

import accounts
import config as C
import theme as T
import uitheme as U
import web
import webui

IDS = ["default", "roblox_inspired", "minecraft_inspired"]
NAMES = {"default": "Default", "roblox_inspired": "Playground", "minecraft_inspired": "Sandbox"}


# ── registry, folders, keys ─────────────────────────────────────────────────
def test_registry_ids_names_and_default():
    assert U.ids() == IDS
    assert {t.id: t.name for t in U.THEMES.values()} == NAMES
    assert U.REGISTRY["default"] == "default"
    for tid in IDS:
        folder = os.path.join(U.ROOT, tid)
        for f in ("theme.json", "tokens.css", "strings.json"):
            assert os.path.isfile(os.path.join(folder, f)), (tid, f)
        assert os.path.isdir(os.path.join(folder, "assets"))


def test_every_theme_has_every_string_and_token_key():
    base = U.THEMES["default"]
    for t in U.THEMES.values():
        assert t.missing == [], (t.id, t.missing)
        for mode in ("dark", "light", "any"):
            assert set(t.tokens[mode]) == set(base.tokens[mode]), (t.id, mode)
        for sec in ("terms", "icons", "ui"):
            assert set(t.strings[sec]) == set(base.strings[sec]), (t.id, sec)
    assert set(base.strings["terms"]) == set(T.TERMS)
    assert set(base.strings["ui"]) == set(T.UI)


def test_missing_keys_fall_back_to_default(tmp_path, monkeypatch):
    root = tmp_path / "themes"
    shutil.copytree(U.ROOT, root)
    s = json.loads((root / "roblox_inspired" / "strings.json").read_text(encoding="utf-8"))
    del s["terms"]["stop"], s["ui"]["empty_positions"], s["icons"]["gear"]
    (root / "roblox_inspired" / "strings.json").write_text(json.dumps(s), encoding="utf-8")
    tok = (root / "roblox_inspired" / "tokens.css").read_text(encoding="utf-8")
    (root / "roblox_inspired" / "tokens.css").write_text(re.sub(r"\n  --muted: [^;]+;", "", tok, count=1), encoding="utf-8")
    monkeypatch.setattr(U, "ROOT", str(root))
    _, themes = U.load()
    t, d = themes["roblox_inspired"], themes["default"]
    assert t.strings["terms"]["stop"] == d.strings["terms"]["stop"] == "Lakshman Rekha"
    assert t.strings["ui"]["empty_positions"] == "No open positions"
    assert t.icon_markup["gear"] == T.ICONS["gear"]
    assert t.tokens["dark"]["muted"] == d.tokens["dark"]["muted"]
    assert {"strings.terms.stop", "strings.ui.empty_positions", "strings.icons.gear", "tokens.dark.muted"} <= set(t.missing)


@pytest.mark.parametrize("cookie", [None, "", "ayodhya-ui-theme=nope", "ayodhya-ui-theme=../etc", "garbage;;=",
                                    "ayodhya-ui-theme=ROBLOX_INSPIRED"])
def test_unknown_or_missing_theme_id_falls_back_to_default(cookie):
    assert U.from_cookie(cookie) == "default"


def test_known_cookie_picks_the_theme():
    assert U.from_cookie("x=1; ayodhya-ui-theme=minecraft_inspired") == "minecraft_inspired"


# ── Default is the original look ────────────────────────────────────────────
def test_default_theme_files_equal_the_original_values():
    d = U.THEMES["default"]
    assert d.tokens["dark"] == T.PALETTES["dark"] and d.tokens["light"] == T.PALETTES["light"]
    assert d.strings["terms"] == {k: v[0] for k, v in T.TERMS.items()}
    assert d.strings["ui"] == T.UI and d.strings["icons"] == T.ICONS
    assert d.avatar == "" and d.skin_css == ""


def test_default_page_frame_is_unchanged_apart_from_the_settings_link():
    page = webui.shell("Live", "/", "<p>x</p>")
    assert "<span class='logo' aria-hidden='true'>🏹</span>" in page
    assert "॥ अयोध्या ॥ · NSE intraday ORB" in page
    assert ":root{--bg:#130a18;" in page and "--radius:12px;" in page
    assert "class='empty-art'" not in page
    assert page.count("href='/settings'") == 1


def test_outside_a_page_request_everything_is_default():
    tok = U.activate("roblox_inspired")
    assert T.term("trades") == "Quest Log · Trades"
    U.reset(tok)
    assert T.term("trades") == "Rajkosh · Trades" and T.BOW == "🏹"
    assert T.ENTER_ICON == "🏹" and T.EXIT_WIN == "🏆"   # event-log icons are data and never themed


# ── finance meaning is never replaced ───────────────────────────────────────
@pytest.mark.parametrize("tid", IDS)
def test_themed_names_always_keep_the_plain_term(tid):
    tok = U.activate(tid)
    try:
        for key, (_, plain) in T.TERMS.items():
            assert T.term(key).endswith(" · " + plain), (tid, key)
        assert T.mode_label("paper").endswith(" · PAPER") and T.mode_label("live").endswith(" · LIVE")
        for reason in ("stop", "stop (gap)", "target", "time stop", "square-off", "trail EMA21"):
            assert reason in T.reason_label(reason)
        assert "daily loss limit 2% hit" in T.decorate("skip: daily loss limit 2% hit")
    finally:
        U.reset(tok)


def test_game_names_never_dress_up_live_trading_or_losses():
    banned = re.compile(r"\b(win|won|victory|level up|bonus|loot|reward|score|play|game|xp|coins?)\b", re.I)
    for tid in ("roblox_inspired", "minecraft_inspired"):
        t = U.THEMES[tid]
        for key in ("live", "paper", "stop", "target", "daily_loss", "cooldown", "positions", "login"):
            assert not banned.search(t.strings["terms"][key]), (tid, key, t.strings["terms"][key])
        assert t.strings["terms"]["live"] == "Real Money"


def test_no_trademarked_names_in_anything_shown():
    for tid in IDS:
        t = U.THEMES[tid]
        shown = json.dumps([t.name, t.description, t.strings]) + t.avatar + t.scene + t.skin_css
        shown = shown.replace(tid, "")  # the internal id itself is allowed (spec)
        assert not re.search(r"roblox|minecraft|mojang|creeper|steve|noob|builderman|bloxburg", shown, re.I), tid


# ── data renders identically in every theme ─────────────────────────────────
REVIEW = {"day": "2026-11-16", "days": ["2026-11-13", "2026-11-16"], "built": "2026-11-16T16:00", "signals": 1, "wins": 1,
          "total_r": 0.65, "settings": {"TARGET_R": 0},
          "rows": {"ITC": {"closes": [284.5, 290.1, 295.6], "or_low": 284.0, "or_high": 286.75, "day_change_pct": 3.78,
                           "direction": "LONG", "r": 0.65, "signal_time": "09:30", "entry": 290.85, "stop": 284.0,
                           "target": None, "exit_reason": "square-off", "exit_time": "15:00", "exit": 295.6},
                   "SBIN": {"closes": [1090, 1089], "or_low": 1083.9, "or_high": 1093.5, "day_change_pct": -0.1,
                            "note": "inside range"}}}
SCAN = {"day": "2026-11-16", "built": "2026-11-16T16:00", "counts": {"20-day high breakout": 1}, "errors": [],
        "rows": {"TCS": {"setups": ["20-day high breakout"], "closes": [3300, 3350, 3410], "close": 3410.0,
                         "change_pct": 1.2, "rsi": 64, "above_200dma": True, "from_52w_high_pct": -3.3,
                         "stop": 3330.0, "target": 3570.0, "qty": 6}}}
TRADES = [
    {"opened": "2026-11-13 09:45:00+05:30", "closed": "2026-11-13 10:45:00+05:30", "symbol": "ITC", "direction": "LONG",
     "qty": "62", "entry": "291.90", "exit": "294.30", "stop": "284.00", "reason": "time stop", "charges": "19.29",
     "net": "129.51", "r": "0.26"},
    {"opened": "2026-11-16 10:00:00+05:30", "closed": "2026-11-16 11:15:00+05:30", "symbol": "SBIN", "direction": "SHORT",
     "qty": "20", "entry": "1090.00", "exit": "1094.50", "stop": "1098.00", "reason": "stop", "charges": "21.10",
     "net": "-111.10", "r": "-0.69"},
]


@pytest.fixture
def server(monkeypatch):
    import datetime as dt
    import review
    import scanner
    monkeypatch.setattr(review, "latest", lambda *a, **k: REVIEW)
    monkeypatch.setattr(scanner, "latest", lambda *a, **k: SCAN)
    monkeypatch.setattr(web, "STATUS_FILE", C.DATA_DIR + "/status.json")
    monkeypatch.setattr(web, "login_status", lambda: (False, "Not logged in today"))
    monkeypatch.setattr(web, "login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=x&v=3")

    def no_kite():
        raise accounts.LoginNeeded("Not logged in to Zerodha today.")

    monkeypatch.setattr(accounts, "_kite_client", no_kite)
    os.makedirs(C.DATA_DIR, exist_ok=True)
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    status = {"time": dt.datetime.now(ist).isoformat(), "mode": "paper", "trades_today": 2, "day_pnl": -42.5,
              "equity": 99957.5, "data_lag_min": 3.0, "data_source": {"using": "yahoo"}, "paused": False,
              "positions": [{"symbol": "TCS", "direction": "LONG", "qty": 2, "entry": 3400.0, "stop": 3380.0,
                             "target": None, "price": 3390.0, "unrealised": -20.0, "r_unreal": -0.5, "held_min": 30,
                             "stop_dist_pct": 0.29, "opened": "x"}],
              "rows": {"TCS": {"close": 3390.0, "or_low": 3380.0, "or_high": 3398.0, "vwap": 3391.2, "vol_ratio": 1.4,
                               "reason": "LONG breakout - skip: daily loss limit 2% hit", "bar": "10:00"},
                       "ITC": {"close": 291.0, "or_low": 284.0, "or_high": 286.75, "reason": "data error: timed out",
                               "bar": "10:00"}},
              "events": ["10:00:31 🏹 ENTER LONG TCS x2 @ 3400.00 Lakshman Rekha (stop) 3380.00",
                         "10:20:02 ⚠ data error: connection to the data server timed out"]}
    with open(os.path.join(C.DATA_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f)
    for name in ("trades_paper.csv", "backtest_trades.csv"):
        with open(os.path.join(C.DATA_DIR, name), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(TRADES[0]))
            w.writeheader()
            w.writerows(TRADES)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch(url, theme=None, follow=True):
    req = urllib.request.Request(url, headers={"Cookie": f"{U.COOKIE}={theme}"} if theme else {})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req) as r:
            return r.status, r.read().decode("utf-8-sig"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


PAGES = ["/", "/?view=zerodha", "/trades", "/trades?tab=log", "/trades?view=zerodha", "/review", "/scanner",
         "/journey", "/settings"]
_STATUS_WORDS = (r"NOT RUNNING|RUNNING|PAUSED|PAPER|LIVE|LONG|SHORT|ON TRACK|AT RISK|NEAR STOP|PASSED|"
                 r"Stale|data error|timed out|Not logged in today|daily loss limit|stop \(gap\)|square-off|time stop|"
                 r"target|Market open|Market closed|virtual money|real money|read-only")
_TOKEN = re.compile(r"[▲▼]|[+\-−]?₹?\d[\d,]*(?:\.\d+)?(?:%|R|x)?|" + _STATUS_WORDS)


def data_tokens(page):
    """Numbers (with sign, ₹, %, R), arrows, times and status words in the visible text, in page order,
    plus every chart's data attribute. Theme names, pictures and colours are not data."""
    charts = re.findall(r"data-chart='([^']*)'", page)
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    body = re.sub(r"<svg\b.*?</svg>", " ", body, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"<!doctype html>", " ", text, flags=re.I)
    # the live clock's seconds tick between two renders; they are not data (HH:MM is still compared)
    text = re.sub(r"\b(\d{1,2}:\d{2}):\d{2}\b", r"\1", text)
    return _TOKEN.findall(text), charts


def same_in_every_theme(get):
    """Render with every theme; retry (up to 3 times) when the clock changes minute between renders."""
    for _attempt in range(3):
        seen = {tid: get(tid) for tid in IDS}
        if all(v == seen["default"] for v in seen.values()):
            break
    return seen


@pytest.mark.parametrize("path", PAGES)
def test_same_data_same_numbers_signs_and_status_words_in_every_theme(server, path):
    def get(tid):
        code, page, _ = fetch(server + path, tid)
        assert code == 200, (tid, path, code)
        return data_tokens(page)
    seen = same_in_every_theme(get)
    base = seen["default"]
    assert base[0], path
    for tid in IDS[1:]:
        assert seen[tid] == base, (path, tid)


def test_live_api_regions_carry_identical_data_in_every_theme(server):
    def get(tid):
        code, body, _ = fetch(server + "/api/live?view=paper", tid)
        assert code == 200
        return {k: data_tokens(v) for k, v in json.loads(body)["regions"].items()}
    out = same_in_every_theme(get)
    assert out["roblox_inspired"] == out["default"] == out["minecraft_inspired"]


def test_errors_confirmations_and_mode_banners_read_the_same_in_every_theme(server):
    must = ["data error", "PAPER ACCOUNT", "virtual money", "Stop bot", "Not logged in today",
            "This shows your REAL Zerodha account", "Both ask before acting"]
    for tid in IDS:
        page = fetch(server + "/", tid)[1]
        for m in must:
            assert m in page, (tid, m)
        z = fetch(server + "/?view=zerodha", tid)[1]
        assert "real money" in z and "read-only" in z, tid
        # confirmations keep the same plain wording; only the themed name beside PAPER/LIVE differs
        tok = U.activate(tid)
        label = T.mode_label("paper")
        U.reset(tok)
        base = re.findall(r"confirm\('([^']*)'\)", fetch(server + "/", "default")[1])
        mine = re.findall(r"confirm\('([^']*)'\)", page)
        assert base and mine == [w.replace("Abhyas · PAPER", label) for w in base], tid


# ── look: contrast, motion, size, no network ────────────────────────────────
def _lum(h):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_TOKENS = ("text", "muted", "up", "dn", "gold", "accent", "warn", "info")
BACKGROUNDS = ("bg", "surface", "surface-2")


def contrast_table():
    rows = []
    for t in U.THEMES.values():
        for mode in ("dark", "light"):
            p = t.tokens[mode]
            for fg in TEXT_TOKENS:
                for bg in BACKGROUNDS:
                    rows.append((t.id, mode, fg, bg, round(contrast(p[fg], p[bg]), 2), 4.5))
            rows += [(t.id, mode, "accent-ink", "accent", round(contrast(p["accent-ink"], p["accent"]), 2), 4.5),
                     (t.id, mode, "accent-ink", "gold", round(contrast(p["accent-ink"], p["gold"]), 2), 4.5),
                     (t.id, mode, "banner-fg", "banner-bg", round(contrast(p["banner-fg"], p["banner-bg"]), 2), 4.5),
                     (t.id, mode, "#ffffff", "live", round(contrast("#ffffff", p["live"]), 2), 4.5),
                     (t.id, mode, "trail", "surface", round(contrast(p["trail"], p["surface"]), 2), 3.0)]
    return rows


# Known, pre-existing in the ORIGINAL design (v1.7.0) and kept because the Default theme must not change:
# white text on the dark-mode red "Zerodha Account" button is 3.76:1. Reported, not fixed here.
DEFAULT_KNOWN = {("default", "dark", "#ffffff", "live")}


def test_every_theme_meets_wcag_aa_in_both_modes():
    bad = [r for r in contrast_table() if r[4] < r[5] and r[:4] not in DEFAULT_KNOWN]
    assert not bad, bad
    known = [r for r in contrast_table() if r[:4] in DEFAULT_KNOWN]
    assert [r[4] for r in known] == [3.76]   # unchanged from the original


@pytest.mark.parametrize("tid", IDS)
def test_decorative_assets_stay_under_100_kb(tid):
    assert U.asset_bytes(U.THEMES[tid]) < 100 * 1024


@pytest.mark.parametrize("tid", IDS)
def test_no_animation_no_network_and_reduced_motion_kept(tid):
    t = U.THEMES[tid]
    files = [os.path.join(d, f) for d, _, fs in os.walk(t.folder) for f in fs]
    for path in files:
        text = open(path, encoding="utf-8").read()
        text_no_ns = text.replace("http://www.w3.org/2000/svg", "")   # SVG namespace: a name, not a download
        assert not re.search(r"https?://|@import|url\(\s*(?!['\"]?(?:\{\{|data:))", text_no_ns), path
        assert not re.search(r"@keyframes|animation\s*:|transition\s*:", text), path
    tok = U.activate(tid)
    try:
        css = webui.css()
    finally:
        U.reset(tok)
    assert "@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important" in css


def test_theme_pages_load_nothing_from_elsewhere(server):
    for tid in IDS:
        page = fetch(server + "/settings", tid)[1]
        assert not re.search(r"<(script|link|img)[^>]+(src|href)=['\"]https?://", page), tid


# ── Settings: UI-only preference, no restart ────────────────────────────────
def snapshot():
    out = {}
    for root, _, files in os.walk(C.DATA_DIR):
        for name in files:
            p = os.path.join(root, name)
            out[p] = open(p, "rb").read()
    return out


def test_settings_shows_three_themes_with_previews(server):
    page = fetch(server + "/settings")[1]
    for tid, name in NAMES.items():
        assert f"value='{tid}'" in page and f">{name}" in page and f"pv-{tid}" in page
    assert page.count("type='radio' name='ui_theme'") == 3 and "checked" in page
    assert "Light or dark" in page and U.COOKIE in page


@pytest.mark.parametrize("asked, stored", [("minecraft_inspired", "minecraft_inspired"), ("roblox_inspired", "roblox_inspired"),
                                           ("evil<script>", "default"), ("ROBLOX_INSPIRED", "default")])
def test_choosing_a_theme_sets_only_a_browser_cookie(server, asked, stored):
    before = snapshot()
    settings_before = {k: getattr(C, k) for k in ("DRY_RUN", "CONFIRM_LIVE", "WATCHLIST", "RISK_PER_TRADE_PCT")}
    code, _, headers = fetch(server + "/settings?ui_theme=" + urllib.request.quote(asked), follow=False)
    assert code == 303 and headers["Location"] == "/settings?saved=1"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{U.COOKIE}={stored};") and "Path=/" in cookie and "SameSite=Lax" in cookie
    assert snapshot() == before
    assert {k: getattr(C, k) for k in settings_before} == settings_before


def test_the_chosen_theme_applies_on_the_next_page_without_a_restart(server):
    assert "Quest Log · Trades" in fetch(server + "/trades", "roblox_inspired")[1]
    assert "Ledger · Trades" in fetch(server + "/trades", "minecraft_inspired")[1]
    assert "Rajkosh · Trades" in fetch(server + "/trades")[1]
    assert "Rajkosh · Trades" in fetch(server + "/trades", "unknown_theme")[1]


def test_settings_page_is_keyboard_friendly(server):
    page = fetch(server + "/settings", "minecraft_inspired")[1]
    assert "<a class='skip' href='#main'>" in page
    assert re.search(r"<label class='tpick'><span class='tn'><input type='radio'", page)   # native radios
    assert '<legend class="sr">Dashboard theme</legend>' in page
    assert "aria-label='Settings (dashboard theme)'" in page and "aria-current='page'" in page
    assert "<button type=\"submit\">Apply theme</button>" in page
