# remote-exec

[English](README.md) | 中文

基于 gRPC 的轻量级远程命令执行工具：在另一台 Linux 服务器上运行命令，**实时流式**获取其
**stdout / stderr** 以及**退出码**，使用体验类似 `ssh host cmd`。

## 1. 项目目的

AI Coding Agent（Claude Code、Codex 等）运行在本地机器上，但有些工作需要的工具或硬件只存在于
特定服务器上：FPGA 服务器上的 Vivado 和 Questa、CUDA GPU、需要 license 的工具、大型数据集等。
`remote-exec` 把 Agent 的*工具执行能力*扩展到这些服务器：

```bash
remote-exec-client fpga01 --cwd /home/user/project "vivado -mode batch -source build.tcl"
echo $?    # 远程 vivado 运行的退出码
```

远程执行层是**与 Agent 无关**的。它不包含任何 AI/LLM 逻辑，对 Agent、Shell 脚本、CI 和人类用户
的行为完全一致。Claude Code 只是其中一个使用者（`remote-exec-client --install-skill`，参见 [Claude Code 集成](#14-claude-code-集成)）。

## 2. 架构

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

一个 Python package 同时提供**两个**命令。任何安装了它的机器都会获得 `remote-exec-server` 和
`remote-exec-client`；一台机器可以运行其中之一，也可以同时运行两者（服务器 A 可以调用服务器 B）。

```
proto/remote_exec/v1/remote_exec.proto   API 定义
src/remote_exec/
    v1/                 生成的 protobuf/gRPC 代码（已提交到仓库）
    executor.py         子进程管理（不依赖 gRPC）
    server.py           gRPC servicer、参数校验、审计日志、生命周期管理
    server_cli.py       remote-exec-server 入口
    client.py           异步 client 库
    client_cli.py       remote-exec-client 入口
    config.py           client/server 配置加载
    security.py         token 认证拦截器、TLS 凭据
    skill.py            --install-skill 的实现
    claude_skill.md     Claude Code skill 文本（由 --install-skill 安装）
tests/                  单元测试 + localhost 集成测试
examples/               config.yaml、server.yaml、systemd unit
scripts/gen_proto.sh    protobuf 代码生成
```

### API（`remote_exec.v1.RemoteExecutor`）

```proto
rpc Execute(ExecuteRequest) returns (stream ExecuteEvent);
rpc GetServerInfo(GetServerInfoRequest) returns (ServerInfo);

ExecuteRequest { command, working_directory, map environment, timeout_seconds }
ExecuteEvent   { oneof: started | stdout | stderr | finished }
Finished       { status: EXITED|TIMEOUT|ERROR, exit_code, signal,
                 start_time, end_time, message, execution_id }
```

关键设计选择：

- **`oneof` 事件**：`Started` → 任意数量的 `stdout`/`stderr` 数据块 → 恰好一个 `Finished`，
  且总是最后一个。类型系统保证每种事件只携带对它有意义的字段。
- **原始字节而非文本行**：没有换行符的进度条会立即显示，非 UTF-8 输出也能原样保留。
- **两种错误通道**：*请求*本身的问题（空命令、非法环境变量名、认证失败、服务器繁忙）以 gRPC
  状态码返回。请求被接受*之后*发生的情况（启动失败、超时、退出状态）在 `Finished` 中报告。
- **Shell 风格的 `exit_code`**：被信号 N 终止时为 `128 + N`，同时提供原始的 `signal` 字段。
- **带版本号的 package**（`remote_exec.v1`），以后可以把 `Submit/GetStatus/GetResult/Cancel`
  或 `Upload/Download/Stat/List` 作为新的 RPC 或 service 加入，而不影响已有 client。

## 3. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 4. 安装工具（`uv tool install`）

```bash
uv tool install .                                   # 从本地源码目录安装
uv tool install git+https://example.com/remote-exec.git   # 从 git 仓库安装
uv tool install --force .                           # 升级 / 重新安装
```

安装后 `remote-exec-server` 和 `remote-exec-client` 位于 `~/.local/bin`。

```bash
remote-exec-server --port 50051          # 在远程服务器上
remote-exec-client fpga01 "hostname"     # 在本地机器上
remote-exec-client --install-skill       # 可选：让 Claude Code 使用该工具（见 §14）
```

`fpga01` 是在 client 配置文件（`~/.config/remote-exec/config.yaml`）中定义的 server 名称，
由配置文件将它映射到具体的 host 和 port，详见 [§9 Client 配置](#9-client-配置)。
`remote-exec-client --list` 可以列出所有已配置的名称及其地址。

## 5. 开发环境

```bash
uv sync                 # 根据 uv.lock 创建包含运行时 + 开发依赖的 .venv
uv run pytest           # 运行测试
uv run remote-exec-server --help
```

运行时依赖：`grpcio`、`protobuf`、`pyyaml`。开发依赖：`grpcio-tools`、`pytest`、`pytest-asyncio`。

## 6. Protobuf 代码生成

生成的代码位于 `src/remote_exec/v1/` 并已提交到仓库，因此安装时不需要 `protoc`。修改 `.proto`
之后，用以下命令重新生成：

```bash
make proto      # 或 ./scripts/gen_proto.sh，它执行的是：
uv run python -m grpc_tools.protoc -I proto --python_out=src --pyi_out=src \
    --grpc_python_out=src proto/remote_exec/v1/remote_exec.proto
```

proto 文件放在 `proto/remote_exec/v1/`（而不是 `proto/remote_exec.proto`），这样生成代码中的
`import` 语句才能正确解析到 `remote_exec.v1` package。如果升级了 `grpcio-tools`，请确保
`pyproject.toml` 中 `grpcio`/`protobuf` 的最低版本不低于代码生成器的版本。

## 7. 启动 Server

```bash
remote-exec-server                       # 127.0.0.1:50051，仅本机可访问
remote-exec-server --host 0.0.0.0 --port 50051 --token-file ~/.config/remote-exec/token
remote-exec-server --config /etc/remote-exec/server.yaml
```

启动时强制执行的安全规则：

- **默认监听地址为 `127.0.0.1`**。由于该服务本质上就是远程代码执行，绝不能被意外暴露。
  要为其他机器提供服务，需要显式指定 `--host`。
- **非 loopback 地址必须配置认证**（`--token-file` 和/或 mTLS）。否则 server 拒绝启动，
  除非传入 `--insecure-no-auth`。
- **拒绝以 root 身份运行**，除非传入 `--allow-root`。
- 在非 loopback 地址上未启用 TLS 时会输出警告。

其他选项：`--shell`（默认 `/bin/bash`）、`--max-concurrent`（32）、`--max-timeout`、
`--kill-grace`（5 秒）、`--log-level`、`--log-file`。详见 `remote-exec-server --help` 和
[examples/server.yaml](examples/server.yaml)。

## 8. Server 端口配置

默认端口为 **50051**。可以通过以下任意方式修改：

```bash
remote-exec-server --port 50052
remote-exec-server --host 0.0.0.0 --port 50052
remote-exec-server --listen 0.0.0.0:50052        # 效果相同，只用一个选项
```

也可以在 server 配置文件中设置 `port:` / `listen:`。命令行选项优先于配置文件。

## 9. Client 配置

默认路径为 `~/.config/remote-exec/config.yaml`。支持 `$XDG_CONFIG_HOME`，并可通过
`$REMOTE_EXEC_CONFIG` 覆盖。也可以传入 `--config FILE`。

```yaml
defaults:                       # 合并到每台 server 的配置中
  token_file: ~/.config/remote-exec/token
servers:
  acme2:
    host: acme2.example.local
    port: 50051
  gpu01:
    host: gpu01.example.local   # 未指定 port -> 50051
    description: GPU computation
  fpga01:
    host: fpga01.example.local
    port: 50052
```

`remote-exec-client fpga01 ...` 会连接 `fpga01.example.local:50052`，`gpu01` 会连接
`gpu01.example.local:50051`。配置中不存在的目标会被当作 `HOST[:PORT]` 处理，例如
`remote-exec-client 10.0.0.5 hostname`。
每台 server 可用的键：`host`、`port`、`description`、`token`、`token_file`、`tls`、
`ca_cert`、`client_cert`、`client_key`、`tls_server_name`、`connect_timeout`。
未知的键会被拒绝，以免拼写错误导致某项设置被悄悄忽略。
未配置 token 时会使用 `$REMOTE_EXEC_TOKEN`。

`remote-exec-client --list` 列出已配置的 server，`remote-exec-client fpga01 --ping` 检查连通性。

## 10. 命令执行

```bash
remote-exec-client acme2 "hostname"
remote-exec-client gpu01 "nvidia-smi"
remote-exec-client acme2 --cwd /home/user/project "python test.py"
remote-exec-client fpga01 --env FPGA_PART=xcvu37p --env BUILD_MODE=release "./build.sh"
remote-exec-client gpu01 --timeout 60 -- ls -la /data   # 未加引号的多个单词像 ssh 一样拼接
```

- 命令在 server 上以 `bash -c "<command>"` 方式运行，stdin 为 `/dev/null`。它以 server 的用户身份
  运行，环境变量为 server 的环境加上 `--env` 指定的值。
- `--cwd` 必须是绝对路径或以 `~` 开头（在 server 端展开），等价于 `cd DIR && command`。
- `--timeout SECONDS` 在超时时终止整个远程进程树。

`remote-exec-client` 的**退出码**：

| 退出码 | 含义 |
|--------|------|
| N      | 远程命令的退出码（`false` → 1，命令不存在 → 127） |
| 128+N  | 远程命令被信号 N 终止 |
| 124    | 超时（与 coreutils `timeout` 一致） |
| 130 / 143 | client 被 SIGINT / SIGTERM 中断；远程命令已被取消 |
| 255    | remote-exec 自身错误（与 ssh 一致）：无法连接、认证失败、配置错误、cwd 无效 |

## 11. Streaming 行为

输出在**产生时即被转发**，每块最多 64 KiB，server 端不做行缓冲。远程 stdout 输出到本地 stdout，
远程 stderr 输出到本地 stderr。*每个流内部*的顺序得到保证；stdout 与 stderr *之间*的顺序是
尽力而为的，与 ssh 相同。

在非 TTY 环境下会自行缓冲输出的程序（例如 Python）可能需要 `python -u` 或
`PYTHONUNBUFFERED=1`（`-e PYTHONUNBUFFERED=1`）。

生命周期保证：

- 每条命令运行在独立的 session/进程组中。在**超时**、**client 断开**（Ctrl-C、SIGTERM、client
  被杀死、gRPC keepalive 检测到网络中断）或 **server 关闭**时，server 向*整个进程组*发送
  SIGTERM，`--kill-grace` 秒后再发送 SIGKILL。子进程（例如 Vivado 的 worker）不会残留。
- 主命令退出后，它遗留在进程组中的后台进程也会被终止，因此不会产生孤儿进程。
- 慢速 client 会产生背压：远程进程在管道写满时阻塞，而不是由 server 无限缓冲输出。
- Keepalive ping 让长时间无输出、运行数小时的任务能够穿过 NAT/防火墙保持连接。

## 12. systemd 配置

参见 [examples/remote-exec-server.service](examples/remote-exec-server.service)。

```bash
sudo -u builder uv tool install /path/to/remote-exec
sudo install -m 644 examples/remote-exec-server.service /etc/systemd/system/
sudo install -d -o builder -m 700 /etc/remote-exec
sudo sh -c 'openssl rand -hex 32 > /etc/remote-exec/token'
sudo chown builder /etc/remote-exec/token && sudo chmod 600 /etc/remote-exec/token
# 修改 unit 文件中的 User= / ExecStart= 路径
sudo systemctl daemon-reload
sudo systemctl enable remote-exec-server
sudo systemctl start remote-exec-server
journalctl -u remote-exec-server -f
```

该 unit 以非特权用户（`User=`）运行，并演示了如何指定 host、port、token、配置文件以及日志级别/
日志文件。停止服务时，基于 cgroup 的 `KillMode` 还会清理所有逃逸出进程组的进程。

## 13. 安全注意事项

该服务从设计上**就是远程代码执行**。任何能通过认证的人都可以以服务用户身份运行任意命令。

- 默认绑定 loopback。用于远程访问时，优先绑定到私有网络接口（Tailscale/WireGuard IP），
  而不是 `0.0.0.0`。绝不要把端口暴露到公网；用防火墙加以限制。
- **即使在私有网络中也要启用认证**：
  - *Bearer token*：server 端使用 `--token-file`；client 配置中使用 `token_file`。token 采用
    常量时间比较。请将文件权限设为 600（否则 server 会发出警告）。未启用 TLS 时 token 以明文
    传输，在 WireGuard/Tailscale 内部没问题，但不适用于共享的局域网。
  - *TLS*：server 端使用 `--tls-cert/--tls-key`；client 端使用 `tls: true` + `ca_cert`。
  - *mTLS*（局域网推荐）：server 端再加上 `--tls-client-ca`，client 端配置
    `client_cert`/`client_key`。client 证书的 CN 会出现在审计日志中。
- 使用专用的非特权用户运行；默认拒绝 root。限制该用户的访问权限（用户组、文件权限、sudo）。
- **审计日志**（`remote_exec.audit` logger）：每条命令记录一条 `start`（id、client、cwd、超时、
  环境变量*名称*、命令）和一条 `end`（状态、退出码、耗时）。**永远不会记录环境变量的值**。
  命令行会被记录，因此不要把密钥写在命令里。
- 输入校验：命令非空且不含 NUL 字节、大小限制、cwd 必须为绝对路径、环境变量名符合 POSIX 规范、
  超时为有限的非负数，以及可选的 server 级别 `--max-timeout` 和 `--max-concurrent` 限制。

## 14. Claude Code 集成

在运行 Claude Code 的机器上执行：

```bash
remote-exec-client --install-skill          # -> ~/.claude/commands/remote-exec.md
remote-exec-client --install-skill DIR      # -> DIR/remote-exec.md
remote-exec-client --install-skill --force  # 覆盖本地已修改过的副本
```

该命令会安装 `remote-exec` skill（Claude Code 自定义命令；源文件：
[src/remote_exec/claude_skill.md](src/remote_exec/claude_skill.md)）。它只涉及
`remote-exec-client`，因为假定各 server 已在运行。它告诉 Claude：

- 用 `remote-exec-client --list` 发现 server 及其工具，用 `--ping` 检查连通性
- 判断何时适合远程执行
- 传递 `--cwd` / `-e` / `--timeout`，理解退出码，处理长时间任务和大量输出
- 避免破坏性命令

请在 client 配置的 `description` 字段中写明每台 server 提供的工具，因为 `--list` 正是把它展示给 Claude：

```yaml
servers:
  fpga01: {host: fpga01.example.local, description: "FPGA development: Vivado, Questa"}
  gpu01:  {host: gpu01.example.local,  description: "GPU computation: CUDA, nvidia-smi, Python"}
```

之后 Claude 会自主使用该 skill（或在你输入 `/remote-exec <任务>` 时使用），调用例如
`remote-exec-client fpga01 --cwd /home/user/project --timeout 7200 "vivado -mode batch -source build.tcl"`，
并读取流式输出和退出码。升级 package 后重新运行 `--install-skill` 即可获得新版 skill。

## 15. 测试

```bash
uv run pytest            # 约 20 秒
```

- `tests/test_executor.py`：子进程层（streaming、退出码、信号、cwd/env、超时 + SIGKILL 升级、
  进程组清理）。
- `tests/test_grpc.py`：进程内 server + client 库（成功执行、非零退出码、stdout、stderr、混合输出、
  cwd、env、超时、命令不存在、实时 streaming、并发、client 断开/取消、server 不可用、非法请求、
  最大并发数、token 认证、mTLS）。
- `tests/test_cli.py`：以子进程方式运行真实的命令行程序。覆盖**默认端口 50051**
  （`remote-exec-server --host 127.0.0.1` + `remote-exec-client local "echo hello"`）、
  **自定义端口 50052**、退出码、超时 → 124、不可用 → 255、client 被 SIGINT/SIGTERM/SIGKILL
  时远程进程树被终止、server 关闭，以及拒绝未认证的公网绑定。如果固定端口已被占用，相应测试会被跳过。
- `tests/test_config.py`：配置解析、默认/自定义端口、server 选项优先级、安全检查。
- `tests/test_skill.py`：`--install-skill`（默认/自定义目录、更新、`--force`）。

## 16. 未来扩展

以下功能有意未放入 v1，但现有结构已经为它们做好准备：

- **异步任务**（`Submit`、`GetStatus`、`GetResult`、`Cancel`）：`Executor` 已经能产生与 gRPC 无关、
  带 `execution_id` 的事件流。一个小型的内存任务注册表可以在后台任务中消费它，新的 RPC 从中读取。
- **文件 RPC**（`Upload`、`Download`、`Stat`、`List`）作为 `remote_exec.v1` 中的同级 service，
  共用相同的认证/TLS 配置。
- 按 client 授权（将 mTLS CN 或 token 映射到允许的命令/目录）。
- 可选的 PTY 模式和 stdin 转发（双向 streaming）。
- Agent 之间的通信明确**不是**这一层的目标。
