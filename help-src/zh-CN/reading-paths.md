# 阅读路径

[Wiki](index.md) · [问题导航](questions.md) · [返回项目站主页](https://rise-x-lab.github.io/OpenCollab/) · [GitHub 源码](https://github.com/RISE-X-Lab/OpenCollab)

下面每条路径是一组按顺序排好的指南，适合想系统学一个主题的人。每条路径都从“为什么”讲到“怎么做”，再到“怎么确认做对了”。从左往右读，已经会的可以跳过。

## 第一次用 OC

建立整体图，装好，跑通第一个任务，看懂结果。

[OC 是什么](topics/orientation/concepts-and-modes.md) → [安装并完成第一次运行](topics/setup/first-run-workspace.md) → [在终端里使用 OC](topics/setup/interactive-cli-controls.md) → [从 Python 调用 OC](topics/setup/python-sdk-entrypoints.md) → [判断任务是否真的完成](topics/observability/run-results.md)

## 组建一个 Team

让几个 Agent 按角色分工：先写团队文件，再掌握协作循环，最后检查它们是否真的按分工干活。

[Team 入门](topics/team/configure-team.md) → [Team 协作：派发、消息、等待与采纳](topics/team/dispatch-team-agents.md) → [隔离：Agent 的文件和命令落在哪里](topics/isolation/isolation-overview.md) → [让 Team 成员运行 Workflow](topics/workflow/team-run-workflow.md) → [检查团队是否真的按分工协作](topics/observability/collaboration-adherence.md)

## 编写自己的 Workflow

用 Python 写协作流程：先理解编程模型，再学调用 Agent 和组合，最后学处理失败和测试。

[编写 Workflow](topics/workflow/workflow-programming-model.md) → [在 Workflow 里调用 Agent](topics/workflow/workflow-agent-calls.md) → [组合与嵌套](topics/workflow/workflow-composition-hierarchy.md) → [失败处理、测试与调试](topics/workflow/workflow-failure-recovery-lifecycle.md) → [Workflow API 速查](topics/workflow/workflow-public-api-map.md) → [Duo](topics/workflow/duo-workflow.md)（一个现成的完整例子）

## 让 Agent 安全地改代码、跑命令

决定 Agent 能做什么、在哪里做，以及改动怎样安全地回到项目。

[内置工具](topics/extensions/tool-selection.md) → [隔离](topics/isolation/isolation-overview.md) → [在容器中运行任务](topics/isolation/docker-isolation.md) → [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) → [隔离改动怎样回到项目](topics/isolation/candidate-delivery.md)

## 接入模型和调好配置

让 OC 连上你的模型服务，或者搞清楚某个配置为什么没生效。

[接入模型服务](topics/models/provider-endpoint-credentials.md) → [配置从哪来、哪个生效、出错怎么查](topics/models/configuration-precedence.md) → [深度思考与采样](topics/models/thinking-reasoning-sampling.md) → [模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) → [配置参数速查](topics/models/configuration-reference.md)

## 把 OC 接进自己的服务

在自己的 Python 程序或后台服务里调用 OC，并管好额度、停止和记录。

[从 Python 调用 OC](topics/setup/python-sdk-entrypoints.md) → [把 OC 接进自己的服务](topics/architecture/service-integration-lifecycle.md) → [预算、步数与并发](topics/operations/token-step-concurrency-limits.md) → [运行怎样停下](topics/operations/timeouts-retries.md) → [运行记录](topics/observability/artifacts-and-history.md) → [用量与费用](topics/observability/token-cache-cost.md)

## 让每个成员在自己的 VM 里编写并运行子 Workflow

让 Team 成员现场写一段 Workflow 代码，在自己的 VM 里并行调用多个 Agent，再把结论交回团队。先选好成员在哪执行，再准备 VM，然后学写子 Workflow；多个子运行同时请求模型时，用共享请求容量控制额度；最后用团队消息把结论发给队友。

[隔离](topics/isolation/isolation-overview.md) → [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) → [manage_workflow 让成员自己写子 Workflow](topics/workflow/team-run-workflow.md#方式二manage_workflow-让成员自己写子-workflow) → [模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) → [和已有成员沟通](topics/team/dispatch-team-agents.md#第二步和已有成员沟通)

## 理解 OC 的内部原理

想知道 OC 内部是怎么运转的，以及它的边界在哪里。

[原理：一次运行在内部怎样推进](topics/architecture/runtime-execution-model.md) → [保存与恢复会话](topics/operations/session-save-resume.md) → [上下文窗口与自动压缩](topics/operations/context-compaction.md) → [隔离](topics/isolation/isolation-overview.md) → [OC 的边界](topics/architecture/extension-and-evaluation-boundaries.md)
