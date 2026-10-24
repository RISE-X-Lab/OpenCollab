# OpenCollab Wiki

OpenCollab（OC）是一个多 Agent 编码框架：同一套 Session 与工具运行时之上，支持三种组织方式，单 Agent、由模型自主派发的 Team，以及用 Python 显式编排的 Workflow。隔离（工作树、Docker、VM）、预算和运行记录可以分别配置。

这里的指南按"你想做什么"来组织。第一次接触 OC，建议先读[先认识 Agent、Session、Team 和 Workflow](topics/orientation/concepts-and-modes.md)，再照着[在自己的项目中完成第一次运行](topics/setup/first-run-workspace.md)动手试一遍，大约十几分钟。遇到具体问题，可以直接用页面上方的搜索，或者翻[问题导航](questions.md)。

本 Wiki 依据 [main 提交 0c47f74a](https://github.com/RISE-X-Lab/OpenCollab/tree/0c47f74a27441888286a22012b23b658185f8b8f) 核对，该源码的包版本为 0.9.5。使用 VM 和运行时能力查询时，按 [源码安装指南](topics/setup/install-source-checkout.md) 安装这一提交。

[返回项目站主页](https://rise-x-lab.github.io/OpenCollab/) · [GitHub 源码](https://github.com/RISE-X-Lab/OpenCollab) · [阅读路径](reading-paths.md) · [问题导航](questions.md)

## OC 名词速查

| OC 名词 | 对应的通用概念 |
| --- | --- |
| Agent | 一个角色配置：模型、系统提示和工具集。真正执行的是它的 Session。 |
| Session | 一次 agent loop 的状态：消息历史、阶段、token 用量、等待状态，可保存和恢复。 |
| Team | 多 agent 协作，由成员模型通过 `spawn_agent`、消息等协作工具自主派发，配置写在 `team.yaml`。默认 CLI 启动的就是内置团队。 |
| Workflow | 用 async Python 显式编排 Agent 调用，控制流（顺序、并行、重试、选优）由代码决定。 |
| 隔离 / 执行环境 | Agent 的文件和命令在哪执行：当前目录、Git 工作树、Docker 容器或 Linux VM。结果以补丁形式交回，需要显式采纳。 |

## 从你的目标开始

不知道从哪读起，就在左边找到你想做的事。

| 我想… | 先读这些 |
| --- | --- |
| 第一次用 OC，先跑起来 | [先认识 Agent、Session、Team 和 Workflow](topics/orientation/concepts-and-modes.md) · [在自己的项目中完成第一次运行](topics/setup/first-run-workspace.md) |
| 自己写一个工作流 | [工作流就是普通 Python：编程模型入门](topics/workflow/workflow-programming-model.md) · [Workflow 公开 API 速查](topics/workflow/workflow-public-api-map.md) · [工作流能做什么、不能做什么](topics/workflow/workflow-capabilities-boundaries.md) |
| 组建团队，让几个 Agent 分工 | [Team 入门：配置你的多 Agent 团队](topics/team/configure-team.md) · [派发 Agent 任务并安排审阅](topics/team/dispatch-team-agents.md) |
| 不想让 Agent 弄乱我的项目 | [隔离怎么选](topics/isolation/isolation-overview.md) · [在容器中运行任务](topics/isolation/docker-isolation.md) |
| 让团队成员调用工作流 | [让 Team 成员调用已注册的 Workflow](topics/workflow/team-run-workflow.md) · [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md) |

## 认识 OC

OC 是什么，几种用法有什么区别。

| 指南 | 读完你能做到 |
| --- | --- |
| [先认识 Agent、Session、Team 和 Workflow](topics/orientation/concepts-and-modes.md) | OC 的 Agent、Session、Team、Workflow 分别对应通用多 agent 框架里的什么，以及它们怎么组合。 |
| [我该用默认 CLI、Base、Team 还是 Workflow](topics/orientation/choose-working-mode.md) | 按你想怎么推进任务，从终端、单个 Agent、团队和工作流里挑一个起点。 |

## 安装与开始使用

装好 OC，在自己的项目里跑通第一个任务。

| 指南 | 读完你能做到 |
| --- | --- |
| [安装 OpenCollab](topics/setup/install-source-checkout.md) | 下载源码、选好版本，装出一个能运行 OpenCollab 的 Python 环境。 |
| [在自己的项目中完成第一次运行](topics/setup/first-run-workspace.md) | 配好模型，用 --workspace 指向自己的项目，在终端跑通第一个任务并找到运行记录。 |
| [在终端里和 Agent 对话，以及命令参数该写在哪](topics/setup/interactive-cli-controls.md) | 在交互终端里切换成员、追问、保存和中断，并把选项写在正确的命令层级上。 |
| [从 Python 调用 OpenCollab](topics/setup/python-sdk-entrypoints.md) | 用 OpenCollab 客户端在 Python 里调用单 Agent、团队或工作流，三者都返回统一的 RunResult。 |
| [使用 Base 单 Agent 与 Single2 配置](topics/setup/base-single2.md) | 在 Python 里让一个 Agent 独立完成任务，并按需要给它读、写或执行命令的工具。 |

## 模型接入与配置

让 OC 连上你用的大模型服务，并调好相关参数。

| 指南 | 读完你能做到 |
| --- | --- |
| [配置从哪里读取以及哪一项生效](topics/models/configuration-precedence.md) | 改了配置却没生效时，看这篇弄清 OpenCollab 按什么顺序读取设置。 |
| [配置参数与环境变量速查](topics/models/configuration-reference.md) | 一张表查清每个配置项叫什么、默认值是多少、该写在哪里。 |
| [接入模型端点与认证凭据](topics/models/provider-endpoint-credentials.md) | 把 OpenAI 兼容服务或原生 Anthropic 服务接到 OC 上，并弄清 OC 会用哪个 API key。 |
| [逻辑模型路由与共享请求容量](topics/models/model-routing-request-capacity.md) | 给模型起别名，让不同角色用不同模型，并限制同一台机器上同时发出的模型请求数。 |
| [选择调用协议与流式响应](topics/models/wire-protocol-streaming.md) | 选对 OC 发请求用的协议（Chat Completions、Responses 或 Anthropic Messages），并按需打开 Chat 流式接收。 |
| [配置深度思考与采样参数](topics/models/thinking-reasoning-sampling.md) | 在 OC 里打开或关闭深度思考、设置推理强度和 temperature/top_p，并知道 OC 会替你省略哪些字段。 |

## 执行环境与隔离

决定 Agent 在哪里干活：直接在项目目录、独立的副本目录、容器或虚拟机里。

| 指南 | 读完你能做到 |
| --- | --- |
| [隔离怎么选](topics/isolation/isolation-overview.md) | 根据任务要不要改文件、跑代码、联网，挑一种合适的隔离方式。 |
| [默认隔离与单次覆盖怎么配](topics/isolation/isolation-configuration.md) | 设置整次运行的默认隔离方式，并在某个角色或某次调用上单独改掉它。 |
| [让多个任务在独立工作区改代码](topics/isolation/worktree-workspaces.md) | 让几个任务同时改同一个项目而不互相踩到，并知道改动最后去了哪里。 |
| [在容器中运行任务](topics/isolation/docker-isolation.md) | 让 Agent 在 Docker 容器里执行命令，并设置共用范围、网络和资源上限。 |
| [在现有 Linux VM 中运行任务](topics/isolation/vm-environment-extension.md) | 配好已有虚拟机，让一组会话共用一台机器或每个会话独占一台，并在结束后收回结果。 |
| [让任务在指定目录或现有容器里执行](topics/isolation/execution-environments.md) | 把 Agent 的读写文件和执行命令接到你指定的目录或容器，并弄清谁负责清理。 |
| [接入自己的执行环境](topics/isolation/custom-environment-providers.md) | 注册一个自己的隔离策略名，让 Agent 跑在你提供的环境里，并负责好分配和清理。 |
| [把候选结果采纳到交付目录](topics/isolation/candidate-delivery.md) | Agent 在隔离环境里改的代码不会自动回到项目，本文教你挑一个结果并采纳回来。 |
| [找回隔离任务的改动并清理资源](topics/isolation/workspace-recovery.md) | 任务被取消、输出被截断或清理失败后，找回完整改动，再把残留的工作树和容器清掉。 |

## Team 与子 Agent

组建一个多 Agent 团队，给成员分配任务并收回结果。

| 指南 | 读完你能做到 |
| --- | --- |
| [Team 入门：配置你的多 Agent 团队](topics/team/configure-team.md) | 写一份 team.yaml，规定团队里有哪些角色、各自能用什么工具、谁能给谁派活。 |
| [派发 Agent 任务并安排审阅](topics/team/dispatch-team-agents.md) | 把一项具体任务交给合适的角色去做，还可以安排“写完自动审阅”的循环。 |
| [向队友发消息并继续协作](topics/team/message-team-agents.md) | 用 message_agent 给已存在的成员异步发消息，补充信息、提修改要求或回复对方。 |
| [等待队友结果并读懂团队状态](topics/team/wait-for-team-results.md) | 派发的结果会自动填回工具调用，消息的回复要靠结束回合等待；再用 team_status 看各成员在干什么。 |
| [采用队友改动并验证交付](topics/team/adopt-team-changes.md) | Coder 改完后，把它的改动合进团队交付目录，再派一个新的 Tester 检查合进去的结果。 |

## Workflow 与子 Workflow

用 Python 把协作步骤写成固定流程，以及内置的 Duo、Weave、Reconstruct 怎么用。

| 指南 | 读完你能做到 |
| --- | --- |
| [运行一个工作流（Workflow）](topics/workflow/run-registered-workflow.md) | 用命令行或 Python 找到一个工作流，交给它一项任务，然后查看结果。 |
| [工作流就是普通 Python：编程模型入门](topics/workflow/workflow-programming-model.md) | 工作流是一个普通的异步 Python 函数，流程用 Python 写，调用 Agent 用 ctx。 |
| [工作流能做什么、不能做什么](topics/workflow/workflow-capabilities-boundaries.md) | 判断你的需求用普通 Python 就能写、要用 OC 的 ctx 功能，还是要自己搭配外部服务。 |
| [编写第一个自定义 Workflow](topics/workflow/author-custom-workflow.md) | 在工作区 workflows/ 下写一个带 @workflow 的异步函数，就能被 OC 发现并按名称运行。 |
| [Workflow 公开 API 速查](topics/workflow/workflow-public-api-map.md) | 按“我要做什么”查到对应的 SDK 入口、ctx 方法、工具和结果类型。 |
| [在哪一层调整 Workflow：从 CLI 参数到单次调用回调](topics/workflow/workflow-control-granularity.md) | 一个控制需求该放在 CLI 参数、SDK 运行设置、ctx 单次调用，还是 RunControl 回调里。 |
| [Workflow 里的数据怎么传：输入、步骤结果、文件与输出](topics/workflow/workflow-dataflow-structured-io.md) | 说明 args 从哪来、上一步结果怎样交给下一步、Agent 怎样返回 JSON，以及最终输出放在哪。 |
| [在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](topics/workflow/workflow-agent-calls.md) | 每次调用开一个新会话；只要结果用 ctx.agent，要停止原因、用量和清理状态用 ctx.agent_run。 |
| [在 Workflow 中并行处理多项任务](topics/workflow/workflow-parallel-pipeline.md) | 用 ctx.parallel 同时跑一批独立任务，用 ctx.pipeline 让每项依次过多个阶段，并用两个并发上限控制规模。 |
| [Agent、Team 和 Workflow 怎么互相调用](topics/workflow/workflow-composition-hierarchy.md) | 讲清楚 Agent、Team、Workflow 三种运行方式各管什么，以及它们嵌套使用时预算和停止由谁负责。 |
| [复用子 Workflow，并在候选环境里试改动](topics/workflow/compose-workflow-candidates.md) | 把写好的异步流程拼成更大的流程；想先隔离着试，就放进候选环境，检查满意后再采用改动。 |
| [Workflow 出错了：失败、重试与恢复](topics/workflow/workflow-failure-recovery-lifecycle.md) | 某一步失败时先弄清是哪一层出的错，检查会话、工作区和产物，再决定返回部分结果、重试还是停下保留现场。 |
| [测试和调试你写的 Workflow](topics/workflow/workflow-testing-debugging.md) | 从最小的可运行例子开始，依次检查分支逻辑、数据传递、工具执行和最终任务结果。 |
| [让 Team 成员调用已注册的 Workflow](topics/workflow/team-run-workflow.md) | 给某个角色加上 run_workflow 工具，成员就能在自己的环境里同步运行一个已注册的 Workflow（如 Duo）并拿到结果。 |
| [让成员自己写并运行子 Workflow](topics/workflow/member-authored-workflow.md) | Team 成员用 manage_workflow 启动自己写的 Python 文件作为子运行，之后按 id 查询、等待、取结果或停止。 |
| [用 Duo 跑两个候选方案并选出一个](topics/workflow/duo-workflow.md) | Duo 是内置 Workflow：让两个 coder 在各自的候选环境里依次完成同一任务，按改动和测试证据选出一个，再把它采用到源工作区。 |
| [用 Weave 分组接力完成任务并检查交付](topics/workflow/weave-workflow.md) | Weave 是内置 Workflow：把任务拆成有依赖的组，每组开一个新会话在同一工作区接力，用真实检查命令判定交付，失败时做有限轮修复。 |
| [用 Reconstruct 从输入输出反推程序](topics/workflow/reconstruct-workflow.md) | Reconstruct 是内置 Workflow：反复向一个可查询的隐藏程序要输入输出样例，让 Agent 写出行为相同的 candidate.py。 |

## 预算、上下文与运行恢复

控制花多少 token、跑多久，任务卡住或中断后怎么办。

| 指南 | 读完你能做到 |
| --- | --- |
| [上下文窗口与自动压缩](topics/operations/context-compaction.md) | OC 在接近窗口上限时自动缩减旧工具结果、移除旧交换组并生成总结；这里说明怎么确认窗口、调整策略和排查溢出。 |
| [设置 token 预算、步数与运行并发](topics/operations/token-step-concurrency-limits.md) | OC 用 budget（累计 token）、max_steps 和 concurrency 分别限制运行；团队从共享池分额度，还可以用软额度回调按进展放宽。 |
| [超时、重试与请求卡住诊断](topics/operations/timeouts-retries.md) | OC 对连接、首个流事件、流空闲、单次请求、整次任务和工具执行分别设超时；先找到停在哪一层，再调那一层，并了解重试何时停止。 |
| [保存会话与恢复历史](topics/operations/session-save-resume.md) | OC 终端团队自动保存成员会话；用 --session 恢复主会话历史，累计 token 和步数随之恢复，未完成的协程不会续跑。 |
| [停止任务并确认清理完成](topics/operations/cancellation-cleanup.md) | 用 Ctrl+C、task.cancel() 或 timeout 停止运行，再通过 metrics 中的清理字段确认会话、环境和用量已经收尾。 |
| [检查生效配置与排查启动故障](topics/operations/configuration-diagnostics.md) | 按报错阶段选检查入口：读客户端生效配置、离线查模型能力、检查团队和工作流加载、发一次真实请求测连接。 |

## 工具、Skill 与扩展

决定 Agent 能用哪些工具，以及怎样加入你自己的工具和规则。

| 指南 | 读完你能做到 |
| --- | --- |
| [选择 Agent 能使用的工具](topics/extensions/tool-selection.md) | 用 builtin_tools 挑选普通编码工具并设置输出上限；Team 协作工具只能在团队角色里声明。 |
| [把自己的函数接成工具](topics/extensions/custom-tools.md) | 实现 opencollab.tools.Tool 结构接口，把业务函数变成 Agent 或 Workflow 可调用的工具，并通过 runtime 在 Agent 所在环境里读写文件和跑命令。 |
| [使用与编写 Skill](topics/extensions/skills-on-demand.md) | 在工作区 skills/ 下放 SKILL.md，给角色加 use_skill 工具，模型按需把技能正文拉进会话。 |
| [在工具执行和生命周期中加入 Hook](topics/extensions/team-hooks.md) | 在团队 YAML 的 hooks 下配置命令，在工具调用前后或成员启动、结束时运行；Hook 只观察，失败只留警告，不会拦截工具。 |
| [安全地查找读取和修改文件](topics/extensions/file-editing-safe-workspace.md) | OC 内置文件工具（file_read、grep、file_write、apply_patch、git_diff）的参数和编辑模式，以及路径越界、符号链接、覆盖保护等报错的含义。 |
| [管理终端权限并确认命令结果](topics/extensions/shell-permissions-execution.md) | bash 工具在 Agent 绑定的环境里执行；能否在宿主运行由 --yolo、--allow-local-child-shell、allow_unisolated_shell 和 headless 决定，结果要看退出状态和输出。 |

## 结果、轨迹与评测

任务跑完后，去哪里看结果、看过程、算费用，怎样确认它真的做对了。

| 指南 | 读完你能做到 |
| --- | --- |
| [读取运行结果与判断任务是否完成](topics/observability/run-results.md) | 看懂 SDK 返回的 RunResult，处理 RunError，并用测试确认任务真的做好了。 |
| [找到运行记录、会话和交付文件](topics/observability/artifacts-and-history.md) | 任务跑完后，去哪个目录找结果、对话记录和执行日志，哪些文件可以放心删。 |
| [查看 Agent 做了什么](topics/observability/trace-and-events.md) | 用轨迹文件事后复盘，或用事件回调实时观察，找出 Agent 调了什么模型、跑了什么工具、为什么停下。 |
| [统计 token、缓存命中和费用](topics/observability/token-cache-cost.md) | 开启用量账本，正确累加 token、算缓存命中率和估算花费，不重复也不漏算。 |
| [用实际测试验证 Agent 的交付](topics/observability/native-test-evidence.md) | 让 Agent 真的跑项目测试并留下记录，读懂哪些算通过、哪些不算、哪些已过期。 |
| [检查团队是否真的按分工协作](topics/observability/collaboration-adherence.md) | 对比团队文件里写的分工和运行时实际发生的事，看成员有没有按约定干活。 |
| [用 OCE 评测 OC 的 Agent、Team 和 Workflow](topics/observability/evaluation-boundary.md) | OC 负责跑任务，OCE 负责出题和打分。想跑 benchmark 先弄清各自管什么。 |

## 架构、集成与维护

想深入了解内部原理、接入自己的服务或升级版本时看这里。

| 指南 | 读完你能做到 |
| --- | --- |
| [一次任务是怎么跑起来的](topics/architecture/runtime-execution-model.md) | 跟着一次任务走一遍：OpenCollab 怎样把消息发给模型、执行模型要求的工具，并把结果送回模型，直到任务结束。 |
| [Session 怎样保存进度和继续执行](topics/architecture/session-state-lifecycle.md) | 看懂会话里存了什么、处在哪个阶段、为什么停下，以及恢复后会从哪里接着跑。 |
| [OpenCollab 管什么，我的需求该从哪里扩展](topics/architecture/extension-and-evaluation-boundaries.md) | 帮你判断一个业务需求该写成工具、Workflow、团队配置还是执行环境，并说清 OpenCollab 和评测项目 OCE 各管哪部分。 |
| [把 OpenCollab 接进自己的服务](topics/architecture/service-integration-lifecycle.md) | 在后端服务里调用 SDK：一个请求对应一次运行，处理 RunResult 和 RunError，设好超时，并按谁创建谁清理的规则释放环境和模型客户端。 |
| [升级 OpenCollab 后怎样迁移和验证](topics/architecture/upgrade-existing-integration.md) | 先记下当前装的是哪个版本，再按迁移记录核对默认行为和接口变化，最后用原来的任务实际跑一遍。 |
| [遇到问题怎么反馈、求助或提建议](topics/architecture/report-problem-and-request-help.md) | 选对 GitHub 反馈入口，附上最小复现和脱敏后的记录，让维护者能快速重现你遇到的问题。 |
