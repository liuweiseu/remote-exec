"""Async gRPC client library (no CLI concerns).

Typical use::

    async with RemoteExecClient(endpoint) as client:
        result = await client.run("make test", on_stdout=..., on_stderr=...)
        print(result.exit_code)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import grpc

from .config import TOKEN_ENV_VAR, ServerEndpoint
from .security import auth_metadata, channel_credentials, read_secret_file
from .v1 import remote_exec_pb2 as pb
from .v1 import remote_exec_pb2_grpc as pb_grpc

DEFAULT_CONNECT_TIMEOUT = 10.0

# Shell/ssh-like exit codes for outcomes that are not a remote exit status.
EXIT_TIMEOUT = 124      # same as coreutils `timeout`
EXIT_REJECTED = 126     # same as the shell's "cannot execute": blocked by the server's tools policy
EXIT_INTERRUPTED = 130  # 128 + SIGINT
EXIT_CLIENT_ERROR = 255  # same as ssh: connection/auth/protocol failure

CLIENT_OPTIONS = [
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 20_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
]


class RemoteExecError(Exception):
    """Failure of the remote execution machinery (not of the remote command)."""


class ServerUnavailable(RemoteExecError):
    pass


class CommandRejected(RemoteExecError):
    """The server refused to run the command (tools policy)."""


@dataclass
class ExecutionResult:
    status: str  # "exited" | "timeout" | "error"
    exit_code: int
    signal: int
    message: str
    execution_id: str
    hostname: str
    start_time: datetime | None
    end_time: datetime | None

    @property
    def ok(self) -> bool:
        return self.status == "exited" and self.exit_code == 0

    @property
    def duration(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    @property
    def local_exit_code(self) -> int:
        """Exit code a CLI wrapper should return for this result."""
        if self.status == "exited":
            return self.exit_code
        if self.status == "timeout":
            return EXIT_TIMEOUT
        return EXIT_CLIENT_ERROR


_STATUS_NAMES = {
    pb.Finished.EXITED: "exited",
    pb.Finished.TIMEOUT: "timeout",
    pb.Finished.ERROR: "error",
}


def resolve_token(endpoint: ServerEndpoint) -> str | None:
    if endpoint.token:
        return endpoint.token
    if endpoint.token_file:
        return read_secret_file(endpoint.token_file)
    return os.environ.get(TOKEN_ENV_VAR) or None


def _describe_rpc_error(endpoint: ServerEndpoint, err: grpc.aio.AioRpcError) -> str:
    code = err.code()
    details = err.details() or ""
    where = f"{endpoint.name} ({endpoint.address})" if endpoint.name != endpoint.address else endpoint.address
    if code == grpc.StatusCode.UNAVAILABLE:
        return f"cannot reach server {where}: {details}"
    if code == grpc.StatusCode.UNAUTHENTICATED:
        return f"authentication failed for {where}: {details}"
    if code == grpc.StatusCode.PERMISSION_DENIED:
        return f"{where} refused to run the command: {details}"
    if code == grpc.StatusCode.INVALID_ARGUMENT:
        return f"invalid request: {details}"
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return f"server {where} is busy: {details}"
    if code == grpc.StatusCode.CANCELLED:
        return f"call to {where} was cancelled: {details}"
    return f"{where}: {code.name}: {details}"


class RemoteExecClient:
    def __init__(self, endpoint: ServerEndpoint, connect_timeout: float | None = None):
        self.endpoint = endpoint
        self.connect_timeout = connect_timeout or endpoint.connect_timeout or DEFAULT_CONNECT_TIMEOUT
        self._metadata = auth_metadata(resolve_token(endpoint))
        self._channel: grpc.aio.Channel | None = None
        self._stub: pb_grpc.RemoteExecutorStub | None = None

    def _make_channel(self) -> grpc.aio.Channel:
        ep = self.endpoint
        options = list(CLIENT_OPTIONS)
        if ep.uses_tls:
            if ep.tls_server_name:
                options.append(("grpc.ssl_target_name_override", ep.tls_server_name))
            creds = channel_credentials(ep.ca_cert, ep.client_cert, ep.client_key)
            return grpc.aio.secure_channel(ep.address, creds, options=options)
        return grpc.aio.insecure_channel(ep.address, options=options)

    async def __aenter__(self) -> RemoteExecClient:
        self._channel = self._make_channel()
        self._stub = pb_grpc.RemoteExecutorStub(self._channel)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    async def server_info(self) -> pb.ServerInfo:
        assert self._stub, "use 'async with RemoteExecClient(...)'"
        try:
            return await self._stub.GetServerInfo(
                pb.GetServerInfoRequest(), metadata=self._metadata, timeout=self.connect_timeout
            )
        except grpc.aio.AioRpcError as err:
            raise self._error(err) from None

    def _error(self, err: grpc.aio.AioRpcError) -> RemoteExecError:
        msg = _describe_rpc_error(self.endpoint, err)
        if err.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
            return ServerUnavailable(msg)
        if err.code() == grpc.StatusCode.PERMISSION_DENIED:
            return CommandRejected(msg)
        return RemoteExecError(msg)

    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[pb.ExecuteEvent]:
        """Yield raw ExecuteEvent messages as they arrive.

        Closing the iterator early (or cancelling the task) cancels the RPC,
        which makes the server terminate the remote process group.
        """
        assert self._stub, "use 'async with RemoteExecClient(...)'"
        request = pb.ExecuteRequest(
            command=command, working_directory=cwd or "", environment=dict(env or {}),
            timeout_seconds=float(timeout or 0),
        )
        call = self._stub.Execute(request, metadata=self._metadata, wait_for_ready=False)
        try:
            # The server answers with Started right after spawning, so the first
            # message doubles as a connection check with a bounded wait.
            try:
                first = await asyncio.wait_for(call.read(), self.connect_timeout)
            except asyncio.TimeoutError:
                raise ServerUnavailable(
                    f"no response from {self.endpoint.address} within {self.connect_timeout:g}s"
                ) from None
            if first is grpc.aio.EOF:
                raise RemoteExecError("server closed the stream without sending any event")
            yield first
            while (event := await call.read()) is not grpc.aio.EOF:
                yield event
        except grpc.aio.AioRpcError as err:
            raise self._error(err) from None
        finally:
            if not call.done():
                call.cancel()

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_stdout: Callable[[bytes], None] | None = None,
        on_stderr: Callable[[bytes], None] | None = None,
    ) -> ExecutionResult:
        """Run a command, forwarding output chunks to the callbacks."""
        hostname = ""
        events = self.execute(command, cwd=cwd, env=env, timeout=timeout)
        try:
            async for ev in events:
                kind = ev.WhichOneof("event")
                if kind == "stdout":
                    if on_stdout:
                        on_stdout(ev.stdout.data)
                elif kind == "stderr":
                    if on_stderr:
                        on_stderr(ev.stderr.data)
                elif kind == "started":
                    hostname = ev.started.hostname
                elif kind == "finished":
                    f = ev.finished
                    return ExecutionResult(
                        status=_STATUS_NAMES.get(f.status, "error"),
                        exit_code=f.exit_code,
                        signal=f.signal,
                        message=f.message,
                        execution_id=f.execution_id,
                        hostname=hostname,
                        start_time=f.start_time.ToDatetime() if f.HasField("start_time") else None,
                        end_time=f.end_time.ToDatetime() if f.HasField("end_time") else None,
                    )
        finally:
            await events.aclose()
        raise RemoteExecError("stream ended without a final status (server crashed or connection lost)")
