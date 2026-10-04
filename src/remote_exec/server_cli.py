"""``remote-exec-server`` command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .cmdlog import DEFAULT_COMMAND_LOG, DEFAULT_MAX_MB, CommandLog
from .config import DEFAULT_HOST, DEFAULT_PORT, ConfigError, load_server_config, split_host_port, validate_port
from .policy import DEFAULT_TOOLS_FILE, PolicyError, PolicyProvider
from .security import DEFAULT_TOKEN_FILE, SecurityError, generate_token_file, is_loopback_host, read_secret_file
from .server import ServerSettings, serve

log = logging.getLogger("remote_exec.server")

DEFAULTS = {
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "max_concurrent": 32,
    "max_timeout": None,
    "shell": "",
    "kill_grace": 5.0,
    "log_level": "INFO",
    "log_file": None,
    "token_file": None,
    "tls_cert": None,
    "tls_key": None,
    "tls_client_ca": None,
    "insecure_no_auth": False,
    "allow_root": False,
    "listen": None,
    "tools": None,
    "command_log": DEFAULT_COMMAND_LOG,
    "command_log_max_mb": DEFAULT_MAX_MB,
    "command_log_keep": 0,
    "no_command_log": False,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remote-exec-server",
        description="Run the remote-exec gRPC server. Executes commands requested by "
        "remote-exec-client as the current (non-root) user.",
        epilog="Listening on a non-loopback address requires authentication "
        "(--token-file and/or --tls-client-ca) unless --insecure-no-auth is given.",
    )
    addr = p.add_argument_group("listen address")
    addr.add_argument("--host", help=f"address to bind (default: {DEFAULT_HOST}; use 0.0.0.0 for all interfaces)")
    addr.add_argument("--port", type=int, help=f"TCP port (default: {DEFAULT_PORT})")
    addr.add_argument("--listen", metavar="HOST:PORT", help="shorthand for --host and --port, e.g. 0.0.0.0:50051")
    p.add_argument("--config", metavar="FILE", help="YAML file with server settings (CLI options override it)")

    sec = p.add_argument_group("security")
    sec.add_argument("--token-file", metavar="FILE", help="file containing the shared bearer token clients must send")
    sec.add_argument("--tls-cert", metavar="FILE", help="server TLS certificate (PEM)")
    sec.add_argument("--tls-key", metavar="FILE", help="server TLS private key (PEM)")
    sec.add_argument("--tls-client-ca", metavar="FILE", help="CA for client certificates; enables mTLS")
    sec.add_argument("--insecure-no-auth", action="store_true", default=None,
                     help="allow listening on a non-loopback address without any authentication (NOT recommended)")
    sec.add_argument("--allow-root", action="store_true", default=None,
                     help="allow running as root (NOT recommended)")
    sec.add_argument("--generate-token", nargs="?", const=str(DEFAULT_TOKEN_FILE), metavar="FILE",
                     help=f"create a new random token file (mode 600) and exit; default FILE: {DEFAULT_TOKEN_FILE}")
    sec.add_argument("--force", action="store_true", default=None,
                     help="with --generate-token: replace an existing token file")

    ex = p.add_argument_group("execution")
    ex.add_argument("--tools", metavar="FILE",
                    help=f"tools.yaml with allowed/denied programs (default: {DEFAULT_TOOLS_FILE} if it exists, "
                    "else allow everything except ssh); reloaded automatically when changed")
    ex.add_argument("--shell", help="shell used as '<shell> -c <command>' (default: /bin/bash, else /bin/sh)")
    ex.add_argument("--max-concurrent", type=int, help="max concurrently running commands (default: 32)")
    ex.add_argument("--max-timeout", type=float, metavar="SECONDS",
                    help="upper bound for command timeouts; also applied to commands without a timeout")
    ex.add_argument("--kill-grace", type=float, metavar="SECONDS",
                    help="seconds between SIGTERM and SIGKILL when stopping a command (default: 5)")

    lg = p.add_argument_group("logging")
    lg.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR (default: INFO)")
    lg.add_argument("--log-file", metavar="FILE", help="also append logs to FILE (default: stderr only)")
    lg.add_argument("--command-log", metavar="FILE",
                    help=f"file recording every command and its output (default: ./{DEFAULT_COMMAND_LOG} "
                    "in the directory the server is started from)")
    lg.add_argument("--command-log-max-mb", type=float, metavar="MB",
                    help=f"start a new command log file when it would exceed MB (default: {DEFAULT_MAX_MB:g})")
    lg.add_argument("--command-log-keep", type=int, metavar="N",
                    help="keep only the N most recent rotated command logs (default: 0 = keep all)")
    lg.add_argument("--no-command-log", action="store_true", default=None, help="do not write a command log")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def resolve_settings(args: argparse.Namespace) -> tuple[ServerSettings, dict]:
    if args.listen and (args.host is not None or args.port is not None):
        raise ConfigError("--listen cannot be combined with --host/--port")
    # Precedence: defaults < config file < command line; "listen" expands to host+port.
    opts = dict(DEFAULTS)
    for layer in (load_server_config(args.config) if args.config else {}, vars(args)):
        layer = {k: v for k, v in layer.items() if v is not None and k != "config"}
        if layer.get("listen"):
            layer["host"], layer["port"] = split_host_port(str(layer.pop("listen")), DEFAULT_PORT)
        opts.update(layer)
    opts["port"] = validate_port(opts["port"])
    if opts["max_concurrent"] < 1:
        raise ConfigError("max_concurrent must be >= 1")
    if opts["max_timeout"] is not None and opts["max_timeout"] <= 0:
        raise ConfigError("max_timeout must be > 0")
    if bool(opts["tls_cert"]) != bool(opts["tls_key"]):
        raise ConfigError("--tls-cert and --tls-key must be given together")
    if opts["tls_client_ca"] and not opts["tls_cert"]:
        raise ConfigError("--tls-client-ca (mTLS) requires --tls-cert/--tls-key")
    if float(opts["command_log_max_mb"]) < 0.001:
        raise ConfigError("command_log_max_mb must be at least 0.001 (1 KiB)")
    if int(opts["command_log_keep"]) < 0:
        raise ConfigError("command_log_keep must be >= 0")
    command_log = None
    if not opts["no_command_log"] and opts["command_log"] not in (None, False, ""):
        # Relative paths are relative to the directory the server is started from.
        command_log = os.path.abspath(os.path.expanduser(str(opts["command_log"])))

    token = read_secret_file(opts["token_file"]) if opts["token_file"] else None
    settings = ServerSettings(
        host=str(opts["host"]), port=opts["port"], shell=opts["shell"] or "",
        max_concurrent=int(opts["max_concurrent"]), max_timeout=opts["max_timeout"],
        kill_grace=float(opts["kill_grace"]), token=token,
        tls_cert=opts["tls_cert"], tls_key=opts["tls_key"], tls_client_ca=opts["tls_client_ca"],
        tools_file=opts["tools"], tools_required=bool(opts["tools"]),
        command_log=command_log, command_log_max_mb=float(opts["command_log_max_mb"]),
        command_log_keep=int(opts["command_log_keep"]),
    )
    return settings, opts


def check_safety(settings: ServerSettings, opts: dict) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not opts["allow_root"]:
        raise SecurityError(
            "refusing to run as root: commands would execute with full privileges. "
            "Run as an unprivileged user (or pass --allow-root if you really mean it)."
        )
    authenticated = bool(settings.token or settings.tls_client_ca)
    if not is_loopback_host(settings.host):
        if not authenticated and not opts["insecure_no_auth"]:
            raise SecurityError(
                f"refusing to listen on non-loopback address {settings.host!r} without authentication. "
                "Configure --token-file and/or mTLS (--tls-cert/--tls-key/--tls-client-ca), "
                "or pass --insecure-no-auth on a fully trusted network."
            )
        if not authenticated:
            log.warning("listening on %s WITHOUT authentication: anyone who can reach this port can run commands", settings.host)
        if not settings.tls_cert:
            log.warning("TLS is disabled: traffic (including the token) is unencrypted; use only over a trusted/encrypted network such as WireGuard/Tailscale")


def setup_logging(level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(os.path.expanduser(log_file)))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


async def _run(settings: ServerSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await serve(settings, stop)


def do_generate_token(path: str, force: bool) -> int:
    try:
        target = generate_token_file(path, force=force)
    except SecurityError as exc:
        print(f"remote-exec-server: error: {exc}", file=sys.stderr)
        return 1
    print(f"token written to {target} (mode 600)")
    print("next steps:")
    print(f"  server: remote-exec-server --host 0.0.0.0 --token-file {target}")
    print("  client: copy this file to the client machine (e.g. ~/.config/remote-exec/token, chmod 600)")
    print("          and set 'token_file:' in the client config")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.generate_token:
        return do_generate_token(args.generate_token, bool(args.force))
    try:
        # Logging first so config warnings (e.g. token file permissions) are shown.
        setup_logging(args.log_level or "INFO", args.log_file)
        settings, opts = resolve_settings(args)
        setup_logging(opts["log_level"], opts["log_file"])
        check_safety(settings, opts)
        PolicyProvider(settings.tools_file, required=settings.tools_required)  # validate tools.yaml early
        if settings.command_log:  # fail now, not on the first command, if the log is not writable
            CommandLog(settings.command_log, int(settings.command_log_max_mb * 1024 * 1024)).close()
    except (ConfigError, SecurityError, PolicyError, OSError) as exc:
        print(f"remote-exec-server: error: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run(settings))
    except OSError as exc:
        print(f"remote-exec-server: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
