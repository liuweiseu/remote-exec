"""tools.yaml: command analysis, allow/deny rules, loading/reloading, enforcement."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from remote_exec.client import CommandRejected, RemoteExecClient
from remote_exec.policy import PolicyError, PolicyProvider, ToolPolicy, load_policy, programs_in

SERVER = str(Path(sys.executable).parent / "remote-exec-server")
CLIENT = str(Path(sys.executable).parent / "remote-exec-client")


@pytest.mark.parametrize("command, programs", [
    ("ssh host", ["ssh"]),
    ("/usr/bin/ssh host", ["/usr/bin/ssh"]),
    ("ls && ssh x", ["ls", "ssh"]),
    ("make 2>&1 | tee log; echo done", ["make", "tee", "echo"]),
    ("a\nb", ["a", "b"]),
    ("echo ssh", ["echo"]),
    ("grep ssh file", ["grep"]),
    ("echo 'ssh h'", ["echo"]),
    ("echo hi > ssh", ["echo"]),
    ("echo $(ssh h)", ["ssh", "echo"]),
    ('echo "$(ssh h) done"', ["ssh", "echo"]),
    ("echo `ssh h`", ["ssh", "echo"]),
    ("diff <(ssh a cat f) b", ["ssh", "diff"]),
    ("(cd /tmp; ssh h)", ["cd", "ssh"]),
    ("FOO=1 BAR=2 ssh h", ["ssh"]),
    ("sudo -u build ssh h", ["sudo", "ssh"]),
    ("env -u HOME FOO=1 ssh h", ["env", "ssh"]),
    ("timeout -s KILL 10 nice -n 5 ssh h", ["timeout", "nice", "ssh"]),
    ("xargs -n 1 -I {} ssh {} uptime", ["xargs", "ssh"]),
    ("bash -c 'ssh h'", ["bash", "ssh"]),
    ('bash -lc "cd x && ssh h"', ["bash", "cd", "ssh"]),
    ("eval ssh h", ["eval", "ssh"]),
    ("if [ -f x ]; then ssh h; fi", ["[", "ssh"]),
    ("for f in *.v; do vlog $f; done", ["vlog"]),
    ("cat <<EOF\nssh inside heredoc\nEOF\nmake", ["cat", "make"]),
    ("command -v ssh", ["command"]),
    ("echo $((1 + 2)) ${HOME}", ["echo"]),
    ("# ssh h\nls", ["ls"]),
    ("ssh-keygen -l", ["ssh-keygen"]),
])
def test_programs_in(command, programs):
    assert programs_in(command) == programs


def test_default_policy_denies_only_ssh():
    p = ToolPolicy()
    assert p.check("vivado -mode batch -source build.tcl") is None
    assert p.check("rm -rf build && make") is None
    assert "ssh" in p.check("ssh host uptime")
    assert "ssh" in p.check("/usr/bin/ssh host")
    assert "ssh" in p.check("make && $(ssh h)")
    assert p.check("ssh-keygen -l") is None  # different program


def test_deny_wins_over_allow():
    p = ToolPolicy(allow=("*", "rm"), deny=("rm",))
    assert "denied" in p.check("rm -rf x")


def test_restricted_allow_list():
    p = ToolPolicy(allow=("vivado", "make", "python*"), deny=())
    assert p.check("cd /proj && make -j8 && vivado -mode batch") is None
    assert p.check("python3 run.py") is None  # glob
    assert "not in the allow list" in p.check("ls -la")
    assert "not in the allow list" in p.check("$CMD")  # unknown dynamic program
    assert p.check("echo hi; export X=1; [ -f a ] && true") is None  # builtins need no allow


def test_path_patterns():
    p = ToolPolicy(allow=("*",), deny=("/usr/bin/*",))
    assert p.check("/usr/bin/ssh h")
    assert p.check("ssh h") is None  # pattern with "/" matches the path as written


def test_deny_star_blocks_everything_but_builtins():
    p = ToolPolicy(allow=("*",), deny=("*",))
    assert p.check("ls")
    assert p.check("echo hi")  # deny applies to builtins too


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_load_policy_defaults_per_key(tmp_path):
    assert load_policy(write(tmp_path / "a.yaml", "deny: [rm]\n")).allow == ("*",)
    p = load_policy(write(tmp_path / "b.yaml", "allow: [make]\n"))
    assert p.allow == ("make",) and p.deny == ("ssh",)
    assert load_policy(write(tmp_path / "c.yaml", "deny: []\n")).deny == ()  # explicitly nothing
    assert load_policy(write(tmp_path / "f.yaml", "deny:\n")).deny == ("ssh",)  # empty = default
    yaml_words = load_policy(write(tmp_path / "g.yaml", "allow: [true, yes, 7z, null]\n")).allow
    assert yaml_words == ("true", "yes", "7z", "null")  # not booleans/numbers
    assert load_policy(write(tmp_path / "d.yaml", "allow: '*'\ndeny: ssh\n")).deny == ("ssh",)
    assert load_policy(write(tmp_path / "e.yaml", "")).deny == ("ssh",)


@pytest.mark.parametrize("text, msg", [
    ("allow: [\n", "invalid YAML"),
    ("allowed: [ls]\n", "unknown"),
    ("deny: {ssh: true}\n", "list"),
    ("deny: [[ssh]]\n", "list"),
    ("deny: ['']\n", "list"),
    ("- ls\n", "mapping"),
])
def test_load_policy_errors(tmp_path, text, msg):
    with pytest.raises(PolicyError, match=msg):
        load_policy(write(tmp_path / "t.yaml", text))


def test_provider_default_when_missing(tmp_path):
    provider = PolicyProvider(tmp_path / "tools.yaml")
    assert provider.current() == ToolPolicy()
    with pytest.raises(PolicyError, match="not found"):
        PolicyProvider(tmp_path / "tools.yaml", required=True)


def test_provider_reloads_and_keeps_last_good(tmp_path):
    path = write(tmp_path / "tools.yaml", "deny: [rm]\n")
    provider = PolicyProvider(path)
    assert provider.current().deny == ("rm",)

    write(path, "deny: [rm, git]\n")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert provider.current().deny == ("rm", "git")

    write(path, "deny: [\n")  # broken edit: keep the previous policy
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert provider.current().deny == ("rm", "git")

    path.unlink()
    assert provider.current() == ToolPolicy()


def test_provider_rejects_invalid_file_at_startup(tmp_path):
    with pytest.raises(PolicyError):
        PolicyProvider(write(tmp_path / "tools.yaml", "deny: [\n"))


# ---------------------------------------------------------------- enforcement


async def test_server_rejects_denied_program(server_factory, tmp_path):
    _, ep = await server_factory(tools_file=str(write(tmp_path / "t.yaml", "deny: [ssh, rm]\n")))
    async with RemoteExecClient(ep) as client:
        with pytest.raises(CommandRejected, match="'rm' is denied"):
            await client.run("ls && rm -rf /tmp/nothing")
        result = await client.run("echo fine")
        assert result.ok
        info = await client.server_info()
    assert list(info.allowed_tools) == ["*"] and list(info.denied_tools) == ["ssh", "rm"]


async def test_server_default_policy_blocks_ssh(server_factory):
    _, ep = await server_factory()
    async with RemoteExecClient(ep) as client:
        with pytest.raises(CommandRejected, match="ssh"):
            await client.run("ssh localhost true")


def test_cli_exit_126_and_ping(tmp_path):
    from conftest import free_port, port_in_use, wait_until

    port = free_port()
    tools = write(tmp_path / "tools.yaml", "allow: [echo, true]\ndeny: [ssh]\n")
    srv = subprocess.Popen([SERVER, "--port", str(port), "--tools", str(tools)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=tmp_path)
    try:
        assert wait_until(lambda: port_in_use(port), 10)
        target = f"127.0.0.1:{port}"
        r = subprocess.run([CLIENT, target, "echo ok"], capture_output=True, timeout=30)
        assert r.returncode == 0 and r.stdout == b"ok\n"
        r = subprocess.run([CLIENT, target, "ls /"], capture_output=True, timeout=30)
        assert r.returncode == 126 and b"tools policy" in r.stderr and b"'ls'" in r.stderr
        r = subprocess.run([CLIENT, target, "--ping"], capture_output=True, timeout=30)
        assert b"allowed programs: echo, true" in r.stdout and b"denied programs:  ssh" in r.stdout
    finally:
        srv.terminate()
        srv.wait(10)


def test_cli_invalid_tools_file_fails_startup(tmp_path):
    r = subprocess.run([SERVER, "--port", "50099", "--tools", str(write(tmp_path / "t.yaml", "deny: [\n"))],
                       capture_output=True, timeout=30)
    assert r.returncode == 2 and b"invalid YAML" in r.stderr
    r = subprocess.run([SERVER, "--port", "50099", "--tools", str(tmp_path / "missing.yaml")],
                       capture_output=True, timeout=30)
    assert r.returncode == 2 and b"not found" in r.stderr
