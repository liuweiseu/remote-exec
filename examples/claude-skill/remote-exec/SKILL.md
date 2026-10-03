---
name: remote-exec
description: Run commands on remote Linux servers that have specialised tools or hardware (Vivado/Questa FPGA tools on fpga01, CUDA GPUs on gpu01, general builds on acme2) via the remote-exec-client CLI. Use when a task needs a tool, license, GPU, or dataset that only exists on one of those machines.
---

# Remote execution with `remote-exec-client`

`remote-exec-client` runs a shell command on a remote server through a
`remote-exec-server` daemon and streams the remote **stdout and stderr live**,
exactly like `ssh SERVER COMMAND`. Its exit status is the remote command's exit
status, so you can judge success the same way as for a local command.

## Available servers

<!-- Edit this table for your environment; `remote-exec-client --list` shows the configured ones. -->

| server   | purpose              | tools / resources                     | notes                         |
|----------|----------------------|----------------------------------------|-------------------------------|
| `fpga01` | FPGA development     | Vivado, Questa                         | projects under `/home/user/fpga` |
| `gpu01`  | GPU computation      | CUDA, nvidia-smi, Python (conda/uv)    | 4x GPUs                       |
| `acme2`  | general builds/tests | gcc, make, git, Python                 |                               |

Check reachability first if unsure: `remote-exec-client --ping fpga01`.

## When to use it

- The task needs a tool that is not installed locally (e.g. `vivado`, `vsim`,
  `nvcc`, `nvidia-smi`) and the table above lists a server that has it.
- The task needs remote hardware (GPU, FPGA board) or data that lives there.
- Do **not** use it for things that work locally (editing files, `git` on the
  local checkout, reading code). Remote servers see their own filesystem only:
  project files must already exist there (git checkout, NFS, shared dir).
  If your local changes are needed remotely, sync them first (e.g. push and
  `git pull` remotely) and say so.

## Usage

```bash
remote-exec-client SERVER [options] "COMMAND"
```

- Quote the command as one argument. It runs as `bash -c "COMMAND"` on the
  server, so pipes, `&&`, `;`, globs and `$VARS` are evaluated **remotely**.
  Use single quotes locally if `$VAR` must not expand on your machine.
- Working directory: `--cwd /abs/path` (or `--cwd '~/project'`). Prefer this
  over `cd x && ...`. A non-existent directory fails with exit status 255.
- Environment variables: `-e KEY=VALUE` (repeatable). `-e KEY` copies the local
  value.
- Timeout: `--timeout SECONDS` kills the command **and all its child processes**
  when exceeded. Always set one, sized to the job (e.g. `--timeout 120` for a
  quick test, `--timeout 7200` for a full Vivado implementation).
- `-v` prints a summary line (host, status, exit code, duration) to stderr.

Examples:

```bash
remote-exec-client gpu01 "nvidia-smi"
remote-exec-client fpga01 --cwd /home/user/project --timeout 7200 \
    "vivado -mode batch -source build.tcl"
remote-exec-client fpga01 --cwd /home/user/project \
    -e FPGA_PART=xcvu37p -e BUILD_MODE=release --timeout 3600 "./build.sh"
remote-exec-client gpu01 --cwd ~/ml --timeout 600 "python train.py --epochs 1"
```

## Interpreting results

| exit status | meaning |
|-------------|---------|
| `0`         | remote command succeeded |
| `1..123`, `125..127` | remote command failed with that status (`127` = command not found on the server: check PATH or the server choice) |
| `124`       | `--timeout` exceeded; the remote process tree was killed. Output so far was printed. Re-run with a larger timeout only if the job was making progress |
| `128+N`     | remote command killed by signal N (e.g. 137 = SIGKILL, often OOM) |
| `130`/`143` | the client itself was interrupted; the remote command was cancelled |
| `255`       | remote-exec problem, not the command: server unreachable, auth failure, bad config, invalid cwd. Read the `remote-exec-client:` message on stderr |

Lines prefixed with `remote-exec-client:` on stderr come from the client, all
other output comes from the remote command.

## Long-running jobs

- Output streams in real time; long tool logs can be large. Pipe through
  `tail -n 200` or `grep` **remotely** (inside the quoted command) to keep only
  what you need, or write the full log to a file remotely and inspect it with
  follow-up commands, e.g.
  `"vivado -mode batch -source build.tcl > build.log 2>&1; tail -n 100 build.log"`.
- If your own tool call has a time limit shorter than the job, keep the remote
  `--timeout` below it so the remote side is cleaned up deterministically.
- Disconnecting (Ctrl-C / killing the client) terminates the remote command.

## Safety

- Commands run with the remote service user's full permissions on shared
  machines. Avoid destructive commands (`rm -rf`, `git reset --hard`,
  `git clean`, killing other users' processes, modifying system files,
  rebooting, flashing hardware) unless the user explicitly asked for exactly
  that; when in doubt, ask first.
- Prefer read-only inspection (`ls`, `cat`, `git status`) before changing
  anything remotely.
- Do not print secrets (tokens, license keys) in commands or environment
  variables; they appear in the server audit log (environment variable
  *names* are logged, values are not).
