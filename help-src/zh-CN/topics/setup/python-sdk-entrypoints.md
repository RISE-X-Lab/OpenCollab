---
{
  "id": "python-sdk-entrypoints",
  "title": "从 Python 调用 OpenCollab",
  "category": "setup",
  "summary": "用 OpenCollab 客户端在 Python 里调用单 Agent、团队或工作流，三者都返回统一的 RunResult。",
  "aliases": [
    "代码调用",
    "嵌入自己的程序",
    "异步调用",
    "Base",
    "Single2",
    "config=",
    "model_routes=",
    "create_model_client"
  ],
  "keywords": [
    "Python SDK",
    "OpenCollab",
    "agent",
    "agent2",
    "team",
    "workflow",
    "asyncio.run",
    "configuration"
  ],
  "questions": [
    "在现有 Python 程序中构造客户端，调用单 agent、团队或工作流，并接入程序的结果处理？",
    "怎么在自己的 Python 程序里调用 OC？",
    "在 Notebook 或 Web 服务里怎么用？",
    "team() 的 config 参数和客户端的 config 有什么不同？"
  ],
  "related": [
    "configuration-precedence",
    "token-step-concurrency-limits",
    "cancellation-cleanup",
    "configuration-diagnostics",
    "run-results",
    "artifacts-and-history",
    "tool-selection"
  ],
  "verified_ref": "0c47f74a"
}
---

# 从 Python 调用 OpenCollab

SDK 的入口是 `OpenCollab(workspace)` 客户端，上面有三个 async 方法：`agent()`（单 Agent）、`team()`（团队）和 `workflow()`（工作流）。三者都返回同一个 `RunResult`，应用可以用一套代码处理结果。模型和传输配置只在构造客户端时解析一次。

## 最小例子

把脚本放在项目根目录，在装了 OpenCollab 的环境里运行。它用只读工具检查项目，沿用配置文件和环境变量里的模型设置。

```python
import asyncio
from opencollab import OpenCollab, RunError

async def main() -> None:
    client = OpenCollab(".")  # 工作区必须已经存在
    result = await client.agent(
        "阅读项目的测试配置，给出最小验证步骤和依据文件。",
        tools="read",
        budget=100_000,   # token 预算
        max_steps=40,     # 步数上限
        timeout=300,      # 任务期限（秒）
        artifacts="artifacts/sdk-first-run",  # 必须是新目录或空目录
    )
    try:
        # 完成时返回自身，其他状态抛出 RunError
        print(result.raise_for_status().output)
    except RunError as exc:
        outcome = exc.result
        if outcome is not None:
            print(outcome.status, outcome.reason, outcome.output)
        raise

asyncio.run(main())
```

注意：

- `artifacts` 必须是新目录或空目录，下一次任务换一个位置。
- 如果你的程序已经有事件循环（Web 服务、Notebook 等），不要用 `asyncio.run`，直接 `await main()`，由宿主程序管理事件循环。

## 三个入口怎么选

| 入口 | 用途 | 说明 |
| --- | --- | --- |
| `agent()` | 一个 Agent 连续完成读、改、测 | 默认 Base，当前采用 Single2 |
| `agent2()` | 同上，明确选择 Single2 | 便捷入口 |
| `team()` | 成员按团队工具和关系自行交接 | `config=` 是团队 YAML 路径 |
| `workflow()` | 按明确阶段和程序条件执行 | 接受名称、工作流函数或 spec |

### `agent()` / `agent2()` 常用参数

`tools`、`system_prompt`、`budget`、`max_steps` 和 `run_control`。`steps` 是旧的步数别名，与 `max_steps` 同时传入会报错。

### `team()`

容易混淆的一点：`team()` 的 `config=` 是团队 YAML 文件路径，和构造客户端时 `OpenCollab(..., config={...})` 的模型运行设置不是一回事。

1. 先用 `opencollab team init team.yaml` 准备团队文件。
2. 检查角色工具和执行环境。
3. 调用（以下代码放在已有 `async` 函数中，`client` 沿用前面的实例）：

```python
result = await client.team(
    "分析目标问题，安排实现与验证，并提交结果。",
    config="team.yaml",
    budget=200_000,
    timeout=900,
)
```

### `workflow()`

可以传三种东西：

- 名称：已安装的工作流，或在工作区发现的工作流。名称查找读取工作区的 `workflows/`；相对路径的 `OPENCOLLAB_WORKFLOWS_DIR` 也从工作区解析。
- 函数：用 `@workflow` 装饰过的函数，或普通 async 函数。
- spec。

下面的完整例子用 Python 显式安排一次角色调用：

```python
import asyncio
from opencollab import OpenCollab, workflow
from opencollab.tools import builtin_tools

@workflow(name="project-summary")
async def project_summary(ctx, inputs):
    # 只给这个角色只读工具
    tools = builtin_tools("file_read", "grep", "git_diff")
    return await ctx.agent(inputs["task"], tools=tools, label="reader")

async def main() -> None:
    result = await OpenCollab(".").workflow(
        project_summary,
        {"task": "阅读项目入口，说明主要模块之间的关系。"},
        budget=100_000,
        concurrency=1,
    )
    print(result.status, result.output)

asyncio.run(main())
```

## 处理结果和错误

| `status` | 含义 |
| --- | --- |
| `completed` | 正常结束 |
| `stopped` | 停下了，原因在 `reason`；`output` 里可能有部分工作 |
| `failed` | 失败，`output` 里可能有部分工作 |

- `raise_for_status()`：完成时返回结果自身；其他状态抛出 `RunError`，异常的 `result` 属性带着原始结果。
- 参数错误在执行前就会抛出。
- 生命周期或持久化故障也可能直接抛出 `RunError`。应用应在调用处统一处理。

## 进阶：公开扩展入口

- 扩展模块：`opencollab.tools`、`opencollab.environments`、`opencollab.workflows`、`opencollab.profiles`。
- `client.configuration`：只读的有效配置，秘密值已经筛掉，可以用来做配置检查。
- `create_model_client()`：用同一份配置创建原生模型客户端，用于自定义模型调用。用完后由调用方执行 `await model.close()`。

## 来源

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L96)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L107)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L213)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L408)

## 相关指南

[配置从哪里读取以及哪一项生效](../models/configuration-precedence.md)　[设置 token 预算、步数与运行并发](../operations/token-step-concurrency-limits.md)　[停止任务并确认清理完成](../operations/cancellation-cleanup.md)　[检查生效配置与排查启动故障](../operations/configuration-diagnostics.md)　[读取运行结果与判断任务是否完成](../observability/run-results.md)　[找到运行记录、会话和交付文件](../observability/artifacts-and-history.md)　[选择 Agent 能使用的工具](../extensions/tool-selection.md)
