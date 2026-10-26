---
{
  "id": "concepts-and-modes",
  "title": "OC 是什么：一张图看懂会话、Team 和 Workflow",
  "category": "orientation",
  "summary": "用一张图理解 OC：每个会话由模型、工具、执行环境和限额组成，多个会话由单 Agent、Team 或 Workflow 组织，最后采纳改动并用证据验收；再按任务挑一个起点。",
  "aliases": [
    "智能体",
    "会话",
    "harness",
    "多智能体",
    "工作流和团队区别",
    "基本概念",
    "单智能体还是多智能体",
    "Team vs Workflow",
    "默认模式",
    "Duo还是Weave",
    "该选哪种方式"
  ],
  "keywords": [
    "OC概念",
    "OC是什么",
    "Agent",
    "Session",
    "Team",
    "Workflow",
    "模式选择",
    "用哪个模式",
    "CLI",
    "Base"
  ],
  "questions": [
    "建立 OC 的使用概念，理解不同对象之间的关系并找到下一篇指南？",
    "OC 到底是什么？",
    "Agent 和 Session 有什么区别？",
    "多个 Agent 之间会共享哪些东西？",
    "根据交互需求、可重复流程和协作方式选择最合适的启动入口？",
    "我只想让 AI 帮我改代码，该用哪个？",
    "Team 和 Workflow 有什么区别？",
    "Duo、Weave、Reconstruct 分别适合什么任务？"
  ],
  "related": [
    "first-run-workspace",
    "python-sdk-entrypoints",
    "configure-team",
    "workflow-programming-model",
    "run-registered-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# OC 是什么：一张图看懂会话、Team 和 Workflow

OpenCollab（下文简称 OC）是一个让 AI Agent 读代码、改代码、跑命令的框架，并且可以让多个 Agent 协作。这篇不讲具体参数，只帮你建立一张整体图。OC 的每一个设置都能放进这张图的某个格子里；看懂它，再读任何一篇指南，你都知道它在讲哪一块。

## 一切从“会话”开始

Agent 干活的方式是一个循环：模型读到任务，决定调用某个工具（读文件、改代码、跑命令）；OC 执行这个工具，把结果交回模型；模型再决定下一步，直到它给出最终回答，或者用完额度。

OC 把这样一个循环的全部状态叫作一个**会话（Session）**：消息历史、执行到哪一步、用了多少 token、正在等什么。会话可以存成文件，之后再恢复。

一个会话由四样东西决定：

| 要素 | 回答什么问题 | 例子 | 详见 |
| --- | --- | --- | --- |
| 模型 | 用哪个大模型来思考 | provider、模型名、深度思考 | [接入模型服务](../models/provider-endpoint-credentials.md) |
| 工具 | 它能做哪些操作 | `file_read`、`apply_patch`、`bash` | [内置工具](../extensions/tool-selection.md) |
| 执行环境 | 这些操作落在哪里 | 项目目录、独立工作树、Docker 容器、Linux VM | [隔离](../isolation/isolation-overview.md) |
| 限额 | 最多花多少、什么时候停 | token 预算、步数、期限 | [预算、步数与并发](../operations/token-step-concurrency-limits.md) |

OC 里的 **Agent** 指的是一份角色配置：用哪个模型、什么系统提示、给哪些工具。真正在跑的是它的会话。同一份 Agent 配置可以开很多个会话。

## 多个会话怎样组织：三种方式

只有一个会话时，下一步由会话里的模型自己决定。有了多个会话，就要回答一个关键问题：**下一步由谁决定？** OC 给出三种答案。

| 方式 | 下一步由谁决定 | 适合 | 最小入口 |
| --- | --- | --- | --- |
| 单 Agent | 模型自己，在给定的工具里挑 | 调查代码、一次修复、分析 | SDK `OpenCollab(".").agent(...)` |
| Team | 成员模型自己：把活派给谁、给谁发消息 | 分工随任务变化，需要互相审查 | CLI `opencollab --workspace .`，SDK `team(...)` |
| Workflow | 你写的 Python 代码 | 步骤固定，要精确控制顺序、并行、重试和验收 | CLI `opencollab workflow run NAME`，SDK `workflow(...)` |

几个容易误会的默认行为：

- 直接运行 `opencollab --workspace .`，启动的不是单 Agent，而是内置的 Self-Collaboration 团队：入口成员 Analyst 可以把实现交给 Coder，把验证交给 Tester。
- SDK 的 `agent()` 才是单 Agent。它默认用名为 Base 的配置，Base 当前指向 Single2。
- Duo、Weave、Reconstruct 是 OC 已经写好的三个 Workflow，可以直接运行。

三种方式可以互相嵌套：Workflow 里每调用一次 Agent 就开一个新会话；Team 成员可以拿到运行 Workflow 的工具。见 [组合与嵌套](../workflow/workflow-composition-hierarchy.md)。

## 运行结束：改动要采纳，结果要验证

Agent 在隔离环境（独立工作树、容器或 VM）里改的文件，**不会自动写回你的项目**。它们先作为“候选”保存，由你的代码或入口成员显式**采纳**之后，才进入交付目录。这样几个 Agent 可以各改各的，最后挑一个；代价是多了一步采纳。见 [隔离改动怎样回到项目](../isolation/candidate-delivery.md)。

另外，运行状态 `completed` 只说明运行正常结束，不说明任务做对了。做没做对，要看实际文件和真正执行过的测试。见 [判断任务是否真的完成](../observability/run-results.md)。

把上面三部分连起来，就是 OC 的整张图：

1. 每个会话 = 模型 + 工具 + 执行环境 + 限额。
2. 一次运行 = 若干会话 + 一种组织方式（单 Agent、Team 或 Workflow）。
3. 交付 = 采纳改动 + 用证据确认做对了。

本 Wiki 的目录就是按这张图排的：先讲组织方式（Team、Workflow），再讲会话的四个要素（模型、工具、执行环境、限额），最后讲结果和记录。

## 三件事分开设置

很多框架默认“所有 Agent 共用一个大环境”。OC 把下面三件事拆开，各自独立设置：

1. **对话不共享。** 每个会话只有自己的历史。一个成员能看到什么，只取决于它收到的任务说明、别人显式发来的消息和返回结果。
2. **文件是否共享，由执行环境决定。** 两个 Agent 可以对话互相独立，却改同一个目录；也可以各用一个 Git 工作树（同一仓库拉出的独立目录），最后通过补丁交付。
3. **同时发给模型服务的请求数，是另一项设置。** 它和“同时跑几个 Agent”不是一回事。见 [模型路由与共享请求容量](../models/model-routing-request-capacity.md)。

## 怎么选起点

按“你想怎样推进任务”来挑：

| 你的目标 | 推荐起点 | 接下来要定的事 |
| --- | --- | --- |
| 在终端里持续处理一个项目 | 默认 CLI | 工作目录、模型、命令权限 |
| 在自己的程序里拿到一次编码结果 | `agent()` | 工具、额度、产物目录 |
| 让分析、实现、测试几个角色自己交接 | Team | 角色、谁能联系谁、采纳谁的改动 |
| 强制先实现、再验证 | 自定义 Workflow | 输入输出、怎样验证、失败怎么办 |
| 让两份方案各写一遍，再挑一份 | [Duo](../workflow/duo-workflow.md) | 执行环境、候选证据、交付方式 |
| 把开发拆成几组，每组都有检查 | [Weave](../workflow/weave-workflow.md) | 组间依赖、检查命令、修复次数 |
| 只能看输入输出，反推一个隐藏程序 | [Reconstruct](../workflow/reconstruct-workflow.md) | 查询接口、候选执行器、何时停止 |

拿不准时，先从一个 Agent 开始。简单任务一个 Agent 通常就够了，过程也最容易看清。角色越多，越要操心成员之间怎么交接、上下文怎么传、改动最后怎么交到目标目录。

三条稳妥的上手习惯：

1. 面对陌生仓库，先只给只读工具，让 Agent 调查。
2. 需要执行项目代码时，先决定在什么环境里跑，以及网络和挂载怎么设。
3. 一开始就保留运行记录。出问题时，才能看到它实际调了什么、为什么停下、改动去了哪里。

## 下一步读什么

- 马上跑起来：[安装并完成第一次运行](../setup/first-run-workspace.md)
- 在自己的 Python 程序里用：[从 Python 调用 OC](../setup/python-sdk-entrypoints.md)
- 组建团队：[Team 入门](../team/configure-team.md)
- 写自己的流程：[编写 Workflow](../workflow/workflow-programming-model.md)

## 延伸阅读

- 源码依据：[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py) · [opencollab/domain/session.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/domain/session.py) · [docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/runtime-design.md) · [docs/single2.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/single2.md)
