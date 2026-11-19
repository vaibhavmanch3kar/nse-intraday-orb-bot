"""Paper | Real Money control (plan part 4): arming needs the typed words, a fresh Zerodha login and every readiness
check; the bot switches only at a safe moment; switching back to paper keeps managing open positions; "Close all"
is its own request; nothing here ever leaks a token, a proxy password or an address."""

import datetime as dt
import json
import os
import pathlib
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import bot
import config as C
import data_kite
import data_yahoo
import livemode
import restart
import web
from conftest import IST, bar, history

TOKEN = "tok-SECRET-0123456789"
PROXY_PW = "proxyPASSWORD99"
EGRESS = "203.0.113.9"


# ---------------------------------------------------------------- bot side
def entry_day():
    hist, day = history()
    return hist + [bar(day, "09:15", 100.2, 101.0, 100.0, 100.8, 5000),
                   bar(day, "09:30", 100.8, 100.9, 100.4, 100.7, 1000),
                   bar(day, "09:45", 100.7, 101.3, 100.6, 101.2, 3000)], day


@pytest.fixture
def market(monkeypatch):
    """One watched stock, fake Yahoo bars and a fake clock shared by the bot and livemode."""
    bars, day = entry_day()
    ref = {"bars": bars, "now": dt.datetime.combine(day, dt.time(10, 0, 30), tzinfo=IST), "day": day}
    monkeypatch.setattr(C, "WATCHLIST", ["SBIN"])
    monkeypatch.setattr(C, "DRY_RUN", True)
    monkeypatch.setattr(data_yahoo, "fetch", lambda s, i, r, **kw: (ref["bars"], ref["bars"][-1]["c"], None))
    monkeypatch.setattr(bot, "now_ist", lambda: ref["now"])
    monkeypatch.setattr(livemode, "_now", lambda: ref["now"])
    monkeypatch.setattr(data_kite, "candle_hold", lambda now=None: 0.0)
    return ref


def live_settings(monkeypatch):
    monkeypatch.setattr(C, "DRY_RUN", False)
    monkeypatch.setattr(C, "CONFIRM_LIVE", "YES")


def test_armed_paper_bot_switches_to_real_money_only_when_flat(market, monkeypatch):
    live_settings(monkeypatch)
    b = bot.Bot()
    assert not b.live                                  # settings on, not armed: paper
    b.cycle()
    assert "SBIN" in b.state.positions                 # a paper trade is open
    livemode.arm(login_at=market["now"] - dt.timedelta(minutes=2))
    assert b.switch_mode_if_requested(market["now"]) is None and b.switch == "to_live"
    assert "1 open position" in b.events[0]
    # the stop is hit: the paper trade closes, then the switch goes ahead
    market["bars"] = market["bars"] + [bar(market["day"], "10:00", 101.0, 101.1, 99.8, 99.9, 2000)]
    market["now"] = market["now"].replace(minute=15)
    b.cycle()
    assert not b.state.positions
    assert b.switch_mode_if_requested(market["now"]) == restart.EXIT_RESTART
    st = restart.state()
    assert st["stage"] == "restarting" and st["before"]["to_mode"] == "live" and "REAL MONEY" in st["reason"]
    nb = bot.Bot()                                     # what run_bot.ps1 starts next, same settings
    assert nb.live and nb.mode == "live"
    checks = restart.verify(nb.mode, list(nb.state.positions), 0, 0, 0, os.getpid() + 1, None,
                            now=market["now"])
    assert all(checks.values()) and "now trading REAL MONEY" in checks


def test_armed_switch_holds_new_paper_entries_and_waits_for_the_candle(market, monkeypatch):
    live_settings(monkeypatch)
    monkeypatch.setattr(data_kite, "candle_hold", lambda now=None: 30.0)
    b = bot.Bot()                                      # started before the arming: paper
    livemode.arm(login_at=market["now"])
    assert b.switch_mode_if_requested(market["now"]) is None and b.switch == "to_live"
    b.cycle()
    assert not b.state.positions and "paused" in b.rows["SBIN"]["reason"]


def test_arming_without_the_live_settings_never_switches(market):
    livemode.arm(login_at=market["now"])
    b = bot.Bot()
    assert b.switch_mode_if_requested(market["now"]) is None and b.switch is None and not b.live


def test_no_switch_after_the_close(market, monkeypatch):
    live_settings(monkeypatch)
    livemode.arm(login_at=market["now"])
    b = bot.Bot()
    late = market["now"].replace(hour=15, minute=31)
    assert b.switch_mode_if_requested(late) is None and not os.path.exists(restart.state_path())


def test_back_to_paper_keeps_managing_and_switches_when_flat(market, monkeypatch):
    live_settings(monkeypatch)
    livemode.arm(login_at=market["now"])
    b = bot.Bot()
    assert b.live
    b.kite = FakeKite(funds=50000.0)                   # read-only balance; PaperBroker places the "orders"
    b.sync_live_balance(market["now"])
    b.cycle()                                          # PaperBroker stands in for Zerodha: no real order
    assert "SBIN" in b.state.positions
    livemode.request_paper()
    assert not os.path.exists(livemode.arm_path())     # the arming goes at once
    assert b.switch_mode_if_requested(market["now"]) is None and b.switch == "to_paper"
    assert "managing 1 open position until it closes" in b.events[0]
    # a fresh breakout would be an entry, but no new real-money trade is opened while switching
    market["bars"] = market["bars"] + [bar(market["day"], "10:00", 101.2, 101.4, 100.9, 101.3, 3000)]
    market["now"] = market["now"].replace(minute=15)
    b.cycle()
    assert list(b.state.positions) == ["SBIN"] and b.state.d["trades_today"] == 1
    # the open trade is still managed: its stop closes it
    market["bars"] = market["bars"] + [bar(market["day"], "10:15", 101.0, 101.1, 99.8, 99.9, 2000)]
    market["now"] = market["now"].replace(minute=30)
    b.cycle()
    assert not b.state.positions
    assert b.switch_mode_if_requested(market["now"]) == restart.EXIT_RESTART
    assert restart.state()["before"]["to_mode"] == "paper"
    nb = bot.Bot()
    assert not nb.live and nb.switch_mode_if_requested(market["now"]) is None
    assert not os.path.exists(livemode.to_paper_path())  # done: the request is cleared


def test_close_all_closes_every_position_once(market):
    b = bot.Bot()
    b.cycle()
    assert "SBIN" in b.state.positions
    livemode.request_close_all(now=market["now"])
    market["now"] = market["now"].replace(minute=5)
    b.cycle()
    assert not b.state.positions and not os.path.exists(livemode.close_all_path())
    trades = open(os.path.join(C.DATA_DIR, "trades_paper.csv"), encoding="utf-8").read()
    assert "closed from dashboard" in trades


def test_old_or_forged_close_all_request_is_dropped(market):
    livemode.request_close_all(now=market["now"] - dt.timedelta(minutes=30))
    assert livemode.take_close_all(market["now"]) is False and not os.path.exists(livemode.close_all_path())
    with open(livemode.close_all_path(), "w") as f:
        json.dump({"requested_at": market["now"].isoformat(), "nonce": "a" * 32, "symbols": ["X"]}, f)
    assert livemode.take_close_all(market["now"]) is False


def test_forged_paper_request_is_ignored():
    with open(livemode.to_paper_path(), "w") as f:
        json.dump({"requested_at": "2026-11-17T10:00:00+05:30", "nonce": "a" * 32, "mode": "live"}, f)
    assert livemode.paper_requested() is False and not os.path.exists(livemode.to_paper_path())


def test_mode_switch_verify_fails_if_the_mode_did_not_change():
    restart.begin(None, "paper", [], 1, 100.0, 100.0, 1, "v", to_mode="live")
    checks = restart.verify("paper", [], 0, 0, 0, 2, "v")
    assert checks["now trading REAL MONEY"] is False and restart.state()["stage"] == "failed"


def test_mode_switch_keeps_a_pending_apply_now_request():
    ok, _ = restart.request_now()
    assert ok
    restart.begin(None, "paper", [], 0, 0, 0, 1, "v", to_mode="live")
    assert os.path.exists(restart.request_path())


# ---------------------------------------------------------------- dashboard side
class FakeKite:
    def __init__(self, funds=50000.0, profile_error=None):
        self.funds, self.profile_error = funds, profile_error

    def profile(self):
        if self.profile_error:
            raise self.profile_error
        return {"user_id": "AB1234"}

    def margins(self, segment=None):
        return {"available": {"live_balance": self.funds}, "net": self.funds}


class FakeProxy:
    def __init__(self, folder, login_min_ago=2, funds=50000.0, profile_error=None):
        self.TOKEN_FILE = pathlib.Path(folder) / ".kite_token.json"
        self.TOKEN_FILE.write_text(json.dumps({
            "access_token": TOKEN, "user_id": "AB1234",
            "created": (dt.datetime.now(IST) - dt.timedelta(minutes=login_min_ago)).isoformat()}))
        self.kite = FakeKite(funds, profile_error)

    def load_token(self):
        return json.loads(self.TOKEN_FILE.read_text())["access_token"] if self.TOKEN_FILE.exists() else None

    def make_kite(self, access_token=None):
        assert access_token == TOKEN
        return self.kite

    def proxies(self):
        url = f"http://user:{PROXY_PW}@198.51.100.7:8080"
        return {"http": url, "https": url}

    def env(self, name, default=None):
        return {"EXPECTED_EGRESS_IP": EGRESS}[name]


class FakeResponse:
    text = EGRESS


@pytest.fixture
def dash(monkeypatch, tmp_path):
    """Everything ready for real money: settings saved in Windows, fresh login, funds, proxy verified."""
    import requests
    settings = {"DRY_RUN": "false", "CONFIRM_LIVE": "YES", "KITE_ACCOUNT_ACTIVE": "YES", "KITE_ORDER_PERMISSION": "YES"}
    monkeypatch.setattr(livemode, "windows_setting", lambda n: settings.get(n, ""))
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append((url, kw)) or FakeResponse())
    monkeypatch.setattr(web, "STATUS_FILE", os.path.join(C.DATA_DIR, "status.json"))
    p = FakeProxy(tmp_path)
    monkeypatch.setattr(web, "_kite_proxy", lambda: p)
    web._READY.update(at=None, checks=None, funds=None)
    return {"settings": settings, "proxy": p, "requests": calls}


def status(mode="paper", positions=0, running=True, **extra):
    t = dt.datetime.now(IST) - (dt.timedelta(0) if running else dt.timedelta(hours=3))
    with open(web.STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({"time": t.isoformat(), "mode": mode, "rows": {}, "events": [], "live_env": True,
                   "positions": [{"symbol": f"S{i}", "qty": 1} for i in range(positions)], **extra}, f)


WORDS = {"words": ["REAL MONEY"]}


def test_arming_writes_only_todays_arming_and_never_the_settings(dash):
    status("paper")
    flags = (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live(), dict(os.environ))
    ok, msg = web.arm_real_money(WORDS)
    assert ok and "armed for today" in msg and "next safe moment" in msg
    arm = json.load(open(livemode.arm_path()))
    assert set(arm) == livemode.ARM_KEYS and arm["date"] == dt.datetime.now(IST).date().isoformat()   # the India date, whatever the PC's clock
    assert (C.DRY_RUN, C.CONFIRM_LIVE, C.is_live(), dict(os.environ)) == flags


@pytest.mark.parametrize("words", ["", "real money", "REAL  MONEY", "YES", "REAL MONEY!"])
def test_arming_needs_the_exact_words(dash, words):
    status("paper")
    ok, msg = web.arm_real_money({"words": [words]})
    assert not ok and "REAL MONEY" in msg and not os.path.exists(livemode.arm_path())


def test_arming_needs_a_login_from_the_last_10_minutes(dash, tmp_path, monkeypatch):
    status("paper")
    monkeypatch.setattr(web, "_kite_proxy", lambda: FakeProxy(tmp_path, login_min_ago=25))
    ok, msg = web.arm_real_money(WORDS)
    assert not ok and "last 10 minutes" in msg and not os.path.exists(livemode.arm_path())


@pytest.mark.parametrize("broken", ["DRY_RUN", "CONFIRM_LIVE", "KITE_ACCOUNT_ACTIVE", "KITE_ORDER_PERMISSION"])
def test_any_missing_setting_locks_live_trading(dash, broken):
    status("paper", live_env=False)
    dash["settings"][broken] = ""
    ok, msg = web.arm_real_money(WORDS)
    assert not ok and "Live Trading is locked" in msg and not os.path.exists(livemode.arm_path())


def test_low_funds_or_wrong_egress_or_refused_profile_lock_it(dash, tmp_path, monkeypatch):
    status("paper")
    for proxy in (FakeProxy(tmp_path, funds=500.0), FakeProxy(tmp_path, profile_error=RuntimeError("dormant"))):
        monkeypatch.setattr(web, "_kite_proxy", lambda proxy=proxy: proxy)
        assert web.arm_real_money(WORDS)[0] is False
    monkeypatch.setattr(web, "_kite_proxy", lambda: FakeProxy(tmp_path))
    FakeResponse.text = "198.51.100.200"
    try:
        assert web.arm_real_money(WORDS)[0] is False
    finally:
        FakeResponse.text = EGRESS
    assert not os.path.exists(livemode.arm_path())


def test_not_while_already_trading_real_money(dash):
    status("live")
    ok, msg = web.arm_real_money(WORDS)
    assert not ok and "already" in msg


def test_running_bot_without_the_settings_stays_paper(dash):
    status("paper", live_env=False)
    ok, msg = web.arm_real_money(WORDS)
    assert ok and "stays in PAPER" in msg


def test_readiness_never_shows_a_token_password_or_address(dash, tmp_path, monkeypatch):
    status("paper")
    monkeypatch.setattr(web, "_kite_proxy", lambda: FakeProxy(
        tmp_path, profile_error=RuntimeError(f"bad token {TOKEN} via {PROXY_PW}@{EGRESS}")))
    checks = web.check_readiness()
    card = web.trading_mode_card(web.read_status())
    shown = json.dumps(checks) + card
    for secret in (TOKEN, PROXY_PW, EGRESS, "198.51.100.7"):
        assert secret not in shown
    log = open(os.path.join(C.DATA_DIR, "logs", "mode_changes.log"), encoding="utf-8").read()
    assert TOKEN not in log and PROXY_PW not in log
    assert dash["requests"] and dash["requests"][0][1]["proxies"]["https"].startswith("http://user:")


def test_settings_card_locked_then_ready(dash, tmp_path, monkeypatch):
    status("paper")
    card = web.trading_mode_card(web.read_status())
    assert "Check readiness" in card and "NO-GO" in card and "/mode/real" not in card
    dash["settings"]["KITE_ORDER_PERMISSION"] = ""
    web.check_readiness()
    card = web.trading_mode_card(web.read_status())
    assert "Live Trading Locked" in card and "KITE_ORDER_PERMISSION" in card and "/mode/real" not in card
    dash["settings"]["KITE_ORDER_PERMISSION"] = "YES"
    web.check_readiness()
    card = web.trading_mode_card(web.read_status())
    assert "All checks passed" in card and "action='/mode/real'" in card and "Type <b>REAL MONEY</b>" in card
    assert "Risk per trade" in card and "Daily loss limit" in card and "₹" in card
    monkeypatch.setattr(web, "_kite_proxy", lambda: FakeProxy(tmp_path, login_min_ago=30))
    card = web.trading_mode_card(web.read_status())
    assert "last 10 minutes" in card and "disabled" in card


def test_live_card_offers_back_to_paper_and_a_separate_close_all(dash):
    status("live", positions=2)
    card = web.trading_mode_card(web.read_status())
    assert "action='/mode/paper'" in card and "action='/positions/close-all'" in card
    assert card.count("confirm(") == 2 and "cannot be undone" in card
    status("live", positions=2, switch="to_paper")
    card = web.trading_mode_card(web.read_status())
    assert "action='/mode/paper'" not in card and "Switching to PAPER when 2 positions close" in card


def test_switch_to_paper_and_close_all_messages(dash):
    status("live", positions=1)
    livemode.arm(login_at=dt.datetime.now(IST))
    assert "keeps managing its 1 open position" in web.switch_to_paper()
    assert not os.path.exists(livemode.arm_path()) and livemode.paper_requested()
    assert "needs your confirmation" in web.close_all_positions({})
    assert not os.path.exists(livemode.close_all_path())
    assert "Closing all 1 real-money" in web.close_all_positions({"confirm": ["yes"]})
    assert os.path.exists(livemode.close_all_path())


def test_badge_says_what_is_waiting():
    now = dt.datetime.now(IST).isoformat()
    two = web.mode_badge({"time": now, "mode": "live", "switch": "to_paper", "positions": [{}, {}]})
    assert "→ PAPER when 2 close" in two and "Switching to PAPER when 2 positions close" in two   # short + full
    assert "when 1 position closes" in web.mode_badge(
        {"time": now, "mode": "live", "switch": "to_paper", "positions": [{}]})
    assert "Switching to REAL MONEY at the next safe moment" in web.mode_badge(
        {"time": now, "mode": "paper", "switch": "to_live"})
    assert "Switching" not in web.mode_badge({"time": now, "mode": "paper"})


# ---------------------------------------------------------------- the buttons go through the request checks
@pytest.fixture
def server(monkeypatch, dash):
    monkeypatch.setattr(web, "login_url", lambda: None)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    monkeypatch.setattr(C, "WEB_PORT", srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(base, path, fields, origin=True):
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Origin": base} if origin else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, urllib.parse.unquote(r.url)
    except urllib.error.HTTPError as e:
        return e.code, ""


@pytest.mark.parametrize("path", ["/mode/check", "/mode/real", "/mode/paper", "/positions/close-all"])
def test_mode_buttons_need_the_dashboard_page(server, path):
    status("paper")
    fields = {"words": "REAL MONEY", "confirm": "yes"}
    assert post(server, path, fields)[0] == 403                                   # no secret code
    assert post(server, path, {**fields, "csrf": web.CSRF_TOKEN}, origin=False)[0] == 403
    assert not os.path.exists(livemode.arm_path()) and not os.path.exists(livemode.close_all_path())


def test_arm_through_the_page_then_back_to_paper(server):
    status("paper")
    code, url = post(server, "/mode/real", {"words": "REAL MONEY", "csrf": web.CSRF_TOKEN})
    assert code == 200 and "/settings" in url and "REAL MONEY armed" in url and os.path.exists(livemode.arm_path())
    code, url = post(server, "/mode/paper", {"csrf": web.CSRF_TOKEN})
    assert code == 200 and not os.path.exists(livemode.arm_path())
    with urllib.request.urlopen(server + "/settings") as r:
        page = r.read().decode()
    assert "id='mode'" in page and "Trading mode: Paper | Real Money" in page
