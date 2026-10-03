"""Configuration loading.

Client config (YAML), default location ``$REMOTE_EXEC_CONFIG`` or
``${XDG_CONFIG_HOME:-~/.config}/remote-exec/config.yaml``::

    defaults:              # optional, merged into every server entry
      token_file: ~/.config/remote-exec/token
    servers:
      fpga01:
        host: fpga01.example.local
        port: 50052        # optional, defaults to 50051
        description: FPGA builds (Vivado, Questa)

Server config (YAML, only with ``remote-exec-server --config``) uses the long
option names of the server CLI as keys, e.g. ``host``, ``port``, ``token_file``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = 50051
DEFAULT_HOST = "127.0.0.1"
CONFIG_ENV_VAR = "REMOTE_EXEC_CONFIG"
TOKEN_ENV_VAR = "REMOTE_EXEC_TOKEN"


class ConfigError(Exception):
    pass


def default_client_config_path() -> Path:
    if env := os.environ.get(CONFIG_ENV_VAR):
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "remote-exec" / "config.yaml"


def validate_port(value: Any, where: str = "port") -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: invalid port {value!r}")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: invalid port {value!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}: port {port} out of range 1-65535")
    return port


def split_host_port(value: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Parse ``host``, ``host:port``, ``[v6]`` or ``[v6]:port``."""
    value = value.strip()
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep:
            raise ConfigError(f"invalid address {value!r}")
        if not rest:
            return host, default_port
        if not rest.startswith(":"):
            raise ConfigError(f"invalid address {value!r}")
        return host, validate_port(rest[1:], value)
    if value.count(":") == 1:
        host, port = value.split(":")
        return host, validate_port(port, value)
    return value, default_port  # plain host or bare IPv6


def format_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


# ---------------------------------------------------------------- client


@dataclass
class ServerEndpoint:
    name: str
    host: str
    port: int = DEFAULT_PORT
    description: str = ""
    token: str | None = None
    token_file: str | None = None
    tls: bool = False
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    tls_server_name: str | None = None
    connect_timeout: float | None = None

    @property
    def address(self) -> str:
        return format_address(self.host, self.port)

    @property
    def uses_tls(self) -> bool:
        return self.tls or bool(self.ca_cert or self.client_cert)


_ENDPOINT_KEYS = {f.name for f in fields(ServerEndpoint)} - {"name"}


def _make_endpoint(name: str, raw: Any, defaults: dict, where: str) -> ServerEndpoint:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: server entry must be a mapping")
    merged = {**defaults, **raw}
    unknown = set(merged) - _ENDPOINT_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")
    if not merged.get("host"):
        raise ConfigError(f"{where}: 'host' is required")
    merged["host"] = str(merged["host"])
    merged["port"] = validate_port(merged.get("port", DEFAULT_PORT), f"{where}.port")
    for key in ("token_file", "ca_cert", "client_cert", "client_key"):
        if merged.get(key):
            merged[key] = os.path.expanduser(str(merged[key]))
    return ServerEndpoint(name=name, **merged)


@dataclass
class ClientConfig:
    servers: dict[str, ServerEndpoint] = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    path: Path | None = None

    def resolve(self, target: str) -> ServerEndpoint:
        """Look ``target`` up by name; otherwise treat it as ``host[:port]``."""
        if target in self.servers:
            return self.servers[target]
        if not target:
            raise ConfigError("empty server name")
        host, port = split_host_port(target)
        defaults = {k: v for k, v in self.defaults.items() if k != "port"}
        return _make_endpoint(target, {**defaults, "host": host, "port": port}, {}, f"server {target!r}")


def load_client_config(path: str | os.PathLike | None = None) -> ClientConfig:
    """Load the client config. A missing *default* config is not an error."""
    explicit = path is not None
    cfg_path = Path(path).expanduser() if explicit else default_client_config_path()
    if not cfg_path.exists():
        if explicit or os.environ.get(CONFIG_ENV_VAR):
            raise ConfigError(f"config file not found: {cfg_path}")
        return ClientConfig(path=None)

    data = _load_yaml(cfg_path)
    unknown = set(data) - {"servers", "defaults"}
    if unknown:
        raise ConfigError(f"{cfg_path}: unknown top-level key(s): {', '.join(sorted(unknown))}")
    defaults = data.get("defaults") or {}
    servers_raw = data.get("servers") or {}
    if not isinstance(defaults, dict) or not isinstance(servers_raw, dict):
        raise ConfigError(f"{cfg_path}: 'defaults' and 'servers' must be mappings")
    servers = {
        str(name): _make_endpoint(str(name), raw, defaults, f"{cfg_path}: servers.{name}")
        for name, raw in servers_raw.items()
    }
    return ClientConfig(servers=servers, defaults=defaults, path=cfg_path)


# ---------------------------------------------------------------- server


SERVER_CONFIG_KEYS = {
    "host", "port", "listen", "token_file", "tls_cert", "tls_key", "tls_client_ca",
    "max_concurrent", "max_timeout", "shell", "kill_grace", "log_level", "log_file",
    "insecure_no_auth", "allow_root",
}


def load_server_config(path: str | os.PathLike) -> dict[str, Any]:
    cfg_path = Path(path).expanduser()
    data = _load_yaml(cfg_path)
    data = {k.replace("-", "_"): v for k, v in data.items()}
    unknown = set(data) - SERVER_CONFIG_KEYS
    if unknown:
        raise ConfigError(f"{cfg_path}: unknown key(s): {', '.join(sorted(unknown))}")
    if "port" in data:
        data["port"] = validate_port(data["port"], f"{cfg_path}: port")
    return data
