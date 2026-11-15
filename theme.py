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
"""

APP = "Ayodhya ORB"
TAGLINE = "NSE intraday opening-range breakout bot · Ramayana edition"
DEVANAGARI = "॥ अयोध्या ॥"

# icons (kept few and simple so they read well in logs and consoles)
BOW, FLAG, LAMP, SCROLL, PATH, FIRE, SEER = "🏹", "🚩", "🪔", "📜", "👣", "🔥", "🔮"
ENTER_ICON, EXIT_WIN, EXIT_LOSS, WARN_ICON = "🏹", "🏆", "🛡", "⚠"

# key -> (themed name, plain term)
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
}

# dashboard tabs: path -> TERMS key, icon
PAGES = [("/", "live_page", BOW), ("/review", "review", LAMP),
         ("/scanner", "scanner", FLAG), ("/trades", "trades", SCROLL)]


def name(key):
    return TERMS[key][0]


def plain(key):
    return TERMS[key][1]


def term(key, sep=" · "):
    """'Lakshman Rekha · stop' - the themed name with the plain term."""
    themed, p = TERMS[key]
    return f"{themed}{sep}{p}"


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
    return "\n".join([line, f"  {BOW} {APP.upper()}  |  {subtitle}", f"  {TAGLINE}", line])


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

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
           "<text y='.9em' font-size='90'>%F0%9F%8F%B9</text></svg>")
