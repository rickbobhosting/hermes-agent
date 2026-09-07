"""Dashboard transport credentials must stop at the TUI/gateway boundary."""

from __future__ import annotations

import os

import pytest

from tools.environments.local import _sanitize_subprocess_env, hermes_subprocess_env
from tui_gateway import entry, event_publisher
from tui_gateway.transport import TeeTransport


_INTERNAL_KEYS = {
    "HERMES_TUI_ACTIVE_SESSION_FILE",
    "HERMES_TUI_GATEWAY_URL",
    "HERMES_TUI_SIDECAR_URL",
}


@pytest.mark.parametrize(
    "sidecar_url",
    [
        "ws://127.0.0.1:3000/api/pub?token=insecure-secret&channel=pty-a",
        "ws://127.0.0.1:3000/api/pub?internal=gated-secret&channel=pty-a",
    ],
)
def test_gateway_captures_sidecar_url_then_consumes_all_internal_env(
    monkeypatch,
    sidecar_url,
):
    captured = []

    class FakePublisher:
        def __init__(self, url):
            captured.append(url)

        def close(self):
            return None

        def write(self, _obj):
            return True

    class FakePrimary:
        def close(self):
            return None

        def write(self, _obj):
            return True

    monkeypatch.setattr(event_publisher, "WsPublisherTransport", FakePublisher)
    monkeypatch.setattr(entry.server, "_stdio_transport", FakePrimary())
    monkeypatch.setenv("HERMES_TUI_SIDECAR_URL", sidecar_url)
    monkeypatch.setenv(
        "HERMES_TUI_GATEWAY_URL",
        "ws://127.0.0.1:3000/api/ws?internal=gateway-secret",
    )
    monkeypatch.setenv(
        "HERMES_TUI_ACTIVE_SESSION_FILE",
        "/tmp/hermes-private-breadcrumb",
    )

    entry._install_sidecar_publisher()

    assert captured == [sidecar_url]
    assert isinstance(entry.server._stdio_transport, TeeTransport)
    assert _INTERNAL_KEYS.isdisjoint(os.environ)


@pytest.mark.parametrize("inherit_credentials", [False, True])
def test_agent_subprocess_sanitizers_cannot_reenable_internal_transport_env(
    monkeypatch,
    inherit_credentials,
):
    source = {
        "HERMES_TUI_ACTIVE_SESSION_FILE": "/tmp/hermes-private-breadcrumb",
        "HERMES_TUI_GATEWAY_URL": "ws://localhost/api/ws?token=gateway-secret",
        "HERMES_TUI_SIDECAR_URL": "ws://localhost/api/pub?internal=sidecar-secret",
        "PATH": "/usr/bin:/bin",
    }
    for key, value in source.items():
        monkeypatch.setenv(key, value)

    assert _INTERNAL_KEYS.isdisjoint(_sanitize_subprocess_env(source))
    child_env = hermes_subprocess_env(inherit_credentials=inherit_credentials)
    assert _INTERNAL_KEYS.isdisjoint(child_env)
    assert child_env["PATH"] == source["PATH"]
