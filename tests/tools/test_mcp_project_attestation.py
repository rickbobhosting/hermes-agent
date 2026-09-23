"""The model cannot claim a Matrix write turn or see its host proof."""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools.mcp_project_attestation import AttestationUnavailable, proof


CONFIG = {
    "headers": {"Authorization": "Bearer scoped-test-token"},
    "session_attestation": {
        "meta_key": "org.project.chat_turn",
        "tools": ["save_miniproject_output"],
        "finish_tool": "finish_miniproject_chat_turn",
    },
}


def test_project_proof_requires_trusted_matrix_turn_and_signs_exact_arguments(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "matrix")
    with pytest.raises(AttestationUnavailable, match="trusted user Matrix"):
        proof(CONFIG, "save_miniproject_output", {"content": "draft"})

    tokens = set_session_vars(
        platform="matrix", chat_id="!room:test", user_id="@user:test",
        message_id="$event", cron_session="", project_chat_authorized=True,
    )
    try:
        args = {"content": "draft", "name": "report.txt"}
        meta = proof(CONFIG, "save_miniproject_output", args)["org.project.chat_turn"]
        payload = meta["payload"]
        assert payload["event_id"] == "$event"
        assert payload["room_id"] == "!room:test"
        assert payload["sender_id"] == "@user:test"
        canonical_args = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        assert payload["input_hash"] == hashlib.sha256(canonical_args).hexdigest()
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        assert meta["signature"] == hmac.new(b"scoped-test-token", canonical_payload, hashlib.sha256).hexdigest()
        assert proof(CONFIG, "finish_miniproject_chat_turn", {})["org.project.chat_turn"]["payload"]["tool"] == "finish_miniproject_chat_turn"
    finally:
        clear_session_vars(tokens)
    with pytest.raises(AttestationUnavailable):
        proof(CONFIG, "save_miniproject_output", args)


def test_invalid_policy_and_background_context_fail_closed():
    tokens = set_session_vars(
        platform="matrix", chat_id="!room:test", user_id="@user:test",
        message_id="$event", cron_session="1", project_chat_authorized=True,
    )
    try:
        with pytest.raises(AttestationUnavailable, match="Background"):
            proof(CONFIG, "save_miniproject_output", {})
    finally:
        clear_session_vars(tokens)
    with pytest.raises(AttestationUnavailable, match="configuration"):
        proof({**CONFIG, "session_attestation": {"tools": []}}, "save_miniproject_output", {})


@pytest.mark.asyncio
async def test_host_meta_is_sent_only_with_attested_tool_call():
    from tools.mcp_tool_handlers import _call_tool_racing_stdio_death

    calls = []

    async def call_tool(name, **kwargs):
        calls.append((name, kwargs))
        return SimpleNamespace(content=[])

    server = SimpleNamespace(session=SimpleNamespace(call_tool=call_tool))
    marker = {"org.project.chat_turn": {"payload": {}, "signature": "host-only"}}
    await _call_tool_racing_stdio_death(server, "project_test", "save_miniproject_output", {}, marker)
    await _call_tool_racing_stdio_death(server, "project_test", "read_source", {})
    assert calls[0][1]["meta"] is marker
    assert "meta" not in calls[1][1]


def test_gateway_binds_authorization_only_from_matrix_event():
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session_context import project_chat_authorized, get_session_env

    source = SimpleNamespace(
        platform=Platform.MATRIX, chat_id="!room:test", chat_type=None,
        chat_name="", thread_id="", user_id="@user:test", user_id_alt="",
        user_name="", scope_id="", parent_chat_id="", message_id="$event", profile="project-test",
    )
    context = SimpleNamespace(source=source, session_key="matrix:room")
    runner = SimpleNamespace(adapters={})
    event = SimpleNamespace(message_id="$event", raw_message={"body": "write this"})
    tokens = GatewayRunner._set_session_env(runner, context, event)
    try:
        assert not project_chat_authorized()
    finally:
        clear_session_vars(tokens)
    event.raw_message["org.project.authorize_miniproject"] = True
    tokens = GatewayRunner._set_session_env(runner, context, event)
    try:
        assert project_chat_authorized()
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "$event"
    finally:
        clear_session_vars(tokens)


def test_hidden_finish_uses_host_proof_and_never_a_model_tool(monkeypatch):
    from tools import mcp_tool as core
    from tools import mcp_tool_loop as loop
    from tools import mcp_tool_config, mcp_tool_scope
    from tools.mcp_project_attestation import finish_attested_turns

    calls = []

    async def call_tool(name, **kwargs):
        calls.append((name, kwargs))
        return SimpleNamespace(content=[])

    server = SimpleNamespace(
        _config=CONFIG, session=SimpleNamespace(call_tool=call_tool), _rpc_lock=asyncio.Lock()
    )
    monkeypatch.setattr(core, "_servers", {"project_test": server})
    monkeypatch.setattr(mcp_tool_config, "_load_mcp_config", lambda: {"project_test": CONFIG})
    monkeypatch.setattr(mcp_tool_scope, "_resolve_server_key", lambda name: name)
    monkeypatch.setattr(loop, "_run_on_mcp_loop", lambda factory, timeout: asyncio.run(factory()))
    tokens = set_session_vars(
        platform="matrix", chat_id="!room:test", user_id="@user:test",
        message_id="$event", cron_session="", project_chat_authorized=True,
    )
    try:
        finish_attested_turns()
    finally:
        clear_session_vars(tokens)
    assert len(calls) == 1 and calls[0][0] == "finish_miniproject_chat_turn"
    assert "org.project.chat_turn" in calls[0][1]["meta"]
