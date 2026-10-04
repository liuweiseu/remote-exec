---
description: Run commands on remote Linux servers that have specialised tools or hardware (FPGA tools, GPUs, licensed software, datasets) using remote-exec-client. Use when a task needs a tool or resource that is not available locally but exists on a configured remote server.
argument-hint: "[task or SERVER COMMAND]"
---

# Remote execution with `remote-exec-client`

`remote-exec-client` runs a shell command on a remote server and streams the
remote **stdout and stderr live**, like `ssh SERVER COMMAND`. Its exit status is
the remote command's exit status, so judge success exactly as for a local
command. You only ever need `remote-exec-client`; the servers are already set
up and running.

If this was invoked with arguments, the request is: $ARGUMENTS

## 1. Find out which servers exist

```bash
remote-exec-client --list            # NAME  HOST:PORT  description (tools/resources)
remote-exec-client SERVER --ping     # reachable? shows remote hostname and user
```

Pick the server whose description lists the tool or resource you need. If
none fits or the description is unclear, check with a harmless probe such as
`remote-exec-client SERVER "command -v vivado nvcc python3; nvidia-smi -L"`,
or ask the user. Do not guess server names that `--list` does not show.

## 2. When to run remotely

- The task needs a tool that is not installed locally (e.g. `vivado`, `vsim`,
  `nvcc`, `nvidia-smi`) and a server provides it.
- The task needs remote hardware (GPU, FPGA board), licenses, or data that live
  on a server.
- Do **not** use it for things that work locally (editing files, local `git`,
  reading code). A server sees only its own filesystem: project files must
  already exist there (git checkout, NFS, shared directory). If local changes
  are needed remotely, sync them first (e.g. commit/push, then `git pull`
  remotely) and tell the user you did so.

## 3. Usage

```bash
remote-exec-client SERVER [options] "COMMAND"
```

- Pass the command as **one quoted argument**. It runs as `bash -c "COMMAND"`
  on the server, so pipes, `&&`, globs and `$VARS` are evaluated remotely. Use
  single quotes locally when `$VAR` must not expand on your machine.
- `--cwd DIR` sets the remote working directory (absolute, or `'~/path'`).
  Prefer it over `cd DIR && ...`.
- `-e KEY=VALUE` sets a remote environment variable (repeatable); `-e KEY`
  copies the local value.
- `--timeout SECONDS` kills the remote command **and all its child processes**
  when exceeded. Always set one, sized to the job (e.g. `120` for a quick test,
  `7200` for a full FPGA implementation run).
- `-v` prints a summary line (host, status, exit code, duration) to stderr.
- stdin is not forwarded: commands must be non-interactive.
- Programs that buffer output when not on a TTY (e.g. Python) stream better
  with `-e PYTHONUNBUFFERED=1` or `python -u`.

Examples:

```bash
remote-exec-client gpu01 --timeout 30 "nvidia-smi"
remote-exec-client fpga01 --cwd /home/user/project --timeout 7200 \
    "vivado -mode batch -source build.tcl"
remote-exec-client fpga01 --cwd /home/user/project \
    -e FPGA_PART=xcvu37p -e BUILD_MODE=release --timeout 3600 "./build.sh"
remote-exec-client gpu01 --cwd '~/ml' -e PYTHONUNBUFFERED=1 --timeout 600 \
    "python train.py --epochs 1"
```

## 4. Interpreting the result

| exit status | meaning |
|-------------|---------|
| `0` | remote command succeeded |
| other `1..123`, `125..127` | remote command failed with that status; `127` = command not found on that server (wrong server or PATH) |
| `124` | `--timeout` exceeded; the remote process tree was killed. Output so far was printed. Increase the timeout only if the job was making progress |
| `128+N` | remote command killed by signal N (`137` = SIGKILL, often out of memory) |
| `130` / `143` | the client itself was interrupted; the remote command was cancelled |
| `255` | remote-exec problem, not the command: unreachable server, authentication failure, bad config, invalid `--cwd`. Read the message on stderr |

Lines starting with `remote-exec-client:` on stderr come from the client;
all other output comes from the remote command.

## 5. Long-running jobs and large output

- Keep output small: filter **remotely** inside the quoted command, e.g.
  `"make 2>&1 | tail -n 100"`, or write the full log to a remote file and
  inspect it with follow-up commands:
  `"vivado -mode batch -source build.tcl > build.log 2>&1; echo exit=$?; tail -n 100 build.log"`.
- If your own tool call has a time limit shorter than the job, set the remote
  `--timeout` below it so the remote side is cleaned up deterministically.
  For jobs longer than that, start them with output redirected to a log file
  and a generous `--timeout`, run the call in the background, and check
  progress with separate commands (`tail -n 50 build.log`).
- Interrupting or killing the client terminates the remote command.

## 6. Safety

- Commands run with the remote service user's permissions on shared machines.
  Do not run destructive commands (`rm -rf`, `git reset --hard`, `git clean`,
  killing other users' processes, editing system files, rebooting, flashing
  hardware) unless the user explicitly asked for exactly that. When in doubt,
  ask first.
- Inspect before changing anything (`ls`, `cat`, `git status`).
- Never put secrets (tokens, license keys, passwords) in commands or `-e`
  values: command lines are recorded in the server's audit log.
