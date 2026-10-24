---
{
  "id": "execution-environments",
  "title": "让任务在指定目录、容器或 VM 中执行",
  "category": "isolation",
  "summary": "把 Agent 的读写文件和执行命令接到你指定的目录、容器或 VM，并弄清谁负责清理。",
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
    "SSH"
  ],
  "keywords": [
    "执行环境",
    "指定工作目录",
    "environment",
    "连接容器"
  ],
  "questions": [
    "将模型的文件与命令操作连接到目标工作区，并掌握环境资源的所有权？",
    "怎么让 Agent 在一个已经运行的容器里干活？",
    "怎么指定 Agent 的工作目录？",
    "环境用完谁来清理？"
  ],
  "related": [
    "isolation-overview",
    "docker-isolation",
    "custom-environment-providers",
    "vm-environment-extension",
    "workspace-recovery"
  ],
  "verified_ref": "0c47f74a"
}
---

# 让任务在指定目录、容器或 VM 中执行

OC 默认让工具在 `workspace` 目录里操作。用 `OpenCollab(workspace, environment=...)` 可以换成一个你自己创建的执行环境，例如另一个本机目录、你的服务已经在跑的容器，或已有 Linux VM。OC 的取舍是：传入的环境只被借用，由你负责 `setup()` 和 `cleanup()`。

两个参数的分工：

| 参数 | 作用 |
| --- | --- |
| `workspace` | 标识调用方的项目 |
| `environment` | 决定工具实际看到的目录和执行位置 |

接入已有容器时，文件要从环境里读。你本机上的同名路径，内容可能完全不同。

## 最小示例：本机目录

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

常用工厂：

| 工厂 | 创建什么 |
| --- | --- |
| `local_environment(path)` | 本机目录环境 |
| `worktree_environment(path)` | 还没准备的工作树环境 |
| `docker_environment(image, backing_environment, options=...)` | 由调用方管理的新容器环境 |
| `attach_container(...)` | 连接一个已经在运行的容器 |
| `VMEnvironment(connection, workspace, state_directory=...)` | 借用已有 Linux VM 的项目目录 |

这些公开工厂都把对象交给你持有。你负责调用 `setup()`，最后调用 `cleanup()`。

`build_repo_map_via_env` 从目标环境读取路径概览，会限制概览大小。

## 连接已有容器

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

这段代码要放在已有的异步函数里执行。

参数说明：

- `container_id`：完整容器 ID 或合法名称。
- `workspace`：容器内规范化后的绝对路径，应指向项目目录。
- `command_prefix`：可接入已有的命令包装。
- `timeout_returncode`：可匹配你集成端约定的超时返回码。

## 新建容器

1. 用 `docker_environment(image, backing_environment, options=...)` 创建。
2. 需要把宿主目录映射为容器工作区时，先准备好来源，再调用 `await container.setup(mount_dir=prepared_path)`。
3. 项目通过这个挂载读写。附加的只读素材用 DockerOptions 里的 DockerMount。
4. 清理容器时，会一并处理它拥有的 backing_environment 资源。按这一点安排清理顺序。

## 连接已有 Linux VM 工作区

需要借用来宾上已经存在的项目目录时，构造 `VMEnvironment` 并传给 SDK。Linux、SSH、同版本 OpenCollab 和私有状态目录的准备见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。下面的连接值应替换成实际资源，来宾目录要已经存在。

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

这个入口直接访问来宾目录，调用方持有连接环境并负责清理。`cleanup()` 停止环境拥有的操作、回收其临时文件，来宾继续运行，项目和操作记录继续保存。需要按会话分配机器、复制源项目并在归还时重置来宾时，使用 `config["vm"]` 和 `vm-group`、`vm-session` 策略。

## 连接后先做一次检查

1. 读一个项目里确实存在的小文件，确认看到的是目标内容。
2. 执行一条和任务相关的命令，确认依赖和当前目录。

## 路径怎么算

本机工作树目录和容器里的项目目录可以不一样。工具的相对路径始终从 `environment.workspace` 算起。

服务可以把宿主上的项目作为客户端的 `workspace`，把容器里的工作目录交给 environment。这样，配置发现在宿主侧进行，工具执行在容器里进行，各有明确位置。

服务在容器或 VM 中运行子 Agent 时，`worktree` 策略继续使用父机器。需要为子会话分配其他容器或 VM 时，选择 `docker-session` 或 `vm-session` 并准备资源，见 [在容器中运行任务](docker-isolation.md) 和 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。

## 谁负责清理

| 情况 | 谁清理 |
| --- | --- |
| 你传给 SDK 的 environment | 你。SDK 只是借用，运行结束后由你清理 |
| 连接的已有容器 | 环境只管理本次发出的命令和临时资源，容器本身仍由创建它的服务管理 |
| 新建的容器 | 拥有它的一方，在导出交付之后清理 |
| 直接传入的 `VMEnvironment` | 调用方停止拥有的操作并回收临时文件，来宾与项目继续保存 |

你自己提供环境时，SDK 结果里表示“环境活动已结束”的指标是 `None`。由你清理环境并确认操作已停止。会话执行和持久化是否完成，看 `session_quiesced`。详见 [把 OpenCollab 接进自己的服务](../architecture/service-integration-lifecycle.md)。

## 常见问题

- 如果 `build_repo_map_via_env` 返回空字符串：可能是读取失败。用 `exec_cmd("pwd")` 和 `read_file("README.md")` 查看执行位置和文件内容。
- 如果 Agent 读到的文件和你本机看到的不一样：检查它是不是在容器里，相对路径从 `environment.workspace` 算起。
- 不确定该用哪种策略：见 [隔离怎么选](isolation-overview.md)。

## 参考

工厂签名见[公开环境 API](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/environments.py)和[环境组合实现](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/programmatic.py)。
