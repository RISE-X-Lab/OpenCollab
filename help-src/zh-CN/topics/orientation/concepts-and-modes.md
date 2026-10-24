---
{
  "id": "concepts-and-modes",
  "title": "先认识 Agent、Session、Team 和 Workflow",
  "category": "orientation",
  "summary": "OC 的 Agent、Session、Team、Workflow 分别对应通用多 agent 框架里的什么，以及它们怎么组合。",
  "aliases": [
    "智能体",
    "会话",
    "harness",
    "多智能体",
    "工作流和团队区别",
    "基本概念"
  ],
  "keywords": [
    "OC概念",
    "OC是什么",
    "Agent",
    "Session",
    "Team",
    "Workflow"
  ],
  "questions": [
    "建立 OC 的使用概念，理解不同对象之间的关系并找到下一篇指南？",
    "OC 到底是什么？",
    "Agent 和 Session 有什么区别？",
    "多个 Agent 之间会共享哪些东西？"
  ],
  "related": [
    "choose-working-mode",
    "base-single2",
    "configure-team",
    "run-registered-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 先认识 Agent、Session、Team 和 Workflow

OC（OpenCollab）是一个 agent harness：它跑标准的 agent loop（模型提出 tool call，执行环境执行，结果回填上下文），并在上面提供两种多 agent 组织方式。一种是 Team，由模型自己派发任务；另一种是 Workflow，用 Python 显式编排。这篇把 OC 的名词对应到你熟悉的通用概念，并说明默认入口和几处与常见做法不同的设计。

## 名词对照

| OC 名词 | 对应的通用概念 | OC 里的具体含义 |
| --- | --- | --- |
| Agent | agent 配置（角色） | 模型 + 系统提示 + 工具集合。同一个 Agent 可以参与多次任务。 |
| Session | 一条对话线程 / agent 运行实例 | 保存一次对话的消息、执行阶段、用量和等待状态（例如正在等用户回答）。每次任务都要关联到明确的会话。 |
| profile | 预设的 agent 配置 | 提示词、默认工具、限额的组合。Base 是默认别名，当前指向 Single2。 |
| Team | 由模型自主派发的多 agent | 成员按角色提示和通信拓扑，自己决定把任务交给谁、给谁发消息。 |
| Workflow | 用代码显式编排 agent 调用 | 一个 Python async 函数，用普通的条件和循环决定调用顺序与结果处理。 |
| 执行环境 / 隔离策略 | sandbox | 决定工具调用在哪里真正执行，以及文件是否共享。与 Team、Workflow 的选择相互独立。 |

## 三种用法与最小入口

| 方式 | 下一步由谁决定 | 适合 | 入口 |
| --- | --- | --- | --- |
| 单 Agent | 模型在当前工具集合里选 | 调查代码、一次修复、分析 | SDK `OpenCollab(...).agent(...)` |
| Team | 成员按角色提示和通信关系派发、交流 | 分工随任务变化 | CLI `opencollab --workspace .`，或 SDK `team()` |
| Workflow | Python 函数 | 对比方案、重复流程、规定好验证和采用步骤 | SDK `workflow()`，或 CLI `opencollab workflow run NAME` |

默认行为：

- `opencollab --workspace .` 启动的不是单 Agent，而是内置的 Self-Collaboration 团队。
- SDK 的 `OpenCollab(...).agent(...)` 是单 Agent 入口，当前 Base 配置映射到 Single2。
- Duo、Weave 和 Reconstruct 是 OC 已经实现好的具体 Workflow。

## 三者如何组合

三种方式共用同一套会话和工具机制，可以互相嵌套：

- Team 的某个成员可以获得调用 Workflow 的能力。
- Workflow 内部可以启动 Agent 会话。

组织方式决定什么时候开会话；会话负责和模型交互并执行工具。

工具也分两类。普通编码工具单 Agent 就能用。Team 协作工具（派发、发消息这类）需要团队调度器支持，单 Agent 下不可用。

## 进阶：哪些东西共享，哪些不共享

OC 把下面三件事拆成独立设置：

1. 上下文不共享。每个会话单独保存对话。成员能看到什么，只取决于它收到的任务说明、显式消息和返回结果。
2. 文件是否共享，由执行环境和隔离策略决定。两个 Agent 可以对话独立、却编辑同一个目录；也可以各用一个 Git 工作树（worktree，同一仓库拉出的独立目录），再通过补丁交付改动。
3. 模型请求并发是另一项设置，它限制同时发往模型服务的请求数。

建议的配置顺序：先选运行方式，再定工作区、模型和工具，最后配预算和结果保存。每一步都能单独观察是否符合预期。

## 源码与进一步阅读

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py) · [opencollab/domain/session.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/domain/session.py) · [docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/runtime-design.md)

[我该用默认 CLI、Base、Team 还是 Workflow](choose-working-mode.md) · [使用 Base 单 Agent 与 Single2 配置](../setup/base-single2.md) · [Team 入门：配置你的多 Agent 团队](../team/configure-team.md) · [运行一个工作流（Workflow）](../workflow/run-registered-workflow.md)
