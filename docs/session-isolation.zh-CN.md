# Session 隔离

[English](session-isolation.md)

Team 和 Workflow 可以选择执行环境策略。各 Agent 的对话状态独立保存，隔离策略控制工作目录和工具执行环境。

`shared` 复用父任务的环境与目录。`worktree` 在父执行环境中创建独立工作树，工作树之间共享操作系统和 Git 对象存储。父任务使用容器时，子工作树继续位于该容器中。非 Git 目录沿用已有的副本处理方式。

## 选择策略

```python
from opencollab import OpenCollab

client = OpenCollab(".")

async def solve(ctx, inputs):
    answer = await ctx.agent(inputs["task"], tools="coding")
    summary = await ctx.agent("Summarize the task", tools=[], isolation="shared")
    return {"answer": answer, "summary": summary}

result = await client.workflow(solve, {"task": "Inspect the code"}, isolation="worktree")
```

省略单次调用的 `isolation` 时，Workflow Agent 继承运行默认策略。`True` 选择 `worktree`，`False` 选择 `shared`。运行默认策略也省略时，保留原有的共享环境行为，已有工作树对干净源目录的要求继续生效。`ctx.agent_run` 和候选子工作流采用相同的选择与清理过程。

Team 配置文件可以声明默认策略，并为角色覆盖。

```yaml
entry: manager
isolation: worktree
roles:
  manager:
    prompt: Coordinate the task.
    isolation: shared
    tools: [spawn_agent]
  coder:
    prompt: Implement the requested change.
    tools: [file_read, file_write]
topology:
  manager: [coder]
```

`client.team(..., isolation="shared")` 覆盖 Team 文件的默认策略，角色设置优先于运行默认值。新选项全部省略时，`use_worktrees` 保留原语义，入口 Agent 继续使用原工作目录。CLI 读取相同的 Team 文件设置。

显式隔离的入口 Agent 在工作区回收前保存完整补丁。SDK 通过 `result.metrics["entry_patch"]` 返回补丁位置。关闭轨迹记录时也会保存补丁，调用方省略产物目录时则创建恢复目录。调用方可以显式将补丁应用到源仓库。

Workflow CLI 支持 `--isolation shared` 和 `--isolation worktree`。

## Session 快照

`opencollab.bootstrap.snapshot_session(session)` 按照[快照协作者要求](migrations/0.5.0.md)，
创建独立的会话状态和分叉环境。源会话与成功创建的快照共享模型传输。
OC 创建的传输由每个会话分别持有一份所有权引用，
`await session.aclose()` 释放该会话的引用，最后一个拥有者关闭传输一次。
源会话或兄弟快照关闭后，存活快照仍可继续请求模型。
通过 `build_session(llm=...)` 注入的模型由调用者拥有，调用者在所有会话结束后关闭它。

快照在状态和运行时协作者全部复制成功后取得模型引用，构建失败会清理已分叉的环境。
共享模型的所有权通过引用计数保存，源会话关闭后可以回收，存活快照继续运行。
从已释放模型所有权的会话创建新快照会抛出 `SnapshotSessionError`。
各会话的环境分别执行自己的清理操作。

## 自定义环境提供器

向 `OpenCollab` 传入 `environment_providers={"custom": factory}`，随后使用 `isolation="custom"`。每个工厂通过关键字参数接收 `workspace`、`base_environment`、`owned_save_dir`、`enable_adoption` 和 `owned_output_paths`。

工厂为每次运行中的该策略创建一个环境池，实现公开的 `opencollab.environments.EnvironmentPool` 接口。操作包括 `acquire(role, source=...)`、`release_env(environment)`、`release()` 和 `adopt(patch)`。

`acquire` 收到当前父任务或审阅任务的真实工作环境，返回已准备好执行工具的环境。提供器负责自己分配的资源，传入的源环境保持借用关系。清理等待在途的环境申请完成，并在失败时保留可恢复材料，由同一个池继续处理清理。工具沿用已有权限和工作区检查。

未知策略会返回错误。模型端点和请求并发采用独立配置。

## Docker 策略

`docker-group` 为每次运行中的该策略分配一个容器，每个 Session 在容器中获得独立工作树，Team 入口 Agent 也使用该容器。`docker-session` 为每个 Session 分配独立容器和工作目录。Python 调度继续在调用方进程中运行，工具在选定容器内执行。默认镜像为 `python:3.11`，需要 Docker daemon 可用。自定义镜像需要 Bash、Git，并且入口程序能够接收 `sleep infinity`。

```python
from opencollab import OpenCollab
from opencollab.environments import DockerMount, DockerOptions, docker_isolation_provider

options = DockerOptions(
    image="python:3.11",
    cpus=2,
    memory="2g",
    pids_limit=256,
    mounts=(DockerMount("/path/to/read-only-assets", "/assets"),),
)
client = OpenCollab(".", environment_providers={
    "docker-session": docker_isolation_provider(options, scope="session"),
})
result = await client.workflow(solve, {"task": "Inspect the code"}, isolation="docker-session")
```

容器网络默认为 `none`。任务需要网络访问时，`network` 可以指定 `bridge` 或已有 Docker 网络名。附加挂载均为只读，并拒绝暴露宿主 socket。工作区内容复制到容器私有存储，已有工作树和子模块捕获逻辑保留文件的物理内容，可移植 Git 元数据让容器独立使用自己的仓库。源工作区可以来自本机或已有 Docker 环境，其所有权继续由调用方持有。

完整补丁在拥有的容器移除前捕获。导出失败或被截断时，工作区及其容器会保留，用于恢复和清理重试，恢复位置包含完整容器 ID。进程取消与环境撤销沿用已有行为。可以通过 `OPENCOLLAB_DOCKER_TEST_IMAGE` 选择本地镜像，执行 Docker adapter 测试中的真实容器路径。

## VM 隔离策略

`vm-group` 为一个 Team 或 Workflow 领取一台现有 Linux VM，每个 Session 在来宾内使用独立工作树。
`vm-session` 为每个 Session 领取一台 VM。`shared` 和 `worktree` 继续相对于父环境生效，
成员可以让自己编写的 Workflow Agent 留在自己的 VM 中执行。
来宾软件和 SSH 连接准备见 [VM 使用指南](virtual-machines.zh-CN.md)。

```python
from opencollab import OpenCollab
from opencollab.environments import VMConnection, VMResource, vm_isolation_provider

resources = [VMResource(
    resource_id="worker-01",
    connection=VMConnection(host="vm.example.net", user="worker"),
    workspace_root="/srv/opencollab",
    state_directory="/var/tmp/opencollab-operations",
    reset_command="/usr/local/sbin/reset-opencollab-guest",
)]
client = OpenCollab(".", environment_providers={
    "vm-group": vm_isolation_provider(resources, lease_database="/var/tmp/oc-leases.sqlite3"),
    "vm-session": vm_isolation_provider(resources, lease_database="/var/tmp/oc-leases.sqlite3", scope="session"),
})
result = await client.workflow(solve, {"task": "Inspect the code"}, isolation="vm-group", cleanup_timeout=60)
```

稳定的 `resource_id` 标识实际机器，同一机器的不同连接别名应使用相同标识。
共享机器池的控制进程使用相同的绝对 `lease_database` 路径。SQLite 事务和资源唯一约束保持独占。
`wait_timeout=0` 在容量不足时立即报错，正数指定等待秒数，`None` 等待容量或取消。
子 Agent 选择 `vm-session` 时，需要父 VM 之外的一台空闲 VM。
多台控制机器共享资源时，可以通过既有自定义提供者入口接入外部分配服务。

来宾的工作区根目录须已存在。每台资源需要显式 `reset_command`，恢复工作负载可能产生的全部来宾改动。
删除工作目录可以恢复其中的文件。依赖安装、系统配置和其他来宾改动需要对应的恢复操作，
例如管理员提供的重置程序。OC 先导出候选文件和日志，停止拥有的操作，清理工作区，
再持有租约执行重置。重置成功后，VM 回到可分配状态。导出、清理、连接或重置失败时，
保留租约和恢复资料以供重试。

SDK 可将相同配置序列化后放入 `config["vm"]`。`vm` 对象包含 `resources`、
`lease_database`、可选的 `wait_timeout` 和默认 60 秒的 `cleanup_timeout`。
每个资源对象采用 `VMResource` 的字段，
其中 `connection` 对象采用 `VMConnection` 字段。OpenCollab 据此注册两种 VM 策略。
显式 `environment_providers` 中的同名项目优先。

配置中的清理等待时间适用于 SDK Team、Workflow、CLI 和成员自编子 Workflow。
SDK 调用或子运行 `launch` 显式传入的 `cleanup_timeout` 优先。
Workflow CLI 支持 `--cleanup-timeout`。省略 VM 配置时，继续使用已有运行默认值。
自定义 VM 提供者通过这些调用参数选择清理等待时间，上面的提供者示例使用了这一方式。

```python
import json
from dataclasses import asdict
from pathlib import Path

profile = {"resources": [asdict(resource) for resource in resources],
           "lease_database": "/var/tmp/oc-leases.sqlite3"}
Path("vm-profile.json").write_text(json.dumps(profile))
profile = json.loads(Path("vm-profile.json").read_text())
client = OpenCollab(".", config={"vm": profile})
result = await client.team("Inspect the source", config="team.yaml", isolation="vm-group")
```

CLI 通过 `OPENCOLLAB_VM` 读取 JSON 配置，也支持既有 `configs/.env` 来源。
Team YAML 继续使用上述运行默认与角色覆盖。Workflow 使用既有 `--isolation` 选项。

```bash
OPENCOLLAB_VM="$(cat vm-profile.json)" opencollab workflow run solve --isolation vm-group
```

模型路由、凭据和共享请求额度保存在控制端。VM 选择决定工具执行位置和工作区归属。
Team 成员之间的通信继续遵循配置中的拓扑。

控制进程退出后租约继续保留。管理员确认资源拥有的远端进程已结束、来宾已成功重置后，
可以移除该资源对应的确切租约记录并恢复分配。仍在运行的池对象支持清理重试。
重新创建池会保留已有租约，等待上述恢复完成。

`manage_workflow` 的 `launch` 可选传入 `isolation`，设置子 Workflow 的默认隔离策略。
省略时继承成员策略。成员使用 `{"action":"launch","source":"generated.py","isolation":"worktree"}`
可让子 Agent 在自己的 VM 中使用独立工作树。单个子调用可通过 `isolation="vm-session"` 申请另一台来宾。
[可执行 VM 示例](../examples/vm-authored-workflow/README.zh-CN.md) 从成员写出源码开始，执行到取得原生子运行结果。

`launch` 的 `budget` 是子运行各 Session 共享的总额度。控制端在每个模型请求开始前检查剩余额度，
子代码使用 `over_budget_ok=True` 的调用也受该额度约束。
