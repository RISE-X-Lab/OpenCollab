# OpenCollab Wiki

OpenCollab（OC）是一个让 AI Agent 读代码、改代码、跑命令的框架。你可以只用一个 Agent；也可以让几个 Agent 组成团队，由模型自己分工（Team）；还可以用 Python 代码把协作步骤写清楚（Workflow）。

本 Wiki 按 [main 分支提交 0c47f74a](https://github.com/RISE-X-Lab/OpenCollab/tree/0c47f74a27441888286a22012b23b658185f8b8f) 的源码核对。这份源码的包版本号仍是 0.9.5；要用 VM 隔离，或让成员用 `manage_workflow describe` 查询可用能力，请按 [安装指南](topics/setup/first-run-workspace.md#第一步安装) 安装这个提交。

[返回项目站主页](https://rise-x-lab.github.io/OpenCollab/) · [GitHub 源码](https://github.com/RISE-X-Lab/OpenCollab) · [阅读路径](reading-paths.md) · [问题导航](questions.md)

## 一张图看懂 OC

OC 的所有设置，都能放进下面这张图：

1. **每个会话 = 模型 + 工具 + 执行环境 + 限额。** 会话（Session）是一个 Agent 的一次“思考 → 调用工具 → 看结果”循环。
2. **一次运行 = 若干会话 + 一种组织方式。** 组织方式回答“下一步由谁决定”：单 Agent、Team（模型决定）或 Workflow（代码决定）。
3. **交付 = 采纳改动 + 用证据确认做对了。** 隔离环境里的改动要显式采纳；`completed` 不等于做对，要看真正执行过的测试。

本 Wiki 的目录就按这张图排列：

| 你要回答的问题 | OC 里对应的设置 | 去哪一章 |
| --- | --- | --- |
| 下一步由谁决定？ | 单 Agent、Team、Workflow | Team：让模型分工 · Workflow：用代码编排 |
| 用哪个模型来想？ | provider、模型名、路由、深度思考 | 模型与配置 |
| 能做哪些操作？ | 工具列表、命令权限、Skill、Hook | 工具与扩展 |
| 在哪里动手？改动怎样回来？ | 隔离策略、执行环境、采纳 | 执行环境与隔离 |
| 最多花多少？什么时候停？ | 预算、步数、并发、超时、上下文 | 运行控制 |
| 做了什么？做对没有？ | 运行结果、测试证据、轨迹、用量 | 结果与记录 |

第一次来，先读 [OC 是什么](topics/orientation/concepts-and-modes.md)，再照着 [安装并完成第一次运行](topics/setup/first-run-workspace.md) 动手试一遍，大约十几分钟。

## 从你的目标开始

| 我想… | 先读这些 |
| --- | --- |
| 第一次用 OC，先跑起来 | [OC 是什么](topics/orientation/concepts-and-modes.md) · [安装并完成第一次运行](topics/setup/first-run-workspace.md) |
| 在自己的 Python 程序里调用 | [从 Python 调用 OC](topics/setup/python-sdk-entrypoints.md) · [判断任务是否真的完成](topics/observability/run-results.md) |
| 组建团队，让几个 Agent 分工 | [Team 入门](topics/team/configure-team.md) · [Team 协作](topics/team/dispatch-team-agents.md) |
| 自己写一个工作流 | [编写 Workflow](topics/workflow/workflow-programming-model.md) · [在 Workflow 里调用 Agent](topics/workflow/workflow-agent-calls.md) |
| 不想让 Agent 弄乱我的项目 | [隔离](topics/isolation/isolation-overview.md) · [隔离改动怎样回到项目](topics/isolation/candidate-delivery.md) |
| 让团队成员调用或自己编写工作流 | [让 Team 成员运行 Workflow](topics/workflow/team-run-workflow.md) |
| 配置没生效、启动报错 | [配置从哪来、哪个生效、出错怎么查](topics/models/configuration-precedence.md) |

## 全部指南

### 认识 OC

| 指南 | 读完你能做到 |
| --- | --- |
| [OC 是什么：一张图看懂会话、Team 和 Workflow](topics/orientation/concepts-and-modes.md) | 建立整体图，按任务挑一个起点 |

### 上手使用

| 指南 | 读完你能做到 |
| --- | --- |
| [安装并完成第一次运行](topics/setup/first-run-workspace.md) | 装好 OC、接上模型，在自己的项目上跑通第一个任务 |
| [在终端里使用 OC](topics/setup/interactive-cli-controls.md) | 切换成员、追问、保存和中断，把参数写在正确的命令层级 |
| [从 Python 调用 OC](topics/setup/python-sdk-entrypoints.md) | 用 SDK 运行单 Agent、Team 和 Workflow，理解 Base 与 Single2 |

### Team：让模型分工

| 指南 | 读完你能做到 |
| --- | --- |
| [Team 入门：用 team.yaml 组织多个 Agent](topics/team/configure-team.md) | 写出角色、工具和拓扑，选按需成员或固定名册，设置团队预算 |
| [Team 协作：派发、消息、等待与采纳](topics/team/dispatch-team-agents.md) | 看懂并用好派活、沟通、等结果、收改动这个完整循环 |

### Workflow：用代码编排

| 指南 | 读完你能做到 |
| --- | --- |
| [运行一个 Workflow](topics/workflow/run-registered-workflow.md) | 找到一个 Workflow，交给它任务，读懂结果 |
| [编写 Workflow：普通 Python 加上 ctx](topics/workflow/workflow-programming-model.md) | 写出第一个可按名称运行的 Workflow，知道 OC 能做和不能做什么 |
| [在 Workflow 里调用 Agent：会话、数据传递与并行](topics/workflow/workflow-agent-calls.md) | 在步骤之间传数据、拿结构化结果、批量并行 |
| [组合与嵌套：子流程、候选环境与互相调用](topics/workflow/workflow-composition-hierarchy.md) | 复用子流程，在候选环境里试改动再采纳，理解嵌套时的预算和停止 |
| [Workflow 的失败处理、测试与调试](topics/workflow/workflow-failure-recovery-lifecycle.md) | 认出各层失败、安全重试，不花模型额度先测控制流 |
| [Workflow API 速查](topics/workflow/workflow-public-api-map.md) | 先找对控制层，再查到具体的参数和接口 |
| [让 Team 成员运行 Workflow](topics/workflow/team-run-workflow.md) | 用 run_workflow 跑已注册的流程，用 manage_workflow 让成员现场编排子 Workflow |
| [Duo：让两个 coder 各写一版，再挑一个](topics/workflow/duo-workflow.md) | 用内置 Duo 比较两份完整实现并采纳一份 |
| [Weave：分组接力完成任务，用检查命令判定交付](topics/workflow/weave-workflow.md) | 把任务拆成有依赖的组，用真实检查判定交付并有限修复 |
| [Reconstruct：从输入输出反推一个程序](topics/workflow/reconstruct-workflow.md) | 接入隐藏程序和执行器，让 Agent 重建行为相同的代码 |

### 模型与配置

| 指南 | 读完你能做到 |
| --- | --- |
| [接入模型服务：端点、凭据、协议与流式](topics/models/provider-endpoint-credentials.md) | 接上 OpenAI 兼容或原生 Anthropic 服务，弄清用哪个 API key |
| [深度思考与采样参数](topics/models/thinking-reasoning-sampling.md) | 开关深度思考、设置推理强度和采样参数 |
| [模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) | 让不同角色用不同服务，限制同机并发请求数 |
| [配置从哪来、哪个生效、出错怎么查](topics/models/configuration-precedence.md) | 弄清配置的优先顺序，按阶段排查启动故障 |
| [配置参数速查](topics/models/configuration-reference.md) | 一张表查清每个配置项的名字、默认值和位置 |

### 工具与扩展

| 指南 | 读完你能做到 |
| --- | --- |
| [内置工具：怎么选、文件怎么改、命令怎么跑](topics/extensions/tool-selection.md) | 挑选和限制工具，理解文件编辑和命令执行的安全边界 |
| [自定义工具：把你的函数交给 Agent 调用](topics/extensions/custom-tools.md) | 把业务函数接成工具，并在 Agent 所在环境里操作 |
| [Skill：按需加载的说明书](topics/extensions/skills-on-demand.md) | 写 SKILL.md，让模型按需加载操作说明 |
| [Hook：在工具调用和成员生命周期里运行命令](topics/extensions/team-hooks.md) | 在工具前后、成员启停时运行观察命令 |

### 执行环境与隔离

| 指南 | 读完你能做到 |
| --- | --- |
| [隔离：Agent 的文件和命令落在哪里](topics/isolation/isolation-overview.md) | 在六种策略里挑一种，弄清在哪设置、谁优先、工作树从哪开始 |
| [在容器中运行任务](topics/isolation/docker-isolation.md) | 让工具在 Docker 里执行，设置镜像、网络和资源上限 |
| [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) | 准备来宾、写 VM 配置，运行后导出改动并重置机器 |
| [使用自己的执行环境](topics/isolation/execution-environments.md) | 把现成的目录、容器或 VM 交给 SDK，或注册新的隔离策略 |
| [隔离改动怎样回到项目：采纳、找回与清理](topics/isolation/candidate-delivery.md) | 采纳候选；出问题时先保住改动再清理资源 |

### 运行控制：预算、停止与会话

| 指南 | 读完你能做到 |
| --- | --- |
| [预算、步数与并发](topics/operations/token-step-concurrency-limits.md) | 设置三个限额旋钮，理解团队分额度和软额度 |
| [运行怎样停下：超时、重试、取消与清理](topics/operations/timeouts-retries.md) | 找到卡在哪一层，停止后确认清理完成 |
| [上下文窗口与自动压缩](topics/operations/context-compaction.md) | 确认窗口、调整压缩策略、排查溢出 |
| [保存与恢复会话](topics/operations/session-save-resume.md) | 找到会话文件，用 --session 接着对话 |

### 结果与记录

| 指南 | 读完你能做到 |
| --- | --- |
| [判断任务是否真的完成：运行结果与测试证据](topics/observability/run-results.md) | 读懂 RunResult，用真实执行的测试确认交付 |
| [运行记录：产物目录、轨迹与实时事件](topics/observability/artifacts-and-history.md) | 找到并串起会话、清单和轨迹，实时观察运行 |
| [用量与费用：token、缓存命中和花费](topics/observability/token-cache-cost.md) | 开启用量账本，不重不漏地算费用 |
| [检查团队是否真的按分工协作](topics/observability/collaboration-adherence.md) | 对比团队声明和实际行为 |

### 原理、集成与维护

| 指南 | 读完你能做到 |
| --- | --- |
| [原理：一次运行在内部怎样推进](topics/architecture/runtime-execution-model.md) | 理解 agent loop 和会话状态机，知道行为由谁控制 |
| [OC 的边界：需求该从哪里扩展，评测由谁负责](topics/architecture/extension-and-evaluation-boundaries.md) | 把需求放到合适的扩展入口，理解 OC 与 OCE 的分工 |
| [把 OC 接进自己的服务](topics/architecture/service-integration-lifecycle.md) | 一个请求一次运行，处理结果、超时和清理责任 |
| [升级 OC 后怎样迁移和验证](topics/architecture/upgrade-existing-integration.md) | 核对版本变化，用原任务验证升级 |
| [遇到问题怎么反馈、求助或提建议](topics/architecture/report-problem-and-request-help.md) | 选对入口，附上最小复现和脱敏记录 |
