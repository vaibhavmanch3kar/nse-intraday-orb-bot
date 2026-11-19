"""Buttons that ask "are you sure?" and then send confirm=yes.

On 19 Nov 2026 "Log out of Zerodha" did nothing in the browser. Its form holds a hidden field NAMED confirm, and
inside a form's own onsubmit the bare word `confirm` means that field, not the browser's question box, so
`confirm('...')` threw an error, the form went out unconfirmed and the dashboard answered "needs your
confirmation". The same was true of every button built the same way: Start bot, Close all positions now, Apply
Now, Restart the dashboard and others. They must call window.confirm."""

import os
import re

import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORM = re.compile(r"<form\b.*?</form>", re.S)
ONSUBMIT = re.compile(r"onsubmit=\"(.*?)\"", re.S)
BARE_CONFIRM = re.compile(r"(?<![\w.])confirm\(")


def asks_properly(form_html):
    """A form with a field named confirm may only ask through window.confirm, and must then set the field."""
    if "name='confirm'" not in form_html and 'name="confirm"' not in form_html:
        return True
    m = ONSUBMIT.search(form_html)
    if not m:
        return False
    js = m.group(1)
    if "confirmCapital(" in js:                      # a page-level function: the bare word is fine in there
        return True
    return "window.confirm(" in js and not BARE_CONFIRM.search(js) and "this.confirm.value='yes'" in js


def test_the_check_itself_catches_the_old_markup():
    old = ("<form method='post' action='/zerodha/logout' onsubmit=\"if(!confirm('Log out?'))return false;"
           "this.confirm.value='yes'\"><input type='hidden' name='confirm' value=''><button>x</button></form>")
    assert not asks_properly(old)
    assert asks_properly(old.replace("if(!confirm(", "if(!window.confirm("))


def test_logout_asks_through_the_browser_and_then_confirms():
    html = web.logout_form({})
    assert "action='/zerodha/logout'" in html and asks_properly(html)


def test_every_shared_confirm_button_asks_through_the_browser():
    for action in ("/start", "/bot/apply-now", "/bot/apply-after-close", "/dashboard/restart", "/bot/go-back",
                   "/bot/restart-cancel", "/bot/restart-dismiss"):
        html = web._post_btn(action, "Do it", "Are you sure?")
        assert f"action='{action}'" in html and asks_properly(html), action


def test_no_form_in_the_dashboard_source_asks_with_the_bare_word():
    """Every form written in web.py or webui.py that carries a confirm field (Close all positions, Cancel pending
    change and any added later) asks through window.confirm."""
    for name in ("web.py", "webui.py"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            src = f.read().replace('\\"', '"')
        for m in FORM.finditer(src):
            assert asks_properly(m.group(0)), f"{name}: {m.group(0)[:120]}"


def test_buttons_without_a_confirm_field_are_untouched():
    """Pause, Resume, Stop and Switch back to Paper have no field named confirm, so the bare word still means the
    browser's question box there."""
    with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as f:
        src = f.read()
    assert "onsubmit=\\\"return confirm('Stop the bot?" in src
