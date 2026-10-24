# 问题导航

[Wiki](index.md) · [返回项目站主页](https://rise-x-lab.github.io/OpenCollab/) · [GitHub 源码](https://github.com/RISE-X-Lab/OpenCollab)

按下面的常见问题找答案，点右侧链接查看详细解释和示例。没找到的话，试试页面上方的搜索。

| 问题 | 对应指南 |
| --- | --- |
| 哪些环境变量和 SDK 字段控制模型与运行参数 | [配置参数与环境变量速查](topics/models/configuration-reference.md) |
| 如何查看各项配置的内置默认值 | [配置参数与环境变量速查](topics/models/configuration-reference.md) |
| OC 本身提供什么，为何还需要模型服务 | [先认识 Agent、Session、Team 和 Workflow](topics/orientation/concepts-and-modes.md) |
| 默认终端启动的是一个 Agent 还是一个团队 | [在终端里和 Agent 对话，以及命令参数该写在哪](topics/setup/interactive-cli-controls.md) |
| Base、Single2、Team 和 Workflow 怎么选 | [我该用默认 CLI、Base、Team 还是 Workflow](topics/orientation/choose-working-mode.md) |
| 第一次使用 OC 应该运行哪个例子 | [在自己的项目中完成第一次运行](topics/setup/first-run-workspace.md) |
| 模型名、端点和环境变量谁优先 | [配置从哪里读取以及哪一项生效](topics/models/configuration-precedence.md) |
| 一个团队可以使用不同模型吗 | [逻辑模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) |
| 同一台机器的多个进程怎样共用模型额度 | [逻辑模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) |
| 并发 Agent 数和模型请求数是同一个设置吗 | [设置 token 预算、步数与运行并发](topics/operations/token-step-concurrency-limits.md) |
| 保存的 reasoning 是否都属于当前可见正文 | [配置深度思考与采样参数](topics/models/thinking-reasoning-sampling.md) |
| 上下文什么时候压缩，历史是否仍保留 | [上下文窗口与自动压缩](topics/operations/context-compaction.md) |
| Workflow 支持哪些语法，可以用 if、while 和导入模块吗 | [工作流就是普通 Python：编程模型入门](topics/workflow/workflow-programming-model.md) |
| 任何协作想法都能写成 Workflow 吗 | [工作流能做什么、不能做什么](topics/workflow/workflow-capabilities-boundaries.md) |
| Workflow 的控制粒度有哪些，有统一的低中高档位吗 | [在哪一层调整 Workflow：从 CLI 参数到单次调用回调](topics/workflow/workflow-control-granularity.md) |
| 写 Python 流程时可用的 ctx API 有哪些 | [Workflow 公开 API 速查](topics/workflow/workflow-public-api-map.md) |
| 怎样把上一轮结果送给下一轮 Agent | [Workflow 里的数据怎么传：输入、步骤结果、文件与输出](topics/workflow/workflow-dataflow-structured-io.md) |
| 普通 async 函数可以组合为子 Workflow 吗 | [Agent、Team 和 Workflow 怎么互相调用](topics/workflow/workflow-composition-hierarchy.md) |
| Workflow 能否直接通过 ctx 启动一个 Team | [Agent、Team 和 Workflow 怎么互相调用](topics/workflow/workflow-composition-hierarchy.md) |
| Team 怎样运行一个已注册 Workflow | [让 Team 成员调用已注册的 Workflow](topics/workflow/team-run-workflow.md) |
| 成员能否自己生成 Python 工作流并在运行中启动 | [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md) |
| 什么时候用 spawn_agent，什么时候用 run_workflow | [派发 Agent 任务并安排审阅](topics/team/dispatch-team-agents.md) |
| 怎样并行跑候选，再按检查结果只采用一个 | [复用子 Workflow，并在候选环境里试改动](topics/workflow/compose-workflow-candidates.md) |
| Duo 的两个候选是否同时执行 | [用 Duo 跑两个候选方案并选出一个](topics/workflow/duo-workflow.md) |
| Weave 的检查和修复由谁定义 | [用 Weave 分组接力完成任务并检查交付](topics/workflow/weave-workflow.md) |
| Reconstruct 怎样通过观察探索隐藏程序 | [用 Reconstruct 从输入输出反推程序](topics/workflow/reconstruct-workflow.md) |
| 一个 Agent 失败会怎样影响其余步骤 | [Workflow 出错了：失败、重试与恢复](topics/workflow/workflow-failure-recovery-lifecycle.md) |
| 进程退出后能否自动从任意 Python 语句继续 | [Workflow 出错了：失败、重试与恢复](topics/workflow/workflow-failure-recovery-lifecycle.md) |
| 怎样在没有模型额度时验证自己写的流程 | [测试和调试你写的 Workflow](topics/workflow/workflow-testing-debugging.md) |
| 工作树能隔离进程、网络和宿主权限吗 | [隔离怎么选](topics/isolation/isolation-overview.md) |
| 几个 Agent 会自动共享对话吗 | [隔离怎么选](topics/isolation/isolation-overview.md) |
| docker-group 和 docker-session 的差别是什么 | [在容器中运行任务](topics/isolation/docker-isolation.md) |
| Agent 在 Docker 中运行时，编排代码在哪里执行 | [让任务在指定目录或现有容器里执行](topics/isolation/execution-environments.md) |
| 能否让任务使用现有容器或自定义执行服务 | [接入自己的执行环境](topics/isolation/custom-environment-providers.md) |
| 子 Agent 改好了，为什么主目录没有变化 | [采用队友改动并验证交付](topics/team/adopt-team-changes.md) |
| 用户取消以后，命令和容器是否已经清理完 | [停止任务并确认清理完成](topics/operations/cancellation-cleanup.md) |
| 模型报错以后，已经生成的改动去哪里找 | [找回隔离任务的改动并清理资源](topics/isolation/workspace-recovery.md) |
| Skill 与工具、Workflow 的关系是什么 | [使用与编写 Skill](topics/extensions/skills-on-demand.md) |
| 能否写一个业务工具，怎样传给模型 | [把自己的函数接成工具](topics/extensions/custom-tools.md) |
| PreToolUse Hook 可以阻止危险命令吗 | [在工具执行和生命周期中加入 Hook](topics/extensions/team-hooks.md) |
| 为什么运行完成还要检查测试证据 | [读取运行结果与判断任务是否完成](topics/observability/run-results.md) |
| 测试过后继续改文件，旧通过记录是否还适用 | [用实际测试验证 Agent 的交付](topics/observability/native-test-evidence.md) |
| trace、会话文件和用量账本有什么区别 | [找到运行记录、会话和交付文件](topics/observability/artifacts-and-history.md) |
| 缓存与 reasoning token 是否还要加到 total 上 | [统计 token、缓存命中和费用](topics/observability/token-cache-cost.md) |
| 如何检查团队实际的分工协作情况 | [检查团队是否真的按分工协作](topics/observability/collaboration-adherence.md) |
| OC 与 OCE 哪个负责正式 benchmark 评分 | [用 OCE 评测 OC 的 Agent、Team 和 Workflow](topics/observability/evaluation-boundary.md) |
| 怎样把 OC 加到 Web 服务或后台任务系统里 | [把 OpenCollab 接进自己的服务](topics/architecture/service-integration-lifecycle.md) |
| 版本升级后旧团队文件和旧示例怎样检查 | [升级 OpenCollab 后怎样迁移和验证](topics/architecture/upgrade-existing-integration.md) |
| 遇到问题时应该带哪些材料来反馈 | [遇到问题怎么反馈、求助或提建议](topics/architecture/report-problem-and-request-help.md) |
| vm-group 和 vm-session 怎样选择 | [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) |
| 怎样让每个成员独占一台 VM 并运行自己写的 DW | [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md) |
| 如何让子 DW 的 Agent 留在父成员的 VM 内 | [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md) |
| VM 资源不足时如何排队 | [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) |
| 模型怎样查看当前可用的隔离策略和工具 | [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md) |
