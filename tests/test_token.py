from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

from remote_exec.client import RemoteExecClient, RemoteExecError
from remote_exec.security import DEFAULT_TOKEN_FILE, SecurityError, generate_token_file, read_secret_file

SERVER = str(Path(sys.executable).parent / "remote-exec-server")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_location():
    assert DEFAULT_TOKEN_FILE == Path("~/.config/remote-exec/token")


def test_generate_creates_private_file_and_dir(tmp_path):
    path = generate_token_file(tmp_path / "remote-exec" / "token")
    token = read_secret_file(path)
    assert len(token) == 64 and int(token, 16) >= 0
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["token"]  # no temp files left


def test_tokens_are_random(tmp_path):
    a = read_secret_file(generate_token_file(tmp_path / "a"))
    b = read_secret_file(generate_token_file(tmp_path / "b"))
    assert a != b


def test_existing_file_requires_force(tmp_path):
    path = generate_token_file(tmp_path / "token")
    old = path.read_text()
    with pytest.raises(SecurityError, match="--force"):
        generate_token_file(path)
    assert path.read_text() == old
    generate_token_file(path, force=True)
    assert path.read_text() != old and mode(path) == 0o600


def test_cli_generate_token_default_path(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([SERVER, "--generate-token"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    target = tmp_path / ".config" / "remote-exec" / "token"
    assert len(read_secret_file(target)) == 64 and mode(target) == 0o600
    assert str(target).encode() in r.stdout
    assert read_secret_file(target).encode() not in r.stdout  # token itself is not printed

    r = subprocess.run([SERVER, "--generate-token"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 1 and b"--force" in r.stderr
    r = subprocess.run([SERVER, "--generate-token", "--force"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0


def test_cli_generate_token_custom_path(tmp_path):
    r = subprocess.run([SERVER, "--generate-token", str(tmp_path / "x.token")], capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert mode(tmp_path / "x.token") == 0o600


async def test_generated_token_authenticates(server_factory, tmp_path):
    token = read_secret_file(generate_token_file(tmp_path / "token"))
    _, ep = await server_factory(token=token)
    ep.token_file = str(tmp_path / "token")
    async with RemoteExecClient(ep) as client:
        result = await client.run("true")
    assert result.ok
    ep.token_file, ep.token = None, "wrong"
    with pytest.raises(RemoteExecError, match="authentication failed"):
        async with RemoteExecClient(ep) as client:
            await client.run("true")
