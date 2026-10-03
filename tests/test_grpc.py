"""End-to-end tests through the real gRPC server and client library (in-process)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

import pytest
from conftest import async_read_pid, async_wait_until, free_port, pid_alive

from remote_exec.client import RemoteExecClient, RemoteExecError, ServerUnavailable
from remote_exec.config import ServerEndpoint


async def run(endpoint, command, **kwargs):
    out, err = bytearray(), bytearray()
    async with RemoteExecClient(endpoint) as client:
        result = await client.run(command, on_stdout=out.extend, on_stderr=err.extend, **kwargs)
    return result, bytes(out), bytes(err)


async def test_successful_command(endpoint):
    result, out, err = await run(endpoint, "echo hello")
    assert result.ok and result.status == "exited" and result.exit_code == 0
    assert out == b"hello\n" and err == b""
    assert result.hostname and result.execution_id
    assert result.start_time and result.end_time and result.duration >= 0


async def test_nonzero_exit_code(endpoint):
    result, _, _ = await run(endpoint, "false")
    assert result.exit_code == 1 and result.local_exit_code == 1 and not result.ok
    result, _, _ = await run(endpoint, "exit 42")
    assert result.local_exit_code == 42


async def test_stderr(endpoint):
    result, out, err = await run(endpoint, "echo problem >&2")
    assert out == b"" and err == b"problem\n" and result.ok


async def test_mixed_stdout_stderr(endpoint):
    cmd = "for i in 1 2 3; do echo out$i; echo err$i >&2; done"
    _, out, err = await run(endpoint, cmd)
    assert out == b"out1\nout2\nout3\n"
    assert err == b"err1\nerr2\nerr3\n"


async def test_working_directory(endpoint, tmp_path):
    _, out, _ = await run(endpoint, "pwd", cwd=str(tmp_path))
    assert out.decode().strip() == str(tmp_path)


async def test_working_directory_missing(endpoint):
    result, _, _ = await run(endpoint, "pwd", cwd="/no/such/dir")
    assert result.status == "error" and result.local_exit_code == 255
    assert "/no/such/dir" in result.message


async def test_environment_variables(endpoint):
    _, out, _ = await run(endpoint, 'echo "$FPGA_PART/$BUILD_MODE"', env={"FPGA_PART": "xcvu37p", "BUILD_MODE": "release"})
    assert out == b"xcvu37p/release\n"


async def test_timeout(endpoint):
    t0 = time.monotonic()
    result, out, _ = await run(endpoint, "echo started; sleep 30", timeout=0.5)
    assert time.monotonic() - t0 < 5
    assert result.status == "timeout" and result.local_exit_code == 124
    assert out == b"started\n"


async def test_command_not_found(endpoint):
    result, _, err = await run(endpoint, "definitely_not_a_command_xyz")
    assert result.exit_code == 127
    assert b"not found" in err


async def test_streaming_is_realtime(endpoint):
    async with RemoteExecClient(endpoint) as client:
        t0 = time.monotonic()
        async for ev in client.execute("echo first; sleep 3; echo second"):
            if ev.WhichOneof("event") == "stdout":
                assert ev.stdout.data == b"first\n"
                assert time.monotonic() - t0 < 2
                break


async def test_event_sequence(endpoint):
    async with RemoteExecClient(endpoint) as client:
        kinds = [ev.WhichOneof("event") async for ev in client.execute("echo a")]
    assert kinds[0] == "started" and kinds[-1] == "finished"
    assert kinds.count("finished") == 1


async def test_concurrent_commands(endpoint):
    t0 = time.monotonic()
    results = await asyncio.gather(*(run(endpoint, f"sleep 1; echo {i}") for i in range(8)))
    elapsed = time.monotonic() - t0
    assert elapsed < 4, f"commands did not run concurrently ({elapsed:.1f}s)"
    assert sorted(out for _, out, _ in results) == sorted(f"{i}\n".encode() for i in range(8))


async def test_client_disconnect_kills_remote_processes(endpoint, tmp_path):
    pidfile = tmp_path / "pids"
    cmd = f"sleep 300 & echo $! > {pidfile}; echo ready; wait"
    client = RemoteExecClient(endpoint)
    await client.__aenter__()
    events = client.execute(cmd)
    async for ev in events:
        if ev.WhichOneof("event") == "stdout":
            break
    child = await async_read_pid(pidfile)
    assert pid_alive(child)
    # Simulate an abrupt disconnect: drop the stream and the whole channel.
    await events.aclose()
    await client.close()
    assert await async_wait_until(lambda: not pid_alive(child), 10), "remote child survived client disconnect"


async def test_client_task_cancel_kills_remote_processes(endpoint, tmp_path):
    pidfile = tmp_path / "pids"
    task = asyncio.ensure_future(run(endpoint, f"sleep 300 & echo $! > {pidfile}; wait"))
    child = await async_read_pid(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await async_wait_until(lambda: not pid_alive(child), 10)


async def test_server_unavailable():
    ep = ServerEndpoint(name="down", host="127.0.0.1", port=free_port())
    with pytest.raises(ServerUnavailable):
        await run(ep, "echo hi")


@pytest.mark.parametrize("kwargs, needle", [
    ({"command": "   "}, "empty"),
    ({"command": "pwd", "cwd": "relative/dir"}, "absolute"),
    ({"command": "env", "env": {"BAD-NAME": "x"}}, "BAD-NAME"),
])
async def test_invalid_requests_rejected(endpoint, kwargs, needle):
    with pytest.raises(RemoteExecError, match="invalid request") as exc:
        await run(endpoint, **kwargs)
    assert needle in str(exc.value)


async def test_max_concurrent(server_factory):
    _, ep = await server_factory(max_concurrent=1)
    slow = asyncio.ensure_future(run(ep, "sleep 2"))
    await asyncio.sleep(0.5)
    with pytest.raises(RemoteExecError, match="busy"):
        await run(ep, "echo hi")
    result, _, _ = await slow
    assert result.ok


async def test_server_max_timeout_applies(server_factory):
    _, ep = await server_factory(max_timeout=0.5)
    result, _, _ = await run(ep, "sleep 30")
    assert result.status == "timeout"


async def test_server_info(endpoint):
    async with RemoteExecClient(endpoint) as client:
        info = await client.server_info()
    assert info.hostname and info.user and info.version


async def test_token_auth(server_factory):
    _, ep = await server_factory(token="s3cret")
    with pytest.raises(RemoteExecError, match="authentication failed"):
        await run(ep, "echo hi")
    ep.token = "wrong"
    with pytest.raises(RemoteExecError, match="authentication failed"):
        await run(ep, "echo hi")
    ep.token = "s3cret"
    result, out, _ = await run(ep, "echo hi")
    assert result.ok and out == b"hi\n"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("pki")

    def sh(*args):
        subprocess.run(args, cwd=d, check=True, capture_output=True)

    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
       "-days", "1", "-subj", "/CN=test-ca")
    (d / "san.ext").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    for name in ("server", "client"):
        sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
           "-subj", f"/CN={name}")
        sh("openssl", "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
           "-CAcreateserial", "-out", f"{name}.pem", "-days", "1", "-extfile", "san.ext")
    return d


async def test_mutual_tls(server_factory, pki):
    _, ep = await server_factory(
        tls_cert=str(pki / "server.pem"), tls_key=str(pki / "server.key"), tls_client_ca=str(pki / "ca.pem"),
    )
    ep.host = "localhost"
    ep.ca_cert = str(pki / "ca.pem")
    ep.connect_timeout = 3

    # Without a client certificate the handshake must fail.
    with pytest.raises(RemoteExecError):
        await run(ep, "echo hi")

    ep.client_cert = str(pki / "client.pem")
    ep.client_key = str(pki / "client.key")
    result, out, _ = await run(ep, "echo secure")
    assert result.ok and out == b"secure\n"
