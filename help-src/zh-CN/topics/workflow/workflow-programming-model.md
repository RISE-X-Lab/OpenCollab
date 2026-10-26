---
{
  "id": "workflow-programming-model",
  "title": "编写 Workflow：普通 Python 加上 ctx",
  "category": "workflow",
  "summary": "Workflow 就是一个普通的异步 Python 函数：流程用 Python 写，调用 Agent 用 ctx。本文写出第一个可按名称运行的 Workflow，并说明哪些需求 OC 能做、哪些要自己搭。",
  "aliases": [
    "工作流到底能写什么",
    "if 和 for 能直接用吗",
    "能写 while 循环吗",
    "可以动态决定下一步吗",
    "一定要固定流程图吗",
    "工作流是不是特殊语法",
    "工作流编程模型",
    "ctx 是什么",
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
    "@workflow 装饰器",
    "任意 Python 都能用吗",
    "能不能写 DAG",
    "支持分布式多机吗",
    "有没有自动断点恢复",
    "Docker 隔离到哪里",
    "能在运行时生成代码吗",
    "工作流能力范围",
    "工作流限制"
  ],
  "keywords": [
    "Workflow 编程模型",
    "Python",
    "async def",
    "WorkflowContext",
    "@workflow",
    "自定义 Workflow",
    "Python 编排",
    "Workflow 能力范围",
    "执行边界",
    "环境扩展",
    "动态 DW",
    "多个 Agent",
    "request_capacity",
    "VM"
  ],
  "questions": [
    "工作流里能直接用 if、for、while 吗？",
    "工作流可以根据上一步结果动态决定下一步吗？",
    "ctx 和普通 Python 代码各负责什么？",
    "工作流的代码到底在哪个进程里运行？",
    "怎样写一个自己的工作流？",
    "写好的工作流文件要放在哪里？",
    "@workflow 的 name、description、phases 有什么用？",
    "工作流里怎样给 Agent 配工具？",
    "怎样在工作流里记录阶段和日志？",
    "工作流里能用任意 Python 库吗？",
    "能写 DAG 依赖图吗？",
    "支持多台机器分布式运行吗？",
    "运行中断后能自动接着跑吗？",
    "Docker 隔离到底隔离了哪些东西？",
    "Team 成员能在运行时自己写工作流再启动吗？",
    "成员能在现场编写 DW 并调度多个 Agent 吗？",
    "Python 编排、工具和模型请求分别在哪里执行？",
    "工作流局部并发与模型共享容量有什么关系？"
  ],
  "related": [
    "workflow-agent-calls",
    "workflow-composition-hierarchy",
    "workflow-failure-recovery-lifecycle",
    "workflow-public-api-map",
    "run-registered-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 编写 Workflow：普通 Python 加上 ctx

OC 的 Workflow 没有图声明，也没有专门的 DSL，它就是一个 `async def fn(ctx, args)` 函数。分支、循环、并发、何时结束，都写成普通的 Python 控制流；需要模型干活时调用 `ctx.agent`，每调用一次就开一个新会话。所谓“流程图”，是代码在运行时一步步展开出来的。

这篇先讲清 Python 和 `ctx` 各管什么，然后写出第一个能按名称运行的 Workflow，再介绍常用的流程写法、OC 能做和不能做的事，以及代码到底在哪里运行。

## 一句话分工

| 谁负责 | 做什么 |
| --- | --- |
| Python 本身 | 变量、函数、条件判断、循环、异常处理、导入模块，也就是“先做什么、再做什么” |
| `ctx`（OC 传进来的 `WorkflowContext`） | 开 Agent 会话、并发调度、查剩余额度和时间、管理候选环境 |
| 你的应用 | 运行之外的业务状态：自己的数据库、外部系统、跨主机协调 |

想清楚每件事归谁，就能选对接口。

## 第一个 Workflow：先实现，再审阅

下面这个流程分两步：第一次 `ctx.agent` 修改代码，第二次 `ctx.agent` 拿着第一步的说明和改动去审阅、补漏。把整个文件存为工作区里的 `workflows/implement_review.py`：

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
    # 只有显式允许时才加入可在本机执行的 bash
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

读懂这段代码的三个要点：

- **两次调用是两个独立的会话。** 第二次看不到第一次的对话，所以代码把 `draft` 和 `changes` 显式拼进了提示词。但两次调用用同一个执行环境，所以第二次能看到第一次改过的文件。
- **默认只给文件工具。** 传入 `allow_unisolated_shell=true` 时才加上能在本机执行的 `bash`。
- **`implemented` 只表示第一步有返回值。** 交付是否满足任务，要看第二步的检查结果和实际文件。

### 从命令行发现并运行

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

- OC 加载所选目录**顶层**的 `*.py` 模块，目录可以用 `OPENCOLLAB_WORKFLOWS_DIR` 改。
- 模块顶层语句会在导入时执行，所以顶层只放导入、定义和装饰器，具体操作放进 `async` 函数。
- 名称在内置和工作区合并后的注册表里必须唯一。

也可以不走发现：在 Python 里导入 `implement_review`，直接传给 `await OpenCollab(workspace).workflow(implement_review, inputs)`。没有装饰器的普通 `async` 函数也能这样运行；只有按名称发现才需要装饰器。

### `@workflow` 只是声明

| 参数 | 作用 | 省略时 |
| --- | --- | --- |
| `name` | 发现和调用用的名称 | 用函数名 |
| `description` | 列表里显示的说明 | 先取 docstring 首行，再取函数名 |
| `phases` | 描述流程有哪些阶段 | 只是元数据 |

`@workflow` 给函数附上 `WorkflowSpec` 元数据，被装饰的函数仍然可以直接调用。真正运行时，OC 做的就是 `await fn(ctx, args)`。`phases` 只描述流程，真正发出阶段事件的是函数体里的 `await ctx.phase(...)`，顺序由你安排。

### 入口函数的约定

- 签名通常是 `async def name(ctx, args)`，`args` 是字典。
- 返回值可以是文本、字典、列表或任何适合调用方的值。结果要经过 CLI、清单或子进程保存时，请返回能 JSON 序列化的数据。
- 校验输入、导入业务库、调用辅助函数，都可以直接写在函数里。

## 用 Python 控制流程

### 调用几次 Agent，由输入决定

下面的函数先用 Python 求和，再逐个让 Agent 解释每个数字。输入有几个数字就调用几次 Agent，调用次数不是事先写死的：

```python
async def explain_numbers(ctx, args):
    # 纯 Python：转换和求和
    values = [int(value) for value in args['values']]
    total = sum(values)
    # 纯 Python：总和为 0 就直接返回，不调用 Agent
    if total == 0:
        return {'total': 0, 'explanations': []}

    explanations = []
    for index, value in enumerate(values):
        # ctx 读取运行资源：时间不够或额度不足就停下
        if ctx.time_low() or ctx.tokens_remaining() < 2000:
            break
        # ctx 创建一次 Agent 会话
        answer = await ctx.agent(
            f'用一句话解释 {value} 在总和 {total} 中的作用。',
            label=f'item-{index}', tools=[], budget=2000,
        )
        explanations.append(answer)
    return {'total': total, 'explanations': explanations}
```

列表转换、求和、`if`、`for`、`break` 和最后的字典都是普通 Python；只有 `ctx.agent`、`ctx.tokens_remaining`、`ctx.time_low` 是 OC 提供的。

### “修改、检查、再修改”的循环

典型写法：让 Agent 修改文件 → 调用真实的验证器（比如跑测试）→ 通过就返回 → 失败就把失败报告放进下一轮提示词，回到第一步。

循环一定要有结束条件：最多几轮、剩余时间、剩余额度，或检查结果。只是重试几次、或者试几个有限的方案，用固定次数的 `for` 就够了。

### 让 Agent 返回结构化结果，由 Python 判断

`ctx.agent` 可以按 JSON Schema 返回字典，比如 `{"needs_fix": true}`。但 OC **不会根据字段自动路由**：

- 模型返回 `needs_fix=true`，并不会自动触发修复。只有你的代码读到这个字段并调用修复步骤，才会真的去修。
- 模型在回答里说“我已经跑过测试”，不等于真的跑过。要看到工具执行记录和检查结果才算数。

结构化输出的写法见 [在 Workflow 里调用 Agent](workflow-agent-calls.md#拿结构化结果schema)。

### 动态生成一批任务

可以用列表推导、按输入分组、闭包等方式，临时造出一批任务：

- 一批互不相关的任务：交给 `ctx.parallel` 同时执行。
- 每一项都要依次经过几个阶段：用 `ctx.pipeline`。
- 任务之间有依赖（DAG）：OC 没有内置通用的图执行器。用你自己的 Python 数据结构记下节点、已完成的结果和依赖集合，每轮挑出依赖都满足的节点交给 `parallel`，再把结果送进下一批。节点是什么、失败了怎样影响别的节点、什么时候停，都由你定义。想要现成的方案，Weave 提供了按组规划依赖的实现，依赖成环的组会合并到同一个会话里，见 [Weave](weave-workflow.md)。

`parallel` 和 `pipeline` 的用法见 [批量并行](workflow-agent-calls.md#批量并行parallel-与-pipeline)。

### 把流程拆成多个函数

```python
def normalize_goal(text):
    # 普通函数：直接调用
    return ' '.join(text.split())

async def describe(ctx, args):
    goal = normalize_goal(args['goal'])
    return await ctx.agent(goal, label='describe', tools=[])

async def composite(ctx, args):
    # 异步函数：用 await 等待，并把同一个 ctx 传下去
    first = await describe(ctx, args)
    if first is None:
        return {'summary': None}
    return {'summary': first, 'length': len(first)}
```

普通函数直接调用，异步函数用 `await`。传同一个 `ctx` 的函数，共用同一套环境、预算和并发容量。想让一段子流程在隔离的副本里试改动、检查后再决定要不要，用候选子流程；想在流程里跑一个独立的 Team，由宿主程序调用 SDK 的 `team()`。两者见 [组合与嵌套](workflow-composition-hierarchy.md)。

## OC 管什么，你自己管什么

| 你想做的事 | 用什么 | 说明 |
| --- | --- | --- |
| 条件分支、循环与反馈 | Python | 写清轮数、资源和结束条件 |
| 普通模块和业务逻辑 | Python | 导入当前环境已装的库，受解释器权限限制 |
| 独立任务并行 | `ctx.parallel` | 共用集合容量和会话容量 |
| 每一项走多个阶段 | `ctx.pipeline` | 每项按阶段顺序各自推进 |
| 依赖图和 DAG | Python，或 Weave 的分组 | 自己写依赖执行逻辑，或套用 Weave |
| Agent 返回结构化结果 | `ctx.agent(schema=...)` | 校验结构，纠正一次 |
| 子函数与候选子流程 | Python，或 `ctx.candidate_workflow` | 共用 `ctx`，或在候选环境里运行再采纳 |
| 在流程里组合一个独立 Team | 宿主 SDK 的 `OpenCollab.team` | 预算、取消和资源要自己接 |
| 内置与自定义工具 | `builtin_tools`、公开的 `Tool` | 见 [内置工具](../extensions/tool-selection.md) |
| 工作区、容器、VM | 隔离策略 | 见 [隔离](../isolation/isolation-overview.md) |
| 共享模型服务容量 | 宿主的请求池配置 | 见 [模型路由与共享请求容量](../models/model-routing-request-capacity.md) |
| 业务失败重试 | Python 和传输层各管一部分 | 步骤按副作用自己决定是否重试；模型客户端处理网络类重试 |
| 调试与交付验证 | `phase`、`log`、`label`、轨迹、真实检查 | 见 [失败处理、测试与调试](workflow-failure-recovery-lifecycle.md) |
| 中断后跨进程自动接着跑 | 你自己设计 | 保存已完成的步骤和业务状态，重建依赖后继续 |
| 分发到多台主机 | 你的服务 | 任务队列、远端资源、跨主机状态和结果协调 |

简单说：OC 负责**单次运行之内**的会话、并发、额度、候选环境和隔离；不负责跨进程自动续跑，也不负责多主机调度。比如“三个 Agent 并行调研，再按结论决定是否追加”，用 Python 加 `ctx.parallel` 就能写；“分发到十台机器”，需要你自己的任务队列。

设计时先写清三件事：输入是什么、要做哪些操作、什么时候停。工作流代码读取模型结果、决定下一步，并通过文件、工具执行记录和实际检查确认结果。

## 代码到底在哪里运行

**Python 编排代码在哪里？** 通过 SDK 运行 Workflow 时，它总在调用方的 Python 进程里，无论你选了什么隔离策略。唯一的例外是 Team 成员用 `manage_workflow` 启动的子 Workflow：它在成员所在的执行环境里起一个独立的 Python 子进程，见 [让 Team 成员运行 Workflow](team-run-workflow.md)。

**Agent 的工具在哪里？** 由隔离策略决定：共享目录、独立工作树、Docker 容器或 Linux VM，见 [隔离](../isolation/isolation-overview.md)。

这带来一个常见误会：选了 Docker 策略后，只有 Agent 的文件和 shell 工具进了容器。你在 Workflow 函数里直接用 `Path` 或 `subprocess`，操作的仍是宿主进程所在的位置。写文件或调用业务库之前，先弄清代码在哪个进程里跑、路径指向哪里。

另一个要注意的是事件循环。`await` 在等模型、工具或其他异步操作时，会把执行机会让给别的任务；但普通的同步计算仍在当前进程里执行。耗时较长的阻塞操作（网络、磁盘或大量计算）会卡住事件循环，让并发变慢、取消不及时。解决办法：改用对应的异步库，用 `asyncio.to_thread` 放进线程，或者由应用另开进程。

## 观察和资源查询

| 方法 | 作用 |
| --- | --- |
| `ctx.phase(name)` | 标记当前阶段，之后开始的会话会关联到这个阶段 |
| `ctx.log(text)` | 写入流程日志 |
| `label=` 参数 | 在会话记录里区分角色 |
| `ctx.diff()` | 返回可观察到的改动；探测不可用时返回 `None` |
| `ctx.source_changed()` | 返回改动探测结果；探测不可用时返回 `None` |
| `ctx.tokens_remaining()`、`ctx.seconds_left()`、`ctx.time_low()` | 用来决定还要不要再开一轮 |

`None` 表示信息暂时拿不到，不等于“没有改动”。完整接口见 [Workflow API 速查](workflow-public-api-map.md)。

建议先跑通并验证一项具体任务，再按需加分支和并行。

## 常见问题

- **`workflow list` 里没有你的 Workflow**：检查文件是否在 `workflows/` 顶层，函数上是否有 `@workflow`。
- **报注册错误**：和已有的 Workflow 重名了，改 `name`。
- **运行时报 `KeyError: 'goal'`**：没有传 `--task` 或 `--task-file`，`--args` 里也没有 `goal`。

## 延伸阅读

- 源码依据：[opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow.py) · [opencollab/application/workflow_registry.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_registry.py) · [opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/workflows.py) · [opencollab/bootstrap/_workflow_runtime_execution.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/_workflow_runtime_execution.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md)
- 相关指南：[在 Workflow 里调用 Agent](workflow-agent-calls.md) · [组合与嵌套](workflow-composition-hierarchy.md) · [失败处理、测试与调试](workflow-failure-recovery-lifecycle.md) · [Workflow API 速查](workflow-public-api-map.md)
