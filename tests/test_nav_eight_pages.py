"""Phase 6 navigation: all eight pages are one click away on every page, each page lights its own tab, the four
2.0 pages keep one name in every theme, and the Swarajya names typed as addresses lead to the page."""

import re

import pytest

import theme as T
import web
import webui

PATHS = ["/", "/trades", "/review", "/scanner", "/chanakya", "/kavach", "/rajya", "/yantrashala"]


def nav(page, cls):
    return re.search(rf"<nav class='{cls}'.*?</nav>", page, re.S).group(0)


def lit(page, cls="tabs"):
    return [h.split("?")[0] for h in re.findall(r"<a class='on' href='([^']*)'", nav(page, cls))]


def test_the_eight_pages_are_in_the_briefs_order_on_desktop_and_phone():
    assert [n[0] for n in webui.NAV] == PATHS
    page = web.render()
    for cls in ("tabs", "bottom"):
        assert [h.split("?")[0] for h in re.findall(r"href='([^']*)'", nav(page, cls))] == PATHS
    for name in ("Chanakya", "Kavach", "Rajya", "Yantrashala"):
        assert name in nav(page, "tabs")
    assert T.name("live_page") in nav(page, "tabs") and T.name("scanner") in nav(page, "tabs")


@pytest.mark.parametrize("render, path", [
    (lambda: web.render(), "/"), (lambda: web.render_trades(), "/trades"), (lambda: web.render_kavach(), "/kavach"),
    (lambda: web.render_chanakya(), "/chanakya"), (lambda: web.render_rajya(), "/rajya"),
    (lambda: web.render_yantrashala(), "/yantrashala"), (lambda: web.render_journey("paper", None), "/trades")])
def test_each_page_lights_exactly_its_own_tab(render, path):
    page = render()
    assert lit(page) == [path] and lit(page, "bottom") == [path]
    assert nav(page, "tabs").count("aria-current='page'") == 1


def test_the_new_pages_keep_their_names_in_every_theme():
    import uitheme
    for tid in uitheme.THEMES:
        token = uitheme.activate(tid)
        try:
            tabs = nav(web.render_kavach(), "tabs")
            for name, what in webui.PAGE_NAMES.values():
                assert name in tabs and what in tabs
        finally:
            uitheme.reset(token)


def test_swarajya_names_are_addresses_and_the_bar_scrolls_to_the_current_page():
    assert web.PAGE_ALIASES == {"/gad": "/", "/daftar": "/trades", "/bakhar": "/review", "/buruj": "/scanner",
                                "/buruj-watch": "/scanner"}
    src = open(web.__file__, encoding="utf-8").read()
    assert "u.path in PAGE_ALIASES" in src
    page = web.render()
    assert webui.NAV_JS in page and "overflow-x:auto" in webui.css()
    assert ".tabs{order:3;flex:1 0 100%}" in webui.css()        # own row when the header is too narrow for eight
