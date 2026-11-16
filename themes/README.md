# Dashboard themes

A theme changes **how the dashboard looks**: colours, shapes, the extra names shown next to the plain trading
terms, the bot avatar, the header scene and the page icons. It never changes the bot, its settings, the trading
mode, orders or account data.

Pick a theme in the dashboard under **Settings** (the gear button at the top right, or `/settings`). The choice
is saved in this browser only, as a cookie named `ayodhya-ui-theme`, and applies on the next page with no
restart. An unknown or missing theme id falls back to **Default**. Light and dark mode (the ☀ / ☾ button) work
with every theme.

| Display name | Internal id | Style |
|---|---|---|
| Default | `default` | The original Ayodhya ORB look (night sky, saffron and gold, Ramayana names). |
| Playground | `roblox_inspired` | Bright, rounded, game-lobby style with a robot helper, Bolt. |
| Sandbox | `minecraft_inspired` | Square, pixel-block style with a block robot, Cubit, and a pixel landscape. |

The internal ids name the *kind* of game the style is inspired by. No theme uses a game's names, logos,
characters, textures or art. Everything in `assets/` is original SVG made for this project.

## Folder layout

```
themes/
  README.md          this file
  registry.json      theme ids, display names, folders and the default id
  <id>/
    theme.json       id, name, description, and which asset files it uses
    tokens.css       colour tokens (dark and light) plus shape and type tokens
    strings.json     themed names ("terms"), page icons ("icons") and short texts ("ui")
    assets/          avatar.svg, scene.svg, skin.css, icons/*.svg   (Default has none)
```

The loader is `uitheme.py`. `theme.py` and `webui.py` read the theme in use through it. If the themes folder
can't be read at all, the dashboard and the bot fall back to the built-in Default values in `theme.py`.

## tokens.css

There are three blocks. The loader turns them into the page's CSS custom properties, keeping the dashboard's
dark/light rules: dark by default, light when the system prefers light, and the ☀ / ☾ button wins.

```css
:root[data-mode=dark]  { --bg: #0b1330; --surface: ...; ... }   /* colours, dark mode */
:root[data-mode=light] { --bg: #e9f3ff; ... }                   /* colours, light mode */
:root[data-mode=any]   { --radius: 16px; --font: ...; ... }     /* shape and type (both modes) */
```

| Token | Used for |
|---|---|
| `bg`, `surface`, `surface-2`, `border` | page, cards, inner panels, lines |
| `text`, `muted` | body text, secondary text |
| `accent`, `accent-ink` | links and main buttons, text on main buttons |
| `gold` | headings and themed names |
| `up`, `dn`, `up-bg`, `dn-bg` | profit and loss (always shown with ▲▼ and + / − as well, never colour alone) |
| `warn`, `warn-bg`, `info` | warnings, notes |
| `trail` | EMA trail line on charts |
| `live` | the real-money (Zerodha) view marker |
| `banner-bg`, `banner-fg` | notice banners |
| `shadow` | card shadow |
| `radius`, `btn-radius`, `card-border-width` | shapes |
| `font`, `serif` | body font, heading/brand font (no web fonts: system fonts only) |
| `btn-bg` | main button fill |

Text colours must reach WCAG AA (4.5:1) on `bg`, `surface` and `surface-2` in both modes. `tests/test_themes.py`
checks this.

## strings.json

```json
{
  "terms": { "trades": "Quest Log", "stop": "Safety Line", ... },
  "icons": { "scroll": "file:assets/icons/scroll.svg", ... },
  "ui":    { "brand_sub": "Playground · NSE intraday ORB", "empty_positions": "...", ... }
}
```

- **terms**: the themed name shown **next to** the plain term, e.g. `Quest Log · Trades`, `Safety Line · stop`,
  `Practice · PAPER`. The plain term is added by the page code and is the same in every theme. Never write a
  name that turns a loss into a win, hides an error, or makes real money look like a game. For `live` use
  plain wording (the game themes use "Real Money").
- **icons**: an emoji, or `file:assets/icons/<name>.svg`. An SVG icon is defined once per page as a CSS mask
  and drawn in the text colour, so only its shape matters and repeating it in every table row stays small.
- **ui**: short texts (brand line, footer, empty states, Settings intro, chart colour words).

Any key a theme leaves out is taken from Default. The tests list missing keys, and each theme should have all
of them.

## Adding or editing a theme

1. Copy a game theme folder to `themes/<new_id>/` (`a-z`, `0-9` and `_` only) and edit its files.
2. Add `{"id": "<new_id>", "name": "<Display name>", "dir": "<new_id>"}` to `registry.json`.
3. Keep `tokens.css` plus `assets/` under 100 KB, with no animation and no external URLs (`url()` only with
   `{{avatar}}`, `{{scene}}` or `data:`).
4. Run `.venv\Scripts\python.exe -m pytest tests\test_themes.py`. It checks keys, contrast, size, no
   animation, no network references, and that every page shows the same numbers in every theme.
5. Restart the dashboard. Theme files are read once at start-up.

**Don't change the Default theme's values.** Default must look exactly like the original dashboard.

## What themes never change

Prices, P&L amounts and signs, the PAPER / LIVE wording and its banners, order and position status words,
stop and target values, errors, and the confirmation dialogs are the same plain text in every theme. The bot's
event log (`status.json` events, log files) is stored data and always keeps its original wording.
