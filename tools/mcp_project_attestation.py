"""Host-only proof for a user-authorized Matrix Project MCP turn.

The model supplies tool arguments. Session identity and the scoped MCP bearer
come only from the gateway context and the server connection configuration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from uuid import uuid4

from gateway.session_context import get_session_env, project_chat_authorized

log = logging.getLogger("tools.mcp_tool")
_META_KEY = "org.project.chat_turn"
_FINISH_TOOL = "finish_miniproject_chat_turn"


class AttestationUnavailable(ValueError):
    pass


def policy(config: dict) -> dict | None:
    if "session_attestation" not in config:
        return None
    value = config["session_attestation"]
    if not isinstance(value, dict) or value.get("meta_key") != _META_KEY:
        raise AttestationUnavailable("MCP session attestation configuration is invalid")
    tools = value.get("tools")
    if (
        not isinstance(tools, list)
        or not tools
        or any(not isinstance(name, str) or not name for name in tools)
        or len(set(tools)) != len(tools)
        or value.get("finish_tool") != _FINISH_TOOL
        or _FINISH_TOOL in tools
    ):
        raise AttestationUnavailable("MCP session attestation configuration is invalid")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _bearer(config: dict) -> str:
    headers = config.get("headers")
    if not isinstance(headers, dict):
        raise AttestationUnavailable("Scoped MCP bearer is unavailable")
    matches = [value for key, value in headers.items() if str(key).lower() == "authorization"]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise AttestationUnavailable("Scoped MCP bearer is unavailable")
    scheme, separator, token = matches[0].partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or token.strip() != token:
        raise AttestationUnavailable("Scoped MCP bearer is unavailable")
    return token


def _matrix_identity() -> tuple[str, str, str]:
    if not project_chat_authorized() or get_session_env("HERMES_SESSION_PLATFORM") != "matrix":
        raise AttestationUnavailable("A trusted user Matrix chat turn is required")
    if get_session_env("HERMES_CRON_SESSION"):
        raise AttestationUnavailable("Background turns cannot authorize Project writes")
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            raise AttestationUnavailable("Delegated turns cannot authorize Project writes")
    except ImportError:
        pass
    event_id = get_session_env("HERMES_SESSION_MESSAGE_ID")
    room_id = get_session_env("HERMES_SESSION_CHAT_ID")
    sender_id = get_session_env("HERMES_SESSION_USER_ID")
    if not (event_id.startswith("$") and room_id.startswith("!") and sender_id.startswith("@")):
        raise AttestationUnavailable("Matrix event identity is unavailable")
    return event_id, room_id, sender_id


def proof(config: dict, tool_name: str, arguments: dict) -> dict:
    configured = policy(config)
    if configured is None or tool_name not in (*configured["tools"], _FINISH_TOOL):
        raise AttestationUnavailable("MCP tool has no session attestation policy")
    event_id, room_id, sender_id = _matrix_identity()
    payload = {
        "event_id": event_id,
        "room_id": room_id,
        "sender_id": sender_id,
        "issued_at": int(time.time()),
        "nonce": str(uuid4()),
        "tool": tool_name,
        "input_hash": hashlib.sha256(_canonical(arguments)).hexdigest(),
    }
    signature = hmac.new(_bearer(config).encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()
    return {_META_KEY: {"payload": payload, "signature": signature}}


def redact_host_material(text: str, meta: dict | None, config: dict) -> str:
    if meta is None:
        return text
    for secret in (meta[_META_KEY]["signature"], _bearer(config)):
        text = text.replace(secret, "[host secret redacted]")
    return text


def finish_attested_turns() -> None:
    """Close authorized rows after the gateway turn; this hidden call never reaches the model."""
    if not project_chat_authorized():
        return
    from tools import mcp_tool as core
    from tools import mcp_tool_loop as loop
    from tools.mcp_tool_config import _load_mcp_config
    from tools.mcp_tool_scope import _resolve_server_key

    for name, config in _load_mcp_config().items():
        try:
            if policy(config) is None:
                continue
            with core._lock:
                server = core._servers.get(_resolve_server_key(name))
            if server is None or server.session is None:
                continue  # No authorization was sent through a disconnected server.
            meta = proof(server._config, _FINISH_TOOL, {})

            async def close():
                async with server._rpc_lock:
                    return await server.session.call_tool(_FINISH_TOOL, arguments={}, meta=meta)

            loop._run_on_mcp_loop(close, timeout=10)
        except Exception as exc:
            # Never log proof, bearer, arguments, or exception text: SDK errors may echo headers.
            log.warning("MCP %s: authorized chat closure failed (%s)", name, type(exc).__name__)
