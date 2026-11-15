"""NSE trading calendar: weekends + the holiday list in nse_holidays.json.

If the file has no list for the current year, is_trading_day() still returns True for weekdays,
and the bot falls back to checking whether NIFTY printed a bar today (see bot.py).
"""

import datetime as dt
import json
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nse_holidays.json")


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def holiday_name(day: dt.date):
    """Name of the NSE holiday on `day`, or None."""
    return _load().get(str(day.year), {}).get(day.isoformat())


def year_listed(year: int) -> bool:
    return str(year) in _load()


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and holiday_name(day) is None


def next_trading_day(day: dt.date) -> dt.date:
    d = day + dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def upcoming(day: dt.date, n=3):
    """Next n listed holidays on or after `day`: [(date, name)]."""
    out = []
    for year_list in _load().values():
        for d, name in year_list.items():
            dd = dt.date.fromisoformat(d)
            if dd >= day:
                out.append((dd, name))
    return sorted(out)[:n]
