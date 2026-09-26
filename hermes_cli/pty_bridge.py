"""PTY bridge for `hermes dashboard` chat tab.

Wraps a child process behind a pseudo-terminal so its ANSI output can be streamed to xterm.js and
keystrokes fed back in; the only caller is the ``/api/pty`` WebSocket endpoint in
``hermes_cli.web_server``. POSIX-only: depends on ``fcntl``, ``termios`` and ``ptyprocess`` (native
Windows would need a separate ConPTY/``pywinpty`` implementation).
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import select
import signal
import struct
import sys
import termios
import threading
import time
from typing import Callable, Optional, Sequence

try:
    import ptyprocess  # type: ignore
    _PTY_AVAILABLE = not sys.platform.startswith("win")
except ImportError:  # pragma: no cover - dev env without ptyprocess
    ptyprocess = None  # type: ignore
    _PTY_AVAILABLE = False


__all__ = ["PTY_HOST_DASHBOARD", "PTY_HOST_ENV", "PtyBridge", "PtyUnavailableError"]

# Set on the spawned TUI so Ink knows which emulator is hosting it. Mirrored in
# ui-tui/packages/hermes-ink/src/ink/termio/host.ts — keep the two in sync.
PTY_HOST_ENV = "HERMES_PTY_HOST"
PTY_HOST_DASHBOARD = "dashboard"


# ``struct winsize`` packs rows/cols as unsigned short; we clamp well below that ceiling because a
# value above it is a broken probe (WSL2 reports columns=131072), not a genuine ultrawide. Lower
# bound is 1 — a zero/negative dimension is the classic "no size yet" signal.
_MIN_DIMENSION = 1
_MAX_COLS = 2000
_MAX_ROWS = 1000
_TERMINATION_GRACE_SECONDS = 0.5


def _clamp_dimension(value: int, maximum: int) -> int:
    """Clamp into ``[_MIN_DIMENSION, maximum]``; non-integer / non-finite values fall back to
    ``_MIN_DIMENSION`` so a bad probe can never reach ``struct.pack``.
    """
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return _MIN_DIMENSION
    return max(_MIN_DIMENSION, min(n, maximum))


class PtyUnavailableError(RuntimeError):
    """PTY cannot be created here (native Windows, or ``ptyprocess`` missing); the dashboard
    surfaces the message as a chat-tab banner.
    """


class PtyBridge:
    """Thin wrapper around ``ptyprocess.PtyProcess`` for byte streaming. Not thread-safe: owned by
    the WebSocket handler that spawned it; reads run in an executor thread, writes are awaited on
    the loop. The master fd is non-blocking so input backpressure suspends only the owning
    WebSocket task, never the dashboard event loop.
    """

    def __init__(self, proc: "ptyprocess.PtyProcess"):  # type: ignore[name-defined]
        self._proc = proc
        self._fd: int = proc.fd
        self._closed = False
        os.set_blocking(self._fd, False)
        # forkpty makes the child a process-group leader. Remember the group
        # before the leader exits so shutdown can still reap helper children.
        try:
            pgid = os.getpgid(proc.pid)
            own_pgid = os.getpgrp()
            self._pgid = pgid if pgid > 0 and pgid != own_pgid else None
        except OSError:
            self._pgid = None

    @classmethod
    def is_available(cls) -> bool:
        """True if a PTY can be spawned on this platform."""
        return bool(_PTY_AVAILABLE)

    @classmethod
    def spawn(
        cls, argv: Sequence[str], *, cwd: Optional[str] = None, env: Optional[dict] = None, cols: int = 80, rows: int = 24
    ) -> "PtyBridge":
        """Spawn ``argv`` behind a new PTY and return a bridge."""
        if not _PTY_AVAILABLE:
            if sys.platform.startswith("win"):
                raise PtyUnavailableError("Pseudo-terminals are unavailable on this platform. "
                                          "Hermes Agent supports Windows only via WSL.")
            raise PtyUnavailableError("The `ptyprocess` package is missing. "  # only other way _PTY_AVAILABLE is False
                                      "Install with: pip install ptyprocess (or pip install -e '.[pty]').")
        # env=None: callers own env policy (process_registry already sanitizes), so inherit via the
        # factory with exact preservation. Backfill TERM when missing/blank — CI often lacks it and
        # probes like `tput cols` then fail before winsize reads; explicit overrides are kept.
        from tools.environments.local import build_subprocess_env
        spawn_env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=False) if env is None else env.copy()
        if not spawn_env.get("TERM"):
            spawn_env["TERM"] = "xterm-256color"
        # Tell the child TUI it is hosted by the dashboard's xterm.js. Ink uses this to skip the
        # focus-in erase+repaint it does for native emulators that coalesce hidden-tab output
        # (xterm.js never drops frames, so under the dashboard that repaint was a visible flash on
        # every OS app-switch).
        spawn_env[PTY_HOST_ENV] = PTY_HOST_DASHBOARD
        proc = ptyprocess.PtyProcess.spawn(list(argv), cwd=cwd, env=spawn_env, dimensions=(rows, cols))  # type: ignore[union-attr]
        return cls(proc)

    @property
    def pid(self) -> int:
        return int(self._proc.pid)

    def is_alive(self) -> bool:
        try:
            return not self._closed and bool(self._proc.isalive())
        except Exception:
            return False

    def read(self, timeout: float = 0.2) -> Optional[bytes]:
        """Read up to 64 KiB from the PTY master, blocking at most ``timeout`` seconds.

        ``b""`` = nothing yet; ``None`` = EOF / closed (also after :meth:`close`).
        """
        if self._closed:
            return None
        try:
            readable, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not readable:
            return b""
        try:
            data = os.read(self._fd, 65536)
        except OSError as exc:
            # EIO on Linux = slave side closed.  EBADF = already closed.
            if exc.errno in {errno.EIO, errno.EBADF}:
                return None
            # The fd is deliberately non-blocking. Readiness can disappear
            # between select() and os.read() when close/output races occur.
            if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return b""
            raise
        return data or None

    async def _wait_writable(self, timeout: float) -> bool:
        """Wait without blocking the event loop until the master accepts input."""
        if self._closed or timeout <= 0:
            return False
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def _mark_ready() -> None:
            if not ready.done():
                ready.set_result(None)

        try:
            loop.add_writer(self._fd, _mark_ready)
            await asyncio.wait_for(ready, timeout=timeout)
            return not self._closed
        except (asyncio.TimeoutError, OSError, ValueError):
            return False
        finally:
            try:
                loop.remove_writer(self._fd)
            except (OSError, ValueError):
                pass

    async def write(self, data: bytes, *, timeout: float = 10.0) -> bool:
        """Write all raw bytes without ever blocking the dashboard event loop.

        Returns ``False`` when the bridge closes or the child leaves its input
        buffer full for ``timeout`` seconds. Callers can then recycle only the
        affected terminal session while the rest of the dashboard stays live.
        """
        if self._closed:
            return False
        if not data:
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        view = memoryview(data)
        while view:
            if self._closed:
                return False
            try:
                n = os.write(self._fd, view)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF, errno.EPIPE}:
                    return False
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    n = 0
                else:
                    raise
            if n > 0:
                view = view[n:]
                # A very large paste can otherwise monopolize the loop while
                # the child drains quickly enough to keep the fd writable.
                if view:
                    await asyncio.sleep(0)
                continue

            remaining = deadline - loop.time()
            if not await self._wait_writable(remaining):
                return False
        return True

    def write_owned(
        self,
        data: bytes,
        *,
        cancelled: Optional[Callable[[], bool]] = None,
        ownership_lock: Optional[threading.Lock] = None,
        timeout: float = 1.0,
    ) -> bool:
        """Bound a retained attachment's input and fence revoked controllers.

        The lock covers only each nonblocking write syscall. A takeover can
        cancel a generation while this worker waits for PTY capacity.
        """
        if self._closed or not data:
            return not data
        if timeout <= 0:
            return False
        deadline = time.monotonic() + timeout
        view = memoryview(data)
        while view:
            if self._closed or (cancelled is not None and cancelled()):
                return False
            if ownership_lock is not None:
                ownership_lock.acquire()
            try:
                if self._closed or (cancelled is not None and cancelled()):
                    return False
                try:
                    n = os.write(self._fd, view)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF, errno.EPIPE}:
                        return False
                    if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                        raise
                    n = 0
            finally:
                if ownership_lock is not None:
                    ownership_lock.release()
            if n > 0:
                view = view[n:]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                select.select([], [self._fd], [], min(0.05, remaining))
            except (OSError, ValueError):
                return False
        return True

    def resize(self, cols: int, rows: int) -> None:
        """Forward a terminal resize to the child via ``TIOCSWINSZ``.

        Clamped first: WSL2 via xterm.js reports garbage like ``columns=131072, rows=1`` and an
        unclamped unsigned-short pack raises ``struct.error`` (not ``OSError``), leaving the TUI
        laid out for a one-row screen — the blank/disappearing-text symptom.
        """
        if self._closed:
            return
        # struct winsize: rows, cols, xpixel, ypixel (all unsigned short)
        winsize = struct.pack("HHHH", _clamp_dimension(rows, _MAX_ROWS), _clamp_dimension(cols, _MAX_COLS), 0, 0)
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def close(self) -> None:
        """Terminate the child process group and close its file descriptors.

        Idempotent. Reaping the child is important so we don't leak zombies
        across the lifetime of the dashboard process. Group liveness is
        checked independently of the PTY leader because the leader can exit
        before gateway/helper descendants do.
        """
        if self._closed:
            return
        self._closed = True

        pgid = getattr(self, "_pgid", None)
        if pgid is None:
            try:
                pgid = os.getpgid(self._proc.pid)  # windows-footgun: ok -- POSIX-only module
            except Exception:
                pgid = None
        try:
            if pgid == os.getpgrp():
                pgid = None
        except OSError:
            pgid = None

        def _reap_leader_status() -> bool:
            try:
                return bool(self._proc.isalive())
            except Exception:
                return False

        def _group_is_alive() -> bool:
            if pgid is None:
                return False
            try:
                os.killpg(pgid, 0)  # windows-footgun: ok -- POSIX-only module
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError as exc:
                return exc.errno != errno.ESRCH
            return True

        # SIGHUP is the conventional "your terminal went away" signal.
        # Send it to the whole foreground process group, not just the PTY
        # leader: the dashboard TUI starts helper children such as the Python
        # slash worker, and killing only the leader can strand those helpers.
        if pgid is not None:
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):  # windows-footgun: ok -- POSIX-only module
                _reap_leader_status()
                try:
                    os.killpg(pgid, sig)  # windows-footgun: ok -- POSIX-only module
                except ProcessLookupError:
                    break
                except Exception:
                    pass
                deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
                while time.monotonic() < deadline:
                    _reap_leader_status()
                    if not _group_is_alive():
                        break
                    time.sleep(0.02)
                if not _group_is_alive():
                    break
        else:
            # Defensive fallback for an unexpected PtyProcess implementation
            # that does not create a distinct process group. This also avoids
            # signaling the dashboard's own group if a fake/misconfigured proc
            # reports the parent's PGID.
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                if not _reap_leader_status():
                    break
                try:
                    self._proc.kill(sig)
                except Exception:
                    pass
                deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
                while _reap_leader_status() and time.monotonic() < deadline:
                    time.sleep(0.02)

        try:
            self._proc.close(force=True)
        except Exception:
            pass

    def __enter__(self) -> "PtyBridge":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
