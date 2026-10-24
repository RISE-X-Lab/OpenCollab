---
{
  "id": "author-custom-workflow",
  "title": "编写第一个自定义 Workflow",
  "category": "workflow",
  "summary": "在工作区 workflows/ 下写一个带 @workflow 的异步函数，就能被 OC 发现并按名称运行。",
  "aliases": [
    "写工作流",
    "工作流模板",
    "async def",
    "ctx",
    "inputs",
    "args",
    "workflows 目录",
    "phase",
    "log",
    "自定义工作流",
    "@workflow 装饰器"
  ],
  "keywords": [
    "@workflow",
    "WorkflowContext",
    "自定义 Workflow",
    "Python 编排"
  ],
  "questions": [
    "怎样写一个自己的工作流？",
    "写好的工作流文件要放在哪里？",
    "@workflow 的 name、description、phases 有什么用？",
    "工作流里怎样给 Agent 配工具？",
    "怎样在工作流里记录阶段和日志？"
  ],
  "related": [
    "run-registered-workflow",
    "workflow-agent-calls",
    "workflow-parallel-pipeline",
    "compose-workflow-candidates",
    "workflow-programming-model",
    "workflow-dataflow-structured-io",
    "workflow-testing-debugging"
  ],
  "verified_ref": "0c47f74a"
}
---

# 编写第一个自定义 Workflow

在 OC 里，自定义 Workflow 就是一个带 `@workflow` 装饰器的 `async def (ctx, args)` 函数。把它放进工作区的 `workflows/` 目录，OC 就能按名称发现并运行。本篇用一个“先实现、再审阅”的两步流程演示：第一次 `ctx.agent` 修改代码，第二次 `ctx.agent` 拿着第一步的说明和 diff 审阅修复。两次调用是两个独立会话，信息只通过 Python 变量和提示词传递。

## 第 1 步：写入工作区模块

把下面的完整文件保存为工作区里的 `workflows/implement_review.py`。

```python
from opencollab import workflow
from opencollab.tools import builtin_tools
from opencollab.workflows import WorkflowContext

@workflow(
    name='implement-review',                    # 按名称运行时用的名字
    description='实现任务后审阅改动并运行相关检查',  # workflow list 里显示的说明
    phases=['implement', 'review'],             # 描述性元数据，不控制执行
)
async def implement_review(ctx: WorkflowContext, args: dict):
    goal = args['goal']  # 本文件自定义的必填输入，缺失时字典访问直接报错
    names = ['file_read', 'file_write', 'apply_patch', 'git_diff', 'grep']
    # 只有显式允许时才加入可在宿主执行的 bash
    if args.get('allow_unisolated_shell') is True:
        names.append('bash')
    tools = builtin_tools(
        *names,
        headless=args.get('allow_unisolated_shell') is not True,
    )
    await ctx.phase('implement')
    draft = await ctx.agent(goal, label='implementer', tools=tools)
    if draft is None:
        return {'implemented': False, 'review': None}
    changes = await ctx.diff()
    await ctx.phase('review')
    # 第二次调用是新会话：第一步的说明和改动要显式写进提示词
    review = await ctx.agent(
        f'审阅并修复任务中的遗漏，运行环境可用的相关检查。\n'
        f'任务\n{goal}\n实现说明\n{draft}\n改动\n{changes or ""}',
        label='reviewer',
        tools=tools,
    )
    await ctx.log(f'Used tokens {ctx.tokens_spent()}')
    return {'implemented': True, 'review': review}
```

要点：

- 默认只给文件类工具。传入 `allow_unisolated_shell=true` 时，才加上允许在宿主执行的 `bash`。
- `implemented` 只表示第一步有返回值。交付是否满足任务，要看第二步的检查结果和实际文件。
- 第二次调用不继承第一次的对话历史，但文件环境沿用当前策略，所以能看到第一步改过的文件。

## 第 2 步：从命令行发现并运行

```bash
# 确认 implement-review 出现在列表里
opencollab workflow list --workspace /path/to/workspace
# --task 会填入 args['goal']
opencollab workflow run implement-review \
  --workspace /path/to/workspace \
  --task '修复配置读取错误并运行相关测试' \
  --args '{"allow_unisolated_shell":true}' \
  --budget 50000
```

发现规则：

- OC 加载所选目录顶层的 `*.py` 模块。
- 模块顶层语句会在导入时执行。顶层只放导入、定义和装饰器，具体任务操作放进异步函数。
- 目录可以用 `OPENCOLLAB_WORKFLOWS_DIR` 改。
- 名称在内置和工作区合并后的注册表里必须唯一。

也可以不走发现，直接在 Python 里导入 `implement_review`，传给 `await OpenCollab(workspace).workflow(implement_review, inputs)`。没有装饰器的普通异步函数也能这样启动；只有按名称发现才依赖装饰器元数据。

## `@workflow` 参数与默认值

| 参数 | 作用 | 省略时 |
| --- | --- | --- |
| `name` | 发现和调用用的名称 | 使用函数名 |
| `description` | 列表里显示的说明 | 先取 docstring 首行，再取函数名 |
| `phases` | 描述流程有哪些阶段 | 只是元数据，阶段事件由 `await ctx.phase(...)` 发出，顺序由函数体决定 |

`@workflow` 只是给函数附上 `WorkflowSpec` 元数据，被装饰的函数仍可直接调用。

## 入口函数的约定

- 签名通常是 `async def name(ctx, args)`，`args` 是字典。
- 返回值可以是文本、字典、列表，或任何适合调用方的值。如果结果要经过 CLI、清单或子进程保存，请返回 JSON 可序列化的数据。
- 校验输入、导入业务库、调用普通辅助函数，都可以直接写在入口函数里。

## 工具配置

`builtin_tools(...)` 返回一组工具对象，支持以下名称：

`bash`、`file_read`、`file_write`、`apply_patch`、`git_diff`、`grep`、`submit`

- 自定义工具实现公开的 `Tool` 协议后，可以放进同一个集合。
- `headless=True` 要求 shell 有进程隔离。权限设置应与实际运行环境对应。

## 观察与资源查询

| 方法 | 作用 |
| --- | --- |
| `ctx.phase(name)` | 标记当前阶段 |
| `ctx.log(text)` | 写入流程日志 |
| `label=` 参数 | 在会话记录里区分角色 |
| `ctx.diff()` | 返回可观察到的改动，探测不可用时可能返回 `None` |
| `ctx.source_changed()` | 返回改动探测结果，探测不可用时可能返回 `None` |
| `ctx.tokens_remaining()`、`ctx.seconds_left()`、`ctx.time_low()` | 用来决定还要不要再开一轮 |

建议先跑通并验证一项具体任务，再按需加分支和并行步骤。

## 常见问题

- 如果 `workflow list` 里没有你的工作流：检查文件是否在 `workflows/` 顶层，函数上是否有 `@workflow`。
- 如果报注册错误：可能和已有工作流重名，改 `name`。
- 如果运行时报 `KeyError: 'goal'`：没有传 `--task`、`--task-file`，`--args` 里也没有 `goal`。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L290)、[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/workflows.py#L14)、[opencollab/application/workflow_registry.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_registry.py#L18)。

## 相关章节

[运行一个工作流（Workflow）](run-registered-workflow.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[在 Workflow 中并行处理多项任务](workflow-parallel-pipeline.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[工作流就是普通 Python：编程模型入门](workflow-programming-model.md)、[Workflow 里的数据怎么传：输入、步骤结果、文件与输出](workflow-dataflow-structured-io.md)、[测试和调试你写的 Workflow](workflow-testing-debugging.md)。
