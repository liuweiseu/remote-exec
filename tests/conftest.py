from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import pytest

from remote_exec.config import ServerEndpoint
from remote_exec.server import RemoteExecServer, ServerSettings


def pid_alive(pid: int) -> bool:
    """True if ``pid`` exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


async def async_wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def read_pid(path: Path, timeout: float = 10.0) -> int:
    assert wait_until(lambda: path.exists() and path.read_text().strip(), timeout), f"{path} not written"
    return int(path.read_text().strip())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
async def server_factory():
    """Start in-process servers on random ports; stopped at teardown."""
    servers: list[RemoteExecServer] = []

    async def start(**kwargs) -> tuple[RemoteExecServer, ServerEndpoint]:
        settings = ServerSettings(host="127.0.0.1", port=0, kill_grace=kwargs.pop("kill_grace", 1.0), **kwargs)
        server = RemoteExecServer(settings)
        port = await server.start()
        servers.append(server)
        return server, ServerEndpoint(name="test", host="127.0.0.1", port=port)

    yield start
    for server in servers:
        await server.stop(grace=1)


@pytest.fixture
async def endpoint(server_factory) -> ServerEndpoint:
    _, ep = await server_factory()
    return ep


async def async_read_pid(path: Path, timeout: float = 10.0) -> int:
    ok = await async_wait_until(lambda: path.exists() and path.read_text().strip(), timeout)
    assert ok, f"{path} not written"
    return int(path.read_text().strip())
