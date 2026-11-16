# Dashboard themes: implementation report

Branch `feature/dashboard-themes`, created from `main` at v1.7.0 (eac7864). Changes are **uncommitted** for review.

The branch lives in its own worktree, `D:\08-Code-Projects\Trading\KiteConnect\kite-trader-themes`. The bot's own
folder (`kite-trader`) stays on `main`, so the scheduled 09:00 bot runs unchanged code.

Inputs that were left to fill in:

| Placeholder | Filled from the code |
|---|---|
| Dashboard | `web.py` (pages and routes), `webui.py` (page frame), `theme.py` (names and icons) |
| Stack | Plain Python `http.server`, server-rendered HTML, inline CSS and JS, no framework |
| Settings page | No settings page existed, so a new `/settings` page was added |

## 1. Files added and changed

**Added**

| Path | Why |
|---|---|
| `uitheme.py` | The theme loader. It reads `themes/registry.json` and each theme folder, fills missing keys from Default, picks the theme for one request from the cookie (unknown → Default), and builds the CSS tokens and preview CSS. Display only; it reads only `themes/`. |
| `themes/README.md` | How themes work, the file formats, and how to add or edit a theme. |
| `themes/registry.json` | Theme ids, display names, folders and the default id. |
| `themes/default/theme.json`, `tokens.css`, `strings.json`, `assets/README.txt` | Default = the original values, written out from v1.7.0's `theme.py`. A test checks they are identical. |
| `themes/roblox_inspired/…` | Playground: `theme.json`, `tokens.css`, `strings.json`, `assets/avatar.svg`, `assets/scene.svg`, `assets/skin.css`, `assets/icons/*.svg` (8 icons). All original art. |
| `themes/minecraft_inspired/…` | Sandbox: the same set of files, in a pixel style. All original art. |
| `tests/test_themes.py` | 44 tests: keys and fallback, Default equality, plain finance wording, the data comparison on every page, contrast, size, motion and network, the Settings cookie, and keyboard structure. |
| `docs/design/themes/` | This report, `contrast.md`, the test output, the Default pixel check and the screenshots. |

**Changed** (`git diff --stat`: CHANGELOG 30 lines, theme.py 81, web.py 120, webui.py 41)

| Path | What changed and why |
|---|---|
| `theme.py` | `name()`, `term()`, the page icons (`BOW`, `SCROLL`, …, through a module `__getattr__`) and a new `ui()` now come from the theme in use. Outside a page request (the bot, the console, logs) everything stays Default. The original values stay as built-in fallbacks, so a broken `themes/` folder can never stop the bot. Event-log icons (`ENTER_ICON`, …) are constants because they are stored data. |
| `webui.py` | The page frame uses the theme's tokens and skin CSS, avatar (Default keeps the 🏹 logo), brand line, footer text, favicon and nav icons. Adds the Settings gear button and `empty()`, which shows the avatar beside empty-state text in game themes; Default output is unchanged. |
| `web.py` | Display text only: 3 empty states, the Trades subtitle, the scanner's "Research only" flavour clause, and 2 chart colour words now come from `strings.json`. Adds the `/settings` page (`render_settings`, `SETTINGS_CSS`, preview tiles) and a `do_GET` wrapper that picks the theme from the cookie for that request. `_settings()` sets the cookie. **No POST handler, control action, Kite call or data function changed** (checked in the diff: the hunks are at the lines listed in section 6). |
| `CHANGELOG.md` | An "Unreleased" entry for the branch. |

Not changed: strategy, risk, broker, bot loop, config, accounts, journal, capital, reports and scheduling.
`DRY_RUN`, `CONFIRM_LIVE` and every live/paper switch are untouched.

## 2. Screenshots

The screenshots are in `docs/design/themes/screens/`. Each name follows `<theme>-<page>-<mode>.png`:
- themes: `default`, `playground`, `sandbox`;
- pages: `live` (the dashboard), `trades`, `settings`;
- modes: `dark`, `light`.

They show the same sample data, with the clock frozen at 17 Nov 2026 12:30 IST, at 1366 px wide.

## 3. Test output

- Full suite: **248 passed** (`test_output_all.txt`), the original 204 plus 44 new.
- Theme tests: **44 passed** (`test_output_themes.txt`, with the name of every test).

What the acceptance checks show:

| Check | Result | Evidence |
|---|---|---|
| Every page renders in all three themes with no console errors | **PASS.** 10 pages × 3 themes = 30 page loads in the browser, 0 errors: Live, Zerodha view, the four Trades tabs (overview, journal, log, backtest), Review, Scanner, Trade chart and Settings. The console log was proven to carry across navigations, so nothing was missed. | Browser pane console read |
| Every string and token key exists in every theme, with fallback to Default | **PASS.** 0 missing keys in each theme. With keys deleted from a copy, they are filled from Default and listed. | `test_every_theme_has_every_string_and_token_key`, `test_missing_keys_fall_back_to_default` |
| The same data renders identical numbers, signs, currency, times and status words | **PASS** on 9 pages and every `/api/live` region. Each page's visible text (scripts, styles and SVG removed) is reduced to its numbers (with sign, ₹, % and R), ▲▼ arrows, times and status words, and compared across themes in page order. Every chart's `data-chart` JSON is compared too. Planting a number in one theme's name makes the comparison fail, so it does detect differences. | `test_same_data_same_numbers_signs_and_status_words_in_every_theme`, `test_live_api_regions_…` |
| Default screenshots match the baseline | **PASS, pixel-identical within the stated tolerance.** See section 4. | `default_pixel_check.txt` |
| Contrast ratios for each theme | **PASS for Playground and Sandbox** in both modes. Default has one pre-existing failure. See section 5. | `contrast.md` |
| Keyboard navigation | **PASS.** Tab reaches the skip link, brand, page tabs, Settings, light/dark and then the theme radios. Arrow keys change the theme, which applies at once, and focus returns to the chosen radio after saving. Focus outline is visible. | Browser pane; `test_settings_page_is_keyboard_friendly` |
| Reduced motion | **PASS.** The only animation in any theme is the existing pulsing "market open" dot. Across three headless shots it changes with normal motion and stays still with `--force-prefers-reduced-motion`, in all three themes. Theme files contain no `@keyframes`, `animation` or `transition`. | `motion_check.txt`; `test_no_animation_…` |
| No extra polling or network requests | **PASS.** On page load the browser made 0 extra requests in every theme (`performance` resource list empty). Art is inline. Theme files contain no URLs (the SVG namespace aside). No new polling: the Live page's existing 10-second refresh is unchanged. | Browser pane; `test_no_animation_no_network_…`, `test_theme_pages_load_nothing_from_elsewhere` |
| Bundle size per theme | Decorative files (`tokens.css` + `assets/`): **Default 1,715 B, Playground 6,787 B, Sandbox 9,931 B**, all under the 100 KB limit. `strings.json` is 1.9 to 2.1 KB each. | `test_decorative_assets_stay_under_100_kb` |
| Page weight (everything is inline, one request per page) | See the table below. Game themes add the theme CSS and the icon shapes once per page, and each repeated icon is a 45-byte `<span>` (a CSS mask). | `curl` against the preview |
| UI-only preference | **PASS.** `/settings?ui_theme=<id>` only sets the `ayodhya-ui-theme` cookie and redirects. The data folder is byte-identical before and after, and `DRY_RUN`, `CONFIRM_LIVE`, `WATCHLIST` and risk settings are unchanged. Unknown ids store `default`. | `test_choosing_a_theme_sets_only_a_browser_cookie` |
| Applies without a restart | **PASS.** The next page renders in the chosen theme. | `test_the_chosen_theme_applies_on_the_next_page_without_a_restart` |

Page weight in bytes, same sample data:

| Page | main (v1.7.0) | Default | Playground | Sandbox |
|---|---|---|---|---|
| Live `/` | 51,852 | 52,071 (+219: the Settings button and 3 new tokens) | 61,589 | 67,443 |
| Trades `/trades` (83 backtest rows) | 144,707 | 144,926 | 159,009 | 164,708 |
| Settings `/settings` | (new) | 32,549 | 42,029 | 47,897 |

## 4. Default theme vs baseline

**Method:**
- Ran `main` (v1.7.0) and the branch side by side, on the same copy of the data, with the clock frozen at 2026-11-17 12:30 IST.
- Took headless Chrome screenshots at 1366×2400 of 9 pages (`settings` is new) in dark and light, forced with `--blink-settings=preferredColorScheme=0/1`.

**Tolerance**, the only areas excluded from the comparison:
- **The header's right-hand controls** (x 980–1366, y 0–66), where the new Settings button was added. It moves the Paper | Zerodha switch 48 px left.
- **On the Live and Zerodha pages only, the "Updated hh:mm:ss" label and the pulsing market-open dot.** These run on the browser's real clock, and they also differ between two runs of the unchanged `main` code (measured: 280 px, bounding box x 32–1280, y 140–154).

**Result: 18 of 18 screenshots are pixel-identical outside those two areas.** That is 9 pages (Live, Zerodha view,
Trades overview, journal, log and backtest tabs, Review, Scanner, Trade chart) in dark and light.

| Page | Dark, masked | Light, masked | Raw difference (all inside the masks) |
|---|---|---|---|
| live | 0 px | 0 px | header right + clock row (y 13–154) |
| zerodha | 0 px | 0 px | header right + clock row (y 13–154) |
| trades, journal, log, backtest | 0 px | 0 px | header right only (x 995–1288, y 13–53) |
| review, scanner, journey | 0 px | 0 px | the gear button only (x 1250–1288, y 14–52) |

Full numbers are in `default_pixel_check.txt`. The `settings` page is new, so it has no baseline.

## 5. Contrast (WCAG AA)

Every text token (`text`, `muted`, `up`, `dn`, `gold`, `accent`, `warn`, `info`) was checked against `bg`,
`surface` and `surface-2`, in dark and light, for every theme. Also checked: text on main buttons (both ends of
the gradient), banner text, white on the live marker, and the chart trail line (3:1). The full table of 174
ratios is in `contrast.md`.

Lowest text ratio per theme and mode:

| Theme | Dark | Light |
|---|---|---|
| Default | 5.95 | 4.62 |
| Playground | 4.70 | 4.89 |
| Sandbox | 4.83 | 5.17 |

**Pre-existing issue in Default, not fixed because Default must not change:** the white text on the dark-mode
red "Zerodha Account" button is **3.76:1**, below 4.5. It comes from the original v1.7.0 design. Both new themes
pass this pair: Playground 4.70, Sandbox 4.83. A one-token fix to Default (`--live` in dark) is a possible
follow-up.

## 6. Trading-adjacent text kept deliberately plain

These are the same in every theme. Where a themed name appears, it sits *beside* the unchanged plain term, never instead of it.

| Text | Why it stays plain |
|---|---|
| **PAPER / LIVE wording**: mode chip "Bot trading: … · PAPER", "PAPER ACCOUNT · virtual money", "ZERODHA ACCOUNT · real money · read-only", the "real money · read-only" tag, the footer "… trading by default". The themed name for live is "Real Money" in both game themes (no game word). | Live vs paper must be unmistakable. |
| **Confirmation dialogs**: switching to the Zerodha view, Pause, Stop bot, and changing the opening balance. The wording is identical. Only the mode name inside "(Practice · PAPER)" changes, and the test compares everything else. | Risk and confirmation prompts must keep their meaning. |
| **Buttons that act**: "Stop bot", "Resume new entries", "Log in to Zerodha", and the opening-balance buttons. "Pause new entries" keeps its plain text with a themed prefix (Default "Vishram ·", game themes "Break ·"), the same pattern as before. | Actions must say what they do. |
| **Numbers**: prices, P&L, ₹ amounts, R values, percentages, ▲▼ and + / − signs, quantities, times and dates. | They are data, and the comparison test proves they are identical. |
| **Position and order status words**: LONG/SHORT, ON TRACK, AT RISK, NEAR STOP, PASSED, TRAIL, stop and target values, "% away". | Status must read the same everywhere. |
| **Errors and warnings**: data errors, "Stale: last update …", the offline banner, login errors, "Scan failed", "Research only … Check the chart yourself before acting". Only a flavour clause in the last one is themed; its warning sentence is unchanged. | Errors must never be hidden or softened. |
| **Watchlist and skip reasons** ("no fresh breakout", "daily loss limit 2% hit", …). Themed names are only added beside risk phrases, as Default already did. | They are strategy output. |
| **The bot's event log** ("ENTER LONG TCS … Lakshman Rekha (stop) …"). | It is stored data (`status.json` and log files), written by the bot outside any page request, so it keeps the original wording whatever the theme. |
| **Zerodha read-only account figures**, **trade log CSV and journal exports**, **saved weekly and monthly HTML reports** (`reports.py` has its own styling) and **console output**. | Data and files, not the dashboard's look. |
| **"software only, not investment advice"** in the footer. | A disclaimer. |
| **The Settings page's description of what a theme can't change.** | It is about safety. |

## 7. Blockers and notes

- **`web.py` mixes page rendering with bot controls**: POST `/pause`, `/resume`, `/stop` and `/paper-capital`, the Kite login callback, `/api/*`.
  - I did **not** split it. Edits are limited to display strings, the new `/settings` page and route, and a small `do_GET` wrapper that picks the theme for the request.
  - The diff hunks are at lines 599, 610, 869, 912, 1239, 1261 and 1349 (text), 1385 (new Settings code) and 1404/1449 (the GET wrapper and the route).
  - `do_POST`, `_paper_capital` and every control and data function are unchanged.
- **`theme.py` is also imported by the bot and console tools.**
  - Their output is unchanged: Default outside a page request, and event icons are constants.
  - A broken `themes/` folder falls back to the built-in values instead of crashing the bot.
- **Internal ids** `roblox_inspired` and `minecraft_inspired` are used exactly as the brief specifies. They appear only in the cookie value, folder names and page source (radio values, CSS class names), never in displayed text; a test checks this. If you'd rather not have game names even there, renaming the ids is a one-line registry change plus a folder rename.
- **Art:** all original SVG. The avatars are a rounded robot head (Bolt) and a 16×16 pixel robot (Cubit). The scenes are clouds with coloured platforms, and generic pixel hills with stone and a sun; no game's textures. The icons are simple line or pixel glyphs.
- **Fonts:** system fonts only (no web fonts, no downloads). Sandbox uses a monospace font for headings and names only. Numbers keep the normal font, so nothing is truncated; this was checked: 0 clipped values on Live and Trades in each theme.
- **Not done:** no commit or push, as asked. There is no release or version bump, and the main `README.md` is unchanged (the details are in `themes/README.md`).
