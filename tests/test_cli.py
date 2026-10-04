"""Localhost integration tests using the installed console scripts as subprocesses."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import free_port, pid_alive, port_in_use, read_pid, wait_until

BIN = Path(sys.executable).parent
SERVER = str(BIN / "remote-exec-server")
CLIENT = str(BIN / "remote-exec-client")


def start_server(*args: str) -> subprocess.Popen:
    # Run in the isolated HOME so the default ./remote-exec-commands.log stays out of the repo.
    proc = subprocess.Popen(
        [SERVER, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        cwd=os.environ["HOME"],
    )
    return proc


def wait_listening(proc: subprocess.Popen, port: int) -> None:
    if not wait_until(lambda: port_in_use(port) or proc.poll() is not None, 10):
        pytest.fail(f"server did not start listening on {port}")
    if proc.poll() is not None:
        pytest.fail(f"server exited: {proc.stdout.read().decode()}")


def stop_server(proc: subprocess.Popen) -> str:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return proc.stdout.read().decode()


@pytest.fixture
def config_file(tmp_path):
    def make(servers: dict[str, dict]) -> Path:
        lines = ["servers:"]
        for name, entry in servers.items():
            lines.append(f"  {name}:")
            lines += [f"    {k}: {v}" for k, v in entry.items()]
        path = tmp_path / "config.yaml"
        path.write_text("\n".join(lines) + "\n")
        return path
    return make


def client(*args: str, config: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    cmd = [CLIENT, *(["--config", str(config)] if config else []), *args]
    return subprocess.run(cmd, capture_output=True, timeout=60, **kwargs)


@pytest.fixture
def running_server():
    procs = []

    def start(*args: str, port: int) -> subprocess.Popen:
        proc = start_server(*args)
        procs.append(proc)
        wait_listening(proc, port)
        return proc

    yield start
    for proc in procs:
        stop_server(proc)


def test_help_commands():
    for exe in (SERVER, CLIENT):
        r = subprocess.run([exe, "--help"], capture_output=True, timeout=30)
        assert r.returncode == 0 and b"usage:" in r.stdout


def test_default_port_50051(running_server, config_file):
    if port_in_use(50051):
        pytest.skip("port 50051 already in use on this machine")
    running_server("--host", "127.0.0.1", port=50051)  # no --port: must default to 50051
    cfg = config_file({"local": {"host": "127.0.0.1"}})  # no port: must default to 50051
    r = client("local", "echo hello", config=cfg)
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"hello\n"


def test_custom_port_50052(running_server, config_file):
    if port_in_use(50052):
        pytest.skip("port 50052 already in use on this machine")
    running_server("--host", "127.0.0.1", "--port", "50052", port=50052)
    cfg = config_file({"test": {"host": "127.0.0.1", "port": 50052}})
    r = client("test", "echo hello", config=cfg)
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"hello\n"


@pytest.fixture
def server_and_config(running_server, config_file):
    port = free_port()
    running_server("--listen", f"127.0.0.1:{port}", "--kill-grace", "1", port=port)
    return config_file({"local": {"host": "127.0.0.1", "port": port}})


def test_exit_codes(server_and_config):
    cfg = server_and_config
    assert client("local", "false", config=cfg).returncode == 1
    assert client("local", "exit 7", config=cfg).returncode == 7
    assert client("local", "true", config=cfg).returncode == 0
    r = client("local", "nonexistent_cmd_abc", config=cfg)
    assert r.returncode == 127 and b"not found" in r.stderr


def test_stdout_stderr_separated(server_and_config):
    r = client("local", "echo out; echo err >&2", config=server_and_config)
    assert r.stdout == b"out\n" and r.stderr == b"err\n" and r.returncode == 0


def test_cwd_env_and_word_joining(server_and_config, tmp_path):
    r = client("local", "--cwd", str(tmp_path), "-e", "X=1", "-e", "Y=two", "--", "echo", "$PWD", "$X$Y",
               config=server_and_config)
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode() == f"{tmp_path} 1two\n"


def test_env_copies_local_value(server_and_config):
    r = client("local", "-e", "LOCAL_ONLY", "echo $LOCAL_ONLY", config=server_and_config,
               env={**os.environ, "LOCAL_ONLY": "from-local"})
    assert r.stdout == b"from-local\n"


def test_timeout_exit_124(server_and_config):
    t0 = time.monotonic()
    r = client("local", "--timeout", "0.5", "sleep 30", config=server_and_config)
    assert r.returncode == 124
    assert b"timed out" in r.stderr
    assert time.monotonic() - t0 < 10


def test_server_unavailable_exit_255(config_file):
    cfg = config_file({"down": {"host": "127.0.0.1", "port": free_port()}})
    r = client("down", "echo hi", config=cfg)
    assert r.returncode == 255
    assert b"cannot reach server" in r.stderr


def test_unknown_config_key_exit_255(config_file):
    cfg = config_file({"x": {"host": "h", "prot": 1}})
    r = client("x", "echo hi", config=cfg)
    assert r.returncode == 255 and b"unknown" in r.stderr


def test_list_and_ping(server_and_config):
    r = client("--list", config=server_and_config)
    assert r.returncode == 0 and r.stdout.startswith(b"local ")
    r = client("local", "--ping", config=server_and_config)
    assert r.returncode == 0 and b"ok" in r.stdout


@pytest.mark.parametrize("sig, expected", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_client_interrupt_kills_remote(server_and_config, tmp_path, sig, expected):
    pidfile = tmp_path / "pid"
    proc = subprocess.Popen(
        [CLIENT, "--config", str(server_and_config), "local", f"sleep 300 & echo $! > {pidfile}; wait"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    child = read_pid(pidfile)
    assert pid_alive(child)
    proc.send_signal(sig)
    assert proc.wait(10) == expected
    assert wait_until(lambda: not pid_alive(child), 10), "remote process survived client interrupt"


def test_client_sigkill_disconnect_kills_remote(server_and_config, tmp_path):
    pidfile = tmp_path / "pid"
    proc = subprocess.Popen(
        [CLIENT, "--config", str(server_and_config), "local", f"sleep 300 & echo $! > {pidfile}; wait"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    child = read_pid(pidfile)
    proc.kill()  # no chance to cancel gracefully: server must notice the broken connection
    proc.wait()
    assert wait_until(lambda: not pid_alive(child), 15), "remote process survived client SIGKILL"


def test_server_refuses_public_bind_without_auth():
    r = subprocess.run([SERVER, "--host", "0.0.0.0", "--port", str(free_port())], capture_output=True, timeout=30)
    assert r.returncode == 2
    assert b"without authentication" in r.stderr


def test_server_shutdown_kills_running_commands(running_server, config_file, tmp_path):
    port = free_port()
    srv = running_server("--port", str(port), "--kill-grace", "1", port=port)
    cfg = config_file({"local": {"host": "127.0.0.1", "port": port}})
    pidfile = tmp_path / "pid"
    proc = subprocess.Popen([CLIENT, "--config", str(cfg), "local", f"sleep 300 & echo $! > {pidfile}; wait"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child = read_pid(pidfile)
    log = stop_server(srv)
    assert wait_until(lambda: not pid_alive(child), 10), "remote process survived server shutdown"
    assert proc.wait(15) != 0
    assert "audit" in log and "start id=" in log


def test_version_flags_match_pyproject():
    import tomllib

    expected = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["version"]
    versions = set()
    for exe in (SERVER, CLIENT):
        for flag in ("-v", "--version"):
            r = subprocess.run([exe, flag], capture_output=True, timeout=30)
            assert r.returncode == 0, r.stderr
            prog, version = r.stdout.decode().split()
            assert prog == Path(exe).name
            versions.add(version)
    assert versions == {expected}


def test_verbose_summary(server_and_config):
    r = client("local", "--verbose", "true", config=server_and_config)
    assert r.returncode == 0 and b"status=exited exit_code=0" in r.stderr
