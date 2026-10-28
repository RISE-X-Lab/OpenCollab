---
{
  "id": "artifacts-and-history",
  "title": "运行记录：产物目录、轨迹与实时事件",
  "category": "observability",
  "summary": "一次运行留下哪些记录：产物目录里的会话、清单和轨迹各是什么，怎样用 run_id 串起来、用实时事件观察运行，以及哪些文件可以删。",
  "aliases": [
    "artifacts",
    "会话文件",
    "日志目录",
    "workflow.json",
    "team.json",
    "no-save",
    "no-trace",
    "运行记录在哪",
    "产物文件夹",
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
    "运行文件",
    "产物目录",
    "记录在哪里",
    "trajectory.jsonl",
    "orchestration.jsonl",
    "轨迹",
    "运行事件",
    "trace",
    "RunControl"
  ],
  "questions": [
    "运行结束后定位对应目录，区分产物、原始会话、轨迹和可再生缓存？",
    "Agent 跑完了，它的对话记录和日志存在哪里？",
    "哪些运行文件可以删，哪些要留着？",
    "通过轨迹和事件定位模型调用、工具执行、交接与停止原因？",
    "怎么实时看到 Agent 的进度？",
    "Agent 中途停了，怎么查原因？"
  ],
  "related": [
    "run-results",
    "token-cache-cost",
    "session-save-resume",
    "collaboration-adherence"
  ],
  "verified_ref": "v0.9.6"
}
---

# 运行记录：产物目录、轨迹与实时事件

想知道一次运行里 Agent 到底做了什么、为什么停下、改动去了哪里，要靠运行记录。OC 有两条互相独立的观察通道：

| 通道 | 是什么 | 适合 |
| --- | --- | --- |
| 产物目录 | 写在磁盘上的会话快照、清单和轨迹文件 | 事后复盘、恢复、归档 |
| 实时事件 | `RunControl(on_event=...)` 同步推送的 `RunEvent` | 界面进度、实时决策、用量统计 |

关掉轨迹（`trace=False`）照样能收到实时事件。这篇先讲产物目录里有什么、怎样用 `run_id` 串起来，再讲怎样读轨迹、订阅事件，以及从停止原因倒查。

## 产物目录里有什么

SDK 用 `artifacts=` 指定本次运行的保存目录，**必须是新目录或空目录**：

```python
# 放在异步函数中运行
result = await oc.agent(
    "阅读项目并给出修改建议",
    artifacts="artifacts/review-parser",
)
print(result.artifacts)  # 实际保存位置
```

按任务标识给每次运行分配独立目录，避免两个运行写同一份结果；运行结束后以 `RunResult.artifacts` 返回的位置为准。想重复使用某个目录，先处理里面已有的内容，保留正式结果和待恢复材料。

| 入口 | 常见文件 | 用来看什么 |
| --- | --- | --- |
| SDK Agent | `agent.json`；启用轨迹时有 `trajectory.jsonl` | 单个会话的快照与执行事件 |
| Team | `team.json`、`agent_<aid>_<role>.json`；SDK 启用轨迹时有 `trajectory.jsonl` | 实际成员名册、每个成员的会话、协作事件 |
| Workflow | `workflow.json`、`<seq>_<role>.json`；启用轨迹时有 `orchestration.jsonl` | 入口与参数、每个步骤的会话、编排事件 |

文件名里的角色或步骤标签会转换成适合做文件名的形式。完整会话可能另有一份持久化日志，读取时用公开的 `OpenCollab.read_session_snapshot(path)`，它会把快照和已完成的日志条目合并起来，见 [用 SDK 离线读取](../operations/session-save-resume.md#用-sdk-离线读取)。终端团队的自动保存位置也在那篇。

### 保存开关

| 场景 | 做法 |
| --- | --- |
| Workflow 默认 | 保存会话，并记录编排轨迹 |
| CLI 不要轨迹 | `--no-trace`：仍保留会话与 Workflow 清单 |
| CLI 基本不保存 | `--no-save`：关闭通常的运行产物 |
| 交互 CLI 要轨迹 | `--trace` |
| 交互 CLI 保存主会话 | `/save` |

隔离入口或候选交付失败时，恢复机制会按需单独保留候选，这和轨迹开关是两回事。恢复目录在哪里，以结果和错误信息里给出的实际路径为准。

### 用 `run_id` 串起记录

1. 先找到这次运行的 `run_id`（`RunResult.metrics["run_id"]`）。
2. 再按 Agent 或会话标识，把模型调用和工具回执对上。
3. 同一个角色跑了多次时，用会话标识区分每一次；角色名只说明它负责什么。

Team 清单保留所选的团队文件和实际成员，完整的通信拓扑要从对应 YAML 读取；Workflow 清单描述 Workflow 本身和它的执行记录；成员启动的子 Workflow 的产物归入父运行，同时保留子运行自己的身份。

### 记录缺失时先看什么

`workflow.json` 里除了运行状态、原因、已用 token 和预算，还有：

| 字段 | 说明 |
| --- | --- |
| `evidence_complete` | 运行记录与清理是否完整 |
| `trace_enabled` | 是否开启了轨迹 |
| `tracer_write_error` | 轨迹写入出错的信息 |
| `tracer_dropped_steps` | 轨迹丢掉的记录 |
| `tracer_failure` | 轨迹保存失败的情况 |

发现记录缺了，先读这几个字段，再去查对应的会话和原始工具结果。注意 `evidence_complete` 只说明记录和清理是否完整，不代表项目做对了；验收要另外读实际测试或验证器的结果，见 [判断任务是否真的完成](run-results.md)。

## 读轨迹文件

打开轨迹：交互 CLI 加 `--trace`；Workflow 默认记录；SDK 设置 `artifacts` 并保留 `trace`，跑完后从结果目录找到 JSONL 文件。

每行是一条记录，顶层字段：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | 时间 |
| `run_id` | 运行标识 |
| `step` | 这份轨迹里的记录序号 |
| `aid`、`role` | 成员编号和角色；调度器自身的记录可以留空 |
| `type` | 事件类型 |
| `payload` | 事件详情 |
| `metrics` | `metrics.tokens` 和 `metrics.latency_s`，对应这一条记录 |

下面的脚本统计一份已完成的轨迹里，每个成员各类事件的数量（路径换成该运行的 `trajectory.jsonl` 或 `orchestration.jsonl`）：

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

按时间和会话把事件串起来，就能还原请求、工具调用、通信和停止的先后顺序。常用事件类型：

| 事件 | 看什么 |
| --- | --- |
| `llm_call_started`、`llm_call`、`llm_call_cancelled` | 一次模型请求的开始、完成、取消；用 payload 里的 `response_session_id` 和 `session_step` 对应起来 |
| `tool_exec` | 实际执行的工具操作 |
| `context_shaping` | 上下文整理，比如压缩有没有真的发生 |
| `message_sent`、`message_refused` | 成员之间的通信决策 |

`llm_call` 记录里的 `request_tool_names` 与 `request_tool_choice`，表示准备交给模型的工具列表，**不等于实际执行了哪些工具**；实际执行看 `tool_exec`，具体内容继续读 payload 里的输入和结果。

## 实时订阅事件

`RunControl` 可用于单 Agent 和 `ctx.agent_run`。下面的代码在已有的异步函数中执行，`on_event` 是同步回调：

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

`RunEvent` 的字段：`type`、`run_id`、`session_id`、`aid`、`used_tokens`、`steps`，以及独立复制的 `data`。`used_tokens` 是会话的累计值；`usage` 事件的 `data["total_tokens"]` 是这一次已记账的用量。事件适合观察用量、预算决策、上下文整理、错误、停止和清理；工具执行的细节要从轨迹或完整会话读。

回调的注意事项：

1. 回调要尽快同步返回。要写数据库或调网络服务，先把事件转交给队列。
2. 回调出错时，错误保存在 `result.metrics["observation_errors"]`；`ctx.agent_run` 则在返回值的 `observation_errors` 里提供。
3. 运行结束后回调不会立刻停：它会继续接收原运行的事件，直到迟到的用量结算完、清理结束，并收到 `cleanup_completed`。

## 从停止原因倒查

1. 先看结果状态。
2. 找到同一运行的 `session_stopped`、错误或清理事件。
3. 检查最后一次模型请求有没有完成。
4. 检查每个工具调用有没有对应的回执。
5. 确认环境是否已经停下。

`context_shaping` 能看出压缩是否真的发生；`usage` 能区分普通生成、摘要和迟到的响应。模型请求刚开始或被取消时可能还没返回用量，迟到的成功或错误用量之后仍会补记，所以分析费用时要把后续的清理记录读完，见 [用量与费用](token-cache-cost.md)。

用轨迹支撑结论时，按问题找对应的记录：想看测试计划，读模型输出的文字；想确认测试真的跑了，读工具命令和实际结果；想分析通信，先从团队声明读允许的通信方向，再从发送记录确认实际发了哪些消息。

## 哪些该留，哪些能删

值得保留（恢复和复核要用）：源码、候选补丁、正式测试结果、你的结论所依赖的原始记录。通常可以重新生成：`.pytest_cache`、`.ruff_cache`、有对应源码的字节码、能从锁文件重建的旧依赖环境。

清理之前按顺序确认：运行已经结束 → 目录没有被程序占用 → 只删明确指定的路径 → 隔离资源还处于保留状态时，先导出恢复补丁或做完必要核对，见 [找回改动](../isolation/candidate-delivery.md#出了问题先找回改动再清理)。

日志可能包含任务源码、用户输入和工具输出。发给别人之前先检查内容范围，按项目要求处理凭据和私密数据。

## 延伸阅读

- 源码依据：[opencollab/adapters/trace.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/trace.py) · [opencollab/application/run_control.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/run_control.py) · [opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/sdk/result.py) · [opencollab/bootstrap/workflow_runtime.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/workflow_runtime.py) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/evaluation-runtime.md) · [docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/runtime-design.md)
- 相关指南：[判断任务是否真的完成](run-results.md) · [用量与费用](token-cache-cost.md) · [保存与恢复会话](../operations/session-save-resume.md) · [检查团队是否真的按分工协作](collaboration-adherence.md)
