---
{
  "id": "extension-and-evaluation-boundaries",
  "title": "OpenCollab 管什么，我的需求该从哪里扩展",
  "category": "architecture",
  "summary": "帮你判断一个业务需求该写成工具、Workflow、团队配置还是执行环境，并说清 OpenCollab 和评测项目 OCE 各管哪部分。",
  "aliases": [
    "OpenCollab-Eval",
    "ports",
    "adapters",
    "bootstrap",
    "LLMPort",
    "ToolPort",
    "EnvironmentPort",
    "workflow",
    "Skill"
  ],
  "keywords": [
    "架构",
    "扩展点",
    "OC 与 OCE",
    "扩展入口",
    "自定义工具",
    "OpenCollab-Eval 分工"
  ],
  "questions": [
    "把业务需求放到合适的扩展位置，并理解 OpenCollab 与评测项目各自维护什么？",
    "我想让 Agent 查工单，该写工具还是写 Workflow？",
    "OpenCollab 和 OpenCollab-Eval 有什么区别？",
    "运行状态 completed 是不是说明任务做对了？"
  ],
  "related": [
    "runtime-execution-model",
    "custom-environment-providers",
    "service-integration-lifecycle"
  ],
  "verified_ref": "0c47f74a"
}
---

# OpenCollab 管什么，我的需求该从哪里扩展

OC（OpenCollab）负责跑 agent loop：执行 Agent、Team 和 Workflow，管理会话、工具、预算、取消和产物记录。任务目标、业务输入和验收方式由调用方提供，OC 不替你判断"做对了没有"。扩展时先想清楚你要的是"多一种操作""改协作步骤"还是"换执行后端"，再按下表选入口。最常见的情况只需写一个 `Tool`，传给 `agent(tools=[tool])`。

## 先看结论：需求对应哪个扩展入口

| 你想做的事 | 用哪个入口 | 你需要写什么 |
| --- | --- | --- |
| 让 Agent 查询或操作业务系统 | `opencollab.tools.Tool` | 参数 schema、实际执行逻辑和返回结果 |
| 固定步骤顺序、分支和重试 | `@workflow` 与 `WorkflowContext` | 一段异步 Python 控制逻辑 |
| 安排多个成员的角色和谁能找谁说话 | Team YAML | 提示词、工具、角色与拓扑 |
| 接入专用的执行服务 | Environment 与 EnvironmentPool | 文件、命令、申请、交付与清理 |
| 给 Agent 准备按需加载的说明材料 | 技能目录和工具 | 可加载的指令与配套资料 |
| 更换或包装模型调用方式 | SDK 的模型客户端接入 | 符合模型调用协议的实现 |

OC 名词对照：

- Team：用 YAML 声明角色，由模型在运行中自主派发和交接的多 agent。拓扑指成员之间谁能给谁派任务、发消息。
- Workflow：用异步 Python 显式编排 agent 调用，顺序、分支、重试由代码决定。
- Environment：工具实际读写文件、执行命令的后端，比如本机目录或容器。EnvironmentPool 负责按任务申请和释放 Environment。

## 怎么选：三个常见情况

1. 如果你只需要让 Agent 多一项能力（比如查工单），就实现一个工具，传给 `agent(tools=[tool])`。
2. 如果你要严格规定"查询、实施、测试、交付"的先后次序，就用 Workflow 把这些步骤写成 Python。
3. 如果现有工具和 SDK 已经够用，就直接在你的应用里组合调用，不需要扩展。

模型能用哪些工具，由你在调用时明确给出。模型不会自己多拿工具。

## 谁负责判断"做对了没有"

OpenCollab 提供这些：模型交互、工具执行、预算、取消和产物记录。

任务是否真的完成，要由你的验证方式判断：

- 修代码的任务：看项目测试结果和实际改动。
- 调用业务服务的任务：用该服务自己的查询接口确认最终状态。

举例：一个工单处理服务可以用自定义工具暴露查询能力。再用 Workflow 规定何时提取工单、何时调用 Agent、何时保存业务结果。这些业务逻辑都由你维护。

## 读取配置和已保存材料

普通应用从包根导入这些名字：`OpenCollab`、`RunResult`、`RunError`、`workflow`、`RunControl`、`RunEvent`、`BudgetSnapshot` 和 `BudgetDecision`。其它能力按需从下表的公开模块导入。升级时再核对对应版本的接口。

| 想读取或复用什么 | 公开模块或资料 | 去哪里看 |
| --- | --- | --- |
| 团队声明与上下文策略 | `opencollab.teams` | [检查团队是否真的按分工协作](../observability/collaboration-adherence.md) |
| 原生测试结果与 Git 补丁路径 | `opencollab.tools`、`opencollab.patches` | [用实际测试验证 Agent 的交付](../observability/native-test-evidence.md) |
| 模型能力与有效 profile | `opencollab.models`、`opencollab.profiles` | [模型检查指南](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/model-inspection.md) |
| 已安装的协作流程 | `opencollab.builtin_workflows` | [运行一个工作流（Workflow）](../workflow/run-registered-workflow.md) |
| 单次运行的过程与开销 | trace、`RunEvent`、提供方用量账本 | [执行轨迹](../observability/trace-and-events.md)和[用量统计](../observability/token-cache-cost.md) |

这些入口用来在本地检查配置和已保存的材料。真正运行任务，还是用 Agent、Team 或 Workflow。研究批次的尝试选择和统计报告，从 OCE 的实验工具进入。

## OC 与 OCE 怎样分工

OCE 是 OpenCollab-Eval，OpenCollab 的评测项目。

| 项目 | 负责什么 |
| --- | --- |
| OC（OpenCollab） | 运行 Agent、Team、Workflow，产出会话与交付记录 |
| OCE（OpenCollab-Eval） | benchmark 数据、实验配置、运行与远端执行 |

OCE 调用 OC 的公开运行接口，拿到会话和交付记录。任务提示词、数据构造和正式评分，要按评测项目对应的版本来理解。

注意：OC 返回 `completed` 只表示运行结束了。正式评测结果来自实际的验证过程。

研究结果的入口：

- [benchmark 结果说明](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/results.md)
- [Adherence 说明](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/adherence.md)

README 里的图表由 `docs/benchmark-results.json` 经 `scripts/generate_benchmark_assets.py` 生成，数据对应已记录的实验。

想分析你自己的新运行，参考 [OCE 实验工具](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/experiment/README.md)和[原始研究材料](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/experiment/README.md)选择报告方式。记得保留任务、模型、角色、工具、预算、上下文和实际验证结果。

## 进阶：为什么核心代码通过接口使用外部能力

代码按层分开，依赖方向是 `bootstrap → adapters → application → domain`：

| 层 | 放什么 |
| --- | --- |
| domain | 纯状态和值对象 |
| application | 运行用例，只依赖"端口"（接口定义） |
| adapters | 模型、工具、环境和存储的具体实现 |
| bootstrap | 把这些实现组装起来 |

好处是：接入一个新的外部执行服务时，核心代码只需要知道它能申请环境、读写文件、执行命令。怎么连接、资源 ID 是什么、怎么清理，都写在你的适配实现里。

集成代码请从公开模块 `opencollab.tools`、`opencollab.environments` 和 `opencollab.workflows` 导入，这样升级包版本时更好维护。

## 常见问题

- 能接入通用记忆或 RAG 吗？当前的上下文来源包括角色、团队、技能、派发任务和项目概览。通用记忆或 RAG 需要你自己接入，并决定内容从哪来、保留多久。
- 能接虚拟机吗？环境提供器可以扩展专用后端。内置 `vm-group` 和 `vm-session` 已支持已有 Linux VM，配置与恢复步骤见 [VM 指南](../isolation/vm-environment-extension.md)。

## 下一步读什么

- 扩展原则：[仓库架构规则](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/AGENTS.md)和[包指南](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md)
- 接入执行环境：[接入自己的执行环境](../isolation/custom-environment-providers.md)
- 接入业务服务：[把 OpenCollab 接进自己的服务](service-integration-lifecycle.md)
