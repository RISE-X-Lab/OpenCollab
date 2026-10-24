---
{
  "id": "collaboration-adherence",
  "title": "检查团队是否真的按分工协作",
  "category": "observability",
  "summary": "对比团队文件里写的分工和运行时实际发生的事，看成员有没有按约定干活。",
  "aliases": [
    "有没有真协作",
    "团队依从性",
    "角色没参与",
    "分工执行",
    "拓扑统计",
    "opencollab.teams",
    "declared_role_names",
    "declared_role_tools",
    "declared_role_budgets",
    "declared_context_policy",
    "成员偷懒",
    "主成员包办"
  ],
  "keywords": [
    "协作统计",
    "Adherence",
    "message_sent",
    "message_refused"
  ],
  "questions": [
    "读懂声明的团队与实际运行轨迹之间的差异，并找到分析入口？",
    "怎么知道 Team 里每个角色是不是真的参与了？",
    "主成员是不是把活全自己干了？"
  ],
  "related": [
    "configure-team",
    "trace-and-events",
    "evaluation-boundary"
  ],
  "verified_ref": "0c47f74a"
}
---

# 检查团队是否真的按分工协作

OC 的 Team 是由模型自主派发、按 YAML 声明角色和通信拓扑的多 Agent。声明的分工不保证被执行：主成员可能派活给 Coder 后自己写完全部代码，最终也不采用 Coder 的改动。Adherence 指对比 Team 声明与运行轨迹，检查实际协作是否符合声明。OC 提供离线读取声明的 `opencollab.teams` 接口和带成员身份的轨迹事件，批量统计与报告在 OCE 侧。

## 第一步：读出团队声明

`opencollab.teams` 提供离线读取团队声明的接口，不用真的启动运行。下面示例读取仓库自带的团队文件，你也可以换成自己的 YAML 路径。读取时会解析配置和关联的提示词文件，适合在发起运行前把实验条件存下来。

```python
from opencollab.teams import (
    declared_context_policy,
    declared_role_budgets,
    declared_role_names,
    declared_role_profiles,
    declared_role_tools,
)

team_file = "configs/team.collab.yaml"
print(declared_role_names(team_file))     # 角色名册，入口角色排第一
print(declared_role_tools(team_file))     # 各角色声明的工具
print(declared_role_budgets(team_file))   # 各角色预算
print(declared_role_profiles(team_file))  # 各角色配置
print(declared_context_policy(team_file)) # 上下文策略
```

返回结果怎么理解：

- 角色名称已经归一化，入口角色排在名册第一位。
- 工具名称按文件里的声明顺序返回。实际启动时，还会再做能力筛选、补上后备工具组合，所以运行时的工具不一定和这里完全一样。
- 预算结果是空字典，说明团队使用调用方提供的共享额度规则。如果 YAML 里写了每个角色的额度，就要求名册固定。
- 上下文结果包含 `policy` 与 `tool_result_budget`。
- 想按提示词内容分组，可用同模块的 `declared_role_prompt_digests`，它读取包含 `prompt_file` 内容的提示词摘要。

也可以在命令行查看入口角色、声明工具和通信边：

```bash
opencollab team show --team-config configs/team.collab.yaml
```

模型和完整角色配置要从选定的 YAML 读取，见 [Team 入门：配置你的多 Agent 团队](../team/configure-team.md)。分析时保留完整的声明名册，并另外记下哪些成员实际参与了。

## 第二步：在轨迹里找实际行为

沿着运行标识，读取模型调用、工具操作、派发、消息和结果。对每个角色分别看三件事：

1. 它有没有收到任务。
2. 它有没有开始执行。
3. 它的改动有没有被上级任务采用。

通信要看两类事件：

| 事件 | 含义 |
| --- | --- |
| `message_sent` | 消息已被接受并进入队列 |
| `message_refused` | 发送被拒绝，额外带 `reason` |

两者的 payload 都有 `from_aid`、`from_role`、`to_aid`、`to_role`。分析通信时，成功送达和被拒绝的尝试都要保留。

再往下查：

- 接收方会话里的消息和后续模型调用，能说明它是否真的用上了这条消息。
- 按 `message_id` 关联持久化消息，可以区分同一对角色之间的多次交接。
- `commit_refs` 保存消息携带的 Git 对象引用，用来核对候选代码是从谁传给谁的。

事件的读取方法见 [查看 Agent 做了什么](trace-and-events.md)。

## 常见问题查什么材料

| 想回答的问题 | 需要对照的材料 |
| --- | --- |
| 所有角色是否参与 | 声明名册、实际模型与工具事件 |
| 谁向谁派发或发送 | 拓扑声明、操作尝试和成功回执 |
| 角色是否越权干了别人的活 | 工具集合、提示词和实际执行 |
| 最终交付来自哪个候选 | 采用记录、候选补丁与最终目录 |
| 几次运行条件是否相同 | 模型、工具、预算与上下文配置 |

## 怎样算出依从性指标

1. 先定义"一次运行算符合声明"的判定条件。
2. 再按这个条件统计比例。

注意几点：

- 论文结果和你新做的应用实验，各自对应自己的配置和轨迹，不能混用。
- 想解释因果，或比较两种团队谁更好，还要结合任务质量、实验条件和研究说明。
- 单成员参与率、全部声明成员参与率、通信边覆盖率，各自回答不同的问题。用 OCE 汇总时，同时看指标的分母和缺失状态。

## 批量分析去哪里做

OC 负责保存通用的运行和协作记录。OCE（OpenCollab-Eval）负责实验配置、批量分析和报告。OC 的命令行从 `opencollab --help` 进入团队与工作流操作，离线读取声明用上面的 Python 接口。

批量研究从 [OCE 实验工具说明](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/experiment/README.md)进入。相关模块：

- [cell_report_rows](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/experiment/cell_report_rows.py)：解释轨迹和角色参与。
- [cell_report_statistics](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/experiment/cell_report_statistics.py)：汇总选定尝试、通信与角色统计，并计算相应区间。
- [cell_report_rendering](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/experiment/cell_report_rendering.py)：输出文字与 JSON。
- [batch_reporting](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/src/opencollab_eval/commands/batch_reporting.py)：维护重试、替换和撤回的选择。
- [experiment/analysis](https://github.com/RISE-X-Lab/OpenCollab-Eval/tree/main/experiment/analysis)：研究批次的原始分析脚本，按对应批次版本和条件使用。

不用 OCE 也行。应用可以用公开的 Team 配置读取接口加事件记录，搭自己的观察视图。建议先拿少量运行核对事件和角色对不对，再汇总整批。这样能早点发现漏记、重复累计和角色对应错误。

## 源码与进一步阅读

[docs/adherence.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/adherence.md) · [opencollab/teams.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/teams.py) · [opencollab/adapters/trace.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/trace.py) · [docs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/README.md)

[Team 入门：配置你的多 Agent 团队](../team/configure-team.md) · [查看 Agent 做了什么](trace-and-events.md) · [用 OCE 评测 OC 的 Agent、Team 和 Workflow](evaluation-boundary.md)
