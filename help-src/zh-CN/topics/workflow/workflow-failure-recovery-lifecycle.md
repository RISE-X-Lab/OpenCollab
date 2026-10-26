---
{
  "id": "workflow-failure-recovery-lifecycle",
  "title": "Workflow 的失败处理、测试与调试",
  "category": "workflow",
  "summary": "Workflow 出错时先判断是哪一层出的错，再决定重试、返回部分结果还是保留现场；以及怎样不花模型额度先测控制流，再分层确认任务真的完成。",
  "aliases": [
    "一步失败会怎样",
    "怎么重试工作流",
    "进程退出后会自动接着跑吗",
    "超时后代码还会继续吗",
    "能从中间步骤恢复吗",
    "保存会话等于保存进度吗",
    "处理 Workflow 的失败、重试与恢复",
    "怎么调试自己写的工作流",
    "怎么确认分支走对了",
    "如何用离线模型测试流程",
    "日志和会话保存在哪里",
    "工作流完成了为什么任务还没完成",
    "验证与调试自定义 Workflow"
  ],
  "keywords": [
    "Workflow 失败恢复",
    "异常传播",
    "重试",
    "生命周期",
    "agent_failures",
    "RunError",
    "cleanup_timeout",
    "workspace_ready",
    "Workflow 调试",
    "Workflow 验证",
    "trace",
    "运行记录",
    "FakeContext",
    "offline",
    "orchestration.jsonl"
  ],
  "questions": [
    "Workflow 里某一步失败了会怎样？",
    "怎么给某一步加重试？",
    "超时之后后台还在跑吗？",
    "进程退出后能从中间步骤接着跑吗？",
    "运行显示 completed，为什么还有失败？",
    "怎么调试自己写的 Workflow？",
    "不调用真实模型，怎么测试流程逻辑？",
    "Workflow 的日志和会话保存在哪里？",
    "Workflow 显示完成了，为什么任务还没完成？"
  ],
  "related": [
    "workflow-agent-calls",
    "workflow-composition-hierarchy",
    "timeouts-retries",
    "weave-workflow",
    "run-results"
  ],
  "verified_ref": "0c47f74a"
}
---

# Workflow 的失败处理、测试与调试

Workflow 出错时，OC 不会抛出一个统一的异常：不同层级的失败长得不一样。重试也由你的 Python 代码负责，OC 不会自动重跑业务步骤，也不会在进程退出后自动续跑。这篇先讲怎样认出失败、怎样安全地重试，再讲怎样在不花模型额度的情况下测试你的流程，以及怎样确认任务真的完成了。

## 出错时先看是哪一层

| 出错位置 | 你会拿到什么 | 接下来看什么 |
| --- | --- | --- |
| `ctx.agent` | 文本、字典或 `None` | 这一步有没有拿到可用的输出 |
| `ctx.agent_run` | 状态、原因、用量和清理快照 | 环境还能不能继续用 |
| `parallel` 或 `pipeline` | 普通错误时，对应那一项是 `None` | 哪个输入需要补做 |
| 候选捕获与采纳 | 候选证据，或者抛出异常 | 源工作区有没有变、保留的工作区在哪 |
| SDK 顶层 | `RunResult`，或与证据保存相关的 `RunError` | 状态、原因、产物和异常 |

顶层结果要注意三件事：

1. **`completed` 不代表没有失败。** 某个内部角色失败了，但入口函数仍返回了其他数据，外层照样是 `completed`。所以要同时读 `RunResult.agent_failures`，它保存内部角色的失败信息（已脱敏）。
2. 普通业务异常发生时，只要执行证据保存好了，会得到一个 `failed` 结果；基础设施出问题或证据保存不了，可能直接抛出 `RunError`。
3. 参数校验错误可能在运行开始前就抛出。

## 给能重复的步骤加有限重试

下面是一个完整的 Workflow，只读文件，最多试两次：

```python
from opencollab.tools import builtin_tools

async def read_with_retry(ctx, args):
    attempts = []
    for index in range(2):  # 最多尝试两次
        run = await ctx.agent_run(
            args['goal'],
            label=f'inspect-{index}',
            tools=builtin_tools('file_read', 'grep'),  # 只读工具，重试没有副作用
            budget=4000,
            timeout=60,
        )
        attempts.append({'status': run.status, 'reason': run.reason})
        if run.status == 'completed':
            return {'answer': run.output, 'attempts': attempts}
        # 环境不能再用，或者剩余时间不多，就别再试了
        if not run.workspace_ready or ctx.time_low():
            break
    return {'answer': None, 'attempts': attempts}
```

外层运行要给够预算，保证第一次失败后还有余额重试。

会写文件或调用外部服务的步骤，要根据它实际做了什么来设计重试。比如文件已经改好了，只是后面的模型回复失败了：这时应该先读文件、检查结果，再决定从哪里继续，而不是直接重跑。

两种重试别混淆，它们分别计数：

| 重试类型 | 谁负责 | 重做什么 |
| --- | --- | --- |
| 业务重试 | 你写的 Python | 重新执行整个步骤 |
| 传输重试 | 模型客户端 | 遇到已分类的临时网络错误，重新发送模型请求，见 [重试怎样停止](../operations/timeouts-retries.md#重试怎样停止) |

## 几种特殊的失败

**并行任务里有一项失败。** 在 `parallel` 或 `pipeline` 里，普通异常会让那一项变成 `None`，其他输入继续执行；想知道具体错在哪，就在单元函数里自己捕获异常，返回带索引、错误类型和部分结果的字典。预算耗尽、执行环境被撤销、外部取消这三种情况不会被吞掉，会向集合外传播。

**执行环境被撤销。** Workflow 在启动新工作之前、以及等到会话容量之后，都会检查环境状态；还在排队的角色会停止，不再开始新的工作。已保存的会话、用量和候选恢复材料仍然可以用来排查。候选环境和父环境是分开管理的，排查时要对照失败发生在哪个环境。

**超时之后还在收尾。** `timeout` 到了，不代表后台立刻停下，模型或工具的工作可能还在收尾。`agent_run` 会在 `cleanup_timeout` 时间内等它启动的任务结束；清理没完成就返回 `failed`，并把共享上下文标记为不能再继续用。`cleanup_complete` 为真表示快照已经稳定，`workspace_ready` 表示后续还适不适合继续用这个环境。外部取消以 `asyncio.CancelledError` 向上传播，运行时会继续清理已经启动的任务，清理期间晚到的模型用量也会被计入。见 [运行怎样停下](../operations/timeouts-retries.md)。

**候选改动没能交付。** 捕获失败、源工作区变了、或导出不完整时，运行时会保留候选环境并报告恢复位置。先去恢复位置拿到完整的文件和差异，再决定怎么处理源工作区，见 [采纳失败时](workflow-composition-hierarchy.md#采纳失败时)。

## 进程退出后能接着跑吗

不能自动接着跑。会话、清单和轨迹保存的是**执行证据**，不是可恢复的程序状态：Python 调用栈、任意库对象和外部副作用都没法跨进程恢复。

想支持中途恢复，应用需要自己做两件事：保存已经完成的业务步骤及其输入和结果；下次从一个明确的入口继续。

Weave 内置了这种做法：它用显式的状态快照，配合原工作区、相同的分组定义和 `run_id`，恢复生成和检查的进度。其他 Workflow 可以按自己的任务设计类似的状态记录，见 [Weave：中断后继续](weave-workflow.md#中断后继续)。

## 测试：分三层

Workflow 是普通的异步 Python 函数，所以可以分三层测：先用假的 `ctx` 测控制流（不需要 OC，也不调模型）；再用 SDK 真实跑一遍，检查集合和运行状态；最后看工具回执和实际文件，确认任务本身完成了。

### 第一层：用替身测控制流

下面的脚本只用 Python 标准库。它用一个假的上下文记录调用，检查分支和结果传递对不对：

```python
import asyncio

# 要测试的流程：总和为 0 就不调用 Agent
async def flow(ctx, args):
    total = sum(args['values'])
    if total == 0:
        return {'total': total, 'answer': None}
    answer = await ctx.agent(str(total), tools=[], label='explain')
    return {'total': total, 'answer': answer}

# 替身：不调模型，只记下收到的提示词和 label
class FakeContext:
    def __init__(self):
        self.prompts = []

    async def agent(self, prompt, **kwargs):
        self.prompts.append((prompt, kwargs['label']))
        return f'Observed {prompt}'

async def main():
    ctx = FakeContext()
    # 总和为 0：不应调用 Agent
    assert await flow(ctx, {'values': [0]}) == {'total': 0, 'answer': None}
    assert ctx.prompts == []
    # 总和为 5：应调用一次 Agent
    result = await flow(ctx, {'values': [2, 3]})
    assert result == {'total': 5, 'answer': 'Observed 5'}
    assert ctx.prompts == [('5', 'explain')]
    print('CONTROL_FLOW_PASSED')

asyncio.run(main())
```

替身的输出由你控制，适合检查条件分支、循环次数上限、参数拼接和 `None` 的处理。先确认这些关键行为，再逐步加复杂度，出了问题更容易定位。

### 第二层：用 SDK 真实跑一遍

下面的脚本会真正建立 Workflow 上下文并执行集合操作，用来检查阶段函数收到的参数、集合输出和最终运行状态：

```python
import asyncio
from tempfile import TemporaryDirectory
from opencollab import OpenCollab

async def collection_flow(ctx, args):
    # pipeline 的阶段函数会收到上一步结果、原始输入和索引
    async def double(previous, original, index):
        return {'index': index, 'value': previous * 2}
    await ctx.phase('calculate')
    values = await ctx.pipeline(args['values'], double)
    await ctx.log(f'Produced {len(values)} rows')
    return values

async def main():
    with TemporaryDirectory() as workspace:  # 用临时目录当工作区
        result = await OpenCollab(workspace, model='offline').workflow(
            collection_flow, {'values': [2, 3]},
            concurrency=1, task_concurrency=1, trace=False,
        )
        assert result.status == 'completed', result.reason
        assert result.output == [{'index': 0, 'value': 4}, {'index': 1, 'value': 6}]
        print('SDK_COLLECTION_PASSED')

asyncio.run(main())
```

这里的 `model='offline'` 只是一个测试用的标识：这个流程完全由普通 Python 和集合方法完成，没有调用 Agent。要测试 Agent 路径，可以通过 `workflow(..., llm=...)` 注入一个可控的、协议兼容的模型替身，或者用真实模型，然后检查实际的工具调用。仓库里的 SDK 测试可以当现成例子参考。

### 第三层：确认任务本身完成了

“流程跑完了”不等于“任务做完了”。要分三层看：运行状态（流程有没有跑完）、工具回执（操作有没有真的执行）、实际检查结果（任务有没有通过验证）。最终检查要从任务要求的入口进去：

| 任务类型 | 怎么检查 |
| --- | --- |
| 库修改 | 调用公开函数 |
| 命令任务 | 运行真实命令，比较输出 |
| 数据任务 | 读取实际产物 |
| 服务任务 | 检查接口行为 |

内置流程也遵守这条规则：Duo 的 `adopted` 表示选中的改动已经交付，Weave 的 `delivery_ok` 依赖实际执行的检查。见 [判断任务是否真的完成](../observability/run-results.md)。

## 调试：给流程留下记号

在流程里加标记，事后才看得懂发生了什么：

| 方法 | 用途 |
| --- | --- |
| `ctx.phase` | 标记业务阶段，之后开始的会话会关联到这个阶段名 |
| `label=` | 标识角色；并行任务共享阶段名时，靠 label 区分 |
| `ctx.log` | 写下输入索引、输出规模、下一步的决定，让读者看出发生了哪件事 |

运行记录保存在哪：CLI 默认保存会话和 `orchestration.jsonl`；SDK 用 `artifacts` 指定一个新目录或空目录，用 `trace` 决定要不要记录轨迹。`trace=False` 时，清单和会话证据仍然会保留。Workflow 清单里的运行 id 就是 `RunResult.metrics['run_id']`，用它把会话和编排记录对上。见 [运行记录](../observability/artifacts-and-history.md)。

检查工具真的执行了，要看真实的文件或工具输出：让角色写一个内容已知的文件，再从绑定的环境里读出来核对；或者让角色运行任务自带的检查，再读退出状态和测试记录。注意执行位置：Docker 工具在容器里运行，Workflow 的 Python 代码留在调用方进程里。

如果流程有额度、期限或写文件的操作，建议**主动触发一次失败**，观察写了一半的文件是否保留、结果里有没有对应的停止原因、清理是否完成。`agent_run` 的 `cleanup_complete` 和 `workspace_ready`、顶层的 `agent_failures`，以及候选错误里的恢复位置，就是你判断“还能不能继续、要不要保住现场”的依据。

## 延伸阅读

- 源码依据：[opencollab/application/workflow_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_collections.py) · [opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow.py) · [opencollab/bootstrap/programmatic.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/programmatic.py) · [opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/result.py) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/evaluation-runtime.md) · [tests/interfaces/test_sdk_workflow_nested_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/tests/interfaces/test_sdk_workflow_nested_collections.py)
- 相关指南：[在 Workflow 里调用 Agent](workflow-agent-calls.md) · [组合与嵌套](workflow-composition-hierarchy.md) · [运行怎样停下](../operations/timeouts-retries.md) · [Weave](weave-workflow.md)
