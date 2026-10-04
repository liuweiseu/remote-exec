"""Command log: content, line handling, rotation, server integration."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import free_port, port_in_use, wait_until

from remote_exec.client import CommandRejected, RemoteExecClient
from remote_exec.cmdlog import MAX_LINE_BYTES, CommandLog

SERVER = str(Path(sys.executable).parent / "remote-exec-server")
CLIENT = str(Path(sys.executable).parent / "remote-exec-client")
LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[(.{12})\] (.*)$")


def entries(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text().splitlines():
        m = LINE_RE.match(line)
        assert m, f"malformed line: {line!r}"
        out.append((m.group(1), m.group(2)))
    return out


def test_records_command_and_output(tmp_path):
    log = CommandLog(tmp_path / "c.log")
    log.start("aaaaaaaaaaaa", "ipv4:1.2.3.4:5", "/proj", "make all", 0, {"B": "secret", "A": "x"})
    log.output("aaaaaaaaaaaa", "stdout", b"line1\nline2\r\npart")
    log.output("aaaaaaaaaaaa", "stderr", b"warn\n")
    log.output("aaaaaaaaaaaa", "stdout", b"ial\n\xff\xfe bad bytes\n")
    log.end("aaaaaaaaaaaa", "exited", 0, 1.5)
    log.close()
    text = (tmp_path / "c.log").read_text()
    assert "secret" not in text  # env values are never logged
    assert entries(tmp_path / "c.log") == [
        ("aaaaaaaaaaaa", 'START client=ipv4:1.2.3.4:5 cwd="/proj" timeout=0 env_keys="A,B" command="make all"'),
        ("aaaaaaaaaaaa", "OUT| line1"),
        ("aaaaaaaaaaaa", "OUT| line2"),
        ("aaaaaaaaaaaa", "ERR| warn"),
        ("aaaaaaaaaaaa", "OUT| partial"),
        ("aaaaaaaaaaaa", "OUT| \ufffd\ufffd bad bytes"),
        ("aaaaaaaaaaaa", "END status=exited exit_code=0 duration=1.500s"),
    ]


def test_unterminated_output_flushed_at_end_and_executions_kept_apart(tmp_path):
    log = CommandLog(tmp_path / "c.log")
    log.output("111111111111", "stdout", b"progress 50%")
    log.output("222222222222", "stdout", b"other\n")
    log.end("111111111111", "timeout", 143, 2.0)
    log.close()
    assert entries(tmp_path / "c.log") == [
        ("222222222222", "OUT| other"),
        ("111111111111", "OUT| progress 50%"),
        ("111111111111", "END status=timeout exit_code=143 duration=2.000s"),
    ]


def test_very_long_line_is_split(tmp_path):
    log = CommandLog(tmp_path / "c.log")
    log.output("aaaaaaaaaaaa", "stdout", b"x" * (MAX_LINE_BYTES * 2 + 10))
    log.end("aaaaaaaaaaaa", "exited", 0, 0)
    log.close()
    sizes = [len(msg) - len("OUT| ") for _, msg in entries(tmp_path / "c.log")[:-1]]
    assert sizes == [MAX_LINE_BYTES, MAX_LINE_BYTES, 10]


def test_file_mode_and_append_across_restarts(tmp_path):
    for i in range(2):
        log = CommandLog(tmp_path / "c.log")
        log.rejected("client", f"cmd{i}", "denied")
        log.close()
    assert stat.S_IMODE((tmp_path / "c.log").stat().st_mode) == 0o600
    assert [msg.endswith(f'command="cmd{i}"') for i, (_, msg) in enumerate(entries(tmp_path / "c.log"))] == [True, True]


def run_many(log: CommandLog, n: int) -> None:
    for i in range(n):
        eid = f"{i:012d}"
        log.start(eid, "c", "/", f"echo {i}", 0, {})
        log.output(eid, "stdout", b"y" * 200 + b"\n")
        log.end(eid, "exited", 0, 0.1)


def test_rotation_keeps_all_by_default(tmp_path):
    log = CommandLog(tmp_path / "c.log", max_bytes=2048)
    run_many(log, 40)
    log.close()
    rotated = log.rotated_files()
    assert len(rotated) >= 5
    for f in [*rotated, tmp_path / "c.log"]:
        assert f.stat().st_size <= 2048
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert re.fullmatch(r"c(\.\d{8}-\d{6}(-\d+)?)?\.log", f.name)
    # nothing lost: every command's END is in exactly one file
    ends = sum(msg.startswith("END") for f in [*rotated, tmp_path / "c.log"] for _, msg in entries(f))
    assert ends == 40


def test_rotation_keep_limits_files(tmp_path):
    log = CommandLog(tmp_path / "c.log", max_bytes=2048, keep=2)
    run_many(log, 40)
    log.close()
    rotated = log.rotated_files()
    assert len(rotated) == 2
    # the newest records survive
    last = entries(tmp_path / "c.log")[-1]
    assert last[0] == f"{39:012d}"


def test_invalid_limits(tmp_path):
    with pytest.raises(ValueError):
        CommandLog(tmp_path / "c.log", max_bytes=10)
    with pytest.raises(ValueError):
        CommandLog(tmp_path / "c.log", keep=-1)


# ---------------------------------------------------------------- server integration


async def test_server_writes_command_log(server_factory, tmp_path):
    path = tmp_path / "cmds.log"
    _, ep = await server_factory(command_log=str(path))
    async with RemoteExecClient(ep) as client:
        await client.run("echo out; echo err >&2; exit 3")
        with pytest.raises(CommandRejected):
            await client.run("ssh elsewhere")
    msgs = [msg for _, msg in entries(path)]
    assert msgs[0].startswith("START ") and 'command="echo out; echo err >&2; exit 3"' in msgs[0]
    assert msgs[1:4] == ["OUT| out", "ERR| err", "END status=exited exit_code=3 duration=" + msgs[3].split("duration=")[1]]
    assert msgs[4].startswith("REJECTED ") and 'command="ssh elsewhere"' in msgs[4]


async def test_server_without_command_log(server_factory, tmp_path):
    _, ep = await server_factory()  # command_log=None
    async with RemoteExecClient(ep) as client:
        assert (await client.run("true")).ok
    assert not list(tmp_path.glob("*.log"))


def _serve(args, cwd):
    port = free_port()
    proc = subprocess.Popen([SERVER, "--port", str(port), *args], cwd=cwd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_until(lambda: port_in_use(port), 10)
    return proc, f"127.0.0.1:{port}"


def test_cli_default_location_is_server_cwd(tmp_path):
    proc, target = _serve([], tmp_path)
    try:
        r = subprocess.run([CLIENT, target, "echo via-cli"], capture_output=True, timeout=30)
        assert r.returncode == 0
    finally:
        proc.terminate()
        proc.wait(10)
    log = tmp_path / "remote-exec-commands.log"
    assert "OUT| via-cli" in log.read_text()


def test_cli_custom_path_and_disable(tmp_path):
    custom = tmp_path / "logs" / "mine.log"
    for args, expected in ((["--command-log", str(custom)], custom), (["--no-command-log"], None)):
        proc, target = _serve(args, tmp_path)
        try:
            subprocess.run([CLIENT, target, "echo x"], capture_output=True, timeout=30)
        finally:
            proc.terminate()
            proc.wait(10)
        if expected:
            assert "OUT| x" in expected.read_text()
    assert not (tmp_path / "remote-exec-commands.log").exists()


def test_cli_rejects_bad_limits(tmp_path):
    r = subprocess.run([SERVER, "--port", "50097", "--command-log-max-mb", "0"], capture_output=True,
                       timeout=30, cwd=tmp_path)
    assert r.returncode == 2 and b"command_log_max_mb" in r.stderr
