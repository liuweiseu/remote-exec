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
    policy.py           tools.yaml allow/deny policy and command analysis
    cmdlog.py           command log (commands + output, size-based rotation)
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
uv tool install remote-exec-grpc                    # from PyPI
uv tool install .                                   # from a checkout
uv tool install git+https://example.com/remote-exec.git   # from a git repo
uv tool install --force .                           # upgrade / reinstall
```

This puts `remote-exec-server` and `remote-exec-client` in `~/.local/bin`.
The PyPI distribution is named `remote-exec-grpc` (`remote-exec` is taken by an
unrelated project); the import package and command names stay `remote_exec` /
`remote-exec-*`.

```bash
remote-exec-server --port 50051          # on the remote server
remote-exec-client fpga01 "hostname"     # on the local machine
remote-exec-client --install-skill       # optional: let Claude Code use it (see §14)
```

`fpga01` is a server name defined in the client config file
(`~/.config/remote-exec/config.yaml`), which maps it to a host and port; see
[§9 Client configuration](#9-client-configuration). `remote-exec-client --list`
shows all configured names and their addresses.

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
remote-exec-server --generate-token      # create ~/.config/remote-exec/token (mode 600)
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

### Allowed programs (`tools.yaml`)

`~/.config/remote-exec/tools.yaml` on the server lists the programs that
commands may and may not run. Use `--tools FILE` or `tools:` in the server
config to load it from elsewhere. See [examples/tools.yaml](examples/tools.yaml).

```yaml
allow: ["*"]        # "*" = every program; globs like "python*" work
deny: [ssh]         # deny wins if a program matches both lists
```

- **Default:** without the file, or for a missing key, the server uses
  `allow: ["*"]` and `deny: ["ssh"]`, so everything except `ssh` is allowed.
  `deny: []` denies nothing.
- **What is checked:** every program a command line would start. That covers
  each part of `|`, `&&`, `;` and newlines, `$(...)` and backticks, wrappers
  such as `sudo`, `env`, `timeout` and `nohup`, and scripts given to
  `bash -c` / `eval`. So `ssh h`, `/usr/bin/ssh h`, `make && ssh h`,
  `echo $(ssh h)` and `bash -c "ssh h"` are all rejected. `echo ssh` and
  `grep ssh log` are not, because there `ssh` is only an argument.
- **How patterns match:** a pattern without `/` matches the program's base
  name; a pattern with `/` matches the path as written. Shell builtins (`cd`,
  `echo`, `export`, `test`, ...) never need to be in `allow`.
- **Rejections:** a rejected command is not started. The client exits with
  **126** and prints the reason, and the audit log records it.
  `remote-exec-client SERVER --ping` shows the active lists.
- **Reloading:** edits take effect on the next command, with no restart. An
  invalid file is rejected at startup; if a later edit breaks the file, the
  previous policy stays active and an error is logged.
- **Limits:** this is a guardrail against mistakes (e.g. an agent hopping to
  other machines), **not a sandbox**. Programs started indirectly, such as by
  a script, `python -c`, or `X=ssh; $X`, are invisible to it. Use OS-level
  controls (the service user's permissions, firewall rules) for hard limits.

### Command log

The server writes every command it runs, together with its output, to
`remote-exec-commands.log` in **the directory it was started from**. Under
systemd that is the unit's `WorkingDirectory=`. Each line carries the
execution id, so concurrent commands can be told apart:

```
2026-10-04 10:00:00.120 [3f2a9c1d0b7e] START client=ipv4:10.0.0.7:51234 cwd="/proj" timeout=0 env_keys="" command="make"
2026-10-04 10:00:00.250 [3f2a9c1d0b7e] OUT| gcc -c main.c
2026-10-04 10:00:00.260 [3f2a9c1d0b7e] ERR| warning: unused variable
2026-10-04 10:00:01.900 [3f2a9c1d0b7e] END status=exited exit_code=0 duration=1.780s
2026-10-04 10:00:02.010 [------------] REJECTED client=... reason="tools policy: ..." command="ssh other"
```

- **Rotation:** when the file would exceed **10 MB**, it is renamed to
  `remote-exec-commands.<YYYYmmdd-HHMMSS>.log` and a new file is started.
- **Retention:** rotated files are **all kept** by default; use
  `--command-log-keep N` to keep only the newest N, since long tool output can
  add up quickly.
- **Options:** `--command-log FILE` (another location), `--command-log-max-mb MB`
  and `--no-command-log`. The config keys are `command_log`,
  `command_log_max_mb`, `command_log_keep` and `no_command_log`.
- **Privacy:** files are created with mode 600. Environment variable values
  are not logged, but command output is, so treat the files as sensitive.
- **Reliability:** a full disk or other write error is reported once in the
  server log and never interrupts commands.
- This is separate from the audit log (`remote_exec.audit`, stderr/journald
  or `--log-file`), which records only command metadata.

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

### One token per server

Each server generates its own token (`remote-exec-server --generate-token`).
On the client, keep one token file per server and point each server entry at
its own file with `token_file`:

```yaml
servers:
  fpga01:
    host: fpga01.example.local
    token_file: ~/.config/remote-exec/tokens/fpga01.token
  gpu01:
    host: gpu01.example.local
    token_file: ~/.config/remote-exec/tokens/gpu01.token
```

`remote-exec-client fpga01 ...` then sends only `fpga01.token`, and only to
fpga01. Separate tokens limit the damage if one leaks: it can be rotated on
that server without touching the others.

Fetch each server's token into its own file, then check every server:

```bash
mkdir -p ~/.config/remote-exec/tokens && chmod 700 ~/.config/remote-exec/tokens
( umask 077   # files are created with mode 600
  ssh fpga01 cat ~/.config/remote-exec/token > ~/.config/remote-exec/tokens/fpga01.token
  ssh gpu01  cat ~/.config/remote-exec/token > ~/.config/remote-exec/tokens/gpu01.token
)
remote-exec-client fpga01 --ping     # "authentication failed" = token mismatch
remote-exec-client gpu01 --ping
```

For each server the client takes the first token it finds, in this order:

1. `token:` in that server's entry (the token string itself)
2. `token_file:` in that server's entry
3. `token` / `token_file` from `defaults:`. A server's own keys override
   `defaults`.
4. the `$REMOTE_EXEC_TOKEN` environment variable

So a shared token in `defaults` and per-server exceptions can be mixed:

```yaml
defaults:
  token_file: ~/.config/remote-exec/tokens/shared.token   # used by gpu01
servers:
  gpu01:
    host: gpu01.example.local
  fpga01:
    host: fpga01.example.local
    token_file: ~/.config/remote-exec/tokens/fpga01.token  # overrides defaults
```

A configured `token_file` that does not exist makes the client exit with 255.
Keep token files at mode 600; the client warns about group/world-readable
token files.

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
| 126    | rejected by the server's tools policy (`tools.yaml`) |
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
sudo -u builder /home/builder/.local/bin/remote-exec-server --generate-token /etc/remote-exec/token
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
    (the server warns otherwise).
    `remote-exec-server --generate-token [FILE]` creates a random 256-bit token.
    The default FILE is `~/.config/remote-exec/token`. The file gets mode 600
    and its directory 700, and the token itself is never printed. An existing
    file is only replaced with `--force`, because that locks out clients still
    using the old token. Copy the file to each client (e.g. `scp`, then
    `chmod 600`) and point `token_file` at it. Without TLS the token travels in clear text,
    which is fine inside WireGuard/Tailscale but not on a shared LAN.
  - *TLS*: `--tls-cert/--tls-key` on the server; `tls: true` + `ca_cert` on the
    client.
  - *mTLS* (recommended for LANs): add `--tls-client-ca` on the server, and
    `client_cert`/`client_key` on the client. The client certificate's CN
    appears in the audit log.
- Run as a dedicated unprivileged user; root is refused by default. Restrict
  what that user can access (groups, file permissions, sudo).
- Limit which programs commands may run with `tools.yaml` (see §7). By
  default `ssh` is denied so commands cannot hop to other machines. It is a
  guardrail, not a sandbox.
- **Command log** (`remote-exec-commands.log`, see §7) contains full command
  output. It is created with mode 600; restrict who can read the server's
  working directory.
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
- `tests/test_token.py`: `--generate-token` (permissions, randomness, `--force`,
  generated token authenticates).
- `tests/test_policy.py`: `tools.yaml` (command analysis, deny-wins, globs,
  defaults, reload, enforcement, exit 126).
- `tests/test_cmdlog.py`: command log (format, partial/long lines, rotation,
  retention, permissions, default location, disabling).

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
