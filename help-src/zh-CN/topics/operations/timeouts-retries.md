---
{
  "id": "timeouts-retries",
  "title": "运行怎样停下：超时、重试、取消与清理",
  "category": "operations",
  "summary": "运行怎样停下来：模型请求的各段超时、整次任务期限、工具执行期限和重试规则；用 Ctrl+C、task.cancel() 或 timeout 停止后，怎样确认清理已经完成。",
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
    "OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT",
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
    "OPENCOLLAB_LLM_TIMEOUT",
    "OPENCOLLAB_LLM_CONNECT_TIMEOUT",
    "OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT",
    "OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT",
    "OPENCOLLAB_LLM_MAX_RETRIES",
    "OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET",
    "timeout",
    "cleanup_timeout",
    "Ctrl+C",
    "asyncio.CancelledError",
    "session_quiesced",
    "environment_quiesced",
    "cleanup_quiesced",
    "execution_quiesced",
    "workspace_ready"
  ],
  "questions": [
    "模型请求卡住不动该调哪个超时？",
    "怎么给整个任务设置最长运行时间？",
    "长时间运行的测试命令被工具超时打断怎么办？",
    "重试几次后会停止？Retry-After 会被遵守吗？",
    "怎么在终端或 Python 里停止正在运行的任务？",
    "取消之后怎么确认环境和会话已经清理干净？",
    "取消后 token 用量为什么还会变化？",
    "被停止的 Team 成员改动去哪里找回？"
  ],
  "related": [
    "token-step-concurrency-limits",
    "provider-endpoint-credentials",
    "candidate-delivery",
    "service-integration-lifecycle",
    "vm-environment-extension"
  ],
  "verified_ref": "v0.9.6"
}
---

# 运行怎样停下：超时、重试、取消与清理

一次运行“卡住了”或“停下了”，原因可能在很多层：模型请求一直没回、某个工具命令跑太久、整次任务到了期限、你主动按了 Ctrl+C。OC 给每一层都设了单独的期限，停下之后还会跑一段由框架负责的清理。所以排查的第一步永远是：**先找到停在哪一层，再调那一层**。

## 先看全貌

| 层 | 管什么 | 主要设置 | 到期后 |
| --- | --- | --- | --- |
| 模型请求 | 连接、等第一个流事件、流空闲、请求本身 | `llm_connect_timeout`、`llm_first_event_timeout`、`llm_stream_idle_timeout`、`llm_timeout`，以及重试 | 按重试规则重试或报错 |
| 工具执行 | 一条命令或一次工具调用 | Bash 的 `timeout`、`OPENCOLLAB_TOOL_EXECUTION_*` | 取消工具，必要时撤销环境 |
| 单次调用 | Workflow 里的一次 `ctx.agent` / `ctx.agent_run` | 调用参数 `timeout`、`max_steps` | 这次调用停止 |
| 整次运行 | 一次 `agent()`、`team()`、`workflow()` | SDK 的 `timeout=` | 返回 `stopped`，`reason="timeout"` |
| 主动停止 | 你决定停 | Ctrl+C、`task.cancel()` | 清理后抛出 `asyncio.CancelledError` |
| 清理 | 停止之后的收尾 | `cleanup_timeout` | 用 `metrics` 里的清理字段确认 |

这些期限互相独立。没有 token 或步数上限的运行，时间限制依然有效；调大整次任务期限时，也要看对应阶段的设置。

## 模型请求：按卡住的位置选超时

单位都是秒。先用默认值跑，再按端点的表现调整。

| 设置 | 默认值 | 管哪个阶段 |
| --- | --- | --- |
| `OPENCOLLAB_LLM_CONNECT_TIMEOUT` | 30 | 建立连接 |
| `OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT` | 180 | 已连接，等第一个流事件 |
| `OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT` | 180 | 收到部分内容后，相邻两个流事件之间 |
| `OPENCOLLAB_LLM_TIMEOUT` | 600 | provider 请求超时，包括流式传输中的单次 socket 读取 |
| `OPENCOLLAB_LLM_MAX_RETRIES`（`llm_max_retries`） | 3 | 可重试故障的重试次数 |
| `OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET`（`provider_error_time_budget`） | 0 | 失败重试的总时间额度，0 表示只按次数 |

排查顺序：

- 连不上：先查地址和网络，再看连接超时。
- 连上了，但迟迟不出第一个 token：调首事件等待。
- 输出到一半长时间停住：查流空闲限制和尾部的完成事件（打开了流式接收时，见 [开启 Chat 流式接收](../models/provider-endpoint-credentials.md#开启-chat-流式接收)）。

下面这组可以写进选定的 env 文件，数值按实际端点选，改完要重新创建 SDK 客户端：

```dotenv
OPENCOLLAB_LLM_CONNECT_TIMEOUT=30
OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=240
OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=180
OPENCOLLAB_LLM_TIMEOUT=600
OPENCOLLAB_LLM_MAX_RETRIES=3
OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET=120
```

## 重试怎样停止

- `llm_max_retries` 默认 3，指被分类为**可重试**的 provider 故障之后的重试次数。设为 0 关闭这类额外尝试。
- 重试使用退避；服务返回 `Retry-After` 时会考虑它。
- 普通配置错误和上下文溢出走各自的处理路径，不算在这里（上下文溢出见 [遇到溢出时看什么](context-compaction.md#遇到溢出时看什么)）。看错误分类可以判断请求进了哪条路径，离线查分类的方法见 [离线查看模型能力](../models/configuration-precedence.md#离线查看模型能力)。
- `provider_error_time_budget` 默认 0，只按次数控制。设为正数后，失败尝试和重试延迟还要共同受这个时间额度限制，次数或时间任一先到上限，重试就结束。
- 这两项和请求超时一起，决定一次失败的调用最多可能占用多久。

同一个 Workflow 里的会话、摘要和候选，共享该 Workflow 的 provider 重试时间额度。用 `create_model_client()` 创建的客户端，被新的 SDK Agent 或 Workflow 运行使用时，会得到该次运行的新额度。

这是**传输层**的重试，只重发模型请求。业务步骤要不要重做，由你的 Workflow 代码决定，见 [给能重复的步骤加有限重试](../workflow/workflow-failure-recovery-lifecycle.md#给能重复的步骤加有限重试)。

## 给整次任务设期限

下面给一次 Agent 执行设 5 分钟期限，清理另给 10 秒。到期后 SDK 返回 `stopped`、`reason="timeout"`：

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

Workflow 里，角色调用的 `timeout=` 限制该角色，`workflow(timeout=...)` 限制整次 Workflow。`cleanup_timeout` 必须是有限正数。

## 工具执行卡住时调哪一层

工具有两层期限：

1. **Bash 工具的 `timeout` 参数**：命令本身的执行期限，默认 120 秒。
2. **调度器给普通工具设的外层期限**：取值顺序是调用参数 `timeout`、工具自己的 `default_timeout`、`OPENCOLLAB_TOOL_EXECUTION_TIMEOUT`（默认 180 秒）；在此基础上加 10 秒清理余量，再被 `OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT` 截断（默认最多 900 秒）。

派发等待这类声明了专用等待规则的工具，沿用各自的控制方式。

例如项目测试要跑 20 分钟：启动前提高外层最大期限，让 Agent 的 Bash 调用明确用 `timeout=1200`，同时给整个任务留出模型分析、执行命令和交付的时间。下面选可信的本机项目，并明确允许动态子成员在本机执行命令：

```bash
export OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT=1800
opencollab --workspace /path/to/application \
  --allow-local-child-shell \
  --prompt '运行项目指定测试，Bash timeout 使用 1200 秒，报告实际执行结果。'
```

- 这些工具期限变量在模块导入时读取，在 shell 里导出后要重新运行 Python 或 CLI 才生效。
- 有限正数会被采用；空值、非法数字、零和负数都回到默认值。
- 工具超时后进入取消清理；环境里还有未停止的操作时，环境会被撤销，见下文的 [工具取消的等待顺序](#工具取消的等待顺序)。

## 主动停止：Ctrl+C 与 `task.cancel()`

| 入口 | 做法 | 结果 |
| --- | --- | --- |
| 终端 | 运行中按 Ctrl+C | 中断当前轮次，清空排队的输入，拒绝待回答的问题；保留已生成的部分回答并显示原因 |
| 终端 | 空闲时按 Ctrl+C，或 `/exit`、`/quit` | 退出 |
| SDK | 传 `timeout=` | 到期返回 `status="stopped"`、`reason="timeout"` |
| SDK | `task.cancel()` | 框架清理阶段结束后，再向外抛出 `asyncio.CancelledError` |

下面是应用主动取消的完整例子，实际界面里可以把 `task.cancel()` 接到停止按钮：

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

`on_event` 接收器应尽快同步返回，可以先把事件存下来，再交给应用的其他部分处理。

## 停止之后：确认清理完成

和多数框架不同，OC 停止后会先跑一段由框架负责的清理：取消工具协程、中止它创建的执行环境、结算迟到的用量。清理结果写进 `result.metrics`，**停下之后别急着复用工作区，先看这些字段**：

| 字段 | 含义 |
| --- | --- |
| `session_quiesced` | 会话和持久化阶段已确认结束 |
| `environment_quiesced` | 框架自己创建的执行环境已停止 |
| `cleanup_quiesced`、`execution_quiesced` | 汇总上面两项 |

执行环境是调用方传进来的时候，OC 不替你管理它，`environment_quiesced`、`cleanup_quiesced`、`execution_quiesced` 都是 `None`，要你在清理完环境后自己检查。在 Workflow 里，`ctx.agent_run` 的返回值直接给出 `cleanup_complete` 和 `workspace_ready`，用来决定共享上下文能否继续。

用量和部分改动：

- 取消时界面上显示的 token 统计可能还会变：迟到的 provider 成功或错误用量会在清理阶段结算一次。`usage` 事件的 `late` 和 `purpose` 字段可以识别这笔用量的来源；`cleanup_completed` 事件表示清理已结束。最终花费以保存的用量记录为准。
- 失败或停止的 Team 子成员仍可交回已有改动。补丁捕获失败或消息装不下完整补丁时，原生工作树会保留完整文件并报告恢复位置，先导出再释放，见 [找回改动](../isolation/candidate-delivery.md#出了问题先找回改动再清理)。

开始下一次任务之前：确认会话清理结果（`session_quiesced` 等）→ 确认你自己负责的环境已停止 → 检查工作区差异。

### 清理等待时间的默认值

| 情况 | 清理等待时间 |
| --- | --- |
| SDK 调用或子运行 `launch` 显式传了值 | 用显式值 |
| 配置了 VM（`config["vm"]` 或 `OPENCOLLAB_VM`）的 Team、Workflow、CLI 或成员自编子运行 | `vm.cleanup_timeout`，默认 60 秒 |
| 没配置 VM 的 Team | 10 秒 |
| 没配置 VM 的 Workflow | 2 秒 |
| `agent()` | 2 秒 |

VM 清理比本地慢，所以单独用 `vm.cleanup_timeout`。OC 清理一台 VM 时依次停止还在运行的命令、把改动和产物导出到控制端、运行你配置的重置命令、释放这台 VM 的租约。某一步失败时会保留租约和现场，对同一个对象再调用一次清理即可重试，见 [VM 断线与租约恢复](../isolation/candidate-delivery.md#vm-断线与租约恢复)。

### 工具取消的等待顺序

工具超时或调用方取消后，运行时按下面的顺序处理：

1. 请求工具协程取消，等待 `OPENCOLLAB_TOOL_CANCELLATION_CLEANUP_TIMEOUT`（默认 1 秒）。
2. 工具仍未停止或清理报错时，撤销环境并执行环境中止，等待 `OPENCOLLAB_TOOL_ENVIRONMENT_ABORT_TIMEOUT`（默认 1 秒）。
3. 中止仍未完成，就取消中止协程，再等待 `OPENCOLLAB_TOOL_CANCELLATION_FORCE_TIMEOUT`（默认 0.5 秒）。
4. 仍在收尾的任务继续由该运行追踪，工具结果会带出清理失败的信息。

这三个进程变量在模块导入时读取，要调整就在启动 Python 或 CLI 之前导出有限正数。单 Agent 或 `ctx.agent_run` 里的 `RunControl.tool_cancellation_cleanup_timeout` 可以只覆盖本次会话的第 1 段等待。SDK 的 `cleanup_timeout` 管的是运行结束后的资源清理。

## 超时之后记录什么

1. 保留 `status`、`reason` 和 `metrics`，再查轨迹、会话和已有文件，部分输出可能已经保存。
2. 迟到的 provider 用量会在清理阶段结算一次。开始或取消事件里 token 为零，只说明当时还没记到用量；最终用量看完成记录和清理结果。
3. 复查故障时记下：生效的协议、四类请求超时、重试次数、失败时间额度、出错的阶段。
4. 用 `agent_failures` 里的类型、HTTP 状态和 `exception_chain` 区分连接、provider 和执行环境的问题，再用完整轨迹找到对应的调用，见 [运行记录](../observability/artifacts-and-history.md)。

## 延伸阅读

- 源码依据：[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/configs/README.md) · [opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/config.py) · [工具执行运行时](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/tool_execution_runtime.py) · [opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/cli/main.py) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/evaluation-runtime.md)
- 相关指南：[预算、步数与并发](token-step-concurrency-limits.md) · [接入模型服务](../models/provider-endpoint-credentials.md) · [隔离改动怎样回到项目](../isolation/candidate-delivery.md) · [把 OC 接进自己的服务](../architecture/service-integration-lifecycle.md)
