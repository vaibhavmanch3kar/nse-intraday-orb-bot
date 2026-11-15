"""
The 30-trade edge check (Trading With Sidhant's rule): keep a journal, and after 30 trades ask
whether the setup's average result per trade (expectancy) is positive. If not, stop trading it.

Because 30 trades is still a small sample, this also gives a rough 95% range for the average R
(mean +/- 2 standard errors). If that range includes zero, the honest answer is "not proven yet".
"""

import math

MIN_TRADES = 30


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def edge_check(rows, min_trades=MIN_TRADES):
    """rows: trade-log dicts with 'net' (₹ after charges) and 'r' (multiple of risk).

    Returns a dict with the stats and a verdict:
      'too_early'  fewer than min_trades trades
      'positive'   average R > 0 and the whole 95% range is above 0
      'negative'   average R <= 0 and the whole 95% range is below 0  -> stop trading the setup
      'unproven'   the 95% range includes 0 (could be luck either way)
      'leaning_negative' average R <= 0 but range includes 0 -> rule says stop
    """
    trades = [(n, r) for n, r in ((_num(x.get("net")), _num(x.get("r"))) for x in rows)
              if n is not None and r is not None]
    n = len(trades)
    out = {"n": n, "min_trades": min_trades, "verdict": "too_early"}
    if n == 0:
        return out
    nets = [t[0] for t in trades]
    rs = [t[1] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    mean_r = sum(rs) / n
    sd = math.sqrt(sum((x - mean_r) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    out.update(
        win_rate=len(wins) / n, avg_net=sum(nets) / n, total_net=sum(nets),
        avg_win=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        avg_r=mean_r, r_low=mean_r - 2 * se, r_high=mean_r + 2 * se,
    )
    if n < min_trades:
        return out
    if out["r_low"] > 0:
        out["verdict"] = "positive"
    elif out["r_high"] < 0:
        out["verdict"] = "negative"
    elif mean_r > 0:
        out["verdict"] = "unproven"
    else:
        out["verdict"] = "leaning_negative"
    return out


MESSAGES = {
    "too_early": ("Too early to judge", "Keep paper trading and journaling. The check unlocks at {min_trades} trades."),
    "positive": ("Edge looks positive", "Average {avg_r:+.2f}R per trade, and even the low end of the 95% range "
                 "({r_low:+.2f}R) is above zero. Keep going, still on paper, and re-check every 30 trades."),
    "unproven": ("Positive so far, not proven", "Average {avg_r:+.2f}R per trade, but the 95% range "
                 "({r_low:+.2f}R to {r_high:+.2f}R) includes zero, so it could still be luck. Keep paper trading."),
    "leaning_negative": ("No edge: stop this setup", "Average {avg_r:+.2f}R per trade after {n} trades "
                         "(95% range {r_low:+.2f}R to {r_high:+.2f}R). The 30-trade rule says stop or change the rules."),
    "negative": ("Negative edge: stop this setup", "Average {avg_r:+.2f}R per trade, and even the high end of the "
                 "95% range ({r_high:+.2f}R) is below zero. Stop trading it and review the rules."),
}


def message(check):
    title, text = MESSAGES[check["verdict"]]
    return title, text.format(**check)
