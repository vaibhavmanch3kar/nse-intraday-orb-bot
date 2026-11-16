"""
Ramayana theme: every themed name, icon and colour in one place.

Rule: a themed name always sits next to the plain trading term ("Lakshman Rekha · stop"),
so the meaning is never lost. Only what people *see* is themed. Data files (trade CSVs,
status.json, state files) and the trading logic keep plain values.

Glossary
    Ayodhya            the bot (home kingdom)            Ayodhya Darbar    Live page
    Chitrakoot         Session review (reflection)       Hanuman's Watch   Swing scanner (scouting)
    Rajkosh            Trades (the royal treasury)       Yatra             one trade's chart (journey)
    Valmiki's Vision   Time Machine (the seer)           Agni Pariksha     backtest / 30-trade edge check
    Lakshman Rekha     stop-loss (the line not crossed)  Lakshya           target
    Muhurat            time stop (the window passed)     Sandhya           square-off (dusk)
    Maryada            daily loss limit (Rama's limits)  Vanvas            cool-down after a loss streak (exile)
    Vanar Sena         index filter (move with the army) Pushpaka          Kite real-time data (the swift chariot)
    Setu               Zerodha login / static-IP bridge  Abhyas / Yuddha   paper / LIVE mode
    Vishram            pause                             Sandesh           recent events (messages)
    Panchang           NSE calendar                      Utsav             market holiday
    Aadesh             controls                          Jagrat / Nidra    running / not running
    Guru Vashishtha    learning advisor (counsels; the king decides)
"""

APP = "Ayodhya ORB"
TAGLINE = "NSE intraday opening-range breakout bot · Ramayana edition"
DEVANAGARI = "॥ अयोध्या ॥"

# Page icons: BOW, FLAG, LAMP, SCROLL, PATH, FIRE, SEER. Read through the dashboard theme in use
# (theme.BOW etc. still work, see __getattr__ below); these are the Default theme's built-in values,
# also used by the console, the bot's event log and as the fallback if the themes folder can't be read.
ICONS = {"bow": "🏹", "flag": "🚩", "lamp": "🪔", "scroll": "📜", "path": "👣", "fire": "🔥", "seer": "🔮", "gear": "⚙"}
_ICON_ATTRS = {"BOW": "bow", "FLAG": "flag", "LAMP": "lamp", "SCROLL": "scroll", "PATH": "path", "FIRE": "fire",
               "SEER": "seer", "GEAR": "gear"}
# event-log icons are data (saved in status.json and logs), so they never change with the dashboard theme
ENTER_ICON, EXIT_WIN, EXIT_LOSS, WARN_ICON = "🏹", "🏆", "🛡", "⚠"

# key -> (Default theme's themed name, plain term). The plain term is shared by every theme and never
# changes; the themed name comes from the theme in use (themes/<id>/strings.json "terms"), these are the
# built-in Default values (tests check they match themes/default/strings.json).
TERMS = {
    "live_page": ("Ayodhya Darbar", "Live"),
    "review": ("Chitrakoot", "Session review"),
    "scanner": ("Hanuman's Watch", "Swing scanner"),
    "trades": ("Rajkosh", "Trades"),
    "journey": ("Yatra", "Trade chart"),
    "time_machine": ("Valmiki's Vision", "Time Machine"),
    "backtest": ("Agni Pariksha", "Backtest"),
    "edge": ("Agni Pariksha", "30-trade edge check"),
    "stop": ("Lakshman Rekha", "stop"),
    "target": ("Lakshya", "target"),
    "time_stop": ("Muhurat", "time stop"),
    "square_off": ("Sandhya", "square-off"),
    "daily_loss": ("Maryada", "daily loss limit"),
    "cooldown": ("Vanvas", "cool-down"),
    "index_filter": ("Vanar Sena", "index filter"),
    "trail": ("Anusaran", "EMA trail"),
    "kite_data": ("Pushpaka", "Kite real-time"),
    "login": ("Setu", "Zerodha login"),
    "paper": ("Abhyas", "PAPER"),
    "live": ("Yuddha", "LIVE"),
    "pause": ("Vishram", "paused"),
    "events": ("Sandesh", "Recent events"),
    "calendar": ("Panchang", "NSE calendar"),
    "holiday": ("Utsav", "NSE holiday"),
    "controls": ("Aadesh", "Controls"),
    "running": ("Jagrat", "RUNNING"),
    "not_running": ("Nidra", "NOT RUNNING"),
    "watchlist": ("Sena", "Watchlist"),
    "positions": ("Rann", "Open positions"),
    "advisor": ("Guru Vashishtha", "Advisor"),
}

# dashboard tabs: path -> TERMS key, icon
PAGES = [("/", "live_page", ICONS["bow"]), ("/review", "review", ICONS["lamp"]),
         ("/scanner", "scanner", ICONS["flag"]), ("/trades", "trades", ICONS["scroll"])]


try:  # dashboard themes (look only). Any problem loading them leaves the built-in Default values above.
    import uitheme as _ui
except Exception:  # noqa: BLE001 - a broken theme file must never stop the bot or the console tools
    _ui = None


def _theme():
    return _ui.active() if _ui else None


def __getattr__(attr):
    """theme.BOW / theme.SCROLL / ...: the page icon of the dashboard theme in use (Default outside a page request)."""
    key = _ICON_ATTRS.get(attr)
    if key is None:
        raise AttributeError(attr)
    t = _theme()
    return t.icon_markup.get(key, ICONS[key]) if t else ICONS[key]


# Short UI texts of the Default theme (built-in fallback; same values as themes/default/strings.json "ui")
UI = {
    "brand_sub": DEVANAGARI + " · NSE intraday ORB",
    "footer_motto": DEVANAGARI,
    "empty_positions": "No open positions",
    "empty_equity": "The chart fills in while the bot runs today (from the daily journal).",
    "empty_trades": "No trades yet - the bot records them here once it trades on a market day.",
    "settings_page": "Settings",
    "settings_theme_intro": "Choose how the dashboard looks. This only changes colours, names and pictures in this browser.",
    "trades_sub": "The royal treasury's ledger: every closed trade, after Zerodha charges.",
    "scanner_scout": "Hanuman scouts",
    "band_colour": "saffron",
    "trail_colour": "violet",
}


def ui(key):
    """A short display text of the theme in use (brand line, empty states, Settings texts)."""
    t = _theme()
    return (t.strings["ui"].get(key) if t else None) or UI[key]


def theme_id():
    t = _theme()
    return t.id if t else "default"


def avatar():
    """The theme's bot avatar (inline SVG), or '' for the Default emoji logo. Decorative only."""
    t = _theme()
    return t.avatar if t else ""


def name(key):
    """Themed name of the theme in use ('Lakshman Rekha' in Default)."""
    t = _theme()
    return (t.strings["terms"].get(key) if t else None) or TERMS[key][0]


def plain(key):
    return TERMS[key][1]


def term(key, sep=" · "):
    """'Lakshman Rekha · stop' - the themed name with the plain term."""
    return f"{name(key)}{sep}{TERMS[key][1]}"


def mode_label(mode):
    """'Abhyas · PAPER' or 'Yuddha · LIVE'."""
    return term("live" if str(mode).lower() == "live" else "paper")


def reason_label(reason):
    """Plain exit reason from the trade log -> themed label that still contains the plain text.
    'stop (gap)' -> 'Lakshman Rekha · stop (gap)', 'time stop' -> 'Muhurat · time stop'."""
    r = (reason or "").strip()
    low = r.lower()
    if low.startswith("time stop"):
        key = "time_stop"
    elif "stop" in low:
        key = "stop"
    elif low.startswith("target"):
        key = "target"
    elif low.startswith("square-off"):
        key = "square_off"
    elif "trail" in low or "ema" in low:
        key = "trail"
    else:
        return r or "-"
    return f"{name(key)} · {r}"


_DECOR = [  # plain phrase inside a status/skip reason -> themed prefix (plain text stays)
    ("daily loss limit", "daily_loss"),
    ("cooldown", "cooldown"),
    ("against index", "index_filter"),
    ("(paused)", "pause"),
]


def decorate(text):
    """Add the themed name in front of known risk/filter phrases in a status line.
    'LONG breakout - skip: daily loss limit 2% hit' -> '... skip: Maryada · daily loss limit 2% hit'."""
    out = text or ""
    for phrase, key in _DECOR:
        i = out.find(phrase)
        if i >= 0:
            if phrase.startswith("("):
                out = out[:i] + f"({name(key)} · {phrase[1:]}" + out[i + len(phrase):]
            else:
                out = out[:i] + f"{name(key)} · " + out[i:]
    return out


def banner(subtitle, width=64):
    """Console banner, ASCII borders so every Windows console shows it."""
    line = "=" * width
    return "\n".join([line, f"  {ICONS['bow']} {APP.upper()}  |  {subtitle}", f"  {TAGLINE}", line])


# ── Dashboard palette: night sky over Ayodhya, saffron and gold ─────────────
COLORS = {
    "bg": "#1b1022",      # deep plum night
    "card": "#2a1a31",
    "fg": "#f6ead8",      # ivory
    "muted": "#bba78f",
    "line": "#4a3352",
    "up": "#4ade80",      # profit stays green, loss stays red - clarity first
    "dn": "#f87171",
    "acc": "#f59e0b",     # saffron
    "gold": "#fcd34d",
    "warn": "#fb923c",
    "trail": "#c084fc",   # EMA trail on charts
    "banner_bg": "#3b1d0a",
    "banner_fg": "#fde68a",
}

# Default theme colours (CSS custom properties); the same values are in themes/default/tokens.css.
# Dark keeps the night-sky palette; light is ivory and saffron.
# Profit is green and loss red in both, always with ▲▼ / +− as well, so colour is never the only signal.
PALETTES = {
    "dark": {
        "bg": "#130a18", "surface": "#1d1224", "surface-2": "#271a30", "border": "#36263f",
        "text": "#f5ead9", "muted": "#b3a191", "accent": "#f59e0b", "accent-ink": "#241000",
        "gold": "#fcd34d", "up": "#34d399", "dn": "#f87171", "warn": "#fbbf24", "info": "#c4b5fd",
        "trail": "#c084fc", "live": "#ef4444", "up-bg": "rgba(52,211,153,.13)", "dn-bg": "rgba(248,113,113,.13)",
        "warn-bg": "rgba(251,191,36,.12)", "banner-bg": "#3b1d0a", "banner-fg": "#fde68a",
        "shadow": "0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px rgba(0,0,0,.25)",
    },
    "light": {
        "bg": "#f7f2ea", "surface": "#ffffff", "surface-2": "#f8f1e6", "border": "#e6dac8",
        "text": "#2a1f17", "muted": "#6c5e50", "accent": "#c2410c", "accent-ink": "#ffffff",
        "gold": "#915a00", "up": "#047857", "dn": "#b91c1c", "warn": "#915a00", "info": "#6d28d9",
        "trail": "#7c3aed", "live": "#b91c1c", "up-bg": "#e7f6ef", "dn-bg": "#fdecec",
        "warn-bg": "#fdf3dc", "banner-bg": "#fdf3dc", "banner-fg": "#7c2d12",
        "shadow": "0 1px 2px rgba(60,40,20,.06), 0 6px 18px rgba(60,40,20,.06)",
    },
}

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
           "<text y='.9em' font-size='90'>%F0%9F%8F%B9</text></svg>")
