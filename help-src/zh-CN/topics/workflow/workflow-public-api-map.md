---
{
  "id": "workflow-public-api-map",
  "title": "Workflow API 速查",
  "category": "workflow",
  "summary": "Workflow 的接口目录：先按“整次运行、单次调用、运行中回调”三层找到控制入口，再查 SDK 参数、ctx 方法、结果类型和模块导出。",
  "aliases": [
    "我要用哪个接口",
    "完整 Workflow 开发接口在哪",
    "ctx 有哪些方法",
    "哪些模块可以直接 import",
    "如何扩展自己的工具",
    "Workflow API 速查",
    "ctx 方法列表",
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
    "Workflow API",
    "公开接口",
    "opencollab.workflows",
    "能力导航",
    "Workflow 控制粒度",
    "CLI",
    "SDK",
    "RunControl"
  ],
  "questions": [
    "我要做某件事，该用哪个接口？",
    "ctx 有哪些方法？",
    "OpenCollab.workflow 有哪些参数和默认值？",
    "哪些模块和类可以直接 import？",
    "怎样接入自己的工具或环境？",
    "一个设置应该放在 CLI、SDK 还是单次 Agent 调用上？",
    "什么时候必须用 Python SDK？",
    "能单独控制每个 Agent 的额度和步数吗？",
    "怎样在运行中动态调整某个 Agent 的预算？",
    "超时到底发生在哪一层？"
  ],
  "related": [
    "workflow-programming-model",
    "workflow-agent-calls",
    "workflow-composition-hierarchy",
    "token-step-concurrency-limits",
    "tool-selection"
  ],
  "verified_ref": "0c47f74a"
}
---

# Workflow API 速查

这是 Workflow 的接口目录。入口只有一个：`OpenCollab(workspace).workflow(flow, inputs, ...)`；你的函数收到的 `ctx`（`WorkflowContext`）提供会话、集合、候选、验证和观察方法。查接口之前，先用第一节判断你的需求属于哪一层，再到对应小节查参数。

## 先找对层：一个设置该放在哪

一个控制需求该写在哪，先问它影响的是**整次运行**、**某一次调用**，还是**一次调用进行中的决策**。从粗到细有四层：

| 层 | 入口 | 能控制什么 |
| --- | --- | --- |
| 1. 现成 Workflow 的输入 | CLI `--args` 或 SDK `inputs` | 该 Workflow 自己定义的字段 |
| 2. 整次运行 | CLI 运行选项，或 SDK `workflow(...)` 参数 | 总额度、期限、并发、默认环境、记录 |
| 3. 单次调用 | `ctx.agent`、`ctx.agent_run` 的参数 | 这次会话的工具、额度、期限、步数、schema、环境 |
| 4. 调用进行中 | `RunControl` 回调 | 在已授权范围内调整软额度、接收事件 |

除此之外，函数体本身就是最灵活的控制入口：分支、循环、动态创建任务、处理结果都写在这里。比如根据结构化审阅结果进入修复轮，或者时间不够时返回已有产物。

第 1 层的字段只对对应的 Workflow 有效，例如 Duo 的 `submission_mode`、Weave 的 `groups` 和 `check_commands`、Reconstruct 的 `max_iterations`。第 2 层在 CLI 上对应 `workflow run NAME` 后面的 `--budget`、`--concurrency`、`--task-concurrency`、`--isolation`，SDK 能设置的更多，见下面的参数表。

期限和资源也分层设置：

| 设置位置 | 主要参数 | 影响范围 |
| --- | --- | --- |
| SDK `workflow()` | `budget`、`timeout`、两种 concurrency | 整次运行和共享资源 |
| 单次调用 | `budget`、`timeout`、`max_steps` | 一次新会话 |
| `RunControl` | 软额度、预算回调、事件回调 | 授权范围内的决策和观察 |
| 客户端模型配置 | 输出上限、请求超时、连接和流等待 | 单次模型请求，见 [运行怎样停下](../operations/timeouts-retries.md) |
| 清理设置 | `cleanup_timeout` | 已停止任务的收尾和持久化 |

排错时，单次模型请求超时、整次运行到期、调用清理超时会分别记录在各自的层级。先看具体的停止原因（`reason`）和清理状态（如 `cleanup_complete`），确定限制发生在哪一层，再去调那一层的参数。

## 按需求查接口

| 我要做什么 | 用什么 |
| --- | --- |
| 启动一次运行 | `OpenCollab(workspace).workflow(...)` |
| 让 Workflow 能按名称被发现 | `@workflow(name=..., description=..., phases=...)` |
| 调一次 Agent，拿文本或字典 | `ctx.agent` |
| 调一次 Agent，拿执行快照、控制预算 | `ctx.agent_run` 加 `RunControl` |
| 同时跑一批独立任务 | `ctx.parallel` |
| 每项依次走多个阶段 | `ctx.pipeline` |
| 在隔离环境里试一个方案，再决定是否采纳 | `ctx.candidate_agent` / `ctx.candidate_workflow` + `ctx.adopt_candidate` |
| 看改动、跑验证 | `ctx.diff`、`ctx.source_changed`、`ctx.execute_verification` |
| 记录阶段和日志 | `ctx.phase`、`ctx.log` |
| 查剩余额度和时间 | `ctx.tokens_remaining`、`ctx.seconds_left`、`ctx.time_low` |
| 给 Agent 配工具 | `opencollab.tools.builtin_tools`；自定义工具实现 `Tool` |
| 自定义执行环境 | `opencollab.environments` 的 `EnvironmentPool` |
| 读最终结果 | `RunResult` |

## 启动一次运行

```python
from opencollab import OpenCollab

async def simple_flow(ctx, args):
    return await ctx.agent(args['goal'], tools=[], label='answer')

async def run_example(workspace):
    result = await OpenCollab(workspace).workflow(
        simple_flow, {'goal': '解释一次普通函数调用的执行顺序'},
        budget=10000, concurrency=2, task_concurrency=2,
        timeout=120, max_steps=20, cleanup_timeout=2.0,
        trace=True, limit_mode='explicit', isolation='shared',
    )
    # 非 completed 时 raise_for_status() 抛 RunError
    return result.raise_for_status().output
```

- `flow` 可以是注册名称、异步函数或 `WorkflowSpec`；`inputs` 是键为字符串的 Mapping；函数的返回值就是 `RunResult.output`。
- 装饰器可以从 `opencollab` 或 `opencollab.workflows` 导入，参数为 `name`、`description`、`phases`。

### `workflow()` 参数

| 参数 | 默认或要求 | 作用 |
| --- | --- | --- |
| `budget` | 默认读取配置 | 整个 Workflow 共享的 token 额度 |
| `concurrency` | 4 | 同时活跃的 Agent 会话上限 |
| `task_concurrency` | 默认等于 `concurrency` | 集合单元（`parallel`/`pipeline` 的项）上限，也覆盖候选子流程 |
| `timeout` | 默认不限时 | 整次运行的期限 |
| `max_steps` | 100，可为 `None` | 各角色默认的步数上限 |
| `system_prompt` | 可选，非空文字 | 角色的默认系统提示 |
| `agent_profile` | 可选 | 所有角色共用的 Agent 配置 |
| `cleanup_timeout` | 显式给出时须为有限正数；省略时配置了 VM 用 `vm.cleanup_timeout`（默认 60 秒），否则 2 秒 | 等待所属任务清理的时间 |
| `artifacts` | 可选路径，须为新目录或空目录 | 持久产物目录 |
| `trace` | `True` | 保存编排轨迹 |
| `candidate_workspace` | 可选后端对象 | 注入候选的分配、捕获和采纳方式 |
| `run_id` | 默认自动生成 | 关联清单、会话和轨迹 |
| `llm` | 可选，符合协议的对象 | 注入模型传输层或测试替身 |
| `limit_mode` | `environment`，可选 `explicit` | 是否读取环境里的无限额度设置，见 [无限制模式](../operations/token-step-concurrency-limits.md#无限制模式) |
| `isolation` | 可选策略名 | Agent 的默认执行环境 |

`OpenCollab` 构造器负责更外层的选择：`workspace` 工作目录、`environment` 传入调用方自己管理的执行环境、`environment_providers` 注册环境策略工厂，以及模型、provider 和其他客户端配置。CLI 的参数范围用 `workflow run NAME --help` 查看。

## 会话操作

| 方法 | 公开参数 | 返回值 |
| --- | --- | --- |
| `agent(prompt, ...)` | `schema`、`label`、`tools`、`isolation`、`budget`、`timeout`、`tool_choice`、`thinking`、`over_budget_ok` | 文本、校验后的字典，或 `None` |
| `agent_run(prompt, ...)` | `label`、`tools`、`isolation`、`budget`、`timeout`、`max_steps`、`system_prompt`、`run_control`、`cleanup_timeout` | `WorkflowAgentResult` |
| `draft_findings(prompt, ...)` | `label`、`budget` | 证据草稿字典或 `None` |

- `tools`：`agent` 接受工具对象序列，运行实现也支持已配置的工具组；`agent_run` 接受工具组名称或对象序列。
- `isolation`：接受布尔值、策略字符串或省略。省略时继承当前策略，`True` 选 `worktree`，`False` 选 `shared`。公开 Protocol 目前的类型注解更窄，开发时可对照实现和 SDK 用例。
- `schema`：对象形状的 JSON Schema，捕获失败时 OC 会纠正一次。
- `draft_findings`：探索前先收集已有证据的草稿，之后仍需实际探索和验证。

`WorkflowAgentResult` 字段：`output`、`status`、`reason`、`tokens`、`steps`、`session_id`、`hard_budget_tokens`、`soft_budget_tokens`、`cleanup_complete`、`workspace_ready`、`observation_errors`。`cleanup_complete` 为真之后用量才是稳定值；后续步骤依赖同一个环境时，还要检查 `workspace_ready`。用法见 [在 Workflow 里调用 Agent](workflow-agent-calls.md)。

## 调用进行中：`RunControl`

下面的 Workflow 在运行中逐步扩大一次调用的软额度，同时记录用量事件。外层预算要覆盖这次调用的实际申请。

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

- **硬额度**限制总用量；**软额度**用于判断会话该继续还是收尾。申请的额度会受共享池和其他预留影响，实际拿到的硬授权可能比申请的小。
- `decide_budget` 可以是同步或异步函数，收到 `BudgetSnapshot`，返回 `BudgetDecision`。新的软额度必须有效、不能递减，并且受硬授权限制。
- `on_event` 在记录用量时同步执行，耗时工作请交给应用另行处理。观察回调抛出的异常进入 `observation_errors`，不影响会话；预算策略出错则会影响会话执行。
- 交接提示 `final_prompt` 等更多用法见 [按进展扩展软额度](../operations/token-step-concurrency-limits.md#按进展扩展软额度)；事件字段见 [实时事件](../observability/artifacts-and-history.md#实时订阅事件)。

## 集合、组合与候选

| 方法 | 输入 | 输出与行为 |
| --- | --- | --- |
| `parallel(thunks)` | 无参异步函数的序列 | 按输入顺序返回列表 |
| `pipeline(items, *stages, stop_on_none=True)` | 输入序列和逐项阶段 | 每个阶段收到上一阶段的值、原输入和索引 |
| 普通子函数 | `await subflow(ctx, args)` | 共用 `ctx` 和资源，返回值自定义 |
| `candidate_agent(prompt, ...)` | 必填 `label`，可配 `tools`、`budget`、`timeout`、`tool_choice`、`thinking` | 单会话的 `CandidateRun` |
| `candidate_workflow(workflow_fn, args, ...)` | 异步函数、字典、必填 `label`、可选 `budget` | 整个子流程的 `CandidateRun` |
| `adopt_candidate(candidate, preserve_paths=())` | 候选对象和要保留的路径 | 采纳完成后返回 `None` |

- `pipeline` 的实现在 `workflow_collections.py`，有 SDK 嵌套集合测试覆盖；公开 Protocol 的注解漏列了它，编辑器可能提示属性不存在，照常调用即可。
- 集合里单元抛出的普通异常会变成该位置的 `None`；预算耗尽、环境撤销和取消继续向上传播。
- 候选子流程内部目前没有注入新的候选后端，子流程里要继续拆分，用普通子函数。

`CandidateRun` 字段：`label`、`output`、`diff`、`test_records`、`verified_targets`、`lifecycle_errors`、`source_revision`、`delivery_diff`、`delivery_paths`、`delivery_source_diff`，以及属性 `effective_diff`。含义见 [读取候选结果](workflow-composition-hierarchy.md#读取候选结果)。

## 工作区、验证与运行记录

| 方法或属性 | 输入与返回 | 用途 |
| --- | --- | --- |
| `diff(exclude_paths=())` | 改动文本或 `None` | 读取可观察到的差异 |
| `source_changed(exclude_paths=())` | `True`、`False` 或 `None` | 排除指定路径后，探测源码是否有变化 |
| `execute_verification(tool, params)` | 验证工具和参数 Mapping，返回字符串 | 在绑定环境里实际执行验证工具 |
| `phase(title)` | 异步，返回 `None` | 标记当前阶段 |
| `log(message)` | 异步，返回 `None` | 写流程日志 |
| `tokens_spent()` | 整数 | 已观察到的 token 消耗 |
| `tokens_remaining()` | 余额，无上限时为无穷 | 决定后续工作规模 |
| `seconds_left()` | 秒数，无期限时为无穷 | 读取剩余的绝对期限 |
| `time_low()` | 布尔值 | 是否已进入运行的预留时间段 |
| `workspace_root`、`host_workspace` | 路径字符串或 `None` | 定位绑定环境和宿主工作区 |
| `run_id` | 字符串或 `None` | 关联一次运行的记录 |

探测返回 `None` 表示信息暂时拿不到，不等于“没有改动”，业务代码应保留这个状态。同一个文件在工具环境和宿主 Python 里的路径可能不同，做文件操作前先确认代码在哪执行。

## 工具、环境和内置 Workflow

**`opencollab.tools`** 导出 `Tool`、`VerificationTool`、`builtin_tools`、`evidence_tools` 和证据辅助函数：

- 内置工具名：`bash`、`file_read`、`file_write`、`apply_patch`、`git_diff`、`grep`、`submit`，可按角色组合；`BuiltinToolName` 是公开的工具名类型。
- `profile_tool_names(profile)` 返回某个 Agent 配置的默认工具名，`profile_tool_limits(profile)` 返回它的默认输出上限。
- `VerificationTool` 额外提供 `verified_targets`；`BashEvidence` 包装 Bash 并保留真实测试记录；`has_pass_evidence(returncode, output, runner=..., target=...)` 按对应 runner 解析“测试通过”的证据。
- 用法见 [内置工具](../extensions/tool-selection.md)、[自定义工具](../extensions/custom-tools.md) 和 [测试证据](../observability/run-results.md#用测试证据确认做对了)。

**`opencollab.environments`** 导出环境协议、Docker 配置，以及 VM 相关的 `VMConnection`、`VMEnvironment`、`VMResource` 和 `vm_isolation_provider`。自定义 `EnvironmentPool` 需提供 `acquire`、`release_env`、`release` 和 `adopt`，它的工厂按“一次运行 + 一种策略”创建资源。见 [使用自己的执行环境](../isolation/execution-environments.md)。

**`opencollab.builtin_workflows`** 导出 Duo、Weave、Reconstruct 及其定制接口：

- `get_builtin_workflows()` 返回一个包含 `duo`、`weave`、`reconstruct` 的新注册表，用 `list_specs()` 读取名称、说明和阶段。
- Duo：`run_dual_coder` 和 `CONTRACT_PROMPT`，用来定制角色和仲裁，见 [Duo](duo-workflow.md)。
- Weave：`WeaveGroup`、`WeaveCheck`、`WeaveConfig`、`WeaveState`、`WeaveAdapter`、`plan_weave_groups` 和 `run_weave`，用于自定义分组、验证和按状态续跑，见 [Weave](weave-workflow.md)。
- Reconstruct：`ReconstructionTask` 和 `register_task`，连接隐藏程序与候选执行器，见 [Reconstruct](reconstruct-workflow.md)。

## 结果与控制类型

`RunResult` 字段：`output`、`status`、`reason`、`tokens`、`artifacts`、`error`、`metrics`、`agent_failures`。`ok` 对应 `completed`；`raise_for_status()` 在其他状态下抛出带结果的 `RunError`。见 [判断任务是否真的完成](../observability/run-results.md)。

| 模块 | 公开导出 |
| --- | --- |
| `opencollab`、`opencollab.sdk` | 同一组八个：`OpenCollab`、`workflow`、`RunResult`、`RunError`、`RunControl`、`BudgetSnapshot`、`BudgetDecision`、`RunEvent` |
| `opencollab.workflows` | `workflow`、`WorkflowContext`、`WorkflowAgentResult`、`CandidateRun` |

普通函数可以直接交给 SDK；要按名称发现，才需要 `@workflow` 注册。评测集成也使用上面这些 SDK、环境和验证工具入口；数据集、隐藏测试和正式评分由外部评测包负责，见 [OC 的边界与 OCE 评测](../architecture/extension-and-evaluation-boundaries.md)。

## 延伸阅读

- 源码依据：[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/workflows.py) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py) · [opencollab/application/workflow_agent_run.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_agent_run.py) · [opencollab/application/workflow_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_collections.py) · [opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/tools.py) · [opencollab/adapters/cli/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/cli/workflow.py) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/evaluation-runtime.md)
- 相关指南：[编写 Workflow](workflow-programming-model.md) · [在 Workflow 里调用 Agent](workflow-agent-calls.md) · [组合与嵌套](workflow-composition-hierarchy.md) · [预算、步数与并发](../operations/token-step-concurrency-limits.md)
