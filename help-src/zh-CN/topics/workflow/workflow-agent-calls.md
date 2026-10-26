---
{
  "id": "workflow-agent-calls",
  "title": "在 Workflow 里调用 Agent：会话、数据传递与并行",
  "category": "workflow",
  "summary": "在 Workflow 里调用 Agent：每次调用开一个新会话，数据要显式传递；用 schema 拿结构化结果，用 ctx.agent_run 拿执行快照，用 ctx.parallel 和 ctx.pipeline 批量并行。",
  "aliases": [
    "工作流角色",
    "结构化输出",
    "schema",
    "fresh session",
    "RunControl",
    "max_steps",
    "budget",
    "timeout",
    "cleanup_complete",
    "ctx.agent 和 agent_run 区别",
    "调用 Agent",
    "args 和 inputs 有什么区别",
    "上一步结果怎么交给下一步",
    "Agent 怎么返回 JSON",
    "文件和返回值怎么传",
    "怎么检查工作流最终结果",
    "步骤间传值",
    "Agent 返回字典",
    "批量任务",
    "扇出",
    "fan out",
    "parallel",
    "pipeline",
    "顺序阶段",
    "task_concurrency",
    "--task-concurrency",
    "嵌套并行",
    "并发上限",
    "parallel 和 pipeline 区别"
  ],
  "keywords": [
    "ctx.agent",
    "ctx.agent_run",
    "WorkflowAgentResult",
    "Agent 调用",
    "Workflow 输入输出",
    "数据流",
    "schema",
    "RunResult",
    "ctx.parallel",
    "ctx.pipeline",
    "并行任务",
    "concurrency"
  ],
  "questions": [
    "ctx.agent 和 ctx.agent_run 有什么区别？",
    "两次 Agent 调用之间会共享对话历史吗？",
    "怎样让 Agent 返回结构化字典？",
    "ctx.agent 什么时候会返回 None？",
    "怎样限制单个 Agent 的步数、时限和额度？",
    "cleanup incomplete 是什么意思？",
    "args 和 inputs 有什么区别？",
    "上一步的结果怎么交给下一步？",
    "怎样让 Agent 返回 JSON 字典？",
    "schema 校验失败会怎样？",
    "Agent 写的文件和函数返回值分别怎么拿到？",
    "哪些数据能跨进程传递？",
    "怎样在工作流里同时跑多个 Agent？",
    "parallel 和 pipeline 有什么区别？",
    "concurrency 和 task_concurrency 有什么不同？",
    "某个分支报错会让整批任务失败吗？",
    "并行任务的结果顺序和输入一致吗？",
    "可以嵌套使用 parallel 吗？"
  ],
  "related": [
    "workflow-programming-model",
    "workflow-composition-hierarchy",
    "workflow-public-api-map",
    "token-step-concurrency-limits"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在 Workflow 里调用 Agent：会话、数据传递与并行

Workflow 的核心动作只有一个：调用 Agent。这篇讲清楚三件事：每次调用到底开了什么、数据怎样在步骤之间流动，以及怎样一次处理一批任务。

## 先记住：每次调用都是一个新会话

`ctx.agent` 和 `ctx.agent_run` 每调用一次，就开一个**全新的会话**，不继承之前任何调用的对话历史。这是 OC 的默认设计：上下文由你显式控制，而不是让历史越积越长。

所以一个 Workflow 里的数据有四条路，各走各的：

| 数据 | 怎么传 |
| --- | --- |
| 运行输入 | SDK 的 `inputs` 或 CLI 的 `--args`，变成函数收到的 `args` 字典 |
| 步骤之间的结果 | 普通 Python 变量；下一步需要时，显式拼进提示词 |
| 真实产物 | 工作区里的文件，由 Agent 用工具写入；文件是否共享由隔离策略决定 |
| 最终输出 | 函数 `return` 的值，成为 `RunResult.output` |

`inputs` 和 `args` 是同一份数据，一个在调用方，一个在函数里：运行时把 `inputs` 转成普通字典，再调用 `flow(ctx, args)`。CLI 的 `--args` 提供同一个字典，`--task` 和 `--task-file` 只填 `goal` 字段。字段名和含义由 Workflow 作者定义，可以用普通 Python 或已有的数据模型校验。

## 用 `ctx.agent` 拿到结果

下面的函数可以直接传给 SDK。它先检查输入，用 schema 拿到一个字典，再把这个字典作为下一次调用的提示词：

```python
import json

async def classify_and_describe(ctx, args):
    # Workflow 整体输入由入口自己校验
    text = args.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be non-empty text')
    # schema 只约束这一次 Agent 输出的结构
    schema = {
        'type': 'object',
        'properties': {
            'category': {'type': 'string', 'enum': ['question', 'statement']},
            'summary': {'type': 'string'},
        },
        'required': ['category', 'summary'],
        'additionalProperties': False,
    }
    item = await ctx.agent(
        f'归类并概括这段文字\n{text}',
        schema=schema, tools=[], label='classify',
    )
    # 纠正一次后仍不满足 schema 时返回 None
    if item is None:
        return {'item': None, 'description': None}
    # 新会话看不到上一次对话，显式把结果传进去
    description = await ctx.agent(
        '解释以下分类结果\n' + json.dumps(item, ensure_ascii=False),
        tools=[], label='describe',
    )
    return {'item': item, 'description': description}
```

### 常用参数

| 参数 | 作用 |
| --- | --- |
| `prompt` | 本次任务 |
| `label` | 在记录里标识这个角色 |
| `tools` | 这个会话能调用的工具；传空列表表示只处理给定的文字 |
| `budget` | 从共享余额里申请本次调用的额度；没用完的预留在清理后释放 |
| `schema` | 要求返回符合 JSON Schema 的字典，见下一节 |
| `isolation` | 本次会话的执行环境，见 [给单次调用选执行环境](#给单次调用选执行环境) |
| `timeout` | 本次会话的期限 |
| `tool_choice`、`thinking` | 只用于自由文本路径；带 schema 时按捕获阶段的请求策略执行 |

### 什么时候返回 `None`

- 带 `schema` 时：结果纠正一次后仍不满足 schema。
- 自由文本时：会话构建失败、运行失败或到达本次期限，都可能返回 `None`。

所以拿到结果后先判空。另外，额度耗尽时可能在会话创建前就抛出预算异常，调用方要结合顶层结果处理。

## 拿结构化结果：schema

OC 的做法是：

1. 要求这个会话调用一个结构化输出工具来交付结果。
2. 工具校验提交的对象，通过就返回字典。
3. 第一次没拿到符合 schema 的对象时，运行时带着已有历史让 Agent 再纠正一次。
4. 仍不满足就返回 `None`。

JSON Schema 能约束字段类型、必填字段、枚举和是否允许多余字段。它**只校验结构**，字段内容对不对，还要靠提示词、工具结果和业务验证一起确认。

设计建议：

- 要根据结果决定下一步时，用明确的字段（布尔值、枚举、对象列表），再用 Python 分支读取。OC 不会根据字段自动路由。
- 自由文本适合报告、摘要和下一步提示。要把文本当作命令、路径或状态使用，先按用途解析它。
- 下一个会话需要的文字，要么显式传进提示词，要么让它用工具读取指定文件。

## 需要停止原因和用量时：`ctx.agent_run`

`ctx.agent` 只给你输出。如果你还想知道这次会话为什么停、用了多少、清理完没有，或者要在运行中用 `RunControl` 调整额度，就用 `ctx.agent_run`，它返回一个执行快照 `WorkflowAgentResult`：

```python
from opencollab import RunControl
from opencollab.tools import builtin_tools

async def inspect_files(ctx, args):
    run = await ctx.agent_run(
        args['goal'],
        label='inspector',
        tools=builtin_tools('file_read', 'grep'),
        budget=12000,          # 申请的硬额度
        timeout=120,           # 本次会话期限（秒）
        max_steps=12,          # 本次会话步数上限
        system_prompt='阅读任务指定文件，说明观察到的事实。',
        run_control=RunControl(initial_soft_budget_tokens=8000),
        cleanup_timeout=2.0,
    )
    return {
        'answer': run.output,
        'status': run.status,
        'reason': run.reason,
        'tokens': run.tokens,
        'steps': run.steps,
        'session_id': run.session_id,
        'workspace_ready': run.workspace_ready,
        'cleanup_complete': run.cleanup_complete,
    }
```

除了上面用到的字段，快照里还有：实际拿到的硬额度 `hard_budget_tokens`、软额度 `soft_budget_tokens`、观察回调出的错 `observation_errors`。

几点说明：

- **额度**：申请值会受共享余额和其他预留影响，实际授权可能比申请的小。`RunControl` 可以在硬额度以内扩展软额度，见 [按进展扩展软额度](../operations/token-step-concurrency-limits.md#按进展扩展软额度)。
- **`system_prompt`**：替换本次角色的默认系统提示；所选 Agent 配置仍然提供工具配置、上下文处理和安全行为。配置用 SDK 的 `agent_profile` 选。
- **`tools`**：接受工具组名称或工具对象序列。要精确指定每个工具，传对象序列。
- **清理**：只有 `cleanup_complete` 为真时，快照里的用量才是稳定值；后续步骤依赖同一环境时，还要检查 `workspace_ready`。清理没在 `cleanup_timeout` 内完成，会返回 `failed`、原因 `cleanup incomplete`，共享上下文也会停止启动新工作。

几个时限各管一段：调用参数 `timeout` 管这次会话，`max_steps` 管这次会话的步数，Workflow 外层的 `timeout` 管整次运行，`cleanup_timeout` 管已取消的工具和模型任务怎样收尾。

## 给单次调用选执行环境

| `isolation` 取值 | 效果 |
| --- | --- |
| 省略 | 继承本次运行的策略 |
| `False` | `shared`，直接用父环境 |
| `True` | `worktree`，独立工作树 |
| 字符串 | `shared`、`worktree`、Docker、已配置的 `vm-group`、`vm-session`，或你注册的策略名 |

会话历史每次都重新建立，文件是否共享由策略决定。Docker 和 VM 策略只把 Agent 的工具放进容器或 VM，Workflow 的 Python 代码仍在调用方进程里。成员用 `manage_workflow` 启动的子 Workflow 规则稍有不同，见 [让 Team 成员运行 Workflow](team-run-workflow.md#子-agent-在哪里执行)。各策略的含义见 [隔离](../isolation/isolation-overview.md)。

## 批量并行：parallel 与 pipeline

| 方法 | 适合 | 签名 |
| --- | --- | --- |
| `ctx.parallel` | 一批互相独立的任务 | `parallel(thunks)`，传一组无参的异步函数 |
| `ctx.pipeline` | 每项输入依次经过几个阶段 | `pipeline(items, *stages, stop_on_none=True)` |

两者都按输入顺序返回结果。和直接用 `asyncio.gather` 相比，它们会按额度给每一项分配预算、受整个 Workflow 的并发上限约束，并把普通异常转成该位置的 `None`。任务何时创建、每个阶段调用什么 Agent、怎样汇总，仍由你的代码决定。

### `parallel`：同时执行独立任务

```python
async def summarize_many(ctx, args):
    # 用工厂函数把 text 和 index 绑进闭包
    def make_task(text, index):
        async def run_one():
            return await ctx.agent(
                f'用一句话概括\n{text}',
                label=f'summary-{index}', tools=[],
            )
        return run_one

    tasks = [make_task(text, i) for i, text in enumerate(args['texts'])]
    answers = await ctx.parallel(tasks)
    return {'answers': answers}
```

- 用工厂函数绑定变量，是为了避免循环里的闭包在执行时都读到最后一个值。
- 返回列表的位置对应原输入，先完成的任务也待在自己的位置上；空列表返回空列表。
- 多个任务同时写同一个文件时，冲突要由你避免：按文件分工，或者让候选使用独立环境。

### `pipeline`：每项依次走几个阶段

```python
async def prepare_then_explain(ctx, args):
    # 每个阶段收到 (上一阶段结果, 原始输入, 输入索引)
    async def prepare(previous, original, index):
        return {'text': original.strip(), 'index': index}

    async def explain(previous, original, index):
        if not previous['text']:
            return None  # 默认 stop_on_none=True：这一项到此结束
        return await ctx.agent(
            f'解释这一段文字\n{previous["text"]}',
            label=f'explain-{index}', tools=[],
        )

    return await ctx.pipeline(args['texts'], prepare, explain, stop_on_none=True)
```

- 第一个阶段的 `previous` 就是原始输入。
- 同一项的各阶段按顺序执行；不同项可以同时处在不同阶段。
- `stop_on_none=True`（默认）时，某阶段返回 `None`，这一项就结束，结果位置为 `None`；设为 `False`，下一阶段会收到这个 `None`，由你自己处理。
- 编辑器可能提示 `pipeline` 不存在：公开的 `WorkflowContext` 类型注解漏列了这个方法，但它确实存在并有测试覆盖，照常调用即可。

### 两个并发上限

| 参数 | CLI | 限制什么 | 默认 |
| --- | --- | --- | --- |
| `concurrency` | `--concurrency` | 同时活跃的 Agent 会话 | SDK 为 4 |
| `task_concurrency` | `--task-concurrency` | 同时活跃的集合单元（`parallel`/`pipeline` 的项） | 等于 `concurrency` |

为什么分成两个？一个集合单元可能只跑普通 Python，也可能调一次 Agent，还可能跑多个顺序步骤，所以两者分开计数，混合运行时活跃工作数可能达到两者之和。例如 `concurrency=4, task_concurrency=2`：最多四个活跃会话，同时最多两个集合单元。

把大量输入交给集合方法，运行时会按 `task_concurrency` 维持有限数量的 worker。你自己用 `asyncio.gather` 发起大量调用时，创建多少宿主任务由你负责，不过 `ctx` 的会话上限仍然生效。

还有第三层限制在模型服务那边：宿主可以用 `request_capacity` 限制同时发出的模型请求数，多个 Team 和 Workflow 可以共用这份容量，见 [模型路由与共享请求容量](../models/model-routing-request-capacity.md)。扩大输入规模之前，先拿小任务量一下实际耗时和用量，再按服务能力选并发数。

### 预算怎样分给每一项

- 额度有限时，OC 按集合大小给每一项规划额度；项内的 Agent 调用从这一项和 Workflow 的剩余额度里扣。
- 嵌套的集合继续受父级额度和共享容量约束。集合可以嵌套，候选子流程也共用集合容量。运行时允许子任务借用父任务的容量，所以 `task_concurrency=1` 时嵌套集合也不会卡死。
- 每一项可以显式申请更小的 `budget`，实际授权还取决于共享余额和其他预留。

### 某一项失败了会怎样

| 情况 | 结果 |
| --- | --- |
| `parallel` 的某个分支抛普通异常 | 该位置为 `None`，其他分支继续 |
| `pipeline` 的某个阶段抛普通异常 | 这一项返回 `None`（`stop_on_none=False` 只影响正常返回 `None` 的情况） |
| 共享预算耗尽、环境被撤销 | 向集合外传播 |
| 外部取消 | 继续传播，并等已启动的任务完成取消和清理 |

想保留普通异常的详情，就在分支里自己 `try`，返回一个包含错误类型和输入索引的字典。

## 文件、候选和最终输出

**文件**：Agent 通过 `file_write`、`apply_patch`、`bash` 等工具操作工作区。想确认模型说写了什么、写没写，就用工具或应用检查去读实际文件。在 Python 函数里直接 `Path.write_text`，操作发生在编排进程所在的机器上；Docker 会话里的工具用的是容器内路径。想在 Agent 绑定的环境里操作文件，用对应的工具或环境接口，这样还能留下执行记录。

**候选**：用 `ctx.candidate_agent` 或 `ctx.candidate_workflow` 在隔离环境里跑出的结果是一个 `CandidateRun`，带输出和捕获到的文件变化。调用 `adopt_candidate` 之后，选中的变化才进入源工作区。见 [组合与嵌套](workflow-composition-hierarchy.md#候选在隔离环境里试改动)。

**最终输出**：入口函数 `return` 的对象成为 `RunResult.output`，`status`、`reason`、`tokens` 和 `agent_failures` 记录执行情况。Workflow 可以在局部失败后返回部分数据，所以调用方要同时看业务字段、失败记录和实际交付检查。失败怎样传到顶层，见 [失败处理、测试与调试](workflow-failure-recovery-lifecycle.md)。

## 哪些数据能跨进程

- 开启产物保存时，输入必须能序列化成 JSON。
- 经过 `manage_workflow` 子进程的输入和输出也必须能序列化，适合传路径、业务 id、数字和普通集合。
- 活的工具对象、数据库连接或函数，留在所属进程里，由入口函数通过依赖连接取得。

Reconstruct 的 `register_task` 是一个例子：活的查询函数、执行器和工具保存在当前进程，输入里只传一个一次性的句柄；CLI 则用可导入的工厂名称加 `task_args` 重建这些对象。要在另一个进程里恢复业务，就保存两样东西：能重新建立依赖的输入，以及已完成步骤的结果。

## 延伸阅读

- 源码依据：[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/workflows.py) · [opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow.py) · [opencollab/application/workflow_agent_run.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_agent_run.py) · [opencollab/application/workflow_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_collections.py) · [tests/interfaces/test_sdk_workflow_nested_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/tests/interfaces/test_sdk_workflow_nested_collections.py) · [opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/result.py)
- 相关指南：[编写 Workflow](workflow-programming-model.md) · [组合与嵌套](workflow-composition-hierarchy.md) · [Workflow API 速查](workflow-public-api-map.md) · [预算、步数与并发](../operations/token-step-concurrency-limits.md)
