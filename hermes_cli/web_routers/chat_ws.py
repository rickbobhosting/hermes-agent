"""Chat-tab WebSocket routes: /api/console, /api/pty, the /api/ws gateway
sidecar and /api/pub + /api/events broadcast.

Helpers/state that tests monkeypatch on ``web_server`` stay there and are
reached through the late-binding seam (cycle-safe).
"""

import asyncio
import functools
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from hermes_cli.pty_session import (
    AttachmentClosed,
    RegistryClosed,
    RegistryFull,
    SessionStopped,
)
from hermes_cli.web_deps import LateState, late
from hermes_cli.web_server_chat import (
    _build_sidecar_url,
    _claim_active_session_file_for_channel,
    _forget_active_session_channel,
    _forget_active_session_file,
    _forget_event_channel_cache,
    _get_console_executor,
    _legacy_pump,
    _release_active_session_file_claim,
    _watch_retained_pty_exit,
    _ws_auth_ok,
    _ws_request_is_allowed,
)

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_active_session_file_for_channel = late("_active_session_file_for_channel", "hermes_cli.web_server_chat")
_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
_resolve_chat_argv_async = late("_resolve_chat_argv_async", "hermes_cli.web_server_chat")
_resolve_profile_dir = late("_resolve_profile_dir", "hermes_cli.web_server_profiles")
_require_token = late("_require_token")
_ws_auth_reason = late("_ws_auth_reason", "hermes_cli.web_server_chat")
_ws_client_reason = late("_ws_client_reason", "hermes_cli.web_server_chat")
_ws_host_origin_reason = late("_ws_host_origin_reason", "hermes_cli.web_server_chat")
_DASHBOARD_EMBEDDED_CHAT_ENABLED = LateState("_DASHBOARD_EMBEDDED_CHAT_ENABLED")


def _get_event_state(app: "FastAPI"):
    """(event_channels, event_lock) from app.state, lazily initialised when the
    lifespan hasn't run (TestClient without a ``with`` block). The lifespan path
    is preferred because it creates the Lock on the correct event loop."""
    try:
        return app.state.event_channels, app.state.event_lock
    except AttributeError:
        app.state.event_channels = {}
        app.state.event_lock = asyncio.Lock()
        return app.state.event_channels, app.state.event_lock


_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_VALID_PTY_ATTACH_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_VALID_RETAINED_PTY_CHANNEL_RE = re.compile(r"^pty-[0-9a-f]{32}$")
_PTY_REGISTRY_KEY_DELIMITER = "\0"
_PTY_INVALID_ATTACH_CLOSE_CODE = 4422
_PTY_CAPACITY_CLOSE_CODE = 4429
_PTY_ACTIVE_SESSION_CACHE_MAX_CHANNELS = 64
_PTY_ACTIVE_SESSION_KEY_MAX_CHARS = 512
_PTY_ACTIVE_SESSION_FRAME_MAX_CHARS = 16 * 1024
_PTY_EVENT_REPLAY_SEND_TIMEOUT_SECONDS = 1.0


def _ws_auth_mode() -> str:
    """Short label for the active WS auth mode — logged on every connection."""
    from hermes_cli.web_server_chat import _LOOPBACK_HOSTS
    from hermes_cli.web_server import app
    if getattr(app.state, "auth_required", False):
        return "gated"
    bound_host = (getattr(app.state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return "insecure"
    return "loopback"


def _get_event_replay_state(app: FastAPI) -> tuple[dict[str, set], dict[str, str]]:
    try:
        publishers = app.state.event_publishers
    except AttributeError:
        publishers = app.state.event_publishers = {}
    try:
        active_sessions = app.state.event_active_sessions
    except AttributeError:
        active_sessions = app.state.event_active_sessions = {}
    return publishers, active_sessions


def _active_session_replay_frame(session_key: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "dashboard.active_session_changed",
                "payload": {"session_key": session_key},
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _project_active_session_event(
    payload: str,
) -> tuple[bool, Optional[tuple[str, str]]]:
    """Project the sole replayable event to a bounded, non-secret frame."""
    try:
        frame = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return False, None
    if not isinstance(frame, dict) or frame.get("method") != "event":
        return False, None
    params = frame.get("params")
    if (
        not isinstance(params, dict)
        or params.get("type") != "dashboard.active_session_changed"
    ):
        return False, None
    if len(payload) > _PTY_ACTIVE_SESSION_FRAME_MAX_CHARS:
        return True, None
    event_payload = params.get("payload")
    session_key = (
        event_payload.get("session_key")
        if isinstance(event_payload, dict)
        else None
    )
    if (
        not isinstance(session_key, str)
        or not session_key
        or len(session_key) > _PTY_ACTIVE_SESSION_KEY_MAX_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in session_key)
    ):
        return True, None
    return True, (session_key, _active_session_replay_frame(session_key))


def _store_active_session_cache_locked(
    active_sessions: dict[str, str], channel: str, replay: str
) -> None:
    active_sessions.pop(channel, None)
    while len(active_sessions) >= _PTY_ACTIVE_SESSION_CACHE_MAX_CHANNELS:
        active_sessions.pop(next(iter(active_sessions)))
    active_sessions[channel] = replay


def _update_active_session_cache_locked(
    app: FastAPI,
    active_sessions: dict[str, str],
    channel: str,
    payload: str,
) -> Optional[str]:
    is_active_event, projection = _project_active_session_event(payload)
    if not is_active_event:
        return payload
    if (
        not _VALID_RETAINED_PTY_CHANNEL_RE.fullmatch(channel)
        or projection is None
    ):
        return None
    session_key, replay = projection
    from hermes_cli.web_server import _get_pty_active_session_files

    path = _get_pty_active_session_files(app).get(channel)
    breadcrumb = _read_active_session_file(path) if path is not None else None
    # The TUI writes this server-owned 0600 file before publishing. It is the
    # channel integrity boundary, so stale or forged aliases cannot win.
    if breadcrumb != session_key:
        return None
    _store_active_session_cache_locked(active_sessions, channel, replay)
    return replay


async def _broadcast_event(app: FastAPI, channel: str, payload: str) -> None:
    """Fan out one frame while retaining only authenticated focus identity."""
    event_channels, event_lock = _get_event_state(app)
    _publishers, active_sessions = _get_event_replay_state(app)
    async with event_lock:
        forwarded = _update_active_session_cache_locked(
            app, active_sessions, channel, payload
        )
        if forwarded is None:
            return
        subs = list(event_channels.get(channel, ()))
    for sub in subs:
        try:
            await sub.send_text(forwarded)
        except Exception:
            _log.warning(
                "broadcast send failed for subscriber on %s",
                channel,
                exc_info=True,
            )


async def _register_event_publisher(
    app: FastAPI, channel: str, publisher: Any
) -> None:
    _event_channels, event_lock = _get_event_state(app)
    publishers, _active_sessions = _get_event_replay_state(app)
    async with event_lock:
        publishers.setdefault(channel, set()).add(publisher)


async def _unregister_event_publisher(
    app: FastAPI, channel: str, publisher: Any
) -> None:
    _event_channels, event_lock = _get_event_state(app)
    publishers, active_sessions = _get_event_replay_state(app)
    async with event_lock:
        owners = publishers.get(channel)
        if owners is None:
            return
        owners.discard(publisher)
        if not owners:
            publishers.pop(channel, None)
            active_sessions.pop(channel, None)


async def _register_event_subscriber(
    app: FastAPI, channel: str, subscriber: Any
) -> bool:
    """Register and replay focus atomically before any later live frame."""
    event_channels, event_lock = _get_event_state(app)
    _publishers, active_sessions = _get_event_replay_state(app)
    async with event_lock:
        subscribers = event_channels.setdefault(channel, set())
        subscribers.add(subscriber)
        replay = None
        if _VALID_RETAINED_PTY_CHANNEL_RE.fullmatch(channel):
            from hermes_cli.web_server import _get_pty_active_session_files

            path = _get_pty_active_session_files(app).get(channel)
            session_key = (
                _read_active_session_file(path) if path is not None else None
            )
            if session_key is not None:
                replay = _active_session_replay_frame(session_key)
                _store_active_session_cache_locked(
                    active_sessions, channel, replay
                )
            else:
                active_sessions.pop(channel, None)
        if replay is not None:
            try:
                await asyncio.wait_for(
                    subscriber.send_text(replay),
                    timeout=_PTY_EVENT_REPLAY_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                subscribers.discard(subscriber)
                if not subscribers:
                    event_channels.pop(channel, None)
                return False
    return True


async def _unregister_event_subscriber(
    app: FastAPI, channel: str, subscriber: Any
) -> None:
    event_channels, event_lock = _get_event_state(app)
    async with event_lock:
        subscribers = event_channels.get(channel)
        if subscribers is None:
            return
        subscribers.discard(subscriber)
        if not subscribers:
            event_channels.pop(channel, None)


def _channel_or_close_code(ws: WebSocket) -> Optional[str]:
    """Channel id from the query string, or None if invalid."""
    channel = ws.query_params.get("channel", "")
    return channel if _VALID_CHANNEL_RE.match(channel) else None


def _read_active_session_file(path: Path) -> Optional[str]:
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > _PTY_ACTIVE_SESSION_FRAME_MAX_CHARS:
            return None
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    session_key = str(data.get("session_id") or "").strip()
    if (
        not session_key
        or len(session_key) > _PTY_ACTIVE_SESSION_KEY_MAX_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in session_key)
    ):
        return None
    return session_key


def _ws_close_reason(text: str) -> str:
    """Clamp to RFC 6455's 123-byte close-reason limit (uvicorn raises past it);
    reasons embed an attacker-controlled origin, so truncate rather than crash."""
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 123:
        return text
    return encoded[:120].decode("utf-8", "ignore") + "..."


async def _ws_gate(ws: WebSocket, kind: str) -> Optional[tuple[str, str, str]]:
    """Run the pre-accept gates for /api/console and /api/pty.

    Each gate maps to a distinct close code so the log and the browser banner
    agree on the cause: 4404 chat disabled, 4401 bad credential, 4403
    host/origin mismatch, 4408 peer not allowed. Returns ``(peer, mode, cred)``
    once every gate passes, or None after closing the socket.
    """
    peer = ws.client.host if ws.client else "?"
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        _log.info("%s refused: embedded chat disabled peer=%s", kind, peer)
        await ws.close(code=4404, reason="embedded chat disabled")
        return None

    auth_reason, cred = _ws_auth_reason(ws)
    mode = _ws_auth_mode()
    if auth_reason is not None:
        _log.warning("%s auth rejected reason=%s mode=%s cred=%s peer=%s", kind, auth_reason, mode, cred, peer)
        await ws.close(code=4401, reason=_ws_close_reason(f"auth: {auth_reason}"))
        return None

    host_origin_reason = _ws_host_origin_reason(ws)
    if host_origin_reason is not None:
        _log.warning("%s refused: %s peer=%s", kind, host_origin_reason, peer)
        await ws.close(code=4403, reason=_ws_close_reason(host_origin_reason))
        return None

    client_reason = _ws_client_reason(ws)
    if client_reason is not None:
        _log.warning("%s refused: %s", kind, client_reason)
        await ws.close(code=4408, reason=_ws_close_reason(client_reason))
        return None
    return peer, mode, cred


async def _close_unless_sidecar_allowed(ws: WebSocket) -> bool:
    """Pre-accept gates for the /api/ws, /api/pub and /api/events sidecars:
    4403 when chat is disabled or the request isn't allowed, 4401 on bad auth."""
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return False
    if not _ws_auth_ok(ws):
        await ws.close(code=4401)
        return False
    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return False
    return True


# --- /api/console: the curated console engine, in-process, exchanging JSON
# frames with the dashboard xterm overlay. Never spawns a PTY, shell or CLI.

_CONSOLE_PROMPT = "hermes> "
_CONSOLE_COMMAND_TIMEOUT_SECONDS = 60.0
_CONSOLE_OUTPUT_LIMIT = 50000


def _execute_console_line(engine: Any, line: str, *, confirmed: bool, profile: Optional[str]) -> Any:
    # _profile_scope swaps process-global skill module paths; keep it inside
    # the worker thread and never hold it across awaits.
    with _profile_scope(profile):
        return engine.execute(line, confirmed=confirmed)


class _ConsoleSender:
    """Serialises frames onto one console socket and owns the prompt suffix."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.lock = asyncio.Lock()

    async def send(self, payload: Dict[str, Any]) -> None:
        async with self.lock:
            await self.ws.send_json(payload)

    async def prompt(self, **payload: Any) -> None:
        await self.send({**payload, "prompt": _CONSOLE_PROMPT})

    async def error(self, message: str, *, id: Optional[int] = None, command: Optional[str] = None,
                    prompt: Optional[str] = None) -> None:
        # Key order matches the historical frames: type, id, message, command, prompt.
        frame: Dict[str, Any] = {"type": "error"}
        if id is not None:
            frame["id"] = id
        frame["message"] = message
        if command is not None:
            frame["command"] = command
        if prompt is not None:
            frame["prompt"] = prompt
        await self.send(frame)

    async def complete(self, status: str, command: str, command_id: int, *, prompt: str = _CONSOLE_PROMPT) -> None:
        await self.send({"type": "complete", "id": command_id, "status": status, "command": command, "prompt": prompt})

    async def error_then_complete(self, message: str, command: str, command_id: int, status: str) -> None:
        await self.error(message, id=command_id, command=command)
        await self.complete(status, command, command_id)

    async def send_result(self, result: Any, *, command_id: int) -> None:
        command = result.command or ""
        status = result.status
        if status == "ok":
            if result.output:
                await self.send({
                    "type": "output", "id": command_id, "stream": "stdout",
                    "data": result.output, "command": command,
                })
            await self.complete("ok", command, command_id)
        elif status == "error":
            await self.error_then_complete(result.output or "Command failed.", command, command_id, "error")
        elif status == "confirm_required":
            await self.prompt(
                type="confirm_required", id=command_id, command=command,
                message=result.confirmation_message or f"Run `{command}`?",
            )
            await self.complete("confirm_required", command, command_id)
        elif status == "clear":
            await self.send({"type": "clear", "id": command_id})
            await self.complete("clear", command, command_id)
        elif status == "exit":
            await self.complete("exit", command, command_id, prompt="")
        else:
            await self.error(f"Unknown console result status: {status}", id=command_id, command=command)


def _console_json_payload(msg: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    raw: str | bytes | None = msg.get("text")
    if raw is None:
        raw = msg.get("bytes")
    if raw is None:
        return None, None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "Console frames must be UTF-8 JSON."
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Console frames must be JSON objects."
    if not isinstance(payload, dict):
        return None, "Console frames must be JSON objects."
    return payload, None


@router.websocket("/api/console")
async def console_ws(ws: WebSocket) -> None:
    gate = await _ws_gate(ws, "console")
    if gate is None:
        return
    peer, mode, cred = gate
    await ws.accept()

    profile = (ws.query_params.get("profile") or "").strip() or None
    out = _ConsoleSender(ws)

    try:
        from hermes_cli.console_engine import HermesConsoleEngine

        engine = HermesConsoleEngine(output_limit=_CONSOLE_OUTPUT_LIMIT)
        if profile and profile.lower() != "current":
            _resolve_profile_dir(profile)
    except HTTPException as exc:
        await out.error(str(exc.detail), prompt="")
        await ws.close(code=4400, reason=_ws_close_reason(str(exc.detail)))
        return
    except Exception as exc:
        _log.exception("console failed to initialize")
        await out.error(f"Console unavailable: {exc}", prompt="")
        await ws.close(code=1011)
        return

    _log.info("console accepted peer=%s mode=%s cred=%s profile=%s", peer, mode, cred, profile or "current")
    await out.prompt(type="ready", profile=profile or "current")

    active_task: asyncio.Task | None = None
    pending_confirmation: Optional[str] = None
    command_generation = 0

    async def run_command(line: str, *, confirmed: bool, command_id: int) -> None:
        nonlocal active_task, pending_confirmation, command_generation
        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    _get_console_executor(),
                    functools.partial(_execute_console_line, engine, line, confirmed=confirmed, profile=profile),
                ),
                timeout=_CONSOLE_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if command_id == command_generation:
                pending_confirmation = None
                await out.error_then_complete(
                    "Command timed out. Hermes Console returned to the prompt.", line, command_id, "timeout",
                )
        except Exception as exc:
            if command_id == command_generation:
                pending_confirmation = None
                _log.exception("console command failed")
                await out.error_then_complete(str(exc) or exc.__class__.__name__, line, command_id, "error")
        else:
            if command_id != command_generation:
                return
            pending_confirmation = result.command if result.status == "confirm_required" else None
            await out.send_result(result, command_id=command_id)
            if result.status == "exit":
                await ws.close(code=1000)
        finally:
            if command_id == command_generation:
                active_task = None

    def start_command(line: str, *, confirmed: bool = False) -> None:
        nonlocal active_task, command_generation
        command_generation += 1
        active_task = asyncio.create_task(run_command(line, confirmed=confirmed, command_id=command_generation))

    try:
        while True:
            try:
                msg = await ws.receive()
            except RuntimeError:
                break
            if msg.get("type") == "websocket.disconnect":
                break

            payload, error = _console_json_payload(msg)
            if error:
                await out.prompt(type="error", message=error)
                continue
            if payload is None:
                continue

            frame_type = str(payload.get("type") or "").strip().lower()
            if frame_type == "ping":
                await out.prompt(type="pong")
                continue

            if frame_type == "cancel":
                if active_task and not active_task.done():
                    command_generation += 1
                    active_task.cancel()
                    active_task = None
                    pending_confirmation = None
                    await out.prompt(type="complete", status="cancelled")
                elif pending_confirmation:
                    pending_confirmation = None
                    await out.prompt(type="complete", status="cancelled")
                else:
                    await out.prompt(type="complete", status="idle")
                continue

            if active_task and not active_task.done():
                await out.prompt(type="error", message="A console command is already running.")
                continue

            if frame_type == "confirm":
                command = str(payload.get("command") or pending_confirmation or "").strip()
                if not pending_confirmation:
                    await out.prompt(type="error", message="No command is waiting for confirmation.")
                    continue
                if command != pending_confirmation:
                    await out.prompt(type="error", message="Confirmation does not match the pending command.")
                    continue
                pending_confirmation = None
                start_command(command, confirmed=True)
                continue

            if frame_type in {"input", "command"}:
                line = str(payload.get("line") or payload.get("command") or "").strip()
                if not line:
                    await out.prompt(type="complete", status="ok")
                    continue
                if pending_confirmation:
                    await out.prompt(
                        type="error",
                        message="Confirm or cancel the pending command before running another one.",
                    )
                    continue
                start_command(line)
                continue

            await out.prompt(type="error", message=f"Unsupported console frame: {frame_type or '?'}")
    except WebSocketDisconnect:
        pass
    finally:
        if active_task and not active_task.done():
            active_task.cancel()
            try:
                await active_task
            except (asyncio.CancelledError, Exception):
                pass


async def _pty_fail(ws: WebSocket, text: str) -> None:
    await ws.send_text(f"\r\n\x1b[31m{text}\x1b[0m\r\n")
    await ws.close(code=1011)


def _canonical_pty_profile(
    raw_profile: Optional[str],
) -> tuple[str, Optional[str]]:
    """Return the stable registry profile and argv-resolver argument."""
    from hermes_cli import profiles as profiles_mod

    requested = (raw_profile or "").strip()
    if not requested or requested.casefold() == "current":
        return profiles_mod.get_active_profile_name(), None
    try:
        canonical = profiles_mod.normalize_profile_name(requested)
        profiles_mod.validate_profile_name(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return canonical, canonical


def _validated_pty_resume(raw_resume: Optional[str]) -> Optional[str]:
    """Validate a resume target before forwarding it to argv or metadata."""
    if raw_resume is None:
        return None
    resume = raw_resume.strip()
    if not resume:
        return None
    if (
        len(resume) > _PTY_ACTIVE_SESSION_KEY_MAX_CHARS
        or _PTY_REGISTRY_KEY_DELIMITER in resume
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in resume)
    ):
        raise HTTPException(
            status_code=400, detail="Invalid session resume target."
        )
    return resume


def _pty_registry_key(attach_token: str, canonical_profile: str) -> str:
    """Build an internal identity from validated token and profile only.

    Resume lineage is mutable during model switches and compression, so it is
    spawn metadata rather than retained-process identity. Independently chosen
    tasks receive distinct browser attachment tokens.
    """
    parts = (attach_token, canonical_profile)
    if any(_PTY_REGISTRY_KEY_DELIMITER in part for part in parts):
        raise ValueError("invalid dashboard PTY registry key component")
    return _PTY_REGISTRY_KEY_DELIMITER.join(parts)


def _pty_snapshot_value(snapshot: Any, field: str, default: Any = None) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(field, default)
    return getattr(snapshot, field, default)


def _public_pty_snapshot(snapshot: Any) -> dict[str, Any]:
    """Project a registry snapshot without secret keys or routing channels."""
    raw_metadata = _pty_snapshot_value(snapshot, "metadata", {}) or {}
    metadata = {
        field: raw_metadata.get(field)
        for field in ("profile", "resume")
        if isinstance(raw_metadata, dict) and raw_metadata.get(field) is not None
    }
    return {
        field: _pty_snapshot_value(snapshot, field)
        for field in (
            "id",
            "alive",
            "attached",
            "created_at",
            "last_attached_at",
            "last_detached_at",
            "buffer_bytes",
            "buffer_truncated",
        )
    } | {"metadata": metadata}


@router.get("/api/pty/sessions")
async def retained_pty_sessions(request: Request):
    """List retained dashboard terminals without attachment credentials."""
    from hermes_cli.web_server_chat import PTY_REGISTRY

    _require_token(request)
    snapshots = await PTY_REGISTRY.snapshots()
    return {"sessions": [_public_pty_snapshot(item) for item in snapshots]}


@router.delete("/api/pty/sessions/{session_id}")
async def stop_retained_pty_session(session_id: str, request: Request):
    """Stop exactly one retained terminal by its public, non-secret id."""
    from hermes_cli.web_server_chat import PTY_REGISTRY

    _require_token(request)
    before = await PTY_REGISTRY.snapshots()
    selected = next(
        (
            item
            for item in before
            if str(_pty_snapshot_value(item, "id", "")) == session_id
        ),
        None,
    )
    if selected is None or not await PTY_REGISTRY.stop(session_id):
        raise HTTPException(status_code=404, detail="Dashboard task not found.")

    raw_metadata = _pty_snapshot_value(selected, "metadata", {}) or {}
    channel = (
        raw_metadata.get("channel") if isinstance(raw_metadata, dict) else None
    )
    if isinstance(channel, str) and _VALID_RETAINED_PTY_CHANNEL_RE.fullmatch(
        channel
    ):
        remaining = await PTY_REGISTRY.snapshots()
        still_owned = any(
            (_pty_snapshot_value(item, "metadata", {}) or {}).get("channel")
            == channel
            for item in remaining
        )
        if not still_owned:
            _forget_active_session_channel(request.app, channel)
            await _forget_event_channel_cache(request.app, channel)
    return {"ok": True, "id": session_id}


@router.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    from hermes_cli.web_server_chat import (
        PTY_REGISTRY,
        PtyBridge,
        PtyUnavailableError,
        _PTY_BRIDGE_AVAILABLE,
        _RESIZE_RE,
    )

    gate = await _ws_gate(ws, "pty")
    if gate is None:
        return
    peer, mode, cred = gate

    raw_attach = (
        ws.query_params.get("attach")
        if "attach" in ws.query_params
        else None
    )
    if raw_attach is not None and not _VALID_PTY_ATTACH_RE.fullmatch(raw_attach):
        _log.warning("pty refused: invalid_attach_token peer=%s", peer)
        await ws.close(
            code=_PTY_INVALID_ATTACH_CLOSE_CODE,
            reason="invalid_attach_token",
        )
        return
    attach_token = raw_attach.lower() if raw_attach is not None else None

    await ws.accept()
    _log.info("pty accepted peer=%s mode=%s cred=%s", peer, mode, cred)

    # Native Windows can't import the POSIX PTY bridge: say so and close cleanly.
    if not _PTY_BRIDGE_AVAILABLE:
        await ws.send_text(
            "\r\n\x1b[31mChat unavailable: the embedded terminal requires a "
            "POSIX PTY, which native Windows Python doesn't provide.\x1b[0m\r\n"
            "\x1b[33mInstall Hermes inside WSL2 to use the dashboard's /chat "
            "tab — the rest of the dashboard works here.\x1b[0m\r\n"
        )
        await ws.close(code=1011)
        return

    try:
        requested_resume = _validated_pty_resume(
            ws.query_params.get("resume") or None
        )
        canonical_profile, resolver_profile = _canonical_pty_profile(
            ws.query_params.get("profile") or None
        )
    except HTTPException as exc:
        await ws.send_text(
            f"\r\n\x1b[31mChat unavailable: {exc.detail}\x1b[0m\r\n"
        )
        await ws.close(code=1011, reason="invalid_pty_target")
        return

    # A retained process owns one stable event/breadcrumb channel for its
    # entire lifetime. Legacy clients keep their caller-supplied channel.
    channel = (
        f"pty-{attach_token}"
        if attach_token is not None
        else _channel_or_close_code(ws)
    )
    sidecar_url = _build_sidecar_url(channel) if channel else None
    force_fresh = (ws.query_params.get("fresh") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    resume = None if force_fresh else requested_resume
    active_session_file: Optional[Path] = None
    breadcrumb_claimed = False

    async def release_breadcrumb_claim() -> None:
        nonlocal breadcrumb_claimed
        if (
            not breadcrumb_claimed
            or channel is None
            or active_session_file is None
        ):
            return
        breadcrumb_claimed = False
        await _release_active_session_file_claim(
            ws.app, PTY_REGISTRY, channel, active_session_file
        )

    if channel:
        if attach_token is not None:
            active_session_file = _claim_active_session_file_for_channel(
                ws.app, channel
            )
            breadcrumb_claimed = True
        else:
            active_session_file = _active_session_file_for_channel(
                ws.app, channel
            )
        if force_fresh:
            _forget_active_session_file(active_session_file)
        elif not resume:
            resume = _read_active_session_file(active_session_file)
            if resume:
                await ws.send_json({"type": "resume", "id": resume})

    resolve_kwargs = {
        "resume": resume,
        "sidecar_url": sidecar_url,
        "profile": resolver_profile,
    }
    if active_session_file is not None:
        resolve_kwargs["active_session_file"] = str(active_session_file)

    try:
        argv, cwd, env = await _resolve_chat_argv_async(**resolve_kwargs)
    except HTTPException as exc:  # unknown/invalid profile
        await release_breadcrumb_claim()
        await _pty_fail(ws, f"Chat unavailable: {exc.detail}")
        return
    except SystemExit as exc:  # _make_tui_argv sys.exit(1)s when node/npm is missing
        await release_breadcrumb_claim()
        await _pty_fail(ws, f"Chat unavailable: {exc}")
        return
    except BaseException:
        await release_breadcrumb_claim()
        raise

    if attach_token is not None:
        # Retained terminals own their gateway process tree. This makes Stop
        # terminate the running agent too, instead of merely disconnecting it
        # from the dashboard's shared in-process gateway.
        env = (os.environ if env is None else env).copy()
        env.pop("HERMES_TUI_GATEWAY_URL", None)

    def _spawn():
        return PtyBridge.spawn(argv, cwd=cwd, env=env)

    if attach_token is None:
        try:
            bridge = await asyncio.to_thread(_spawn)
        except PtyUnavailableError as exc:
            await _pty_fail(ws, f"Chat unavailable: {exc}")
            return
        except (FileNotFoundError, OSError) as exc:
            await _pty_fail(ws, f"Chat failed to start: {exc}")
            return
        await _legacy_pump(ws, bridge)
        return

    try:
        effective_resume = resume
        if resume and env:
            effective_resume = _validated_pty_resume(
                env.get("HERMES_TUI_RESUME") or resume
            )
    except HTTPException as exc:
        await release_breadcrumb_claim()
        await ws.send_text(
            f"\r\n\x1b[31mChat unavailable: {exc.detail}\x1b[0m\r\n"
        )
        await ws.close(code=1011, reason="invalid_pty_target")
        return

    registry_key = _pty_registry_key(attach_token, canonical_profile)
    metadata = {
        "profile": canonical_profile,
        "resume": effective_resume,
        "channel": channel,
    }
    try:
        session, created = await PTY_REGISTRY.attach_or_spawn(
            registry_key, spawn=_spawn, metadata=metadata
        )
    except PtyUnavailableError as exc:
        await release_breadcrumb_claim()
        await _pty_fail(ws, f"Chat unavailable: {exc}")
        return
    except RegistryFull as exc:
        await release_breadcrumb_claim()
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(
            code=_PTY_CAPACITY_CLOSE_CODE, reason="pty_capacity_reached"
        )
        return
    except RegistryClosed:
        await release_breadcrumb_claim()
        await ws.close(code=1001, reason="pty_server_shutting_down")
        return
    except SessionStopped:
        await release_breadcrumb_claim()
        await ws.close(
            code=_PTY_INVALID_ATTACH_CLOSE_CODE,
            reason="pty_session_stopped",
        )
        return
    except (FileNotFoundError, OSError) as exc:
        await release_breadcrumb_claim()
        await _pty_fail(ws, f"Chat unavailable: {exc}")
        return
    except BaseException:
        await release_breadcrumb_claim()
        raise

    if not created:
        session.metadata.update(metadata)
    _watch_retained_pty_exit(ws.app, PTY_REGISTRY, session, channel)
    await release_breadcrumb_claim()

    attachment = None
    try:
        attachment = await session.attach(ws, force_redraw=not created)
        while True:
            try:
                message = await ws.receive()
            except RuntimeError:
                break
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if raw is None:
                text = message.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                if not session.resize(
                    attachment,
                    cols=int(match.group(1)),
                    rows=int(match.group(2)),
                ):
                    break
                continue
            if not session.write(attachment, raw):
                break
    except AttachmentClosed as exc:
        _log.info(
            "dashboard PTY attachment closed session=%s reason=%s",
            session.id,
            exc,
        )
        try:
            await ws.close(code=exc.code, reason=exc.reason)
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception:
        _log.exception("dashboard PTY attachment failed session=%s", session.id)
        try:
            await ws.close(code=1011, reason="pty_attachment_failed")
        except Exception:
            pass
    finally:
        if attachment is not None:
            PTY_REGISTRY.detach(registry_key, attachment)


@router.websocket("/api/ws")
async def gateway_ws(ws: WebSocket) -> None:
    if not await _close_unless_sidecar_allowed(ws):
        return
    from tui_gateway.ws import handle_ws

    await handle_ws(
        ws,
        auth_identity=getattr(ws, "_hermes_auth_identity", None),
        subprotocol=getattr(ws, "_hermes_ws_subprotocol", None),
    )


async def _accept_channel_ws(ws: WebSocket) -> Optional[str]:
    if not await _close_unless_sidecar_allowed(ws):
        return None
    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return None
    await ws.accept()
    return channel


@router.websocket("/api/pub")
async def pub_ws(ws: WebSocket) -> None:
    channel = await _accept_channel_ws(ws)
    if channel is None:
        return
    await _register_event_publisher(ws.app, channel, ws)
    try:
        while True:
            await _broadcast_event(ws.app, channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass
    finally:
        await _unregister_event_publisher(ws.app, channel, ws)


@router.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    channel = await _accept_channel_ws(ws)
    if channel is None:
        return
    if not await _register_event_subscriber(ws.app, channel, ws):
        return
    event_channels, event_lock = _get_event_state(ws.app)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await _unregister_event_subscriber(ws.app, channel, ws)
