"""
Yantrashala strategies: ideas under test. NONE of these is traded by the bot. Each is a small object with the same
three methods the backtest engine calls on the live rules (strategy.py):

    evaluate(bars, i, index_bars)   -> {"signal": "LONG" | "SHORT" | None, "stop": price, "vol_ratio": x, "context": {...}}
    manage_exit(pos, bars, i, held) -> (reason, price) or None
    target_for(entry, stop, dir)    -> price or None

so that every idea is tested by the one engine (backtest.simulate): entry at the next candle's open with slippage,
stop/target/gap fills, Kavach's checks and position sizes, and Zerodha's charges.

Signals use only candles up to and including bar i (no look-ahead): everything a strategy reads comes from
`Features`, which is built once per stock in one pass where feature[i] depends only on bars[0..i].
"""

import datetime as dt

import config as C
import strategy

BUF = 0.0005        # 0.05% beyond a candle's high/low for stops placed there


class Features:
    """Per-bar facts for one stock, each using only that bar and earlier ones."""

    def __init__(self, bars, index_bars=None):
        n = len(bars)
        self.n = n
        self.day_start = [0] * n          # index of the first bar of this bar's day
        self.or_high = [None] * n         # the day's opening range (None until its candles exist)
        self.or_low = [None] * n
        self.or_end = [None] * n
        self.vwap = [None] * n
        self.vol_ratio = [None] * n       # volume / average of the same time slot over the last sessions
        self.vol_days = [0] * n
        self.prev_close = [None] * n      # previous bar's close within the day
        self.ema9 = [None] * n
        self.ema21 = [None] * n
        self.atr = [None] * n             # ATR(14) of 15-minute candles
        self.index_chg = [None] * n       # the stock's index: % from its day open to this bar's close
        self.day_high = [None] * n        # highest high of the day so far (including this bar)
        self.day_low = [None] * n
        idx_by_t = {b["t"]: b for b in (index_bars or [])}
        idx_open = {}
        for b in (index_bars or []):
            idx_open.setdefault(b["t"].date(), b["o"])
        or_bars_needed = max(1, C.OR_MINUTES // C.INTERVAL_MIN)
        open_t = strategy.hm(C.MARKET_OPEN)
        sessions = []                     # earlier sessions, each {time of day: volume of that candle (>0)}
        today_slots = {}                  # the current session's, added to `sessions` when the day ends
        k9, k21 = 2 / 10, 2 / 22
        e9 = e21 = None
        trs = []
        day = None
        pv = vol = 0.0
        for i, b in enumerate(bars):
            d = b["t"].date()
            if d != day:
                if day is not None:
                    sessions.append(today_slots)
                today_slots = {}
                day, start = d, i
                pv = vol = 0.0
                hi, lo = b["h"], b["l"]
                or_start = dt.datetime.combine(d, open_t, tzinfo=b["t"].tzinfo)
                or_end = or_start + dt.timedelta(minutes=C.OR_MINUTES)
                or_bars = []
            else:
                hi, lo = max(hi, b["h"]), min(lo, b["l"])
                self.prev_close[i] = bars[i - 1]["c"]
            self.day_start[i] = start
            self.day_high[i], self.day_low[i] = hi, lo
            if or_start <= b["t"] < or_end:
                or_bars.append(b)
            if len(or_bars) >= or_bars_needed:
                self.or_high[i] = max(x["h"] for x in or_bars)
                self.or_low[i] = min(x["l"] for x in or_bars)
                self.or_end[i] = or_end
            pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
            vol += b["v"]
            self.vwap[i] = pv / vol if vol > 0 else b["c"]
            # as strategy.slot_avg_volume: the last N sessions, those that have this time slot with volume
            slot = b["t"].time()
            hist = [s[slot] for s in sessions[-C.VOLUME_LOOKBACK_DAYS:] if slot in s]
            if hist:
                self.vol_days[i] = len(hist)
                self.vol_ratio[i] = b["v"] / (sum(hist) / len(hist))
            if b["v"] > 0 and slot not in today_slots:
                today_slots[slot] = b["v"]
            e9 = b["c"] if e9 is None else b["c"] * k9 + e9 * (1 - k9)
            e21 = b["c"] if e21 is None else b["c"] * k21 + e21 * (1 - k21)
            self.ema9[i], self.ema21[i] = e9, e21
            pc = bars[i - 1]["c"] if i else b["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
            if len(trs) >= 14:
                self.atr[i] = sum(trs[-14:]) / 14
            ib = idx_by_t.get(b["t"])
            if ib is not None and d in idx_open:
                self.index_chg[i] = (ib["c"] / idx_open[d] - 1) * 100


class Base:
    """Shared plumbing. A strategy sets `name`, `params`, and writes signal()."""
    name = "base"
    grid = [{}]                           # the parameter sets tried (each one is counted as a separate test)

    def __init__(self, **params):
        self.params = params
        self._feat = {}

    def label(self):
        return self.name + ("(" + ", ".join(f"{k}={v}" for k, v in sorted(self.params.items())) + ")" if self.params else "")

    def features(self, bars, index_bars):
        f = self._feat.get(id(bars))
        if f is None or f.n != len(bars):
            f = self._feat[id(bars)] = Features(bars, index_bars)
        return f

    # -- the engine's three calls
    def evaluate(self, bars, i=None, index_bars=None):
        if i is None:
            i = len(bars) - 1
        f = self.features(bars, index_bars)
        out = {"signal": None, "stop": None, "vol_ratio": f.vol_ratio[i], "context": None, "reason": ""}
        sig = self.signal(bars, i, f)
        if sig:
            direction, stop = sig[0], sig[1]
            if (direction == "SHORT" and not C.ALLOW_SHORT) or stop is None:
                return out
            out.update(signal=direction, stop=stop, reason=f"{direction} {self.name}",
                       context={"signal_bar": f"{bars[i]['t']:%H:%M}", "vol_ratio": f.vol_ratio[i]})
        return out

    def manage_exit(self, pos, bars, i, held):
        return strategy.manage_exit(pos, bars, i, held)       # the bot's exits: stop, target, EMA trail, time stop

    def target_for(self, entry, stop, direction):
        r = self.params.get("target_r")
        if r:
            risk = abs(entry - stop)
            return entry + r * risk if direction == "LONG" else entry - r * risk
        return strategy.target_for(entry, stop, direction)

    def signal(self, bars, i, f):
        raise NotImplementedError

    # -- pieces several strategies share
    @staticmethod
    def in_time(bar, last=None):
        return bar["t"].time() <= strategy.hm(last or C.LAST_ENTRY)

    @staticmethod
    def width_ok(f, i):
        w = (f.or_high[i] - f.or_low[i]) / f.or_low[i] * 100
        return C.MIN_OR_PCT <= w <= C.MAX_OR_PCT

    @staticmethod
    def breakout(bars, i, f):
        """The bot's own breakout test on bar i: 'LONG' / 'SHORT' / None (a fresh close beyond the range)."""
        if f.or_high[i] is None or bars[i]["t"] < f.or_end[i] or f.prev_close[i] is None:
            return None
        c, pc = bars[i]["c"], f.prev_close[i]
        if c > f.or_high[i] >= pc:
            return "LONG"
        if c < f.or_low[i] <= pc:
            return "SHORT"
        return None

    @staticmethod
    def index_ok(f, i, direction, need=0.0):
        """The index filter as the bot applies it (missing index data never blocks), with an optional minimum
        move in the trade's direction."""
        chg = f.index_chg[i]
        if chg is None:
            return need <= 0
        if need <= 0:                     # exactly the bot's rule: above (below) its day open, not equal
            return chg > 0 if direction == "LONG" else chg < 0
        return chg >= need if direction == "LONG" else chg <= -need

    def orb_filters(self, bars, i, f, direction, vol_mult=None):
        """Every filter of the live ORB after the breakout itself."""
        if not self.in_time(bars[i]) or not self.width_ok(f, i):
            return False
        if f.vol_days[i] < 3 or f.vol_ratio[i] is None or f.vol_ratio[i] < (vol_mult or C.VOLUME_MULT):
            return False
        vw, c = f.vwap[i], bars[i]["c"]
        if C.USE_VWAP_FILTER and ((direction == "LONG" and c <= vw) or (direction == "SHORT" and c >= vw)):
            return False
        return not C.INDEX_FILTER or self.index_ok(f, i, direction)


class Orb(Base):
    """The live bot's opening-range breakout, written against Features. It must match strategy.evaluate signal
    for signal (tests/test_yantra.py); it is here so the lab can be checked against the existing backtest."""
    name = "orb"

    def signal(self, bars, i, f):
        d = self.breakout(bars, i, f)
        if not d or not self.orb_filters(bars, i, f, d):
            return None
        if C.STOP_RULE == "mid":
            return d, (f.or_high[i] + f.or_low[i]) / 2
        return d, (f.or_low[i] if d == "LONG" else f.or_high[i])


class OrbRetest(Base):
    """Breakout, then wait for a pullback that touches the range edge and closes back beyond it. Entry after the
    retest candle; stop just beyond that candle (a tighter stop than the far side of the range)."""
    name = "orb_retest"
    grid = [{"wait": 2}, {"wait": 4}]

    def signal(self, bars, i, f):
        if f.or_high[i] is None:
            return None
        wait = self.params.get("wait", 2)
        b = bars[i]
        for j in range(max(f.day_start[i], i - wait), i):          # the breakout candle, 1..wait bars ago
            d = self.breakout(bars, j, f)
            if not d or not self.orb_filters(bars, j, f, d):
                continue
            if not self.in_time(b, "13:00"):
                return None
            if d == "LONG" and b["l"] <= f.or_high[i] * (1 + BUF) and b["c"] > f.or_high[i] and b["c"] > b["o"]:
                if all(bars[k]["c"] > f.or_low[i] for k in range(j, i)):       # the breakout did not fail outright
                    return "LONG", b["l"] * (1 - BUF)
            if d == "SHORT" and b["h"] >= f.or_low[i] * (1 - BUF) and b["c"] < f.or_low[i] and b["c"] < b["o"]:
                if all(bars[k]["c"] < f.or_high[i] for k in range(j, i)):
                    return "SHORT", b["h"] * (1 + BUF)
        return None


class FailedBreakout(Base):
    """Fade a breakout that fails: a close beyond the range, then within a few candles a close back inside it.
    Trade against the failed move; stop beyond the extreme it reached; a fixed target."""
    name = "failed_breakout"
    grid = [{"within": 2, "target_r": 1.5}, {"within": 2, "target_r": 2.0},
            {"within": 3, "target_r": 1.5}, {"within": 3, "target_r": 2.0}]

    def signal(self, bars, i, f):
        if f.or_high[i] is None or not self.in_time(bars[i], "13:00") or not self.width_ok(f, i):
            return None
        within = self.params.get("within", 2)
        b = bars[i]
        for j in range(max(f.day_start[i], i - within), i):
            d = self.breakout(bars, j, f)
            if d == "LONG" and b["c"] < f.or_high[i] and all(bars[k]["c"] >= f.or_high[i] for k in range(j, i)):
                top = max(bars[k]["h"] for k in range(j, i + 1))
                return "SHORT", top * (1 + BUF)
            if d == "SHORT" and b["c"] > f.or_low[i] and all(bars[k]["c"] <= f.or_low[i] for k in range(j, i)):
                bottom = min(bars[k]["l"] for k in range(j, i + 1))
                return "LONG", bottom * (1 - BUF)
        return None


class VwapPullback(Base):
    """Trend confirmation: the day has already broken its opening range, price is on the trend side of VWAP with
    EMA9 beyond EMA21, and a candle dips to VWAP and closes back in the trend's direction."""
    name = "vwap_pullback"
    grid = [{"tol": 0.05}, {"tol": 0.15}]

    def signal(self, bars, i, f):
        if f.or_high[i] is None or bars[i]["t"] < f.or_end[i] or not self.in_time(bars[i], "13:30"):
            return None
        if not self.width_ok(f, i) or f.vol_ratio[i] is None:
            return None
        tol = self.params.get("tol", 0.05) / 100
        b, vw = bars[i], f.vwap[i]
        if (f.day_high[i] > f.or_high[i] and f.ema9[i] > f.ema21[i] and b["l"] <= vw * (1 + tol)
                and b["c"] > vw and b["c"] > b["o"] and self.index_ok(f, i, "LONG")):
            return "LONG", b["l"] * (1 - BUF)
        if (f.day_low[i] < f.or_low[i] and f.ema9[i] < f.ema21[i] and b["h"] >= vw * (1 - tol)
                and b["c"] < vw and b["c"] < b["o"] and self.index_ok(f, i, "SHORT")):
            return "SHORT", b["h"] * (1 + BUF)
        return None


class OrbAtr(Base):
    """The live breakout with an ATR stop instead of the far side of the range, and a stricter volume test."""
    name = "orb_atr"
    grid = [{"k": 1.0, "vol": 1.2}, {"k": 1.5, "vol": 1.2}, {"k": 1.0, "vol": 2.0}, {"k": 1.5, "vol": 2.0}]

    def signal(self, bars, i, f):
        d = self.breakout(bars, i, f)
        if not d or f.atr[i] is None or not self.orb_filters(bars, i, f, d, vol_mult=self.params.get("vol", 1.2)):
            return None
        dist = self.params.get("k", 1.0) * f.atr[i]
        c = bars[i]["c"]
        return d, (c - dist if d == "LONG" else c + dist)


class OrbRegime(Base):
    """The live breakout, taken only when the stock's index is already moving the same way by a minimum amount
    (a simple market-regime filter: trade breakouts on trending days only)."""
    name = "orb_regime"
    grid = [{"index_min": 0.2}, {"index_min": 0.4}]

    def signal(self, bars, i, f):
        d = self.breakout(bars, i, f)
        if not d or not self.orb_filters(bars, i, f, d):
            return None
        if not self.index_ok(f, i, d, need=self.params.get("index_min", 0.2)):
            return None
        return d, (f.or_low[i] if d == "LONG" else f.or_high[i])


STRATEGIES = {c.name: c for c in (Orb, OrbRetest, FailedBreakout, VwapPullback, OrbAtr, OrbRegime)}
WHAT = {
    "orb": "The live bot's rules (the yardstick)",
    "orb_retest": "Breakout, then a pullback to the range edge that holds",
    "failed_breakout": "Fade a breakout that closes back inside the range",
    "vwap_pullback": "Trend day: pullback to VWAP that closes back with the trend",
    "orb_atr": "Breakout with an ATR stop and a stricter volume test",
    "orb_regime": "Breakout only when the index is already moving the same way",
}


def variants(name):
    """Every parameter set of a strategy as ready objects."""
    cls = STRATEGIES[name]
    return [cls(**p) for p in cls.grid]
