---
{
  "id": "workflow-testing-debugging",
  "title": "测试和调试你写的 Workflow",
  "category": "workflow",
  "summary": "从最小的可运行例子开始，依次检查分支逻辑、数据传递、工具执行和最终任务结果。",
  "aliases": [
    "怎么调试自己写的工作流",
    "怎么确认分支走对了",
    "如何用离线模型测试流程",
    "日志和会话保存在哪里",
    "工作流完成了为什么任务还没完成",
    "验证与调试自定义 Workflow"
  ],
  "keywords": [
    "Workflow 调试",
    "Workflow 验证",
    "trace",
    "运行记录",
    "FakeContext",
    "offline",
    "orchestration.jsonl"
  ],
  "questions": [
    "怎么调试自己写的 Workflow？",
    "不调用真实模型，怎么测试流程逻辑？",
    "Workflow 的日志和会话保存在哪里？",
    "Workflow 显示完成了，为什么任务还没完成？"
  ],
  "related": [
    "workflow-public-api-map",
    "workflow-failure-recovery-lifecycle",
    "duo-workflow",
    "weave-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 测试和调试你写的 Workflow

OC 的 Workflow 是普通异步 Python 函数，所以可以分三层测：先用一个假的 `ctx` 测控制流（不需要 OC，不调模型）；再用 `OpenCollab(...).workflow(...)` 真实跑一遍，检查集合和运行状态；最后看工具回执和实际文件，确认任务本身完成。

本文给出前两层的可运行脚本，并说明 OC 把会话、编排记录和轨迹保存在哪里。

## 第一步：用替身测试 Python 逻辑

下面的脚本只用 Python 标准库，可以直接在本地运行。它用一个假的上下文（替身）记录调用，检查分支和结果传递对不对。

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

替身的输出是你控制的，适合检查这些：

- 条件分支走得对不对
- 循环次数上限
- 参数拼接
- None 的处理

涉及文件和副作用的步骤，可以检查真实文件内容，或者由应用的测试环境提供实现。先确认关键行为，再逐步加复杂度，出问题时更容易定位。

## 第二步：用 SDK 真实跑一遍

下面的脚本会真正建立 Workflow 上下文并执行集合操作，用来检查阶段函数的参数、集合输出和最终运行状态。

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

说明：

- 这里的 `model='offline'` 只是标识测试配置。这个流程完全由普通 Python 和集合方法完成，没有调用 Agent。
- 要测试 Agent 路径，可以接入一个可控的、协议兼容的 `llm` 替身，或者真实模型，然后检查实际的工具调用。
- 模型从 `workflow(..., llm=...)` 注入。仓库里的 SDK 测试可以当现成例子参考。

## 第三步：观察阶段、会话和文件

在流程里加标记，事后才看得懂发生了什么：

| 方法 | 用途 |
| --- | --- |
| `phase` | 标记业务阶段。之后开始的会话会关联到这个阶段名 |
| `label` | 标识角色。并行任务共享阶段名时，要靠 label 区分 |
| `log` | 写下输入索引、输出规模、下一步决定。要让读者看出发生了哪件事 |

运行记录保存在哪：

- CLI 默认保存会话和 `orchestration.jsonl`。
- SDK 用 `artifacts` 指定一个新目录或空目录，用 `trace` 选择是否记录轨迹。
- `trace=False` 时，清单和原生会话证据仍然会保留。
- 工作流清单里的运行 id 就是 `RunResult.metrics['run_id']`，可以用它把会话和编排记录对上。

检查工具真的执行了，要看真实的文件或工具输出：

1. 让角色写一个内容已知的文件，再从绑定的环境里读出来核对。
2. 让角色运行任务自带的检查，再读退出状态和测试记录。

注意执行位置：Docker 工具在容器工作区里运行，而 Workflow 的 Python 代码留在调用方进程里。核对路径前，先确认代码在哪执行。

## 测试失败情况

如果流程有额度、时限或写文件的操作，就应该真的触发一次失败，观察：

- 写了一半的文件是否保留
- 结果里有没有对应的停止原因
- 清理是否完成

这些字段能帮你判断：

- `agent_run` 的 `cleanup_complete` 和 `workspace_ready`：还能不能继续。
- `RunResult.agent_failures`：内部发生过的失败。
- 候选错误里的恢复位置：用来保住还没交付的工作。

更多见[Workflow 出错了：失败、重试与恢复](workflow-failure-recovery-lifecycle.md)。

## 最后：确认任务本身完成了

“流程跑完了”不等于“任务做完了”。要分三层看：

1. 运行状态：流程有没有跑完。
2. 工具回执：操作有没有真的执行。
3. 实际检查结果：任务有没有通过验证。

最终检查要从任务要求的入口进去：

| 任务类型 | 怎么检查 |
| --- | --- |
| 库修改 | 调用公开函数 |
| 命令任务 | 运行真实命令，比较输出 |
| 数据任务 | 读取实际产物 |
| 服务任务 | 检查接口行为 |

内置流程的情况：Duo 的采用结果表示选中的改动已经交付；Weave 的 `delivery_ok` 依赖实际执行的检查。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L91)、[opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/result.py#L18)、[opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow.py#L734)、[tests/interfaces/test_sdk_workflow_nested_collections.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/tests/interfaces/test_sdk_workflow_nested_collections.py#L10)。

## 相关章节

[Workflow 公开 API 速查](workflow-public-api-map.md)、[Workflow 出错了：失败、重试与恢复](workflow-failure-recovery-lifecycle.md)、[用 Duo 跑两个候选方案并选出一个](duo-workflow.md)、[用 Weave 分组接力完成任务并检查交付](weave-workflow.md)。
