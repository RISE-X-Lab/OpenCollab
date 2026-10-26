---
{
  "id": "workflow-composition-hierarchy",
  "title": "组合与嵌套：子流程、候选环境与互相调用",
  "category": "workflow",
  "summary": "把流程拼成更大的流程：普通子函数共享当前环境，候选 Agent 和候选子流程在隔离环境里试改动、检查后再采纳；以及 Agent、Team、Workflow 互相嵌套时预算和停止由谁负责。",
  "aliases": [
    "Workflow 里面可以再跑 Workflow 吗",
    "Workflow 可以调用 Team 吗",
    "Agent 能不能开团队",
    "Team 可以跑工作流吗",
    "子工作流能继续开 Agent 吗",
    "嵌套工作流",
    "复用工作流",
    "ctx.candidate_agent",
    "候选方案",
    "候选工作区",
    "共享上下文",
    "隔离候选",
    "candidate workflow",
    "组合子 Workflow 并采用候选结果"
  ],
  "keywords": [
    "Agent Team Workflow",
    "调用层级",
    "Workflow 组合",
    "run_workflow",
    "manage_workflow",
    "candidate_workflow",
    "DW 内多个 Agent",
    "vm-session",
    "worktree",
    "子 Workflow",
    "ctx.candidate_workflow",
    "CandidateRun",
    "ctx.adopt_candidate",
    "preserve_paths"
  ],
  "questions": [
    "Workflow 里面能再调用另一个 Workflow 吗？",
    "Team 成员怎样运行一个 Workflow？",
    "在 Workflow 里怎么启动一个 Team？",
    "嵌套运行时预算算谁的？",
    "Team 成员写出的 DW 怎样继续创建多个 Agent？",
    "Workflow 局部并发和模型共享请求容量由谁管理？",
    "VM 成员的子工作树位于哪里？",
    "怎么在一个 Workflow 里复用另一个 Workflow？",
    "怎么让 Agent 先在隔离环境里改代码，确认后再合并？",
    "candidate_workflow 和 candidate_agent 有什么区别？",
    "采用候选改动时发生冲突怎么办？"
  ],
  "related": [
    "workflow-agent-calls",
    "team-run-workflow",
    "candidate-delivery",
    "duo-workflow",
    "run-results"
  ],
  "verified_ref": "0c47f74a"
}
---

# 组合与嵌套：子流程、候选环境与互相调用

流程写大了，自然要拆分和复用。OC 里有两类“组合”：一类是在一个 Workflow 内部复用子流程，关键问题是**改动直接落在当前环境，还是先在隔离副本里试**；另一类是 Agent、Team、Workflow 三种运行方式互相嵌套，关键问题是**预算算谁的、谁负责停止、结果交给谁**。这篇依次讲这两类。

## 三种复用方式怎么选

| 你想要的 | 用什么 | 改动落在哪 |
| --- | --- | --- |
| 复用一段流程，直接在当前环境干活 | 把 `ctx` 传给另一个异步函数 | 当前工作区 |
| 只跑一个 Agent，先隔离着试 | `ctx.candidate_agent` | 候选环境 |
| 整套多步流程先隔离着试 | `ctx.candidate_workflow` | 候选环境 |

## 方式一：直接调用子函数

```python
async def summarize(ctx, args):
    return await ctx.agent(args['text'], label='summary', tools=[])

async def shared_flow(ctx, args):
    # 复用 summarize：就是普通的 Python 函数调用，把 ctx 传进去
    short = await summarize(ctx, {'text': args['goal']})
    if short is None:
        return {'summary': None, 'explanation': None}
    detail = await ctx.agent(
        f'解释这段摘要的含义\n{short}', label='detail', tools=[],
    )
    return {'summary': short, 'explanation': detail}
```

两个函数可以放在同一个模块里，`shared_flow` 可以直接交给 SDK 运行。`summarize` 里的会话用的是同一个上下文，环境、预算和并发容量都共享。装饰器只用于按名称查找 Workflow；函数之间组合时，当普通函数调用就行。输入和返回值的格式由你约定。

## 候选：在隔离环境里试改动

很多 Agent 框架让 Agent 直接改你的工作区。OC 多提供了一种做法：**先在隔离副本里生成，检查之后再显式采纳**。

- **候选环境**：源工作区的一份隔离副本，默认用 Git 工作树实现。Agent 在里面随便改，不会碰到源工作区。
- **候选（`CandidateRun`）**：一次在候选环境里完成的运行，带着输出、完整的文件改动和测试证据。
- **采纳**：你检查候选之后，调用 `ctx.adopt_candidate` 把它的改动应用回源工作区。

这样你可以并行试几种实现再挑一个，代价是多一步采纳。内置的 Duo 就建立在这个机制上。执行、选择、采纳是三个可以分别观察的步骤。

### 只跑一个候选 Agent

下面的函数在候选环境里跑一次实现，记录改动，确认没有错误再采纳：

```python
from opencollab import workflow
from opencollab.tools import builtin_tools

@workflow(name="inspect-and-adopt")
async def inspect_and_adopt(ctx, inputs):
    # 1. 在候选环境里跑一次实现
    candidate = await ctx.candidate_agent(
        inputs["task"],
        label="implementation",
        tools=builtin_tools("file_read", "apply_patch", "grep"),
        budget=40_000,
    )
    # 2. 看看它改了什么
    await ctx.log(candidate.diff)
    # 3. 出错或没有输出就不采纳
    if candidate.lifecycle_errors or candidate.output is None:
        return {"adopted": False, "output": candidate.output}
    # 4. 采纳到源工作区
    await ctx.adopt_candidate(candidate)
    return {"adopted": True, "output": candidate.output}
```

示例只在“有输出且没有生命周期错误”时采纳。实际使用中，你也可以按测试记录、人工选择或多个候选的比较结果来决定。想让候选自动收集测试证据，把工具换成 `evidence_tools`：

```python
from opencollab.tools import evidence_tools

async def collect_single_candidate(ctx, args):
    candidate = await ctx.candidate_agent(
        args['goal'], label='single', budget=20000,
        # evidence_tools 会记录 Agent 实际跑过的测试，方便后面比较
        tools=evidence_tools(
            'bash', 'file_read', 'file_write', 'apply_patch', 'git_diff',
            # 默认 headless=True，要求 shell 在隔离进程里运行
            headless=args.get('allow_unisolated_shell') is not True,
        ),
    )
    return {
        'report': candidate.output,
        'diff': candidate.diff,
        'tests': list(candidate.test_records),
        'verified_targets': list(candidate.verified_targets),
    }
```

这个函数只准备候选并返回证据，不会改动你的工作区。测试证据的规则见 [测试证据](../observability/run-results.md#用测试证据确认做对了)。

### 把整套子流程作为候选

下面把一次修改和它的报告放进候选环境，`proposal` 可以换成包含多次 Agent 调用的流程：

```python
from opencollab.tools import builtin_tools

# 子流程：在候选环境里修改文件
async def proposal(ctx, args):
    answer = await ctx.agent(
        args['goal'], label='implement',
        tools=builtin_tools('file_read', 'file_write', 'apply_patch', 'git_diff'),
    )
    return {'answer': answer}

# 父流程：把 proposal 放进候选环境运行，拿回结果
async def prepare_candidate(ctx, args):
    candidate = await ctx.candidate_workflow(
        proposal, {'goal': args['goal']}, label='proposal', budget=20000,
    )
    await ctx.log(f'Candidate patch size {len(candidate.diff)}')
    return {
        'report': candidate.output,
        'diff': candidate.diff,
        'lifecycle_errors': list(candidate.lifecycle_errors),
    }
```

`candidate_workflow` 为子流程准备一个候选环境和一个子上下文，子函数里的多次会话都在这个环境里工作。

- **资源**：候选和父运行共享 Agent 并发容量、集合任务容量和最终截止时间；候选的有限预算从父运行的授权里出。
- **嵌套**：默认的候选子上下文没有再配备候选后端，所以子流程里不能再开一层候选环境。子流程可以继续调用普通子函数，也可以给单次会话选隔离策略；确实需要再建一层候选环境，要由集成方提供对应的后端。

### 读取候选结果

`candidate_agent` 和 `candidate_workflow` 都返回 `CandidateRun`：

| 字段 | 含义 |
| --- | --- |
| `label` | 候选的名字 |
| `output` | 文本输出，或子流程的返回值 |
| `diff` | 捕获到的文件改动 |
| `test_records`、`verified_targets` | 测试记录，以及当前确认通过的测试目标（空集合表示没记录到验证） |
| `lifecycle_errors` | 清理等生命周期问题 |
| `source_revision` | 源版本，后端支持时才有 |
| `delivery_diff`、`delivery_paths`、`delivery_source_diff` | 额外的交付材料 |
| `effective_diff` | 按捕获关系选出的有效差异 |

注意测试证据的来源：单会话候选可以通过证据工具自动收集测试记录；`candidate_workflow` 返回的顶层 `test_records` 和 `verified_targets` 目前是空的，子流程要把自己实际执行的检查结果放进 `output` 或运行记录里。

两个候选的回答可能差不多，改的文件却不同；也可能补丁一样，执行环境不同。所以比较时要保留每个候选对象，看完整差异、测试记录和生命周期信息，再把选中的那个传给 `adopt_candidate`。

### 采纳候选改动

在还持有 `CandidateRun` 的异步函数里调用 `adopt_candidate`：

```python
async def deliver_selected(ctx, candidate):
    # preserve_paths 里的文件在采纳时保持源工作区的版本不被覆盖
    await ctx.adopt_candidate(candidate, preserve_paths=('tests/reference.py',))
    return {'adopted': candidate.label}
```

- `preserve_paths` 列出采纳时要保留的源路径，常用于集成方自己维护的测试文件。
- 默认的 Git 后端把候选的增量改动应用到源工作区；你在源工作区里另外做的修改和 Git 暂存区都会保留。
- 全环境后端可以接收完整的候选对象，按候选身份交付文件和服务状态。

推荐顺序：捕获候选 → 采纳 → 在源工作区里运行任务检查。把每一步的结果写进最终返回值，调用方就能看到交付进行到了哪一步。有多个候选时，先比较完，再按选定的顺序采纳。

### 采纳失败时

以下情况都可能无法交付，运行时会保留候选环境并告诉你恢复位置：

- 候选运行期间，源工作区发生了变化；或者采纳之前，源版本变了。
- 改动捕获失败。
- 改了子模块里的内容。默认的仓库补丁交付只覆盖父仓库的文件，子模块交付需要合适的后端负责。

采纳报冲突时，读异常信息和恢复材料，看哪些文件冲突。想看原始文件，Git 候选工作区里的 `refs/worktree/opencollab-source` 指向原始源树。完整的找回步骤见 [隔离改动怎样回到项目](../isolation/candidate-delivery.md#出了问题先找回改动再清理)。

## Agent、Team、Workflow 怎样互相嵌套

三种运行方式都返回 `RunResult`，并且可以嵌套。嵌套时的默认规则各不相同：

| 方向 | 入口 | 什么时候返回 | 资源算谁的 |
| --- | --- | --- | --- |
| Workflow 里调用 Agent | `ctx.agent` / `ctx.agent_run` | 会话结束 | 当前 Workflow 的预算和并发容量 |
| Workflow 里复用子流程 | `await helper(ctx, args)` | 子函数返回 | 沿用传入的 `ctx` |
| Workflow 里试一套方案 | `ctx.candidate_workflow` | 候选捕获并清理之后 | 候选环境，受父运行的容量和授权约束 |
| Team 成员运行已注册的 Workflow | `run_workflow` | 整个 Workflow 结束 | 借用调用成员的环境，计入它的额度 |
| Team 成员运行自己写的 Workflow | `manage_workflow launch` | 启动后马上返回子运行 id | 由创建它的成员拥有和管理 |
| Workflow 里启动一个 Team | 宿主 SDK 的 `OpenCollab.team` | Team 运行结束 | 一次独立运行，需要你自己接 |

### Team 成员运行 Workflow

两种工具都要在角色的 `tools` 里明确写上才能用：

- 成员要跑一个已知流程并等结果，用 `run_workflow`：先 `describe` 看输入说明，再 `run` 执行。
- 成员要自己写代码再启动，用 `manage_workflow`：先 `describe` 看隔离策略、工具授权和入口示例，再写源码并 `launch`，之后用 `status`、`wait`、`result`、`stop` 管理子运行 id。成员需要有 `bash` 权限，目标环境里要有装了 OC 的 Python。

完整用法、预算规则和在 VM 里并行调用多个 Agent 的例子，见 [让 Team 成员运行 Workflow](team-run-workflow.md)。

### 在 Workflow 里启动 Team

公开的 `ctx` 提供 Agent、集合和候选操作，**没有 `ctx.team`**。要在业务流程里跑 Team，用宿主 SDK 的 `OpenCollab.team`。这样启动的 Team 是一次独立运行，不会自动跟外层共享预算和停止，你需要自己接好三件事：

1. **预算**：比如外层 Workflow 预算 50,000 token，内层 `team(..., budget=30000)`。这 30,000 算不算在 50,000 里面，由你的应用决定和管理。
2. **停止**：想一起停，就让应用持有内层任务，把取消信号传下去，等它清理完再释放环境。
3. **归属**：产物目录、模型客户端、调用方环境都要明确由谁负责。

### 在你的程序里分别启动三者

```python
from opencollab import OpenCollab

# 一个最简单的 Workflow：让一个 Agent 做摘要
async def summarize(ctx, args):
    return await ctx.agent(args['goal'], label='summary', tools=[])

async def run_independent_examples(workspace, team_config):
    client = OpenCollab(workspace)
    # 单次 Agent 会话
    single = await client.agent('概括工作区任务', tools=[])
    # 运行上面的 Workflow 函数，预算 10000 token
    flow = await client.workflow(
        summarize, {'goal': '解释任务中需要验证的行为'}, budget=10000,
    )
    # 启动一个 Team，team_config 换成你实际的团队文件
    team = await client.team(
        '检查任务需求并提交协作结果', config=team_config, budget=30000,
    )
    return {'agent': single.status, 'workflow': flow.status, 'team': team.status}
```

这三次是彼此独立的运行：用同一个客户端配置，但预算按每次调用传入的值各算各的；它们共用同一个工作区，前一次留下的改动会不会影响后一次，取决于执行环境策略。

## 小结

- 同一个 Workflow 里调 `ctx.agent` 或普通子函数：资源都算在当前运行上，最简单。
- 要隔离着试一套方案：用候选，检查后再采纳。
- Team 成员跑流程：已注册的用 `run_workflow`，自己写的用 `manage_workflow`。
- 在 Workflow 里跑 Team：用 `OpenCollab.team`，预算和停止要自己接。

## 延伸阅读

- 源码依据：[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/workflows.py) · [opencollab/application/workflow_candidates.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/workflow_candidates.py) · [opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/tools.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/README.md) · [docs/duo.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/duo.md)
- 相关指南：[在 Workflow 里调用 Agent](workflow-agent-calls.md) · [让 Team 成员运行 Workflow](team-run-workflow.md) · [隔离改动怎样回到项目](../isolation/candidate-delivery.md) · [Duo](duo-workflow.md)
