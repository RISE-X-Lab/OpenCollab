---
{
  "id": "runtime-execution-model",
  "title": "一次任务是怎么跑起来的",
  "category": "architecture",
  "summary": "跟着一次任务走一遍：OpenCollab 怎样把消息发给模型、执行模型要求的工具，并把结果送回模型，直到任务结束。",
  "aliases": [
    "Harness",
    "Agent loop",
    "SessionRunUseCase",
    "ToolExecutionUseCase",
    "tool_calls",
    "tool_choice",
    "ContextBuilder"
  ],
  "keywords": [
    "执行原理",
    "运行时",
    "runtime",
    "工具调用",
    "执行流程"
  ],
  "questions": [
    "理解模型、Session、调度器和执行环境怎样合作，并找到实际行为由谁控制？",
    "模型是怎么真正读文件、跑命令的？",
    "Agent 能做什么由谁决定？",
    "任务什么时候算结束？"
  ],
  "related": [
    "session-state-lifecycle",
    "extension-and-evaluation-boundaries",
    "shell-permissions-execution",
    "candidate-delivery"
  ],
  "verified_ref": "0c47f74a"
}
---

# 一次任务是怎么跑起来的

OC 的 agent loop 是标准的"模型请求 → 工具调用 → 结果回填"循环，状态都放在 Session 里。和常见实现相比，OC 多做了几件事：按 schema 校验参数，只在当前角色的工具集合里查实现，执行前做权限检查，工具真正跑在你指定的 environment 里。这篇讲一次任务的完整路径，以及某个行为该去哪里调整。

## 整体流程

1. 组装配置、会话（Session）和执行环境。
2. 把消息和工具说明发给模型。
3. 模型返回普通回答，或者返回工具调用。
4. 如果是工具调用：校验参数、检查权限、执行工具。
5. 工具结果放回会话，进入下一次模型请求，回到第 2 步。
6. 模型给出最终回答，或触发限制时结束。

OC 名词：

- Session：一次 Agent 对话的状态对象，持有消息、计数和阶段，可保存和恢复。见 [Session 怎样保存进度和继续执行](session-state-lifecycle.md)。
- environment：工具操作的真实工作区，比如本机目录或容器。文件工具和 Bash 都在这里执行，不一定是宿主机。

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

运行后，打开产物目录里的轨迹，按顺序检查：

1. 模型请求里提供了 `file_read`。
2. 找到对应的工具调用和实际返回的片段。
3. 对照最终回答里的引用，确认来自这个片段。

这样就能看到"模型选工具、读内容、组织回答"的全过程。如果项目里没有 README.md，读取结果会返回一个模型能看到的错误。

## 三种入口

| 入口 | 适合 |
| --- | --- |
| `OpenCollab.agent` | 运行单个 Agent |
| `team` | 建立多个成员和调度关系 |
| `workflow` | 运行你自己定义的 Python 编排 |

三种入口都是围绕 Session 执行模型和工具的交互。

## 发给模型的内容由什么组成

上下文由几段内容按顺序拼起来：

1. 如果角色用了 profile，先加 profile 的基础提示词。
2. 再加角色职责。
3. 团队信息、技能目录和项目概览，满足条件时才加入。
4. 派发来的任务，作为开头的用户内容。

工具 schema 单独传给模型服务，不拼进提示词。

会话里保存的是完整记录。发给模型的请求是经过长度处理的版本。两者用途不同。

## 模型怎样发起真实操作

收到工具调用后，运行时依次做这些事：

1. 解析工具名称和 JSON 参数。
2. 按工具 schema 校验参数。
3. 在当前角色可用的工具里找到对应实现。
4. 执行，并传入带 Agent 身份和环境的运行信息。文件工具读取对应工作区，Bash 在对应环境里启动进程。

## Agent 能做什么，由谁决定

| 控制点 | 决定什么 |
| --- | --- |
| 提示词 | 说明任务和约束 |
| `tool_choice` | 引导本次请求选择工具 |
| 配置的工具集合 | 能调用哪些能力 |
| 运行时权限检查 | 操作是否允许 |
| 执行环境 | 文件、进程和网络的实际范围 |

只读分析任务直接给读取工具就够了。

## 进阶：工具结果怎样回到对话，任务怎样结束

工具输出会和原来的工具调用配对保存，再进入后续请求。

- 派发子成员会产生"延迟结果"。Session 会等这一批关联结果全部到齐再继续。
- 成员之间的消息交接，会在接收方后面的回合里作为用户消息加入。详见 [Session 怎样保存进度和继续执行](session-state-lifecycle.md)。

结束时 Session 进入以下状态之一，SDK 再把它整理成运行结果：

| 状态 | 什么时候 |
| --- | --- |
| `DONE` | 模型给出最终回答 |
| `STOPPED` | 受预算、步数或其他运行限制影响，保留原因 |
| `ERROR` | 出现未处理的故障 |

验收任务时，请检查实际文件、命令结果或外部系统状态，不要只看状态。

## 下一步读什么

- 修改怎样交付：[把候选结果采纳到交付目录](../isolation/candidate-delivery.md)
- 命令权限：[管理终端权限并确认命令结果](../extensions/shell-permissions-execution.md)
- 源码：[会话执行](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/session_run.py)、[工具执行](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/tool_execution.py)、[上下文设计](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/runtime-design.md)
