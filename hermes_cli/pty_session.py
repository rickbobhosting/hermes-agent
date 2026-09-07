"""Server-owned PTYs with replaceable, bounded browser attachments.

Browser transport never owns process lifetime. Living sessions are retained until
explicitly stopped or server shutdown; capacity pressure rejects new sessions.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409
WS_CLOSE_SLOW_CLIENT = 1013
TUI_FORCE_REDRAW = b"\x0c"
DEFAULT_INPUT_BUFFER_CAP = 256 * 1024
# xterm may deliver a paste in one onData frame. Permit one reasonably large
# paste up to the entire per-attachment budget; larger frames revoke before
# any bytes are queued, keeping memory bounded without silently truncating.
DEFAULT_INPUT_CHUNK_CAP = DEFAULT_INPUT_BUFFER_CAP
DEFAULT_INPUT_QUEUE_CHUNKS = 128
DEFAULT_INPUT_WRITE_TIMEOUT = 1.0
_log = logging.getLogger(__name__)
_CLOSE_REASONS = {
    WS_CLOSE_PROCESS_EXITED: "pty_process_ended",
    WS_CLOSE_SUPERSEDED: "pty_superseded",
    WS_CLOSE_SLOW_CLIENT: "pty_attachment_interrupted",
}


class RingBuffer:
    """Keep only the most recent ``capacity`` bytes appended to the buffer."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("buffer capacity must be positive")
        self._capacity = capacity
        self._buffer = bytearray()
        self._truncated = False

    def append(self, data: bytes) -> None:
        self._buffer.extend(data)
        overflow = len(self._buffer) - self._capacity
        if overflow > 0:
            del self._buffer[:overflow]
            self._truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._buffer)

    def __len__(self) -> int:
        return len(self._buffer)

    @property
    def truncated(self) -> bool:
        return self._truncated


class AttachmentClosed(ConnectionError):
    """An attachment was revoked before its initial replay completed."""

    def __init__(self, message: str, *, code: int = WS_CLOSE_SLOW_CLIENT) -> None:
        super().__init__(message)
        self.code = code
        self.reason = _CLOSE_REASONS[code]


class PtyAttachment:
    """A single controller lease; input is accepted only while it is current."""

    def __init__(
        self,
        ws: Any,
        generation: int,
        *,
        input_queue_chunks: int,
    ) -> None:
        self.ws = ws
        self.generation = generation
        self.active = True
        self.close_code: int | None = None
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        # Includes the in-flight send, not just chunks waiting in the queue.
        self.pending_bytes = 0
        self.replayed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.sender: asyncio.Task[None] | None = None
        self.input_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=input_queue_chunks
        )
        # Counts queued plus currently-writing bytes. Revocation drains queued
        # chunks while the generation cancellation flag terminates in-flight
        # bridge work at its next bounded poll.
        self.input_pending_bytes = 0
        self.input_cancelled = threading.Event()
        self.input_active = True


async def _settle(task: asyncio.Future[Any]) -> Any:
    """Finish ownership cleanup even if the caller receives another cancel."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class PtySession:
    def __init__(
        self,
        key: str,
        bridge: Any,
        *,
        buffer_cap: int,
        read_timeout: float,
        sender_buffer_cap: int | None = None,
        send_timeout: float = 5.0,
        input_buffer_cap: int = DEFAULT_INPUT_BUFFER_CAP,
        input_chunk_cap: int = DEFAULT_INPUT_CHUNK_CAP,
        input_queue_chunks: int = DEFAULT_INPUT_QUEUE_CHUNKS,
        input_write_timeout: float = DEFAULT_INPUT_WRITE_TIMEOUT,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.key = key
        self.id = secrets.token_urlsafe(18)
        self.bridge = bridge
        self.metadata = copy.deepcopy(dict(metadata or {}))
        self.buffer = RingBuffer(buffer_cap)
        self.alive = True
        self.created_at = time.time()
        self.last_attached_at: float | None = None
        self.last_detached_at: float | None = None
        self._read_timeout = read_timeout
        self._sender_buffer_cap = (
            sender_buffer_cap if sender_buffer_cap is not None else 2 * buffer_cap
        )
        if (
            self._sender_buffer_cap < buffer_cap
            or send_timeout <= 0
            or input_buffer_cap <= 0
            or input_chunk_cap <= 0
            or input_queue_chunks <= 0
            or input_write_timeout <= 0
        ):
            raise ValueError("sender must fit the replay and have a positive timeout")
        self._send_timeout = send_timeout
        self._input_buffer_cap = input_buffer_cap
        self._input_chunk_cap = min(input_chunk_cap, input_buffer_cap)
        self._input_queue_chunks = input_queue_chunks
        self._input_write_timeout = input_write_timeout
        # Revocation and the async bridge writer run on the event-loop thread.
        # The bridge performs only O_NONBLOCK syscalls between awaits, so a
        # generation transition cannot interleave between its final ownership
        # check and one kernel write.
        self._input_ownership_lock = threading.Lock()
        self._input_ready = asyncio.Event()
        self._input_task: asyncio.Task[None] | None = None
        # Normally detached generations keep already-accepted input ahead of a
        # reconnecting controller. Aggregate byte/chunk counters bound the
        # whole FIFO, not each short-lived WebSocket generation independently.
        self._input_attachments: deque[PtyAttachment] = deque()
        self._input_pending_bytes = 0
        self._input_pending_chunks = 0
        self._attachment: PtyAttachment | None = None
        self._generation = 0
        self._drain_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._transport_tasks: set[asyncio.Task[None]] = set()

    @property
    def attached(self) -> bool:
        return self._attachment is not None and self._attachment.active

    def snapshot(self) -> dict[str, Any]:
        """Return an independent management record, never the attachment key."""
        return {
            "id": self.id,
            "alive": self.alive,
            "attached": self.attached,
            "created_at": self.created_at,
            "last_attached_at": self.last_attached_at,
            "last_detached_at": self.last_detached_at,
            "buffer_bytes": len(self.buffer),
            "buffer_truncated": self.buffer.truncated,
            "metadata": copy.deepcopy(self.metadata),
        }

    async def start(self) -> None:
        if self._drain_task is None and self.alive:
            self._drain_task = asyncio.create_task(self._drain())
        self._ensure_input_writer()

    def _ensure_input_writer(self) -> None:
        if self._input_task is None and self.alive:
            self._input_task = asyncio.create_task(self._write_input())

    def _track_transport(self, task: asyncio.Task[None]) -> None:
        self._transport_tasks.add(task)
        task.add_done_callback(self._transport_tasks.discard)

    async def _close_socket(self, ws: Any, code: int) -> None:
        try:
            await asyncio.wait_for(
                ws.close(code=code, reason=_CLOSE_REASONS[code]),
                min(self._send_timeout, 1.0),
            )
        except Exception:
            # Failed/closed browser transports do not affect the PTY.
            pass

    def _revoke(
        self,
        attachment: PtyAttachment,
        *,
        code: int | None,
        preserve_input: bool = False,
        preserve_browser: bool = False,
    ) -> None:
        with self._input_ownership_lock:
            revoke_browser = attachment.active and not preserve_browser
            cancel_input = attachment.input_active and not preserve_input
            if not revoke_browser and not cancel_input:
                return
            if revoke_browser:
                attachment.active = False
                attachment.close_code = code
                if self._attachment is attachment:
                    self._attachment = None
                    self.last_detached_at = time.time()
            if cancel_input:
                attachment.input_active = False
                attachment.input_cancelled.set()
                try:
                    self._input_attachments.remove(attachment)
                except ValueError:
                    pass
        if revoke_browser:
            if not attachment.replayed.done():
                attachment.replayed.set_result(False)
            if (
                attachment.sender is not None
                and attachment.sender is not asyncio.current_task()
            ):
                attachment.sender.cancel()
            while not attachment.queue.empty():
                attachment.queue.get_nowait()
            attachment.pending_bytes = 0
        if cancel_input:
            while not attachment.input_queue.empty():
                chunk = attachment.input_queue.get_nowait()
                attachment.input_pending_bytes -= len(chunk)
                self._input_pending_bytes -= len(chunk)
                self._input_pending_chunks -= 1
        self._input_ready.set()
        if revoke_browser and code is not None:
            self._track_transport(asyncio.create_task(self._close_socket(attachment.ws, code)))

    def _enqueue(self, attachment: PtyAttachment, chunk: bytes) -> None:
        if not attachment.active:
            return
        if (
            attachment.pending_bytes + len(chunk) > self._sender_buffer_cap
            or attachment.queue.full()
        ):
            self._revoke(attachment, code=WS_CLOSE_SLOW_CLIENT)
            return
        attachment.pending_bytes += len(chunk)
        attachment.queue.put_nowait(chunk)

    async def _send(self, attachment: PtyAttachment) -> None:
        try:
            while attachment.active:
                chunk = await attachment.queue.get()
                if chunk is None:
                    self._revoke(attachment, code=WS_CLOSE_PROCESS_EXITED)
                    return
                await asyncio.wait_for(
                    attachment.ws.send_bytes(chunk), self._send_timeout
                )
                attachment.pending_bytes -= len(chunk)
                if not attachment.replayed.done():
                    attachment.replayed.set_result(True)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.debug("PTY attachment send failed session=%s", self.id, exc_info=True)
        finally:
            # A normal receive-side FIN cancels this sender after browser
            # ownership is cleared. Do not let the sender's cancellation erase
            # input that detach deliberately preserved for ordered delivery.
            preserve_input = not attachment.active and attachment.close_code is None
            self._revoke(
                attachment,
                code=WS_CLOSE_SLOW_CLIENT if self.alive else WS_CLOSE_PROCESS_EXITED,
                preserve_input=preserve_input,
            )

    async def _drain(self) -> None:
        pending: asyncio.Task[Any] | None = None
        try:
            while self.alive:
                pending = asyncio.create_task(
                    asyncio.to_thread(self.bridge.read, self._read_timeout)
                )
                chunk = await asyncio.shield(pending)
                pending = None
                if chunk is None:
                    break
                if chunk:
                    self.buffer.append(chunk)
                    if self._attachment is not None:
                        self._enqueue(self._attachment, chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("PTY read failed session=%s", self.id)
        finally:
            self.alive = False
            # Cancelling an executor await does not cancel its OS read. Wait
            # before closing/reusing the fd, avoiding a read/close race.
            if pending is not None:
                try:
                    await _settle(pending)
                except Exception:
                    pass
            self._begin_close(flush_output=True)

    async def _write_input(self) -> None:
        """Deliver accepted browser input in order without blocking the loop."""
        while self.alive:
            await self._input_ready.wait()
            self._input_ready.clear()
            while self.alive:
                attachment = (
                    self._input_attachments[0]
                    if self._input_attachments
                    else None
                )
                if attachment is None or not self._owns_input(attachment):
                    break
                try:
                    chunk = attachment.input_queue.get_nowait()
                except asyncio.QueueEmpty:
                    if not attachment.active:
                        with self._input_ownership_lock:
                            if (
                                self._input_attachments
                                and self._input_attachments[0] is attachment
                                and attachment.input_pending_bytes == 0
                            ):
                                attachment.input_active = False
                                self._input_attachments.popleft()
                        continue
                    break
                if not self._owns_input(attachment):
                    attachment.input_pending_bytes -= len(chunk)
                    self._input_pending_bytes -= len(chunk)
                    self._input_pending_chunks -= 1
                    continue
                try:
                    wrote = await self.bridge.write(
                        chunk,
                        cancelled=attachment.input_cancelled.is_set,
                        timeout=self._input_write_timeout,
                    )
                except Exception:
                    _log.exception("PTY input write failed session=%s", self.id)
                    wrote = False
                finally:
                    attachment.input_pending_bytes -= len(chunk)
                    self._input_pending_bytes -= len(chunk)
                    self._input_pending_chunks -= 1
                if not wrote:
                    if self._owns_input(attachment):
                        self._revoke(attachment, code=WS_CLOSE_SLOW_CLIENT)
                    continue

    async def attach(self, ws: Any, *, force_redraw: bool = False) -> PtyAttachment:
        if not self.alive:
            raise AttachmentClosed("PTY process has ended", code=WS_CLOSE_PROCESS_EXITED)
        # Everything through queue installation is synchronous: concurrent
        # attaches see and revoke the last lease before awaiting any network I/O.
        # A live-controller takeover cancels that generation immediately. A
        # normal FIN has already cleared browser ownership while deliberately
        # retaining accepted input, so its FIFO entry is left to drain first.
        if self._attachment is not None:
            self._revoke(self._attachment, code=WS_CLOSE_SUPERSEDED)
        self._generation += 1
        attachment = PtyAttachment(
            ws,
            self._generation,
            input_queue_chunks=self._input_queue_chunks,
        )
        self._attachment = attachment
        self._input_attachments.append(attachment)
        self._ensure_input_writer()
        self.last_attached_at = time.time()
        self.last_detached_at = None
        snapshot = self.buffer.snapshot()
        if snapshot:
            self._enqueue(attachment, snapshot)
        else:
            attachment.replayed.set_result(True)
        attachment.sender = asyncio.create_task(self._send(attachment))
        self._track_transport(attachment.sender)
        try:
            if not await asyncio.shield(attachment.replayed) or not self._owns(attachment):
                code = attachment.close_code or (
                    WS_CLOSE_SLOW_CLIENT if self.alive else WS_CLOSE_PROCESS_EXITED
                )
                raise AttachmentClosed("PTY attachment closed during replay", code=code)
            if force_redraw:
                self.write(attachment, TUI_FORCE_REDRAW)
            return attachment
        except AttachmentClosed as exc:
            # Both the sender and the request handler may observe this failure;
            # whichever closes the socket first must report the same reason.
            self._revoke(attachment, code=exc.code)
            raise
        except BaseException:
            self._revoke(attachment, code=WS_CLOSE_SLOW_CLIENT)
            raise

    def _owns(self, attachment: PtyAttachment) -> bool:
        return self.alive and self._attachment is attachment and attachment.active

    def _owns_input(self, attachment: PtyAttachment) -> bool:
        return (
            self.alive
            and bool(self._input_attachments)
            and self._input_attachments[0] is attachment
            and attachment.input_active
            and not attachment.input_cancelled.is_set()
        )

    def write(self, attachment: PtyAttachment, data: bytes) -> bool:
        if not self._owns(attachment):
            return False
        if (
            not data
            or len(data) > self._input_chunk_cap
            or self._input_pending_bytes + len(data) > self._input_buffer_cap
            or self._input_pending_chunks >= self._input_queue_chunks
            or attachment.input_queue.full()
        ):
            if data:
                self._revoke(attachment, code=WS_CLOSE_SLOW_CLIENT)
            return not data
        attachment.input_pending_bytes += len(data)
        self._input_pending_bytes += len(data)
        self._input_pending_chunks += 1
        attachment.input_queue.put_nowait(bytes(data))
        self._input_ready.set()
        return True

    def resize(self, attachment: PtyAttachment, *, cols: int, rows: int) -> bool:
        if not self._owns(attachment):
            return False
        self.bridge.resize(cols=cols, rows=rows)
        return True

    def detach(self, attachment: Any) -> None:
        # Accept the old websocket argument for callers migrating to leases.
        current = self._attachment
        if current is not None and (attachment is current or attachment is current.ws):
            # Browser transport ownership ends immediately, but input already
            # accepted into this generation drains in order ahead of a normal
            # reconnect. Stop, failure, or an overlapping live controller
            # takeover still cancels its exact generation.
            self._revoke(
                current,
                code=None,
                preserve_input=current.input_pending_bytes > 0,
            )

    def _begin_close(self, *, flush_output: bool = False) -> asyncio.Task[None]:
        if self._close_task is None:
            self.alive = False
            if self._attachment is not None:
                if flush_output and not self._attachment.queue.full():
                    # EOF is queued behind the final bytes. The drain never
                    # waits for a browser; cleanup bounds the total flush time.
                    self._attachment.queue.put_nowait(None)
                else:
                    self._revoke(self._attachment, code=WS_CLOSE_PROCESS_EXITED)
            for input_attachment in tuple(self._input_attachments):
                self._revoke(
                    input_attachment,
                    code=WS_CLOSE_PROCESS_EXITED,
                    preserve_browser=(
                        flush_output and input_attachment is self._attachment
                    ),
                )
            self._input_ready.set()
            self._close_task = asyncio.create_task(self._close())
        return self._close_task

    async def _close(self) -> None:
        drain = self._drain_task
        if drain is not None and not drain.done():
            drain.cancel()
            try:
                await drain
            except asyncio.CancelledError:
                pass
        input_task = self._input_task
        if input_task is not None and input_task is not asyncio.current_task():
            try:
                await asyncio.shield(input_task)
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.to_thread(self.bridge.close)
        except Exception:
            _log.exception("PTY close failed session=%s", self.id)
        attachment = self._attachment
        if attachment is not None and attachment.sender is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(attachment.sender), self._send_timeout
                )
            except (TimeoutError, asyncio.CancelledError):
                self._revoke(attachment, code=WS_CLOSE_PROCESS_EXITED)
        if self._transport_tasks:
            await asyncio.gather(*tuple(self._transport_tasks), return_exceptions=True)

    async def close(self) -> None:
        # A cancelled HTTP/WS caller waits for cleanup before cancellation is
        # propagated: returning from an explicit stop means the child is reaped.
        task = self._begin_close()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(task)
            raise


class RegistryFull(Exception):
    pass


class RegistryClosed(RuntimeError):
    """The registry has begun shutdown and cannot admit another process."""


class SessionStopped(RuntimeError):
    """This attachment identity was explicitly stopped; use a fresh identity."""


class _StopTombstones:
    """Fixed-memory Bloom filter; stopped identities never become valid again.

    One MiB and seven digest-derived probes keep false positives negligible for
    a single operator (about 2e-8 after 100,000 stops). A false positive rejects
    a fresh identity too; rotating it recovers. Unlike expiring/LRU tombstones,
    this cannot resurrect an old stopped task. No raw attachment keys are kept.
    """

    def __init__(self, size_bytes: int = 1024 * 1024) -> None:
        if size_bytes <= 0:
            raise ValueError("stop tombstone storage must be positive")
        self._bits = bytearray(size_bytes)

    def _indices(self, key: str):
        digest = hashlib.sha256(b"hermes-pty-explicit-stop\0" + key.encode()).digest()
        size = len(self._bits) * 8
        return [int.from_bytes(digest[i * 4:i * 4 + 4], "big") % size for i in range(7)]

    def add(self, key: str) -> None:
        for index in self._indices(key):
            self._bits[index // 8] |= 1 << (index % 8)

    def __contains__(self, key: str) -> bool:
        return all(self._bits[i // 8] & (1 << (i % 8)) for i in self._indices(key))


async def run_reaper(registry: "PtySessionRegistry", *, interval: float = 60.0) -> None:
    """Clean exited sessions; elapsed browser absence never ends living work."""
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.reap_idle()
        except Exception:
            _log.exception("PTY registry cleanup failed")


class PtySessionRegistry:
    def __init__(
        self,
        *,
        max_sessions: int,
        buffer_cap: int,
        read_timeout: float,
        ttl: float | None = None,
        send_timeout: float = 5.0,
    ) -> None:
        # ``ttl`` is retained only for compatibility with older construction
        # sites. Agent activity cannot be inferred from elapsed detachment.
        if max_sessions <= 0 or buffer_cap <= 0 or send_timeout <= 0:
            raise ValueError("PTY registry limits must be positive")
        self._max_sessions = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._send_timeout = send_timeout
        self._sessions: dict[str, PtySession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._stopped = _StopTombstones()

    def _retire(self, session: PtySession) -> asyncio.Task[None]:
        task = session._begin_close()
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return task

    def _remove_dead_locked(self) -> list[asyncio.Task[None]]:
        tasks = []
        for key, session in list(self._sessions.items()):
            if not session.alive and self._sessions.get(key) is session:
                del self._sessions[key]
                tasks.append(self._retire(session))
        return tasks

    async def attach_or_spawn(
        self,
        key: str,
        *,
        spawn: Callable[[], Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[PtySession, bool]:
        await self.reap_idle()
        # Holding the reservation lock through spawn serializes same-key
        # requests and accounts for in-flight processes in the capacity bound.
        async with self._lock:
            if self._closing:
                raise RegistryClosed("dashboard PTY registry is shutting down")
            # A drain can finish after reap_idle released its lock. Retire
            # that exact object now; cleanup stays tracked for close_all and
            # runs off-lock rather than delaying this creation on OS teardown.
            self._remove_dead_locked()
            existing = self._sessions.get(key)
            if existing is not None:
                return existing, False
            if key in self._stopped:
                raise SessionStopped("chat was stopped; start fresh to create another task")
            if len(self._sessions) >= self._max_sessions:
                raise RegistryFull("too many live dashboard chat sessions; stop a session first")
            spawn_task = asyncio.create_task(asyncio.to_thread(spawn))
            try:
                bridge = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                try:
                    bridge = await _settle(spawn_task)
                except Exception:
                    pass  # A failed spawn did not return an owned process.
                else:
                    await _settle(asyncio.create_task(asyncio.to_thread(bridge.close)))
                raise
            try:
                session = PtySession(
                    key,
                    bridge,
                    buffer_cap=self._buffer_cap,
                    read_timeout=self._read_timeout,
                    send_timeout=self._send_timeout,
                    metadata=metadata,
                )
                await session.start()
                self._sessions[key] = session
                return session, True
            except BaseException:
                await _settle(asyncio.create_task(asyncio.to_thread(bridge.close)))
                raise

    def detach(self, key: str, attachment: Any) -> None:
        session = self._sessions.get(key)
        if session is not None:
            session.detach(attachment)

    async def reap_idle(self, now: float | None = None) -> None:
        """Compatibility name: only exited/failed processes are now eligible."""
        async with self._lock:
            tasks = self._remove_dead_locked()
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks))

    async def snapshots(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [session.snapshot() for session in self._sessions.values()]

    async def stop(self, public_id: str) -> bool:
        async with self._lock:
            session = next((s for s in self._sessions.values() if s.id == public_id), None)
            if session is None:
                return False
            if self._sessions.get(session.key) is session:
                # Publish revocation before dropping ownership or awaiting OS
                # cleanup. Delayed reconnects (even during cancelled stop) must
                # never recreate the explicitly stopped process identity.
                self._stopped.add(session.key)
                del self._sessions[session.key]
            task = self._retire(session)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(task)
            raise
        return True

    async def close_all(self) -> None:
        async with self._lock:
            # Terminal admission transition precedes releasing the lock for
            # OS cleanup. A fresh application lifespan needs a fresh registry.
            self._closing = True
            for key, session in list(self._sessions.items()):
                if self._sessions.get(key) is session:
                    del self._sessions[key]
                    self._retire(session)
            tasks = tuple(self._cleanup_tasks)
        if tasks:
            cleanup = asyncio.gather(*tasks)
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await _settle(cleanup)
                raise
