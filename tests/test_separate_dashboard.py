"""The dashboard is its own program: the bot no longer serves it, it runs and controls the bot through files,
the launcher starts both, and edited theme files show without a restart."""

import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

import bot
import config as C
import uitheme

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_bot_no_longer_starts_the_dashboard(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    import web
    monkeypatch.setattr(bot, "setup_logging", lambda: None)
    monkeypatch.setattr(bot, "claim_single_instance", lambda *a: True)
    monkeypatch.setattr(bot, "dashboard_listening", lambda *a: False)
    monkeypatch.setattr(web, "serve", lambda: (_ for _ in ()).throw(AssertionError("the bot must not serve pages")))
    ran = []

    class FakeBot:
        def run(self):
            ran.append(1)

    monkeypatch.setattr(bot, "Bot", FakeBot)
    bot.main()
    assert ran == [1]
    assert any("Dashboard not running" in r.getMessage() for r in caplog.records)


def test_launcher_starts_dashboard_then_bot():
    ps = open(os.path.join(ROOT, "run_bot.ps1"), encoding="utf-8").read()
    assert 'Start-Process -FilePath $py -ArgumentList @("web.py", "--no-browser")' in ps
    assert ps.index("Start-Dashboard\nStart-Job") < ps.index("& $py bot.py")
    assert "-LocalPort 5000 -State Listen" in ps               # never a second dashboard
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                              f"'{os.path.join(ROOT, 'run_bot.ps1')}',[ref]$null,[ref]$e);$e.Count"],
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "0", out.stdout + out.stderr


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dashboard_runs_as_its_own_program_and_controls_the_bot_by_files(tmp_path):
    port = free_port()
    env = dict(os.environ, DATA_DIR=str(tmp_path), WEB_PORT=str(port), DATA_SOURCE="yahoo")
    p = subprocess.Popen([sys.executable, "-B", "web.py", "--no-browser"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urllib.request.urlopen(url + "/api/jobs", timeout=2) as r:
                    assert r.status == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("dashboard did not start")
        with urllib.request.urlopen(url + "/", timeout=10) as r:
            assert r.status == 200 and "Ayodhya" in r.read().decode("utf-8")
        req = urllib.request.Request(url + "/pause", data=b"", method="POST", headers={"Origin": url})
        urllib.request.urlopen(req, timeout=5).read()
        assert os.path.exists(os.path.join(str(tmp_path), "PAUSE"))   # the bot reads this file
    finally:
        p.terminate()
        p.wait(10)


@pytest.fixture
def theme_copy(tmp_path, monkeypatch):
    root = tmp_path / "themes"
    shutil.copytree(uitheme.ROOT, root)
    monkeypatch.setattr(uitheme, "ROOT", str(root))
    reg, themes = uitheme.load()
    monkeypatch.setattr(uitheme, "REGISTRY", reg)
    monkeypatch.setattr(uitheme, "THEMES", themes)
    monkeypatch.setattr(uitheme, "_SIG", uitheme._signature())
    monkeypatch.setattr(uitheme, "_checked", 0.0)
    return root


def test_edited_theme_files_show_without_a_restart(theme_copy):
    css = theme_copy / "roblox_inspired" / "tokens.css"
    old = uitheme.THEMES["roblox_inspired"].tokens["any"].get("radius")
    text = css.read_text(encoding="utf-8")
    css.write_text(text.replace(f"--radius: {old};", "--radius: 7px;"), encoding="utf-8")
    assert uitheme.maybe_reload(min_interval=0) is True
    assert uitheme.THEMES["roblox_inspired"].tokens["any"]["radius"] == "7px"
    assert uitheme.maybe_reload(min_interval=0) is False                 # nothing new


def test_a_broken_theme_edit_keeps_the_loaded_themes(theme_copy):
    before = uitheme.THEMES
    (theme_copy / "minecraft_inspired" / "strings.json").write_text("{ half saved", encoding="utf-8")
    assert uitheme.maybe_reload(min_interval=0) is False
    assert uitheme.THEMES is before and "minecraft_inspired" in uitheme.THEMES
