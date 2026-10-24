---
{
  "id": "team-run-workflow",
  "title": "让 Team 成员调用已注册的 Workflow",
  "category": "workflow",
  "summary": "给某个角色加上 run_workflow 工具，成员就能在自己的环境里同步运行一个已注册的 Workflow（如 Duo）并拿到结果。",
  "aliases": [
    "队友跑工作流",
    "Team → Workflow",
    "工作流工具",
    "调用 Duo",
    "调用 Weave",
    "PR237",
    "嵌套工作流",
    "让 Team 成员调用已注册 Workflow"
  ],
  "keywords": [
    "run_workflow",
    "Team 调用 Workflow",
    "子 Workflow",
    "注册工作流",
    "workflow-agent"
  ],
  "questions": [
    "Team 成员怎样调用 Duo？",
    "run_workflow 会等到运行结束吗？",
    "子 Workflow 花费算到谁的预算？",
    "run_workflow 能传哪些参数？",
    "Team 成员怎样发现 Duo、Weave 或自定义流程的输入？"
  ],
  "related": [
    "run-registered-workflow",
    "member-authored-workflow",
    "compose-workflow-candidates",
    "wait-for-team-results",
    "workflow-composition-hierarchy"
  ],
  "verified_ref": "0c47f74a"
}
---

# 让 Team 成员调用已注册的 Workflow

`run_workflow` 是 OC 给 Team 成员用的一个工具：成员传入已注册 Workflow 的名称和输入，工具调用会阻塞到整个 Workflow 结束，再一次性返回结果。子 Workflow 跑在调用成员自己的环境里，token 记在该成员的额度上。

典型用法：Team 里的修复负责人在需要时启动 Duo，读完交付结果再继续协作。如果成员要运行自己现写的 Python 文件，或者想启动后不等待，改用 `manage_workflow`，见[让成员自己写并运行子 Workflow](member-authored-workflow.md)。

## 最小用法

1. 在 Team 配置里，把 `run_workflow` 加到需要它的角色上。保存为 `team.yaml`，通过 Team 运行入口使用：

```yaml
entry: coordinator
roles:
  coordinator:
    prompt: 先用 run_workflow describe 读取输入说明，再按任务调用流程并核对交付结果。
    tools: [file_read, bash, run_workflow]
```

2. 确认可用的 Workflow 名称：

```bash
opencollab workflow list --workspace ...
```

注册来源和普通 Workflow 入口相同，包括内置工作流和工作区目录。运行时会根据已发现的工作流，自动生成 `name` 的可选值和说明。

3. 成员先读取所选 Workflow 的输入说明。

```json
{"action": "describe", "name": "duo"}
```

返回 `name`、`description`、`inputs` 和 `phases`。`inputs` 来自流程函数的 docstring，缺少 docstring 时采用流程说明。自定义作者在这里提供必填字段、类型和输入示例，成员按所选流程的说明构造 `args`。Duo、Weave 与 Reconstruct 各自接受不同输入。

4. 成员按说明执行流程，例如下面的 Duo 调用。

```json
{
  "action": "run",
  "name": "duo",
  "args": {"goal": "修复配置读取错误并验证"},
  "budget": 50000,
  "concurrency": 2
}
```

## 参数

| 字段 | 要求 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `action` | 可选，`describe` 或 `run` | `run` | 读取说明或执行流程 |
| `name` | 必填，注册名称 | 无 | 选择要跑的 Workflow |
| `args` | `run` 时必填，JSON 对象 | 无 | 原样作为 Workflow 输入 |
| `budget` | 可选，正整数 | 成员剩余额度 | 本次上限，与剩余额度取较小值 |
| `concurrency` | 可选，正整数 | 4 | 限制 Workflow 内同时活跃的 Agent 会话数 |

工具开放上表中的字段，`budget` 和 `concurrency` 用于执行阶段。SDK 的其他运行参数在宿主调用或 Workflow 源代码里设置。

## 运行环境和额度

子 Workflow 借用调用成员当前的：

- 执行环境和工作目录
- 有效模型配置
- 权限策略

环境的创建和释放仍由 Team 运行管理。

额度规则：

- 子 Workflow 内部会话的 token 通过 usage 事件记到调用成员名下。成员之后的请求会看到扣减后的剩余额度。
- 成员额度已经用完时，工具直接返回额度耗尽错误。

权限规则：

- Workflow 作者为每次 `ctx.agent` 指定工具，成员的权限策略照样参与执行检查。
- Workflow 也继承父成员的步骤上限。

## 读取返回值

`run_workflow` 会一直等到子 Workflow 跑完。如果你需要启动后继续协调、过一会儿再查进度，改用 `manage_workflow`。

返回值是 JSON 文本：

| 字段 | 含义 |
| --- | --- |
| `output` | Workflow 入口函数的返回值 |
| `tokens` | 本次消耗 |
| `stop_reason` | 停止原因，例如额度耗尽 |
| `agent_failures` | 内部会话的失败信息 |
| `artifacts` | 保存目录 |

工具成功返回不等于任务完成。还要读 Workflow 自己的交付字段，例如 Duo 的 `adopted`、Weave 的 `delivery_ok`，并检查实际文件或应用的验证结果。

记录保存位置：Team 设置了保存目录时，子 Workflow 的记录在该目录下单独的 `workflow-agent-...` 子目录里。里面的会话、工作流清单和可选轨迹，可以用来查某个成员调用了哪个具体角色。模型输出帮你理解决策过程，工具回执和交付检查才说明实际执行了什么。

## 进阶

### 在被调用的 Workflow 里继续组合

被调用的 Workflow 可以把同一个 `ctx` 传给异步子函数，顺序或并行安排更多会话。需要独立候选环境时，用 `ctx.candidate_workflow`。这些调用都仍在当前 Workflow 的预算和环境管理之下。

### 能不能在 Workflow 里再起 Team

Team 的成员创建操作仍属于同一个 Team。Workflow 的 `ctx` 只提供 Agent 会话、集合和候选操作。要启动独立 Team，用 SDK 入口 `OpenCollab.team`。宿主另外启动 Team 时，需要自己管理它的额度、取消信号和产物。见[Agent、Team 和 Workflow 怎么互相调用](workflow-composition-hierarchy.md)。

### 取消

用户停止父成员时，取消信号会打断正在等待的 Workflow。工具会等已有 Workflow 清理完，再返回中断提示。

排查停止问题时，同时看父成员记录和子 Workflow 产物，确认停止原因、已写入的文件和清理结果。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md#L263)、[opencollab/bootstrap/team_workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/team_workflow.py#L25)、[tests/interfaces/test_team_workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/tests/interfaces/test_team_workflow.py#L24)。

## 相关章节

[运行一个工作流（Workflow）](run-registered-workflow.md)、[让成员自己写并运行子 Workflow](member-authored-workflow.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[等待队友结果并读懂团队状态](../team/wait-for-team-results.md)。
