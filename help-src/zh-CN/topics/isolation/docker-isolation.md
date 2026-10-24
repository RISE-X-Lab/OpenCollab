---
{
  "id": "docker-isolation",
  "title": "在容器中运行任务",
  "category": "isolation",
  "summary": "让 Agent 在 Docker 容器里执行命令，并设置共用范围、网络和资源上限。",
  "aliases": [
    "组容器",
    "每人一个容器",
    "离线环境",
    "DockerOptions",
    "DockerMount",
    "docker_isolation_provider",
    "OPENCOLLAB_DOCKER_TEST_IMAGE"
  ],
  "keywords": [
    "Docker",
    "容器",
    "docker-group",
    "docker-session",
    "容器网络",
    "资源限制"
  ],
  "questions": [
    "准备可执行的容器环境并按任务需求设置共享范围、网络和资源？",
    "怎么让每个 Agent 在自己的容器里跑？",
    "容器里为什么连不上网？",
    "怎么限制容器的 CPU 和内存？",
    "怎么把只读素材挂进容器？"
  ],
  "related": [
    "isolation-overview",
    "execution-environments",
    "workspace-recovery"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在容器中运行任务

OC 内置两种 Docker 隔离策略，用 `isolation="docker-group"` 或 `isolation="docker-session"` 选择。默认镜像 `python:3.11`，默认网络 `none`（离线）。

| 策略 | 含义 |
| --- | --- |
| `docker-group` | 一组会话共用一个容器，每个会话在容器里有自己的工作树 |
| `docker-session` | 每个会话一个独立容器 |

和“整个 agent 跑在容器里”的做法不同，OC 只把 Agent 的文件和命令工具送进容器。模型请求和 Python 调度仍在调用方进程里。

## 运行前准备

1. 确认 Docker daemon 在运行。
2. 选镜像。内置默认镜像是 `python:3.11`。自定义镜像需要：
   - 带 Bash 和 Git；
   - 入口程序能接受 `sleep infinity`。
3. 把项目依赖直接装进镜像。这样执行和测试时可以保持离线。

## 最小示例：每个会话一个容器

```python
import asyncio
from opencollab import OpenCollab
from opencollab.environments import (
    DockerOptions, docker_isolation_provider,
)

async def inspect(ctx, inputs):
    return await ctx.agent(
        inputs["task"], tools="coding", label="inspector"
    )

async def main():
    options = DockerOptions(
        image="python:3.11",   # 镜像
        network="none",        # 默认不联网
        cpus=2,                # CPU 上限
        memory="2g",           # 内存上限
        pids_limit=256,        # 进程数上限
    )
    client = OpenCollab(".", environment_providers={
        "docker-session": docker_isolation_provider(options, scope="session"),
    })
    result = await client.workflow(
        inspect, {"task": "执行 pwd 和 git status，并解释结果"},
        isolation="docker-session",
    )
    print(result.status, result.output)

asyncio.run(main())
```

改成组容器，要改三处：

1. 注册名改为 `docker-group`。
2. `isolation` 改为 `docker-group`。
3. `scope` 改为 `group`。

默认提供器本来就支持这两种策略。像上面这样显式注册，主要是为了换镜像和资源设置。

## 网络

- `DockerOptions.network` 默认是 `none`，也就是不联网。
- 要下载依赖或访问服务，可以写 `bridge`，或一个已有的 Docker 网络名。
- 写 `host` 会被拒绝。

注意有两条连接要分别检查：

| 连接 | 由谁控制 |
| --- | --- |
| 工具执行时的连接 | 容器网络设置 |
| 模型请求 | 调用方进程，使用客户端的模型配置 |

## 资源上限

| 参数 | 允许的值 |
| --- | --- |
| `cpus` | 正的有限数 |
| `memory` | 正的字节数，或带 `b`、`k`、`m`、`g` 后缀的值 |
| `pids_limit` | 正整数 |

- 组模式：多个成员共用这一份额度。
- 会话模式：每个容器各用一份。

## 文件、临时目录与挂载

两种模式都会把源内容复制到容器的私有存储，并准备好容器里能用的 Git 信息。源工作区可以在本机，也可以来自已有的 Docker 或 VM 环境。

组模式里，各会话的工作树是分开的，但容器里的进程和公共临时目录是共用的。想连这些也分开，就用会话模式。

挂只读素材：

```python
from opencollab.environments import DockerMount

# 放进 DockerOptions：mounts=(DockerMount("/absolute/assets", "/assets"),)
```

挂载规则：

- 附加挂载一律只读。
- 宿主机 socket 会被拒绝。
- 隔离提供器要求挂载目标避开工作区、`/tmp` 和 `/var/tmp`。
- 挂载前检查素材目录里有没有凭据或私人文件。

## 怎样确认设置生效

在 Agent 执行的 Bash 里：

1. 看 `pwd`。
2. 读 `/sys/fs/cgroup/cpu.max`、`memory.max`、`pids.max`，看资源上限。具体字段会随容器平台的 cgroup 版本变化。
3. 按镜像里有的工具测试网络访问。
4. 想区分组模式和会话模式，就看两个会话能不能读到同一个临时标记文件。

## 常见问题

- 如果容器里连不上网：默认网络是 `none`。改成 `bridge` 或已有网络名，或者把依赖预装进镜像。
- 如果补丁导出失败或结果被截断：工作区和容器会被保留，恢复位置带完整容器 ID。按 [找回隔离任务的改动并清理资源](workspace-recovery.md) 先收集改动，再清理资源。完整补丁本来会在运行拥有的容器被移除前捕获。
- 如果想接一个已经在跑的容器：见 [让任务在指定目录或现有容器里执行](execution-environments.md)。

## 参考

配置和默认值见[Docker 值对象](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/domain/docker.py)。共用范围和生命周期见[Docker 隔离池](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/docker_isolation.py)。
