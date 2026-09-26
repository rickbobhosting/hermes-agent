"""Chat-tab WebSocket routes: /api/console, /api/pty, the /api/ws gateway
sidecar and /api/pub + /api/events broadcast.

Helpers/state that tests monkeypatch on ``web_server`` stay there and are
reached through the late-binding seam (cycle-safe).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from agent.interrupt_scope import InterruptScope, bind_interrupt_scope
from hermes_cli.pty_session import AttachmentClosed, RegistryClosed, RegistryFull, SessionStopped
from hermes_cli.web_deps import LateState, late
from hermes_cli.web_routers.chat_ws_errors import chat_start_failure_message
from hermes_cli.web_server_chat import (
    _build_sidecar_url, _close_stalled_pty_input, _get_console_executor, _legacy_pump, _ws_auth_ok,
    _ws_request_is_allowed,
)

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_active_session_file_for_channel = late("_active_session_file_for_channel", "hermes_cli.web_server_chat")
_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
_resolve_chat_argv_async = late("_resolve_chat_argv_async", "hermes_cli.web_server_chat")
_resolve_profile_dir = late("_resolve_profile_dir", "hermes_cli.web_server_profiles")
_ws_auth_reason = late("_ws_auth_reason", "hermes_cli.web_server_chat")
_ws_client_reason = late("_ws_client_reason", "hermes_cli.web_server_chat")
_ws_host_origin_reason = late("_ws_host_origin_reason", "hermes_cli.web_server_chat")
_DASHBOARD_EMBEDDED_CHAT_ENABLED = LateState("_DASHBOARD_EMBEDDED_CHAT_ENABLED")
_require_token = late("_require_token")


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
_VALID_ATTACH_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


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


async def _broadcast_event(app: Any, channel: str, payload: str) -> None:
    """Fan out one publisher frame to every subscriber on `channel`."""
    try:
        event = json.loads(payload)
        params = event.get("params") if isinstance(event, dict) else None
    except json.JSONDecodeError:
        params = None
    if isinstance(params, dict) and params.get("type") == "dashboard.active_session_changed":
        actual = _channel_active_session(app, channel)
        declared = (params.get("payload") or {}).get("session_key") if isinstance(params.get("payload"), dict) else None
        if actual is None or actual != declared:
            return
    event_channels, event_lock = _get_event_state(app)
    async with event_lock:
        subs = list(event_channels.get(channel, ()))
    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            # Subscriber went away mid-send; /api/events' finally removes it.
            _log.warning("broadcast send failed for subscriber on %s", channel, exc_info=True)


def _channel_or_close_code(ws: WebSocket) -> Optional[str]:
    """Channel id from the query string, or None if invalid."""
    channel = ws.query_params.get("channel", "")
    return channel if _VALID_CHANNEL_RE.match(channel) else None


def _read_active_session_file(path: Path) -> Optional[str]:
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 1024:
            return None
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _valid_resume(str(data.get("session_id") or ""))


def _channel_active_session(app: FastAPI, channel: str) -> Optional[str]:
    from hermes_cli.web_server import _get_pty_active_session_files

    path = _get_pty_active_session_files(app).get(channel)
    return _read_active_session_file(path) if path is not None else None


def _valid_resume(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    if not value or len(value) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


def _canonical_profile(raw: Optional[str]) -> tuple[str, Optional[str]]:
    from hermes_cli import profiles
    from hermes_cli.dashboard_fleet import is_fleet_selector
    from hermes_cli.dashboard_fleet_access import require_fleet_access

    requested = (raw or "").strip()
    if not requested or requested.casefold() == "current":
        return profiles.get_active_profile_name(), None
    if is_fleet_selector(requested):
        require_fleet_access(requested, "execute")
        return requested, requested
    canonical = profiles.normalize_profile_name(requested)
    profiles.validate_profile_name(canonical)
    return canonical, canonical


def _retained_channel(token: str, profile_scope: str) -> str:
    # A token reused for another target must never read that target's active
    # session breadcrumb. The scope includes the resolved registered home.
    if "\0" not in profile_scope:
        return f"pty-{token}"
    digest = hashlib.sha256(profile_scope.encode("utf-8")).hexdigest()[:16]
    return f"pty-{token}-{digest}"


def _public_pty_snapshot(snapshot: dict) -> dict:
    metadata = snapshot.get("metadata") or {}
    return {
        field: snapshot.get(field)
        for field in ("id", "alive", "attached", "created_at", "last_attached_at",
                      "last_detached_at", "buffer_bytes", "buffer_truncated")
    } | {"metadata": {field: metadata.get(field) for field in ("profile", "resume") if metadata.get(field) is not None}}


def _fleet_pty_access(metadata: dict, required: str) -> bool:
    """Whether a retained external process still belongs to its registration."""
    from hermes_cli.dashboard_fleet import is_fleet_selector
    from hermes_cli.dashboard_fleet_access import require_fleet_access

    profile = metadata.get("profile")
    if not is_fleet_selector(profile):
        return True
    target = require_fleet_access(profile, required)
    return metadata.get("fleet_home") == str(target.home)


async def _forget_pty_channel(app: FastAPI, channel: str) -> None:
    from hermes_cli.web_server import _get_pty_active_session_files

    if _pty_claims(app).get(channel, 0):
        return
    snapshots = await _pty_registry().snapshots()
    if any((snapshot.get("metadata") or {}).get("channel") == channel for snapshot in snapshots):
        return
    path = _get_pty_active_session_files(app).pop(channel, None)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _log.warning("Could not remove PTY session breadcrumb for channel %s", channel)


def _pty_claims(app: FastAPI) -> dict[str, int]:
    try:
        return app.state.pty_active_session_file_claims
    except AttributeError:
        app.state.pty_active_session_file_claims = {}
        return app.state.pty_active_session_file_claims


async def _release_pty_claim(app: FastAPI, channel: str) -> None:
    claims = _pty_claims(app)
    remaining = claims.get(channel, 0) - 1
    if remaining > 0:
        claims[channel] = remaining
        return
    claims.pop(channel, None)
    await _forget_pty_channel(app, channel)


async def reap_pty_artifacts(app: FastAPI, *, interval: float = 60.0) -> None:
    """Reap exited tasks and their channel files; elapsed absence is harmless."""
    from hermes_cli.web_server import _get_pty_active_session_files

    while True:
        await asyncio.sleep(interval)
        try:
            await _pty_registry().reap_idle()
            for channel in list(_get_pty_active_session_files(app)):
                await _forget_pty_channel(app, channel)
        except Exception:
            _log.exception("PTY artifact cleanup failed")


async def reap_revoked_fleet_ptys_once(app: FastAPI) -> None:
    """Stop retained fleet children whose original home no longer has execute access.

    A detached PTY has no WebSocket through which to notice grant revocation.
    The registration/home check is repeated against the same metadata used for
    attachment, and only the exact matching retained process is stopped.
    """
    registry = _pty_registry()
    for snapshot in await registry.snapshots():
        metadata = snapshot.get("metadata") or {}
        if not isinstance(metadata.get("profile"), str) or not metadata["profile"].startswith("fleet:"):
            continue
        try:
            allowed = _fleet_pty_access(metadata, "execute")
        except HTTPException:
            allowed = False
        if allowed:
            continue
        await _stop_revoked_fleet_pty(app, snapshot["id"], metadata)


async def _stop_revoked_fleet_pty(app: FastAPI, session_id: str, metadata: dict) -> None:
    if await _pty_registry().stop(session_id):
        channel = metadata.get("channel")
        if isinstance(channel, str):
            await _forget_pty_channel(app, channel)


async def reap_revoked_fleet_ptys(app: FastAPI, *, interval: float = 1.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await reap_revoked_fleet_ptys_once(app)
        except Exception:
            _log.exception("Fleet PTY grant cleanup failed")


def _pty_registry():
    from hermes_cli.web_server_chat import PTY_REGISTRY
    return PTY_REGISTRY


@router.get("/api/pty/sessions")
async def retained_pty_sessions(request: Request):
    _require_token(request)
    visible = []
    for snapshot in await _pty_registry().snapshots():
        try:
            if _fleet_pty_access(snapshot.get("metadata") or {}, "inspect"):
                visible.append(_public_pty_snapshot(snapshot))
        except HTTPException:
            pass
    return {"sessions": visible}


@router.delete("/api/pty/sessions/{session_id}")
async def stop_retained_pty_session(session_id: str, request: Request):
    _require_token(request)
    registry = _pty_registry()
    selected = next((s for s in await registry.snapshots() if s["id"] == session_id), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Dashboard task not found.")
    metadata = selected.get("metadata") or {}
    profile = metadata.get("profile")
    # Stopping an already-owned process is a cleanup operation even after its
    # grant is revoked or registration removed. Bind that cleanup to the exact
    # original selector plus the opaque session ID; a sibling selector cannot
    # stop it. Local PTY callers keep their existing endpoint contract.
    if isinstance(profile, str) and profile.startswith("fleet:"):
        if request.query_params.get("profile") != profile:
            raise HTTPException(status_code=403, detail="Fleet task selector mismatch.")
    if not await registry.stop(session_id):
        raise HTTPException(status_code=404, detail="Dashboard task not found.")
    channel = metadata.get("channel")
    if isinstance(channel, str):
        await _forget_pty_channel(request.app, channel)
    return {"ok": True, "id": session_id}


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
# Cancel/timeout interrupt the worker cooperatively; this bounds how long the prompt waits for it to exit.
_CONSOLE_UNWIND_TIMEOUT_SECONDS = 10.0
_CONSOLE_OUTPUT_LIMIT = 50000


def _execute_console_line(
    engine: Any, line: str, *, confirmed: bool, profile: Optional[str], scope: Optional[InterruptScope] = None,
) -> Any:
    # _profile_scope swaps process-global skill module paths; keep it inside
    # the worker thread and never hold it across awaits.
    with _profile_scope(profile), bind_interrupt_scope(scope):
        return engine.execute(line, confirmed=confirmed)


async def _unwind_console_worker(worker: Any, scope: InterruptScope, reason: str) -> None:
    """Stop the command's worker after cancel/timeout: asyncio can only drop the waiter, so interrupt
    any agent the command forked (closing its provider request) and wait for the thread to exit.
    A user cancel is attributed to the user; only the timeout is a host-issued stop (#112647)."""
    if reason == "cancelled":
        scope.cancel(f"Console command {reason}", tool_reason=None)
    else:
        scope.cancel(f"Console command {reason}")
    if worker.cancel():  # still queued: never ran
        return
    exited = asyncio.wrap_future(worker)
    done, _ = await asyncio.wait({exited}, timeout=_CONSOLE_UNWIND_TIMEOUT_SECONDS)
    if not done:
        exited.cancel()
        _log.warning("console worker still running %ss after %s", _CONSOLE_UNWIND_TIMEOUT_SECONDS, reason)


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
        from hermes_cli.dashboard_fleet import is_fleet_selector
        if is_fleet_selector(profile):
            raise HTTPException(403, "The dashboard host console is unavailable for external fleet targets.")
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
        scope = InterruptScope()
        worker = _get_console_executor().submit(
            _execute_console_line, engine, line, confirmed=confirmed, profile=profile, scope=scope,
        )
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(worker), timeout=_CONSOLE_COMMAND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            await _unwind_console_worker(worker, scope, "cancelled")
            raise
        except asyncio.TimeoutError:
            await _unwind_console_worker(worker, scope, "timed out")
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
                    task, active_task = active_task, None
                    task.cancel()
                    # Report cancelled only once the worker (and any provider request it owns) is gone.
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
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


async def _pty_fail(ws: WebSocket, exc: BaseException) -> None:
    """Tell the user why chat could not start, then close 1011 so the SPA renders
    "Start new session". The raw exception goes to the server log only."""
    _log.warning("pty start failed: %s: %s", type(exc).__name__, exc)
    await ws.send_text(f"\r\n\x1b[31m{chat_start_failure_message(exc)}\x1b[0m\r\n")
    await ws.close(code=1011)


@router.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    from hermes_cli.web_server_chat import PTY_REGISTRY, PtyBridge, PtyUnavailableError, _PTY_BRIDGE_AVAILABLE, _RESIZE_RE

    gate = await _ws_gate(ws, "pty")
    if gate is None:
        return
    raw_attach = ws.query_params.get("attach") if "attach" in ws.query_params else None
    if raw_attach is not None and not _VALID_ATTACH_RE.fullmatch(raw_attach):
        await ws.close(code=4400, reason="invalid_attach_token")
        return
    attach_token = raw_attach.lower() if raw_attach is not None else None
    peer, mode, cred = gate
    await ws.accept()
    _log.info("pty accepted peer=%s mode=%s cred=%s", peer, mode, cred)

    if not _PTY_BRIDGE_AVAILABLE:
        await _pty_fail(ws, PtyUnavailableError("The embedded terminal is unavailable on this platform."))
        return

    raw_resume = ws.query_params.get("resume") or None
    if raw_resume and not _valid_resume(raw_resume):
        await _pty_fail(ws, ValueError("Invalid session resume target."))
        return
    try:
        canonical_profile, resolver_profile = _canonical_profile(ws.query_params.get("profile"))
    except (ValueError, HTTPException) as exc:
        await _pty_fail(ws, exc)
        return
    from hermes_cli.dashboard_fleet import is_fleet_selector
    from hermes_cli.dashboard_fleet_access import require_fleet_access
    try:
        fleet_target = (require_fleet_access(canonical_profile, "execute")
                        if is_fleet_selector(canonical_profile) else None)
    except HTTPException as exc:
        await _pty_fail(ws, exc)
        return
    if fleet_target and attach_token is None:
        await _pty_fail(ws, ValueError("Fleet chat requires a retained attachment token."))
        return
    profile_scope = f"{canonical_profile}\0{fleet_target.home}" if fleet_target else canonical_profile
    # Legacy caller-supplied channels are global names. An external chat without
    # a retained token gets no breadcrumb or sidecar channel to collide with a
    # different target; current browser clients always send an attach token.
    channel = (_retained_channel(attach_token, profile_scope) if attach_token
               else None if fleet_target else _channel_or_close_code(ws))
    sidecar_url = _build_sidecar_url(channel) if channel else None
    force_fresh = (ws.query_params.get("fresh") or "").strip().lower() in {"1", "true", "yes", "on"}
    resume = None if force_fresh else _valid_resume(raw_resume)
    implicit_resume = None
    if channel:
        claims = _pty_claims(ws.app)
        claims[channel] = claims.get(channel, 0) + 1
    claim_held = bool(channel)

    async def release_claim() -> None:
        nonlocal claim_held
        if claim_held and channel:
            claim_held = False
            await _release_pty_claim(ws.app, channel)

    active_session_file = _active_session_file_for_channel(ws.app, channel) if channel else None
    if active_session_file is not None:
        if force_fresh:
            active_session_file.unlink(missing_ok=True)
        elif not resume:
            resume = _read_active_session_file(active_session_file)
            implicit_resume = resume

    resolve_kwargs = {"resume": resume, "sidecar_url": sidecar_url, "profile": resolver_profile}
    if active_session_file is not None:
        resolve_kwargs["active_session_file"] = str(active_session_file)
    try:
        argv, cwd, env = await _resolve_chat_argv_async(**resolve_kwargs)
    except (HTTPException, SystemExit) as exc:
        await _pty_fail(ws, exc)
        await release_claim()
        return
    except BaseException:
        await release_claim()
        raise

    if fleet_target:
        try:
            current_target = require_fleet_access(canonical_profile, "execute")
            if (current_target.home != fleet_target.home or not env
                    or env.get("HERMES_HOME") != str(fleet_target.home)):
                raise HTTPException(403, "Fleet target changed while chat was starting.")
        except HTTPException as exc:
            await _pty_fail(ws, exc)
            await release_claim()
            return

    if attach_token is not None:
        # A retained PTY owns its gateway process. Stopping the PTY then
        # terminates the live agent instead of leaving an in-process turn.
        import os
        env = (os.environ if env is None else env).copy()
        env.pop("HERMES_TUI_GATEWAY_URL", None)

    def spawn():
        return PtyBridge.spawn(argv, cwd=cwd, env=env)

    if attach_token is None:
        try:
            bridge = await asyncio.to_thread(spawn)
        except (PtyUnavailableError, FileNotFoundError, OSError) as exc:
            await _pty_fail(ws, exc)
            await release_claim()
            return
        await release_claim()
        if implicit_resume:
            await ws.send_json({"type": "resume", "id": implicit_resume})
        await _legacy_pump(ws, bridge)
        return

    key = f"{attach_token}\0{profile_scope}"
    metadata = {"profile": canonical_profile, "resume": resume, "channel": channel}
    if fleet_target:
        metadata["fleet_home"] = str(fleet_target.home)
    try:
        session, created = await PTY_REGISTRY.attach_or_spawn(key, spawn=spawn, metadata=metadata)
    except RegistryClosed:
        await ws.close(code=1001, reason="pty_server_shutting_down")
        await release_claim()
        return
    except SessionStopped:
        await ws.close(code=4400, reason="pty_session_stopped")
        await release_claim()
        return
    except RegistryFull as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=4429, reason="pty_capacity_reached")
        await release_claim()
        return
    except (PtyUnavailableError, FileNotFoundError, OSError) as exc:
        await _pty_fail(ws, exc)
        await release_claim()
        return
    except BaseException:
        await release_claim()
        raise
    if fleet_target:
        try:
            allowed = _fleet_pty_access(metadata, "execute")
        except HTTPException:
            allowed = False
        if not allowed:
            await _stop_revoked_fleet_pty(ws.app, session.id, metadata)
            with contextlib.suppress(Exception):
                await ws.close(code=4403, reason="fleet_access_revoked")
            await release_claim()
            return
    if not created:
        # The stored metadata follows the latest canonical resume target.
        session.metadata.update(metadata)
    await release_claim()
    if implicit_resume:
        await ws.send_json({"type": "resume", "id": implicit_resume})

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
            if fleet_target:
                try:
                    allowed = _fleet_pty_access(session.metadata, "execute")
                except HTTPException:
                    allowed = False
                if not allowed:
                    await _stop_revoked_fleet_pty(ws.app, session.id, session.metadata)
                    with contextlib.suppress(Exception):
                        await ws.close(code=4403, reason="fleet_access_revoked")
                    break
            raw = message.get("bytes")
            if raw is None:
                text = message.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                if not session.resize(attachment, cols=int(match.group(1)), rows=int(match.group(2))):
                    break
                continue
            if not session.write(attachment, raw):
                break
    except AttachmentClosed as exc:
        with contextlib.suppress(Exception):
            await ws.close(code=exc.code, reason=exc.reason)
    except WebSocketDisconnect:
        pass
    except Exception:
        _log.exception("dashboard PTY attachment failed session=%s", session.id)
        with contextlib.suppress(Exception):
            await ws.close(code=1011, reason="pty_attachment_failed")
    finally:
        if attachment is not None:
            PTY_REGISTRY.detach(key, attachment)


# --- /api/ws: JSON-RPC sidecar for the Chat tab. Drives the same
# tui_gateway.dispatch surface Ink uses over stdio so the dashboard can render
# structured metadata next to the xterm; both transports bind to the same
# session id, so agent emits fan out to both sinks.


@router.websocket("/api/ws")
async def gateway_ws(ws: WebSocket) -> None:
    if not await _close_unless_sidecar_allowed(ws):
        return
    from tui_gateway.ws import handle_ws

    # The authenticated identity (ticket / internal credential) stamped by
    # _ws_auth_reason becomes the identity authority for privileged RPCs
    # (browser.controller.register). None on the legacy token path.
    await handle_ws(
        ws,
        auth_identity=getattr(ws, "_hermes_auth_identity", None),
        subprotocol=getattr(ws, "_hermes_ws_subprotocol", None),
    )


# --- /api/pub + /api/events: the PTY-side tui_gateway.entry opens /api/pub
# (HERMES_TUI_SIDECAR_URL from /api/pty's env) and writes every dispatcher emit
# through it; the dashboard fans frames out to /api/events subscribers on the
# same channel — the React sidebar's tool-call feed without touching the PTY
# child's stdio handshake with Ink.


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
    try:
        while True:
            await _broadcast_event(ws.app, channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass


@router.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    channel = await _accept_channel_ws(ws)
    if channel is None:
        return
    event_channels, event_lock = _get_event_state(ws.app)
    async with event_lock:
        event_channels.setdefault(channel, set()).add(ws)
    try:
        if session_key := _channel_active_session(ws.app, channel):
            await ws.send_json({
                "jsonrpc": "2.0", "method": "event",
                "params": {"type": "dashboard.active_session_changed", "payload": {"session_key": session_key}},
            })
        while True:
            # Subscribers don't speak — receive() just blocks until disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with event_lock:
            subs = event_channels.get(channel)
            if subs is not None:
                subs.discard(ws)
                if not subs:
                    event_channels.pop(channel, None)
