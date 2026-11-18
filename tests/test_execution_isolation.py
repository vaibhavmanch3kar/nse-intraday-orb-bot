"""Who is able to trade, checked from the code itself: only broker.py can send, change or cancel an order; only the
bot loads broker.py; the dashboard and every analysis module (Chanakya, Rajya, Yantrashala, Bakhar, the scanner,
the review, health) have no path to it; and the dashboard's two views of Zerodha can only read."""

import ast
import os

import pytest

import accounts
import buildinfo
import rajya

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER_CALLS = {"place_order", "modify_order", "cancel_order", "exit_order", "place_gtt", "modify_gtt", "delete_gtt",
               "convert_position", "place_mf_order", "cancel_mf_order", "place_mf_sip", "modify_mf_sip", "cancel_mf_sip",
               "initiate_holdings_authorisation"}
ANALYSIS = ("chanakya.py", "rajya.py", "yantra.py", "yantra_strategies.py", "bakhar.py", "scanner.py", "review.py",
            "health.py", "edge.py", "timemachine.py", "eventlog.py", "secretbox.py", "secure_secrets.py", "readiness.py")


def project_files():
    return sorted(n for n in os.listdir(ROOT) if n.endswith(".py"))


def tree(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return ast.parse(fh.read(), name)


def imports(name):
    """Every module a file imports, at any depth (top of the file or inside a function)."""
    out = set()
    for node in ast.walk(tree(name)):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def order_calls(name):
    return sorted({node.func.attr for node in ast.walk(tree(name))
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ORDER_CALLS})


def reaches(name, target, seen=None):
    """Whether `name` can load `target` through project imports."""
    seen = seen if seen is not None else set()
    if name in seen:
        return False
    seen.add(name)
    local = {m + ".py" for m in imports(name)} & set(project_files())
    return target in local or any(reaches(n, target, seen) for n in local)


def test_only_the_broker_module_can_send_change_or_cancel_an_order():
    callers = {n: order_calls(n) for n in project_files() if order_calls(n)}
    assert callers == {"broker.py": ["cancel_order", "place_order"]}


def test_only_the_bot_loads_the_broker_module():
    assert [n for n in project_files() if "broker" in imports(n)] == ["bot.py"]
    # paper_replay.py drives the real loop on purpose, on virtual money only (tests/test_paper_replay.py proves it
    # refuses to run live); nothing else may load the bot, and nothing the dashboard loads may load paper_replay
    assert [n for n in project_files() if "bot" in imports(n) and n != "bot.py"] == ["paper_replay.py"]
    assert [n for n in project_files() if "paper_replay" in imports(n)] == []


@pytest.mark.parametrize("name", ANALYSIS + ("web.py", "webui.py", "accounts.py", "jobs.py"))
def test_the_dashboard_and_every_analysis_module_have_no_path_to_the_broker(name):
    assert os.path.exists(os.path.join(ROOT, name)), name
    assert not reaches(name, "broker.py") and not reaches(name, "bot.py")
    assert order_calls(name) == []


@pytest.mark.parametrize("name", ("chanakya.py", "yantra.py", "yantra_strategies.py", "bakhar.py", "health.py",
                                  "eventlog.py", "scanner.py", "review.py"))
def test_reasoning_modules_do_not_even_hold_a_zerodha_client(name):
    assert not imports(name) & {"kiteconnect", "accounts", "data_kite"} - ({"data_kite"} if name in ("scanner.py", "review.py") else set())
    assert "KiteConnect(" not in open(os.path.join(ROOT, name), encoding="utf-8").read()


def test_new_analysis_pages_are_dashboard_files_not_bot_files():
    bot = {os.path.basename(p) for p in buildinfo.bot_files()}
    for name in ("chanakya.py", "rajya.py", "yantra.py", "yantra_strategies.py", "bakhar.py", "health.py", "secure_secrets.py"):
        assert name not in bot, name


def test_the_dashboards_views_of_zerodha_can_only_read():
    class Kite:
        def place_order(self, **kw):
            raise AssertionError("an order was attempted")

        def positions(self):
            return {"net": []}

        def holdings(self):
            return []

    ro = accounts.ReadOnlyKite(Kite())
    assert set(accounts.READ_ONLY_CALLS) == {"margins", "positions", "orders"} and ro.positions() == {"net": []}
    for call in ORDER_CALLS | {"holdings", "profile", "generate_session"}:
        with pytest.raises(PermissionError):
            getattr(ro, call)
    only = rajya.HoldingsOnly(Kite())
    assert only.holdings() == [] and [n for n in dir(only) if not n.startswith("_")] == ["holdings"]


def test_the_dashboard_changes_the_bot_only_through_small_request_files():
    """Stop, Pause, Start, Apply and the mode switch are files or a start request the bot acts on by its own
    rules; the dashboard's form handler never calls into trading code."""
    src = open(os.path.join(ROOT, "web.py"), encoding="utf-8").read()
    post = src[src.index("    def do_POST"):src.index("\ndef serve")]
    for word in ("broker.", "LiveBroker", "kite.place", "close_position(", "enter("):
        assert word not in post, word
    assert "C.STOP_FILE" in post and "C.PAUSE_FILE" in post and "csrf_ok(form)" in post and "origin_ok(" in post
