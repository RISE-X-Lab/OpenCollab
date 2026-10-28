---
{
  "id": "execution-environments",
  "title": "使用自己的执行环境",
  "category": "isolation",
  "summary": "两种接入自己执行环境的方式：把一个现成的目录、容器或 VM 直接交给 SDK（你负责清理），或注册一个新的隔离策略名，由你的提供器按会话分配和回收环境。",
  "aliases": [
    "接已有容器",
    "本地目录",
    "local_environment",
    "worktree_environment",
    "docker_environment",
    "attach_container",
    "build_repo_map_via_env",
    "Environment",
    "VMEnvironment",
    "VMConnection",
    "现有 VM",
    "SSH",
    "custom",
    "EnvironmentPool",
    "acquire",
    "release_env",
    "release",
    "adopt"
  ],
  "keywords": [
    "执行环境",
    "指定工作目录",
    "environment",
    "连接容器",
    "自定义环境",
    "环境提供器",
    "environment_providers",
    "自定义隔离"
  ],
  "questions": [
    "将模型的文件与命令操作连接到目标工作区，并掌握环境资源的所有权？",
    "怎么让 Agent 在一个已经运行的容器里干活？",
    "怎么指定 Agent 的工作目录？",
    "环境用完谁来清理？",
    "使用已有环境提供器扩展隔离策略并让资源分配、交付和清理保持一致？",
    "怎么让 isolation 用我自己的环境？",
    "提示策略需要 environment provider 是什么意思？",
    "自己写环境池要实现哪些方法？"
  ],
  "related": [
    "isolation-overview",
    "docker-isolation",
    "vm-environment-extension",
    "candidate-delivery",
    "service-integration-lifecycle"
  ],
  "verified_ref": "v0.9.6"
}
---

# 使用自己的执行环境

内置的六种隔离策略不够用时，比如要让 Agent 在你的服务已经在跑的容器里干活，或者接入自己的远程执行服务，OC 提供两种接入方式。区别在于**资源由谁管理**：

| 方式 | 怎么接 | 谁负责准备和清理 | 适合 |
| --- | --- | --- | --- |
| 方式一：交给 SDK 一个现成的环境 | `OpenCollab(workspace, environment=...)` | 你。SDK 只是借用 | 一个固定的目录、容器或 VM，整次运行都用它 |
| 方式二：注册一个新的隔离策略 | `OpenCollab(..., environment_providers={"名字": 工厂})`，再用 `isolation="名字"` | 你的提供器负责实际资源；OC 负责什么时候申请、交付和清理 | 每个会话要分配一份独立环境 |

先分清三个名词：

- **环境（Environment）**：OC 工具实际读写文件、执行命令的对象，比如本机目录或容器。
- **环境池（EnvironmentPool）**：负责给每个会话发放环境、回收环境。
- **环境提供器（environment provider）**：一个工厂函数，每次运行时为某个策略名创建一个环境池。

## 方式一：把一个现成的环境交给 SDK

OC 默认让工具在 `workspace` 目录里操作。用 `environment=` 可以换成你自己创建的执行环境。两个参数分工不同：`workspace` 标识调用方的项目（配置发现在这里进行）；`environment` 决定工具实际看到的目录和执行位置。

### 最小例子：本机目录

```python
import asyncio
from opencollab import OpenCollab
from opencollab.environments import local_environment, build_repo_map_via_env

async def main():
    environment = local_environment(".")
    try:
        await environment.setup()
        # 从环境里读取一份路径概览
        print(await build_repo_map_via_env(environment))
        result = await OpenCollab(".", environment=environment).agent(
            "阅读 README 并解释项目目标", tools="coding"
        )
        print(result.raise_for_status().output)
    finally:
        await environment.cleanup()   # 环境是你传进去的，由你清理

asyncio.run(main())
```

`build_repo_map_via_env` 从目标环境读取路径概览，会限制概览大小。

### 常用工厂

| 工厂 | 创建什么 |
| --- | --- |
| `local_environment(path)` | 本机目录环境 |
| `worktree_environment(path)` | 还没准备好的工作树环境 |
| `docker_environment(image, backing_environment, options=...)` | 由调用方管理的新容器环境 |
| `attach_container(...)` | 连接一个已经在运行的容器 |
| `VMEnvironment(connection, workspace, state_directory=...)` | 借用已有 Linux VM 上的项目目录 |

这些公开工厂都把对象交给你持有：你负责调用 `setup()`，最后调用 `cleanup()`。

### 手动管理一个工作树

在一个干净的 Git 项目或普通目录里运行：

```python
import asyncio
from opencollab.environments import worktree_environment

async def main():
    environment = worktree_environment(".")   # 只创建对象，还没有建目录
    try:
        await environment.setup()             # 这一步才真正准备目录
        print(environment.workspace)          # 工具实际使用的路径
        result = await environment.exec_cmd("pwd")
        print(result.returncode, result.stdout)
    finally:
        await environment.cleanup()           # 用完要自己清理

asyncio.run(main())
```

`worktree_environment(".")` 只创建对象，`setup()` 才准备目录，`workspace` 是工具实际读写的路径。这样单独创建的工作树由你自己管理：先导出需要的改动，再 `cleanup()`。工作树从哪个版本开始，见 [工作树的起点](isolation-overview.md#起点是哪个版本)。

### 连接一个已经在运行的容器

下面的代码放在已有的异步函数里执行：

```python
from opencollab.environments import attach_container

environment = attach_container(
    container_id="existing-project-container",  # 完整容器 ID 或合法名称
    workspace="/workspace/project",             # 容器内项目目录的绝对路径
)
await environment.setup()
probe = await environment.exec_cmd("pwd")
print(probe.returncode, probe.stdout)
```

- `container_id`：完整容器 ID 或合法名称。
- `workspace`：容器内规范化后的绝对路径，指向项目目录。
- `command_prefix`：可接入已有的命令包装；`timeout_returncode`：可匹配你约定的超时返回码。

接入已有容器时，文件要从环境里读：你本机上的同名路径，内容可能完全不同。

### 新建容器

用 `docker_environment(image, backing_environment, options=...)` 创建。需要把宿主目录映射为容器工作区时，先准备好来源，再调用 `await container.setup(mount_dir=prepared_path)`，项目通过这个挂载读写；附加的只读素材用 `DockerOptions` 里的 `DockerMount`。清理容器时，会一并处理它拥有的 `backing_environment` 资源，按这一点安排清理顺序。

### 连接已有 Linux VM 上的项目目录

项目已经放在某台 Linux VM（来宾）上，想让 Agent 直接在那个目录里干活，就构造一个 `VMEnvironment` 传给 SDK。OC 通过 SSH 在来宾上读写文件、执行命令。来宾上要准备好 Linux 和 SSH、与控制端同版本的 OC、一个私有状态目录，步骤见 [在现有 Linux VM 中运行任务](vm-environment-extension.md#第一步准备控制端和来宾)。

```python
import asyncio
from opencollab import OpenCollab
from opencollab.environments import VMConnection, VMEnvironment

async def main():
    environment = VMEnvironment(
        VMConnection(
            host="vm-a.example.net",
            user="worker",
            identity_file="/home/controller/.ssh/vm-key",
            known_hosts_file="/home/controller/.ssh/known_hosts",
            python="/home/worker/.venvs/oc/bin/python",
        ),
        "/home/worker/project",
        state_directory="/home/worker/oc-state",
    )
    try:
        await environment.setup()
        probe = await environment.exec_cmd("hostname && pwd")
        print(probe.returncode, probe.stdout)
        result = await OpenCollab(".", environment=environment).agent(
            "读取 README.md，解释项目目标", tools="coding"
        )
        print(result.raise_for_status().output)
    finally:
        await environment.cleanup()

asyncio.run(main())
```

- 这种方式**直接改来宾上的项目目录**，不复制。
- `cleanup()` 会停止这个环境发起的操作、删掉它的临时文件；来宾继续运行，项目和操作记录都保留。
- 如果你要的是“每个会话领一台机器、先复制项目、用完重置来宾”，别手动建 `VMEnvironment`，改用 `config["vm"]` 加 `vm-group` 或 `vm-session` 策略。

### 路径怎么算

工具的相对路径始终从 `environment.workspace` 算起，本机工作树目录和容器里的项目目录可以不一样。服务可以把宿主上的项目作为客户端的 `workspace`，把容器里的工作目录交给 `environment`：配置发现在宿主侧进行，工具执行在容器里进行，各有明确位置。

如果服务在容器或 VM 里运行子 Agent，`worktree` 策略会继续用父环境所在的那台机器。想让每个子会话拿到另一个容器或 VM，就选 `docker-session` 或 `vm-session` 并准备好资源。

连接后先做一次检查：读一个项目里确实存在的小文件，确认看到的是目标内容；再执行一条和任务相关的命令，确认依赖和当前目录。

### 谁负责清理

| 情况 | 谁清理 |
| --- | --- |
| 你传给 SDK 的 `environment` | 你。SDK 只是借用，运行结束后由你清理 |
| 连接的已有容器 | 环境只管理本次发出的命令和临时资源，容器本身仍由创建它的服务管理 |
| 新建的容器 | 拥有它的一方，在导出交付之后清理 |
| 你直接传入的 `VMEnvironment` | 你。`cleanup()` 停止它发起的操作、删掉临时文件，来宾和项目都保留 |

你自己提供环境时，结果里表示“环境活动已结束”的指标是 `None`，需要你在清理后自己确认操作已停止；会话执行和持久化是否完成，看 `session_quiesced`。见 [确认清理完成](../operations/timeouts-retries.md#停止之后确认清理完成)。

## 方式二：注册一个新的隔离策略

用 `environment_providers` 注册：键名就是新的策略名，值是工厂。之后 `isolation="名字"` 就会用它。OC 仍负责申请环境、交付改动和清理的调度，你的提供器负责实际资源。

### 最小例子：把 Docker 提供器包一层

下面把现成的 Docker 提供器注册成 `custom`，镜像、网络和隔离能力都来自这份 Docker 配置：

```python
from opencollab import OpenCollab
from opencollab.environments import DockerOptions, docker_isolation_provider

container_factory = docker_isolation_provider(
    DockerOptions(image="python:3.11", network="none"), scope="session"
)

# 工厂接收运行信息，返回一个环境池
def project_environment_factory(
    *, workspace, base_environment, owned_save_dir,
    enable_adoption, owned_output_paths,
):
    return container_factory(
        workspace=workspace,
        base_environment=base_environment,
        owned_save_dir=owned_save_dir,
        enable_adoption=enable_adoption,
        owned_output_paths=owned_output_paths,
    )

client = OpenCollab(".", environment_providers={
    "custom": project_environment_factory,
})
```

使用：Workflow 里 `await client.workflow(flow, inputs, isolation="custom")`；Team 里在团队文件或角色里写 `isolation: custom`。策略名要合法，并且和注册的键一致。

### 工厂收到的参数

| 参数 | 含义 |
| --- | --- |
| `workspace` | 调用方项目的位置 |
| `base_environment` | 真实的来源环境 |
| `owned_save_dir` | 当前运行的产物目录 |
| `owned_output_paths` | 帮环境区分“运行自己写出的记录”和“任务源内容” |
| `enable_adoption` | 运行是否需要采纳能力，会影响候选准备和交付方式 |

### 使用现有 VM 提供器

有现成的 Linux VM 时，可以直接用内置的 `vm_isolation_provider`，不用自己写环境池。它返回的工厂和 Docker 工厂收同一组运行参数，可以用任意策略名注册。注册前先按 [VM 指南](vm-environment-extension.md) 准备好 SSH 连接、来宾目录和管理员的重置程序。

```python
from opencollab import OpenCollab
from opencollab.environments import VMConnection, VMResource, vm_isolation_provider

resources = [VMResource(
    resource_id="worker-01",
    connection=VMConnection(
        host="vm-a.example.net", user="worker",
        python="/home/worker/.venvs/oc/bin/python",
        identity_file="/home/controller/.ssh/vm-key",
        known_hosts_file="/home/controller/.ssh/known_hosts",
    ),
    workspace_root="/home/worker/oc-workspaces",
    state_directory="/home/worker/oc-state",
    reset_command="/usr/local/sbin/reset-opencollab-guest",
)]
client = OpenCollab(".", environment_providers={
    "vm-group": vm_isolation_provider(
        resources,
        lease_database="/home/controller/oc-state/vm-leases.sqlite3",
        scope="group",
        wait_timeout=0,
    ),
})
```

使用：`await client.workflow(flow, inputs, isolation="vm-group", cleanup_timeout=60)`。

- 直接注册提供器时，清理等待时间要在运行参数 `cleanup_timeout` 里写。想让 SDK、CLI 和 Team 共用一份可序列化的配置，就改用 `config["vm"]` 或环境变量 `OPENCOLLAB_VM`，这时 `cleanup_timeout` 默认 60 秒。
- 如果 `environment_providers` 里注册了和 `config["vm"]` 同名的工厂，以 `environment_providers` 为准。
- 多台控制主机共用一个机器池时，本地 SQLite 文件不够用。可以把外部分配服务包成自己的 `EnvironmentPool`，由这个服务负责跨机器分配和归还。

### 自己写环境池要实现什么

环境池实现公开接口 `opencollab.environments.EnvironmentPool`：

| 方法 | 作用 |
| --- | --- |
| `acquire(role, source=...)` | 接收角色和实际的父环境或审阅源，返回已经能执行工具的环境 |
| `adopt(patch)` | 把改动应用到交付工作区 |
| `release_env(environment)` | 归还一次分配 |
| `release()` | 结束池拥有的全部资源 |

环境本身要提供准备、文件读写、命令执行、撤销、停止和清理，详细签名见 [公开端口](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/ports.py)。几条要遵守的规则：

- 审阅源可能来自另一种策略，也可能在容器里，要通过环境操作读取它的内容。直接从宿主机同名目录复制，审阅任务会读到错误的版本。
- 传进来的源环境是借来的，释放时只释放池自己分配的资源。
- 想清楚环境对象和底层资源的关系。例如一个环境对象代表一份会话租约、底层容器由池统一管理时，多个环境引用同一个资源，归还其中一个后资源还要留到其他使用者都结束。审阅和清理都按这个关系来。
- 取消可能发生在申请资源的途中：提供器要跟踪还没结束的申请，等它有了明确结果再完成撤销。补丁导出失败时，保留可恢复的文件和资源信息，并让同一个池能再次清理。
- 自定义提供器同样要遵守工具权限、工作区检查和输出限制。

### 接入新后端时怎样测试

1. 在一个环境里写入一个文件，再读回来。
2. 执行一条短命令，看退出码。
3. 申请两份环境，分别写入同名文件，确认内容互不影响。
4. 实际试一下文件写入、命令取消和导出失败后的恢复，确认资源能被正确清理。

最后，为你的提供器写清楚三件事给使用者看：文件共享到什么程度、进程和网络怎么隔离、权限有哪些限制。

## 常见问题

- **提示该策略需要 environment provider**：这个策略名还没注册。检查 `environment_providers` 的键和 `isolation` 写的名字是否一致。
- **`build_repo_map_via_env` 返回空字符串**：可能是读取失败，用 `exec_cmd("pwd")` 和 `read_file("README.md")` 查看执行位置和文件内容。
- **Agent 读到的文件和你本机看到的不一样**：检查它是不是在容器里；相对路径从 `environment.workspace` 算起。
- **想用虚拟机**：内置 VM 提供器已经做好了领取机器、复制项目、导出改动和归还前重置，见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。

## 延伸阅读

- 源码依据：[公开环境 API](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/environments.py) · [环境组合实现](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/programmatic.py) · [策略池实现](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/session_environments.py) · [公开端口](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/ports.py)
- 相关指南：[隔离](isolation-overview.md) · [在容器中运行任务](docker-isolation.md) · [在现有 Linux VM 中运行任务](vm-environment-extension.md) · [把 OC 接进自己的服务](../architecture/service-integration-lifecycle.md)
