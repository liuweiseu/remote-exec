"""Subprocess execution with streaming output and process-group cleanup.

This module knows nothing about gRPC. It turns a :class:`CommandSpec` into an
async stream of :class:`ExecEvent` objects, which keeps it reusable for future
APIs (e.g. Submit/GetStatus/GetResult backed by a task registry).

Process model
-------------
Every command runs as ``<shell> -c <command>`` in a *new session*
(``start_new_session=True``), so the shell and everything it spawns share a
process group whose id equals the shell's pid. Cleanup always targets the whole
group (SIGTERM, then SIGKILL after a grace period) so that tools like Vivado do
not leave orphaned children behind. This happens when:

* the command times out,
* the consumer stops iterating (client disconnect / cancellation),
* the main process exits but left background processes in its group.

A process that deliberately escapes the group (``setsid``/double fork) cannot be
tracked this way; under systemd the unit's cgroup still covers it on stop.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import signal
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

READ_CHUNK_SIZE = 64 * 1024
# Bounded so a slow consumer applies back-pressure all the way to the process
# (its pipe fills up and it blocks), instead of buffering unbounded output.
QUEUE_MAX_CHUNKS = 64
DEFAULT_KILL_GRACE = 5.0
# After the main process exited (and its group was cleaned up) wait at most
# this long for the pipes to reach EOF. Only matters if an escaped process
# still holds the pipes open.
PIPE_DRAIN_TIMEOUT = 5.0


class Stream(enum.Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


class Status(enum.Enum):
    EXITED = "exited"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class CommandSpec:
    command: str
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float | None = None


@dataclass(frozen=True)
class Started:
    execution_id: str
    pid: int
    start_time: datetime


@dataclass(frozen=True)
class Output:
    stream: Stream
    data: bytes


@dataclass(frozen=True)
class Finished:
    execution_id: str
    status: Status
    exit_code: int
    signal: int
    start_time: datetime
    end_time: datetime
    message: str = ""


ExecEvent = Started | Output | Finished


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _signal_process_group(pgid: int, sig: int) -> bool:
    """Send ``sig`` to a process group. Returns False if the group is gone."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Should not happen for our own children; treat as gone rather than crash.
        log.warning("permission denied signalling process group %d", pgid)
        return False


async def terminate_process_group(pgid: int, grace: float = DEFAULT_KILL_GRACE) -> None:
    """SIGTERM the group, wait up to ``grace`` seconds, then SIGKILL it."""
    if not _signal_process_group(pgid, signal.SIGTERM):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        if not _signal_process_group(pgid, 0):
            return
    log.info("process group %d survived SIGTERM for %.1fs, sending SIGKILL", pgid, grace)
    _signal_process_group(pgid, signal.SIGKILL)


class _ExitNotifyingProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    """Stream protocol that resolves ``exited`` as soon as the process exits.

    Before Python 3.13, ``Process.wait()`` only returns once the process has
    exited *and* all its pipes are closed. A background child that inherited
    stdout (``sleep 300 &``) would therefore block ``wait()`` until it exits,
    so we could never notice the main process exiting and clean up its group.
    """

    def __init__(self, limit: int, loop: asyncio.AbstractEventLoop):
        super().__init__(limit=limit, loop=loop)
        self.exited: asyncio.Future[int] = loop.create_future()

    def process_exited(self) -> None:
        # Read the code first: the base class may drop _transport (3.13+).
        returncode = self._transport.get_returncode()
        super().process_exited()
        if not self.exited.done():
            self.exited.set_result(returncode)


async def _spawn(program: str, *args: str, **kwds) -> tuple[asyncio.subprocess.Process, asyncio.Future[int]]:
    """Like ``asyncio.create_subprocess_exec`` but also returns an exit future."""
    loop = asyncio.get_running_loop()
    protocol_factory = lambda: _ExitNotifyingProtocol(limit=2**16, loop=loop)  # noqa: E731
    transport, protocol = await loop.subprocess_exec(protocol_factory, program, *args, **kwds)
    return asyncio.subprocess.Process(transport, protocol, loop), protocol.exited


class Executor:
    """Runs commands; tracks background cleanup tasks so shutdown can await them."""

    def __init__(self, shell: str = "/bin/sh", kill_grace: float = DEFAULT_KILL_GRACE):
        self.shell = shell
        self.kill_grace = kill_grace
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._running_pgids: set[int] = set()

    @property
    def running_count(self) -> int:
        return len(self._running_pgids)

    def build_env(self, extra: Mapping[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        env.update(extra)
        return env

    async def run(self, spec: CommandSpec, execution_id: str | None = None) -> AsyncIterator[ExecEvent]:
        """Run ``spec`` and yield events. Always ends with exactly one Finished.

        If the consumer stops early (``aclose()`` or task cancellation) the
        process group is terminated in the background.
        """
        execution_id = execution_id or uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        start_time = utcnow()

        try:
            proc, exited = await _spawn(
                self.shell,
                "-c",
                spec.command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd or None,
                env=self.build_env(spec.env),
                start_new_session=True,
            )
        except OSError as exc:
            where = f" (cwd={spec.cwd})" if spec.cwd else ""
            yield Finished(
                execution_id, Status.ERROR, -1, 0, start_time, utcnow(),
                f"failed to start command{where}: {exc.strerror or exc}",
            )
            return

        pgid = proc.pid  # session leader => pgid == pid
        self._running_pgids.add(pgid)
        queue: asyncio.Queue[tuple[Stream, bytes | None]] = asyncio.Queue(QUEUE_MAX_CHUNKS)

        async def pump(reader: asyncio.StreamReader, stream: Stream) -> None:
            try:
                while chunk := await reader.read(READ_CHUNK_SIZE):
                    await queue.put((stream, chunk))
            finally:
                # put_nowait could fail on a full queue; the consumer is gone
                # in that case anyway (we are being cancelled).
                try:
                    queue.put_nowait((stream, None))
                except asyncio.QueueFull:
                    pass

        async def wait_and_reap() -> int:
            # Not proc.wait(): before 3.13 it also waits for the pipes to close.
            rc = await exited
            # Kill whatever the command left behind in its process group.
            await terminate_process_group(pgid, self.kill_grace)
            return rc

        timed_out = False
        killer: asyncio.Task | None = None

        def on_timeout() -> None:
            nonlocal timed_out, killer
            timed_out = True
            log.info("execution %s timed out after %ss, terminating pgid %d", execution_id, spec.timeout, pgid)
            killer = asyncio.ensure_future(terminate_process_group(pgid, self.kill_grace))

        pumps = [
            asyncio.ensure_future(pump(proc.stdout, Stream.STDOUT)),
            asyncio.ensure_future(pump(proc.stderr, Stream.STDERR)),
        ]
        waiter = asyncio.ensure_future(wait_and_reap())
        timer = loop.call_later(spec.timeout, on_timeout) if spec.timeout else None
        get_task: asyncio.Future | None = None
        finished = False

        try:
            yield Started(execution_id, proc.pid, start_time)

            open_streams = 2
            drain_deadline: float | None = None
            while open_streams:
                wait_timeout = None
                if waiter.done():
                    if drain_deadline is None:
                        drain_deadline = loop.time() + PIPE_DRAIN_TIMEOUT
                    wait_timeout = drain_deadline - loop.time()
                    if wait_timeout <= 0:
                        log.warning("execution %s: pipes still open after process exit; giving up", execution_id)
                        break
                if get_task is None:
                    get_task = asyncio.ensure_future(queue.get())
                waitables = {get_task} if waiter.done() else {get_task, waiter}
                done, _ = await asyncio.wait(waitables, timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED)
                if get_task in done:
                    stream, data = get_task.result()
                    get_task = None
                    if data is None:
                        open_streams -= 1
                    else:
                        yield Output(stream, data)

            returncode = await waiter
            if timer:
                timer.cancel()
            if killer:
                await killer
            sig = -returncode if returncode < 0 else 0
            exit_code = 128 + sig if sig else returncode
            if timed_out:
                status, message = Status.TIMEOUT, f"command timed out after {spec.timeout:g}s"
            else:
                status, message = Status.EXITED, ""
            finished = True
            yield Finished(execution_id, status, exit_code, sig, start_time, utcnow(), message)
        finally:
            if timer:
                timer.cancel()
            if get_task is not None:
                get_task.cancel()
            for task in pumps:
                task.cancel()
            if not finished:
                # Consumer went away (client disconnect / cancel). Must not
                # await here: we may be running inside a cancelled task.
                log.info("execution %s aborted, terminating pgid %d", execution_id, pgid)
                waiter.cancel()
                _signal_process_group(pgid, signal.SIGTERM)
                self._spawn_cleanup(proc, pgid)
            self._running_pgids.discard(pgid)

    def _spawn_cleanup(self, proc: asyncio.subprocess.Process, pgid: int) -> None:
        async def cleanup() -> None:
            await terminate_process_group(pgid, self.kill_grace)
            try:
                await asyncio.wait_for(proc.wait(), self.kill_grace)
            except asyncio.TimeoutError:
                log.warning("process %d did not exit after SIGKILL", proc.pid)

        task = asyncio.ensure_future(cleanup())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def kill_all(self) -> None:
        """Synchronously SIGKILL all running process groups (last resort)."""
        for pgid in list(self._running_pgids):
            _signal_process_group(pgid, signal.SIGKILL)

    async def wait_cleanups(self, timeout: float | None = None) -> None:
        if self._cleanup_tasks:
            await asyncio.wait(set(self._cleanup_tasks), timeout=timeout)
