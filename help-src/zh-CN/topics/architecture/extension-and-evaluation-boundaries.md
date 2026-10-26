---
{
  "id": "extension-and-evaluation-boundaries",
  "title": "OC 的边界：需求该从哪里扩展，评测由谁负责",
  "category": "architecture",
  "summary": "OC 管什么、不管什么：一个业务需求该写成工具、Workflow、团队配置还是执行环境；以及 OC 和评测项目 OCE 怎样分工、一次评测要保留哪些材料。",
  "aliases": [
    "OpenCollab-Eval",
    "ports",
    "adapters",
    "bootstrap",
    "LLMPort",
    "ToolPort",
    "EnvironmentPort",
    "workflow",
    "Skill",
    "跑题",
    "Terminal-Bench",
    "DeepSWE",
    "SWE-bench",
    "官方评分",
    "跑分"
  ],
  "keywords": [
    "架构",
    "扩展点",
    "OC 与 OCE",
    "扩展入口",
    "自定义工具",
    "OpenCollab-Eval 分工",
    "评测",
    "OCE",
    "Benchmark"
  ],
  "questions": [
    "把业务需求放到合适的扩展位置，并理解 OpenCollab 与评测项目各自维护什么？",
    "我想让 Agent 查工单，该写工具还是写 Workflow？",
    "OpenCollab 和 OpenCollab-Eval 有什么区别？",
    "运行状态 completed 是不是说明任务做对了？",
    "理解 OC 与 OCE 的分工，找到正式运行和收集评分结果的入口？",
    "想用 SWE-bench 测我的 Team，从哪里开始？",
    "运行显示 completed，算不算题目通过？"
  ],
  "related": [
    "custom-tools",
    "execution-environments",
    "service-integration-lifecycle",
    "run-results",
    "collaboration-adherence"
  ],
  "verified_ref": "0c47f74a"
}
---

# OC 的边界：需求该从哪里扩展，评测由谁负责

OC 负责的事情很明确：跑 agent loop，执行 Agent、Team 和 Workflow，管理会话、工具、预算、取消和运行记录。**任务目标、业务输入和验收方式由调用方提供**，OC 不替你判断“做对了没有”；正式的 benchmark 评分则交给独立的评测项目 OCE。这篇帮你做两个判断：一个业务需求该从哪个入口扩展；跑评测时，OC 和 OCE 各管什么。

## 需求对应哪个扩展入口

| 你想做的事 | 用哪个入口 | 你需要写什么 |
| --- | --- | --- |
| 让 Agent 查询或操作业务系统 | `opencollab.tools.Tool`，见 [自定义工具](../extensions/custom-tools.md) | 参数 schema、实际执行逻辑和返回结果 |
| 固定步骤顺序、分支和重试 | `@workflow` 与 `WorkflowContext`，见 [编写 Workflow](../workflow/workflow-programming-model.md) | 一段异步 Python 控制逻辑 |
| 安排几个成员的角色和谁能联系谁 | 团队 YAML，见 [Team 入门](../team/configure-team.md) | 提示词、工具、角色与拓扑 |
| 接入专用的执行服务 | Environment 与 EnvironmentPool，见 [使用自己的执行环境](../isolation/execution-environments.md) | 文件、命令、申请、交付与清理 |
| 给 Agent 准备按需加载的说明材料 | 技能目录和 `use_skill`，见 [Skill](../extensions/skills-on-demand.md) | 可加载的指令与配套资料 |
| 更换或包装模型调用方式 | SDK 的模型客户端接入，见 [把 OC 接进自己的服务](service-integration-lifecycle.md#注入自己的模型客户端) | 符合模型调用协议的实现 |

三个最常见的情况：

1. 只需要让 Agent 多一项能力（比如查工单）：实现一个工具，传给 `agent(tools=[tool])`。这是最常见的扩展。
2. 要严格规定“查询、实施、测试、交付”的先后次序：用 Workflow 把这些步骤写成 Python。
3. 现有工具和 SDK 已经够用：直接在你的应用里组合调用，不需要扩展。

模型能用哪些工具，由你在调用时明确给出，模型不会自己多拿工具。举个例子：一个工单处理服务可以用自定义工具暴露查询能力，再用 Workflow 规定何时提取工单、何时调用 Agent、何时保存业务结果。这些业务逻辑都由你维护。

## 谁负责判断“做对了没有”

OC 提供模型交互、工具执行、预算、取消和运行记录。任务是否真的完成，要由你的验证方式判断：修代码的任务，看项目测试结果和实际改动；调用业务服务的任务，用该服务自己的查询接口确认最终状态。见 [判断任务是否真的完成](../observability/run-results.md)。

## 读取配置和已保存材料的公开入口

普通应用从包根导入这些名字：`OpenCollab`、`RunResult`、`RunError`、`workflow`、`RunControl`、`RunEvent`、`BudgetSnapshot` 和 `BudgetDecision`。其他能力按需从公开模块导入：

| 想读取或复用什么 | 公开模块或资料 | 去哪里看 |
| --- | --- | --- |
| 团队声明与上下文策略 | `opencollab.teams` | [检查团队是否真的按分工协作](../observability/collaboration-adherence.md) |
| 原生测试结果与 Git 补丁路径 | `opencollab.tools`、`opencollab.patches` | [测试证据](../observability/run-results.md#用测试证据确认做对了) |
| 模型能力与有效的 Agent 配置 | `opencollab.models`、`opencollab.profiles` | [离线查看模型能力](../models/configuration-precedence.md#离线查看模型能力) |
| 已安装的协作流程 | `opencollab.builtin_workflows` | [运行一个 Workflow](../workflow/run-registered-workflow.md) |
| 单次运行的过程与开销 | 轨迹、`RunEvent`、用量账本 | [运行记录](../observability/artifacts-and-history.md) 和 [用量与费用](../observability/token-cache-cost.md) |

这些入口用来在本地检查配置和已保存的材料，真正运行任务还是用 Agent、Team 或 Workflow。集成代码请从 `opencollab.tools`、`opencollab.environments` 和 `opencollab.workflows` 这些公开模块导入，升级时更好维护。

## 为什么核心代码通过接口使用外部能力

代码按层分开，依赖方向是 `bootstrap → adapters → application → domain`：

| 层 | 放什么 |
| --- | --- |
| domain | 纯状态和值对象 |
| application | 运行用例，只依赖“端口”（接口定义） |
| adapters | 模型、工具、环境和存储的具体实现 |
| bootstrap | 把这些实现组装起来 |

好处是：接入一个新的外部执行服务时，核心代码只需要知道它能申请环境、读写文件、执行命令；怎么连接、资源 ID 是什么、怎么清理，都写在你的适配实现里。仓库的架构规则见 [AGENTS.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/AGENTS.md)。

## OC 和 OCE 怎样分工

OCE 是 OpenCollab-Eval，OC 的评测项目：

| 项目 | 负责什么 |
| --- | --- |
| OC（OpenCollab） | 运行 Agent、Team、Workflow，产出会话与交付记录 |
| OCE（OpenCollab-Eval） | benchmark 数据、实验配置、批量运行、远端执行、候选评分和实验分析 |

OCE 通过 OC 的公开 SDK 发起运行、拿到会话和交付记录，再用 benchmark 自己的验证器打分。所以 `RunResult.status == "completed"` 不等于题目通过。任务提示词、数据构造和正式评分，要按评测项目对应的版本来理解。

### 怎么开始一次评测

1. 确定要比较的 OC 运行方式：单 Agent、团队文件，还是 Workflow。
2. 自定义 Workflow 先在普通项目上跑通，确认输入、输出和交付行为正常。
3. 到 OCE 配置数据集和验证器。
4. 评测器通过 SDK 调用 OC 时，传入模型、预算、期限、执行环境和产物位置。
5. 正式运行前，确认两个仓库的版本和要用的功能。

当前的评测命令与数据集设置以 [OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval#readme) 为入口，具体 benchmark 的安装步骤和评分参数见对应的 OCE 指南。OC 这边的 [评测运行时文档](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/evaluation-runtime.md) 说明 SDK 的预算、取消、清理与结果接口。项目里历史实验的命令，要对照当时的提交来读。

### “跑完了”不等于“做对了”

一次评测通常包括：任务输入、初始仓库或镜像、模型与运行配置、生成的候选、原始验证结果、用量记录。评测里有几个不同的阶段，结果里要分开看：候选生成完成 → 验证器启动 → 测试通过 → 最终成绩被采用。OC 的 `RunResult` 只说明第一段怎样结束。比如：运行正常结束，却留下了失败的测试；或者基础设施出错，验证根本没能启动。

### 让比较公平

模型提供方、角色提示词、工具集合、执行环境、验证器资源，都会影响成绩。比较不同协作方案时，把这些设置写清楚，并保留能对应到每道题的记录。

### 代码怎样交给验证器

不同评测适配器的交付方式可能不同：补丁、最终工作树，或完整的容器状态。选哪种要遵守任务规则，任务涉及未提交文件、启动的服务或容器内产物时尤其要注意。候选具体走哪条路径送进验证器，要到 OCE 对应的适配器里核对。

基础设施故障导致没评上分时，在恢复后用相应的恢复流程把已保存的候选重新评分；是否允许重新生成候选，由实验协议决定。建议保留原尝试、恢复过程和最终结果，方便解释前后差异。

### 研究结果去哪里看

- [benchmark 结果说明](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/results.md) 和 [Adherence 说明](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/adherence.md)。README 里的图表由 `docs/benchmark-results.json` 经 `scripts/generate_benchmark_assets.py` 生成，数据对应已记录的实验。
- 想分析你自己的新运行，参考 [OCE 实验工具](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/experiment/README.md) 和 [原始研究材料](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/experiment/README.md) 选择报告方式，并保留任务、模型、角色、工具、预算、上下文和实际验证结果。

## 常见问题

- **能接入通用记忆或 RAG 吗？** 当前的上下文来源包括角色、团队、技能、派发任务和项目概览。通用记忆或 RAG 需要你自己接入，并决定内容从哪来、保留多久。
- **能接虚拟机吗？** 内置的 `vm-group` 和 `vm-session` 已支持已有的 Linux VM，见 [在现有 Linux VM 中运行任务](../isolation/vm-environment-extension.md)；更专用的后端可以通过环境提供器扩展。

## 延伸阅读

- 源码依据：[AGENTS.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/AGENTS.md) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/evaluation-runtime.md) · [docs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/README.md) · [scripts/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/scripts/README.md) · [docs/model-inspection.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/model-inspection.md)
- 相关指南：[自定义工具](../extensions/custom-tools.md) · [使用自己的执行环境](../isolation/execution-environments.md) · [把 OC 接进自己的服务](service-integration-lifecycle.md) · [判断任务是否真的完成](../observability/run-results.md)
