"""
Health: one list of checks that answers "is everything working right now?", for the dashboard's health card, the
/api/health address and the popups. It only reads: the bot's saved status and heartbeat, the watchdog's record,
whether today's Zerodha login exists, the backups folder and the disk. It starts nothing, stops nothing and never
contacts the broker (the watchdog does the restarting; Kavach and the bot do the trading checks). The one line
that needs the network, the static-IP proxy, shows the last result of proxycheck.py, which the dashboard runs in
the background while the bot trades real money; a page never waits for it.

Each check is {"key", "name", "state", "detail"} with state one of:
    ok    working
    warn  works, but something needs a look
    bad   not working: act on it
    off   does not apply right now (market closed, paper mode, ...)

When a check changes to warn or bad, or recovers from them, one line is added to data/logs/alerts_<day>.jsonl.
That file is the alert history, and the popups are drawn from it.

    .venv\\Scripts\\python.exe health.py          # print the checks
"""

import datetime as dt
import json
import os
import shutil
import sys
import threading

import backup
import config as C
import holidays
import watchdog

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
RANK = {OFF: 0, OK: 0, WARN: 1, BAD: 2}
BEAT_WARN_SEC = 180           # the bot beats every round; this long without one is worth a look
STATUS_OLD_SEC = 300          # the bot rewrites its status every round
DISK_WARN_GB = 1.0
OBSERVE_EVERY_SEC = 15        # changes are looked for at most this often, however many pages are open

_lock = threading.Lock()
_seen = {"at": None, "states": None}


def now_ist():
    return dt.datetime.now(IST)


def _check(key, name, state, detail):
    return {"key": key, "name": name, "state": state, "detail": detail}


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _age(iso, now):
    try:
        return (now - dt.datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return None


def _mins(sec):
    """An age in words. Whole minutes only: seconds would change on every refresh without telling anything."""
    return "under a minute" if sec < 60 else "1 minute" if sec < 120 else f"{int(sec // 60)} minutes"


def logged_in():
    """Whether today's Zerodha login is saved on this PC. The login itself is never read out."""
    try:
        import kite_proxy
        return bool(kite_proxy.load_token())
    except BaseException:  # noqa: BLE001 - missing Kite settings make kite_proxy exit; that is simply "no"
        return False


def proxy_now(now=None):
    """The static-IP proxy's last check (proxycheck.latest()), starting a new one in the background when due."""
    try:
        import proxycheck
        proxycheck.ensure(now)
        return proxycheck.latest()
    except Exception:  # noqa: BLE001 - the card shows "being checked" rather than break
        return {}


def checks(now=None, status=None, login=None, disk_free_gb=None, store=None, proxy=None):
    """Every check, in the order shown. `status` is the bot's saved status (read from the file when not given);
    `login`, `disk_free_gb`, `store` and `proxy` (a proxycheck.latest() result) are only passed by tests."""
    now = now or now_ist()
    s = status if status is not None else (_read(os.path.join(C.DATA_DIR, "status.json")) or {})
    session = watchdog.in_session(now)
    trading_day = holidays.is_trading_day(now.date())
    wd = watchdog.load(now)
    live = s.get("mode") == "live"
    out = []

    # 1. the bot
    status_age = _age(s.get("time"), now)
    running = status_age is not None and status_age < STATUS_OLD_SEC
    if running:
        out.append(_check("bot", "Bot", OK, f"running in {'REAL MONEY' if live else 'paper'} mode; status "
                                            f"{_mins(status_age)} old"))
    elif not session:
        out.append(_check("bot", "Bot", OFF, "not running: the market is closed" if trading_day else
                          "not running: no trading today"))
    elif wd.get("stopped"):
        out.append(_check("bot", "Bot", WARN, f"stopped on purpose today ({wd['stopped']}); the watchdog leaves it "
                                              f"stopped until someone starts it"))
    else:
        out.append(_check("bot", "Bot", BAD, "NOT running during market hours; the watchdog starts it again "
                                             "within 5 minutes unless it has given up"))

    # 2. heartbeat (a running bot that has stopped going round)
    beat, _pid = watchdog.heartbeat(now)
    if not running:
        out.append(_check("heartbeat", "Bot heartbeat", OFF, "the bot is not running"))
    elif not session:
        # The bot ends by itself after the close while its last status is still minutes fresh. Its heartbeat
        # stopping then is the normal end of the day, not a stuck bot (it raised a false alert on 18 Nov 2026).
        out.append(_check("heartbeat", "Bot heartbeat", OFF, "not watched outside market hours"))
    elif beat is None:
        out.append(_check("heartbeat", "Bot heartbeat", WARN, "no heartbeat file"))
    elif beat > watchdog.HANG_SEC:
        out.append(_check("heartbeat", "Bot heartbeat", BAD, f"no heartbeat for {_mins(beat)}: the bot looks stuck; "
                                                             f"the watchdog ends and restarts it"))
    else:
        out.append(_check("heartbeat", "Bot heartbeat", WARN if beat > BEAT_WARN_SEC else OK,
                          f"last beat {_mins(beat)} ago"))

    # 3. the watchdog's own record
    events = wd.get("events") or []
    if any(e.get("kind") == "gave_up" for e in events):
        out.append(_check("watchdog", "Watchdog", BAD, "gave up restarting the bot today: look at data\\logs"))
    elif wd.get("restarts"):
        out.append(_check("watchdog", "Watchdog", WARN, f"restarted the bot {wd['restarts']} time(s) today "
                                                        f"(limit {watchdog.MAX_RESTARTS})"))
    else:
        out.append(_check("watchdog", "Watchdog", OK, "no automatic restart was needed today"))

    # 4. prices
    feed = s.get("feed") or {}
    if not running or not feed:
        out.append(_check("feed", "Price feed", OFF, "the bot is not running" if not running else "not reported yet"))
    elif feed.get("live_needs_kite"):
        out.append(_check("feed", "Price feed", BAD, "real money needs Kite prices, and they are not arriving: new "
                                                     "trades are blocked"))
    elif feed.get("errors") or feed.get("problems") or (feed.get("stocks_ok") or 0) < (feed.get("stocks") or 0):
        n = len(feed.get("problems") or {}) + len(feed.get("errors") or [])
        out.append(_check("feed", "Price feed", WARN, f"{feed.get('stocks_ok')} of {feed.get('stocks')} stocks have "
                                                      f"good candles ({n} problem(s)); source {feed.get('source')}"))
    else:
        out.append(_check("feed", "Price feed", OK, f"{feed.get('stocks_ok')} of {feed.get('stocks')} stocks, newest "
                                                    f"candle {feed.get('newest_candle')}, source {feed.get('source')}"))

    # 5. Zerodha login
    has_login = logged_in() if login is None else login
    needs = live or C.DATA_SOURCE == "kite"
    if has_login:
        out.append(_check("login", "Zerodha login", OK, "today's login is saved on this PC"))
    elif needs and trading_day:
        out.append(_check("login", "Zerodha login", BAD if live and session else WARN,
                          "not logged in today: Kite prices and real-money trading need it"))
    else:
        out.append(_check("login", "Zerodha login", OFF, "not logged in; not needed right now"))

    # 5b. how the saved login is kept on disk
    try:
        import kite_proxy
        kept = kite_proxy.token_storage() if store is None else store
    except BaseException:  # noqa: BLE001
        kept = "none"
    if kept == "plain":
        out.append(_check("login_store", "Login kept safely", WARN, "the saved login is in plain text; it is "
                          "protected by Windows the next time it is read or you log in"))
    elif kept == "protected":
        out.append(_check("login_store", "Login kept safely", OK, "protected by Windows for this user on this PC"))
    else:
        out.append(_check("login_store", "Login kept safely", OFF, "no login is saved"))

    # 5c. the static-IP proxy that every Kite request and every real-money order goes through
    if not live or not running:
        out.append(_check("proxy", "Static-IP proxy", OFF, "only checked while trading real money; Settings > "
                                                           "Trading mode > Check readiness checks it any time"))
    else:
        px = proxy if proxy is not None else proxy_now()
        last = px.get("last") or {}
        age = _age(last.get("at"), now)
        if age is None:
            out.append(_check("proxy", "Static-IP proxy", OFF, "being checked"))
        elif last.get("egress_ok") is False:
            out.append(_check("proxy", "Static-IP proxy", BAD, "Kite traffic does NOT leave from the whitelisted "
                                                               "address: Zerodha refuses orders from any other"))
        elif not last.get("ok"):
            down = _age(px.get("failing_since"), now) or 0
            if (px.get("fails") or 0) >= 2:
                out.append(_check("proxy", "Static-IP proxy", BAD, f"NOT reachable for {_mins(down)} "
                                                                   f"({last.get('error')}): real orders cannot be "
                                                                   f"sent; checked again every minute"))
            else:
                out.append(_check("proxy", "Static-IP proxy", WARN, f"the last check failed ({last.get('error')}); "
                                                                    f"checked again within a minute"))
        elif age > 3 * _proxy_every():
            out.append(_check("proxy", "Static-IP proxy", WARN, f"not checked for {_mins(age)}"))
        else:
            out.append(_check("proxy", "Static-IP proxy", OK, f"working, leaves from the whitelisted address; "
                                                              f"checked {_mins(age)} ago ({last.get('ms')} ms)"))

    # 6. the bot's books against Zerodha (real money only)
    recon = s.get("recon") or {}
    if not live or not running:
        out.append(_check("recon", "Books match Zerodha", OFF, "only checked while trading real money"))
    elif recon.get("problems") or recon.get("ok") is False:
        out.append(_check("recon", "Books match Zerodha", BAD, "; ".join(map(str, recon.get("problems") or [])) or
                          "the last comparison failed"))
    else:
        age = _age(recon.get("at"), now)
        late = age is None or age > 3 * C.RECON_EVERY_SEC
        out.append(_check("recon", "Books match Zerodha", WARN if late else OK,
                          "no comparison yet" if age is None else f"matched {_mins(age)} ago"))

    # 7. is anything holding new trades back?
    if not running:
        out.append(_check("entries", "New trades", OFF, "the bot is not running"))
    elif s.get("entry_hold"):
        out.append(_check("entries", "New trades", BAD, f"held: {s['entry_hold']}"))
    elif s.get("paused"):
        out.append(_check("entries", "New trades", WARN, "paused by you: open positions are still managed"))
    elif s.get("cooldown_until") and now.date().isoformat() < str(s["cooldown_until"]):
        # the saved date stays in the bot's record after it has passed: only a date still ahead holds trades back
        out.append(_check("entries", "New trades", WARN, f"cool-down until {s['cooldown_until']}"))
    elif now.strftime("%H:%M") > C.LAST_ENTRY:
        out.append(_check("entries", "New trades", OFF, f"none after {C.LAST_ENTRY} (the last-entry time); open "
                                                        f"positions are still managed"))
    else:
        out.append(_check("entries", "New trades", OK, "allowed, subject to Kavach's checks"))

    # 8. the daily loss limit
    eq, day = s.get("equity"), float(s.get("day_pnl") or 0.0)
    if not eq:
        out.append(_check("day_loss", "Daily loss limit", OFF, "no account figure yet"))
    else:
        start = float(eq) - day
        used = max(0.0, -day) / start * 100 if start else 0.0
        share = used / C.MAX_DAILY_LOSS_PCT * 100 if C.MAX_DAILY_LOSS_PCT else 0.0
        out.append(_check("day_loss", "Daily loss limit", BAD if share >= 100 else WARN if share >= 75 else OK,
                          f"{used:.2f}% of the {C.MAX_DAILY_LOSS_PCT:g}% limit used today"))

    # 9. crash reports
    today = now.strftime("%Y-%m-%d")
    try:
        crashes = [n for n in os.listdir(watchdog.logs_dir()) if n.startswith("crash_") and today in n]
    except OSError:
        crashes = []
    out.append(_check("crashes", "Crash reports", WARN if crashes else OK,
                      f"{len(crashes)} today in data\\logs" if crashes else "none today"))

    # 10. backups
    try:
        zips = backup.backups()
    except Exception:  # noqa: BLE001
        zips = []
    newest = max((os.path.getmtime(p) for p in _paths(zips)), default=None)
    if newest is None:
        out.append(_check("backup", "Data backup", WARN, "no backup found yet (one is made after each market close)"))
    else:
        days = (now.date() - dt.datetime.fromtimestamp(newest, IST).date()).days
        out.append(_check("backup", "Data backup", WARN if days > 4 else OK,
                          "made today" if days == 0 else "made yesterday" if days == 1 else f"newest is {days} days old"))

    # 11. disk
    if disk_free_gb is None:
        try:
            disk_free_gb = shutil.disk_usage(os.path.abspath(C.DATA_DIR)).free / 1e9
        except OSError:
            disk_free_gb = None
    if disk_free_gb is None:
        out.append(_check("disk", "Disk space", OFF, "could not be measured"))
    else:
        out.append(_check("disk", "Disk space", WARN if disk_free_gb < DISK_WARN_GB else OK,
                          f"{disk_free_gb:.1f} GB free where the data is kept"))
    return out


def _proxy_every():
    try:
        import proxycheck
        return proxycheck.CHECK_EVERY_SEC
    except Exception:  # noqa: BLE001
        return 300


def _paths(zips):
    """backup.backups() entries as paths, whatever shape it returns them in."""
    out = []
    for z in zips or []:
        p = z.get("path") if isinstance(z, dict) else z[0] if isinstance(z, (tuple, list)) else z
        if isinstance(p, str):
            out.append(p if os.path.isabs(p) else os.path.join(backup.backup_dir(), p))
    return [p for p in out if os.path.exists(p)]


def overall(rows):
    worst = max((RANK[r["state"]] for r in rows), default=0)
    return BAD if worst == 2 else WARN if worst == 1 else OK


def report(now=None, status=None):
    """What /api/health sends: no secrets, no account numbers, only states and plain sentences."""
    now = now or now_ist()
    rows = checks(now, status)
    return {"at": now.isoformat(timespec="seconds"), "overall": overall(rows), "checks": rows,
            "bad": [r["key"] for r in rows if r["state"] == BAD], "warn": [r["key"] for r in rows if r["state"] == WARN]}


# ---------------------------------------------------------------- alert history
def alerts_path(day):
    return os.path.join(watchdog.logs_dir(), f"alerts_{day.isoformat()}.jsonl")


def alerts(day=None):
    """The day's alert lines, oldest first."""
    day = day or now_ist().date()
    out = []
    try:
        with open(alerts_path(day), encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def observe(now=None, rows=None):
    """Compare the checks with what they were last time and write one alert line per change that matters:
    a check going to warn or bad, or coming back from them. Returns the lines written. Never raises."""
    now = now or now_ist()
    try:
        with _lock:
            if rows is None and _seen["at"] and (now - _seen["at"]).total_seconds() < OBSERVE_EVERY_SEC:
                return []
            rows = rows if rows is not None else checks(now)
            cur = {r["key"]: r["state"] for r in rows}
            prev = _seen["states"] or {}          # after a dashboard restart, what is wrong now is said again
            _seen.update(at=now, states=cur)
            written = []
            for r in rows:
                was, is_ = prev.get(r["key"], OK), r["state"]
                if RANK[is_] == RANK[was] or (RANK[is_] == 0 and RANK.get(was, 0) == 0):
                    continue
                written.append({"at": now.isoformat(timespec="seconds"), "key": r["key"], "name": r["name"],
                                "state": is_ if RANK[is_] else OK, "was": was, "detail": r["detail"]})
            if written:
                os.makedirs(watchdog.logs_dir(), exist_ok=True)
                with open(alerts_path(now.date()), "a", encoding="utf-8") as fh:
                    for w in written:
                        fh.write(json.dumps(w, ensure_ascii=False) + "\n")
            return written
    except Exception:  # noqa: BLE001 - a health problem must never break the page that reports health
        return []


def notices(now=None):
    """Today's alerts as popups: [{key, tone, title, body, at}] (same shape as watchdog.notices)."""
    now = now or now_ist()
    tone = {BAD: "dn", WARN: "warn", OK: "up"}
    out = []
    for i, a in enumerate(alerts(now.date())):
        back = a["state"] == OK
        out.append({"key": f"health-{a['at']}-{a['key']}-{i}", "tone": tone[a["state"]], "at": a["at"],
                    "title": f"{a['name']}: {'working again' if back else 'needs attention' if a['state'] == WARN else 'NOT working'}",
                    "body": a["detail"]})
    return out


def reset():
    """Forget what was seen (tests)."""
    with _lock:
        _seen.update(at=None, states=None)


def main():
    rep = report()
    for r in rep["checks"]:
        print(f"{r['state']:5s} {r['name']:22s} {r['detail']}")
    print("overall:", rep["overall"])
    return 0 if rep["overall"] != BAD else 1


if __name__ == "__main__":
    sys.exit(main())
