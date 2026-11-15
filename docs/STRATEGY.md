# How the strategy works

[← back to README](../README.md)

## Trading rules

| | Rule |
|---|---|
| Stocks | Trades 12 liquid large caps: RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS, SBIN, AXISBANK, KOTAKBANK, LT, ITC, BHARTIARTL, TATASTEEL (`WATCHLIST`). The swing scanner checks the full Nifty 50 (`SCAN_LIST`) |
| Calendar | Skips weekends and **NSE holidays** (`nse_holidays.json`). If a year isn't listed, it treats "no NIFTY data by 09:45" as a holiday |
| Opening range | High/low of the 09:15–09:30 candle |
| Long | A 15-min candle **closes above** the range high (the previous close was inside) |
| Short | A 15-min candle **closes below** the range low (intraday MIS shorts) |
| Filters | Range width 0.25–1.5% · candle volume ≥ 1.2× the same-time average of the last 5 days · long above VWAP / short below · no entries after 12:00 |
| Index filter | Long only if the stock's index (NIFTY; Bank NIFTY for banks; NIFTY IT for IT) is up on the day; short only if down |
| Stop | Other side of the opening range. In live mode it is **placed at the exchange as an SL order** |
| Time stop | After 1 hour (4 bars), exit unless the trade is at least 0.5R in profit |
| Trail | Exit when a 15-min close crosses the 21 EMA. **No fixed target**: winners run until the trail or square-off |
| Square-off | 15:10 (Zerodha auto-squares MIS around 15:20 as a backstop, with a fee) |
| More signals than slots | Taken in order of breakout volume (strongest first) |

R = profit or loss as a multiple of the risk (entry to stop). +1R means you made what you risked.

## Risk rules (all in `config.py`, all can be overridden by environment variables)

| Rule | Default |
|---|---|
| Risk per trade, charges included | 0.5% of equity |
| Max per stock / all stocks | 25% / 75% of equity (no leverage) |
| Max open positions · trades per day | 3 · 4 |
| One trade per stock per day | yes |
| Daily loss limit | 2% of the day's starting equity, counting booked **and open** losses → no new entries that day |
| Loss streak | 3 losses → 2 days off |
| Charges | Zerodha intraday: 0.03% or ₹20 brokerage, STT 0.025% on sell, NSE transaction, SEBI, stamp duty, 18% GST |

## Backtest (₹1,00,000, charges + 0.05% slippage, 25 Aug – 13 Nov 2026)

| Stocks · rules | Return | Max DD | Trades | Win rate | Profit factor | Charges | Buy & hold |
|---|---|---|---|---|---|---|---|
| **12 stocks · current rules (default)** | **−2.3%** | 2.65% | 84 | 34.5% | 0.59 | ₹2,130 | +6.8% |
| Nifty 50 · current rules | −7.9% | 8.1% | 115 | 33.0% | 0.25 | ₹2,766 | +1.3% |
| 12 stocks · original rules (2R target, no extra rules) | −4.9% | 5.5% | 121 | 43.8% | 0.64 | ₹3,034 | +6.8% |

- **No version made money.** Treat this as a learning tool.
- Trading all 50 Nifty stocks was worse: more stocks bring more weak breakouts.
- The current rules were picked from about 10 variants on the same 60 days, so their result
  flatters them. Forward paper trading is the real test.
- Yahoo keeps only about 60 days of 15-minute data, which is a small sample.
- Fills: entries at the **next** candle's open + 0.05% slippage; stops at the stop − slippage, or at the
  open if price gaps through; open positions are valued at each candle's close for the drawdown.
  Tests in `tests/test_backtest_engine.py` pin these rules and check for look-ahead.

```powershell
.\.venv\Scripts\python.exe backtest.py                       # 60 days, trade list -> data\backtest_trades.csv
.\.venv\Scripts\python.exe backtest.py --symbols SBIN,INFY   # only some stocks
```

### Optional rules (from Trading With Sidhant's risk-management video)

Each can be switched on or off in `config.py`. The last row is the current default.

| Setting | Rule | 60-day result on its own (original rules: −4.9%) |
|---|---|---|
| `INDEX_FILTER=true` | Trade with the stock's index, not against it | −3.9% |
| `TIME_STOP_BARS=4` | Exit after 1 hour unless ≥ 0.5R in profit | −3.8% |
| `TRAIL_EMA=21` + `TARGET_R=0` | Trail the 21 EMA, no fixed target | −4.8% |
| `TRAIL_EMA=8` | Trail the 8 EMA | −4.9% |
| `VIX_SIZING=true` | Smaller trades when India VIX is high (>15, >20) | −4.9% (VIX was mostly low) |
| `MAX_DAILY_LOSS_PCT=1` | Max 1% loss per day | −4.9% (never reached) |
| `STOP_RULE=mid` | Stop at the range midpoint | −2.6% with current rules (vs −2.3%), not used |
| index + time stop + trail 21, no target | combined (default) | −2.1% |

## Time Machine

Replays a trade 10,000 times using **this stock's own 5-minute moves** from the same time of day
(±10 minutes) over the last 60 days, shuffled, from entry to 15:10. The random seed is fixed, so the
same data always gives the same numbers. It reports:

- how often a 1R or 2R target is hit before the stop, the average result with no target, and the price range at 15:10;
- a **verdict**: of the replays that hit a side, how often the target came first, compared with break-even
  (50% at 1R). Within 5 points is a *coin flip*; 5–15 points *leans*; 15+ *strongly*;
- the **real history**: the bot's own rules run on every earlier session in the 60 days for that stock and direction.

It assumes no edge, so treat the odds as a baseline: a good setup should beat them.

```powershell
.\.venv\Scripts\python.exe timemachine.py TCS LONG 2439.8 2410.5 --at 09:45 --date 2026-11-13
.\.venv\Scripts\python.exe review.py 2026-11-13     # a whole day's review in the terminal
.\.venv\Scripts\python.exe scanner.py               # the swing scan in the terminal
```

## 30-trade edge check

From Trading With Sidhant's discipline rules: keep a journal, and after 30 trades check that the average
result per trade (expectancy, in R) is positive; if it isn't, stop trading the setup. The Trades page shows
a progress bar until 30 trades, then a verdict. Because 30 trades is a small sample it also shows a rough
95% range (average ± 2 standard errors): if that range includes zero, the result could still be luck.
On the 84 backtest trades it says **No edge** (−0.10R per trade, range −0.21R to +0.02R).

## Files

| File | Purpose |
|---|---|
| `1-SETUP.bat` … `6-CHECK-KITE-DATA.bat` | Double-click launchers |
| `setup.ps1` | One-time paper-trading setup (Python environment, packages, self-test) |
| `bot.py` | Main loop: fetch candles every 60 s, manage positions, take signals |
| `strategy.py` | Opening range, VWAP, volume, signals, exits (shared with the backtest) |
| `risk.py` | Position sizing, risk limits, saved state |
| `costs.py` | Zerodha intraday charges |
| `broker.py` | Paper and live (Kite) order handling |
| `data_yahoo.py` | Free NSE candles from Yahoo Finance |
| `data_kite.py` | Real-time prices and long history from Kite Connect (paid Connect plan); `data_kite.py check` |
| `marketdata.py` | Picks Yahoo or Kite from `DATA_SOURCE`, falling back to Yahoo if Kite isn't usable |
| `web.py` | Dashboard (Live / Session review / Swing scanner / Trades) + Zerodha login on 127.0.0.1:5000 |
| `review.py`, `timemachine.py`, `scanner.py` | Session review, Time Machine, swing scanner |
| `holidays.py`, `nse_holidays.json` | NSE trading calendar |
| `backtest.py` | Portfolio backtest |
| `kite_proxy.py`, `setup_kite_proxy.ps1` | Live trading only: Kite client through a static-IP proxy |
| `run_bot.ps1`, `dashboard.ps1`, `register_task.ps1` | What the `.bat` files run |
| `edge.py` | The 30-trade edge check shown on the Trades page |
| `charts.py` | Equity curve, P&L bars and trade-chart drawings (plain SVG, no extra packages) |
| `tests/` | 91 offline tests (`.venv\Scripts\python.exe -m pytest -q`) |
| `data/` | Created at run time: paper state, trade log, bot log (not in git) |
