"""Disposable Matrix event tests for the opt-in Orchestra ingress."""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.personal_specialist_matrix import (
    InvocationClient, InvocationUnavailable, MatrixPersonalRouter, build_opt_in_router, event_key,
)
from gateway.personal_specialist_routing import BindingStore, Conversation, Router, Rules
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


ACCOUNT = "@orchestra:example.org"


def _event(text="@geo map the site", *, room="!room:example.org", thread="thread-a",
           sender="@ricky:example.org", event_id="$event-a", message_type=MessageType.TEXT):
    return MessageEvent(text=text, message_type=message_type,
                        source=SessionSource(Platform.MATRIX, room, user_id=sender, thread_id=thread),
                        message_id=event_id)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = False

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise InvocationUnavailable("offline")
        return {"profile": kwargs["profile"], "session_id": "actual-session",
                "result": {"text": "Done."}, "status": "succeeded"}


def _dispatcher(tmp_path, *, default=None):
    defaults = {("matrix", ACCOUNT, "!room:example.org"): default} if default else {}
    rules = Rules({"matrix": ACCOUNT}, defaults,
                  {"geo": ("geo",), "research": ("research",), "infra": ("infra",)},
                  {name: frozenset({"@ricky:example.org"}) for name in ("geo", "research", "infra")})
    client = FakeClient()
    return MatrixPersonalRouter(Router(rules, BindingStore(tmp_path / "bindings.db")), client, ACCOUNT), client


@pytest.mark.asyncio
async def test_exact_selector_invokes_and_continues_only_the_same_room_thread_sender(tmp_path):
    dispatch, client = _dispatcher(tmp_path)
    fallback_calls = []

    async def fallback(event):
        fallback_calls.append(event)
        return "Orchestra"

    first = _event()
    assert await dispatch.handle(first, fallback) == "[geo]\nDone."
    assert client.calls[0]["session_id"] is None
    second = _event("continue", event_id="$event-b")
    assert await dispatch.handle(second, fallback) == "[geo]\nDone."
    assert client.calls[1]["session_id"] == "actual-session"
    assert client.calls[1]["key"] != client.calls[0]["key"]
    for different in (
        _event("continue", thread="thread-b"),
        _event("continue", room="!another:example.org"),
        _event("continue", sender="@someone:example.org"),
    ):
        assert await dispatch.handle(different, fallback) == "Orchestra"
    assert len(fallback_calls) == 3


@pytest.mark.asyncio
async def test_priority_ambiguity_and_existing_matrix_command_fallback(tmp_path):
    dispatch, client = _dispatcher(tmp_path, default="research")

    async def fallback(_event):
        return "Orchestra"

    assert await dispatch.handle(_event("@geo research the map"), fallback) == "[geo]\nDone."
    assert client.calls[-1]["profile"] == "geo"
    assert await dispatch.handle(_event("/status", message_type=MessageType.COMMAND), fallback) == "Orchestra"
    other, other_client = _dispatcher(tmp_path / "other")
    reply = await other.handle(_event("research infra overlap"), fallback)
    assert "@infra" in reply and "@research" in reply
    assert not other_client.calls
    suggestion = await other.handle(_event("geo map", event_id="$event-c"), fallback)
    assert "@geo" in suggestion and not other_client.calls


@pytest.mark.asyncio
async def test_api_failure_preserves_binding_and_does_not_fall_back_to_orchestra(tmp_path):
    dispatch, client = _dispatcher(tmp_path)
    client.error = True
    fallback_calls = []

    async def fallback(event):
        fallback_calls.append(event)
        return "Orchestra"

    reply = await dispatch.handle(_event(), fallback)
    assert "could not be completed" in reply
    assert dispatch.router.bindings.get(Conversation(
        "matrix", ACCOUNT, "!room:example.org", "thread-a", "@ricky:example.org")) is None
    assert not fallback_calls


@pytest.mark.asyncio
async def test_specialist_sender_policy_denies_explicit_default_and_saved_routes(tmp_path):
    dispatch, client = _dispatcher(tmp_path, default="geo")
    async def fallback(_event):
        return "Orchestra"

    denied = "@other:example.org"
    assert "not allowed" in await dispatch.handle(_event(sender=denied), fallback)
    assert "not allowed" in await dispatch.handle(_event("map", sender=denied), fallback)
    ctx = Conversation("matrix", ACCOUNT, "!room:example.org", "thread-a", denied)
    dispatch.router.bindings.commit(ctx, "geo", "old-session")
    assert "not allowed" in await dispatch.handle(_event("continue", sender=denied), fallback)
    assert dispatch.router.bindings.get(ctx) == ("geo", "old-session")
    assert not client.calls

    # An omitted policy denies even an otherwise valid Orchestra sender.
    dispatch.router.rules.matrix_allowed_senders.pop("geo")
    assert "not allowed" in await dispatch.handle(_event(), fallback)
    assert not client.calls


@pytest.mark.asyncio
async def test_authenticated_matrix_account_change_fails_closed(tmp_path):
    dispatch, client = _dispatcher(tmp_path)
    authenticated = {"account": ACCOUNT}
    dispatch.account_getter = lambda: authenticated["account"]
    async def fallback(_event):
        return "Orchestra"
    assert await dispatch.handle(_event(), fallback) == "[geo]\nDone."
    authenticated["account"] = "@unexpected:example.org"
    assert await dispatch.handle(_event(event_id="$event-b"), fallback) == "Orchestra"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_client_reuses_durable_event_and_polls_without_a_second_submit(monkeypatch):
    client = InvocationClient(lambda: "test-bearer")
    calls = []
    record = {"id": "invoke-id", "profile": "geo", "status": "succeeded",
              "session_id": "actual-session", "result": {"text": "Done."}}

    def request(method, path, body=None):
        calls.append((method, path, body))
        return record

    monkeypatch.setattr(client, "_request", request)
    result = await client.run(key="matrix-personal:abc", profile="geo",
                              prompt="map", session_id="actual-session")
    assert result is record
    assert len(calls) == 1 and calls[0][0] == "GET"
    assert calls[0][2] is None


@pytest.mark.asyncio
async def test_replayed_matrix_event_submits_once_via_durable_key(monkeypatch):
    client = InvocationClient(lambda: "test-bearer")
    calls = []
    stored = {}

    def request(method, path, body=None):
        calls.append(method)
        if method == "GET":
            return stored.get("record")
        record = {"id": "invoke-id", "profile": "geo", "status": "succeeded",
                  "session_id": "actual-session", "result": {"text": "Done."}}
        stored["record"] = record
        return record

    monkeypatch.setattr(client, "_request", request)
    args = {"key": "matrix-personal:abc", "profile": "geo", "prompt": "map", "session_id": None}
    assert (await client.run(**args))["result"]["text"] == "Done."
    assert (await client.run(**args))["result"]["text"] == "Done."
    assert calls == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_client_waits_for_real_api_pending_status(monkeypatch):
    client = InvocationClient(lambda: "test-bearer")
    calls = []
    states = iter(("pending", "running", "succeeded"))

    def request(method, path, body=None):
        calls.append(method)
        if len(calls) == 1:
            return None
        status = next(states)
        return {"id": "invoke-id", "profile": "geo", "status": status,
                "session_id": "actual-session" if status == "succeeded" else None,
                "result": {"text": "Done."} if status == "succeeded" else None}

    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr("gateway.personal_specialist_matrix.asyncio.sleep", no_wait)
    result = await client.run(key="matrix-personal:pending", profile="geo",
                              prompt="map", session_id=None)
    assert result["status"] == "succeeded"
    assert calls == ["GET", "POST", "GET", "GET"]


def test_client_accepts_valid_maximum_result_with_json_envelope(monkeypatch):
    client = InvocationClient(lambda: "test-bearer")
    record = {"id": "invoke-id", "profile": "geo", "status": "succeeded",
              "session_id": "actual-session", "result": {"text": "x" * (1024 * 1024)}}
    payload = json.dumps(record).encode()
    assert len(payload) > 1024 * 1024

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(client._opener, "open", lambda *_args, **_kwargs: Response(payload))
    assert client._request("GET", "/v1/invocations/invoke-id") == record


def test_client_accepts_json_escaped_control_text_at_api_limit(monkeypatch):
    client = InvocationClient(lambda: "test-bearer")
    record = {"id": "invoke-id", "profile": "geo", "status": "succeeded",
              "session_id": "actual-session", "result": {"text": "\u0001" * (1024 * 1024)}}
    payload = json.dumps(record, ensure_ascii=False).encode()
    assert 6 * 1024 * 1024 < len(payload) < 8 * 1024 * 1024

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(client._opener, "open", lambda *_args, **_kwargs: Response(payload))
    assert client._request("GET", "/v1/invocations/invoke-id") == record


def test_client_disables_process_proxy_and_redirects(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8888")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.invalid:8888")
    monkeypatch.setenv("NO_PROXY", "")
    client = InvocationClient(lambda: "test-bearer")
    handlers = client._opener.handlers
    assert not any(isinstance(handler, __import__("urllib.request").request.ProxyHandler)
                   for handler in handlers)
    assert any(isinstance(handler, __import__("urllib.request").request.HTTPRedirectHandler)
               and handler.redirect_request(None, None, 302, "", {}, "https://example.invalid") is None
               for handler in handlers)


def test_opt_in_requires_exact_receiving_owner_and_bearer(tmp_path, monkeypatch):
    import gateway.platforms._shared as shared
    import hermes_constants

    values = {"HERMES_PERSONAL_INVOCATION_TOKEN": "test-only"}
    monkeypatch.setattr(shared, "get_scoped_secret", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "rules.yaml").write_text(
        "version: 1\nowner_accounts: {matrix: '@orchestra:example.org'}\nroom_defaults: []\nkeywords: {}\n")
    settings = {"version": 1, "enabled": True, "rules_path": str(tmp_path / "rules.yaml")}
    adapter = SimpleNamespace(_user_id=ACCOUNT, config=PlatformConfig(extra={"personal_routing": settings}))
    assert build_opt_in_router(adapter).client._base == "http://127.0.0.1:8789"
    settings["invocation_port"] = 18789
    assert build_opt_in_router(adapter).client._base == "http://127.0.0.1:18789"
    settings["invocation_port"] = "not-a-port"
    with pytest.raises(ValueError, match="loopback invocation port"):
        build_opt_in_router(adapter)
    settings["invocation_port"] = 8789
    with pytest.raises(ValueError, match="owner"):
        build_opt_in_router(SimpleNamespace(_user_id="@geo:example.org", config=adapter.config))
    values["HERMES_PERSONAL_INVOCATION_TOKEN"] = ""
    with pytest.raises(ValueError, match="credential"):
        build_opt_in_router(adapter)
    (tmp_path / "rules.yaml").write_text(
        "version: 1\nowner_accounts: {}\nroom_defaults: []\nkeywords: {}\n")
    with pytest.raises(ValueError, match="owner"):
        build_opt_in_router(SimpleNamespace(_user_id="", config=adapter.config))


@pytest.mark.asyncio
async def test_token_only_matrix_identity_routes_only_after_matching_whoami(tmp_path, monkeypatch):
    import gateway.platforms._shared as shared

    monkeypatch.setattr(shared, "get_scoped_secret", lambda key, default="":
                        "test-only" if key == "HERMES_PERSONAL_INVOCATION_TOKEN" else default)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "version: 1\nowner_accounts: {matrix: '@orchestra:example.org'}\n"
        "room_defaults: []\nkeywords: {}\n"
        "matrix_allowed_senders: {geo: ['@ricky:example.org']}\n")
    adapter = SimpleNamespace(_user_id="", config=PlatformConfig(extra={
        "personal_routing": {"version": 1, "enabled": True, "rules_path": str(rules_path)}}))
    dispatch = build_opt_in_router(adapter)
    assert dispatch.account == ACCOUNT
    client = FakeClient()
    dispatch.client = client

    async def fallback(_event):
        return "Orchestra"

    assert await dispatch.handle(_event(), fallback) == "Orchestra"
    assert not client.calls
    adapter._user_id = ACCOUNT  # Matrix connect() filled this from authenticated whoami.
    assert await dispatch.handle(_event(), fallback) == "[geo]\nDone."
    assert len(client.calls) == 1
    adapter._user_id = "@unexpected:example.org"
    assert await dispatch.handle(_event(event_id="$event-b"), fallback) == "Orchestra"
    assert len(client.calls) == 1


def test_personal_routing_requires_versioned_boolean_yaml_and_ignores_old_env(tmp_path, monkeypatch):
    for name, value in (("HERMES_PERSONAL_ROUTING_ENABLED", "true"),
                        ("HERMES_PERSONAL_ROUTING_RULES", str(tmp_path / "rules.yaml")),
                        ("HERMES_PERSONAL_INVOCATION_PORT", "18789")):
        monkeypatch.setenv(name, value)
    adapter = SimpleNamespace(_user_id=ACCOUNT, config=PlatformConfig())
    assert build_opt_in_router(adapter) is None
    adapter.config.extra["personal_routing"] = {"version": 1, "enabled": False}
    assert build_opt_in_router(adapter) is None
    for settings, error in (({"version": 2, "enabled": True}, "version 1"),
                            ({"version": 1, "enabled": "true"}, "boolean"),
                            ({"version": 1, "enabled": True, "rules_path": "rules.yaml"}, "absolute rules")):
        adapter.config.extra["personal_routing"] = settings
        with pytest.raises(ValueError, match=error):
            build_opt_in_router(adapter)


def test_real_gateway_yaml_loader_keeps_personal_routing_profile_scoped(tmp_path, monkeypatch):
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        (home / "rules.yaml").write_text(
            "version: 1\nowner_accounts: {matrix: '@orchestra:example.org'}\n"
            "room_defaults: []\nkeywords: {}\n")
    (homes[0] / "config.yaml").write_text(
        "matrix:\n  personal_routing:\n    version: 1\n    enabled: true\n"
        f"    rules_path: {homes[0] / 'rules.yaml'}\n    invocation_port: 18789\n")
    (homes[1] / "config.yaml").write_text(
        "matrix:\n  personal_routing:\n    version: 1\n    enabled: false\n")
    monkeypatch.setenv("HERMES_PERSONAL_INVOCATION_TOKEN", "test-only")
    for home, expected_port in ((homes[0], 18789), (homes[1], None), (homes[0], 18789)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        matrix_config = load_gateway_config().platforms[Platform.MATRIX]
        adapter = SimpleNamespace(_user_id=ACCOUNT, config=matrix_config)
        router = build_opt_in_router(adapter)
        assert (int(router.client._base.rsplit(":", 1)[1]) if router else None) == expected_port


def test_event_key_is_scoped_and_requires_real_matrix_event_id():
    ctx = Conversation("matrix", ACCOUNT, "!room:example.org", "thread-a", "@ricky:example.org")
    assert event_key(ctx, "$event-a") == event_key(ctx, "$event-a")
    assert event_key(ctx, "$event-a") != event_key(replace(ctx, thread="thread-b"), "$event-a")
    with pytest.raises(ValueError):
        event_key(ctx, "")
