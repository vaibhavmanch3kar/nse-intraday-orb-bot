"""
One place that decides where prices come from: DATA_SOURCE in config.py.

  "yahoo" (default)  free, no login, can lag the exchange by a few minutes, ~60 days of 15-min history
  "kite"             Kite Connect paid plan: real-time prices and much longer history; needs today's login

If "kite" is chosen but can't be used right now (no login today, plan not upgraded, network error),
this falls back to Yahoo and records why in `status()`, so the bot keeps running and the dashboard
says which source is actually in use.
"""

import config as C
import data_kite
import data_yahoo
from data_yahoo import DataError, closed_bars, IST  # noqa: F401  (re-exported for callers)

_state = {"wanted": None, "using": "yahoo", "reason": ""}


def _source():
    wanted = (C.DATA_SOURCE or "yahoo").lower()
    _state["wanted"] = wanted
    if wanted != "kite":
        _state.update(using="yahoo", reason="")
        return data_yahoo
    try:
        data_kite._client()  # login and settings present?
    except DataError as e:
        _state.update(using="yahoo", reason=f"Kite not available ({e}) - using Yahoo")
        return data_yahoo
    _state.update(using="kite", reason="")
    return data_kite


def _with_fallback(name, *args, **kwargs):
    src = _source()
    try:
        return getattr(src, name)(*args, **kwargs)
    except data_kite.PlanError as e:
        if src is data_yahoo:
            raise
        _state.update(using="yahoo", reason=str(e) + " - using Yahoo")
        return getattr(data_yahoo, name)(*args, **kwargs)


def fetch(*args, **kwargs):
    return _with_fallback("fetch", *args, **kwargs)


def fetch_many(*args, **kwargs):
    return _with_fallback("fetch_many", *args, **kwargs)


def vix_by_day(*args, **kwargs):
    return _with_fallback("vix_by_day", *args, **kwargs)


def status():
    """{'wanted': setting, 'using': 'kite'|'yahoo', 'reason': why not kite (or '')}."""
    if _state["wanted"] is None:
        _source()
    return dict(_state)


def max_history_days(interval_min=15):
    """How far back a backtest can go with the source in use."""
    return 365 if status()["using"] == "kite" else 60
