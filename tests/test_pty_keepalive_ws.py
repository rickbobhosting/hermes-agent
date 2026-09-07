import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server
from hermes_cli import web_server_chat
from hermes_cli.pty_session import (
    AttachmentClosed,
    PtySession,
    PtySessionRegistry,
    RegistryClosed,
)
from hermes_cli.web_routers import chat_ws


ATTACH_TOKEN = "0123456789abcdef0123456789abcdef"
SECOND_ATTACH_TOKEN = "fedcba9876543210fedcba9876543210"
_ORIGINAL_PTY_SESSION_START = PtySession.start


class FakeBridge:
    def __init__(self):
        self.written = bytearray()
        self.resizes = []
        self.closed = False

    def read(self, timeout):
        time.sleep(timeout)
        return b""

    async def write(
        self,
        data,
        *,
        cancelled=None,
        timeout=1.0,
    ):
        if timeout <= 0 or (cancelled is not None and cancelled()):
            return False
        if cancelled is not None and cancelled():
            return False
        self.written.extend(data)
        return True

    def resize(self, *, cols, rows):
        self.resizes.append((cols, rows))

    def close(self):
        self.closed = True


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.fixture
def keepalive_harness(monkeypatch):
    state = {"spawned": [], "spawn_envs": [], "argv_calls": []}
    registry = PtySessionRegistry(
        max_sessions=16,
        buffer_cap=1024,
        read_timeout=0.01,
    )
    monkeypatch.setattr(web_server, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server_chat, "PTY_REGISTRY", registry)

    @asynccontextmanager
    async def test_lifespan(app):
        # Repeated Starlette TestClient lifespans can deliver a delayed shutdown
        # while the next WebSocket test is already running against this module-
        # global app. Isolate transport wiring here; lifecycle shutdown has its
        # own deterministic registry tests.
        app.state.event_channels = {}
        app.state.event_publishers = {}
        app.state.event_active_sessions = {}
        app.state.event_lock = asyncio.Lock()
        app.state.pty_active_session_files = {}
        app.state.pty_active_session_file_claims = {}
        app.state.pty_session_watchers = {}
        app.state.chat_argv_lock = asyncio.Lock()
        try:
            yield
        finally:
            watchers = list(app.state.pty_session_watchers.values())
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            for path in app.state.pty_active_session_files.values():
                path.unlink(missing_ok=True)

    monkeypatch.setattr(web_server.app.router, "lifespan_context", test_lifespan)

    # TestClient tears down each WebSocket in an AnyIO cancel scope. Keep the
    # server-integration tests deterministic by leaving PTY drain ownership to
    # the dedicated lifecycle suite; these tests exercise registry/WS wiring.
    async def fake_start(_session):
        return None

    monkeypatch.setattr(PtySession, "start", fake_start)

    def fake_spawn(_argv, cwd=None, env=None):
        bridge = FakeBridge()
        state["spawned"].append(bridge)
        state["spawn_envs"].append(env)
        return bridge

    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", staticmethod(fake_spawn))
    monkeypatch.setattr(web_server_chat, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server_chat, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server_chat, "_ws_client_reason", lambda ws: None)

    async def fake_argv(**kwargs):
        state["argv_calls"].append(dict(kwargs))
        env = {}
        if kwargs.get("resume"):
            env["HERMES_TUI_RESUME"] = kwargs["resume"]
        return (["fake-hermes-tui"], "/tmp", env)

    monkeypatch.setattr(web_server_chat, "_resolve_chat_argv_async", fake_argv)
    yield state
    registry._sessions.clear()


def test_browser_disconnect_detaches_without_stopping_agent_and_reattaches(
    keepalive_harness,
):
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as first:
            first.send_bytes(b"before-close")
            _wait_until(
                lambda: bytes(keepalive_harness["spawned"][0].written)
                == b"before-close"
            )

        assert len(keepalive_harness["spawned"]) == 1
        assert keepalive_harness["spawned"][0].closed is False

        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as second:
            second.send_bytes(b"after-reopen")
            _wait_until(
                lambda: bytes(keepalive_harness["spawned"][0].written)
                == b"before-close\x0cafter-reopen"
            )

    assert len(keepalive_harness["spawned"]) == 1


def test_keepalive_owns_gateway_process_but_preserves_other_spawn_env(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    resolved_env = {
        "HERMES_TUI_GATEWAY_URL": "ws://dashboard.invalid/api/ws",
        "HERMES_TUI_SIDECAR_URL": "ws://dashboard.invalid/api/pub",
        "HERMES_TUI_ACTIVE_SESSION_FILE": "/tmp/hermes-active-test.json",
        "HERMES_ENV_SENTINEL": "preserved",
    }

    async def fake_argv(**_kwargs):
        return (["fake-hermes-tui"], "/tmp", resolved_env)

    monkeypatch.setattr(web_server_chat, "_resolve_chat_argv_async", fake_argv)

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}"):
            pass

    spawn_env = keepalive_harness["spawn_envs"][0]
    assert spawn_env is not resolved_env
    assert "HERMES_TUI_GATEWAY_URL" not in spawn_env
    assert spawn_env["HERMES_TUI_SIDECAR_URL"] == resolved_env[
        "HERMES_TUI_SIDECAR_URL"
    ]
    assert spawn_env["HERMES_TUI_ACTIVE_SESSION_FILE"] == resolved_env[
        "HERMES_TUI_ACTIVE_SESSION_FILE"
    ]
    assert spawn_env["HERMES_ENV_SENTINEL"] == "preserved"
    assert resolved_env["HERMES_TUI_GATEWAY_URL"] == (
        "ws://dashboard.invalid/api/ws"
    )


def test_keepalive_with_none_env_does_not_inherit_gateway_url(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    monkeypatch.setenv(
        "HERMES_TUI_GATEWAY_URL",
        "ws://inherited-dashboard.invalid/api/ws",
    )
    monkeypatch.setenv("HERMES_ENV_SENTINEL", "inherited")

    async def fake_argv(**_kwargs):
        return (["fake-hermes-tui"], "/tmp", None)

    monkeypatch.setattr(web_server_chat, "_resolve_chat_argv_async", fake_argv)

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}"):
            pass

    spawn_env = keepalive_harness["spawn_envs"][0]
    assert isinstance(spawn_env, dict)
    assert "HERMES_TUI_GATEWAY_URL" not in spawn_env
    assert spawn_env["HERMES_ENV_SENTINEL"] == "inherited"


def test_legacy_pty_preserves_resolved_gateway_env(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    resolved_env = {
        "HERMES_TUI_GATEWAY_URL": "ws://dashboard.invalid/api/ws",
        "HERMES_ENV_SENTINEL": "preserved",
    }

    async def fake_argv(**_kwargs):
        return (["fake-hermes-tui"], "/tmp", resolved_env)

    monkeypatch.setattr(web_server_chat, "_resolve_chat_argv_async", fake_argv)

    with TestClient(web_server.app) as client:
        with client.websocket_connect("/api/pty"):
            pass

    assert keepalive_harness["spawn_envs"] == [resolved_env]


def test_keepalive_enforces_stable_sidecar_and_breadcrumb_across_reconnect(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    sidecar_channels = []

    def fake_sidecar(channel):
        sidecar_channels.append(channel)
        return f"ws://sidecar.invalid/{channel}"

    monkeypatch.setattr(chat_ws, "_build_sidecar_url", fake_sidecar)
    uppercase_token = ATTACH_TOKEN.upper()

    with TestClient(web_server.app) as client:
        with client.websocket_connect(
            f"/api/pty?attach={uppercase_token}&channel=browser-one"
        ):
            first_path = Path(
                keepalive_harness["argv_calls"][0]["active_session_file"]
            )
            first_path.write_text(
                json.dumps({"session_id": "20260906_101010_a1b2c3"}),
                encoding="utf-8",
            )

        with client.websocket_connect(
            f"/api/pty?attach={ATTACH_TOKEN}&channel=browser-two"
        ):
            pass

        second_path = Path(
            keepalive_harness["argv_calls"][1]["active_session_file"]
        )
        assert first_path == second_path

    canonical_channel = f"pty-{ATTACH_TOKEN}"
    assert sidecar_channels == [canonical_channel, canonical_channel]
    assert [call["sidecar_url"] for call in keepalive_harness["argv_calls"]] == [
        f"ws://sidecar.invalid/{canonical_channel}",
        f"ws://sidecar.invalid/{canonical_channel}",
    ]
    assert keepalive_harness["argv_calls"][1]["resume"] == (
        "20260906_101010_a1b2c3"
    )
    assert len(keepalive_harness["spawned"]) == 1


def test_registry_identity_uses_token_and_canonical_profile_not_mutable_lineage(
    keepalive_harness,
):
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        with client.websocket_connect(
            f"/api/pty?attach={ATTACH_TOKEN}&profile=Default&resume=session-one"
        ):
            pass
        with client.websocket_connect(
            f"/api/pty?attach={ATTACH_TOKEN}&profile=default&resume=session-one"
        ):
            pass
        assert len(keepalive_harness["spawned"]) == 1

        with client.websocket_connect(
            f"/api/pty?attach={ATTACH_TOKEN}&profile=default&resume=session-two"
        ):
            pass
        assert len(keepalive_harness["spawned"]) == 1

        with client.websocket_connect(
            f"/api/pty?attach={SECOND_ATTACH_TOKEN}"
            "&profile=default&resume=session-two"
        ):
            pass

    assert len(keepalive_harness["spawned"]) == 2


@pytest.mark.parametrize("token", ["", "too-short", "g" * 32, "0" * 31, "0" * 33])
def test_invalid_attach_is_rejected_before_resolve_or_spawn(
    keepalive_harness,
    token,
):
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(f"/api/pty?attach={token}"):
                pass

    assert caught.value.code == chat_ws._PTY_INVALID_ATTACH_CLOSE_CODE
    assert caught.value.reason == "invalid_attach_token"
    assert keepalive_harness["argv_calls"] == []
    assert keepalive_harness["spawned"] == []


def test_resume_delimiter_injection_is_rejected_without_spawn(keepalive_harness):
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        with client.websocket_connect(
            f"/api/pty?attach={ATTACH_TOKEN}&resume=unsafe%00target"
        ) as ws:
            assert "Invalid session resume target" in ws.receive_text()
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()

    assert caught.value.code == 1011
    assert keepalive_harness["argv_calls"] == []
    assert keepalive_harness["spawned"] == []


def test_superseded_socket_cannot_write_to_reattached_child(keepalive_harness):
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as first:
            first.send_bytes(b"before-supersede")
            bridge = keepalive_harness["spawned"][0]
            _wait_until(lambda: b"before-supersede" in bridge.written)

            with client.websocket_connect(
                f"/api/pty?attach={ATTACH_TOKEN}"
            ) as second:
                _wait_until(lambda: b"\x0c" in bridge.written)
                first.send_bytes(b"stale-input")
                second.send_bytes(b"current-input")
                _wait_until(lambda: b"current-input" in bridge.written)

    assert b"stale-input" not in bridge.written
    assert bytes(bridge.written) == b"before-supersede\x0ccurrent-input"
    assert len(keepalive_harness["spawned"]) == 1


@pytest.mark.parametrize(
    ("close_code", "close_reason"),
    [(4409, "pty_superseded"), (1013, "pty_attachment_interrupted"), (4410, "pty_process_ended")],
)
def test_attach_failure_preserves_lifecycle_reason_without_detaching_unknown_lease(
    keepalive_harness,
    monkeypatch,
    close_code,
    close_reason,
):
    from starlette.testclient import TestClient

    class FailedSession:
        id = "public-failed-session"
        cleaned = False
        metadata = {}

        async def attach(self, _ws, *, force_redraw=False):
            self.cleaned = True
            raise AttachmentClosed("replay failed", code=close_code)

    failed = FailedSession()
    detach_calls = []

    async def attach_or_spawn(_key, *, spawn, metadata):
        return failed, False

    monkeypatch.setattr(web_server_chat.PTY_REGISTRY, "attach_or_spawn", attach_or_spawn)
    monkeypatch.setattr(
        web_server_chat.PTY_REGISTRY,
        "detach",
        lambda key, attachment: detach_calls.append((key, attachment)),
    )

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as ws:
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()

    assert caught.value.code == close_code
    assert caught.value.reason == close_reason
    assert failed.cleaned is True
    assert detach_calls == []
    assert keepalive_harness["spawned"] == []


def test_closed_registry_reports_server_shutdown_without_spawning(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    async def closed_registry(_key, *, spawn, metadata):
        raise RegistryClosed("registry shutting down")

    monkeypatch.setattr(web_server_chat.PTY_REGISTRY, "attach_or_spawn", closed_registry)
    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as ws:
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_bytes()
    assert caught.value.code == 1001
    assert caught.value.reason == "pty_server_shutting_down"
    assert keepalive_harness["spawned"] == []


def test_retained_session_list_and_stop_are_authenticated_and_do_not_leak_keys(
    keepalive_harness,
):
    from starlette.testclient import TestClient

    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    channel = f"pty-{ATTACH_TOKEN}"

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}"):
            pass

        assert client.get("/api/pty/sessions").status_code == 401
        response = client.get("/api/pty/sessions", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert len(body["sessions"]) == 1
        snapshot = body["sessions"][0]
        public_id = snapshot["id"]
        assert snapshot["alive"] is True
        assert snapshot["attached"] is False
        assert snapshot["metadata"]["profile"]
        assert "channel" not in snapshot["metadata"]
        serialized = response.text
        assert ATTACH_TOKEN not in serialized
        assert "key" not in snapshot
        assert "attach" not in snapshot

        active_file = client.app.state.pty_active_session_files[channel]
        assert active_file.exists()
        client.app.state.event_active_sessions[channel] = (
            chat_ws._active_session_replay_frame("stored-session")
        )

        stopped = client.delete(
            f"/api/pty/sessions/{public_id}",
            headers=headers,
        )
        assert stopped.status_code == 200
        assert stopped.json() == {"ok": True, "id": public_id}
        assert keepalive_harness["spawned"][0].closed is True
        assert not active_file.exists()
        assert channel not in client.app.state.pty_active_session_files
        assert channel not in client.app.state.event_active_sessions
        assert client.get("/api/pty/sessions", headers=headers).json() == {
            "sessions": []
        }


@pytest.mark.parametrize("force_collision", [False, True])
def test_explicit_stop_rejects_delayed_reconnect_and_fresh_token_recovers(
    keepalive_harness,
    monkeypatch,
    force_collision,
):
    from starlette.testclient import TestClient

    reconnect_token = "1" * 32 if force_collision else ATTACH_TOKEN
    if force_collision:
        # Deterministically model a Bloom false positive: the never-used
        # reconnect identity shares all probes with the stopped identity.
        monkeypatch.setattr(
            web_server_chat.PTY_REGISTRY._stopped,
            "_indices",
            lambda key: [1] if key.startswith(SECOND_ATTACH_TOKEN) else [0],
        )
    headers = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}"):
            pass
        public_id = client.get("/api/pty/sessions", headers=headers).json()["sessions"][0]["id"]
        assert client.delete(f"/api/pty/sessions/{public_id}", headers=headers).status_code == 200
        with client.websocket_connect(f"/api/pty?attach={reconnect_token}") as delayed:
            with pytest.raises(WebSocketDisconnect) as caught:
                delayed.receive_bytes()
        assert caught.value.code == chat_ws._PTY_INVALID_ATTACH_CLOSE_CODE
        assert caught.value.reason == "pty_session_stopped"
        assert f"pty-{reconnect_token}" not in client.app.state.pty_active_session_files
        assert len(keepalive_harness["spawned"]) == 1
        assert keepalive_harness["spawned"][0].closed
        with client.websocket_connect(f"/api/pty?attach={SECOND_ATTACH_TOKEN}") as fresh:
            fresh.send_bytes(b"fresh-task")
            _wait_until(lambda: len(keepalive_harness["spawned"]) == 2)
            _wait_until(lambda: b"fresh-task" in keepalive_harness["spawned"][1].written)
        assert len(client.get("/api/pty/sessions", headers=headers).json()["sessions"]) == 1


def test_capacity_rejects_new_keepalive_without_evicting_detached_session(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    registry = PtySessionRegistry(
        max_sessions=1,
        buffer_cap=1024,
        read_timeout=0.01,
    )
    monkeypatch.setattr(web_server, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server_chat, "PTY_REGISTRY", registry)

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}"):
            pass

        first_bridge = keepalive_harness["spawned"][0]
        assert first_bridge.closed is False

        rejected_tokens = [SECOND_ATTACH_TOKEN, *(f"{index:032x}" for index in range(1, 9))]
        rejected_paths = []
        for token in rejected_tokens:
            with client.websocket_connect(f"/api/pty?attach={token}") as rejected:
                assert "stop a session first" in rejected.receive_text()
                with pytest.raises(WebSocketDisconnect) as caught:
                    rejected.receive_text()
            assert caught.value.code == chat_ws._PTY_CAPACITY_CLOSE_CODE
            assert caught.value.reason == "pty_capacity_reached"
            rejected_paths.append(
                Path(keepalive_harness["argv_calls"][-1]["active_session_file"])
            )

        assert first_bridge.closed is False
        assert len(keepalive_harness["spawned"]) == 1
        assert set(client.app.state.pty_active_session_files) == {
            f"pty-{ATTACH_TOKEN}"
        }
        assert all(not path.exists() for path in rejected_paths)


def test_spawn_failure_rolls_back_provisional_breadcrumb(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    def failed_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(web_server_chat.PtyBridge, "spawn", staticmethod(failed_spawn))
    channel = f"pty-{ATTACH_TOKEN}"

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as ws:
            assert "spawn failed" in ws.receive_text()
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()

        assert caught.value.code == 1011
        path = Path(keepalive_harness["argv_calls"][0]["active_session_file"])
        assert channel not in client.app.state.pty_active_session_files
        assert not path.exists()


def test_natural_process_exit_reaps_retained_breadcrumb(
    keepalive_harness,
    monkeypatch,
):
    from starlette.testclient import TestClient

    class ExitedBridge(FakeBridge):
        def read(self, _timeout):
            return None

    monkeypatch.setattr(PtySession, "start", _ORIGINAL_PTY_SESSION_START)
    monkeypatch.setattr(
        web_server_chat.PtyBridge,
        "spawn",
        staticmethod(lambda *_args, **_kwargs: ExitedBridge()),
    )
    channel = f"pty-{ATTACH_TOKEN}"

    with TestClient(web_server.app) as client:
        with client.websocket_connect(f"/api/pty?attach={ATTACH_TOKEN}") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

        path = Path(keepalive_harness["argv_calls"][0]["active_session_file"])
        _wait_until(lambda: channel not in client.app.state.pty_active_session_files)
        assert not path.exists()


@pytest.mark.asyncio
async def test_final_admitted_claim_cleans_if_immediate_exit_was_already_reaped(
    keepalive_harness,
):
    channel = f"pty-{ATTACH_TOKEN}"
    path = web_server_chat._claim_active_session_file_for_channel(
        web_server.app,
        channel,
    )

    class AlreadyReapedRegistry:
        async def snapshots(self):
            return []

    # Models the watcher seeing EOF and reaping the session while this claim
    # still prevented its own first cleanup attempt.
    await web_server_chat._release_active_session_file_claim(
        web_server.app,
        AlreadyReapedRegistry(),
        channel,
        path,
    )

    assert channel not in web_server.app.state.pty_active_session_files
    assert not path.exists()


def _active_session_event(session_key, **extra_payload):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "dashboard.active_session_changed",
                "session_id": "runtime-only",
                "payload": {"session_key": session_key, **extra_payload},
            },
        }
    )


@pytest.mark.asyncio
async def test_active_session_replay_is_projected_authenticated_and_replaced(
    keepalive_harness,
):
    channel = f"pty-{ATTACH_TOKEN}"
    active_file = web_server_chat._active_session_file_for_channel(
        web_server.app, channel
    )
    active_file.write_text(json.dumps({"session_id": "stored-a"}), encoding="utf-8")

    first = _active_session_event("stored-a", secret="must-not-be-cached")
    await chat_ws._broadcast_event(web_server.app, channel, first)

    cached = web_server.app.state.event_active_sessions[channel]
    assert "stored-a" in cached
    assert "must-not-be-cached" not in cached
    assert "runtime-only" not in cached

    class Subscriber:
        def __init__(self):
            self.sent = []

        async def send_text(self, payload):
            self.sent.append(payload)

    subscriber = Subscriber()
    assert await chat_ws._register_event_subscriber(
        web_server.app, channel, subscriber
    )
    replay = json.loads(subscriber.sent[0])
    assert replay["params"] == {
        "type": "dashboard.active_session_changed",
        "payload": {"session_key": "stored-a"},
    }

    # The breadcrumb is authoritative: a delayed event for A is dropped after
    # focus has already moved to B, then B replaces the replay value.
    active_file.write_text(json.dumps({"session_id": "stored-b"}), encoding="utf-8")
    await chat_ws._broadcast_event(web_server.app, channel, first)
    assert len(subscriber.sent) == 1

    second = _active_session_event("stored-b", private_history="not retained")
    await chat_ws._broadcast_event(web_server.app, channel, second)
    assert json.loads(subscriber.sent[-1])["params"] == {
        "type": "dashboard.active_session_changed",
        "payload": {"session_key": "stored-b"},
    }
    cached = web_server.app.state.event_active_sessions[channel]
    assert "stored-b" in cached
    assert "private_history" not in cached

    tool_frame = json.dumps(
        {"method": "event", "params": {"type": "tool.start", "payload": {"secret": "live-only"}}}
    )
    await chat_ws._broadcast_event(web_server.app, channel, tool_frame)
    assert subscriber.sent[-1] == tool_frame
    assert web_server.app.state.event_active_sessions[channel] == cached
    await chat_ws._unregister_event_subscriber(
        web_server.app, channel, subscriber
    )


@pytest.mark.asyncio
async def test_active_session_event_on_non_retained_channel_is_dropped(
    keepalive_harness,
):
    channel = "browser-generated-channel"

    class Subscriber:
        def __init__(self):
            self.sent = []

        async def send_text(self, payload):
            self.sent.append(payload)

    subscriber = Subscriber()
    assert await chat_ws._register_event_subscriber(
        web_server.app,
        channel,
        subscriber,
    )
    await chat_ws._broadcast_event(
        web_server.app,
        channel,
        _active_session_event("forged-session"),
    )
    assert subscriber.sent == []
    assert channel not in web_server.app.state.event_active_sessions


@pytest.mark.asyncio
async def test_cached_focus_replay_cannot_be_overtaken_by_concurrent_live_change(
    keepalive_harness,
):
    channel = f"pty-{ATTACH_TOKEN}"
    active_file = web_server_chat._active_session_file_for_channel(
        web_server.app, channel
    )
    active_file.write_text(json.dumps({"session_id": "stored-a"}), encoding="utf-8")
    await chat_ws._broadcast_event(
        web_server.app, channel, _active_session_event("stored-a")
    )

    replay_started = asyncio.Event()
    release_replay = asyncio.Event()

    class BlockingSubscriber:
        def __init__(self):
            self.sent = []

        async def send_text(self, payload):
            self.sent.append(payload)
            if len(self.sent) == 1:
                replay_started.set()
                await release_replay.wait()

    subscriber = BlockingSubscriber()
    registration = asyncio.create_task(
        chat_ws._register_event_subscriber(web_server.app, channel, subscriber)
    )
    await replay_started.wait()

    active_file.write_text(json.dumps({"session_id": "stored-b"}), encoding="utf-8")
    live = _active_session_event("stored-b")
    broadcast = asyncio.create_task(
        chat_ws._broadcast_event(web_server.app, channel, live)
    )
    await asyncio.sleep(0)
    assert not broadcast.done()

    release_replay.set()
    assert await registration is True
    await broadcast
    assert json.loads(subscriber.sent[0])["params"]["payload"] == {
        "session_key": "stored-a"
    }
    assert json.loads(subscriber.sent[1])["params"]["payload"] == {
        "session_key": "stored-b"
    }
    await chat_ws._unregister_event_subscriber(
        web_server.app, channel, subscriber
    )


@pytest.mark.asyncio
async def test_focus_replay_timeout_cleans_subscriber_without_blocking_lock(
    keepalive_harness,
    monkeypatch,
):
    channel = f"pty-{ATTACH_TOKEN}"
    active_file = web_server_chat._active_session_file_for_channel(
        web_server.app, channel
    )
    active_file.write_text(json.dumps({"session_id": "stored-a"}), encoding="utf-8")
    monkeypatch.setattr(
        chat_ws,
        "_PTY_EVENT_REPLAY_SEND_TIMEOUT_SECONDS",
        0.01,
    )

    class WedgedSubscriber:
        async def send_text(self, _payload):
            await asyncio.Event().wait()

    subscriber = WedgedSubscriber()
    assert not await chat_ws._register_event_subscriber(
        web_server.app,
        channel,
        subscriber,
    )
    assert channel not in web_server.app.state.event_channels

    # The global event lock was released after the timeout; another channel
    # can register and broadcast immediately.
    other_channel = f"pty-{SECOND_ATTACH_TOKEN}"
    other_file = web_server_chat._active_session_file_for_channel(
        web_server.app,
        other_channel,
    )
    other_file.write_text(json.dumps({"session_id": "stored-b"}), encoding="utf-8")
    await asyncio.wait_for(
        chat_ws._broadcast_event(
            web_server.app,
            other_channel,
            _active_session_event("stored-b"),
        ),
        timeout=0.1,
    )


@pytest.mark.asyncio
async def test_active_session_cache_is_bounded_and_clears_with_last_publisher(
    keepalive_harness,
):
    app = web_server.app
    for index in range(chat_ws._PTY_ACTIVE_SESSION_CACHE_MAX_CHANNELS + 1):
        token = f"{index:032x}"
        channel = f"pty-{token}"
        session_key = f"stored-{index}"
        active_file = web_server_chat._active_session_file_for_channel(
            app, channel
        )
        active_file.write_text(
            json.dumps({"session_id": session_key}), encoding="utf-8"
        )
        await chat_ws._broadcast_event(
            app, channel, _active_session_event(session_key)
        )

    assert len(app.state.event_active_sessions) == (
        chat_ws._PTY_ACTIVE_SESSION_CACHE_MAX_CHANNELS
    )
    assert f"pty-{0:032x}" not in app.state.event_active_sessions

    channel = f"pty-{chat_ws._PTY_ACTIVE_SESSION_CACHE_MAX_CHANNELS:032x}"
    first_publisher = object()
    second_publisher = object()
    await chat_ws._register_event_publisher(app, channel, first_publisher)
    await chat_ws._register_event_publisher(app, channel, second_publisher)
    await chat_ws._unregister_event_publisher(app, channel, first_publisher)
    assert channel in app.state.event_active_sessions
    await chat_ws._unregister_event_publisher(app, channel, second_publisher)
    assert channel not in app.state.event_active_sessions


@pytest.mark.asyncio
async def test_lifespan_shutdown_removes_all_breadcrumb_and_replay_state(
    monkeypatch,
):
    from fastapi import FastAPI

    registry = PtySessionRegistry(
        max_sessions=1,
        buffer_cap=1024,
        read_timeout=0.01,
    )
    local_app = FastAPI()
    monkeypatch.setattr(web_server, "PTY_REGISTRY", registry)
    monkeypatch.setattr(web_server, "_warm_gateway_module", lambda: None)

    async with web_server._lifespan(local_app):
        channel = f"pty-{ATTACH_TOKEN}"
        path = web_server_chat._active_session_file_for_channel(
            local_app, channel
        )
        local_app.state.event_channels[channel] = {object()}
        local_app.state.event_publishers[channel] = {object()}
        local_app.state.event_active_sessions[channel] = (
            chat_ws._active_session_replay_frame("stored-a")
        )
        assert path.exists()

    assert not path.exists()
    assert local_app.state.pty_active_session_files == {}
    assert local_app.state.pty_active_session_file_claims == {}
    assert local_app.state.pty_session_watchers == {}
    assert local_app.state.event_channels == {}
    assert local_app.state.event_publishers == {}
    assert local_app.state.event_active_sessions == {}
