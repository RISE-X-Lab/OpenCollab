---
{
  "id": "timeouts-retries",
  "title": "超时、重试与请求卡住诊断",
  "category": "operations",
  "summary": "OC 对连接、首个流事件、流空闲、单次请求、整次任务和工具执行分别设超时；先找到停在哪一层，再调那一层，并了解重试何时停止。",
  "aliases": [
    "请求卡住",
    "首包慢",
    "半路没输出",
    "连接超时",
    "429",
    "503",
    "Retry-After",
    "llm_timeout",
    "llm_max_retries",
    "provider_error_time_budget",
    "工具超时",
    "OPENCOLLAB_TOOL_EXECUTION_TIMEOUT",
    "OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT"
  ],
  "keywords": [
    "OPENCOLLAB_LLM_TIMEOUT",
    "OPENCOLLAB_LLM_CONNECT_TIMEOUT",
    "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT",
    "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT",
    "OPENCOLLAB_LLM_MAX_RETRIES",
    "OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET",
    "timeout",
    "cleanup_timeout"
  ],
  "questions": [
    "模型请求卡住不动该调哪个超时？",
    "怎么给整个任务设置最长运行时间？",
    "长时间运行的测试命令被工具超时打断怎么办？",
    "重试几次后会停止？Retry-After 会被遵守吗？"
  ],
  "related": [
    "wire-protocol-streaming",
    "cancellation-cleanup",
    "configuration-diagnostics",
    "token-step-concurrency-limits"
  ],
  "verified_ref": "0c47f74a"
}
---

# 超时、重试与请求卡住诊断

OC 把一次模型请求拆成几个阶段，每个阶段单独设超时：建立连接、等第一个流事件、流事件之间的空闲、provider 请求本身。整次任务的期限另用 SDK `timeout=` 设置，结束后的资源清理再用 `cleanup_timeout=` 限制。工具执行也有自己的两层期限。所以"卡住"时先确定停在哪一层，再调那一层。

## 按停顿位置选设置

单位都是秒。先用默认值跑，再按端点表现调整。

| 设置 | 默认值 | 管哪个阶段 |
| --- | --- | --- |
| `OPENCOLLAB_LLM_CONNECT_TIMEOUT` | 30 | 建立连接 |
| `OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT` | 180 | 等第一个流事件 |
| `OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT` | 180 | 相邻两个流事件之间 |
| `OPENCOLLAB_LLM_TIMEOUT` | 600 | provider 请求超时，包括流式传输中的单次 socket 读取 |
| `OPENCOLLAB_LLM_MAX_RETRIES`（`llm_max_retries`） | 3 | 可重试故障的重试次数 |
| `OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET`（`provider_error_time_budget`） | 0 | 失败重试的总时间额度，0 表示只按次数 |

排查顺序：

- 连不上：先查地址和网络，再看连接超时。
- 连上了但迟迟不出第一个 token：调首事件等待。
- 输出到一半长时间停住：查流空闲限制和尾部的完成事件。

这些请求阶段的限制和整次任务期限互相独立。调总期限时，也要看对应阶段的设置。

下面这组可以写进选定的 env 文件。数值按实际端点选，改完重新构造 SDK 客户端。

```dotenv
OPENCOLLAB_LLM_CONNECT_TIMEOUT=30
OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=240
OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=180
OPENCOLLAB_LLM_TIMEOUT=600
OPENCOLLAB_LLM_MAX_RETRIES=3
OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET=120
```

## 给整个任务设期限

下面给一次 Agent 执行设 5 分钟期限，清理另给 10 秒。到期后 SDK 返回 `stopped`，`reason="timeout"`。如果是调用方主动取消，会在资源清理后收到 `asyncio.CancelledError`。

```python
import asyncio
from opencollab import OpenCollab

async def main() -> None:
    client = OpenCollab(".")
    result = await client.agent(
        "阅读项目结构，并给出最小测试步骤。",
        tools="read",
        timeout=300,         # 整次运行期限
        cleanup_timeout=10,  # 结束后框架等待自身资源停止的时间
        budget=100_000,
    )
    print(result.status, result.reason)
    print(result.metrics.get("session_quiesced"))

asyncio.run(main())
```

- 工作流里角色的 `timeout=` 限制该角色，SDK `workflow(timeout=...)` 限制整次工作流。
- `cleanup_timeout` 必须是有限正数。
- 没有 token 或步数上限的运行，这些时间限制依然有效。

## 工具执行卡住时调哪一层

工具有两层期限：

1. Bash 工具的 `timeout` 参数，决定命令本身的执行期限，默认 120 秒。
2. 工具调度器给普通工具设的外层期限。取值顺序是：调用参数 `timeout`，然后工具自己的 `default_timeout`，最后是 `OPENCOLLAB_TOOL_EXECUTION_TIMEOUT`（默认 180 秒）。在此基础上加 10 秒清理余量，再被 `OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT` 截断（默认最多 900 秒）。

派发等待等声明了专用等待规则的工具，沿用各自的控制方式。

例如项目测试要跑 20 分钟：启动前提高外层最大期限，并让 Agent 的 Bash 调用明确用 `timeout=1200`，同时给整个任务留出模型分析、执行命令和交付的时间。下面选可信宿主项目，并明确允许动态子成员在宿主执行命令：

```bash
export OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT=1800
opencollab --workspace /path/to/application \
  --allow-local-child-shell \
  --prompt '运行项目指定测试，Bash timeout 使用 1200 秒，报告实际执行结果。'
```

- 这些工具期限变量在模块导入时读取。在 shell 里导出后，重新运行 Python 或 CLI 才生效。
- 有限正数会被采用。空值、非法数字、零和负数都回到默认值。
- 工具超时后进入取消清理。环境里还有未停止的操作时，环境会被撤销，后续见[停止任务并确认清理完成](cancellation-cleanup.md)。期限计算和撤销行为见[工具执行运行时](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/tool_execution_runtime.py)。

## 重试怎样停止

- `llm_max_retries` 默认 3，指被分类为可重试 provider 故障之后的重试次数。设为 0 关闭这类额外尝试。
- 重试使用退避。服务返回 `Retry-After` 时会考虑它。
- 普通配置错误和上下文溢出走各自的处理路径，不算在这里。看错误分类可以判断请求进了哪条路径。
- `provider_error_time_budget` 默认 0，只按次数控制。设为正数后，失败尝试和重试延迟还要共同受这个时间额度限制。次数或时间任一先到上限，重试就结束。
- 这两项和请求超时一起，决定一次失败调用最多可能占用多久。

工作流里的额度是共享的：同一工作流中的会话、总结和候选共享该工作流的 provider 重试时间额度。用 `create_model_client()` 创建的客户端，被新的 SDK Agent 或 Workflow 运行使用时，会得到该次运行的新额度。

## 超时后记录什么

1. 保留 `status`、`reason` 和 `metrics`，再查轨迹、会话和已有文件。部分输出可能已经保存。
2. 迟到的 provider usage 会在框架清理阶段结算一次。开始或取消事件里 token 为零，只说明当时还没记录到用量。最终用量看完成记录和清理结果。
3. 复查故障时记下：有效协议、四类请求超时、重试次数、失败时间额度、出错的阶段。
4. 用 `agent_failures` 里的类型、HTTP 状态和 `exception_chain` 区分连接、provider 和执行环境问题，再用完整轨迹找到对应调用。

## 来源

[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L94)　[opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/config.py#L199)　[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L34)　[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L121)

## 相关指南

[选择调用协议与流式响应](../models/wire-protocol-streaming.md)　[停止任务并确认清理完成](cancellation-cleanup.md)　[检查生效配置与排查启动故障](configuration-diagnostics.md)　[设置 token 预算、步数与运行并发](token-step-concurrency-limits.md)
