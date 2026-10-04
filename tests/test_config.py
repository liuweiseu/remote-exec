from __future__ import annotations

import pytest

from remote_exec import server_cli
from remote_exec.config import (
    DEFAULT_PORT,
    ConfigError,
    load_client_config,
    load_server_config,
    split_host_port,
)
from remote_exec.security import SecurityError


def write(tmp_path, text, name="config.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_default_port_constant():
    assert DEFAULT_PORT == 50051


def test_client_config_ports(tmp_path):
    cfg = load_client_config(write(tmp_path, """
servers:
  fpga01:
    host: fpga01.example.local
  fpga02:
    host: fpga02.example.local
    port: 50052
    description: FPGA builds
"""))
    assert cfg.resolve("fpga01").address == "fpga01.example.local:50051"
    assert cfg.resolve("fpga02").address == "fpga02.example.local:50052"
    assert cfg.resolve("fpga02").description == "FPGA builds"


def test_defaults_are_merged(tmp_path):
    cfg = load_client_config(write(tmp_path, """
defaults:
  token_file: ~/tok
  port: 6000
servers:
  a: {host: a.local}
  b: {host: b.local, port: 7000, token_file: /etc/b.tok}
"""))
    a, b = cfg.resolve("a"), cfg.resolve("b")
    assert a.port == 6000 and a.token_file.endswith("/tok") and not a.token_file.startswith("~")
    assert b.port == 7000 and b.token_file == "/etc/b.tok"


def test_unconfigured_target_is_host_port(tmp_path):
    cfg = load_client_config(write(tmp_path, "servers: {}\n"))
    assert cfg.resolve("10.0.0.5").address == "10.0.0.5:50051"
    assert cfg.resolve("10.0.0.5:6000").address == "10.0.0.5:6000"


@pytest.mark.parametrize("text, msg", [
    ("servers:\n  a: {port: 1}\n", "host"),
    ("servers:\n  a: {host: x, prot: 1}\n", "unknown"),
    ("servers:\n  a: {host: x, port: 70000}\n", "range"),
    ("servers:\n  a: {host: x, port: abc}\n", "invalid port"),
    ("server:\n  a: {host: x}\n", "unknown top-level"),
    ("servers: [\n", "invalid YAML"),
])
def test_client_config_errors(tmp_path, text, msg):
    with pytest.raises(ConfigError, match=msg):
        load_client_config(write(tmp_path, text))


def test_missing_default_config_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("REMOTE_EXEC_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = load_client_config()
    assert cfg.servers == {} and cfg.path is None


def test_default_config_location(tmp_path, monkeypatch):
    monkeypatch.delenv("REMOTE_EXEC_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "remote-exec").mkdir()
    write(tmp_path / "remote-exec", "servers:\n  x: {host: h}\n")
    assert load_client_config().resolve("x").host == "h"


def test_missing_explicit_config_is_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_client_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize("value, expected", [
    ("host", ("host", 50051)),
    ("host:1234", ("host", 1234)),
    ("[::1]:99", ("::1", 99)),
    ("[::1]", ("::1", 50051)),
    ("::1", ("::1", 50051)),
])
def test_split_host_port(value, expected):
    assert split_host_port(value) == expected


# ---------------------------------------------------------------- server CLI settings


def settings_for(argv):
    args = server_cli.build_parser().parse_args(argv)
    return server_cli.resolve_settings(args)


def test_server_default_port_and_host():
    settings, _ = settings_for([])
    assert settings.port == 50051
    assert settings.host == "127.0.0.1"


def test_server_custom_port():
    settings, _ = settings_for(["--port", "50052"])
    assert settings.port == 50052


def test_server_listen():
    settings, _ = settings_for(["--listen", "0.0.0.0:6000"])
    assert (settings.host, settings.port) == ("0.0.0.0", 6000)
    with pytest.raises(ConfigError):
        settings_for(["--listen", "0.0.0.0:6000", "--port", "1"])


def test_server_config_file_and_override(tmp_path):
    tok = write(tmp_path, "tok123\n", "token")
    cfg = write(tmp_path, f"host: 0.0.0.0\nport: 6001\ntoken_file: {tok}\nmax_concurrent: 4\n", "server.yaml")
    settings, _ = settings_for(["--config", str(cfg)])
    assert (settings.host, settings.port, settings.token, settings.max_concurrent) == ("0.0.0.0", 6001, "tok123", 4)
    settings, _ = settings_for(["--config", str(cfg), "--port", "6002"])
    assert (settings.host, settings.port) == ("0.0.0.0", 6002)


def test_server_config_unknown_key(tmp_path):
    with pytest.raises(ConfigError, match="unknown"):
        load_server_config(write(tmp_path, "hots: x\n"))


def test_refuse_public_bind_without_auth():
    settings, opts = settings_for(["--host", "0.0.0.0"])
    with pytest.raises(SecurityError, match="authentication"):
        server_cli.check_safety(settings, opts)
    settings, opts = settings_for(["--host", "0.0.0.0", "--insecure-no-auth"])
    server_cli.check_safety(settings, opts)  # explicit opt-out


def test_public_bind_with_token_ok(tmp_path):
    tok = write(tmp_path, "abc", "token")
    settings, opts = settings_for(["--host", "0.0.0.0", "--token-file", str(tok)])
    server_cli.check_safety(settings, opts)


def test_loopback_without_auth_ok():
    settings, opts = settings_for([])
    server_cli.check_safety(settings, opts)


def test_refuse_root(monkeypatch):
    monkeypatch.setattr(server_cli.os, "geteuid", lambda: 0)
    settings, opts = settings_for([])
    with pytest.raises(SecurityError, match="root"):
        server_cli.check_safety(settings, opts)
    settings, opts = settings_for(["--allow-root"])
    server_cli.check_safety(settings, opts)


EXAMPLES = __import__("pathlib").Path(__file__).parent.parent / "examples"


def test_example_configs_are_valid():
    cfg = load_client_config(EXAMPLES / "config.yaml")
    assert cfg.resolve("gpu01").port == 50051
    assert cfg.resolve("fpga01").port == 50052
    server = load_server_config(EXAMPLES / "server.yaml")
    assert server["port"] == 50051
    from remote_exec.policy import ToolPolicy, load_policy

    tools = load_policy(EXAMPLES / "tools.yaml")
    assert (tools.allow, tools.deny) == (ToolPolicy().allow, ToolPolicy().deny)


def test_server_token_overrides_default_token(tmp_path):
    cfg = load_client_config(write(tmp_path, """
defaults:
  token: shared-secret
servers:
  a: {host: a.local}
  b: {host: b.local, token_file: /etc/b.token}
  c: {host: c.local, token: own}
"""))
    a, b, c = cfg.resolve("a"), cfg.resolve("b"), cfg.resolve("c")
    assert (a.token, a.token_file) == ("shared-secret", None)
    assert (b.token, b.token_file) == (None, "/etc/b.token")  # default token must not win
    assert (c.token, c.token_file) == ("own", None)


async def test_each_server_uses_its_own_token_file(server_factory, tmp_path):
    from remote_exec.client import RemoteExecClient, RemoteExecError

    (tmp_path / "a.token").write_text("token-a\n")
    (tmp_path / "b.token").write_text("token-b\n")
    _, ep_a = await server_factory(token="token-a")
    _, ep_b = await server_factory(token="token-b")
    cfg = load_client_config(write(tmp_path, f"""
servers:
  a: {{host: 127.0.0.1, port: {ep_a.port}, token_file: {tmp_path}/a.token}}
  b: {{host: 127.0.0.1, port: {ep_b.port}, token_file: {tmp_path}/b.token}}
  wrong: {{host: 127.0.0.1, port: {ep_b.port}, token_file: {tmp_path}/a.token}}
"""))
    for name in ("a", "b"):
        async with RemoteExecClient(cfg.resolve(name)) as client:
            assert (await client.run("true")).ok
    with pytest.raises(RemoteExecError, match="authentication failed"):
        async with RemoteExecClient(cfg.resolve("wrong")) as client:
            await client.run("true")
