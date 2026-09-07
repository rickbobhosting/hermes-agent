import asyncio
import threading
import time

import pytest

from hermes_cli.pty_session import (
    AttachmentClosed,
    DEFAULT_INPUT_CHUNK_CAP,
    PtySession,
    PtySessionRegistry,
    RegistryClosed,
    RegistryFull,
    RingBuffer,
    SessionStopped,
    WS_CLOSE_PROCESS_EXITED,
    WS_CLOSE_SLOW_CLIENT,
    WS_CLOSE_SUPERSEDED,
    _StopTombstones,
)


class FakeBridge:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.written = bytearray()
        self.closed = False
        self.close_count = 0
        self.resizes = []

    def read(self, _timeout):
        if not self._chunks:
            time.sleep(_timeout)
            return b""
        chunk = self._chunks.pop(0)
        if not chunk:
            time.sleep(_timeout)
        return chunk

    async def write(
        self,
        data,
        *,
        cancelled=None,
        timeout=1.0,
    ):
        if timeout <= 0 or (cancelled is not None and cancelled()):
            return False
        if cancelled is not None and cancelled():
            return False
        self.written.extend(data)
        return True

    def close(self):
        self.closed = True
        self.close_count += 1

    def resize(self, *, cols, rows):
        self.resizes.append((cols, rows))


class FakeWS:
    def __init__(self):
        self.sent = []
        self.close_codes = []

    async def send_bytes(self, data):
        self.sent.append(bytes(data))

    async def close(self, code=1000, reason=""):
        self.close_codes.append(code)


class BlockedWS(FakeWS):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send_bytes(self, data):
        self.entered.set()
        await self.release.wait()
        await super().send_bytes(data)


class BlockedInputBridge(FakeBridge):
    """Models a full PTY while honoring generation cancellation."""

    def __init__(self):
        super().__init__([])
        self.write_entered = asyncio.Event()
        self.write_release = asyncio.Event()

    async def write(
        self,
        data,
        *,
        cancelled=None,
        timeout=1.0,
    ):
        self.write_entered.set()
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.write_release.is_set():
            if cancelled is not None and cancelled():
                return False
            if asyncio.get_running_loop().time() >= deadline:
                return False
            try:
                await asyncio.wait_for(self.write_release.wait(), timeout=0.01)
            except asyncio.TimeoutError:
                pass
        if cancelled is not None and cancelled():
            return False
        self.written.extend(data)
        return True


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def make_registry(**kwargs):
    return PtySessionRegistry(
        max_sessions=kwargs.pop("max_sessions", 16),
        buffer_cap=1024,
        read_timeout=0.005,
        **kwargs,
    )


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
        await until(lambda: bytes(bridge.written) == b"\x0c")
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


@pytest.mark.asyncio
async def test_blocked_sender_never_blocks_detached_or_reattached_output_drain():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    ws = BlockedWS()
    lease = await session.attach(ws)
    await session.start()
    try:
        bridge._chunks.append(b"first")
        await asyncio.wait_for(ws.entered.wait(), 1)
        bridge._chunks.append(b"second")
        await until(lambda: session.buffer.snapshot() == b"firstsecond")
        session.detach(lease)
        bridge._chunks.append(b"third")
        await until(lambda: session.buffer.snapshot() == b"firstsecondthird")
        resumed = FakeWS()
        await session.attach(resumed)
        assert b"".join(resumed.sent) == b"firstsecondthird"
        assert not bridge.closed
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_replay_precedes_live_output_while_replay_send_is_blocked():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    session.buffer.append(b"replay")
    ws = BlockedWS()
    attach = asyncio.create_task(session.attach(ws))
    await asyncio.wait_for(ws.entered.wait(), 1)
    await session.start()
    try:
        bridge._chunks.append(b"live")
        await until(lambda: session.buffer.snapshot() == b"replaylive")
        assert not attach.done()
        ws.release.set()
        await attach
        await until(lambda: len(ws.sent) == 2)
        assert ws.sent == [b"replay", b"live"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_failed_initial_replay_detaches_without_stopping_process():
    class BrokenWS(FakeWS):
        async def send_bytes(self, data):
            raise OSError("browser disconnected")

    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    session.buffer.append(b"replay")
    try:
        with pytest.raises(AttachmentClosed) as failed:
            await session.attach(BrokenWS())
        assert failed.value.code == WS_CLOSE_SLOW_CLIENT
        assert failed.value.reason == "pty_attachment_interrupted"
        assert not session.attached
        assert session.last_detached_at is not None
        assert session.alive and not bridge.closed
        good = FakeWS()
        await session.attach(good)
        assert good.sent == [b"replay"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_overlapping_attaches_have_one_controller_and_stale_input_is_rejected():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    old = FakeWS()
    old_lease = await session.attach(old)
    session.buffer.append(b"replay")
    blocked = BlockedWS()
    intermediate = asyncio.create_task(session.attach(blocked))
    await asyncio.wait_for(blocked.entered.wait(), 1)
    newest = FakeWS()
    try:
        lease = await session.attach(newest)
        with pytest.raises(AttachmentClosed) as superseded:
            await intermediate
        assert superseded.value.code == WS_CLOSE_SUPERSEDED
        assert superseded.value.reason == "pty_superseded"
        session.detach(old_lease)
        assert session.attached
        assert not session.write(old_lease, b"stale")
        assert not session.resize(old_lease, cols=1, rows=1)
        assert session.write(lease, b"winner")
        assert session.resize(lease, cols=80, rows=24)
        await until(lambda: bridge.written == b"winner")
        assert bridge.written == b"winner"
        assert bridge.resizes == [(80, 24)]
        await until(lambda: bool(blocked.close_codes))
        assert old.close_codes == [WS_CLOSE_SUPERSEDED]
        assert blocked.close_codes == [WS_CLOSE_SUPERSEDED]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_blocked_input_keeps_event_loop_and_explicit_stop_responsive():
    registry = make_registry()
    bridge = BlockedInputBridge()
    session, _created = await registry.attach_or_spawn(
        "blocked-input",
        spawn=lambda: bridge,
    )
    ws = FakeWS()
    lease = await session.attach(ws)
    assert session.write(lease, b"blocked")
    await until(bridge.write_entered.is_set)

    timer_fired = asyncio.Event()
    asyncio.get_running_loop().call_later(0.01, timer_fired.set)
    await asyncio.wait_for(timer_fired.wait(), 0.2)
    await asyncio.wait_for(registry.stop(session.id), 0.2)

    assert bridge.closed
    assert bridge.written == b""


@pytest.mark.asyncio
async def test_supersede_cancels_queued_and_inflight_old_generation_input():
    bridge = BlockedInputBridge()
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    old_ws = FakeWS()
    old = await session.attach(old_ws)
    try:
        assert session.write(old, b"old-inflight")
        assert session.write(old, b"old-queued")
        await until(bridge.write_entered.is_set)

        current = await session.attach(FakeWS())
        assert not session.write(old, b"old-after-supersede")
        assert session.write(current, b"new-owner")
        bridge.write_release.set()

        await until(lambda: bridge.written == b"new-owner")
        assert b"old" not in bridge.written
        await until(lambda: old_ws.close_codes == [WS_CLOSE_SUPERSEDED])
    finally:
        bridge.write_release.set()
        await session.close()


@pytest.mark.asyncio
async def test_normal_detach_then_immediate_reattach_preserves_input_fifo():
    bridge = BlockedInputBridge()
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    old_ws = FakeWS()
    old = await session.attach(old_ws)
    try:
        assert session.write(old, b"old-enter")
        await until(bridge.write_entered.is_set)

        # A normal FIN relinquishes browser ownership but does not erase bytes
        # the server already accepted. The reconnect can attach immediately;
        # its input waits behind the detached generation.
        session.detach(old)
        current = await session.attach(FakeWS())
        assert session.write(current, b"new-input")
        assert bridge.written == b""
        assert not old.input_cancelled.is_set()

        bridge.write_release.set()
        await until(lambda: bridge.written == b"old-enternew-input")
        assert session.attached
        assert old_ws.close_codes == []
    finally:
        bridge.write_release.set()
        await session.close()


@pytest.mark.asyncio
async def test_reconnect_generations_share_one_bounded_input_budget():
    bridge = BlockedInputBridge()
    session = PtySession(
        "key",
        bridge,
        buffer_cap=1024,
        input_buffer_cap=8,
        input_chunk_cap=8,
        input_queue_chunks=2,
        read_timeout=0.005,
    )
    old = await session.attach(FakeWS())
    try:
        assert session.write(old, b"1234")
        await until(bridge.write_entered.is_set)
        assert session.write(old, b"5678")
        session.detach(old)

        ws = FakeWS()
        current = await session.attach(ws)
        assert not session.write(current, b"9")
        await until(lambda: ws.close_codes == [WS_CLOSE_SLOW_CLIENT])

        # Reconnects cannot multiply the configured queue budget. Rejection of
        # the new generation does not erase the older accepted FIFO.
        assert not old.input_cancelled.is_set()
        bridge.write_release.set()
        await until(lambda: bridge.written == b"12345678")
    finally:
        bridge.write_release.set()
        await session.close()


@pytest.mark.asyncio
async def test_input_queue_budget_revokes_attachment_without_partial_acceptance():
    bridge = BlockedInputBridge()
    session = PtySession(
        "key",
        bridge,
        buffer_cap=1024,
        input_buffer_cap=8,
        input_chunk_cap=8,
        input_write_timeout=0.2,
        read_timeout=0.005,
    )
    ws = FakeWS()
    lease = await session.attach(ws)
    try:
        assert session.write(lease, b"1234")
        await until(bridge.write_entered.is_set)
        assert session.write(lease, b"5678")
        assert not session.write(lease, b"9")
        await until(lambda: ws.close_codes == [WS_CLOSE_SLOW_CLIENT])

        # Both accepted old-generation chunks are cancelled rather than one
        # being silently truncated. A replacement attachment remains usable.
        bridge.write_release.set()
        current = await session.attach(FakeWS())
        assert session.write(current, b"ok")
        await until(lambda: bridge.written == b"ok")
    finally:
        bridge.write_release.set()
        await session.close()


@pytest.mark.asyncio
async def test_input_writer_preserves_accepted_chunk_order():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    lease = await session.attach(FakeWS())
    try:
        assert session.write(lease, b"first-")
        assert session.write(lease, b"second-")
        assert session.write(lease, b"third")
        await until(lambda: bridge.written == b"first-second-third")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_one_frame_paste_cap_is_intentional_and_rejects_before_queueing():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    first = await session.attach(FakeWS())
    try:
        paste = b"x" * DEFAULT_INPUT_CHUNK_CAP
        assert session.write(first, paste)
        await until(lambda: len(bridge.written) == len(paste))

        ws = FakeWS()
        current = await session.attach(ws)
        assert not session.write(current, b"y" * (DEFAULT_INPUT_CHUNK_CAP + 1))
        await until(lambda: ws.close_codes == [WS_CLOSE_SLOW_CLIENT])
        assert bridge.written == paste
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_sender_overflow_revokes_only_browser_and_preserves_output_tail():
    bridge = FakeBridge([])
    session = PtySession(
        "key", bridge, buffer_cap=8, sender_buffer_cap=8, read_timeout=0.005
    )
    ws = BlockedWS()
    lease = await session.attach(ws)
    await session.start()
    try:
        bridge._chunks.append(b"12345678")
        await asyncio.wait_for(ws.entered.wait(), 1)
        bridge._chunks.append(b"9")
        await until(lambda: not session.attached)
        assert not session.write(lease, b"ignored")
        assert session.alive and not bridge.closed
        assert session.buffer.snapshot() == b"23456789"
        await until(lambda: bool(ws.close_codes))
        assert ws.close_codes == [WS_CLOSE_SLOW_CLIENT]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_replay_send_timeout_detaches_and_preserves_live_session():
    session = PtySession(
        "key", FakeBridge([]), buffer_cap=1024, read_timeout=0.005, send_timeout=0.02
    )
    session.buffer.append(b"replay")
    try:
        with pytest.raises(AttachmentClosed):
            await asyncio.wait_for(session.attach(BlockedWS()), 1)
        assert session.alive and not session.attached
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_live_detached_sessions_never_expire_or_get_evicted_for_capacity():
    registry = make_registry(max_sessions=1, ttl=0.001)
    bridge = FakeBridge([])
    session, _ = await registry.attach_or_spawn("first", spawn=lambda: bridge)
    lease = await session.attach(FakeWS())
    session.detach(lease)
    try:
        await registry.reap_idle(now=time.time() + 100 * 365 * 24 * 3600)
        with pytest.raises(RegistryFull):
            await registry.attach_or_spawn("second", spawn=lambda: pytest.fail("spawned"))
        same, created = await registry.attach_or_spawn("first", spawn=lambda: pytest.fail("spawned"))
        assert same is session and not created
        assert not bridge.closed
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_dead_cleanup_does_not_close_replacement_or_reattached_live_session():
    entered = threading.Event()
    release = threading.Event()

    class SlowCloseBridge(FakeBridge):
        def close(self):
            entered.set()
            assert release.wait(2)
            super().close()

    registry = make_registry()
    dead, _ = await registry.attach_or_spawn("dead", spawn=lambda: SlowCloseBridge([]))
    alive, _ = await registry.attach_or_spawn("live", spawn=lambda: FakeBridge([]))
    lease = await alive.attach(FakeWS())
    alive.detach(lease)
    dead.alive = False
    reap = asyncio.create_task(registry.reap_idle())
    try:
        await until(entered.is_set)
        replacement, _ = await registry.attach_or_spawn("dead", spawn=lambda: FakeBridge([]))
        await alive.attach(FakeWS())
        release.set()
        await reap
        assert replacement.alive and alive.attached
        assert not replacement.bridge.closed and not alive.bridge.closed
        assert len(await registry.snapshots()) == 2
    finally:
        release.set()
        await reap
        await registry.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_failure", [False, True])
async def test_eof_or_read_failure_marks_dead_closes_bridge_and_releases_capacity(read_failure):
    class ExitedBridge(FakeBridge):
        def read(self, timeout):
            if read_failure:
                raise OSError("read failed")
            return None

    registry = make_registry(max_sessions=1)
    bridge = ExitedBridge([])
    session, _ = await registry.attach_or_spawn("old", spawn=lambda: bridge)
    try:
        await until(lambda: not session.alive)
        await registry.reap_idle()
        assert bridge.closed
        assert await registry.snapshots() == []
        replacement, _ = await registry.attach_or_spawn("new", spawn=lambda: FakeBridge([]))
        assert replacement.alive
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_cancelling_spawn_waits_for_child_and_closes_it_before_propagating():
    registry = make_registry()
    entered = threading.Event()
    release = threading.Event()
    bridge = FakeBridge([])

    def spawn():
        entered.set()
        assert release.wait(2)
        return bridge

    create = asyncio.create_task(registry.attach_or_spawn("key", spawn=spawn))
    try:
        await until(entered.is_set)
        create.cancel()
        await asyncio.sleep(0)
        assert not create.done()
        create.cancel()  # repeated cancellation cannot abandon ownership
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await create
        assert bridge.close_count == 1
        assert await registry.snapshots() == []
    finally:
        release.set()
        await registry.close_all()


@pytest.mark.asyncio
async def test_same_key_concurrent_creation_spawns_one_process():
    registry = make_registry()
    bridges = []

    def spawn():
        bridge = FakeBridge([])
        bridges.append(bridge)
        return bridge

    try:
        results = await asyncio.gather(*(registry.attach_or_spawn("key", spawn=spawn) for _ in range(8)))
        assert len(bridges) == 1
        assert sum(created for _, created in results) == 1
        assert all(session is results[0][0] for session, _ in results)
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_snapshots_are_copies_hide_attach_key_and_stop_uses_public_id():
    registry = make_registry()
    metadata = {"profile": "test", "nested": {"value": "original"}}
    session, _ = await registry.attach_or_spawn("secret-key", spawn=lambda: FakeBridge([]), metadata=metadata)
    try:
        snapshots = await registry.snapshots()
        assert "secret-key" not in str(snapshots)
        assert snapshots[0]["id"] == session.id
        snapshots[0]["metadata"]["nested"]["value"] = "changed"
        metadata["profile"] = "also changed"
        assert session.snapshot()["metadata"] == {"profile": "test", "nested": {"value": "original"}}
        assert not await registry.stop("secret-key")
        assert await registry.stop(session.id)
        assert session.bridge.closed
        assert not await registry.stop(session.id)
    finally:
        await registry.close_all()


def test_stop_tombstones_are_bounded_and_never_forget_inserted_keys():
    tombstones = _StopTombstones(size_bytes=8)
    for index in range(1000):
        tombstones.add(f"stopped-{index}")
    assert len(tombstones._bits) == 8
    assert all(f"stopped-{index}" in tombstones for index in range(1000))
    for invalid_size in (0, -1):
        with pytest.raises(ValueError):
            _StopTombstones(size_bytes=invalid_size)


@pytest.mark.asyncio
async def test_explicit_stop_rejects_delayed_reconnect_but_rotated_identity_succeeds():
    registry = make_registry()
    session, _ = await registry.attach_or_spawn("stopped", spawn=lambda: FakeBridge([]))
    try:
        assert await registry.stop(session.id)
        await registry.reap_idle(now=time.time() + 100 * 365 * 24 * 3600)
        with pytest.raises(SessionStopped):
            await registry.attach_or_spawn("stopped", spawn=lambda: pytest.fail("resurrected stopped task"))
        fresh, created = await registry.attach_or_spawn("rotated", spawn=lambda: FakeBridge([]))
        assert created and fresh.alive
        assert fresh.id != session.id
        await registry.close_all()
        with pytest.raises(RegistryClosed):
            await registry.attach_or_spawn("stopped", spawn=lambda: pytest.fail("admitted after shutdown"))
    finally:
        await registry.close_all()


@pytest.mark.asyncio
async def test_concurrent_close_is_idempotent_and_revokes_controller():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    ws = FakeWS()
    lease = await session.attach(ws)
    await session.start()
    await asyncio.gather(session.close(), session.close(), session.close())
    assert not session.write(lease, b"after-close")
    assert bridge.close_count == 1
    assert not session.alive and not session.attached
    assert ws.close_codes == [WS_CLOSE_PROCESS_EXITED]


@pytest.mark.asyncio
async def test_eof_delivers_queued_final_output_before_process_ended_close():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    ws = BlockedWS()
    await session.attach(ws)
    await session.start()
    try:
        bridge._chunks.extend([b"final output", None])
        await asyncio.wait_for(ws.entered.wait(), 1)
        await until(lambda: not session.alive)
        assert not ws.close_codes
        ws.release.set()
        await session.close()
        assert ws.sent == [b"final output"]
        assert ws.close_codes == [WS_CLOSE_PROCESS_EXITED]
        assert bridge.close_count == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_cancelled_explicit_stop_still_waits_until_process_is_closed():
    entered = threading.Event()
    release = threading.Event()

    class SlowCloseBridge(FakeBridge):
        def close(self):
            entered.set()
            assert release.wait(2)
            super().close()

    registry = make_registry()
    bridge = SlowCloseBridge([])
    session, _ = await registry.attach_or_spawn("key", spawn=lambda: bridge)
    stop = asyncio.create_task(registry.stop(session.id))
    try:
        await until(entered.is_set)
        stop.cancel()
        await asyncio.sleep(0)
        assert not stop.done()
        with pytest.raises(SessionStopped):
            await registry.attach_or_spawn("key", spawn=lambda: pytest.fail("resurrected while stopping"))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert bridge.close_count == 1
        assert await registry.snapshots() == []
        with pytest.raises(SessionStopped):
            await registry.attach_or_spawn("key", spawn=lambda: pytest.fail("resurrected after cancelled stop"))
    finally:
        release.set()
        await registry.close_all()


@pytest.mark.asyncio
async def test_cancel_during_replay_detaches_and_next_browser_can_recover():
    session = PtySession("key", FakeBridge([]), buffer_cap=1024, read_timeout=0.005)
    session.buffer.append(b"replay")
    ws = BlockedWS()
    attach = asyncio.create_task(session.attach(ws))
    try:
        await asyncio.wait_for(ws.entered.wait(), 1)
        attach.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attach
        assert not session.attached and session.alive
        recovered = FakeWS()
        lease = await session.attach(recovered)
        assert session.write(lease, b"recovered")
        assert recovered.sent == [b"replay"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_shutdown_rejects_admission_during_cleanup_and_after_completion():
    entered = threading.Event()
    release = threading.Event()

    class SlowCloseBridge(FakeBridge):
        def close(self):
            entered.set()
            assert release.wait(2)
            super().close()

    registry = make_registry()
    original, _ = await registry.attach_or_spawn("original", spawn=lambda: SlowCloseBridge([]))
    shutdown = asyncio.create_task(registry.close_all())
    try:
        await until(entered.is_set)
        for key in ("new", "original"):
            with pytest.raises(RegistryClosed):
                await registry.attach_or_spawn(key, spawn=lambda: pytest.fail("spawned during shutdown"))
        release.set()
        await shutdown
        with pytest.raises(RegistryClosed):
            await registry.attach_or_spawn("later", spawn=lambda: pytest.fail("spawned after shutdown"))
        assert original.bridge.closed
        assert await registry.snapshots() == []
    finally:
        release.set()
        await shutdown
        await registry.close_all()


@pytest.mark.asyncio
async def test_process_exit_during_replay_reports_ended_instead_of_interrupted():
    bridge = FakeBridge([])
    session = PtySession("key", bridge, buffer_cap=1024, read_timeout=0.005)
    session.buffer.append(b"replay")
    ws = BlockedWS()
    attach = asyncio.create_task(session.attach(ws))
    try:
        await asyncio.wait_for(ws.entered.wait(), 1)
        bridge._chunks.append(None)
        await session.start()
        await until(lambda: not session.alive)
        ws.release.set()
        with pytest.raises(AttachmentClosed) as ended:
            await attach
        assert ended.value.code == WS_CLOSE_PROCESS_EXITED
        assert ended.value.reason == "pty_process_ended"
        await session.close()
        assert ws.close_codes == [WS_CLOSE_PROCESS_EXITED]
        with pytest.raises(AttachmentClosed) as already_ended:
            await session.attach(FakeWS())
        assert already_ended.value.code == WS_CLOSE_PROCESS_EXITED
    finally:
        await session.close()
