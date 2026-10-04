"""Async gRPC server: request validation, audit logging, and lifecycle.

The servicer is a thin adapter between protobuf messages and
:class:`remote_exec.executor.Executor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import logging
import math
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from . import __version__
from .config import DEFAULT_HOST, DEFAULT_PORT, format_address
from .executor import CommandSpec, Executor, Finished, Output, Started, Status, Stream
from .policy import PolicyProvider
from .security import TokenAuthInterceptor, peer_identity, server_credentials
from .v1 import remote_exec_pb2 as pb
from .v1 import remote_exec_pb2_grpc as pb_grpc

log = logging.getLogger(__name__)
audit = logging.getLogger("remote_exec.audit")

MAX_COMMAND_BYTES = 256 * 1024
MAX_ENV_ENTRIES = 1024
MAX_ENV_VALUE_BYTES = 128 * 1024
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Keepalive: lets long silent commands survive NAT/firewall idle timeouts and
# lets the server notice vanished clients (which cancels their commands).
SERVER_OPTIONS = [
    ("grpc.keepalive_time_ms", 60_000),
    ("grpc.keepalive_timeout_ms", 20_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.min_recv_ping_interval_without_data_ms", 10_000),
    ("grpc.http2.max_ping_strikes", 0),
]


def default_shell() -> str:
    return "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"


@dataclass
class ServerSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    shell: str = ""
    max_concurrent: int = 32
    max_timeout: float | None = None
    kill_grace: float = 5.0
    token: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_client_ca: str | None = None
    tools_file: str | None = None      # None: default ~/.config/remote-exec/tools.yaml if present
    tools_required: bool = False       # error if tools_file is missing (explicit --tools)

    @property
    def address(self) -> str:
        return format_address(self.host, self.port)


class RequestError(ValueError):
    pass


def validate_request(req: pb.ExecuteRequest, max_timeout: float | None) -> CommandSpec:
    command = req.command
    if not command.strip():
        raise RequestError("command must not be empty")
    if len(command.encode()) > MAX_COMMAND_BYTES:
        raise RequestError(f"command exceeds {MAX_COMMAND_BYTES} bytes")
    if "\x00" in command:
        raise RequestError("command must not contain NUL bytes")

    cwd = None
    if req.working_directory:
        if "\x00" in req.working_directory:
            raise RequestError("working_directory must not contain NUL bytes")
        cwd = os.path.expanduser(req.working_directory)
        if not os.path.isabs(cwd):
            raise RequestError(f"working_directory must be an absolute path (or start with ~): {req.working_directory!r}")

    env = dict(req.environment)
    if len(env) > MAX_ENV_ENTRIES:
        raise RequestError(f"too many environment variables (max {MAX_ENV_ENTRIES})")
    for key, value in env.items():
        if not _ENV_NAME_RE.match(key):
            raise RequestError(f"invalid environment variable name: {key!r}")
        if "\x00" in value or len(value.encode()) > MAX_ENV_VALUE_BYTES:
            raise RequestError(f"invalid value for environment variable {key}")

    timeout = req.timeout_seconds
    if math.isnan(timeout) or math.isinf(timeout) or timeout < 0:
        raise RequestError("timeout_seconds must be a finite number >= 0")
    if max_timeout and (timeout == 0 or timeout > max_timeout):
        timeout = max_timeout
    return CommandSpec(command=command, cwd=cwd, env=env, timeout=timeout or None)


def _ts(dt) -> Timestamp:
    t = Timestamp()
    t.FromDatetime(dt)
    return t


_STATUS_TO_PB = {
    Status.EXITED: pb.Finished.EXITED,
    Status.TIMEOUT: pb.Finished.TIMEOUT,
    Status.ERROR: pb.Finished.ERROR,
}


def event_to_proto(ev, hostname: str) -> pb.ExecuteEvent:
    if isinstance(ev, Output):
        chunk = pb.OutputChunk(data=ev.data)
        return pb.ExecuteEvent(stdout=chunk) if ev.stream is Stream.STDOUT else pb.ExecuteEvent(stderr=chunk)
    if isinstance(ev, Started):
        return pb.ExecuteEvent(started=pb.Started(
            execution_id=ev.execution_id, pid=ev.pid, start_time=_ts(ev.start_time), hostname=hostname,
        ))
    if isinstance(ev, Finished):
        return pb.ExecuteEvent(finished=pb.Finished(
            status=_STATUS_TO_PB[ev.status], exit_code=ev.exit_code, signal=ev.signal,
            start_time=_ts(ev.start_time), end_time=_ts(ev.end_time),
            message=ev.message, execution_id=ev.execution_id,
        ))
    raise TypeError(f"unknown event {ev!r}")


def _q(value: str) -> str:
    """Quote a value for single-line key=value audit records."""
    return json.dumps(value, ensure_ascii=False)


class RemoteExecutorService(pb_grpc.RemoteExecutorServicer):
    def __init__(self, settings: ServerSettings, executor: Executor, policy: PolicyProvider):
        self.settings = settings
        self.executor = executor
        self.policy = policy
        self.hostname = socket.gethostname()
        self._active = 0

    async def Execute(self, request: pb.ExecuteRequest, context: grpc.aio.ServicerContext):
        client = peer_identity(context)
        try:
            spec = validate_request(request, self.settings.max_timeout)
        except RequestError as exc:
            audit.warning("rejected client=%s reason=%s", client, _q(str(exc)))
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        if reason := self.policy.current().check(spec.command):
            audit.warning("rejected client=%s reason=%s command=%s", client, _q(f"tools policy: {reason}"),
                          _q(spec.command))
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, f"blocked by the server's tools policy: {reason}")

        if self._active >= self.settings.max_concurrent:
            audit.warning("rejected client=%s reason=%s", client, _q("too many concurrent commands"))
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"server is already running {self._active} commands (max {self.settings.max_concurrent})",
            )

        execution_id = uuid.uuid4().hex[:12]
        audit.info(
            "start id=%s client=%s cwd=%s timeout=%s env_keys=%s command=%s",
            execution_id, client, _q(spec.cwd or os.getcwd()), spec.timeout or 0,
            _q(",".join(sorted(spec.env))), _q(spec.command),
        )
        started = time.monotonic()
        outcome = "cancelled"
        exit_code: int | str = "-"
        self._active += 1
        try:
            async with contextlib.aclosing(self.executor.run(spec, execution_id)) as events:
                async for ev in events:
                    if isinstance(ev, Finished):
                        outcome, exit_code = ev.status.value, ev.exit_code
                        if ev.status is Status.ERROR:
                            exit_code = "-"
                            outcome = f"error message={_q(ev.message)}"
                    yield event_to_proto(ev, self.hostname)
        finally:
            self._active -= 1
            audit.info(
                "end id=%s client=%s status=%s exit_code=%s duration=%.3fs",
                execution_id, client, outcome, exit_code, time.monotonic() - started,
            )

    async def GetServerInfo(self, request, context):
        policy = self.policy.current()
        return pb.ServerInfo(
            hostname=self.hostname, version=__version__, user=getpass.getuser(), shell=self.executor.shell,
            allowed_tools=policy.allow, denied_tools=policy.deny,
        )


class RemoteExecServer:
    """Owns the grpc.aio server and the executor."""

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.executor = Executor(shell=settings.shell or default_shell(), kill_grace=settings.kill_grace)
        self.policy = PolicyProvider(settings.tools_file, required=settings.tools_required)
        interceptors = [TokenAuthInterceptor(settings.token)] if settings.token else []
        self._server = grpc.aio.server(interceptors=interceptors, options=SERVER_OPTIONS)
        pb_grpc.add_RemoteExecutorServicer_to_server(
            RemoteExecutorService(settings, self.executor, self.policy), self._server
        )
        self.bound_port: int | None = None

    async def start(self) -> int:
        s = self.settings
        if s.tls_cert:
            creds = server_credentials(s.tls_cert, s.tls_key, s.tls_client_ca)
            port = self._server.add_secure_port(s.address, creds)
        else:
            port = self._server.add_insecure_port(s.address)
        if not port:
            raise OSError(f"failed to bind {s.address}")
        await self._server.start()
        self.bound_port = port
        log.info(
            "remote-exec-server %s listening on %s (tls=%s, mtls=%s, token_auth=%s, shell=%s)",
            __version__, format_address(s.host, port), bool(s.tls_cert), bool(s.tls_client_ca),
            bool(s.token), self.executor.shell,
        )
        log.info("tools policy: %s", self.policy.current().describe())
        return port

    async def stop(self, grace: float = 5.0) -> None:
        """Stop accepting RPCs, cancel running ones and clean up their processes."""
        await self._server.stop(grace)
        await self.executor.wait_cleanups(timeout=self.settings.kill_grace + 2)
        self.executor.kill_all()

    async def wait_for_termination(self) -> None:
        await self._server.wait_for_termination()


async def serve(settings: ServerSettings, stop_event: asyncio.Event | None = None) -> None:
    server = RemoteExecServer(settings)
    await server.start()
    stop_event = stop_event or asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        log.info("shutting down")
        await server.stop()
