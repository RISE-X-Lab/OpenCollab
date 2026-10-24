---
{
  "id": "workflow-public-api-map",
  "title": "Workflow 公开 API 速查",
  "category": "workflow",
  "summary": "按“我要做什么”查到对应的 SDK 入口、ctx 方法、工具和结果类型。",
  "aliases": [
    "我要用哪个接口",
    "完整 Workflow 开发接口在哪",
    "ctx 有哪些方法",
    "哪些模块可以直接 import",
    "如何扩展自己的工具",
    "Workflow API 速查",
    "ctx 方法列表"
  ],
  "keywords": [
    "Workflow API",
    "公开接口",
    "opencollab.workflows",
    "能力导航"
  ],
  "questions": [
    "我要做某件事，该用哪个接口？",
    "ctx 有哪些方法？",
    "OpenCollab.workflow 有哪些参数和默认值？",
    "哪些模块和类可以直接 import？",
    "怎样接入自己的工具或环境？"
  ],
  "related": [
    "author-custom-workflow",
    "workflow-agent-calls",
    "workflow-parallel-pipeline",
    "compose-workflow-candidates",
    "workflow-testing-debugging"
  ],
  "verified_ref": "0c47f74a"
}
---

# Workflow 公开 API 速查

本篇是 OC Workflow 的接口目录。入口只有一个：`OpenCollab(workspace).workflow(flow, inputs, ...)`；你的异步函数收到的 `ctx`（`WorkflowContext`）提供会话、集合、候选、验证和观察方法。常规开发从 `ctx.agent` 起步；需要逐次控制停止、预算和清理时，改用 `ctx.agent_run`。先看下面的速查表，再到对应小节查参数。

## 按需求查接口

| 我要做什么 | 用什么 |
| --- | --- |
| 启动一次运行 | `OpenCollab(workspace).workflow(...)` |
| 让工作流能按名称被发现 | `@workflow(name=..., description=..., phases=...)` |
| 调一次 Agent，拿文本或字典 | `ctx.agent` |
| 调一次 Agent，拿完整执行快照、控制预算 | `ctx.agent_run` 加 `RunControl` |
| 同时跑一批独立任务 | `ctx.parallel` |
| 每项依次走多个阶段 | `ctx.pipeline` |
| 在隔离环境里试一个方案，再决定是否采用 | `ctx.candidate_agent` / `ctx.candidate_workflow` + `ctx.adopt_candidate` |
| 看改动、跑验证 | `ctx.diff`、`ctx.source_changed`、`ctx.execute_verification` |
| 记录阶段和日志 | `ctx.phase`、`ctx.log` |
| 查剩余额度和时间 | `ctx.tokens_remaining`、`ctx.seconds_left`、`ctx.time_low` |
| 给 Agent 配工具 | `opencollab.tools.builtin_tools`，自定义工具实现 `Tool` |
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

- `flow` 可以是注册名称、异步函数或 `WorkflowSpec`。
- `inputs` 是键为字符串的 Mapping。
- 函数返回值就是 `RunResult.output`。
- 装饰器可从 `opencollab` 或 `opencollab.workflows` 导入，参数为 `name`、`description`、`phases`。

### `workflow()` 参数

| SDK 参数 | 默认或要求 | 作用 |
| --- | --- | --- |
| `budget` | 默认读取配置 | 整个工作流共享的 token 额度 |
| `concurrency` | 4 | 同时活跃的 Agent 会话上限 |
| `task_concurrency` | 默认等于 `concurrency` | 集合单元（`parallel`/`pipeline` 的项）上限，也覆盖候选子流程 |
| `timeout` | 默认不限时 | 整次运行的期限 |
| `max_steps` | 100，可为 `None` | 各角色默认的步数上限 |
| `system_prompt` | 可选，非空文字 | 工作流角色的默认系统提示 |
| `agent_profile` | 可选 | 选择共用的 Agent 实现配置 |
| `cleanup_timeout` | 省略时按配置选择，显式值为有限正数 | Workflow 采用 `config.vm.cleanup_timeout`，VM profile 默认 60 秒，未配置 VM 时为 2 秒 |
| `artifacts` | 可选路径 | 持久产物目录，须为新目录或空目录 |
| `trace` | `True` | 保存编排观察记录 |
| `candidate_workspace` | 可选后端对象 | 注入候选的分配、捕获和采用方式 |
| `run_id` | 默认自动生成 | 关联清单、会话和轨迹 |
| `llm` | 可选，符合协议的对象 | 注入模型传输层或测试替身 |
| `limit_mode` | `environment` | 可选 `explicit`；决定是否读取环境里的无限额度设置 |
| `isolation` | 可选策略名 | Agent 的默认环境策略 |

`OpenCollab` 构造器参数：

- `workspace`：工作目录。
- `environment`：传入调用方自己管理的执行环境。
- `environment_providers`：注册环境策略工厂。
- 模型、provider 和普通配置也在构造器里选。

SDK 和 CLI 的参数覆盖范围各自定义。CLI 可以用 `workflow run NAME --help` 查看。

## 会话操作

| 方法 | 公开参数 | 返回值 |
| --- | --- | --- |
| `agent(prompt, ...)` | `schema`、`label`、`tools`、`isolation`、`budget`、`timeout`、`tool_choice`、`thinking`、`over_budget_ok` | 文本、校验后的字典，或 `None` |
| `agent_run(prompt, ...)` | `label`、`tools`、`isolation`、`budget`、`timeout`、`max_steps`、`system_prompt`、`run_control`、`cleanup_timeout` | `WorkflowAgentResult` |
| `draft_findings(prompt, ...)` | `label`、`budget` | 证据草稿字典或 `None` |

参数说明：

- `tools`：`agent` 接受工具对象序列，运行实现也支持已配置的工具组。`agent_run` 接受工具组名称或对象序列。
- `isolation`：运行实现接受布尔值、策略字符串或省略。省略时继承当前策略，`True` 选 worktree，`False` 选 shared。公开 Protocol 目前的类型注解更窄，开发时可对照实现和 SDK 用例确认。
- `schema`：传对象形状的 JSON Schema。捕获失败时，OC 会做一次纠正尝试。
- `tool_choice`、`thinking`：在自由文本路径上按调用配置模型行为。
- `draft_findings`：探索前先收集已有证据的草稿，之后仍需实际探索和验证。

`WorkflowAgentResult` 字段：`output`、`status`、`reason`、`tokens`、`steps`、`session_id`、`hard_budget_tokens`、`soft_budget_tokens`、`cleanup_complete`、`workspace_ready`、`observation_errors`。

- `cleanup_complete` 为真之后，用量才是稳定值。
- 后续步骤如果依赖同一个环境，还要检查 `workspace_ready`。

## 集合、组合与候选

| 方法 | 输入 | 输出与行为 |
| --- | --- | --- |
| `parallel(thunks)` | 无参异步函数的序列 | 按输入顺序返回列表 |
| `pipeline(items, *stages, stop_on_none=True)` | 输入序列和逐项阶段 | 每个阶段收到上一阶段的值、原输入和索引 |
| 普通子函数 | `await subflow(ctx, args)` | 共用 `ctx` 和资源，返回值自定义 |
| `candidate_agent(prompt, ...)` | 必填 `label`，可配 `tools`、`budget`、`timeout`、`tool_choice`、`thinking` | 单会话的 `CandidateRun` |
| `candidate_workflow(workflow_fn, args, ...)` | 异步函数、字典、必填 `label`、可选 `budget` | 整个子流程的 `CandidateRun` |
| `adopt_candidate(candidate, preserve_paths=())` | 候选对象和要保留的路径 | 采用完成后返回 `None` |

注意事项：

- `pipeline` 的真实签名是 `pipeline(items, *stages, stop_on_none=True)`，实现在 `workflow_collections.py`。当前公开 Protocol 的注解漏了这个方法，编辑器可能提示属性不存在。可对照 SDK 的嵌套集合用例。
- 集合里单元抛出的普通异常会变成该单元的 `None`。预算耗尽、环境撤销和取消则按各自层级继续向上传播。
- 候选（candidate）是 OC 的概念：在独立环境里跑一个方案，产出 diff 和测试记录，再由你决定是否用 `adopt_candidate` 合入。执行、选择、采用是三个可分别观察的步骤。
- 候选子流程内部目前没有注入新的候选后端。子流程里要继续拆分，可以用普通子函数。

`CandidateRun` 字段：`label`、`output`、`diff`、`test_records`、`verified_targets`、`lifecycle_errors`、`source_revision`、`delivery_diff`、`delivery_paths`、`delivery_source_diff`，以及属性 `effective_diff`。

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

- 探测返回 `None` 表示信息暂时拿不到，不等于“没有改动”。业务代码应保留这个状态。
- 同一文件在工具环境和宿主 Python 里的路径可能不同。做文件操作前，先确认代码在哪执行。

## 工具

`opencollab.tools` 导出 `Tool`、`VerificationTool`、`builtin_tools`、`evidence_tools` 和证据辅助函数。

- 内置工具名：`bash`、`file_read`、`file_write`、`apply_patch`、`git_diff`、`grep`、`submit`，可按角色组合。
- `VerificationTool` 额外提供 `verified_targets`。验证结果应来自真实的工具执行。
- `profile_tool_names(profile)` 返回默认配置的工具名元组，`profile_tool_limits(profile)` 返回独立的上限字典。
- `BuiltinToolName` 是公开的工具名类型。
- `BashEvidence` 包装 Bash，并保留真实测试记录。
- `has_pass_evidence(returncode, output, runner=..., target=...)` 按对应 runner 解析“测试通过”的证据。

可配置的输出字段和完整构造例子见[选择 Agent 能使用的工具](../extensions/tool-selection.md)。自定义业务操作见[把自己的函数接成工具](../extensions/custom-tools.md)。

## 环境

`opencollab.environments` 导出环境协议和 Docker 配置。自定义 `EnvironmentPool` 需提供 `acquire`、`release_env`、`release` 和 `adopt`，它的工厂按“一次运行 + 一种策略”创建资源。

## 内置工作流

`opencollab.builtin_workflows` 导出 Duo、Weave、Reconstruct，以及 Weave 适配器、状态和 Reconstruct 绑定类型。

- `get_builtin_workflows()` 返回一个包含 `duo`、`weave`、`reconstruct` 的新注册表，用 `list_specs()` 读取名称、说明和阶段。
- Duo：`run_dual_coder` 和 `CONTRACT_PROMPT` 用来定制角色和仲裁，示例见[用 Duo 跑两个候选方案并选出一个](duo-workflow.md)。
- Weave：`WeaveGroup`、`WeaveCheck`、`WeaveConfig`、`WeaveState`、`WeaveAdapter`、`plan_weave_groups` 和 `run_weave`，用于自定义分组、验证和按状态续跑。
- Reconstruct：`ReconstructionTask` 和 `register_task` 连接隐藏程序与候选执行器。

## 结果与控制类型

`RunResult` 字段：`output`、`status`、`reason`、`tokens`、`artifacts`、`error`、`metrics`、`agent_failures`。`ok` 对应 completed；`raise_for_status()` 在其它状态下抛出带结果的 `RunError`。

`RunControl`、`BudgetSnapshot`、`BudgetDecision` 和 `RunEvent` 从 `opencollab` 导入，用于单次会话的预算策略和观察。

## 模块导出一览

| 模块 | 公开导出 |
| --- | --- |
| `opencollab`、`opencollab.sdk` | 同一组八个：`OpenCollab`、`workflow`、`RunResult`、`RunError`、`RunControl`、`BudgetSnapshot`、`BudgetDecision`、`RunEvent` |
| `opencollab.workflows` | `workflow`、`WorkflowContext`、`WorkflowAgentResult`、`CandidateRun` |

普通函数可以直接交给 SDK；要按名称发现，才需要 `@workflow` 注册。

## 评测集成

评测集成使用上面的 SDK、环境和验证工具入口，使用文档见[配置评测运行时](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md)。数据集加载、隐藏测试和正式评分由外部评测包负责，详见[用 OCE 评测 OC 的 Agent、Team 和 Workflow](../observability/evaluation-boundary.md)。

`opencollab.environments` 也公开导出 `VMConnection`、`VMEnvironment`、`VMResource` 和 `vm_isolation_provider`，完整用法见 [VM 指南](../isolation/vm-environment-extension.md)。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L96)、[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/workflows.py#L14)、[opencollab/application/workflow_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_collections.py#L253)。

[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py)、[opencollab/application/workflow_agent_run.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_agent_run.py)、[opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/tools.py)。

## 相关章节

[编写第一个自定义 Workflow](author-custom-workflow.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[在 Workflow 中并行处理多项任务](workflow-parallel-pipeline.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[测试和调试你写的 Workflow](workflow-testing-debugging.md)。
