---
{
  "id": "runtime-execution-model",
  "title": "原理：一次运行在内部怎样推进",
  "category": "architecture",
  "summary": "一次任务在内部怎样推进：模型请求、工具调用、结果回填的循环，会话（Session）里存了什么、经过哪些阶段、为什么停下，以及恢复后从哪里继续。",
  "aliases": [
    "Harness",
    "Agent loop",
    "SessionRunUseCase",
    "ToolExecutionUseCase",
    "tool_calls",
    "tool_choice",
    "ContextBuilder",
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
    "执行原理",
    "运行时",
    "runtime",
    "工具调用",
    "执行流程",
    "Session",
    "会话状态",
    "恢复执行",
    "会话快照",
    "停止原因"
  ],
  "questions": [
    "理解模型、Session、调度器和执行环境怎样合作，并找到实际行为由谁控制？",
    "模型是怎么真正读文件、跑命令的？",
    "Agent 能做什么由谁决定？",
    "任务什么时候算结束？",
    "读懂会话状态、停止原因和恢复方式，判断继续运行会从哪里开始？",
    "会话快照里有哪些内容？",
    "任务停了以后还能接着跑吗？",
    "两次 agent 调用会共享对话吗？"
  ],
  "related": [
    "concepts-and-modes",
    "session-save-resume",
    "context-compaction",
    "tool-selection",
    "extension-and-evaluation-boundaries"
  ],
  "verified_ref": "0c47f74a"
}
---

# 原理：一次运行在内部怎样推进

这篇讲 OC 的内部机制，帮你在遇到“为什么会这样”时知道该去哪里找答案。核心只有两样东西：一个**循环**（模型请求 → 工具调用 → 结果回填），和一个记录循环状态的**会话（Session）**。Agent、Team、Workflow 三种入口，都是围绕会话来执行模型和工具的交互。

## 一次任务的完整路径

1. 组装配置、会话和执行环境。
2. 把消息和工具说明发给模型。
3. 模型返回普通回答，或者返回工具调用。
4. 如果是工具调用：解析参数、校验、检查权限，然后执行工具。
5. 工具结果放回会话，进入下一次模型请求，回到第 2 步。
6. 模型给出最终回答，或触发限制时结束。

和常见实现相比，OC 在第 4 步多做了几件事：按 schema 校验参数，只在**当前角色的工具集合**里查找实现，执行前做权限检查，工具真正跑在你指定的执行环境里（不一定是本机）。

## 动手看一次：只读分析 README

```python
import asyncio
from opencollab import OpenCollab
from opencollab.tools import builtin_tools

async def main():
    result = await OpenCollab(".").agent(
        "读取 README.md，概括项目目标，并引用文件中的一段短句。",
        tools=builtin_tools("file_read"),  # 只给读取工具
        budget=30_000,
        artifacts="artifacts/readme-inspection-001",  # 需要新建或为空
    )
    print(result.status, result.reason)
    print(result.output)
    print(result.artifacts)

asyncio.run(main())
```

运行后，打开产物目录里的轨迹，按顺序检查：模型请求里提供了 `file_read`；找到对应的工具调用和实际返回的片段；对照最终回答里的引用，确认它来自这个片段。这样就能看到“模型选工具、读内容、组织回答”的全过程。项目里没有 README.md 时，读取结果会返回一个模型能看到的错误。轨迹的读法见 [运行记录](../observability/artifacts-and-history.md#读轨迹文件)。

## 发给模型的内容由什么组成

上下文按顺序由几段拼起来：

1. 角色用了 Agent 配置（profile）时，先加它的基础提示词。
2. 再加角色职责。
3. 团队信息、技能目录和项目概览，满足条件时才加入。
4. 派发来的任务，作为开头的用户内容。

工具 schema 单独传给模型服务，不拼进提示词。通用的外部记忆来源（比如 RAG）需要你自己接入。

会话里保存的是**完整记录**，用于追踪和恢复；发给模型的请求是经过长度整理的版本。两者用途不同，见 [上下文窗口与压缩](../operations/context-compaction.md)。

## 模型怎样发起真实操作

收到工具调用后，运行时依次：

1. 解析工具名称和 JSON 参数。
2. 按工具 schema 校验参数。
3. 在当前角色可用的工具里找到对应实现。
4. 执行，并传入带 Agent 身份和环境的运行信息：文件工具读取对应的工作区，`bash` 在对应环境里启动进程。

工具输出和原来的工具调用配对保存，再进入后续请求。

### Agent 能做什么，由谁决定

| 控制点 | 决定什么 |
| --- | --- |
| 提示词 | 说明任务和约束 |
| `tool_choice` | 引导本次请求选择工具 |
| 配置的工具集合 | 能调用哪些能力 |
| 运行时权限检查 | 操作是否允许 |
| 执行环境 | 文件、进程和网络的实际范围 |

只读分析任务，直接给读取工具就够了。工具和权限的细节见 [内置工具](../extensions/tool-selection.md)。

## Session 里存了什么

| 字段 | 含义 |
| --- | --- |
| `SessionState.messages` | 完整对话：用户输入、模型回复、工具调用与结果 |
| `used_tokens` | 累计的输入加输出 token |
| `step_count` | 会话累计步数 |
| `context_tokens` | 最近一次请求的输入大小 |
| 阶段与 `terminal_reason` | 现在做到哪一步，以及结束原因 |

另外还保存：待处理事件表（把延迟的工具调用和它的结果对应起来）、排队消息（调度器已经排好、等着加入模型请求的用户消息），以及新一轮外部用户输入从收到到开始执行的状态。这样保存、等待和恢复时，消息顺序不会乱。

用公开接口读一份快照，先看看保存了什么：

```python
from opencollab import OpenCollab

# 把路径换成你实际的会话快照
snapshot = OpenCollab.read_session_snapshot("/path/to/session.json")
print(sorted(snapshot))
```

这个接口读取快照和已经完成的日志条目。终端里的保存和恢复命令见 [保存与恢复会话](../operations/session-save-resume.md)。

## 一轮是怎么推进的：会话的阶段

会话是一个显式的状态机：

| 阶段 | 正在做什么 |
| --- | --- |
| `IDLE` | 等待开始或恢复执行 |
| `PRECHECK` | 检查取消、预算、步数与运行条件 |
| `CALLING_LLM` | 等待模型响应 |
| `HANDLING_RESPONSE` | 处理回复及工具调用 |
| `EXECUTING_TOOLS` | 执行选中的工具 |
| `AWAITING_EVENTS` | 等待延迟的子任务结果 |
| `AUTOSAVING` | 保存本步状态，再继续或结束 |

几种走法：

- 普通工具执行完：先保存，再回到 `PRECHECK`。
- 派发工具产生延迟结果：进入 `AWAITING_EVENTS`，整批结果到齐并保存后再继续。这就是 Team 里 `spawn_agent` “自动等待”的来源，见 [Team 协作](../team/dispatch-team-agents.md#派发之后会发生什么)。
- 成员之间的消息交接，在接收方之后的回合里作为用户消息加入。
- 某一步包含 `submit`：先保存本步，再结束当前回合。

结束时会话进入三种状态之一，SDK 再把它整理成运行结果：

| 状态 | 什么时候 | 保存了什么 |
| --- | --- | --- |
| `DONE` | 模型给出最终回答 | 最终回答 |
| `STOPPED` | 受预算、步数或其他运行限制影响 | 停止原因和已有进展 |
| `ERROR` | 出现未处理的故障 | 故障信息 |

已经结束当前回合的成员，收到新消息后可以重新执行；Team 要整体结束，还得等所有活动成员和待投递的消息都处理完。验收任务时，请检查实际文件、命令结果或外部系统状态，不要只看状态，见 [判断任务是否真的完成](../observability/run-results.md)。

## 新一轮输入、新调用和进程重启

**同一个会话收到新的用户输入**时，会重置本轮的循环检测、进度信号、临时工具选择等，但保留累计额度（token 与步数）。

**不同调用之间默认不共享对话**：每次独立的 SDK `agent()` 调用通常会创建新会话；Workflow 里多次 `ctx.agent` 也各自创建对话。想让后面的调用用上前面的结论，就把输出显式传进去，或者共享需要的文件。

**从保存记录恢复**时：累计用量、结束状态、可恢复的等待关系会保留；正在进行的模型请求和工具执行不会保留（它们是原进程里的协程，已经结束），恢复会从 `IDLE` 重新进入运行；排队消息、工具结果配对和延迟用量按保存的内容继续处理。

## 延伸阅读

- 源码依据：[会话状态](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/domain/session.py) · [会话执行](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/session_run.py) · [工具执行](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/tool_execution.py) · [运行设计](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/runtime-design.md)
- 相关指南：[OC 是什么](../orientation/concepts-and-modes.md) · [保存与恢复会话](../operations/session-save-resume.md) · [上下文窗口与压缩](../operations/context-compaction.md) · [OC 的边界：需求该从哪里扩展](extension-and-evaluation-boundaries.md)
