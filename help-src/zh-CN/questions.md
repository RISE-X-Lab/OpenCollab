# 问题导航

[Wiki](index.md) · [阅读路径](reading-paths.md) · [返回项目站主页](https://rise-x-lab.github.io/OpenCollab/) · [GitHub 源码](https://github.com/RISE-X-Lab/OpenCollab)

按常见问题找答案，点右侧链接直接跳到解答所在的段落。问题按 [OC 的整体图](topics/orientation/concepts-and-modes.md#运行结束改动要采纳结果要验证) 分组：先是组织方式，再是会话的四个要素（模型、工具、执行环境、限额），最后是结果。没找到的话，试试页面上方的搜索。

## 认识与上手

| 问题 | 对应指南 |
| --- | --- |
| OC 本身提供什么，为何还需要模型服务 | [OC 是什么](topics/orientation/concepts-and-modes.md#一切从会话开始) |
| Base、Single2、Team 和 Workflow 怎么选 | [OC 是什么：怎么选起点](topics/orientation/concepts-and-modes.md#怎么选起点) |
| 第一次使用 OC 应该运行哪个例子 | [安装并完成第一次运行](topics/setup/first-run-workspace.md#第三步在你的项目上运行) |
| 默认终端启动的是一个 Agent 还是一个团队 | [OC 是什么：三种组织方式](topics/orientation/concepts-and-modes.md#多个会话怎样组织三种方式) |

## Team

| 问题 | 对应指南 |
| --- | --- |
| 什么时候用 spawn_agent，什么时候用 run_workflow | [让 Team 成员运行 Workflow](topics/workflow/team-run-workflow.md#两种工具怎么选) |
| 子 Agent 改好了，为什么主目录没有变化 | [Team 协作：采纳改动](topics/team/dispatch-team-agents.md#第四步采纳改动再验证) |
| 几个 Agent 会自动共享对话吗 | [隔离其实是五件事](topics/isolation/isolation-overview.md#先理解隔离其实是五件事) |
| 如何检查团队实际的分工协作情况 | [检查团队是否真的按分工协作](topics/observability/collaboration-adherence.md) |

## Workflow

| 问题 | 对应指南 |
| --- | --- |
| Workflow 支持哪些语法，可以用 if、while 和导入模块吗 | [编写 Workflow：用 Python 控制流程](topics/workflow/workflow-programming-model.md#用-python-控制流程) |
| 任何协作想法都能写成 Workflow 吗 | [OC 管什么，你自己管什么](topics/workflow/workflow-programming-model.md#oc-管什么你自己管什么) |
| Workflow 的控制粒度有哪些，有统一的低中高档位吗 | [先找对层](topics/workflow/workflow-public-api-map.md#先找对层一个设置该放在哪) |
| 写 Python 流程时可用的 ctx API 有哪些 | [Workflow API 速查](topics/workflow/workflow-public-api-map.md#按需求查接口) |
| 怎样把上一轮结果送给下一轮 Agent | [每次调用都是一个新会话](topics/workflow/workflow-agent-calls.md#先记住每次调用都是一个新会话) |
| 普通 async 函数可以组合为子 Workflow 吗 | [直接调用子函数](topics/workflow/workflow-composition-hierarchy.md#方式一直接调用子函数) |
| Workflow 能否直接通过 ctx 启动一个 Team | [在 Workflow 里启动 Team](topics/workflow/workflow-composition-hierarchy.md#在-workflow-里启动-team) |
| Team 怎样运行一个已注册 Workflow | [run_workflow 运行已注册的 Workflow](topics/workflow/team-run-workflow.md#方式一run_workflow-运行已注册的-workflow) |
| 成员能否自己生成 Python 工作流并在运行中启动 | [manage_workflow 让成员自己写子 Workflow](topics/workflow/team-run-workflow.md#方式二manage_workflow-让成员自己写子-workflow) |
| 怎样并行跑候选，再按检查结果只采用一个 | [候选：在隔离环境里试改动](topics/workflow/workflow-composition-hierarchy.md#候选在隔离环境里试改动) |
| Duo 的两个候选是否同时执行 | [Duo 的执行过程](topics/workflow/duo-workflow.md#执行过程) |
| Weave 的检查和修复由谁定义 | [Weave 的检查与修复](topics/workflow/weave-workflow.md#检查与修复) |
| Reconstruct 怎样通过观察探索隐藏程序 | [Reconstruct 每一轮怎么推进](topics/workflow/reconstruct-workflow.md#每一轮怎么推进) |
| 一个 Agent 失败会怎样影响其余步骤 | [出错时先看是哪一层](topics/workflow/workflow-failure-recovery-lifecycle.md#出错时先看是哪一层) |
| 进程退出后能否自动从任意 Python 语句继续 | [进程退出后能接着跑吗](topics/workflow/workflow-failure-recovery-lifecycle.md#进程退出后能接着跑吗) |
| 怎样在没有模型额度时验证自己写的流程 | [用替身测控制流](topics/workflow/workflow-failure-recovery-lifecycle.md#第一层用替身测控制流) |

## 成员在自己的 VM 里运行子 Workflow

| 问题 | 对应指南 |
| --- | --- |
| vm-group 和 vm-session 该选哪个 | [先选 vm-group 还是 vm-session](topics/isolation/vm-environment-extension.md#先选-vm-group-还是-vm-session) |
| VM 都被占用时，新会话怎样排队等待 | [VM 配置的 wait_timeout](topics/isolation/vm-environment-extension.md#第二步写-vm-配置) |
| 怎样让每个成员独占一台 VM，并在里面运行自己写的子 Workflow | [在 VM 成员里用独立工作树](topics/workflow/team-run-workflow.md#在-vm-成员里用独立工作树) |
| 成员写的子 Workflow 里的 Agent，怎样留在这个成员的 VM 里执行 | [子 Agent 在哪里执行](topics/workflow/team-run-workflow.md#子-agent-在哪里执行) |
| 成员 Agent 怎样查出自己能用哪些隔离策略和工具 | [看看能用什么](topics/workflow/team-run-workflow.md#第-1-步看看能用什么) |

## 模型与配置

| 问题 | 对应指南 |
| --- | --- |
| 模型名、端点和环境变量谁优先 | [配置的优先顺序](topics/models/configuration-precedence.md#优先顺序) |
| 哪些环境变量和 SDK 字段控制模型与运行参数 | [配置参数速查](topics/models/configuration-reference.md) |
| 如何查看各项配置的内置默认值 | [配置参数速查](topics/models/configuration-reference.md) |
| 一个团队可以使用不同模型吗 | [定义一条逻辑路由](topics/models/model-routing-request-capacity.md#定义一条逻辑路由) |
| 同一台机器的多个进程怎样共用模型额度 | [限制同一台机器上的请求数](topics/models/model-routing-request-capacity.md#限制同一台机器上的请求数) |
| 保存的 reasoning 是否都属于当前可见正文 | [深度思考与采样](topics/models/thinking-reasoning-sampling.md) |

## 工具与扩展

| 问题 | 对应指南 |
| --- | --- |
| Skill 与工具、Workflow 的关系是什么 | [Skill、工具和 Workflow 的关系](topics/extensions/skills-on-demand.md#skill工具和-workflow-的关系) |
| 能否写一个业务工具，怎样传给模型 | [自定义工具](topics/extensions/custom-tools.md) |
| PreToolUse Hook 可以阻止危险命令吗 | [Hook](topics/extensions/team-hooks.md) |

## 执行环境与隔离

| 问题 | 对应指南 |
| --- | --- |
| 工作树能隔离进程、网络和宿主权限吗 | [隔离其实是五件事](topics/isolation/isolation-overview.md#先理解隔离其实是五件事) |
| docker-group 和 docker-session 的差别是什么 | [在容器中运行任务](topics/isolation/docker-isolation.md) |
| Agent 在 Docker 中运行时，编排代码在哪里执行 | [代码到底在哪里运行](topics/workflow/workflow-programming-model.md#代码到底在哪里运行) |
| 能否让任务使用现有容器或自定义执行服务 | [使用自己的执行环境](topics/isolation/execution-environments.md) |
| 模型报错以后，已经生成的改动去哪里找 | [先找回改动，再清理](topics/isolation/candidate-delivery.md#出了问题先找回改动再清理) |

## 运行控制

| 问题 | 对应指南 |
| --- | --- |
| 并发 Agent 数和模型请求数是同一个设置吗 | [Workflow 的两类并发](topics/operations/token-step-concurrency-limits.md#workflow-的两类并发) |
| 上下文什么时候压缩，历史是否仍保留 | [上下文窗口与自动压缩](topics/operations/context-compaction.md) |
| 用户取消以后，命令和容器是否已经清理完 | [停止之后：确认清理完成](topics/operations/timeouts-retries.md#停止之后确认清理完成) |

## 结果与记录

| 问题 | 对应指南 |
| --- | --- |
| 为什么运行完成还要检查测试证据 | [确认任务做对了](topics/observability/run-results.md#第二步确认任务做对了) |
| 测试过后继续改文件，旧通过记录是否还适用 | [测试之后又改了文件](topics/observability/run-results.md#测试之后又改了文件) |
| trace、会话文件和用量账本有什么区别 | [运行记录](topics/observability/artifacts-and-history.md) |
| 缓存与 reasoning token 是否还要加到 total 上 | [这些数怎样相加](topics/observability/token-cache-cost.md#这些数怎样相加) |
| OC 与 OCE 哪个负责正式 benchmark 评分 | [OC 和 OCE 怎样分工](topics/architecture/extension-and-evaluation-boundaries.md#oc-和-oce-怎样分工) |

## 集成与维护

| 问题 | 对应指南 |
| --- | --- |
| 怎样把 OC 加到 Web 服务或后台任务系统里 | [把 OC 接进自己的服务](topics/architecture/service-integration-lifecycle.md) |
| 版本升级后旧团队文件和旧示例怎样检查 | [升级 OC 后怎样迁移和验证](topics/architecture/upgrade-existing-integration.md) |
| 遇到问题时应该带哪些材料来反馈 | [遇到问题怎么反馈](topics/architecture/report-problem-and-request-help.md) |
