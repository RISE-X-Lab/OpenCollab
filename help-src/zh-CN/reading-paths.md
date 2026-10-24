# 阅读路径

[Wiki](index.md) · [返回项目站主页](https://rise-x-lab.github.io/OpenCollab/) · [GitHub 源码](https://github.com/RISE-X-Lab/OpenCollab)

下面每条路径是一组按顺序排好的指南，适合想系统学习某个主题的人。从左往右读，已经会的可以跳过。

## 刚接触 OC

从零开始：了解概念，装好，跑通第一个任务，看懂结果。

[先认识 Agent、Session、Team 和 Workflow](topics/orientation/concepts-and-modes.md) → [我该用默认 CLI、Base、Team 还是 Workflow](topics/orientation/choose-working-mode.md) → [安装 OpenCollab](topics/setup/install-source-checkout.md) → [在自己的项目中完成第一次运行](topics/setup/first-run-workspace.md) → [在终端里和 Agent 对话，以及命令参数该写在哪](topics/setup/interactive-cli-controls.md) → [读取运行结果与判断任务是否完成](topics/observability/run-results.md)

## 设计新的 Workflow

想用 Python 写自己的协作流程。

[工作流就是普通 Python：编程模型入门](topics/workflow/workflow-programming-model.md) → [工作流能做什么、不能做什么](topics/workflow/workflow-capabilities-boundaries.md) → [编写第一个自定义 Workflow](topics/workflow/author-custom-workflow.md) → [Workflow 公开 API 速查](topics/workflow/workflow-public-api-map.md) → [Workflow 里的数据怎么传：输入、步骤结果、文件与输出](topics/workflow/workflow-dataflow-structured-io.md) → [在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](topics/workflow/workflow-agent-calls.md) → [在 Workflow 中并行处理多项任务](topics/workflow/workflow-parallel-pipeline.md) → [Agent、Team 和 Workflow 怎么互相调用](topics/workflow/workflow-composition-hierarchy.md) → [复用子 Workflow，并在候选环境里试改动](topics/workflow/compose-workflow-candidates.md) → [Workflow 出错了：失败、重试与恢复](topics/workflow/workflow-failure-recovery-lifecycle.md) → [测试和调试你写的 Workflow](topics/workflow/workflow-testing-debugging.md)

## 建立团队并派发任务

想让多个 Agent 分工合作。

[Team 入门：配置你的多 Agent 团队](topics/team/configure-team.md) → [派发 Agent 任务并安排审阅](topics/team/dispatch-team-agents.md) → [向队友发消息并继续协作](topics/team/message-team-agents.md) → [等待队友结果并读懂团队状态](topics/team/wait-for-team-results.md) → [采用队友改动并验证交付](topics/team/adopt-team-changes.md) → [让 Team 成员调用已注册的 Workflow](topics/workflow/team-run-workflow.md) → [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md)

## 理解与配置隔离

想让 Agent 在独立目录或容器里干活。

[隔离怎么选](topics/isolation/isolation-overview.md) → [默认隔离与单次覆盖怎么配](topics/isolation/isolation-configuration.md) → [让多个任务在独立工作区改代码](topics/isolation/worktree-workspaces.md) → [在容器中运行任务](topics/isolation/docker-isolation.md) → [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) → [让任务在指定目录或现有容器里执行](topics/isolation/execution-environments.md) → [把候选结果采纳到交付目录](topics/isolation/candidate-delivery.md) → [找回隔离任务的改动并清理资源](topics/isolation/workspace-recovery.md)

## 配置模型与运行参数

想接入自己的模型服务，或者搞清楚某个配置为什么没生效。

[配置从哪里读取以及哪一项生效](topics/models/configuration-precedence.md) → [配置参数与环境变量速查](topics/models/configuration-reference.md) → [接入模型端点与认证凭据](topics/models/provider-endpoint-credentials.md) → [选择调用协议与流式响应](topics/models/wire-protocol-streaming.md) → [配置深度思考与采样参数](topics/models/thinking-reasoning-sampling.md) → [检查生效配置与排查启动故障](topics/operations/configuration-diagnostics.md)

## 接入自己的应用服务

想在自己的 Python 程序或后台服务里调用 OC。

[从 Python 调用 OpenCollab](topics/setup/python-sdk-entrypoints.md) → [接入模型端点与认证凭据](topics/models/provider-endpoint-credentials.md) → [逻辑模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) → [把 OpenCollab 接进自己的服务](topics/architecture/service-integration-lifecycle.md) → [停止任务并确认清理完成](topics/operations/cancellation-cleanup.md) → [查看 Agent 做了什么](topics/observability/trace-and-events.md) → [找到运行记录、会话和交付文件](topics/observability/artifacts-and-history.md)

## 理解原理与架构

想知道 OC 内部是怎么运转的。

[一次任务是怎么跑起来的](topics/architecture/runtime-execution-model.md) → [Session 怎样保存进度和继续执行](topics/architecture/session-state-lifecycle.md) → [工作流就是普通 Python：编程模型入门](topics/workflow/workflow-programming-model.md) → [工作流能做什么、不能做什么](topics/workflow/workflow-capabilities-boundaries.md) → [隔离怎么选](topics/isolation/isolation-overview.md) → [OpenCollab 管什么，我的需求该从哪里扩展](topics/architecture/extension-and-evaluation-boundaries.md) → [检查团队是否真的按分工协作](topics/observability/collaboration-adherence.md) → [用实际测试验证 Agent 的交付](topics/observability/native-test-evidence.md)

## 让每个成员编写并运行自己的 DW

先在 [隔离指南](topics/isolation/isolation-overview.md) 选择成员执行环境，按 [VM 指南](topics/isolation/vm-environment-extension.md) 准备资源。随后阅读 [成员自编 Workflow](topics/workflow/member-authored-workflow.md)，完成能力查询、源码生成、并发启动和结果回收。用 [共享请求容量](topics/models/model-routing-request-capacity.md) 协调模型额度，按 [团队通信](topics/team/message-team-agents.md) 把结论发回队友。
