---
{
  "id": "workflow-control-granularity",
  "title": "在哪一层调整 Workflow：从 CLI 参数到单次调用回调",
  "category": "workflow",
  "summary": "一个控制需求该放在 CLI 参数、SDK 运行设置、ctx 单次调用，还是 RunControl 回调里。",
  "aliases": [
    "有哪些开发档位",
    "高级和低级怎么选",
    "什么参数能控制工作流",
    "什么时候需要 Python SDK",
    "能控制每个 Agent 吗",
    "能接管每次模型请求吗",
    "控制粒度",
    "RunControl 回调",
    "软额度"
  ],
  "keywords": [
    "Workflow 控制粒度",
    "CLI",
    "SDK",
    "RunControl"
  ],
  "questions": [
    "一个设置应该放在 CLI、SDK 还是单次 Agent 调用上？",
    "什么时候必须用 Python SDK？",
    "能单独控制每个 Agent 的额度和步数吗？",
    "怎样在运行中动态调整某个 Agent 的预算？",
    "超时到底发生在哪一层？"
  ],
  "related": [
    "workflow-programming-model",
    "run-registered-workflow",
    "workflow-agent-calls",
    "workflow-public-api-map"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在哪一层调整 Workflow：从 CLI 参数到单次调用回调

OC 给 Workflow 提供了四层控制入口，从粗到细依次是：现成工作流的输入参数、SDK 的运行设置、`ctx.agent` / `ctx.agent_run` 的单次调用参数、`RunControl` 回调。决定一个要求放在哪一层，先问它影响的是整次运行、某个角色，还是一次具体的模型或工具请求。本篇按这四层依次给出可用参数和示例。

## 四层一览

| 层 | 入口 | 能控制什么 |
| --- | --- | --- |
| 1. 现成工作流输入 | CLI `--args` | 该工作流自己定义的字段 |
| 2. 整次运行 | CLI 运行选项，或 SDK `workflow(...)` 参数 | 总额度、时限、并发、默认环境、记录 |
| 3. 单个角色 | `ctx.agent`、`ctx.agent_run` | 本次会话的工具、额度、时限、步数、schema、环境 |
| 4. 角色运行中 | `RunControl` 回调 | 在已授权范围内动态调整软额度、收集事件 |

除了第 1 层，自定义函数体本身也是控制入口：分支、循环、动态创建任务、处理结果都写在这里。比如根据结构化审阅结果进入修复轮，或时间不够时返回已有产物。

## 第 1 层：现成工作流的输入

CLI 用名称选工作流，用 `--args` 传入它认识的 JSON 字段。例如：

| 工作流 | 字段 | 作用 |
| --- | --- | --- |
| Duo | `submission_mode` | 候选的交付方式 |
| Weave | `groups`、`check_commands` | 分组任务和检查命令 |
| Reconstruct | `max_iterations` | 探索迭代次数 |

这些字段只对对应的内置工作流有效。

## 第 2 层：整次运行

CLI 的运行选项写在 `workflow run NAME` 后面：

| CLI 选项 | 作用 |
| --- | --- |
| `--budget` | 整次运行共享的额度 |
| `--concurrency` | 同时活跃的 Agent 会话上限 |
| `--task-concurrency` | 集合单元（`parallel`/`pipeline` 的项）上限 |
| `--isolation` | 角色的默认环境策略 |

用 SDK 可以设置更多：

```python
from opencollab import OpenCollab

async def limited_flow(ctx, args):
    # 第 3 层：这个角色单独限 6000 token
    return await ctx.agent(args['goal'], tools=[], label='answer', budget=6000)

async def launch(workspace):
    # 第 2 层：整次运行的设置
    return await OpenCollab(workspace).workflow(
        limited_flow,
        {'goal': '解释给定问题'},
        budget=20000,
        concurrency=2,
        task_concurrency=2,
        timeout=180,
        max_steps=20,
        cleanup_timeout=3,
        isolation='shared',
        limit_mode='explicit',
    )
```

SDK 还能设置 `artifacts`、`trace`、`run_id`、`agent_profile`、`system_prompt`、`candidate_workspace` 和 `llm`。完整参数表见[Workflow 公开 API 速查](workflow-public-api-map.md)。

## 第 3 层：单个角色

| 方法 | 可设置 | 返回 |
| --- | --- | --- |
| `ctx.agent` | 工具、额度、调用时限、结构化 schema、环境 | 输出值 |
| `ctx.agent_run` | 以上大部分，外加本次的 `max_steps`、`system_prompt`、`run_control`、`cleanup_timeout` | 可检查的执行快照 |

OC 特有的预算行为：

- 申请的额度会受共享池和其它预留影响。实际拿到的硬授权可能比申请的小。
- 硬额度（hard budget）限制总用量。软额度（soft budget）用于判断会话该继续还是收尾。
- `agent_run` 传入的 `system_prompt` 会替换本次默认的系统提示。Agent profile 里的工具配置、上下文处理和安全行为仍然保留。

## 第 4 层：用 `RunControl` 在授权内参与决策

下面是完整的工作流，可以直接传给 SDK。它在运行中逐步扩大软额度，同时记录 usage 事件。外层预算要覆盖这个角色的实际申请。

```python
from opencollab import BudgetDecision, RunControl

async def controlled_answer(ctx, args):
    usage = []

    def on_event(event):
        # 同步执行：只做简短收集
        if event.type == 'usage':
            usage.append(event.data['total_tokens'])

    def decide(snapshot):
        # 每次在当前软额度上加 2000，但不超过硬授权
        current = snapshot.soft_budget_tokens or max(1, snapshot.used_tokens)
        proposed = current + 2000
        if snapshot.hard_budget_tokens is not None:
            proposed = min(proposed, snapshot.hard_budget_tokens)
        return BudgetDecision(soft_budget_tokens=proposed)

    run = await ctx.agent_run(
        args['goal'], tools=[], label='controlled', budget=8000,
        run_control=RunControl(
            initial_soft_budget_tokens=4000,
            decide_budget=decide,
            on_event=on_event,
        ),
    )
    return {'answer': run.output, 'status': run.status, 'usage_events': usage}
```

回调规则：

- `decide_budget` 可以是同步或异步函数。它收到 `BudgetSnapshot`，返回 `BudgetDecision`。
- 软额度必须有效、不能递减，并且受实际硬授权限制。
- `on_event` 在记录用量时同步执行。耗时工作请由应用另行处理。
- 如果观察回调（如 `on_event`）抛异常，异常会进入 `observation_errors`，不影响会话。如果预算策略出错，则会影响会话执行。

## 时限与资源分别在哪层

| 设置位置 | 主要参数 | 影响范围 |
| --- | --- | --- |
| SDK Workflow | `budget`、`timeout`、两种 concurrency | 整次运行和共享资源 |
| 角色调用 | `budget`、`timeout`、`max_steps` | 一次新会话 |
| `RunControl` | 软额度、预算回调、事件回调 | 授权范围内的角色决策和观察 |
| 模型配置 | 输出上限、请求时限、连接和流等待设置 | 单次模型请求 |
| 清理设置 | `cleanup_timeout` 等 | 已停止任务的收尾和持久化 |

运行中可用 `ctx.seconds_left()` 读剩余时间，`ctx.time_low()` 表示是否已进入预留时间段。

## 排错：超时或停止发生在哪一层

单次模型请求超时、整次运行到期、调用清理超时，会分别记录在各自的层级。调试时：

1. 先看具体的停止原因（`reason`）和清理状态（如 `cleanup_complete`）。
2. 确定限制发生在哪一层。
3. 再去调整那一层的参数。

## 源码依据

[opencollab/adapters/cli/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/workflow.py#L76)、[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L408)、[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/workflows.py#L21)、[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L18)。

## 相关章节

[工作流就是普通 Python：编程模型入门](workflow-programming-model.md)、[运行一个工作流（Workflow）](run-registered-workflow.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[Workflow 公开 API 速查](workflow-public-api-map.md)。
