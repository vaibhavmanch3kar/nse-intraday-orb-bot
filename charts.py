"""
Small dependency-free SVG charts for the dashboard (equity curve, P&L bars, trade journey).

Every function returns an <svg> string that scales to its container (viewBox + width 100%).
Colours come from theme.py (Ramayana palette: saffron opening range, violet EMA trail;
profit stays green and loss red).
"""

import html

import theme

_c = theme.COLORS
UP, DN, ACC, MUTED, LINE, WARN, FG = _c["up"], _c["dn"], _c["acc"], _c["muted"], _c["line"], _c["trail"], _c["fg"]
BG = _c["bg"]


def _fmt_inr(v):
    """An amount such as an equity balance: ₹1,234 / -₹1,234."""
    sign = "-" if v < 0 else ""
    return f"{sign}₹{abs(v):,.0f}"


def _fmt_pnl(v, nd=0):
    """A profit or loss: ▲ ₹1,234 / ▼ ₹1,234 / ● ₹0 - an arrow and the absolute amount, never a +/- sign."""
    x = round(v, nd)
    return f"{'▲' if x > 0 else '▼' if x < 0 else '●'} ₹{abs(v):,.{nd}f}"


def _scale(lo, hi, top, bottom):
    span = (hi - lo) or 1.0
    return lambda v: bottom - (v - lo) / span * (bottom - top)


def _svg(w, h, body, label):
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' preserveAspectRatio='none' role='img' "
            f"aria-label='{html.escape(label)}' style='max-height:{h}px;display:block'>{body}</svg>")


def intraday(points, start=None, w=800, h=200, title="Equity during the day"):
    """Today's equity line from the journal's samples [(HH:MM:SS, equity)]. Colours come from the page's
    theme (CSS variables), so it reads in both light and dark."""
    if len(points) < 2:
        return "<div class='muted'>Not enough data yet.</div>"
    vals = [v for _, v in points]
    base = start if start is not None else vals[0]
    lo, hi = min(vals + [base]), max(vals + [base])
    pad = (hi - lo) * 0.12 or max(abs(hi) * 0.001, 1)
    y = _scale(lo - pad, hi + pad, 10, h - 22)
    left, right = 66, 10
    x = lambda i: left + i * (w - left - right) / (len(points) - 1)  # noqa: E731
    col = "var(--up)" if vals[-1] >= base else "var(--dn)"
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"{x(0):.1f},{h - 22} {line} {x(len(vals) - 1):.1f},{h - 22}"
    parts = [f"<line x1='{left}' x2='{w - right}' y1='{y(base):.1f}' y2='{y(base):.1f}' style='stroke:var(--muted)' stroke-dasharray='4 4'/>",
             f"<polygon points='{area}' style='fill:{col}' opacity='0.12'/>",
             f"<polyline points='{line}' fill='none' style='stroke:{col}' stroke-width='2' stroke-linejoin='round'/>"]
    for v in (max(vals), min(vals)):
        parts.append(f"<text x='{left - 6}' y='{y(v) + 4:.1f}' font-size='12' text-anchor='end' style='fill:var(--muted)'>{_fmt_inr(v)}</text>")
    parts.append(f"<text x='{left}' y='{h - 5}' font-size='12' style='fill:var(--muted)'>{html.escape(points[0][0][:5])}</text>"
                 f"<text x='{w - right}' y='{h - 5}' font-size='12' text-anchor='end' style='fill:var(--muted)'>"
                 f"{html.escape(points[-1][0][:5])}</text>")
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' aria-label='{html.escape(title)}' "
            f"style='display:block;max-height:{h}px'>{''.join(parts)}</svg>")


def equity_curve(values, start, labels=None, w=900, h=220, title="Equity"):
    """Equity after each closed trade, starting at `start`. Shades drawdowns below the running peak.

    values: equity after trade 1..n. labels: optional text per point (shown as tooltips)."""
    if not values:
        return "<div class='muted'>No trades yet.</div>"
    pts = [start] + list(values)
    pad_l, pad_r, top, bottom = 64, 12, 12, h - 24
    lo, hi = min(pts), max(pts)
    margin = (hi - lo) * 0.08 or max(abs(hi) * 0.01, 1)
    y = _scale(lo - margin, hi + margin, top, bottom)
    step = (w - pad_l - pad_r) / max(1, len(pts) - 1)
    x = lambda i: pad_l + i * step  # noqa: E731
    parts = []
    # grid: start line + min/max labels (a label is skipped if it would overlap one already drawn)
    drawn = []
    for v, col, dash in ((start, MUTED, "4 4"), (hi, LINE, ""), (lo, LINE, "")):
        parts.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='{col}' "
                     f"stroke-width='1' stroke-dasharray='{dash}'/>")
        if all(abs(y(v) - d) > 14 for d in drawn):
            drawn.append(y(v))
            parts.append(f"<text x='{pad_l - 6}' y='{y(v) + 4:.1f}' fill='{MUTED}' font-size='11' text-anchor='end'>"
                         f"{_fmt_inr(v)}</text>")
    # drawdown shading: area between running peak and equity
    peak, top_pts, bot_pts = pts[0], [], []
    for i, v in enumerate(pts):
        peak = max(peak, v)
        top_pts.append(f"{x(i):.1f},{y(peak):.1f}")
        bot_pts.append(f"{x(i):.1f},{y(v):.1f}")
    parts.append(f"<polygon points='{' '.join(top_pts + bot_pts[::-1])}' fill='{DN}' opacity='0.18'/>")
    col = UP if pts[-1] >= start else DN
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))
    parts.append(f"<polyline points='{line}' fill='none' stroke='{col}' stroke-width='2'/>")
    # invisible hover targets with tooltips
    for i, v in enumerate(pts[1:], start=1):
        tip = f"Trade {i}: equity {_fmt_inr(v)}" + (f" · {labels[i - 1]}" if labels else "")
        parts.append(f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='4' fill='{col}' opacity='0'>"
                     f"<title>{html.escape(tip)}</title></circle>")
    parts.append(f"<text x='{pad_l}' y='{h - 6}' fill='{MUTED}' font-size='11'>trade 1</text>"
                 f"<text x='{w - pad_r}' y='{h - 6}' fill='{MUTED}' font-size='11' text-anchor='end'>"
                 f"trade {len(pts) - 1}</text>")
    return _svg(w, h, "".join(parts), title)


def pnl_bars(values, labels=None, w=900, h=180, title="P&L per trade"):
    """One bar per trade: green above zero, red below."""
    if not values:
        return "<div class='muted'>No trades yet.</div>"
    pad_l, pad_r, top, bottom = 64, 12, 10, h - 10
    m = max(abs(v) for v in values) or 1.0
    y = _scale(-m, m, top, bottom)
    slot = (w - pad_l - pad_r) / len(values)
    bw = max(1.0, slot * 0.7)
    parts = [f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y(0):.1f}' y2='{y(0):.1f}' stroke='{MUTED}' stroke-width='1'/>",
             f"<text x='{pad_l - 6}' y='{y(m) + 4:.1f}' fill='{MUTED}' font-size='11' text-anchor='end'>{_fmt_pnl(m)}</text>",
             f"<text x='{pad_l - 6}' y='{y(-m) + 4:.1f}' fill='{MUTED}' font-size='11' text-anchor='end'>{_fmt_pnl(-m)}</text>"]
    for i, v in enumerate(values):
        x0 = pad_l + i * slot + (slot - bw) / 2
        y0, y1 = sorted((y(0), y(v)))
        tip = f"Trade {i + 1}: {_fmt_pnl(v)}" + (f" · {labels[i]}" if labels else "")
        parts.append(f"<rect x='{x0:.1f}' y='{y0:.1f}' width='{bw:.1f}' height='{max(1.0, y1 - y0):.1f}' "
                     f"fill='{UP if v > 0 else DN}'><title>{html.escape(tip)}</title></rect>")
    return _svg(w, h, "".join(parts), title)


def _bar_index(bars, when):
    """The candle containing `when` (a close time on a bar boundary belongs to the bar before)."""
    if when is None:
        return None
    import datetime as _dt
    probe = when - _dt.timedelta(seconds=1)
    idx = 0
    for i, b in enumerate(bars):
        if b["t"] <= probe:
            idx = i
    return idx


def journey_data(bars, trade, ema=None, or_range=None):
    """The same trade picture as numbers, for the interactive candle chart (chartjs 'candles')."""
    lines = []
    if trade.get("stop"):
        lines.append({"n": "stop", "v": round(trade["stop"], 2), "c": "dn"})
    if trade.get("target"):
        lines.append({"n": "target", "v": round(trade["target"], 2), "c": "up"})
    if trade.get("entry"):
        lines.append({"n": "entry", "v": round(trade["entry"], 2), "c": "mu", "dash": "2 3"})
    up = trade.get("direction") == "LONG"
    marks = []
    for key, price_key, kind in (("opened", "entry", "entry"), ("closed", "exit", "exit")):
        i, px = _bar_index(bars, trade.get(key)), trade.get(price_key)
        if i is not None and px:
            marks.append({"i": i, "p": round(px, 2), "k": kind, "upward": (key == "opened") == up})
    return {"type": "candles", "h": 340,
            "bars": [[f"{b['t']:%H:%M}", b["o"], b["h"], b["l"], b["c"]] for b in bars],
            "ema": [None if e is None else round(e, 2) for e in ema] if ema else None,
            "band": [or_range[0], or_range[1]] if or_range and all(or_range) else None,
            "lines": lines, "marks": marks}


def journey(bars, trade, ema=None, or_range=None, w=900, h=320, title="Trade journey"):
    """Candles for one session with the trade drawn on top.

    bars: the day's 15-min bars (dicts t,o,h,l,c). trade: {direction, entry, exit, stop, target,
    opened, closed} where opened/closed are datetimes (or None). ema: list of EMA values aligned
    with bars (or None). or_range: (low, high) opening range."""
    if not bars:
        return "<div class='muted'>No price data for this day.</div>"
    levels = [b["h"] for b in bars] + [b["l"] for b in bars]
    for k in ("entry", "exit", "stop", "target"):
        if trade.get(k):
            levels.append(trade[k])
    if ema:
        levels += [e for e in ema if e is not None]
    lo, hi = min(levels), max(levels)
    pad_l, pad_r, top, bottom = 20, 110, 14, h - 26
    margin = (hi - lo) * 0.05 or 1
    y = _scale(lo - margin, hi + margin, top, bottom)
    slot = (w - pad_l - pad_r) / len(bars)
    cx = lambda i: pad_l + i * slot + slot / 2  # noqa: E731
    parts = []
    if or_range and all(or_range):
        parts.append(f"<rect x='{pad_l}' y='{y(or_range[1]):.1f}' width='{w - pad_l - pad_r}' "
                     f"height='{max(1.0, y(or_range[0]) - y(or_range[1])):.1f}' fill='{ACC}' opacity='0.10'>"
                     f"<title>Opening range {or_range[0]:,.2f} – {or_range[1]:,.2f}</title></rect>")

    def hline(v, col, label, dash="6 4"):
        parts.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='{col}' "
                     f"stroke-width='1.2' stroke-dasharray='{dash}'/>"
                     f"<text x='{w - pad_r + 4}' y='{y(v) + 4:.1f}' fill='{col}' font-size='11'>"
                     f"{html.escape(label)} {v:,.2f}</text>")

    if trade.get("stop"):
        hline(trade["stop"], DN, "stop")          # Lakshman Rekha
    if trade.get("target"):
        hline(trade["target"], UP, "target")      # Lakshya
    if trade.get("entry"):
        hline(trade["entry"], MUTED, "entry", "2 3")
    bw = max(2.0, slot * 0.6)
    for i, b in enumerate(bars):
        col = UP if b["c"] >= b["o"] else DN
        parts.append(f"<line x1='{cx(i):.1f}' x2='{cx(i):.1f}' y1='{y(b['h']):.1f}' y2='{y(b['l']):.1f}' "
                     f"stroke='{col}' stroke-width='1'/>")
        y0, y1 = sorted((y(b["o"]), y(b["c"])))
        tip = f"{b['t']:%H:%M}  O {b['o']:,.2f} H {b['h']:,.2f} L {b['l']:,.2f} C {b['c']:,.2f}"
        parts.append(f"<rect x='{cx(i) - bw / 2:.1f}' y='{y0:.1f}' width='{bw:.1f}' height='{max(1.0, y1 - y0):.1f}' "
                     f"fill='{col}'><title>{tip}</title></rect>")
    if ema:
        pts = [f"{cx(i):.1f},{y(e):.1f}" for i, e in enumerate(ema) if e is not None]
        if len(pts) > 1:
            parts.append(f"<polyline points='{' '.join(pts)}' fill='none' stroke='{WARN}' stroke-width='1.5'>"
                         f"<title>EMA trail</title></polyline>")

    def idx_for(when):
        """The candle containing `when` (a close time on a bar boundary belongs to the bar before)."""
        if when is None:
            return None
        import datetime as _dt
        probe = when - _dt.timedelta(seconds=1)
        idx = 0
        for i, b in enumerate(bars):
            if b["t"] <= probe:
                idx = i
        return idx

    up = trade.get("direction") == "LONG"
    for key, price_key, label, col in (("opened", "entry", "ENTRY", ACC), ("closed", "exit", "EXIT", FG)):
        i, px = idx_for(trade.get(key)), trade.get(price_key)
        if i is None or not px:
            continue
        pointing_up = (key == "opened") == up
        tip_y = y(px)
        tri = (f"{cx(i):.1f},{tip_y:.1f} {cx(i) - 7:.1f},{tip_y + 12:.1f} {cx(i) + 7:.1f},{tip_y + 12:.1f}"
               if pointing_up else
               f"{cx(i):.1f},{tip_y:.1f} {cx(i) - 7:.1f},{tip_y - 12:.1f} {cx(i) + 7:.1f},{tip_y - 12:.1f}")
        parts.append(f"<polygon points='{tri}' fill='{col}' stroke='{BG}' stroke-width='1'>"
                     f"<title>{label} {px:,.2f}</title></polygon>")
    # time axis: first, middle, last bar
    for i in sorted({0, len(bars) // 2, len(bars) - 1}):
        parts.append(f"<text x='{cx(i):.1f}' y='{h - 8}' fill='{MUTED}' font-size='11' text-anchor='middle'>"
                     f"{bars[i]['t']:%H:%M}</text>")
    return _svg(w, h, "".join(parts), title)
