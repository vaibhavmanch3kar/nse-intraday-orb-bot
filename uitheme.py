"""
Dashboard themes (look only): loads themes/registry.json and each theme folder, and picks the theme
for one page request from the browser's ui-theme cookie.

DISPLAY ONLY. Nothing here reads or writes the bot's settings, trading mode, account data or any file
outside themes/. A theme changes colours, shapes, themed names, pictures and icons. The plain trading
words (PAPER / LIVE, stop, target, P&L, errors, confirmations) are shared by all themes and live in the
page code, not in themes.

A theme folder has:
    theme.json    id, display name, description, the asset files it uses
    tokens.css    colour tokens for dark and light mode, plus shape and type tokens
    strings.json  themed names ("terms"), page icons ("icons") and short UI texts ("ui")
    assets/       original SVG art (avatar, scene, icons) and an optional skin.css
Any key a theme leaves out falls back to the Default theme. An unknown or missing theme id falls back
to Default too.
"""

import contextvars
import json
import os
import re
from http.cookies import SimpleCookie
from urllib.parse import quote

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes")
COOKIE = "ayodhya-ui-theme"
DEFAULT = "default"
_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_VAR_RE = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")
_BLOCK_RE = re.compile(r":root\[data-mode=(dark|light|any)\]\s*\{([^}]*)\}", re.S)

_active = contextvars.ContextVar("ui_theme", default=DEFAULT)


class Theme:
    def __init__(self, tid, name, description, folder, meta):
        self.id, self.name, self.description, self.folder, self.meta = tid, name, description, folder, meta
        self.tokens = {"dark": {}, "light": {}, "any": {}}
        self.strings = {"terms": {}, "icons": {}, "ui": {}}
        self.avatar = ""      # inline SVG markup ('' = the Default emoji logo)
        self.scene = ""       # inline SVG markup for the header strip / empty states
        self.skin_css = ""    # extra CSS (decorative), with {{avatar}} / {{scene}} replaced by data URIs
        self.icon_markup = {}  # icon key -> HTML (emoji text or <span class='ico'><svg ...></span>)
        self.missing = []     # keys this theme left out (filled from Default)

    def term_name(self, key):
        return self.strings["terms"][key]

    def ui(self, key):
        return self.strings["ui"][key]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def parse_tokens(css):
    out = {"dark": {}, "light": {}, "any": {}}
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for mode, body in _BLOCK_RE.findall(css):
        for k, v in _VAR_RE.findall(body):
            out[mode][k] = v.strip()
    return out


def data_uri(svg):
    # used inside double quotes (CSS url("...") and HTML href="..."): only " # % and the like need escaping
    return "data:image/svg+xml," + quote(" ".join(svg.split()), safe=" =:/',.()-;<>")


def _load_one(entry):
    folder = os.path.join(ROOT, entry["dir"])
    meta = json.loads(_read(os.path.join(folder, "theme.json")))
    t = Theme(entry["id"], entry["name"], meta.get("description", ""), folder, meta)
    t.tokens = parse_tokens(_read(os.path.join(folder, "tokens.css")))
    s = json.loads(_read(os.path.join(folder, "strings.json")))
    for sec in ("terms", "icons", "ui"):
        t.strings[sec] = dict(s.get(sec) or {})
    assets = meta.get("assets") or {}
    if assets.get("avatar"):
        t.avatar = _read(os.path.join(folder, assets["avatar"])).strip()
    if assets.get("scene"):
        t.scene = _read(os.path.join(folder, assets["scene"])).strip()
    if assets.get("skin_css"):
        css = _read(os.path.join(folder, assets["skin_css"]))
        t.skin_css = (css.replace("{{avatar}}", data_uri(t.avatar) if t.avatar else "")
                      .replace("{{scene}}", data_uri(t.scene) if t.scene else ""))
    return t


ICON_CSS = ".ico{display:inline-block;width:1em;height:1em;vertical-align:-.15em;background-color:currentColor}"


def _icon(theme, key, value):
    """An icon value -> (HTML, CSS). 'file:assets/icons/x.svg' becomes a tiny <span> whose shape is the SVG,
    defined once per page as a CSS mask in the text colour (so an icon repeated in every table row costs a few
    bytes); anything else (an emoji) is used as text."""
    if isinstance(value, str) and value.startswith("file:"):
        uri = data_uri(_read(os.path.join(theme.folder, value[5:])).strip())
        css = f".ico-{key}{{mask:url(\"{uri}\") center/contain no-repeat}}"
        return f"<span class='ico ico-{key}' aria-hidden='true'></span>", css
    return value, ""


LOAD_ERRORS = []   # themes that could not be read (skipped; Default is required)


def load():
    reg = json.loads(_read(os.path.join(ROOT, "registry.json")))
    themes = {}
    for entry in reg["themes"]:
        if not _ID_RE.match(entry["id"]):
            LOAD_ERRORS.append(f"bad theme id {entry['id']!r}")
            continue
        try:
            themes[entry["id"]] = _load_one(entry)
        except (OSError, ValueError) as e:  # a broken optional theme is skipped, never fatal
            if entry["id"] == reg.get("default", DEFAULT):
                raise
            LOAD_ERRORS.append(f"{entry['id']}: {e}")
    reg = dict(reg, themes=[e for e in reg["themes"] if e["id"] in themes])
    base = themes[reg.get("default", DEFAULT)]
    # fill every missing key from Default, and record what was missing
    for t in themes.values():
        for mode in ("dark", "light", "any"):
            for k, v in base.tokens[mode].items():
                if k not in t.tokens[mode]:
                    t.tokens[mode][k] = v
                    t.missing.append(f"tokens.{mode}.{k}")
        for sec in ("terms", "icons", "ui"):
            for k, v in base.strings[sec].items():
                if k not in t.strings[sec]:
                    t.strings[sec][k] = v
                    t.missing.append(f"strings.{sec}.{k}")
        icon_css = []
        for k, v in t.strings["icons"].items():
            t.icon_markup[k], css = _icon(t if f"strings.icons.{k}" not in t.missing else base, k, v)
            icon_css.append(css)
        if any(icon_css):
            t.skin_css += ICON_CSS + "".join(icon_css)
    return reg, themes


REGISTRY, THEMES = load()


def _signature():
    """(path, size, modified time) of every file under themes/: changes when any theme file changes."""
    out = []
    for d, _, files in os.walk(ROOT):
        for f in files:
            try:
                st = os.stat(os.path.join(d, f))
                out.append((os.path.join(d, f), st.st_size, st.st_mtime_ns))
            except OSError:
                pass
    return tuple(sorted(out))


_SIG = _signature()
_checked = 0.0


def maybe_reload(min_interval=2.0):
    """Read the theme files again if any changed on disk (looked at no more than every `min_interval` s),
    so a theme edit shows on the next page without restarting the dashboard. A broken edit is ignored:
    the themes already loaded stay in use and the problem is listed in LOAD_ERRORS. Returns True if
    the themes were reloaded."""
    global REGISTRY, THEMES, _SIG, _checked
    import time
    now = time.monotonic()
    if now - _checked < min_interval:
        return False
    _checked = now
    sig = _signature()
    if sig == _SIG:
        return False
    _SIG = sig
    errors_before = len(LOAD_ERRORS)
    try:
        reg, themes = load()
    except Exception as e:  # Default itself unreadable: keep what we have
        LOAD_ERRORS.append(f"reload failed, kept the loaded themes: {e}")
        return False
    if len(LOAD_ERRORS) > errors_before:   # a theme is broken right now (half-saved edit?): keep the old set
        LOAD_ERRORS.append("reload skipped, kept the loaded themes")
        return False
    REGISTRY, THEMES = reg, themes
    return True


def ids():
    return [e["id"] for e in REGISTRY["themes"]]


def get(tid):
    return THEMES.get(tid) or THEMES[DEFAULT]


def from_cookie(header):
    """Theme id from a Cookie header; unknown or missing -> 'default'."""
    try:
        c = SimpleCookie()
        c.load(header or "")
        v = c[COOKIE].value if COOKIE in c else ""
    except Exception:  # a malformed cookie header must never break a page
        v = ""
    return v if v in THEMES else DEFAULT


def activate(tid):
    """Use theme `tid` for the rest of this request. Returns a token for reset()."""
    return _active.set(tid if tid in THEMES else DEFAULT)


def reset(token):
    _active.reset(token)


def active():
    return THEMES.get(_active.get()) or THEMES[DEFAULT]   # a theme removed by a reload falls back


def _vars(d):
    return "".join(f"--{k}:{v};" for k, v in d.items())


def tokens_css(t=None):
    """The :root custom properties for theme t in the dashboard's dark/light structure:
    dark by default, light when the system prefers light (unless the dark button was chosen),
    and the ☀/☾ button's choice (data-theme on <html>) wins."""
    t = t or active()
    dark, light, shape = t.tokens["dark"], t.tokens["light"], t.tokens["any"]
    alias = "--card:var(--surface);--fg:var(--text);--line:var(--border);--acc:var(--accent);"
    return (f":root{{{_vars(dark)}{alias}{_vars(shape)}color-scheme:dark}}"
            f"@media (prefers-color-scheme:light){{:root:not([data-theme=dark]){{{_vars(light)}color-scheme:light}}}}"
            f":root[data-theme=light]{{{_vars(light)}color-scheme:light}}"
            f":root[data-theme=dark]{{{_vars(dark)}color-scheme:dark}}")


def preview_css(t, scope):
    """The theme's tokens scoped to one element (Settings preview tiles), following dark/light the same way."""
    dark, light, shape = t.tokens["dark"], t.tokens["light"], t.tokens["any"]
    return (f"{scope}{{{_vars(dark)}{_vars(shape)}}}"
            f"@media (prefers-color-scheme:light){{:root:not([data-theme=dark]) {scope}{{{_vars(light)}}}}}"
            f":root[data-theme=light] {scope}{{{_vars(light)}}}"
            f":root[data-theme=dark] {scope}{{{_vars(dark)}}}")


def asset_bytes(t):
    """Bytes of a theme's decorative files: tokens.css plus everything under assets/."""
    n = os.path.getsize(os.path.join(t.folder, "tokens.css"))
    adir = os.path.join(t.folder, "assets")
    for d, _, files in os.walk(adir):
        for f in files:
            n += os.path.getsize(os.path.join(d, f))
    return n
