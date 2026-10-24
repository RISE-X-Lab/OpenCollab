---
{
  "id": "isolation-overview",
  "title": "隔离怎么选",
  "category": "isolation",
  "summary": "根据任务要不要改文件、跑代码、联网，挑一种合适的隔离方式。",
  "aliases": [
    "沙箱",
    "互相覆盖",
    "独立会话",
    "shared",
    "worktree",
    "docker-group",
    "docker-session",
    "custom",
    "vm-group",
    "vm-session"
  ],
  "keywords": [
    "隔离",
    "环境隔离",
    "isolation",
    "选哪种隔离"
  ],
  "questions": [
    "几个 Agent 会不会互相覆盖文件？",
    "worktree 能隔离进程和网络吗？",
    "怎样让每个 Agent 在独立容器里运行？",
    "Agent 之间能看到彼此的对话吗？",
    "只是读代码，需要隔离吗？"
  ],
  "related": [
    "isolation-configuration",
    "docker-isolation",
    "custom-environment-providers",
    "shell-permissions-execution",
    "execution-environments",
    "candidate-delivery",
    "vm-environment-extension",
    "member-authored-workflow",
    "model-routing-request-capacity"
  ],
  "verified_ref": "0c47f74a"
}
---

# 隔离怎么选

OC 里的 `isolation` 参数决定每个 Agent 会话的文件工具和命令落在哪个执行环境：直接用父目录、创建独立 Git worktree、进入 Docker 容器，或连接已经准备好的 Linux VM。它可以在整次运行、Team 角色或单次 `ctx.agent` 调用上设置。和很多框架“所有 agent 共用一个沙箱”不同，OC 把对话、文件、进程、网络分开考虑，而且隔离环境里的改动不会自动写回项目，要经过采纳。

最小用法：`OpenCollab(".").workflow(flow, inputs, isolation="worktree")`。下表帮你挑策略。

## 先看结论：六种内置策略

| 策略 | 文件 | 进程和网络 | 适合什么 |
| --- | --- | --- | --- |
| `shared` | 直接用父环境的目录 | 和父环境一样 | 按顺序读代码、明确要共用同一份文件 |
| `worktree` | 每人一个独立工作树；不是 Git 仓库时用目录副本 | 和父环境一样 | 并行改代码、比较几种改法 |
| `docker-group` | 同一个容器里，每个会话各有一个工作树 | 同组成员共用这个容器的进程和网络 | 一组任务要共用装好的依赖和临时服务 |
| `docker-session` | 每个会话一个独立容器和目录 | 每个会话用自己的容器 | 代码执行会互相影响的任务 |
| `vm-group` | 当前运行的策略池共用一台 VM，每个会话各有一个工作树 | 共用来宾内核、进程与网络 | 一组任务需要独立的来宾系统 |
| `vm-session` | 每个会话领取一台 VM 并准备工作区 | 每个会话使用自己的来宾系统 | 安装依赖、后台服务会互相影响的任务 |
| 你注册的策略名 | 由提供器决定 | 由提供器决定 | 接入专门的环境服务 |

`group` 指每次运行中对应环境池的共享范围。成员启动的子 Workflow 会创建自己的环境池，要让子 Agent 留在父成员的 VM 中，将子运行设为 `worktree`。单次子调用选择 `vm-session` 时，需要父 VM 之外的空闲机器。

OC 名词：

- 会话：OC 里一个 Agent 的一次运行，有独立的对话历史。
- 环境提供器（environment provider）：按策略名创建环境池的工厂，策略名就是 `isolation` 的值。
- 父环境：发起调用的那一层所在的环境。`shared` 和 `worktree` 都建立在父环境之上。

快速挑选：

1. 只读、不改文件：用 `shared`，再给角色配只读工具。
2. 要同时改代码：用 `worktree`。
3. 要运行代码，而且怕它们互相干扰（端口、后台进程、临时文件）：用 `docker-session`。
4. 要运行代码，但想共用一次装好的依赖和服务：用 `docker-group`。

任务需要独立内核或来宾系统状态时，可以选择 `vm-group` 共用一台已准备的 VM，或选择 `vm-session` 按会话分配。配置步骤见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。

## 最小示例：运行和单次调用分别设置

下面的 Workflow 整体默认用 `worktree`，第二次调用单独改成 `shared`。

```python
import asyncio
from opencollab import OpenCollab
from opencollab.tools import builtin_tools

async def inspect(ctx, inputs):
    # 第一次调用：在独立工作树里只读分析
    first = await ctx.agent(
        inputs["task"], tools=builtin_tools("file_read", "grep"), isolation="worktree"
    )
    # 第二次调用：共用父环境，不给工具，只处理传进来的文本
    summary = await ctx.agent(
        f"根据这份分析概括项目结构\n{first}", tools=[], isolation="shared"
    )
    return {"inspection": first, "summary": summary}

async def main():
    result = await OpenCollab(".").workflow(
        inspect, {"task": "阅读项目并说明入口"}, isolation="worktree"
    )
    print(result.raise_for_status().output)

asyncio.run(main())
```

要点：

- 两次调用的对话历史是分开的。第二次能看到第一次的结果，是因为代码把 `first` 显式拼进了提示词。
- `tools=[]` 表示摘要调用只处理传入的文本。如果要它继续读文件，就传文件工具，并在任务里写清材料路径。
- 工作树里的改动以候选或补丁的形式交付。采纳之后，改动才会出现在目标交付目录。具体见 [把候选结果采纳到交付目录](candidate-delivery.md)。

更多参数见 [默认隔离与单次覆盖怎么配](isolation-configuration.md) 和 [在容器中运行任务](docker-isolation.md)。

## 隔离其实分五件事

“隔离”不是一个开关，下面五件事要分开考虑：

| 范围 | 决定什么 |
| --- | --- |
| 对话 | 模型能读到哪些消息 |
| 文件 | 成员是不是在同一个目录里改文件 |
| 进程 | 进程树、后台服务和资源是否共用 |
| 网络 | 任务能连哪些服务 |
| 权限 | 模型能调用哪些工具，能访问哪些路径和命令 |

对话方面，每个会话保存自己的历史。协作信息通过任务说明、显式上下文、队友消息和完成结果传递。共享文件也能传递信息，所以对话和文件要分别选择。

权限方面举两个例子：移除角色的 `bash` 可以限制命令执行；设置容器网络可以限制服务连接。

## 按实际任务来选

如果是顺序阅读仓库，就共用目录，并给角色配只读工具。这样省去重复准备工作区的时间。

如果两个成员要各自尝试一种修复，就用独立工作树。它们从同一个干净起点各改各的，入口成员再比较、采纳。在任务里写清每个成员负责哪些文件，可以减少合并冲突。

如果要运行未知脚本，或测试项目的安装过程，先想清楚进程和网络要不要分开：

- 组容器适合共用一次准备好的服务和依赖。
- 会话容器适合让安装、后台进程和临时文件各自分开。
- 联网构建可以指定允许的 Docker 网络。尽量把依赖预装进镜像，减少运行时下载。
- 涉及宿主机上的敏感资源时，检查只读素材、可写项目目录和能访问到的凭据。

## 容器和权限怎样配合

- 用 Docker 策略时，Python 调度仍然在调用方进程里运行。只有文件工具和命令会进入所选容器。
- 内置 Docker 默认网络是 `none`。要装在线依赖，需要预先准备镜像，或明确设置允许的 Docker 网络。
- 宿主机上 `bash` 的授权、文件工具的路径检查、容器本身的限制，三者分别生效。
- 处理未知代码时，选用完可以丢掉的隔离环境，并检查挂进去的素材和凭据。

## 进阶：隔离并不是完全隔开

- 工作树之间共用 Git 对象存储。
- 同一个组容器里的成员，仍可能通过绝对路径、公共临时目录或进程互相影响。
- 隔离策略只决定工具操作落在哪里。项目依赖要靠镜像或环境准备过程提供。候选文件要经过采纳才进入最终目录。

想亲眼确认这些区别，可以在实际任务环境里做三个小实验：

1. 让两个成员分别执行 `pwd`，看目录是否不同。
2. 让一个成员在 `/tmp` 写个标记文件，看另一个成员能否看到。
3. 让一个成员启动一个服务，看另一个成员能否访问。

环境准备好后，执行项目本来的构建或测试命令，看退出码和真实输出。

## 常见问题

- 如果遇到权限被拒绝：先判断是哪一层拦的，是角色工具、命令授权、路径检查，还是环境限制，再改对应的设置。

`custom` 是注册自定义提供器后使用的策略名，见 [接入自己的执行环境](custom-environment-providers.md)。VM 策略需要先配置来宾、SSH 与共享租约数据库，完整步骤见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。

VM 工具在来宾执行，模型请求在控制端处理，Team 通信继续遵循拓扑。子运行过程见 [让成员编写并管理自己的子 Workflow](../workflow/member-authored-workflow.md)，模型容量见 [逻辑模型路由与共享请求容量](../models/model-routing-request-capacity.md)。

## 参考

策略的含义和实际分配方式，可对照[隔离指南](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/session-isolation.zh-CN.md)和[环境池实现](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/session_environments.py)。
