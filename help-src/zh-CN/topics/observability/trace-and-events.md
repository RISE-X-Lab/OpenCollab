---
{
  "id": "trace-and-events",
  "title": "查看 Agent 做了什么",
  "category": "observability",
  "summary": "用轨迹文件事后复盘，或用事件回调实时观察，找出 Agent 调了什么模型、跑了什么工具、为什么停下。",
  "aliases": [
    "JSONL",
    "Tracer",
    "on_event",
    "RunEvent",
    "llm_call",
    "tool_exec",
    "response_session_id",
    "observation_errors",
    "执行过程",
    "日志怎么看",
    "为什么停了"
  ],
  "keywords": [
    "轨迹",
    "运行事件",
    "trace",
    "RunControl"
  ],
  "questions": [
    "通过轨迹和事件定位模型调用、工具执行、交接与停止原因？",
    "怎么实时看到 Agent 的进度？",
    "Agent 中途停了，怎么查原因？"
  ],
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 查看 Agent 做了什么

OC 有两条互相独立的观察通道：持久化轨迹（`trajectory.jsonl` 或 `orchestration.jsonl`，事后分析用），以及 `RunControl(on_event=...)` 的同步回调，实时推送 `RunEvent`（界面、进度、实时决策用）。设置 `trace=False` 仍能收到回调。本文给出开启方式、记录字段、常用事件类型和从停止原因倒查的步骤。

## 第一步：打开轨迹

- 交互 CLI：加 `--trace`。
- Workflow：默认记录编排轨迹，保存目录由运行配置决定。
- SDK：设置 `artifacts` 并保留 trace，跑完后从结果目录找到 JSONL 文件。

## 第二步：读轨迹文件

每行是一条记录，顶层字段如下：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | 时间 |
| `run_id` | 运行标识 |
| `step` | 这份轨迹里的记录序号 |
| `aid`、`role` | 成员标识和角色。调度器自身的记录可以留空 |
| `type` | 事件类型 |
| `payload` | 事件详情 |
| `metrics` | `metrics.tokens` 和 `metrics.latency_s`，对应这一条记录 |

下面脚本统计一份已完成轨迹里每个成员各类事件的数量。把路径换成该运行的 `trajectory.jsonl` 或 `orchestration.jsonl`。

```python
import json
from collections import Counter
from pathlib import Path

trace_path = Path("artifacts/example/trajectory.jsonl")
counts = Counter()
with trace_path.open(encoding="utf-8") as stream:
    for line in stream:
        if line.strip():
            row = json.loads(line)
            counts[(row["run_id"], row.get("aid"), row.get("role"), row["type"])] += 1
for identity, count in counts.items():
    print(identity, count)
```

按时间和会话把事件串起来，就能还原请求、工具调用、通信和停止的先后顺序。

常用事件类型：

| 事件 | 看什么 |
| --- | --- |
| `llm_call_started`、`llm_call`、`llm_call_cancelled` | 一次模型请求的开始、完成、取消。用 payload 里的 `response_session_id` 和 `session_step` 对应起来 |
| `tool_exec` | 实际执行的工具操作 |
| `context_shaping` | 上下文处理，比如压缩有没有真的发生 |
| `message_sent`、`message_refused` | 成员之间的通信决策 |

`llm_call` 完成记录里的 `request_tool_names` 与 `request_tool_choice`，表示应用准备交给模型的工具列表，不等于实际执行了哪些工具。实际执行看 `tool_exec`。要看具体调用内容，继续读 payload 里的输入和结果。

## 第三步：实时订阅事件

`RunControl` 可用于单 Agent 和 `ctx.agent_run`。下面代码在已有的异步函数中执行，`on_event` 是同步回调。

```python
from opencollab import RunControl

events = []
result = await oc.agent(
    "说明当前项目的入口",
    run_control=RunControl(on_event=events.append),  # 每个事件追加到列表
)
for event in events:
    print(event.type, event.aid, event.used_tokens)
```

事件和轨迹文件互不依赖。设置 `trace=False` 也能收到回调。

`RunEvent` 的字段：`type`、`run_id`、`session_id`、`aid`、`used_tokens`、`steps`，以及独立复制的 `data`。

- `used_tokens` 是会话累计值。
- `usage` 事件的 `data["total_tokens"]` 是这一次已记账的用量。

事件适合观察用量、预算决策、上下文处理、错误、停止和清理。工具执行的细节要从轨迹或完整会话读。

回调的注意事项：

1. 回调要尽快同步返回。要写数据库或调网络服务，先把事件转交给队列。
2. 回调出错时，错误保存在 `result.metrics["observation_errors"]`。`ctx.agent_run` 则通过返回值的 `observation_errors` 提供。
3. 运行结束后回调不会立刻停。它会继续接收原运行的事件，直到迟到用量结算完、清理结束，并收到 `cleanup_completed`。

## 从停止原因倒查

1. 先看结果状态。
2. 找到同一运行的 `session_stopped`、错误或清理事件。
3. 检查最后一次模型请求有没有完成。
4. 检查每个工具调用有没有对应回执。
5. 确认环境是否已经停下。

辅助判断：

- `context_shaping` 能看出压缩是否真的发生。
- `usage` 能区分普通生成、摘要和迟到响应。

模型请求刚开始或被取消时，可能还没返回用量。结束后迟到的成功或错误用量仍会补记。所以分析费用时，要把后续清理记录读完。

## 用轨迹支撑结论

根据问题找对应的记录：

- 想看测试计划：读模型输出的文字。
- 想确认测试真的跑了：读工具命令和实际结果。
- 想分析通信：先从团队声明读允许的通信方向，再从发送记录确认实际发了哪些消息。

## 分享日志前注意

日志可能包含任务源码、用户输入和工具输出。发给别人前先检查内容范围，按项目要求处理凭据和私密数据。

## 源码与进一步阅读

[opencollab/adapters/trace.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/trace.py) · [opencollab/application/run_control.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/run_control.py) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md) · [docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/runtime-design.md)
