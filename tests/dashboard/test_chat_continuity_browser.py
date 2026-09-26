"""Disposable, authenticated browser acceptance path for dashboard chat.

Run after ``npm run build -w web`` with ``pytest -q
tests/dashboard/test_chat_continuity_browser.py``. The PTY child is a fixed
fixture; no provider or model is contacted.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from hermes_state import SessionDB


ROOT = Path(__file__).resolve().parents[2]
PTY_CHILD = Path(__file__).parent / "fixtures" / "continuity_tui.py"
BROWSER = Path(__file__).parent / "fixtures" / "continuity_browser.mjs"


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.parametrize("fleet", [False, True], ids=["named-profile", "registered-fleet"])
def test_rendered_chat_keeps_named_profile_session_and_history(tmp_path, monkeypatch, request, fleet):
    if sys.platform == "win32":
        pytest.skip("POSIX PTY fixture")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or ""
    dist = ROOT / "hermes_cli" / "web_dist"
    if not (dist / "index.html").exists():
        pytest.skip("Build the candidate dashboard with npm run build -w web")

    # Lifespan reads these flags and can otherwise reap a host gateway, run
    # cron, or mint a guest credential. Clear inherited Hermes controls before
    # importing the server, then supply only fixture-owned paths.
    for name in tuple(os.environ):
        if name.startswith("HERMES_"):
            monkeypatch.delenv(name)
    for name in ("GATEWAY_HEALTH_URL", "GATEWAY_HEALTH_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)

    # Both Path.home() and HERMES_HOME must resolve inside this test. A named
    # profile under the candidate's real ~/.hermes would violate isolation.
    home = tmp_path / "user"
    home.mkdir()
    # Chromium's SingletonSocket has a short Unix-path limit; pytest's long
    # per-test path exceeds it. This short disposable directory is removed by
    # a finalizer even when startup fails before the listener's try/finally.
    temp_dir = Path(tempfile.mkdtemp(prefix="hchat-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
    monkeypatch.setenv("TMPDIR", str(temp_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    hermes_home = tmp_path / "hermes-fixture"
    hermes_home.mkdir()
    profile_home = (tmp_path / "external-infra") if fleet else (hermes_home / "profiles" / "acceptance")
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    if fleet:
        import yaml
        (hermes_home / "config.yaml").write_text(yaml.safe_dump({"dashboard": {"fleet_targets": {
            "infra": {"home": str(profile_home), "label": "Infra", "access": "execute"},
        }}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(hermes_home))
    for name in ("CONFIG", "CACHE", "DATA", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))

    session_id = "browser-continuity-session"
    with SessionDB(profile_home / "state.db") as db:
        db.create_session(session_id, "cli")
        db.set_session_title(session_id, "Fixture continuity")
        db.append_message(session_id, "user", "first saved question")
        db.append_message(session_id, "assistant", "first saved answer")

    from hermes_cli import web_server
    from hermes_cli import web_server_chat
    from hermes_cli.pty_session import PtySessionRegistry
    assert web_server.WEB_DIST.resolve() == dist.resolve(), (
        "web_server was imported with a different dashboard bundle"
    )

    # Another pytest module may have imported web_server before this test
    # cleared inherited variables. Give this app run its own token regardless
    # of collection/import order; SPA injection reads the server value live.
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", secrets.token_urlsafe(32))
    monkeypatch.setattr(web_server, "_GATEWAY_HEALTH_URL", None)
    # TestClient lifespans in earlier tests close their module-global PTY
    # registry permanently. Each disposable server run needs its own fresh
    # registry, just as a new production process would have.
    registry = PtySessionRegistry(max_sessions=16, buffer_cap=1024 * 1024, read_timeout=0.2)
    monkeypatch.setattr(web_server_chat, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server, "PTY_REGISTRY", registry)

    def fixture_argv(*, resume=None, sidecar_url=None, profile=None, active_session_file=None):
        assert profile == ("fleet:infra" if fleet else "acceptance")
        assert resume in (None, session_id)
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "HERMES_HOME": str(profile_home),
            "HERMES_TUI_RESUME": resume or "fixture-fresh",
            "HERMES_TEST_ISOLATION": str(hermes_home),
            "PYTHONPATH": str(ROOT),
            "TMPDIR": str(temp_dir),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        if os.environ.get("LANG"):
            child_env["LANG"] = os.environ["LANG"]
        return [sys.executable, str(PTY_CHILD)], str(ROOT), child_env

    monkeypatch.setattr(web_server_chat, "_resolve_chat_argv", fixture_argv)
    old_state = {
        key: getattr(web_server.app.state, key, None)
        for key in ("bound_host", "bound_port", "auth_required", "trusted_public_hosts")
    }
    port = _free_loopback_port()
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = port
    web_server.app.state.auth_required = False
    web_server.app.state.trusted_public_hosts = frozenset()

    server = uvicorn.Server(uvicorn.Config(
        web_server.app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "temporary dashboard did not bind"
        browser_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "HERMES_TEST_ISOLATION": str(hermes_home),
            "TMPDIR": str(temp_dir),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        if os.environ.get("LANG"):
            browser_env["LANG"] = os.environ["LANG"]
        run = subprocess.run(
            ["node", str(BROWSER), f"http://127.0.0.1:{port}", session_id, chrome,
             "fleet" if fleet else "named"],
            cwd=ROOT, env=browser_env, capture_output=True,
            text=True, timeout=65, check=False,
        )
        assert run.returncode == 0, f"browser fixture failed:\n{run.stdout}\n{run.stderr}"
        with SessionDB(profile_home / "state.db", read_only=True) as db:
            messages = db.get_messages(session_id)
        assert [m["content"] for m in messages] == [
            "first saved question", "first saved answer",
            "browser turn one", "fixture reply 2: browser turn one",
            "browser turn two", "fixture reply 3: browser turn two",
            "browser after cancel", "fixture reply 4: browser after cancel",
        ]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for key, value in old_state.items():
            setattr(web_server.app.state, key, value)
        assert not thread.is_alive(), "temporary dashboard failed to stop"
