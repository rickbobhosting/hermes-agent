import time

import pytest

from hermes_cli import web_server


class FakeBridge:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def read(self, _timeout):
        time.sleep(_timeout)
        return b""

    def write(self, data):
        self.written.extend(data)

    def resize(self, *, cols, rows):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def keepalive_harness(monkeypatch):
    spawned = []

    def fake_spawn(_argv, cwd=None, env=None):
        bridge = FakeBridge()
        spawned.append(bridge)
        return bridge

    monkeypatch.setattr(web_server.PtyBridge, "spawn", staticmethod(fake_spawn))
    monkeypatch.setattr(web_server, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(web_server, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(web_server, "_ws_client_reason", lambda ws: None)

    async def fake_argv(**_kwargs):
        return (["fake-hermes-tui"], "/tmp", {})

    monkeypatch.setattr(web_server, "_resolve_chat_argv_async", fake_argv)
    yield spawned
    web_server.PTY_REGISTRY._sessions.clear()


def test_browser_disconnect_detaches_without_stopping_agent_and_reattaches(
    keepalive_harness,
):
    from starlette.testclient import TestClient

    with TestClient(web_server.app) as client:
        with client.websocket_connect("/api/pty?attach=stable-browser-token") as first:
            first.send_bytes(b"before-close")

        assert len(keepalive_harness) == 1
        assert keepalive_harness[0].closed is False

        with client.websocket_connect("/api/pty?attach=stable-browser-token") as second:
            second.send_bytes(b"after-reopen")

        assert len(keepalive_harness) == 1
        assert bytes(keepalive_harness[0].written) == (
            b"before-close\x0cafter-reopen"
        )
