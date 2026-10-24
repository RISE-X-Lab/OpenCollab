---
{
  "id": "workflow-composition-hierarchy",
  "title": "Agent、Team 和 Workflow 怎么互相调用",
  "category": "workflow",
  "summary": "讲清楚 Agent、Team、Workflow 三种运行方式各管什么，以及它们嵌套使用时预算和停止由谁负责。",
  "aliases": [
    "Workflow 里面可以再跑 Workflow 吗",
    "Workflow 可以调用 Team 吗",
    "Agent 能不能开团队",
    "Team 可以跑工作流吗",
    "子工作流能继续开 Agent 吗"
  ],
  "keywords": [
    "Agent Team Workflow",
    "调用层级",
    "Workflow 组合",
    "run_workflow",
    "manage_workflow",
    "candidate_workflow",
    "DW 内多个 Agent",
    "vm-session",
    "worktree"
  ],
  "questions": [
    "Workflow 里面能再调用另一个 Workflow 吗？",
    "Team 成员怎样运行一个 Workflow？",
    "在 Workflow 里怎么启动一个 Team？",
    "嵌套运行时预算算谁的？",
    "Team 成员写出的 DW 怎样继续创建多个 Agent？",
    "Workflow 局部并发和模型共享请求容量由谁管理？",
    "VM 成员的子工作树位于哪里？"
  ],
  "related": [
    "choose-working-mode",
    "team-run-workflow",
    "member-authored-workflow",
    "compose-workflow-candidates",
    "workflow-failure-recovery-lifecycle",
    "workflow-parallel-pipeline",
    "vm-environment-extension"
  ],
  "verified_ref": "0c47f74a"
}
---

# Agent、Team 和 Workflow 怎么互相调用

OC 有三种运行入口，都返回 `RunResult`：Agent 是单个 agent loop 会话；Team 是由模型自主派发、按角色和通信关系协作的多 agent（成员自己决定找谁、做什么）；Workflow 是用异步 Python 函数显式编排 agent 调用（类似把 LangGraph 的图换成普通 Python 控制流）。

三者可以嵌套：Workflow 里调 Agent 或子流程，Team 成员调 Workflow，Workflow 里起 Team。嵌套时 OC 的默认行为各不相同，关键差别在于预算算谁的、谁负责停止、结果交给谁。本文按嵌套方向列出入口和默认规则。

## 先选入口：三种方式对照

| 方式 | 谁决定下一步 | 适合的场景 |
| --- | --- | --- |
| Agent | 单个 agent loop | 一次会话能完成 |
| Team | 成员模型自主派发 | 需要角色分工、互相审查，流程不固定 |
| Workflow | 你写的异步 Python | 步骤固定，需要程序精确控制 |

`RunResult` 里有状态、输出和产物。

## 在你的 Python 程序里分别启动三者

```python
from opencollab import OpenCollab

# 一个最简单的 Workflow：让一个 Agent 做摘要
async def summarize(ctx, args):
    return await ctx.agent(args['goal'], label='summary', tools=[])

async def run_independent_examples(workspace, team_config):
    client = OpenCollab(workspace)
    # 单次 Agent 会话
    single = await client.agent('概括工作区任务', tools=[])
    # 运行上面的 Workflow 函数，预算 10000 token
    flow = await client.workflow(
        summarize, {'goal': '解释任务中需要验证的行为'}, budget=10000,
    )
    # 启动一个 Team，team_config 要换成你实际的 Team 配置
    team = await client.team(
        '检查任务需求并提交协作结果', config=team_config, budget=30000,
    )
    return {'agent': single.status, 'workflow': flow.status, 'team': team.status}
```

这里有几点要知道：

1. 这三次是彼此独立的运行，各自返回状态、输出和产物。
2. 它们用同一个 client 配置，但预算各算各的，按每次调用传入的值。
3. 它们共用同一个工作区。前一次运行留下的改动会不会影响后一次，取决于环境策略。

## 在 Workflow 里面再调用 Agent 或子流程

Workflow 函数的第一个参数 `ctx` 是这次运行的上下文，预算、并发容量、环境策略都挂在它上面。常用做法有三种：

- 调 `ctx.agent` 或 `ctx.agent_run`：开一个属于当前 Workflow 的会话。它和当前运行共用预算和并发容量，工具按当前环境策略执行。
- 写 `await helper(ctx, args)`：把 `ctx` 传给另一个普通异步函数。这就是普通 Python 函数调用，资源仍算在当前 `ctx` 上。
- 调 `ctx.candidate_workflow(fn, args, label=...)`：把整套子流程放进一个单独的候选环境里跑。返回 `CandidateRun`，父流程检查后再决定要不要采用。

关于 `candidate_workflow` 的细节：

- 子流程里的会话受父运行管着，包括会话容量、集合容量和绝对期限（到点必须结束的时间）。
- 当前候选子上下文没有配备新的候选后端，所以子流程里不能再开一层候选环境。
- 子流程想继续拆分逻辑，可以调用普通异步函数。

详细用法见[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)。

## Team 成员怎样运行 Workflow

Team 成员有两种工具可以启动 Workflow。两种都要在角色配置里明确写上才能用。

| 入口 | 运行的是什么 | 什么时候返回 | 资源算谁的 |
| --- | --- | --- | --- |
| `run_workflow` | 启动时已经注册好的名称 | 整个工作流结束后返回 | 借用调用成员的环境，计入它的额度 |
| `manage_workflow launch` | 成员自己写的 Python 文件 | 启动后马上返回 child id | 由创建它的成员拥有并管理 |
| 普通异步子函数 | 源代码里已导入的函数 | 子函数返回时继续 | 沿用传入的 ctx |
| `candidate_workflow` | 源代码里的子流程 | 候选结果捕获并清理后返回 | 用候选环境，受父运行的容量和授权约束 |

怎么选：

- 如果成员要跑一个已知流程并等结果，先用 `run_workflow describe` 读取输入说明，再执行 `run`。见[让 Team 成员调用已注册的 Workflow](team-run-workflow.md)。
- 如果成员要先自己写代码再启动，先用 `manage_workflow describe` 读取策略、工具授权和入口示例，再写源码并 `launch`。启动后用 `status`、`wait`、`result`、`stop` 管理那个 child id。成员需要有 bash 权限，目标环境里还要有装了 OpenCollab 的 Python 解释器。见[让成员自己写并运行子 Workflow](member-authored-workflow.md)。

`manage_workflow` 的预算规则：

- 给了有限的子预算，就会从父成员的余额里预留出这部分。
- 不给预算时，取父成员未预留余额的一半。
- 父成员完成或停止时，它还在运行的子 Workflow 会被一并停止。
- child id 的查询与停止均由原创建者执行，其他成员通过 Team 已授权的通信关系联系它。

### 自编 DW 内多个 Agent 与隔离

DW 指用程序安排步骤的工作单元。成员可以现场构造三个或一百个任务，再用 `ctx.parallel` 调度。`launch.concurrency` 管理该子 DW 的活跃会话与默认集合容量，宿主 `request_capacity` 约束共享模型服务的原生请求。模型路由、凭据和请求池由宿主 bridge 管理，凭据配置放在控制端进程环境或项目工作区之外。

`launch.isolation` 设置整个子 DW 的默认策略，省略时继承成员，单次 `ctx.agent(..., isolation=...)` 可以覆盖。父成员使用 `vm-session` 获得 VM 后，可以用 `worktree` 启动子 DW，让多个 Agent 在同一 guest 的独立工作树内执行工具。单次选择 `vm-session` 则需要另一台可租用 VM。

完整的三 Agent、SDK 与 guest 解释器示例见[成员自编 DW 教程](member-authored-workflow.md)，资源准备见[VM 环境扩展](../isolation/vm-environment-extension.md)。

## 在 Workflow 里启动 Team

目前公开的 `ctx` 提供 Agent、集合和候选操作，还没有 `ctx.team`。要在业务流程里跑 Team，请用宿主 SDK 的 `OpenCollab.team`。

如果你把 `OpenCollab.team` 写进自定义 Workflow 的函数体，它仍是一次独立运行，不会自动跟外层共享预算和停止。你需要自己接好：

1. 预算：比如外层 Workflow 预算 50,000 token，内层 `oc.team(..., budget=30000)`。这 30,000 算不算在 50,000 里面，要由你的应用来决定和管理。
2. 停止：想一起停，就让应用持有内层任务，把取消信号传下去，等它清理完再释放环境。
3. 归属：产物目录、模型客户端、调用方环境都要明确由谁负责。

## 小结

- 同一个 Workflow 里调 `ctx.agent` 或普通子函数：资源都算在当前运行上，最简单。
- 要隔离着试一套方案：用 `ctx.candidate_workflow`。
- Team 成员跑流程：已注册的用 `run_workflow`，自己写的用 `manage_workflow`。
- 在 Workflow 里跑 Team：用 `OpenCollab.team`，预算和停止要自己接。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md#L121)、[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/workflows.py#L21)、[examples/member-authored-workflow/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/member-authored-workflow/README.md#L42)。

[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py)。

## 相关章节

[我该用默认 CLI、Base、Team 还是 Workflow](../orientation/choose-working-mode.md)、[让 Team 成员调用已注册的 Workflow](team-run-workflow.md)、[让成员自己写并运行子 Workflow](member-authored-workflow.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[Workflow 出错了：失败、重试与恢复](workflow-failure-recovery-lifecycle.md)。
