"""Scoreboard: the bot's result next to simply holding the watchlist and the Nifty 50 over the same completed
days. Read-only arithmetic from the journal, the trade log and the candle archive; a yardstick, not advice."""

import datetime as dt
import os

import buildinfo
import config as C
import scoreboard
import web

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
D = [dt.date(2026, 11, d) for d in (13, 16, 17, 18, 19)]           # Fri, then Mon-Thu
TODAY = dt.date(2026, 11, 20)


def bars(prices):
    """{symbol: [close on each day of D]} as archive rows (two candles a day: the last one is the close)."""
    out = {}
    for sym, closes in prices.items():
        rows = []
        for day, c in zip(D, closes):
            rows.append({"t": dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST), "c": c * 0.99})
            rows.append({"t": dt.datetime.combine(day, dt.time(15, 15), tzinfo=IST), "c": c})
        out[sym] = rows
    return out


def loader(prices):
    data = bars(prices)

    def load(symbols=None, start=None, end=None):
        return {s: [b for b in rows if start <= b["t"].date() <= end] for s, rows in data.items() if s in symbols}
    return load


def setup(monkeypatch):
    monkeypatch.setattr(C, "WATCHLIST", ["AAA", "BBB"])
    prices = {"AAA": [100, 101, 102, 103, 110], "BBB": [200, 200, 198, 196, 190], "^NSEI": [1000, 1005, 1010, 1000, 1020]}
    journal_rows = [{"date": "2026-11-16", "start_equity": "10000"}, {"date": "2026-11-17", "start_equity": "10100"},
                    {"date": "2026-11-19", "start_equity": "50000"}]          # the bot did not run on the 18th
    trades = [{"closed": "2026-11-16 10:45:00+05:30", "net": "100"}, {"closed": "2026-11-17 11:00:00+05:30", "net": "-50.5"},
              {"closed": "2026-11-17 13:00:00+05:30", "net": "-50.5"}, {"closed": "2026-11-19 12:00:00+05:30", "net": "250"}]
    return dict(journal_rows=journal_rows, trades=trades, load=loader(prices), archived=D)


def test_the_three_lines_over_the_same_days(monkeypatch):
    sb = scoreboard.build("paper", TODAY, **setup(monkeypatch))
    assert (sb["from"], sb["to"], sb["base_day"], sb["market_days"], sb["bot_days"]) == (
        "2026-11-16", "2026-11-19", "2026-11-13", 4, 3)
    # the bot: +1.00%, then -1.00% of that day's balance, a day off, then +0.50%: compounded, not added
    assert abs(sb["bot_pct"] - ((1.01 * 0.99 * 1.005) - 1) * 100) < 1e-9
    # holding: AAA +10%, BBB -5% -> +2.5% on equal rupees, less the one-time cost of buying
    assert abs(sb["hold_pct"] - (1.025 * (1 - scoreboard.BUY_COST_PCT / 100) - 1) * 100) < 1e-9
    assert abs(sb["index_pct"] - 2.0) < 1e-9
    assert abs(sb["vs_hold_pts"] - (sb["bot_pct"] - sb["hold_pct"])) < 1e-12 and sb["few_days"] is True
    days = {r["day"]: r for r in sb["rows"]}
    assert days["2026-11-18"]["bot_ran"] is False and days["2026-11-18"]["bot_pct"] == days["2026-11-17"]["bot_pct"]
    assert "behind holding the watchlist" in scoreboard.sentence(sb)


def test_money_added_is_not_a_result(monkeypatch):
    kw = setup(monkeypatch)
    # the balance jumped from about 10,000 to 50,000 between the 17th and the 19th: only the trades count
    sb = scoreboard.build("paper", TODAY, **kw)
    assert sb["bot_pct"] < 1.0


def test_today_and_days_without_archived_prices_wait(monkeypatch):
    kw = setup(monkeypatch)
    kw["journal_rows"].append({"date": "2026-11-20", "start_equity": "50250"})
    kw["trades"].append({"closed": "2026-11-20 10:00:00+05:30", "net": "-500"})
    sb = scoreboard.build("paper", TODAY, **kw)
    assert sb["to"] == "2026-11-19"                                   # today's close is not in the archive yet
    early = scoreboard.build("paper", dt.date(2026, 11, 16), **setup(monkeypatch))
    assert "not archived yet" in early["reason"] or "completed" in early["reason"]


def test_reasons_instead_of_numbers_when_it_cannot_be_worked_out(monkeypatch):
    kw = setup(monkeypatch)
    assert "no trading days" in scoreboard.build("live", TODAY, journal_rows=[], trades=[], load=kw["load"],
                                                 archived=D)["reason"]
    assert "no prices are archived" in scoreboard.build("paper", TODAY, **{**kw, "archived": []})["reason"]
    assert "from before" in scoreboard.build("paper", TODAY, **{**kw, "archived": D[1:]})["reason"]

    def broken(symbols=None, start=None, end=None):
        raise OSError("disk")
    assert "could not be worked out (OSError)" in scoreboard.build("paper", TODAY, **{**kw, "load": broken})["reason"]
    assert scoreboard.sentence({"reason": "x"}) == "No scoreboard yet: x."


def test_the_card_on_the_trades_page(monkeypatch):
    kw = setup(monkeypatch)
    real = scoreboard.build
    monkeypatch.setattr(scoreboard, "build", lambda mode: real(mode, TODAY, **kw))
    card = web.scoreboard_card("paper")
    for text in ("Bot vs just holding", "a yardstick, not advice", "Holding the watchlist", "Nifty 50",
                 "far too few to judge", "(bot off)", "money added or withdrawn is not a result"):
        assert text in card, text
    monkeypatch.setattr(scoreboard, "build", lambda mode: {"reason": "this account has no trading days on record yet"})
    assert "No scoreboard yet" in web.scoreboard_card("live")
    assert "scoreboard_card(mode)" in open(web.__file__, encoding="utf-8").read()


def test_it_is_read_only_and_not_part_of_the_bot():
    code = open(scoreboard.__file__, encoding="utf-8").read()
    for word in ("import broker", "kiteconnect", "place_order", "requests", "open(", ".write("):
        assert word not in code, word
    assert "scoreboard.py" not in {os.path.basename(p) for p in buildinfo.bot_files()}


def test_the_cards_helper_does_not_replace_the_portfolio_pages_one():
    # 20 Nov 2026: the card first named its helper _pct, the name the Portfolio page already used with two
    # arguments, and that page crashed. Caught by the full test run before anything was merged.
    assert web._pct(12.345, 2) == "12.35%" and web._pct(1.5, 1, signed=True) == "+1.5%"
    assert web._pct_arrow(-0.3) == "▼ 0.30%" and web._pct_arrow(0.0) == "● 0.00%" and web._pct_arrow(None) == "-"
