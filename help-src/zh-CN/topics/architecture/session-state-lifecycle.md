---
{
  "id": "session-state-lifecycle",
  "title": "Session 怎样保存进度和继续执行",
  "category": "architecture",
  "summary": "看懂会话里存了什么、处在哪个阶段、为什么停下，以及恢复后会从哪里接着跑。",
  "aliases": [
    "状态机",
    "SessionState",
    "SessionPhase",
    "DONE",
    "STOPPED",
    "ERROR",
    "AWAITING_EVENTS",
    "read_session_snapshot"
  ],
  "keywords": [
    "Session",
    "会话状态",
    "恢复执行",
    "会话快照",
    "停止原因"
  ],
  "questions": [
    "读懂会话状态、停止原因和恢复方式，判断继续运行会从哪里开始？",
    "会话快照里有哪些内容？",
    "任务停了以后还能接着跑吗？",
    "两次 agent 调用会共享对话吗？"
  ],
  "related": [
    "runtime-execution-model",
    "service-integration-lifecycle",
    "workspace-recovery"
  ],
  "verified_ref": "0c47f74a"
}
---

# Session 怎样保存进度和继续执行

Session 是 OC 里一次 Agent 对话的状态对象：完整消息、累计用量、当前阶段（一个显式状态机）和待处理事件。它可以落盘，也能从快照恢复。派发子成员时它停在 `AWAITING_EVENTS`，整批结果到齐再继续。新用户轮次会重启本轮循环，但 token 和步数按整个会话累计。这篇讲怎么读快照、看懂阶段和停止原因、恢复后从哪里继续。

## 先动手：读一份会话快照

```python
from opencollab import OpenCollab

# 把路径换成你实际的会话快照
snapshot = OpenCollab.read_session_snapshot("/path/to/session.json")
print(sorted(snapshot))
```

这个接口读取快照和已经完成的日志条目。适合先看看保存了什么，再决定怎么恢复。终端里的保存和恢复命令见 [保存会话与恢复历史](../operations/session-save-resume.md)。

## 会话里保存了什么

| 字段 | 含义 |
| --- | --- |
| `SessionState.messages` | 完整对话：用户输入、模型回复、工具调用与结果 |
| `used_tokens` | 累计的输入加输出 token |
| `step_count` | 会话累计步数 |
| `context_tokens` | 最近一次请求的输入大小 |
| 阶段与 `terminal_reason` | 现在做到哪一步，以及结束原因 |

另外还保存：

- 待处理事件表：把延迟的工具调用和它的结果对应起来。
- 排队消息：调度器已经排好、等着加入模型请求的用户消息，单独保存。
- 新一轮外部用户输入：从收到到开始执行的状态也会记录。这样保存、等待和恢复时，消息顺序不会乱。

## 一轮是怎么推进的

| 阶段 | 正在做什么 |
| --- | --- |
| `IDLE` | 等待开始或恢复执行 |
| `PRECHECK` | 检查取消、预算、步数与运行条件 |
| `CALLING_LLM` | 等待模型响应 |
| `HANDLING_RESPONSE` | 处理回复及工具调用 |
| `EXECUTING_TOOLS` | 执行已选工具 |
| `AWAITING_EVENTS` | 等待延迟子任务结果 |
| `AUTOSAVING` | 保存本步状态，再继续或结束 |

几种走法：

- 普通工具执行完：先保存，再回到 `PRECHECK`。
- 派发工具产生延迟结果：进入 `AWAITING_EVENTS`。整批结果到齐并保存后再继续。
- 某一步包含 `submit`：先保存本步，再结束当前回合。

## 结束时的三种状态

| 状态 | 保存了什么 |
| --- | --- |
| `DONE` | 最终回答 |
| `STOPPED` | 停止原因和已有进展 |
| `ERROR` | 未处理的故障 |

已经结束当前回合的成员，收到新消息后可以重新执行。Team 要整体结束，还得等活动成员和待投递消息都处理完，详见 [等待队友结果并读懂团队状态](../team/wait-for-team-results.md)。

## 新一轮输入和进程重启

同一个 Session 收到新的用户输入时：

- 会重置：本轮的循环检测、进度信号、临时工具选择等。
- 会保留：累计额度（token 与步数）。

注意，不同调用之间默认不共享对话：

- 每次独立的 SDK `agent` 调用通常会创建新会话。
- Workflow 里多次 `ctx.agent` 也各自创建对话。
- 想让后面的调用用上前面的结论，就把输出显式传进去，或者共享需要的文件。

从保存记录恢复时：

- 保留：累计用量、结束状态、可恢复的等待关系。
- 不保留：正在进行的模型请求和工具执行（进程里的协程已经结束）。恢复会从 `IDLE` 重新进入运行过程。
- 排队消息、工具结果配对和延迟用量，会按保存的内容继续处理。

## 进阶：完整记录和发给模型的内容不一样

完整消息用于追踪和恢复。发给模型的版本会在上下文太长时被裁剪和压缩。

当前的上下文来源包括 profile、角色、团队、技能、派发任务和项目概览。通用的外部记忆来源需要另外接入。

## 参考

- [会话状态实现](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/domain/session.py)
- 持久化与上下文的关系：[运行设计](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/runtime-design.md)
