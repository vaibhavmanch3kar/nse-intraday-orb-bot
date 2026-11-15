"""Ramayana theme: themed names always keep the plain trading term, and data stays plain."""

import theme as T
import web


def test_every_term_keeps_its_plain_meaning():
    for key, (themed, plain) in T.TERMS.items():
        assert T.term(key) == f"{themed} · {plain}"
    assert T.mode_label("paper") == "Abhyas · PAPER" and T.mode_label("LIVE") == "Yuddha · LIVE"


def test_exit_reasons_get_a_name_but_keep_the_plain_text():
    cases = {"stop": "Lakshman Rekha", "stop (gap)": "Lakshman Rekha", "exchange stop": "Lakshman Rekha",
             "time stop": "Muhurat", "target": "Lakshya", "square-off": "Sandhya", "trail EMA21": "Anusaran"}
    for reason, name in cases.items():
        label = T.reason_label(reason)
        assert label.startswith(name + " · ") and reason in label
    assert T.reason_label("left over from earlier day") == "left over from earlier day"
    assert T.reason_label("") == "-"


def test_decorate_adds_names_to_risk_phrases_only():
    s = T.decorate("LONG breakout - skip: daily loss limit 2.0% hit (booked + open)")
    assert "Maryada · daily loss limit" in s and s.startswith("LONG breakout")
    assert "Vanvas · cooldown" in T.decorate("skip: cooldown until 2026-11-18")
    assert T.decorate("LONG breakout (paused)").endswith("(Vishram · paused)")
    assert T.decorate("inside range") == "inside range" and T.decorate(None) == ""


def test_console_banner_is_plain_ascii_framed():
    b = T.banner("Abhyas · PAPER mode").splitlines()
    assert b[0] == b[-1] == "=" * 64 and "AYODHYA ORB" in b[1] and "Ramayana edition" in b[2]


def test_every_dashboard_page_has_the_realm_header_and_themed_nav():
    for html_page in (web.render(), web.render_trades(), web.render_journey("paper", None)):
        assert T.DEVANAGARI in html_page and T.APP in html_page and "<footer>" in html_page
        for _, key, _ in T.PAGES:
            assert T.name(key) in html_page and T.plain(key) in html_page
    live = web.render()
    assert "Lakshman Rekha · stop" in live and "Sandesh · Recent events" in live
    assert "Panchang · NSE calendar" in live and "Abhyas · PAPER" in live
