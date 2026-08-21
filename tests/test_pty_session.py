import asyncio
import time

import pytest

from hermes_cli.pty_session import PtySession, PtySessionRegistry, RingBuffer


class FakeBridge:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.written = bytearray()
        self.closed = False

    def read(self, _timeout):
        if not self._chunks:
            time.sleep(_timeout)
            return b""
        chunk = self._chunks.pop(0)
        if not chunk:
            time.sleep(_timeout)
        return chunk

    def write(self, data):
        self.written.extend(data)

    def close(self):
        self.closed = True


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_bytes(self, data):
        self.sent.append(bytes(data))

    async def close(self, code=1000, reason=""):
        pass


def test_ring_buffer_retains_only_bounded_tail():
    buffer = RingBuffer(4)
    buffer.append(b"abcdef")
    assert buffer.snapshot() == b"cdef"
    assert buffer.truncated is True


@pytest.mark.asyncio
async def test_detached_session_keeps_running_and_replays_output():
    bridge = FakeBridge([b"before", b"", b" after"])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.01)
    await session.start()
    first = FakeWS()
    await session.attach(first)
    session.detach(first)
    deadline = asyncio.get_running_loop().time() + 1.0
    while session.buffer.snapshot() != b"before after":
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)

    try:
        second = FakeWS()
        await session.attach(second, force_redraw=True)

        assert b"".join(second.sent) == b"before after"
        assert bytes(bridge.written) == b"\x0c"
        assert bridge.closed is False
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_registry_reattaches_same_process_instead_of_spawning_again():
    registry = PtySessionRegistry(
        ttl=1800,
        max_sessions=16,
        buffer_cap=1024,
        read_timeout=0.01,
    )
    bridge = FakeBridge([b""])
    first, created_first = await registry.attach_or_spawn("token", spawn=lambda: bridge)
    second, created_second = await registry.attach_or_spawn(
        "token", spawn=lambda: FakeBridge([])
    )

    try:
        assert created_first is True
        assert created_second is False
        assert second is first
        assert second.bridge is bridge
    finally:
        await registry.close_all()
