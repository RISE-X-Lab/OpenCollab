---
{
  "id": "workflow-failure-recovery-lifecycle",
  "title": "Workflow 出错了：失败、重试与恢复",
  "category": "workflow",
  "summary": "某一步失败时先弄清是哪一层出的错，检查会话、工作区和产物，再决定返回部分结果、重试还是停下保留现场。",
  "aliases": [
    "一步失败会怎样",
    "怎么重试工作流",
    "进程退出后会自动接着跑吗",
    "超时后代码还会继续吗",
    "能从中间步骤恢复吗",
    "保存会话等于保存进度吗",
    "处理 Workflow 的失败、重试与恢复"
  ],
  "keywords": [
    "Workflow 失败恢复",
    "异常传播",
    "重试",
    "生命周期",
    "agent_failures",
    "RunError",
    "cleanup_timeout",
    "workspace_ready"
  ],
  "questions": [
    "Workflow 里某一步失败了会怎样？",
    "怎么给某一步加重试？",
    "超时之后后台还在跑吗？",
    "进程退出后能从中间步骤接着跑吗？",
    "运行显示 completed，为什么还有失败？"
  ],
  "related": [
    "workflow-testing-debugging",
    "workflow-agent-calls",
    "compose-workflow-candidates",
    "weave-workflow",
    "member-authored-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# Workflow 出错了：失败、重试与恢复

OC 的 Workflow 失败不是一个统一的异常：`ctx.agent` 返回 None，`ctx.agent_run` 返回带状态和清理快照的对象，集合里单项失败变成 None，顶层可能是 failed 的 `RunResult` 或抛出 `RunError`。重试由你的 Python 代码负责，OC 不会自动重跑业务步骤，也不会跨进程自动续跑。

本文先给出各层失败的形态，再给一个有限重试的最小例子，最后讲超时清理、环境撤销和候选交付失败时 OC 的默认行为。

## 第一步：看是哪一层出错

| 出错位置 | 你会拿到什么 | 接下来要看什么 |
| --- | --- | --- |
| `ctx.agent` | 文本、字典或 None | 这一步有没有拿到可用的输出 |
| `ctx.agent_run` | 状态、原因、用量和清理快照 | 环境还能不能继续用 |
| `parallel` 或 `pipeline` | 普通错误时，对应那一项是 None | 哪个输入需要补做 |
| 候选捕获与采用 | 候选证据，或者抛出异常 | 源工作区有没有变、保留的工作区在哪 |
| SDK 顶层 | `RunResult`，或与证据保存相关的 `RunError` | 执行状态、原因、产物和异常 |

顶层结果要注意三件事：

1. 状态是 completed 不代表没有失败。如果某个内部角色失败了，但入口函数仍返回了其他数据，外层照样可能是 completed。所以要同时读 `RunResult.agent_failures`，它保存了内部角色的失败信息（已脱敏）。
2. 普通业务异常发生时，只要执行证据保存好了，会得到一个 failed 结果。如果基础设施出问题或者证据保存不了，可能直接抛出 `RunError`。
3. 参数校验错误可能在运行开始前就直接抛出。

## 第二步：给可以重复的步骤加有限重试

下面是一个完整的工作流，只读文件，最多试两次：

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

外层运行要给够预算，保证第一次失败后还有余额可以重试。

如果步骤会写文件或调用外部服务，就要根据它实际做了什么来设计重试。比如文件已经改好了，只是后面的模型回复失败了。这时应先读文件、检查结果，再决定从哪里继续，而不是直接重跑。

两种重试不要混淆：

| 重试类型 | 谁负责 | 重做什么 |
| --- | --- | --- |
| 业务重试 | 你写的 Python | 重新执行整个步骤 |
| 传输重试 | 模型客户端 | 遇到已分类的临时网络错误，重新发送模型请求 |

两者分别计数。

## 并行任务中有一项失败

在 `parallel` 或 `pipeline` 里：

- 普通异常会让那一项变成 None，其他输入继续执行。
- 想知道具体错在哪，就在单元函数里自己捕获异常，返回一个字典，写上索引、错误类型和部分结果。
- 预算耗尽、执行环境被撤销、外部取消，这三种情况不会被吞掉，会向集合外传播。

## 执行环境被撤销

环境被撤销后：

- Workflow 在启动新工作前、以及等到会话容量后，都会检查环境状态。
- 还在排队的角色会停止，不再开始新的工作。
- 已保存的会话、用量和候选恢复材料仍然可以用来排查。

候选环境和父环境是分开管理的。排查时要对照失败具体发生在哪个环境。

## 超时后要等清理完成

`timeout` 到了，不代表后台立刻停下，模型或工具的工作可能还在收尾。

- `agent_run` 会在 `cleanup_timeout` 时间内等它启动的任务结束。
- 如果清理没完成，就返回 failed，并把共享上下文标记为不能再继续用。
- `cleanup_complete` 为真：这次的快照已经稳定。
- `workspace_ready`：后续还适不适合继续用这个环境。

外部取消的情况：

- 取消以 `asyncio.CancelledError` 向上传播，运行时会继续管理并清理已经启动的任务。
- 清理期间晚到的模型用量也会被计入。
- 最终成本和退出状态，要结合持久化的用量、轨迹和清理指标一起看。
- 如果环境是调用方传进来的，就由调用方负责清理。在调用方确认清理结束之前，相关 environment 指标可能一直显示未知。

## 候选改动没能交付

候选改动捕获失败、源工作区变了、或者导出不完整时，运行时会保留候选环境，并报告恢复位置。

处理顺序：

1. 先去恢复位置拿到完整的文件和差异。
2. 再决定怎么处理源工作区。

Git 候选里的原始源树和当前候选文件，可以帮你找出冲突和增量。详见[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)。

## 进程退出后能接着跑吗

不能自动接着跑。会话、清单和轨迹保存的是执行证据，不是可恢复的程序状态。Python 调用栈、任意库对象和外部副作用都无法跨进程恢复。

想支持中途恢复，应用需要自己做：

1. 保存已经完成的业务步骤、它们的输入和结果。
2. 下次从一个明确的入口继续。

Weave 已经内置了这种做法：它用显式的状态快照，配合原工作区、相同的分组定义和 `run_id`，恢复生成和检查的进度。其他 Workflow 可以按自己的任务设计类似的状态记录。见[用 Weave 分组接力完成任务并检查交付](weave-workflow.md)。

## 源码依据

[opencollab/application/workflow_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_collections.py#L245)、[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L121)、[docs/weave.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/weave.md#L184)。

[opencollab/bootstrap/programmatic.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/programmatic.py)。

## 相关章节

[测试和调试你写的 Workflow](workflow-testing-debugging.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[用 Weave 分组接力完成任务并检查交付](weave-workflow.md)、[让成员自己写并运行子 Workflow](member-authored-workflow.md)。
