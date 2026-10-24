---
{
  "id": "isolation-configuration",
  "title": "默认隔离与单次覆盖怎么配",
  "category": "isolation",
  "summary": "设置整次运行的默认隔离方式，并在某个角色或某次调用上单独改掉它。",
  "aliases": [
    "角色覆盖",
    "单次覆盖",
    "use_worktrees",
    "--no-worktrees",
    "--isolation",
    "ctx.agent",
    "ctx.agent_run",
    "OpenCollab.team",
    "OpenCollab.workflow",
    "vm-group",
    "vm-session",
    "OPENCOLLAB_VM",
    "cleanup_timeout"
  ],
  "keywords": [
    "隔离配置",
    "默认策略",
    "isolation",
    "优先级"
  ],
  "questions": [
    "让 Team 角色和 Workflow 调用使用预期的执行环境？",
    "团队文件、SDK 参数和角色都写了隔离，听谁的？",
    "怎么让某一次 Agent 调用不用工作树？",
    "命令行怎么指定隔离方式？"
  ],
  "related": [
    "isolation-overview",
    "worktree-workspaces",
    "candidate-delivery",
    "vm-environment-extension",
    "member-authored-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 默认隔离与单次覆盖怎么配

OC 的 `isolation` 可以写在四个地方：Workflow 运行参数、单次 `ctx.agent` 调用、Team 文件顶层、Team 角色。Workflow 是用 Python 显式编排 agent 调用；Team 是由入口 Agent 用 `spawn_agent` 自主派发成员。本文讲每处怎么写、冲突时谁优先、怎么确认生效值。

## 一句话记住优先级

- Workflow：单次调用的值 > 运行的默认值。
- Team：角色的值 > SDK 运行参数 > 团队文件的默认值。

## Workflow：运行默认值和单次调用

```python
import asyncio
from opencollab import OpenCollab
from opencollab.tools import builtin_tools

async def inspect(ctx, inputs):
    # 没写 isolation，继承运行的 worktree
    code = await ctx.agent("阅读项目入口", tools=builtin_tools("file_read", "grep"))
    # 显式写 shared，复用父环境
    note = await ctx.agent(
        f"根据分析写一段执行摘要\n{code}", tools=[], isolation="shared"
    )
    return {"code": code, "note": note}

async def main():
    result = await OpenCollab(".").workflow(
        inspect, {}, isolation="worktree"   # 整次运行的默认值
    )
    print(result.status, result.output)

asyncio.run(main())
```

要点：

- 第一次 `ctx.agent` 继承运行的 `worktree`。
- 第二次显式选 `shared`，复用父环境。它能看到第一次的输出，是因为提示词里传了进去。
- `ctx.agent_run` 用同样的选择规则，另外还会返回执行和清理状态。
- 布尔值也可以用：`True` 等于 `worktree`，`False` 等于 `shared`。
- 什么都不写时，Workflow 使用共享环境。

## Team：三层优先关系

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

用 SDK 运行：

```python
await client.team(task, config="team.yaml", isolation="docker-session")
```

结果：

| 角色 | 最终策略 | 原因 |
| --- | --- | --- |
| `coder` | `docker-session` | 角色没写，SDK 参数覆盖团队文件 |
| `manager` | `shared` | 角色自己写了，角色优先 |

入口角色也可以单独选环境。入口被显式隔离后，它的文件改动会以完整的入口补丁返回，见 [把候选结果采纳到交付目录](candidate-delivery.md)。

## 不写新选项时的老规则

- 不写新的隔离选项时，Team 按 `use_worktrees` 处理子成员，默认是 `True`。入口 Agent 使用原工作目录。
- 只在子角色里写隔离时，入口仍用原工作目录。
- `--no-worktrees` 对应原来那种子成员目录选择方式。团队文件里显式写的策略，仍按上面的优先级生效。

## VM 配置与子运行继承

SDK 构造参数 `config={"vm": profile}` 注册 `vm-group` 和 `vm-session`，其中 `profile` 包含来宾资源、控制端共享 SQLite 路径、容量等待和清理时间。可直接修改的 JSON 与完整调用见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。显式传入的 `environment_providers` 同名工厂优先。

`group` 是每次运行中对应环境池的范围。成员启动自己编写的子 Workflow 后，子运行拥有自己的环境池。需要让子 Agent 留在成员当前 VM 中时，在 `manage_workflow` 的 `launch` 中设置 `isolation="worktree"`。省略 `launch.isolation` 时继承成员策略，单次子调用仍可覆盖。使用 `vm-session` 申请其他来宾时，需要父 VM 之外的空闲机器。示例见 [让成员编写并管理自己的子 Workflow](../workflow/member-authored-workflow.md)。

VM profile 的 `cleanup_timeout` 默认为 `60` 秒，显式 SDK 运行参数与子运行 `launch.cleanup_timeout` 优先。它控制清理等待时间，机器容量等待由 `wait_timeout` 单独配置。

## 命令行

- Workflow：`workflow run NAME` 后面用 `--isolation` 指定策略名，例如 `shared`、`worktree`、`vm-group` 或 `vm-session`。
- Team：根命令从 `--team-config team.yaml` 读取隔离配置。
- 需要 DockerOptions 或自定义环境提供器时，用 SDK 组合。见 [在容器中运行任务](docker-isolation.md)。

VM CLI 从 `OPENCOLLAB_VM` 读取完整 JSON，Workflow 可用 `--cleanup-timeout` 覆盖清理时间。Team 文件中的策略使用同一份 VM 配置。来宾准备和 JSON 示例见 [在现有 Linux VM 中运行任务](vm-environment-extension.md)。

## 设置后怎样确认生效

1. 派一个很小的读取任务，让它报告实际工作目录和目标文件路径。
2. 如果是容器策略，再让它执行一条镜像里有的命令。
3. 如果是工作树策略，检查新目录里的文件内容。
4. 跑一次项目需要的测试，确认依赖和相对路径在这个环境里能用。

排查时，把调用代码、团队文件和任务报告的路径放在一起对照。记住角色写了隔离，就以角色为准。

## 常见问题

- 如果策略名报错：名字只能用字母、数字、连字符和下划线，首尾带空格也会报错。
- 如果提示该策略需要 environment provider：名字格式没问题，但还没注册对应的提供器。见 [接入自己的执行环境](custom-environment-providers.md)。
- 如果环境申请成功了，却在准备工作区时失败：比如普通工作树的源目录有未提交改动。检查工作区内容和走的是哪种创建方式，处理方法见 [让多个任务在独立工作区改代码](worktree-workspaces.md)。

选择 VM 策略却缺少资源时，错误会要求提供 `config.vm` 或对应环境提供器。

## 参考

这些规则分别来自[入口策略解析](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/isolation.py)、[策略名解析](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/isolation.py)和[隔离指南](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/session-isolation.zh-CN.md)。
