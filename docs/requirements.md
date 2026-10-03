项目：基于 gRPC 的远程命令执行工具

我希望开发一个轻量级的远程命令执行系统，主要供 Claude Code、Codex 等 AI Coding Agent 使用。

核心使用场景如下：

* Claude Code 运行在本地机器上。
* 一台或多台远程 Linux Server 上安装有不同的专业工具或计算资源，例如 Vivado、Questa、CUDA、GPU、Python 环境以及其他开发工具。
* Claude Code 可以在本地调用一个工具，透明地在指定远程服务器上执行命令。
* 远程命令的 stdout、stderr、exit code 以及相关状态信息需要返回本地。
* 本地的 remote execution client 后续会封装成 Claude Code Skill，使 Claude Code 可以自主决定何时调用远程服务器。
* 整个项目通过 uv 管理，并支持通过 uv tool install 安装。

整体架构：

Claude Code
     │
     │ Skill / Bash
     ▼
remote-exec-client
     │
     ▼
gRPC Client
     │
     │ Network
     ▼
Remote Linux Server
     │
     ▼
remote-exec-server
     │
     ▼
gRPC Server
     │
     ▼
subprocess
     │
     ├── Vivado
     ├── Questa
     ├── Python
     ├── CUDA
     ├── Git
     └── arbitrary shell commands

⸻

1. 技术栈

第一版使用 Python 实现。

使用：

* Python 3.11+
* uv
* grpcio
* grpcio-tools
* protobuf
* asyncio
* grpc.aio
* Python subprocess API

尽量减少第三方依赖。

代码结构应该清晰、模块化，并方便未来扩展。

⸻

2. 使用 uv 管理项目

项目必须使用 uv 进行 Python package 和 dependency 管理。

项目应该包含：

pyproject.toml
uv.lock

开发环境应该可以通过：

uv sync

创建。

测试可以通过类似：

uv run pytest

运行。

protobuf code generation 也应该可以通过 uv run 执行。

例如：

uv run python -m grpc_tools.protoc ...

如果合适，可以在项目中提供 script 或 Makefile 简化 protobuf generation。

⸻

3. 使用 uv tool install 安装

项目应该被设计成一个标准 Python package，可以通过：

uv tool install .

安装。

未来如果发布到 Git repository，也应该能够支持类似：

uv tool install git+https://example.com/remote-exec.git

安装完成后应该提供两个独立 CLI tool：

remote-exec-server
remote-exec-client

即在 pyproject.toml 中定义两个 console entry points。

概念上类似：

[project.scripts]
remote-exec-server = "remote_exec.server_cli:main"
remote-exec-client = "remote_exec.client_cli:main"

具体模块名称可以根据最终代码结构调整。

⸻

4. Client 和 Server 使用同一个 Package

不要创建两个完全独立的 Python package。

整个项目应该是一个 package：

remote-exec

安装：

uv tool install .

以后，任意机器都会获得：

remote-exec-server
remote-exec-client

两个命令。

机器根据自己的角色选择运行哪个命令。

例如远程 FPGA Server：

remote-exec-server

本地 Claude Code 所在机器：

remote-exec-client fpga01 "hostname"

同一台机器也可以同时作为：

Client
+
Server

例如：

Server A
    remote-exec-server
    remote-exec-client
Server B
    remote-exec-server
    remote-exec-client

这样未来 Server A 也可以调用 Server B。

因此不要在 package 层面人为区分：

client machine
server machine

而应该把 Client/Server 看作同一个 remote execution package 提供的两种能力。

⸻

5. Server 默认端口

gRPC Server 默认监听端口：

50051

因此：

remote-exec-server

应该等价于：

remote-exec-server --port 50051

建议默认只监听：

127.0.0.1

还是：

0.0.0.0

请根据安全性和实际远程执行需求进行分析，并在实现前说明选择。

因为该工具的目的就是允许其他服务器访问，实际部署必须能够指定监听地址，例如：

remote-exec-server \
    --host 0.0.0.0 \
    --port 50051

也可以支持：

remote-exec-server \
    --listen 0.0.0.0:50051

如果认为 --listen 的接口更清晰，可以采用这种方式。

无论 CLI 具体设计如何：

默认端口必须为 50051，并且用户必须可以修改端口。

例如：

remote-exec-server --port 50052

⸻

6. Client Server 配置

Client 应支持配置多台远程服务器。

例如：

servers:
  acme2:
    host: acme2.example.local
    port: 50051
  gpu01:
    host: gpu01.example.local
    port: 50051
  fpga01:
    host: fpga01.example.local
    port: 50052

如果配置文件没有指定 port：

servers:
  fpga01:
    host: fpga01.example.local

则自动使用：

50051

作为默认端口。

因此：

remote-exec-client fpga01 "hostname"

会连接：

fpga01.example.local:50051

而：

remote-exec-client fpga02 "hostname"

如果配置：

fpga02:
    host: fpga02.example.local
    port: 50052

则连接：

fpga02.example.local:50052

请选择符合 Linux/macOS 习惯的默认配置路径。

同时允许通过 CLI 参数指定其他配置文件。

⸻

7. 基本远程执行功能

Server 应允许 Client 在服务器上执行命令。

例如：

remote-exec-client acme2 "hostname"

或者：

remote-exec-client fpga01 \
    "vivado -mode batch -source build.tcl"

Client 至少需要能够指定：

command
working directory
environment variables
timeout

Server 需要返回：

stdout
stderr
exit code
execution status
start time
end time

例如：

remote-exec-client acme2 \
    --cwd /home/user/project \
    "python test.py"

应该等价于远程执行：

cd /home/user/project
python test.py

⸻

8. 实时 Streaming

这是一个重要需求。

某些命令可能运行很长时间，例如：

vivado -mode batch -source build.tcl

可能运行几十分钟甚至几个小时。

因此不要等待程序结束后一次性返回全部 stdout/stderr。

使用 gRPC Server Streaming，使 stdout/stderr 可以实时返回。

例如：

Client                         Server
Execute()
   │
   ├───────────────────────────>
   │
   │    STDOUT: Starting...
   │<───────────────────────────
   │
   │    STDOUT: Synthesis...
   │<───────────────────────────
   │
   │    STDERR: warning...
   │<───────────────────────────
   │
   │    STDOUT: WNS = 0.18 ns
   │<───────────────────────────
   │
   │    EXIT: 0
   │<───────────────────────────

Client 应该在收到输出后立即打印。

使用体验尽量接近：

ssh server command

stdout 和 stderr 必须可以区分。

⸻

9. Protobuf API

设计一个清晰的 protobuf API。

例如：

service RemoteExecutor {
    rpc Execute(ExecuteRequest)
        returns (stream ExecuteEvent);
}

ExecuteRequest 可以包含：

command
working_directory
environment
timeout_seconds

ExecuteEvent 应该能够表示：

STARTED
STDOUT
STDERR
EXITED
ERROR
TIMEOUT

如果有更合理的 protobuf schema，可以自行调整。

请解释关键 API 设计选择。

⸻

10. Client CLI

提供简单易用的 CLI。

例如：

remote-exec-client acme2 "hostname"
remote-exec-client gpu01 "nvidia-smi"
remote-exec-client fpga01 \
    --cwd /home/user/project \
    "vivado -mode batch -source build.tcl"

支持设置环境变量：

remote-exec-client fpga01 \
    --env FPGA_PART=xcvu37p \
    --env BUILD_MODE=release \
    "./build.sh"

远程命令的 exit code 应尽可能直接成为：

remote-exec-client

本身的 exit code。

例如：

remote-exec-client acme2 "false"
echo $?

应该得到非 0 返回值。

这一点非常重要，因为 Claude Code/Codex 可以据此判断远程命令是否执行成功。

⸻

11. Server Daemon

remote-exec-server 应该：

* 支持多个 Client
* 支持多个 Command 并发执行
* 正确管理 subprocess
* 正确处理 Client disconnect
* 支持 timeout
* 避免 orphan process
* 正确终止 Command 创建的 child processes

尤其需要考虑 Unix process group。

例如：

remote-exec-server
       │
       ▼
    Vivado
       │
       ├── child process
       ├── child process
       └── child process

如果任务被取消、Client disconnect 或发生 timeout：

不应该只终止最外层 Vivado process，而留下大量 child processes。

⸻

12. systemd

提供：

remote-exec-server.service

示例。

例如最终可以：

sudo systemctl enable remote-exec-server
sudo systemctl start remote-exec-server

daemon 应该使用普通 Unix 用户运行，而不是 root。

systemd 示例中应该展示如何指定：

host
port
config
logging

例如：

ExecStart=... remote-exec-server --host 0.0.0.0 --port 50051

⸻

13. 安全

这个服务本质上提供：

Remote Code Execution

因此必须认真考虑安全问题。

第一版至少应该：

* 明确禁止默认暴露到公网
* 支持或为 TLS 做好设计
* 最好支持 mTLS
* daemon 使用普通 Unix 用户运行
* 不允许 daemon 默认以 root 身份执行 Command
* 对 Command execution 进行 audit logging
* 对输入参数进行合理检查

系统通常可能运行在：

Tailscale
WireGuard
LAN
private network

之上。

即使运行在 private network，也应该考虑 authentication。

⸻

14. Logging

Server 使用 Python 标准 logging。

日志至少记录：

timestamp
client
command
working_directory
start time
end time
exit code
execution duration

默认不要记录 environment variable value。

⸻

15. 长时间任务

第一版主要实现：

Execute()

Streaming RPC。

但代码结构应该方便未来增加：

Submit()
GetStatus()
GetResult()
Cancel()

例如：

Submit(command)
      │
      ▼
task_id = abc123
GetStatus(abc123)
      │
      ▼
RUNNING
GetResult(abc123)
      │
      ▼
COMPLETED

第一版不要为了这些功能过度设计。

⸻

16. 文件传输

第一版暂时不要实现复杂文件同步。

假设项目文件已经通过：

Git
NFS
shared filesystem
existing project directory

存在于 Remote Server。

但是代码结构应该方便未来增加：

Upload
Download
Stat
List

等 RPC。

⸻

17. Claude Code Skill

最终目标是让 Claude Code 通过 Skill 使用：

remote-exec-client

例如 Claude Code 判断：

当前任务需要运行 Vivado。
Vivado 位于 fpga01。

然后调用：

remote-exec-client fpga01 \
    --cwd /home/user/project \
    "vivado -mode batch -source build.tcl"

整个流程：

Claude Code
     │
     ▼
Remote Execution Skill
     │
     ▼
remote-exec-client
     │
     ▼
gRPC
     │
     ▼
remote-exec-server
     │
     ▼
Vivado
     │
     ▼
stdout / stderr / exit code
     │
     ▼
Claude Code Context
     │
     ▼
Claude Code 分析结果

创建一个 Claude Code Skill 示例。

Skill 应告诉 Claude Code：

* remote-exec-client 是什么
* 哪些服务器可以使用
* 每台服务器有什么专业工具/资源
* 什么情况下应该使用远程执行
* 如何指定 working directory
* 如何传递 environment variables
* 如何理解 exit code
* 如何处理 timeout
* 避免不必要的 destructive command

例如：

fpga01:
    purpose:
        FPGA development
    tools:
        Vivado
        Questa
gpu01:
    purpose:
        GPU computation
    tools:
        CUDA
        nvidia-smi
        Python

⸻

18. 项目目录结构

建议：

remote-exec/
│
├── pyproject.toml
├── uv.lock
├── README.md
│
├── proto/
│   └── remote_exec.proto
│
├── src/
│   └── remote_exec/
│       │
│       ├── __init__.py
│       │
│       ├── server.py
│       ├── server_cli.py
│       │
│       ├── client.py
│       ├── client_cli.py
│       │
│       ├── config.py
│       └── ...
│
├── tests/
│   └── ...
│
├── examples/
│   ├── config.yaml
│   ├── remote-exec-server.service
│   └── claude-skill/
│
└── scripts/

其中：

remote-exec-server
        │
        ▼
server_cli.py
        │
        ▼
server.py

以及：

remote-exec-client
        │
        ▼
client_cli.py
        │
        ▼
client.py

CLI 层和实际 gRPC implementation 尽量分离。

⸻

19. 测试

增加 automated tests。

至少测试：

successful command
non-zero exit code
stdout
stderr
mixed stdout/stderr
working directory
environment variables
timeout
command not found
server unavailable
custom port
default port 50051
concurrent commands
client disconnect

增加 localhost integration test。

例如：

remote-exec-server \
    --host 127.0.0.1 \
    --port 50051

然后：

remote-exec-client local "echo hello"

应该输出：

hello

并返回：

exit code = 0

还需要测试非默认端口：

remote-exec-server \
    --host 127.0.0.1 \
    --port 50052

Client 配置：

servers:
  test:
    host: 127.0.0.1
    port: 50052

然后：

remote-exec-client test "echo hello"

应该正常工作。

测试完成后实际运行 test suite，并修复发现的问题。

⸻

20. Documentation

README 至少包含：

1. 项目目的
2. Architecture
3. uv installation
4. uv tool install
5. Development environment
6. protobuf generation
7. Server startup
8. Server port configuration
9. Client configuration
10. Command execution
11. Streaming behavior
12. systemd setup
13. Security considerations
14. Claude Code integration
15. Testing
16. Future extensions

需要明确给出：

uv tool install .

安装后的使用示例：

remote-exec-server --port 50051

以及：

remote-exec-client fpga01 "hostname"

⸻

21. 实现原则

第一版保持：

small
simple
understandable
reliable

优先级：

Reliability
    ↓
Clear architecture
    ↓
Error handling
    ↓
Streaming
    ↓
Security
    ↓
Additional features

第一版不要引入：

Kubernetes
Redis
RabbitMQ
database
distributed scheduler

除非存在非常明确的技术理由。

当前环境只有少量可信任的 Linux Server。

⸻

22. 一个重要的架构原则

Remote Execution System 本身不要包含 AI/LLM 特定逻辑。

它应该只是：

Local Process
      │
      ▼
remote-exec-client
      │
      ▼
gRPC
      │
      ▼
remote-exec-server
      │
      ▼
Remote Process

Claude Code 只是它的一个 consumer。

同一个工具未来应该可以被：

Claude Code
Codex
其他 Agent
Shell Script
CI
Human

共同使用。

因此 Remote Execution Layer 应保持：

Agent-independent

⸻

23. 第一阶段不要实现 Agent-to-Agent Communication

项目第一阶段目标不是：

Claude A
   ↕
Claude B

而是：

Claude Code
      │
      │ reasoning
      ▼
Remote Execution Skill
      │
      ▼
remote-exec-client
      │
      ▼
gRPC
      │
      ▼
remote-exec-server
      │
      ▼
Professional Tools

即：

Agent 负责 reasoning，gRPC Remote Execution Layer 负责把 Agent 的 Tool Execution Capability 扩展到远程服务器。

未来可以进一步增加：

Local Agent
     │
     ▼
Remote Agent

但不要在第一版实现。

⸻

最终任务

请完成以下工作：

1. 首先检查以上需求，在 Coding 前指出重要 Architecture Issue 或潜在问题。
2. 设计 protobuf API。
3. 设计项目目录结构。
4. 简短说明关键 Design Decisions。
5. 使用 uv 创建和管理项目。
6. 正确配置 pyproject.toml 和 uv.lock。
7. 确保项目可以通过 uv tool install . 安装。
8. 提供 remote-exec-server 和 remote-exec-client 两个 CLI。
9. Server 默认端口使用 50051。
10. Server 支持用户指定其他端口。
11. Client 配置支持指定每台 Server 的端口，没有指定时默认使用 50051。
12. 创建实际项目文件。
13. 实现可以工作的异步 gRPC Server。
14. 实现可以工作的异步 gRPC Client。
15. 实现 streaming stdout/stderr。
16. 正确处理 exit code、timeout、Client disconnect 和 subprocess cleanup。
17. 添加 automated tests。
18. 测试默认端口和自定义端口。
19. 创建 systemd service 示例。
20. 创建 Claude Code Skill 示例。
21. 编写 README。
22. 实际运行：

uv run pytest

23. 如果测试失败，分析原因并修改代码，直到测试通过。
24. 实际测试：

uv tool install .

确认安装完成后：

remote-exec-server --help
remote-exec-client --help

均可以正常运行。

不要只提供：

* Design Document
* Architecture Proposal
* Code Snippets
* Pseudo Code

请直接创建一个实际可以运行的完整项目。

第一版最重要的目标是：

使用简单、可靠、清晰的方式，让 Claude Code 可以通过本地 remote-exec-client，利用 gRPC 在其他 Linux Server 上执行命令，并实时获得 stdout、stderr 和 exit code。

同时整个 package 可以通过 uv tool install 方便地安装到本地或远程服务器，并根据机器角色运行 remote-exec-client、remote-exec-server，或者同时运行两者。
