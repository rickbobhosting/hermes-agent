"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task
always reads the PTY into a bounded :class:`RingBuffer` and forwards to the
attached socket when present. Reconnecting with the same opaque token replays
the buffer and resumes the live process.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Optional

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409
TUI_FORCE_REDRAW = b"\x0c"


class RingBuffer:
    """Keep only the most recent ``capacity`` bytes appended to the buffer."""

    def __init__(self, capacity: int) -> None:
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

    @property
    def truncated(self) -> bool:
        return self._truncated


class PtySession:
    def __init__(self, key: str, bridge, *, buffer_cap: int, read_timeout: float) -> None:
        self.key = key
        self.bridge = bridge
        self.buffer = RingBuffer(buffer_cap)
        self.alive = True
        self.attached = False
        self.last_detached_at: Optional[float] = None
        self._read_timeout = read_timeout
        self._ws = None
        self._drain_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, self.bridge.read, self._read_timeout)
            if chunk is None:
                self.alive = False
                ws = self._ws
                if ws is not None:
                    try:
                        await ws.close(code=WS_CLOSE_PROCESS_EXITED)
                    except Exception:
                        pass
                return
            if not chunk:
                await asyncio.sleep(0)
                continue
            self.buffer.append(chunk)
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send_bytes(chunk)
                except Exception:
                    pass

    async def attach(self, ws, *, force_redraw: bool = False) -> None:
        """Attach a browser terminal and replay buffered PTY output."""
        old = self._ws
        if old is not None and old is not ws:
            try:
                await old.close(code=WS_CLOSE_SUPERSEDED)
            except Exception:
                pass
        self._ws = ws
        self.attached = True
        self.last_detached_at = None
        snapshot = self.buffer.snapshot()
        if snapshot:
            await ws.send_bytes(snapshot)
        if force_redraw:
            # The TUI uses alternate-screen differential rendering. A fresh
            # xterm needs one complete redraw after replaying an arbitrary tail.
            self.bridge.write(TUI_FORCE_REDRAW)

    def detach(self, ws) -> None:
        # A superseded socket also reaches its finally block; only the current
        # socket may mark the live session detached.
        if self._ws is not ws:
            return
        self._ws = None
        self.attached = False
        self.last_detached_at = time.monotonic()

    async def close(self) -> None:
        self.alive = False
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            # bridge.close() joins the child; keep it off the event loop.
            await asyncio.to_thread(self.bridge.close)
        except Exception:
            pass


class RegistryFull(Exception):
    pass


async def run_reaper(registry: "PtySessionRegistry", *, interval: float = 60.0) -> None:
    """Periodically reap idle/dead keep-alive sessions until cancelled."""
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.reap_idle()
        except Exception:
            pass


class PtySessionRegistry:
    def __init__(
        self,
        *,
        ttl: float,
        max_sessions: int,
        buffer_cap: int,
        read_timeout: float,
    ) -> None:
        self._ttl = ttl
        self._max_sessions = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._sessions: dict[str, PtySession] = {}
        self._lock = asyncio.Lock()

    async def attach_or_spawn(
        self, key: str, *, spawn: Callable[[], object]
    ) -> tuple[PtySession, bool]:
        # Serialize same-token reconnects so concurrent tabs cannot spawn two
        # PTYs and orphan the loser before it reaches the registry.
        async with self._lock:
            await self.reap_idle()
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                return existing, False
            if existing is not None:
                await existing.close()
                self._sessions.pop(key, None)
            if len(self._sessions) >= self._max_sessions:
                self._reap_one_idle_or_raise()
            bridge = await asyncio.to_thread(spawn)
            session = PtySession(
                key,
                bridge,
                buffer_cap=self._buffer_cap,
                read_timeout=self._read_timeout,
            )
            await session.start()
            self._sessions[key] = session
            return session, True

    def detach(self, key: str, ws) -> None:
        session = self._sessions.get(key)
        if session is not None:
            session.detach(ws)

    async def reap_idle(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        doomed = [
            key
            for key, session in self._sessions.items()
            if (not session.alive)
            or (
                not session.attached
                and session.last_detached_at is not None
                and (now - session.last_detached_at) > self._ttl
            )
        ]
        for key in doomed:
            await self._sessions.pop(key).close()

    def _reap_one_idle_or_raise(self) -> None:
        idle = [
            session
            for session in self._sessions.values()
            if not session.attached and session.last_detached_at is not None
        ]
        if not idle:
            raise RegistryFull("too many live dashboard chat sessions")
        oldest = min(idle, key=lambda session: session.last_detached_at or 0.0)
        self._sessions.pop(oldest.key, None)
        asyncio.create_task(oldest.close())

    async def close_all(self) -> None:
        for key in list(self._sessions):
            await self._sessions.pop(key).close()
