---
{
  "id": "workflow-agent-calls",
  "title": "在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run",
  "category": "workflow",
  "summary": "每次调用开一个新会话；只要结果用 ctx.agent，要停止原因、用量和清理状态用 ctx.agent_run。",
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
    "调用 Agent"
  ],
  "keywords": [
    "ctx.agent",
    "ctx.agent_run",
    "WorkflowAgentResult",
    "Agent 调用"
  ],
  "questions": [
    "ctx.agent 和 ctx.agent_run 有什么区别？",
    "两次 Agent 调用之间会共享对话历史吗？",
    "怎样让 Agent 返回结构化字典？",
    "ctx.agent 什么时候会返回 None？",
    "怎样限制单个 Agent 的步数、时限和额度？",
    "cleanup incomplete 是什么意思？"
  ],
  "related": [
    "author-custom-workflow",
    "workflow-parallel-pipeline",
    "compose-workflow-candidates",
    "weave-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run

Workflow 里调用 Agent 有两个方法，都会开一个全新会话（fresh session，不继承之前任何调用的对话历史）：

| 方法 | 返回 | 什么时候用 |
| --- | --- | --- |
| `ctx.agent` | 文本、校验后的字典，或 `None` | 只需要任务输出 |
| `ctx.agent_run` | `WorkflowAgentResult` 执行快照 | 还需要停止原因、用量、步数、清理状态，或要用 `RunControl` |

角色之间交换信息只有三条路：显式写进提示词、Python 变量、工作区文件。文件是否共享由隔离策略决定。

## `ctx.agent`：用回答决定下一步

以下是完整的异步工作流函数，可以传给 SDK 的 `workflow`。它用 JSON Schema 拿到一个判断，再按结果决定要不要追问。

```python
async def inspect_and_explain(ctx, args):
    schema = {
        'type': 'object',
        'properties': {
            'needs_detail': {'type': 'boolean'},
            'summary': {'type': 'string'},
        },
        'required': ['needs_detail', 'summary'],
        'additionalProperties': False,
    }
    finding = await ctx.agent(
        args['goal'], schema=schema, label='inspect', tools=[], budget=8000,
    )
    if finding is None:
        return {'finding': None, 'detail': None}
    detail = None
    # 由 Python 读字段决定分支，OC 不会自动路由
    if finding['needs_detail']:
        detail = await ctx.agent(
            f'解释这一结论的依据\n{finding["summary"]}',
            label='explain', tools=[], budget=8000,
        )
    return {'finding': finding, 'detail': detail}
```

### 常用参数

| 参数 | 作用 |
| --- | --- |
| `prompt` | 本次任务 |
| `label` | 在记录里标识这个角色 |
| `tools` | 会话能调用的工具；传空集合表示只处理给定文字 |
| `budget` | 从共享余额里申请本次调用的额度；没用完的预留在清理后释放 |
| `schema` | 要求返回符合 JSON Schema 的字典 |
| `isolation` | 本次会话的环境策略，见下文 |
| `timeout` | 本次会话的时限 |
| `tool_choice`、`thinking` | 只用于自由文本路径；结构化路径按捕获阶段的请求策略执行 |

### 什么时候返回 `None`

- 带 `schema` 时：OC 通过结构化捕获工具取得并校验字典。捕获未满足要求时，运行时会带着已有历史作一次纠正尝试。
- 自由文本时：常见的会话构建失败、运行失败或到达调用时限，都可能返回 `None`。

schema 只校验结构。字段内容和业务判断，要结合任务依据和调用方规则验证。

另外，额度耗尽时，可能在会话创建前就抛出预算异常。调用方应结合顶层结果处理。

## `ctx.agent_run`：取得执行快照

```python
from opencollab import RunControl
from opencollab.tools import builtin_tools

async def inspect_files(ctx, args):
    run = await ctx.agent_run(
        args['goal'],
        label='inspector',
        tools=builtin_tools('file_read', 'grep'),
        budget=12000,          # 申请的硬额度
        timeout=120,           # 本次会话时限（秒）
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

`WorkflowAgentResult` 除了上面用到的字段，还有：

- `hard_budget_tokens`：实际拿到的硬额度。
- `soft_budget_tokens`：软额度。
- `observation_errors`：观察回调里出的错。

额度说明：申请值会受共享余额和其它预留影响，回调里看到的是实际授权。`RunControl` 可以在硬额度以内扩展软额度，也可以同步接收事件。详见[在哪一层调整 Workflow：从 CLI 参数到单次调用回调](workflow-control-granularity.md)。

`system_prompt` 与 profile：

- 显式传 `system_prompt` 会替换本次角色的默认系统提示。
- 所选 Agent profile 仍然提供工具配置、上下文处理和安全行为。
- profile 用 SDK 的 `agent_profile` 选择；省略时保留工作流角色自己的配置。

`tools` 参数：`agent_run` 接受工具配置名称或工具对象序列。要精确指定每个工具，就传工具对象序列。

## 时限与清理

| 限制 | 作用范围 |
| --- | --- |
| `timeout`（调用参数） | 本次会话运行 |
| `max_steps` | 本次会话步数 |
| `timeout`（工作流外层） | 整次运行 |
| `cleanup_timeout` | 已取消的工具和模型任务怎样收尾 |

如果 `agent_run` 清理没完成：

- 返回状态为 `failed`，原因为 `cleanup incomplete`。
- 共享上下文会停止启动新工作。

只有 `cleanup_complete` 为真时，快照里的用量才是稳定值。后续步骤若依赖同一环境，还要检查 `workspace_ready`。

## 隔离策略：`isolation`

| 取值 | 效果 |
| --- | --- |
| 省略 | 继承本次运行的策略 |
| `False` | `shared` |
| `True` | `worktree` |
| 字符串 | `shared`、`worktree`、Docker、已配置的 `vm-group`、`vm-session` 或自定义策略 |

- 会话历史每次调用都重新建立。文件是否共享由隔离策略决定。
- Docker 与 VM 策略将工具放入对应环境，普通 SDK Workflow 的 Python 编排继续在调用方进程执行。

成员自编 DW 的 Python 子进程位于成员当前环境。省略单次 `isolation` 时，继承 `launch` 选择的默认策略。父成员在 VM 内、子 DW 使用 `worktree` 时，会话工具继续位于同一 guest 的独立工作树。单次选择 `vm-session` 会申请另一台可租用 VM，见[成员自编 DW 教程](member-authored-workflow.md)与[VM 环境扩展](../isolation/vm-environment-extension.md)。

## 失败如何传到顶层

- 内部失败保留在顶层 `RunResult.agent_failures` 里。
- 外部取消会继续向调用方传播。
- 交付结果要通过应用的真实入口、测试或验证器检查。

## 源码依据

[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/workflows.py#L21)、[opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow.py#L503)、[opencollab/application/workflow_agent_run.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_agent_run.py#L17)。

## 相关章节

[编写第一个自定义 Workflow](author-custom-workflow.md)、[在 Workflow 中并行处理多项任务](workflow-parallel-pipeline.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[用 Weave 分组接力完成任务并检查交付](weave-workflow.md)。
