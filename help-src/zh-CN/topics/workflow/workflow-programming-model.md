---
{
  "id": "workflow-programming-model",
  "title": "工作流就是普通 Python：编程模型入门",
  "category": "workflow",
  "summary": "工作流是一个普通的异步 Python 函数，流程用 Python 写，调用 Agent 用 ctx。",
  "aliases": [
    "工作流到底能写什么",
    "if 和 for 能直接用吗",
    "能写 while 循环吗",
    "可以动态决定下一步吗",
    "一定要固定流程图吗",
    "工作流是不是特殊语法",
    "工作流编程模型",
    "ctx 是什么"
  ],
  "keywords": [
    "Workflow 编程模型",
    "Python",
    "async def",
    "WorkflowContext"
  ],
  "questions": [
    "工作流里能直接用 if、for、while 吗？",
    "工作流可以根据上一步结果动态决定下一步吗？",
    "ctx 和普通 Python 代码各负责什么？",
    "工作流的代码到底在哪个进程里运行？"
  ],
  "related": [
    "author-custom-workflow",
    "workflow-control-granularity",
    "workflow-dataflow-structured-io",
    "workflow-parallel-pipeline",
    "workflow-public-api-map",
    "workflow-capabilities-boundaries"
  ],
  "verified_ref": "0c47f74a"
}
---

# 工作流就是普通 Python：编程模型入门

OC 的 Workflow 没有图声明或 DSL，它就是一个 `async def fn(ctx, args)`。分支、循环、并发、何时结束都写成普通 Python 控制流；需要模型干活时调用 `ctx.agent`，每次调用开一个新会话。与 LangGraph 这类先定义节点和边的框架不同，OC 的“图”是在运行时由代码动态展开的。本篇说明 Python 和 `ctx` 各管什么，以及代码实际在哪个进程执行。

## 一句话分工

| 谁负责 | 做什么 |
| --- | --- |
| Python 本身 | 变量、函数、条件判断、循环、异常处理、导入模块 |
| `ctx`（工作流上下文对象） | 创建 Agent 会话、查询剩余额度和时间、管理运行环境 |

`ctx` 是 OC 在调用你的函数时传进来的第一个参数，类型是 `WorkflowContext`。

## 最小示例：先算，再按需调用 Agent

下面这个函数可以直接传给 SDK 运行。它先用 Python 求和，再逐个让 Agent 解释每个数字。

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

这里可以看出：

- 列表转换、求和、`if`、`for`、`break` 和最后的字典，都是普通 Python。
- `ctx.agent` 创建模型会话。
- `ctx.tokens_remaining` 和 `ctx.time_low` 读取剩余额度和时间。
- 输入有几个数字，就会调用几次 Agent。调用次数由输入决定，不是事先写死的。

同样的写法也适合“先探索、再修复、再收集结果”这类任务。

## 根据结果决定下一步

### 用 `while` 做“修改、检查、再修改”

典型做法：

1. 让 Agent 修改文件。
2. 调用真实的验证器（例如跑测试）。
3. 如果通过，就返回。
4. 如果失败，就把失败报告放进下一轮的提示词，回到第 1 步。

循环一定要有结束条件，比如最多几轮、剩余时间、剩余额度，或检查结果。如果只是重试几次，或试几个有限的候选方案，用固定次数的 `for` 就够了。

### 让 Agent 返回结构化结果，再由 Python 判断

`ctx.agent` 可以按 schema 返回字典，比如 `{"needs_fix": true}`。OC 不会根据字段自动路由，注意两点：

- 模型返回 `needs_fix=true`，并不会自动触发修复。只有你的代码读到这个字段，并调用修复步骤，才会真的去修。
- 模型在回答里说“我已经跑过测试”，不等于真的跑过。要看到工具执行记录和检查结果才算数。

### 动态生成任务

你可以用列表推导、按输入分组、闭包等方式，临时造出一批任务：

- 一批互不相关的任务：用 `ctx.parallel` 同时执行，它接收一个异步函数列表。
- 每一项都要经过几个阶段：用 `ctx.pipeline`。
- 任务之间有依赖关系（DAG）：OC 没有内置通用图执行器，用你自己的 Python 数据结构记录依赖。节点是什么、什么时候可以开始、失败了怎么办，都由你定义。如果想要现成实现，Weave 提供了按组规划依赖的方案。

## 把流程拆成多个函数

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

- 普通函数直接调用，异步函数用 `await` 等待。
- 传同一个 `ctx` 的函数，共用同一套环境和资源。
- 如果需要一个完整的候选环境，用 `candidate_workflow`。
- 如果需要一个独立的 Team，由宿主程序显式调用 SDK 的 `team` 入口，并自己管理它的资源。

能嵌套多深，取决于具体调用路径和后端是否支持。

## 进阶：代码实际在哪里运行

### `await` 与事件循环

`await` 在等待模型、工具或其它异步操作时，会把执行机会让出去，让其它任务继续推进。但普通的同步计算仍在当前进程里执行。

如果你写了耗时较长的阻塞操作（网络、磁盘或大量 CPU 计算），它会卡住事件循环，导致并发变慢、取消也不及时。解决办法：

- 改用对应的异步库；
- 用 `asyncio.to_thread` 放到线程里；
- 或者由应用另开一个独立进程。

### 不同启动方式下的位置

| 启动方式 | Python 编排代码在哪 | 说明 |
| --- | --- | --- |
| 宿主程序直接运行工作流 | 调用方进程 | 默认情况 |
| 配置了 Docker 会话策略 | 仍在宿主进程 | 只有 Agent 的文件和 shell 工具进容器，你自己写的 Python 文件操作仍在宿主执行 |
| Team 成员通过 `manage_workflow` 启动源文件 | 成员所在的执行环境 | Python 进程在成员那边 |

写文件或调用业务库之前，先弄清楚代码在哪个进程里跑，以及路径指向哪里。

## 进阶：`@workflow` 只是声明

`@workflow` 给函数附上名称、说明和描述性的阶段列表，方便按名称找到它。真正运行时，OC 做的就是 `await fn(ctx, args)`。

- `phases` 元数据只是描述流程。
- `ctx.phase` 才会真正发出阶段事件。
- 两者都由作者自己安排。

## 小结

1. 先把步骤写成能跑的 Python 控制流。
2. 为每次 Agent 会话选好工具和环境。
3. 用真实检查确认交付。
4. 需要更多控制时，再逐步加分支、循环和并行。

## 源码依据

[opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow.py#L1)、[opencollab/application/workflow_registry.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_registry.py#L18)、[opencollab/bootstrap/_workflow_runtime_execution.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/_workflow_runtime_execution.py#L103)、[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L251)。

## 相关章节

[编写第一个自定义 Workflow](author-custom-workflow.md)、[在哪一层调整 Workflow：从 CLI 参数到单次调用回调](workflow-control-granularity.md)、[Workflow 里的数据怎么传：输入、步骤结果、文件与输出](workflow-dataflow-structured-io.md)、[在 Workflow 中并行处理多项任务](workflow-parallel-pipeline.md)、[Workflow 公开 API 速查](workflow-public-api-map.md)、[工作流能做什么、不能做什么](workflow-capabilities-boundaries.md)。
