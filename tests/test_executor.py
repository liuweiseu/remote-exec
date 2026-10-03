"""Tests for the gRPC-independent subprocess layer."""

from __future__ import annotations

import asyncio
import contextlib
import time

from conftest import async_read_pid, async_wait_until, pid_alive, read_pid

from remote_exec.executor import CommandSpec, Executor, Finished, Output, Started, Status, Stream


async def collect(executor: Executor, spec: CommandSpec):
    out, err, events = b"", b"", []
    async for ev in executor.run(spec):
        events.append(ev)
        if isinstance(ev, Output):
            if ev.stream is Stream.STDOUT:
                out += ev.data
            else:
                err += ev.data
    assert isinstance(events[-1], Finished)
    assert sum(isinstance(e, Finished) for e in events) == 1
    return out, err, events[-1], events


async def test_stdout_and_exit_code():
    out, err, fin, events = await collect(Executor(), CommandSpec("echo hello"))
    assert isinstance(events[0], Started)
    assert out == b"hello\n" and err == b""
    assert fin.status is Status.EXITED and fin.exit_code == 0 and fin.signal == 0
    assert fin.end_time >= fin.start_time


async def test_nonzero_exit_and_stderr():
    out, err, fin, _ = await collect(Executor(), CommandSpec("echo oops >&2; exit 7"))
    assert out == b"" and err == b"oops\n"
    assert fin.status is Status.EXITED and fin.exit_code == 7


async def test_killed_by_signal_reports_128_plus_n():
    _, _, fin, _ = await collect(Executor(), CommandSpec("kill -9 $$"))
    assert fin.signal == 9 and fin.exit_code == 137


async def test_cwd_and_env(tmp_path):
    out, _, fin, _ = await collect(
        Executor(), CommandSpec('pwd; echo "$A-$B"', cwd=str(tmp_path), env={"A": "1", "B": "two words"})
    )
    assert out.decode().splitlines() == [str(tmp_path), "1-two words"]


async def test_bad_cwd_is_error():
    _, _, fin, events = await collect(Executor(), CommandSpec("pwd", cwd="/definitely/not/here"))
    assert len(events) == 1
    assert fin.status is Status.ERROR and "/definitely/not/here" in fin.message


async def test_binary_and_large_output():
    out, _, fin, _ = await collect(Executor(), CommandSpec("head -c 3000000 /dev/zero; printf '\\377\\376'"))
    assert len(out) == 3_000_002 and out.endswith(b"\xff\xfe")
    assert fin.exit_code == 0


async def test_output_is_streamed_before_exit():
    """Partial lines (no newline) must arrive while the process still runs."""
    executor = Executor()
    t0 = time.monotonic()
    async for ev in executor.run(CommandSpec("printf 'progress'; sleep 2; echo done")):
        if isinstance(ev, Output):
            assert ev.data == b"progress"
            assert time.monotonic() - t0 < 1.5
            break


async def test_timeout_kills_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    spec = CommandSpec(f"sleep 300 & echo $! > {pidfile}; sleep 300", timeout=0.5)
    t0 = time.monotonic()
    _, _, fin, _ = await collect(Executor(kill_grace=1), spec)
    assert time.monotonic() - t0 < 5
    assert fin.status is Status.TIMEOUT
    assert fin.exit_code == 128 + 15
    child = read_pid(pidfile)
    assert await async_wait_until(lambda: not pid_alive(child), 5)


async def test_timeout_escalates_to_sigkill(tmp_path):
    spec = CommandSpec("trap '' TERM; sleep 300 & wait; sleep 300", timeout=0.3)
    _, _, fin, _ = await collect(Executor(kill_grace=0.5), spec)
    assert fin.status is Status.TIMEOUT and fin.signal == 9


async def test_background_children_killed_after_main_exits(tmp_path):
    pidfile = tmp_path / "bg.pid"
    t0 = time.monotonic()
    out, _, fin, _ = await collect(Executor(), CommandSpec(f"sleep 300 & echo $! > {pidfile}; echo main-done"))
    assert out == b"main-done\n" and fin.exit_code == 0
    assert time.monotonic() - t0 < 5  # did not wait for the background sleep
    assert await async_wait_until(lambda: not pid_alive(read_pid(pidfile)), 5)


async def test_consumer_abort_kills_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    executor = Executor(kill_grace=1)
    gen = executor.run(CommandSpec(f"sleep 300 & echo $! > {pidfile}; echo ready; sleep 300"))
    shell_pid = None
    async for ev in gen:
        if isinstance(ev, Started):
            shell_pid = ev.pid
        if isinstance(ev, Output):
            break
    await gen.aclose()
    child = read_pid(pidfile)
    assert await async_wait_until(lambda: not pid_alive(child) and not pid_alive(shell_pid), 5)
    await executor.wait_cleanups(5)


async def test_task_cancellation_kills_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    executor = Executor(kill_grace=1)

    async def consume():
        async with contextlib.aclosing(executor.run(CommandSpec(f"sleep 300 & echo $! > {pidfile}; wait"))) as gen:
            async for _ in gen:
                pass

    task = asyncio.ensure_future(consume())
    child = await async_read_pid(pidfile)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert await async_wait_until(lambda: not pid_alive(child), 5)
    await executor.wait_cleanups(5)
