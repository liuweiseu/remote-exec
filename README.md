# remote-exec

Lightweight remote command execution over gRPC: run a command on another Linux
server and get its **stdout / stderr streamed in real time** plus its **exit
code**, with an `ssh host cmd`-like experience.

## 1. Purpose

AI coding agents (Claude Code, Codex, ...) run on a local machine, but some
work needs tools or hardware that only exist on specific servers: Vivado and
Questa on an FPGA box, CUDA GPUs, licensed tools, large datasets.
`remote-exec` extends the agent's *tool execution* to those servers:

```bash
remote-exec-client fpga01 --cwd /home/user/project "vivado -mode batch -source build.tcl"
echo $?    # exit code of the remote vivado run
```

The remote execution layer is **agent-independent**. It contains no AI/LLM logic
and works the same for agents, shell scripts, CI and humans. Claude Code is just
one consumer (`remote-exec-client --install-skill`, see [Claude Code integration](#14-claude-code-integration)).

## 2. Architecture

```
Claude Code / script / human
        │  (Skill → Bash)
        ▼
remote-exec-client  ── client_cli.py → client.py (grpc.aio)
        │  gRPC server-streaming, optional TLS/mTLS + bearer token
        ▼
remote-exec-server  ── server_cli.py → server.py (grpc.aio servicer)
        │                                 │
        │                           executor.py (asyncio subprocess,
        ▼                            process groups, timeouts)
  bash -c "<command>"  (own session / process group)
        ├── vivado ── child processes
        ├── python / nvcc / git ...
```

One Python package provides **both** commands. Every machine that installs it
gets `remote-exec-server` and `remote-exec-client`; a machine can run either or
both (server A can call server B).

```
proto/remote_exec/v1/remote_exec.proto   API definition
src/remote_exec/
    v1/                 generated protobuf/gRPC code (committed)
    executor.py         subprocess management (no gRPC dependency)
    server.py           gRPC servicer, validation, audit log, lifecycle
    server_cli.py       remote-exec-server entry point
    client.py           async client library
    client_cli.py       remote-exec-client entry point
    config.py           client/server config loading
    security.py         token auth interceptor, TLS credentials
    skill.py            --install-skill implementation
    claude_skill.md     Claude Code skill text (installed by --install-skill)
tests/                  unit + localhost integration tests
examples/               config.yaml, server.yaml, systemd unit
scripts/gen_proto.sh    protobuf code generation
```

### API (`remote_exec.v1.RemoteExecutor`)

```proto
rpc Execute(ExecuteRequest) returns (stream ExecuteEvent);
rpc GetServerInfo(GetServerInfoRequest) returns (ServerInfo);

ExecuteRequest { command, working_directory, map environment, timeout_seconds }
ExecuteEvent   { oneof: started | stdout | stderr | finished }
Finished       { status: EXITED|TIMEOUT|ERROR, exit_code, signal,
                 start_time, end_time, message, execution_id }
```

Key design choices:

- **`oneof` events**: `Started` → any number of `stdout`/`stderr` chunks →
  exactly one `Finished`, always last. The type system enforces that each event
  carries only the fields that make sense for it.
- **Raw bytes, not lines**: progress bars without newlines appear immediately
  and non-UTF-8 output survives unchanged.
- **Two error channels**: problems with the *request* (empty command, invalid
  env name, auth, server busy) are gRPC status codes. What happens *after* the
  request is accepted (spawn failure, timeout, exit status) is reported in
  `Finished`.
- **Shell-style `exit_code`**: `128 + N` when killed by signal N, plus the raw
  `signal` field.
- **Versioned package** (`remote_exec.v1`), so `Submit/GetStatus/GetResult/
  Cancel` or `Upload/Download/Stat/List` can be added later as new RPCs or
  services without breaking existing clients.

## 3. Installing uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 4. Installing the tools (`uv tool install`)

```bash
uv tool install .                                   # from a checkout
uv tool install git+https://example.com/remote-exec.git   # from a git repo
uv tool install --force .                           # upgrade / reinstall
```

This puts `remote-exec-server` and `remote-exec-client` in `~/.local/bin`.

```bash
remote-exec-server --port 50051          # on the remote server
remote-exec-client fpga01 "hostname"     # on the local machine
```

## 5. Development environment

```bash
uv sync                 # creates .venv with runtime + dev deps (from uv.lock)
uv run pytest           # run tests
uv run remote-exec-server --help
```

Runtime dependencies: `grpcio`, `protobuf`, `pyyaml`. Dev: `grpcio-tools`,
`pytest`, `pytest-asyncio`.

## 6. Protobuf generation

The generated code in `src/remote_exec/v1/` is committed, so installing never
needs `protoc`. After editing the `.proto`, regenerate it with:

```bash
make proto      # or ./scripts/gen_proto.sh, which runs:
uv run python -m grpc_tools.protoc -I proto --python_out=src --pyi_out=src \
    --grpc_python_out=src proto/remote_exec/v1/remote_exec.proto
```

The proto lives at `proto/remote_exec/v1/` (rather than `proto/remote_exec.proto`)
so the generated `import` statements resolve to the `remote_exec.v1` package.
If you upgrade `grpcio-tools`, keep the `grpcio`/`protobuf` lower bounds in
`pyproject.toml` at least at the generator versions.

## 7. Server startup

```bash
remote-exec-server                       # 127.0.0.1:50051, local only
remote-exec-server --host 0.0.0.0 --port 50051 --token-file ~/.config/remote-exec/token
remote-exec-server --config /etc/remote-exec/server.yaml
```

Safety rules enforced at startup:

- **Default bind address is `127.0.0.1`**. Because the service is remote code
  execution, it must never be exposed by accident. To serve other machines you
  set `--host` explicitly.
- **Non-loopback addresses require authentication** (`--token-file` and/or mTLS).
  Otherwise the server refuses to start unless you pass `--insecure-no-auth`.
- **Refuses to run as root** unless you pass `--allow-root`.
- Warns when TLS is off on a non-loopback address.

Other options: `--shell` (default `/bin/bash`), `--max-concurrent` (32),
`--max-timeout`, `--kill-grace` (5s), `--log-level`, `--log-file`. See
`remote-exec-server --help` and [examples/server.yaml](examples/server.yaml).

## 8. Server port configuration

The default port is **50051**. Change it with any of:

```bash
remote-exec-server --port 50052
remote-exec-server --host 0.0.0.0 --port 50052
remote-exec-server --listen 0.0.0.0:50052        # same thing, one option
```

or `port:` / `listen:` in the server config file. CLI options override the file.

## 9. Client configuration

The default path is `~/.config/remote-exec/config.yaml`. It honours
`$XDG_CONFIG_HOME`, and `$REMOTE_EXEC_CONFIG` overrides it. You can also pass
`--config FILE`.

```yaml
defaults:                       # merged into every server
  token_file: ~/.config/remote-exec/token
servers:
  acme2:
    host: acme2.example.local
    port: 50051
  gpu01:
    host: gpu01.example.local   # no port -> 50051
    description: GPU computation
  fpga01:
    host: fpga01.example.local
    port: 50052
```

`remote-exec-client fpga01 ...` connects to `fpga01.example.local:50052` and
`gpu01` to `gpu01.example.local:50051`. A target that is not in the config is
treated as `HOST[:PORT]`, for example `remote-exec-client 10.0.0.5 hostname`.
Per-server keys: `host`, `port`, `description`, `token`, `token_file`, `tls`,
`ca_cert`, `client_cert`, `client_key`, `tls_server_name`, `connect_timeout`.
Unknown keys are rejected so a typo can't silently disable a setting.
`$REMOTE_EXEC_TOKEN` is used when no token is configured.

`remote-exec-client --list` shows the configured servers and
`remote-exec-client fpga01 --ping` checks connectivity.

## 10. Command execution

```bash
remote-exec-client acme2 "hostname"
remote-exec-client gpu01 "nvidia-smi"
remote-exec-client acme2 --cwd /home/user/project "python test.py"
remote-exec-client fpga01 --env FPGA_PART=xcvu37p --env BUILD_MODE=release "./build.sh"
remote-exec-client gpu01 --timeout 60 -- ls -la /data   # unquoted words joined like ssh
```

- The command runs as `bash -c "<command>"` on the server, with stdin set to
  `/dev/null`. It runs as the server's user, with the server's environment plus
  the `--env` values.
- `--cwd` must be absolute or start with `~` (expanded on the server). It is
  equivalent to `cd DIR && command`.
- `--timeout SECONDS` kills the whole remote process tree when it expires.

**Exit status** of `remote-exec-client`:

| status | meaning |
|--------|---------|
| N      | the remote command's exit status (`false` → 1, command not found → 127) |
| 128+N  | remote command killed by signal N |
| 124    | timeout (like coreutils `timeout`) |
| 130 / 143 | client interrupted by SIGINT / SIGTERM; remote command was cancelled |
| 255    | remote-exec error (like ssh): unreachable, auth, config, invalid cwd |

## 11. Streaming behaviour

Output is forwarded **as it is produced**, in chunks of up to 64 KiB, with no
line buffering on the server side. Remote stdout goes to local stdout and
remote stderr goes to local stderr. The relative order *within* each stream is
preserved; ordering *between* stdout and stderr is best-effort, as with ssh.

Programs that buffer their own output when not on a TTY (e.g. Python) may need
`python -u` or `PYTHONUNBUFFERED=1` (`-e PYTHONUNBUFFERED=1`).

Lifecycle guarantees:

- Each command runs in its own session/process group. On **timeout**, **client
  disconnect** (Ctrl-C, SIGTERM, killed client, lost network detected by gRPC
  keepalive) or **server shutdown**, the server sends SIGTERM to the *whole
  group*, then SIGKILL after `--kill-grace` seconds. Child processes (e.g.
  Vivado workers) are not left behind.
- Background processes the command leaves in its group are also terminated
  once the main command exits, so nothing is orphaned.
- A slow client applies back-pressure: the remote process blocks on a full
  pipe instead of the server buffering unbounded output.
- Keepalive pings keep silent multi-hour jobs alive through NAT/firewalls.

## 12. systemd setup

See [examples/remote-exec-server.service](examples/remote-exec-server.service).

```bash
sudo -u builder uv tool install /path/to/remote-exec
sudo install -m 644 examples/remote-exec-server.service /etc/systemd/system/
sudo install -d -o builder -m 700 /etc/remote-exec
sudo sh -c 'openssl rand -hex 32 > /etc/remote-exec/token'
sudo chown builder /etc/remote-exec/token && sudo chmod 600 /etc/remote-exec/token
# edit User=/ExecStart= paths in the unit
sudo systemctl daemon-reload
sudo systemctl enable remote-exec-server
sudo systemctl start remote-exec-server
journalctl -u remote-exec-server -f
```

The unit runs as an unprivileged user (`User=`). It shows how to set host,
port, token, config file and log level/file. On stop, the cgroup-based
`KillMode` also removes any process that escaped its process group.

## 13. Security considerations

This service **is remote code execution** by design. Anyone who can
authenticate can run any command as the service user.

- Bind to loopback by default. For remote use, prefer binding to a private
  interface (Tailscale/WireGuard IP) over `0.0.0.0`. Never expose the port to
  the internet; firewall it.
- **Authenticate even on private networks**:
  - *Bearer token*: `--token-file` on the server; `token_file` in the client
    config. The token is compared in constant time. Keep the file at mode 600
    (the server warns otherwise). Without TLS the token travels in clear text,
    which is fine inside WireGuard/Tailscale but not on a shared LAN.
  - *TLS*: `--tls-cert/--tls-key` on the server; `tls: true` + `ca_cert` on the
    client.
  - *mTLS* (recommended for LANs): add `--tls-client-ca` on the server, and
    `client_cert`/`client_key` on the client. The client certificate's CN
    appears in the audit log.
- Run as a dedicated unprivileged user; root is refused by default. Restrict
  what that user can access (groups, file permissions, sudo).
- **Audit log** (`remote_exec.audit` logger): one `start` record (id, client,
  cwd, timeout, env variable *names*, command) and one `end` record (status,
  exit code, duration) per command. Environment variable **values are never
  logged**. Command lines are logged, so don't put secrets in them.
- Input validation: non-empty command without NUL bytes, size limits, absolute
  cwd, POSIX env names, finite non-negative timeout, optional server-wide
  `--max-timeout` and `--max-concurrent` limits.

## 14. Claude Code integration

On the machine where Claude Code runs:

```bash
remote-exec-client --install-skill          # -> ~/.claude/commands/remote-exec.md
remote-exec-client --install-skill DIR      # -> DIR/remote-exec.md
remote-exec-client --install-skill --force  # overwrite a locally modified copy
```

This installs the `remote-exec` skill (a Claude Code custom command; source:
[src/remote_exec/claude_skill.md](src/remote_exec/claude_skill.md)). It covers
`remote-exec-client` only, since the servers are assumed to be running. It tells
Claude to:

- discover servers and their tools with `remote-exec-client --list`, and check
  them with `--ping`
- decide when remote execution is appropriate
- pass `--cwd` / `-e` / `--timeout`, interpret exit codes, and handle long jobs
  and large output
- avoid destructive commands

Describe each server's tools in the client config's `description` field, because
that is what `--list` shows Claude:

```yaml
servers:
  fpga01: {host: fpga01.example.local, description: "FPGA development: Vivado, Questa"}
  gpu01:  {host: gpu01.example.local,  description: "GPU computation: CUDA, nvidia-smi, Python"}
```

Claude then uses the skill on its own (or when you type `/remote-exec <task>`) and calls, e.g.,
`remote-exec-client fpga01 --cwd /home/user/project --timeout 7200 "vivado -mode batch -source build.tcl"`.
It reads the streamed output and the exit code. Re-run `--install-skill` after
upgrading the package to pick up a newer version of the skill.

## 15. Testing

```bash
uv run pytest            # ~20 s
```

- `tests/test_executor.py`: subprocess layer (streaming, exit codes, signals,
  cwd/env, timeout + SIGKILL escalation, process-group cleanup).
- `tests/test_grpc.py`: in-process server + client library (success,
  non-zero exit, stdout, stderr, mixed, cwd, env, timeout, command not found,
  real-time streaming, concurrency, client disconnect/cancel, server
  unavailable, invalid requests, max concurrency, token auth, mTLS).
- `tests/test_cli.py`: the actual console scripts as subprocesses. Covers the
  **default port 50051** (`remote-exec-server --host 127.0.0.1` +
  `remote-exec-client local "echo hello"`), the **custom port 50052**, exit
  codes, timeout → 124, unavailable → 255, SIGINT/SIGTERM/SIGKILL of the client
  killing the remote process tree, server shutdown, and refusal of
  unauthenticated public binds. The fixed-port tests are skipped if the port is
  already in use.
- `tests/test_config.py`: config parsing, default/custom ports, server option
  precedence, safety checks.
- `tests/test_skill.py`: `--install-skill` (default/custom dir, update, `--force`).

## 16. Future extensions

Kept out of v1 on purpose, but the structure is ready for them:

- **Asynchronous jobs** (`Submit`, `GetStatus`, `GetResult`, `Cancel`): the
  `Executor` already produces a gRPC-independent event stream with an
  `execution_id`. A small in-memory task registry could consume it in a
  background task, and the new RPCs could read from it.
- **File RPCs** (`Upload`, `Download`, `Stat`, `List`) as a sibling service in
  `remote_exec.v1`, sharing the same auth/TLS setup.
- Per-client authorization (map mTLS CN or token → allowed commands/dirs).
- Optional PTY mode and stdin forwarding (bidirectional streaming).
- Agent-to-agent communication is explicitly **not** a goal of this layer.
