---
{
  "id": "workflow-parallel-pipeline",
  "title": "在 Workflow 中并行处理多项任务",
  "category": "workflow",
  "summary": "用 ctx.parallel 同时跑一批独立任务，用 ctx.pipeline 让每项依次过多个阶段，并用两个并发上限控制规模。",
  "aliases": [
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
    "ctx.parallel",
    "ctx.pipeline",
    "并行任务",
    "concurrency"
  ],
  "questions": [
    "怎样在工作流里同时跑多个 Agent？",
    "parallel 和 pipeline 有什么区别？",
    "concurrency 和 task_concurrency 有什么不同？",
    "某个分支报错会让整批任务失败吗？",
    "并行任务的结果顺序和输入一致吗？",
    "可以嵌套使用 parallel 吗？"
  ],
  "related": [
    "author-custom-workflow",
    "workflow-agent-calls",
    "compose-workflow-candidates"
  ],
  "verified_ref": "0c47f74a"
}
---

# 在 Workflow 中并行处理多项任务

OC 提供两个集合方法来做扇出（fan out）：

| 方法 | 适合 | 签名 |
| --- | --- | --- |
| `ctx.parallel` | 一批互相独立的任务 | `parallel(thunks)`，传无参异步函数列表 |
| `ctx.pipeline` | 每项输入依次经过多个阶段 | `pipeline(items, *stages, stop_on_none=True)` |

两者都按输入顺序返回结果，并且受整个工作流范围内的集合单元上限（`task_concurrency`）约束。与直接用 `asyncio.gather` 不同，它们会按额度给每个单元分配预算，并把普通异常转成该位置的 `None`。任务何时创建、每个阶段调用什么 Agent、怎样汇总，仍由你的 Python 代码决定。

## `parallel`：同时执行独立任务

下面是可以直接传给 SDK 的完整函数。它为每段文本造一个无参异步函数，再交给运行时调度。

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

要点：

- 用工厂函数绑定变量，是为了避免循环闭包在执行时都读到最后一个值。
- 返回列表的位置对应原输入。先完成的任务也待在自己的位置上。
- 空任务列表返回空列表。
- 多个任务同时写同一个文件时，冲突要由应用自己避免。可以按文件分工，或者让候选使用独立环境。

## `pipeline`：每项依次走多个阶段

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

规则：

- 第一个阶段的 `previous` 就是原始输入。
- 同一项的各阶段按顺序执行；不同项可以同时处在不同阶段。
- `stop_on_none=True`（默认）：某阶段返回 `None` 时，这一项结束，结果位置为 `None`。
- `stop_on_none=False`：下一阶段会收到这个 `None`，需要你自己处理。

## 两个并发上限

| 参数 | CLI | 限制什么 | 默认 |
| --- | --- | --- | --- |
| `concurrency` | `--concurrency` | 同时活跃的 Agent 会话 | SDK 为 4 |
| `task_concurrency` | `--task-concurrency` | 同时活跃的集合单元 | 等于 `concurrency` |

例如 SDK 里写 `concurrency=4, task_concurrency=2`，表示最多四个活跃 Agent 会话，同时最多两个集合单元。

为什么分成两个：一个集合单元可能只跑普通 Python，也可能调一次 Agent，或者跑多个顺序步骤。两者分别计数，混合运行时，活跃工作数可能达到两个上限之和。

建议把大量输入交给集合方法，运行时会按 `task_concurrency` 维持有限数量的 worker。如果你自己对大量调用用 `asyncio.gather`，创建多少宿主任务由你负责；不过 `ctx` 的会话上限仍然生效。

成员自编 DW 使用 `manage_workflow launch` 的 `concurrency` 设置子运行的活跃会话容量，集合容量默认沿用它。成员可以现场写出任务列表再交给 `ctx.parallel`，完整的三任务例子见[成员自编 DW 教程](member-authored-workflow.md)。

宿主 `request_capacity` 限制模型客户端同时执行的原生请求，通过共享 `request_pool` 配置。多个 Team 与 DW 可以共享这项服务容量，各工作流的局部容量继续分别生效。扩大输入前，可以测量小任务的实际耗时和用量，再按服务能力选择并发。

## 预算分配

- 额度有限时，OC 按集合大小给每个单元规划额度。单元内的 Agent 调用从该单元和工作流的剩余额度里扣。
- 嵌套集合继续受父级额度和共享容量约束。
- 单元可以显式申请更小的 `budget`。实际授权还取决于共享余额和其它预留。

## 局部失败与取消

| 情况 | 结果 |
| --- | --- |
| `parallel` 某分支抛普通异常 | 该位置为 `None`，其它分支继续 |
| `pipeline` 某阶段抛普通异常 | 该项返回 `None`（`stop_on_none=False` 只影响正常返回 `None` 的情况） |
| 共享预算耗尽、环境被撤销 | 向集合外传播 |
| 外部取消 | 继续传播，并等待已启动任务完成取消和清理 |

如果想保留普通异常的详情，在分支里自己 `try`，返回一个包含错误类型和输入索引的字典。日志、`label` 和保存的会话可以进一步定位是哪个输入出的问题。

## 进阶：嵌套

- 集合可以嵌套。候选子工作流也共用集合任务容量。
- 运行时支持子任务借用父任务的容量，所以 `task_concurrency=1` 时嵌套集合也不会卡死。
- 嵌套结构会影响实际并发程度。可以用小输入、可控的延时和计数器测一下自己的安排。

## 已知问题：编辑器提示 `pipeline` 不存在

`pipeline` 实现在 `workflow_collections.py`，有 SDK 嵌套集合测试覆盖。但公开的 `WorkflowContext` Protocol 注解漏列了这个方法，编辑器可能提示属性缺失。可以照常调用，签名为 `pipeline(items, *stages, stop_on_none=True)`；开发时对照实际实现和对应 SDK 用例即可。

## 源码依据

[opencollab/application/workflow_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_collections.py#L137)、[tests/interfaces/test_sdk_workflow_nested_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/tests/interfaces/test_sdk_workflow_nested_collections.py#L10)、[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md#L136)。

## 相关章节

[编写第一个自定义 Workflow](author-custom-workflow.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)。
