"""``remote-exec-client`` command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from . import __version__
from .client import (
    EXIT_CLIENT_ERROR,
    EXIT_INTERRUPTED,
    RemoteExecClient,
    RemoteExecError,
    ServerUnavailable,
)
from .config import ClientConfig, ConfigError, default_client_config_path, load_client_config
from .security import SecurityError

PROG = "remote-exec-client"

EPILOG = f"""\
exit status:
  N     the remote command's exit status (128+N if killed by signal N)
  124   the command timed out (--timeout)
  130   interrupted locally (Ctrl-C); the remote command is terminated
  255   remote-exec error: config, connection, authentication, or the command
        could not be started (message on stderr)

default config: {default_client_config_path()}  (override with --config or $REMOTE_EXEC_CONFIG)

examples:
  {PROG} fpga01 hostname
  {PROG} fpga01 --cwd /home/user/project "vivado -mode batch -source build.tcl"
  {PROG} fpga01 -e FPGA_PART=xcvu37p -e BUILD_MODE=release ./build.sh
  {PROG} gpu01 --timeout 60 -- nvidia-smi -L
  {PROG} --list
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Run a shell command on a remote-exec-server and stream its stdout/stderr, "
        "exiting with the remote exit status (like ssh).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("server", nargs="?", help="server name from the config, or HOST[:PORT] (default port 50051)")
    p.add_argument("command", nargs="*", help="command line; multiple words are joined with spaces (like ssh)")
    p.add_argument("-C", "--cwd", metavar="DIR", help="remote working directory (absolute, or ~/...)")
    p.add_argument("-e", "--env", metavar="KEY=VALUE", action="append", default=[],
                   help="set a remote environment variable (repeatable); bare KEY copies the local value")
    p.add_argument("-t", "--timeout", type=float, metavar="SECONDS",
                   help="kill the remote command (and its children) after SECONDS")
    p.add_argument("-c", "--config", metavar="FILE", help="client config file")
    p.add_argument("--connect-timeout", type=float, metavar="SECONDS",
                   help="max wait for the server to accept the command (default: 10)")
    p.add_argument("-v", "--verbose", action="store_true", help="print execution summary to stderr")
    p.add_argument("--list", action="store_true", help="list configured servers and exit")
    p.add_argument("--ping", action="store_true", help="check that SERVER is reachable and show its info")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def parse_env(items: list[str]) -> dict[str, str]:
    env = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not key:
            raise ConfigError(f"invalid --env {item!r}, expected KEY=VALUE")
        if not sep:
            if key not in os.environ:
                raise ConfigError(f"--env {key}: not set in the local environment")
            value = os.environ[key]
        env[key] = value
    return env


def err(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)


def _write(stream):
    def write(data: bytes) -> None:
        stream.write(data)
        stream.flush()
    return write


def list_servers(config: ClientConfig) -> int:
    if not config.servers:
        where = config.path or default_client_config_path()
        print(f"no servers configured (config: {where})")
        return 0
    width = max(len(n) for n in config.servers)
    for name, ep in config.servers.items():
        line = f"{name:<{width}}  {ep.address}"
        if ep.description:
            line += f"  {ep.description}"
        print(line)
    return 0


async def _ping(client: RemoteExecClient) -> int:
    info = await client.server_info()
    print(f"{client.endpoint.name}: ok host={info.hostname} user={info.user} version={info.version} shell={info.shell}")
    return 0


async def _execute(client: RemoteExecClient, args, command: str, env: dict[str, str]) -> int:
    result = await client.run(
        command, cwd=args.cwd, env=env, timeout=args.timeout,
        on_stdout=_write(sys.stdout.buffer), on_stderr=_write(sys.stderr.buffer),
    )
    name = client.endpoint.name
    if result.status == "timeout":
        err(f"{name}: {result.message or 'command timed out'}; remote process group terminated")
    elif result.status == "error":
        err(f"{name}: {result.message or 'remote execution failed'}")
    elif result.signal:
        err(f"{name}: remote command killed by signal {result.signal}")
    if args.verbose:
        dur = f"{result.duration:.3f}s" if result.duration is not None else "?"
        err(f"{name}: host={result.hostname} status={result.status} exit_code={result.exit_code} "
            f"duration={dur} id={result.execution_id}")
    return result.local_exit_code


async def _main_async(args, endpoint, command: str | None, env: dict[str, str]) -> int:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    # SIGTERM (e.g. from an agent's tool timeout) cancels the RPC cleanly so the
    # server kills the remote command right away.
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    async with RemoteExecClient(endpoint, connect_timeout=args.connect_timeout) as client:
        if args.ping:
            return await _ping(client)
        return await _execute(client, args, command, env)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_intermixed_args(argv)

    try:
        config = load_client_config(args.config)
        if args.list:
            return list_servers(config)
        if not args.server:
            parser.error("missing SERVER")
        command = " ".join(args.command)
        if not args.ping and not command.strip():
            parser.error("missing COMMAND")
        if args.timeout is not None and args.timeout <= 0:
            parser.error("--timeout must be > 0")
        env = parse_env(args.env)
        endpoint = config.resolve(args.server)
    except (ConfigError, SecurityError) as exc:
        err(f"error: {exc}")
        return EXIT_CLIENT_ERROR

    try:
        return asyncio.run(_main_async(args, endpoint, command, env))
    except KeyboardInterrupt:
        err("interrupted; remote command cancelled")
        return EXIT_INTERRUPTED
    except asyncio.CancelledError:
        err("terminated; remote command cancelled")
        return 128 + signal.SIGTERM
    except ServerUnavailable as exc:
        hint = ""
        if args.server not in config.servers and not any(c in args.server for c in ".:"):
            hint = f" ('{args.server}' is not a configured server name; config: {config.path or default_client_config_path()})"
        err(f"error: {exc}{hint}")
        return EXIT_CLIENT_ERROR
    except (RemoteExecError, SecurityError) as exc:
        err(f"error: {exc}")
        return EXIT_CLIENT_ERROR
    except BrokenPipeError:
        # Local stdout closed (e.g. piped into `head`); the RPC was cancelled.
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 141


if __name__ == "__main__":
    sys.exit(main())
