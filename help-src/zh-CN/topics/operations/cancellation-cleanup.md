---
{
  "id": "cancellation-cleanup",
  "title": "停止任务并确认清理完成",
  "category": "operations",
  "summary": "用 Ctrl+C、task.cancel() 或 timeout 停止运行，再通过 metrics 中的清理字段确认会话、环境和用量已经收尾。",
  "aliases": [
    "取消任务",
    "停止 agent",
    "超时退出",
    "强制中断",
    "清理没结束",
    "延迟用量",
    "工作区恢复",
    "保留 worktree",
    "OPENCOLLAB_TOOL_CANCELLATION_CLEANUP_TIMEOUT",
    "OPENCOLLAB_TOOL_CANCELLATION_FORCE_TIMEOUT",
    "OPENCOLLAB_TOOL_ENVIRONMENT_ABORT_TIMEOUT"
  ],
  "keywords": [
    "Ctrl+C",
    "asyncio.CancelledError",
    "cleanup_timeout",
    "session_quiesced",
    "environment_quiesced",
    "cleanup_quiesced",
    "execution_quiesced",
    "workspace_ready"
  ],
  "questions": [
    "怎么在终端或 Python 里停止正在运行的任务？",
    "取消之后怎么确认环境和会话已经清理干净？",
    "取消后 token 用量为什么还会变化？",
    "被停止的 Team 成员改动去哪里找回？"
  ],
  "related": [
    "session-save-resume",
    "timeouts-retries",
    "model-routing-request-capacity",
    "token-step-concurrency-limits",
    "workspace-recovery",
    "run-results"
  ],
  "verified_ref": "0c47f74a"
}
---

# 停止任务并确认清理完成

OC 的停止有两条路：终端里按 Ctrl+C，或在 Python 里 `task.cancel()` / 设置 SDK 的 `timeout=`。和多数框架不同，OC 在停止后会先跑一段由框架负责的清理（取消工具协程、中止它创建的执行环境、结算迟到的用量），清理结果分别写进 `result.metrics`。所以停止后别急着复用工作区，先看清理字段。

## 最小用法

| 入口 | 做法 | 结果 |
| --- | --- | --- |
| 终端 | 运行中按 Ctrl+C | 中断当前轮次，清空排队输入，拒绝待回答的问题；保留已生成的部分回答并显示原因 |
| 终端 | 空闲时按 Ctrl+C，或 `/exit`、`/quit` | 退出 |
| SDK | 传 `timeout=` | 到期返回 `status="stopped"`、`reason="timeout"` |
| SDK | `task.cancel()` | 框架清理阶段结束后再向外抛 `asyncio.CancelledError` |

下面是应用主动取消的完整例子。实际界面里可以把 `task.cancel()` 接到停止按钮。示例只用读取工具，模型配置沿用已准备好的设置。

```python
import asyncio
from opencollab import OpenCollab, RunControl

async def main() -> None:
    events = []
    client = OpenCollab(".")
    task = asyncio.create_task(client.agent(
        "阅读项目入口并整理主要模块。",
        tools="read",
        budget=100_000,
        cleanup_timeout=10,  # 限制运行结束后框架清理资源的等待时间（秒）
        artifacts="artifacts/cancel-example",
        run_control=RunControl(on_event=events.append),  # 接收器要快速同步返回
    ))
    await asyncio.sleep(1)
    if not task.done():
        task.cancel()
    try:
        result = await task
        print(result.status, result.reason)
    except asyncio.CancelledError:
        print("应用已收到取消结果")
    print([event.type for event in events])

asyncio.run(main())
```

- `cleanup_timeout`：有限正数，限制执行结束后框架清理自身资源的等待时间。
- `RunControl.tool_cancellation_cleanup_timeout`：单独调整工具取消的清理等待。
- `on_event` 接收器应尽快同步返回。可以先把事件存下来，再交给应用其他部分处理。

## 停止后核对状态

用 `result.metrics.get(...)` 读这些字段：

| 字段 | 含义 |
| --- | --- |
| `session_quiesced` | 会话和持久化阶段已确认结束 |
| `environment_quiesced` | 框架自己创建的执行环境已停止 |
| `cleanup_quiesced`、`execution_quiesced` | 汇总上面两项 |

如果执行环境是调用方传进来的，OC 不替你管理它。这时 `environment_quiesced`、`cleanup_quiesced`、`execution_quiesced` 都是 `None`。你要在自己清理完环境后，自己检查并记录它是否已停止。再次写入同一工作区前，要同时确认会话已停止、工作区可用。

在 Workflow 里，`ctx.agent_run` 返回受控调用结果，直接看其中的 `workspace_ready`，决定共享上下文能否继续。超时后如果还有 provider 或工具工作在清理，这次调用会等待自己的资源。清理没完成就返回失败，并阻止共享上下文继续执行。尚未结束的任务和它保留的预算仍由工作流管理。

## 用量和部分改动

- 取消时界面上已显示的 token 统计可能还会变。迟到的 provider 成功或错误用量会在框架清理阶段结算一次。
- `usage` 事件的 `late` 和 `purpose` 字段可以帮你识别这笔用量的来源。`cleanup_completed` 事件表示该接收器对应运行的清理已结束。
- 最终花费以保存的用量记录为准。
- 失败或停止的 Team 子成员仍可交回已有改动。如果补丁捕获失败，或返回消息装不下完整补丁，原生工作树会保留完整文件，并报告恢复位置。先导出需要的改动，再按工作区恢复指南释放工作树。

开始下一次任务前的检查顺序：

1. 确认会话清理结果（`session_quiesced` 等）。
2. 确认自己负责的环境已停止。
3. 检查工作区差异。

## 进阶：工具取消的等待顺序

工具超时或调用方取消后，运行时按下面顺序处理：

1. 请求工具协程取消，等待 `OPENCOLLAB_TOOL_CANCELLATION_CLEANUP_TIMEOUT`（默认 1 秒）。
2. 工具仍未停止或清理报错时，撤销环境并执行环境中止，等待 `OPENCOLLAB_TOOL_ENVIRONMENT_ABORT_TIMEOUT`（默认 1 秒）。
3. 中止仍未完成就取消中止协程，再等待 `OPENCOLLAB_TOOL_CANCELLATION_FORCE_TIMEOUT`（默认 0.5 秒）。
4. 仍在收尾的任务继续由该运行追踪，工具结果会带出清理失败信息。

这三个进程变量在模块导入时读取。要调整，就在启动 Python 或 CLI 之前导出有限正数。单 Agent 或 `ctx.agent_run` 里的 `RunControl.tool_cancellation_cleanup_timeout` 可以只覆盖本次会话的第 1 段等待。SDK 的 `cleanup_timeout` 管的是运行结束后的资源清理，调用方应根据返回的清理状态安排下一步。具体实现见[工具取消与环境中止实现](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/tool_execution_runtime.py)。

## VM 清理等待

配置 VM profile 后，Team、Workflow、CLI 和成员自编子运行采用 `vm.cleanup_timeout`，默认 60 秒。SDK 调用或子运行 `launch` 中的显式值优先。未配置 VM profile 时，Team 默认 10 秒，Workflow 默认 2 秒。`agent()` 的默认清理等待仍为 2 秒。

VM 清理停止命令、导出产物并执行管理员重置，完成后释放租约。失败时保留现场供恢复，同一对象可重试清理。控制进程重启后的资源恢复见 [VM 指南](../isolation/vm-environment-extension.md)。

## 来源

[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L182)　[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L121)　[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L149)　[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L165)

## 相关指南

[保存会话与恢复历史](session-save-resume.md)　[超时、重试与请求卡住诊断](timeouts-retries.md)　[逻辑模型路由与共享请求容量](../models/model-routing-request-capacity.md)　[设置 token 预算、步数与运行并发](token-step-concurrency-limits.md)　[找回隔离任务的改动并清理资源](../isolation/workspace-recovery.md)　[读取运行结果与判断任务是否完成](../observability/run-results.md)
