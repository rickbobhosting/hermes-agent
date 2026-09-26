import json
import time
import asyncio
from contextlib import asynccontextmanager

import pytest

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat
from hermes_cli.pty_session import PtySessionRegistry


TOKEN = "a" * 32


class FakeBridge:
    def __init__(self):
        self.alive = True
        self.accept_input = True
        self.written = bytearray()

    def read(self, timeout):
        return b""        # idle forever

    def write_owned(self, data, **_kwargs):
        if not self.accept_input:
            return False
        self.written.extend(data)
        return True

    def resize(self, cols, rows):
        pass

    def close(self):
        self.alive = False


@pytest.fixture
def pty_keepalive_harness(monkeypatch):
    class Spawned(list):
        pass

    spawned = Spawned()
    spawned.bridges = []
    registry = PtySessionRegistry(max_sessions=16, buffer_cap=1024, read_timeout=0.01)
    monkeypatch.setattr(_web_server_chat, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server, "PTY_REGISTRY", registry)

    @asynccontextmanager
    async def test_lifespan(app):
        app.state.event_channels = {}
        app.state.event_lock = asyncio.Lock()
        app.state.pty_active_session_files = {}
        app.state.pty_active_session_file_claims = {}
        app.state.chat_argv_lock = asyncio.Lock()
        try:
            yield
        finally:
            await registry.close_all()

    monkeypatch.setattr(web_server.app.router, "lifespan_context", test_lifespan)

    def fake_spawn(argv, cwd=None, env=None):
        b = FakeBridge()
        spawned.append(argv)
        spawned.bridges.append(b)
        return b

    monkeypatch.setattr(_web_server_chat.PtyBridge, "spawn", staticmethod(fake_spawn))
    monkeypatch.setattr(_web_server_chat, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(_web_server_chat, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(_web_server_chat, "_ws_client_reason", lambda ws: None)

    async def fake_argv(**kw):
        resume = "child" if kw.get("resume") == "parent" else kw.get("resume")
        env = {"HERMES_TUI_RESUME": resume} if resume else {}
        return (["x", resume or "fresh"], "/tmp", env)

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv_async", fake_argv)

    from starlette.testclient import TestClient
    client = TestClient(web_server.app)
    client.__enter__()
    spawned.client = client
    try:
        yield spawned
    finally:
        client.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_attach_token_reuses_same_session(pty_keepalive_harness):
    """Two connects with the same ?attach= token hit one spawned bridge."""
    from starlette.testclient import TestClient

    client = pty_keepalive_harness.client
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") as ws2:
        ws2.send_bytes(b"again")
    assert len(pty_keepalive_harness) == 1                # reattached, did not respawn
    deadline = time.monotonic() + 2
    while bytes(pty_keepalive_harness.bridges[0].written) != b"hi\x0cagain" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert bytes(pty_keepalive_harness.bridges[0].written) == b"hi\x0cagain"


@pytest.mark.asyncio
async def test_stalled_input_closes_only_the_keepalive_socket(
    pty_keepalive_harness,
):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = pty_keepalive_harness.client
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") as ws:
        bridge = pty_keepalive_harness.bridges[0]
        bridge.accept_input = False
        ws.send_bytes(b"input")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 1013
    assert next(iter(web_server.PTY_REGISTRY._sessions.values())).alive is True


@pytest.mark.asyncio
async def test_attach_token_reuses_same_resume(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = pty_keepalive_harness.client
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&resume=same") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&resume=same") as ws2:
        ws2.send_bytes(b"again")
    assert pty_keepalive_harness == [["x", "same"]]




@pytest.mark.asyncio
async def test_attach_token_reuses_canonical_resume(pty_keepalive_harness):
    from starlette.testclient import TestClient

    client = pty_keepalive_harness.client
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&resume=parent") as ws1:
        ws1.send_bytes(b"hi")
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&resume=child") as ws2:
        ws2.send_bytes(b"again")
    assert pty_keepalive_harness == [["x", "child"]]




@pytest.mark.asyncio
async def test_attach_token_reuses_default_chat_after_active_session_fallback(
    pty_keepalive_harness, tmp_path, monkeypatch
):
    from starlette.testclient import TestClient

    active_session_file = tmp_path / "active-session.json"
    monkeypatch.setattr(
        _web_server_chat,
        "_active_session_file_for_channel",
        lambda app, channel: active_session_file,
    )

    client = pty_keepalive_harness.client
    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&channel=CHAT") as ws1:
        ws1.send_bytes(b"hi")

    active_session_file.write_text(json.dumps({"session_id": "existing"}))

    with client.websocket_connect("/api/pty?attach=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&channel=CHAT") as ws2:
        ws2.send_bytes(b"again")

    assert pty_keepalive_harness == [["x", "fresh"]]


def test_retained_task_controls_require_auth_and_stop_child(pty_keepalive_harness):
    from starlette.websockets import WebSocketDisconnect

    client = pty_keepalive_harness.client
    url = f"/api/pty?attach={TOKEN}"
    with client.websocket_connect(url) as ws:
        ws.send_bytes(b"hello")

    assert client.get("/api/pty/sessions").status_code == 401
    headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
    listed = client.get("/api/pty/sessions", headers=headers)
    assert listed.status_code == 200
    tasks = listed.json()["sessions"]
    assert len(tasks) == 1
    assert tasks[0]["alive"] is True
    assert tasks[0]["attached"] is False
    assert "key" not in tasks[0]
    assert "channel" not in tasks[0]["metadata"]

    task_id = tasks[0]["id"]
    assert client.delete(f"/api/pty/sessions/{task_id}").status_code == 401
    stopped = client.delete(f"/api/pty/sessions/{task_id}", headers=headers)
    assert stopped.status_code == 200
    assert pty_keepalive_harness.bridges[0].alive is False
    assert client.get("/api/pty/sessions", headers=headers).json()["sessions"] == []
    with client.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()
    assert exc_info.value.code == 4400
