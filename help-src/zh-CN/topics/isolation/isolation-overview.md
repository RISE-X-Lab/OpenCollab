---
{
  "id": "isolation-overview",
  "title": "隔离：Agent 的文件和命令落在哪里",
  "category": "isolation",
  "summary": "隔离决定 Agent 的文件和命令落在哪里：在 shared、worktree、Docker、VM 六种策略里挑一种，弄清在哪一层设置、谁优先，以及工作树从哪个版本开始。",
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
    "vm-session",
    "角色覆盖",
    "单次覆盖",
    "use_worktrees",
    "--no-worktrees",
    "--isolation",
    "ctx.agent",
    "ctx.agent_run",
    "OpenCollab.team",
    "OpenCollab.workflow",
    "OPENCOLLAB_VM",
    "cleanup_timeout",
    "分目录",
    "独立分支",
    "Git worktree",
    "WorktreeEnvironment",
    "WorktreePool",
    "source workspace has uncommitted changes"
  ],
  "keywords": [
    "隔离",
    "环境隔离",
    "isolation",
    "选哪种隔离",
    "隔离配置",
    "默认策略",
    "优先级",
    "工作树",
    "并行改代码",
    "worktree",
    "独立工作区"
  ],
  "questions": [
    "几个 Agent 会不会互相覆盖文件？",
    "worktree 能隔离进程和网络吗？",
    "怎样让每个 Agent 在独立容器里运行？",
    "Agent 之间能看到彼此的对话吗？",
    "只是读代码，需要隔离吗？",
    "让 Team 角色和 Workflow 调用使用预期的执行环境？",
    "团队文件、SDK 参数和角色都写了隔离，听谁的？",
    "怎么让某一次 Agent 调用不用工作树？",
    "命令行怎么指定隔离方式？",
    "并行修改同一个项目时保留清晰的文件起点并理解结果如何返回？",
    "两个 Agent 同时改代码会不会冲突？",
    "报错 source workspace has uncommitted changes 怎么办？",
    "工作树里的改动怎么回到我的项目？"
  ],
  "related": [
    "docker-isolation",
    "vm-environment-extension",
    "execution-environments",
    "candidate-delivery",
    "tool-selection"
  ],
  "verified_ref": "0c47f74a"
}
---

# 隔离：Agent 的文件和命令落在哪里

OC 的 `isolation` 参数决定每个会话的文件工具和命令在哪里执行：直接用父目录、一个独立的 Git 工作树、一个 Docker 容器，还是你准备好的 Linux VM。它可以设在整次运行、Team 角色或单次 `ctx.agent` 调用上。

这篇先讲清“隔离”到底隔开了什么，再给出六种内置策略和选择方法，然后讲在哪一层设置、冲突时谁优先，最后讲最常用的工作树策略从哪个版本开始、改动去了哪里。

## 先理解：“隔离”其实是五件事

很多框架默认“所有 Agent 共用一个沙箱”。OC 不把隔离当成一个开关，而是分成五件事分别考虑：

| 范围 | 决定什么 | 由什么控制 |
| --- | --- | --- |
| 对话 | 模型能读到哪些消息 | 每个会话只有自己的历史，信息靠任务说明、显式消息和返回结果传递 |
| 文件 | 成员是不是在同一个目录里改文件 | 隔离策略 |
| 进程 | 进程树、后台服务和资源是否共用 | 隔离策略（容器、VM） |
| 网络 | 任务能连哪些服务 | 容器网络设置、VM 所在的基础设施 |
| 权限 | 能调用哪些工具、访问哪些路径和命令 | 角色工具、命令授权、路径检查，见 [内置工具](../extensions/tool-selection.md) |

共享文件本身也能传递信息，所以对话和文件要分别选择。还有一条贯穿始终的规则：**隔离环境里的改动不会自动写回项目**，要经过采纳，见 [隔离改动怎样回到项目](candidate-delivery.md)。

## 六种内置策略

| 策略 | 文件 | 进程和网络 | 适合什么 |
| --- | --- | --- | --- |
| `shared` | 直接用父环境的目录 | 和父环境一样 | 按顺序读代码，或明确要共用同一份文件 |
| `worktree` | 每个会话一个独立工作树；不是 Git 仓库时用目录副本 | 和父环境一样 | 并行改代码、比较几种改法 |
| `docker-group` | 同一个容器里，每个会话各有一个工作树 | 同组共用这个容器的进程和网络 | 一组任务要共用装好的依赖和临时服务 |
| `docker-session` | 每个会话一个独立容器和目录 | 每个会话用自己的容器 | 代码执行会互相影响的任务 |
| `vm-group` | 同一台 VM 里，每个会话各有一个工作树 | 同组共用这台 VM 的内核、进程和网络 | 一组任务要一个和本机分开的系统 |
| `vm-session` | 每个会话领一台 VM，在里面准备工作区 | 每个会话用自己的 VM | 装依赖、起后台服务、改系统配置会互相影响的任务 |
| 你注册的策略名 | 由你的提供器决定 | 由你的提供器决定 | 接入专门的环境服务，见 [使用自己的执行环境](execution-environments.md) |

几个名词：

- **父环境**：发起调用的那一层所在的环境。`shared` 和 `worktree` 都建立在父环境之上：父任务在容器里，子工作树也在那个容器里。
- **group**：“一次运行里的一个环境池”共用一份资源。成员启动的子 Workflow 有自己的环境池，所以子运行想留在父成员的容器或 VM 里，要选 `worktree`。
- **环境提供器**：按策略名创建环境池的工厂，策略名就是 `isolation` 的值。`vm-*` 策略要先配好 VM 资源才会注册，见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)；Docker 策略见 [在容器中运行任务](docker-isolation.md)。

### 快速挑选

1. 只读、不改文件：用 `shared`，再给角色配只读工具。
2. 要同时改代码：用 `worktree`。
3. 要运行代码，而且怕互相干扰（端口、后台进程、临时文件）：用 `docker-session`。
4. 要运行代码，但想共用一次装好的依赖和服务：用 `docker-group`。
5. 要独立的内核或系统状态（例如会改系统配置）：一组任务共用一台 VM 用 `vm-group`，每个会话一台用 `vm-session`。

### 按实际任务来选

- **顺序阅读仓库**：共用目录，给角色配只读工具，省去重复准备工作区的时间。
- **两个成员各自尝试一种修复**：用独立工作树。它们从同一个干净起点各改各的，入口成员再比较、采纳。在任务里写清每个成员负责哪些文件，可以减少合并冲突。
- **运行未知脚本，或测试项目的安装过程**：先想清楚进程和网络要不要分开。组容器适合共用一次准备好的服务和依赖；会话容器让安装、后台进程和临时文件各自分开。联网构建可以指定允许的 Docker 网络，但尽量把依赖预装进镜像。涉及宿主机上的敏感资源时，检查只读素材、可写项目目录和能访问到的凭据。

## 在哪里设置，谁优先

`isolation` 可以写在四个地方：Workflow 运行参数、单次 `ctx.agent` 调用、团队文件顶层、团队角色。优先级一句话记住：

- **Workflow**：单次调用的值 > 运行的默认值。什么都不写时用 `shared`。
- **Team**：角色的值 > SDK 运行参数 > 团队文件的默认值。

### Workflow：运行默认值和单次调用

下面的 Workflow 整体默认用 `worktree`，第二次调用单独改成 `shared`：

```python
import asyncio
from opencollab import OpenCollab
from opencollab.tools import builtin_tools

async def inspect(ctx, inputs):
    # 没写 isolation：继承运行的 worktree，在独立工作树里只读分析
    first = await ctx.agent(
        inputs["task"], tools=builtin_tools("file_read", "grep")
    )
    # 显式写 shared：复用父环境；不给工具，只处理传进来的文本
    summary = await ctx.agent(
        f"根据这份分析概括项目结构\n{first}", tools=[], isolation="shared"
    )
    return {"inspection": first, "summary": summary}

async def main():
    result = await OpenCollab(".").workflow(
        inspect, {"task": "阅读项目并说明入口"}, isolation="worktree"  # 整次运行的默认值
    )
    print(result.raise_for_status().output)

asyncio.run(main())
```

- 两次调用的对话历史是分开的。第二次能看到第一次的结果，是因为代码把 `first` 显式拼进了提示词。
- `tools=[]` 表示摘要调用只处理传入的文本；要它继续读文件，就传文件工具，并在任务里写清材料路径。
- `ctx.agent_run` 用同样的选择规则，另外还返回执行和清理状态。布尔值也可以用：`True` 等于 `worktree`，`False` 等于 `shared`。

### Team：三层优先关系

```yaml
entry: manager
isolation: worktree          # 团队文件的默认值
roles:
  manager:
    prompt: Coordinate the task and summarize the results.
    isolation: shared        # 角色自己的值，优先级最高
    tools: [spawn_agent, file_read]
  coder:
    prompt: Implement the requested change.
    tools: [file_read, apply_patch]
topology:
  manager: [coder]
```

用 SDK 运行 `await client.team(task, config="team.yaml", isolation="docker-session")` 时：

| 角色 | 最终策略 | 原因 |
| --- | --- | --- |
| `coder` | `docker-session` | 角色没写，SDK 参数覆盖团队文件 |
| `manager` | `shared` | 角色自己写了，角色优先 |

入口角色也可以单独选环境。入口被显式隔离后，它的改动会以完整的入口补丁返回，见 [入口成员自己被隔离时](candidate-delivery.md#入口成员自己被隔离时)。

不写这些新选项时，Team 按老规则处理：`use_worktrees` 默认 `True`，子成员各用一个工作树，入口成员使用原工作目录；只在子角色里写隔离时，入口仍用原工作目录。CLI 的 `--no-worktrees` 对应关闭子成员工作树的老方式，团队文件里显式写的策略仍按上面的优先级生效。

### 命令行

- Workflow：在 `workflow run NAME` 后面用 `--isolation` 指定策略名，例如 `shared`、`worktree`、`vm-group`。
- Team：根命令从 `--team-config team.yaml` 读取隔离配置。
- VM 策略：CLI 从环境变量 `OPENCOLLAB_VM` 读完整的 VM 配置 JSON；Workflow 命令可以用 `--cleanup-timeout` 覆盖清理等待时间。
- 需要 `DockerOptions` 或自定义环境提供器时，用 SDK 组合。

## 工作树：从哪个版本开始，改动去了哪里

`worktree` 是最常用的策略：每个会话在自己的 Git 工作树里改代码，适合并行实现和候选比较。

### 起点是哪个版本

| 情况 | 起点 |
| --- | --- |
| 普通 `worktree` | 当前的 Git 提交（`HEAD`） |
| 声明了 `adopt_agent` 的 Team | 当前交付内容做成的候选快照，包括你未提交的修改和之前已采纳的结果 |

普通工作树从 Git 提交创建，所以有两个要求：源仓库要有能解析的 `HEAD`，并且是干净的，没有未提交的改动。不干净时会报错 `source workspace has uncommitted changes` 并列出相关文件。处理方法：用 `git status --short` 看是哪些文件，按项目习惯提交或 stash，再次运行。当前运行自己生成的未跟踪产物目录不算在内，已跟踪文件的改动仍会被检查。

带采纳能力的 Team 不受这个限制：子任务从候选快照开始；审阅任务还可以直接以被审阅成员的真实环境为起点。

从仓库的子目录启动时，新工作树里也会停在同样的子目录，已有脚本里的相对路径还能用。

### 改动去了哪里

工作树里的改动**不会自动写回**。子成员的完成结果里可以带工作树的 diff；声明了采纳工具的入口成员要显式采纳成功的 Coder 的结果，之后再派新的 Tester，才会从更新后的交付内容开始检查。见 [Team 协作：采纳改动](../team/dispatch-team-agents.md#第四步采纳改动再验证) 和 [隔离改动怎样回到项目](candidate-delivery.md)。

### 非 Git 目录、子模块和共享资源

- 源目录不是 Git 仓库时，环境会复制一份独立副本，用文件差异记录改动。
- Git 项目还要注意已初始化的子模块、文件的可执行位和实际写到磁盘上的内容。候选准备会处理能用的源子模块，并检查实际文件内容能否重建。
- 工作树之间共用 Git 对象存储，而且只把文件编辑分开：两个任务在同一端口启动服务仍可能冲突，共用同一个外部缓存也可能互相影响。需要连进程都分开时，选容器或 VM 策略。

## 设置之后怎样确认生效

1. 派一个很小的读取任务，让它报告实际工作目录和目标文件路径。
2. 容器策略：再让它执行一条镜像里有的命令。工作树策略：检查新目录里的文件内容。
3. 跑一次项目需要的测试，确认依赖和相对路径在这个环境里能用。

想亲眼确认几种策略的区别，可以在实际任务环境里做三个小实验：让两个成员分别执行 `pwd`，看目录是否不同；让一个成员在 `/tmp` 写一个标记文件，看另一个能否看到；让一个成员启动一个服务，看另一个能否访问。

排查时，把调用代码、团队文件和任务报告的路径放在一起对照，并记住：角色写了隔离，就以角色为准。

## 隔离并不是完全隔开

- 同一个组容器或组 VM 里的成员，仍可能通过绝对路径、公共临时目录或进程互相影响。
- 隔离策略只决定**工具操作**落在哪里。模型请求仍从控制端（运行 OC 的机器）发出；通过 SDK 运行的 Workflow，Python 编排代码也留在调用方进程里（成员用 `manage_workflow` 启动的子 Workflow 例外，见 [代码到底在哪里运行](../workflow/workflow-programming-model.md#代码到底在哪里运行)）；Team 成员之间的通信仍按拓扑走。
- 项目依赖要靠镜像或环境准备过程提供。
- 宿主机上 `bash` 的授权、文件工具的路径检查、容器本身的限制，三者分别生效。处理未知代码时，选用完可以丢掉的隔离环境，并检查挂进去的素材和凭据。

## 常见问题

- **权限被拒绝**：先判断是哪一层拦的：角色工具、命令授权、路径检查，还是环境限制，再改对应的设置。
- **策略名报错**：名字只能用字母、数字、连字符和下划线，首尾带空格也会报错。
- **提示该策略需要 environment provider**：名字格式没问题，但还没注册对应的提供器，见 [使用自己的执行环境](execution-environments.md)。
- **选了 `vm-group` 或 `vm-session`，报错要求提供 `config.vm`**：还没配 VM 资源。在 SDK 里传 `config={"vm": ...}`，或在命令行设置 `OPENCOLLAB_VM`。
- **环境申请成功，却在准备工作区时失败**：比如普通工作树的源目录有未提交改动，见上文“起点是哪个版本”。
- **补丁捕获失败，或看到改动被保留的提示**：先导出完整材料，见 [找回改动](candidate-delivery.md#出了问题先找回改动再清理)。

## 延伸阅读

- 源码依据：[隔离指南](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/session-isolation.zh-CN.md) · [环境池实现](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/session_environments.py) · [入口策略解析](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/isolation.py) · [策略名解析](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/isolation.py) · [工作树环境](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/_env_worktree.py) · [工作树池](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/worktree_pool.py)
- 相关指南：[在容器中运行任务](docker-isolation.md) · [在现有 Linux VM 中运行任务](vm-environment-extension.md) · [使用自己的执行环境](execution-environments.md) · [隔离改动怎样回到项目](candidate-delivery.md)
