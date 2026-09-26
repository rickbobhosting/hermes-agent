"""External chat resumes stay inside the selected registered home."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
import yaml
from fastapi import HTTPException
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def fleet_chat(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli import web_server_chat
    from hermes_cli import main_tui_launch

    dashboard, alpha, beta = (tmp_path / name for name in ("dashboard", "alpha", "beta"))
    for home in (dashboard, alpha, beta):
        home.mkdir()
        (home / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    for home, session_id in ((alpha, "alpha-session"), (beta, "beta-session")):
        db = SessionDB(db_path=home / "state.db")
        db.create_session(session_id, "web")
        db.close()

    monkeypatch.setenv("HERMES_HOME", str(dashboard))
    monkeypatch.setenv("OPENAI_API_KEY", "dashboard-test-key")
    (alpha / ".env").write_text("OPENAI_API_KEY=alpha-test-key\n", encoding="utf-8")
    monkeypatch.setattr(main_tui_launch, "_make_tui_argv", lambda *_a, **_kw: (["fake-tui"], tmp_path))
    monkeypatch.setattr(main_tui_launch, "_apply_tui_python_env", lambda _env: None)
    monkeypatch.setattr(web_server_chat, "_build_gateway_ws_url", lambda: None)

    def access(alpha_access="execute", beta_access="execute"):
        (dashboard / "config.yaml").write_text(yaml.safe_dump({"dashboard": {"fleet_targets": {
            "alpha": {"home": str(alpha), "access": alpha_access},
            "beta": {"home": str(beta), "access": beta_access},
        }}}), encoding="utf-8")

    access()
    return dashboard, alpha, beta, access


def test_fleet_argv_uses_registered_home_and_exact_owned_resume(fleet_chat):
    from hermes_cli.web_server_chat import _resolve_chat_argv

    _, alpha, beta, access = fleet_chat
    argv, cwd, env = _resolve_chat_argv(profile="fleet:alpha", resume="alpha-session")
    assert argv == ["fake-tui"]
    assert env["HERMES_HOME"] == str(alpha)
    assert env["HERMES_TUI_RESUME"] == "alpha-session"
    assert env.get("OPENAI_API_KEY") == "alpha-test-key"
    assert "HERMES_TUI_GATEWAY_URL" not in env

    for invalid in ("beta-session", "alpha-sess", "../beta/state.db"):
        with pytest.raises(HTTPException) as exc:
            _resolve_chat_argv(profile="fleet:alpha", resume=invalid)
        assert exc.value.status_code == 404

    access(alpha_access="manage")
    with pytest.raises(HTTPException) as exc:
        _resolve_chat_argv(profile="fleet:alpha", resume="alpha-session")
    assert exc.value.status_code == 403
    assert (beta / "state.db").is_file()


def test_fleet_pty_refuses_non_execute_before_spawning(fleet_chat, monkeypatch):
    from hermes_cli import web_server_chat
    from hermes_cli.web_server import app, _SESSION_TOKEN

    _, alpha, _, access = fleet_chat
    access(alpha_access="inspect")
    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", staticmethod(
        lambda *_a, **_kw: pytest.fail("unauthorized PTY spawned")))
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/pty?token={_SESSION_TOKEN}&profile=fleet:alpha&attach={'a' * 32}"
        ) as ws:
            assert "requires execute access" in ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    assert (alpha / "state.db").is_file()


def test_retained_channel_is_bound_to_registered_home(fleet_chat):
    from hermes_cli.web_routers.chat_ws import _retained_channel

    _, alpha, beta, _ = fleet_chat
    token = "a" * 32
    assert _retained_channel(token, f"fleet:alpha\0{alpha}") != _retained_channel(
        token, f"fleet:beta\0{beta}")
    assert _retained_channel(token, f"fleet:alpha\0{alpha}") != _retained_channel(
        token, f"fleet:alpha\0{beta}")


def test_fleet_pty_requires_retained_attachment(fleet_chat, monkeypatch):
    from hermes_cli import web_server_chat
    from hermes_cli.web_server import app, _SESSION_TOKEN

    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", staticmethod(
        lambda *_a, **_kw: pytest.fail("unretained fleet PTY spawned")))
    with TestClient(app) as client:
        with client.websocket_connect(f"/api/pty?token={_SESSION_TOKEN}&profile=fleet:alpha") as ws:
            assert "requires a retained attachment token" in ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_execute_pty_spawns_in_selected_home_and_keeps_targets_separate(fleet_chat, monkeypatch):
    from hermes_cli import web_server_chat
    from hermes_cli.pty_session import PtySessionRegistry
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    _, alpha, beta, access = fleet_chat
    spawned = []
    bridges = []

    class Bridge:
        def __init__(self):
            self.closed = False

        def read(self, _timeout):
            return b""

        def write_owned(self, _data, **_kwargs):
            return True

        def resize(self, **_kwargs):
            pass

        def close(self):
            self.closed = True

    def spawn(_argv, *, cwd=None, env=None):
        spawned.append(env.copy())
        bridge = Bridge()
        bridges.append(bridge)
        return bridge

    registry = PtySessionRegistry(max_sessions=4, buffer_cap=1024, read_timeout=0.01)
    monkeypatch.setattr(web_server_chat, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", staticmethod(spawn))
    @asynccontextmanager
    async def test_lifespan(test_app):
        test_app.state.event_channels = {}
        test_app.state.event_lock = asyncio.Lock()
        test_app.state.pty_active_session_files = {}
        test_app.state.pty_active_session_file_claims = {}
        test_app.state.chat_argv_lock = asyncio.Lock()
        try:
            yield
        finally:
            await registry.close_all()

    monkeypatch.setattr(app.router, "lifespan_context", test_lifespan)
    token = "b" * 32
    with TestClient(app) as client:
        for selector, session_id in (("alpha", "alpha-session"), ("beta", "beta-session")):
            with client.websocket_connect(
                f"/api/pty?token={_SESSION_TOKEN}&profile=fleet:{selector}"
                f"&resume={session_id}&attach={token}"
            ) as ws:
                ws.send_bytes(b"hello")
                deadline = time.monotonic() + 2
                while len(spawned) < (1 if selector == "alpha" else 2) and time.monotonic() < deadline:
                    time.sleep(0.01)
        headers = {_SESSION_HEADER_NAME: _SESSION_TOKEN}
        listed = client.get("/api/pty/sessions", headers=headers)
        assert listed.status_code == 200
        alpha_session = next(s for s in listed.json()["sessions"] if s["metadata"]["profile"] == "fleet:alpha")
        access(alpha_access="manage")
        assert client.delete(f"/api/pty/sessions/{alpha_session['id']}", headers=headers).status_code == 403
        assert client.delete(f"/api/pty/sessions/{alpha_session['id']}?profile=fleet:beta", headers=headers).status_code == 403
        from hermes_cli.web_routers.chat_ws import reap_revoked_fleet_ptys_once
        client.portal.call(reap_revoked_fleet_ptys_once, app)
        remaining = client.get("/api/pty/sessions", headers=headers).json()["sessions"]
        assert len(remaining) == 1
        assert remaining[0]["metadata"]["profile"] == "fleet:beta"
        assert bridges[0].closed and not bridges[1].closed
        beta_session = remaining[0]
        access(beta_access="inspect")
        assert client.delete(f"/api/pty/sessions/{beta_session['id']}?profile=fleet:beta", headers=headers).status_code == 200
        assert bridges[1].closed

        async def add_local():
            session, _ = await registry.attach_or_spawn(
                "local-task", spawn=Bridge, metadata={"profile": "default"})
            return session.id

        local_id = client.portal.call(add_local)
        assert client.delete(f"/api/pty/sessions/{local_id}?profile=fleet:alpha", headers=headers).status_code == 403
        assert client.request("DELETE", f"/api/pty/sessions/{local_id}",
                              json={"profile": "fleet:alpha"}, headers=headers).status_code == 403
        assert client.delete(f"/api/pty/sessions/{local_id}", headers=headers).status_code == 200
    assert [item["HERMES_HOME"] for item in spawned] == [str(alpha), str(beta)]
    assert [item["HERMES_TUI_RESUME"] for item in spawned] == ["alpha-session", "beta-session"]


def test_attached_fleet_pty_stops_before_forwarding_input_after_revocation(fleet_chat, monkeypatch):
    from hermes_cli import web_server_chat
    from hermes_cli.pty_session import PtySessionRegistry
    from hermes_cli.web_server import app, _SESSION_TOKEN

    _, _, _, access = fleet_chat
    writes = []
    closed = []

    class Bridge:
        def read(self, _timeout):
            return b""

        def write_owned(self, data, **_kwargs):
            writes.append(data)
            return True

        def resize(self, **_kwargs):
            pass

        def close(self):
            closed.append(True)

    registry = PtySessionRegistry(max_sessions=2, buffer_cap=1024, read_timeout=0.01)
    monkeypatch.setattr(web_server_chat, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", staticmethod(lambda *_a, **_kw: Bridge()))

    @asynccontextmanager
    async def test_lifespan(test_app):
        test_app.state.event_channels = {}
        test_app.state.event_lock = asyncio.Lock()
        test_app.state.pty_active_session_files = {}
        test_app.state.pty_active_session_file_claims = {}
        test_app.state.chat_argv_lock = asyncio.Lock()
        try:
            yield
        finally:
            await registry.close_all()

    monkeypatch.setattr(app.router, "lifespan_context", test_lifespan)
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/pty?token={_SESSION_TOKEN}&profile=fleet:alpha&attach={'c' * 32}"
        ) as ws:
            ws.send_bytes(b"before")
            deadline = time.monotonic() + 2
            while b"before" not in writes and time.monotonic() < deadline:
                time.sleep(0.01)
            assert b"before" in writes
            access(alpha_access="inspect")
            ws.send_bytes(b"after")
            with pytest.raises(WebSocketDisconnect):
                ws.receive_bytes()
            assert b"after" not in writes
        assert closed
        assert not client.get("/api/pty/sessions", headers={"X-Hermes-Session-Token": _SESSION_TOKEN}).json()["sessions"]
