---
{
  "id": "compose-workflow-candidates",
  "title": "复用子 Workflow，并在候选环境里试改动",
  "category": "workflow",
  "summary": "把写好的异步流程拼成更大的流程；想先隔离着试，就放进候选环境，检查满意后再采用改动。",
  "aliases": [
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
    "子 Workflow",
    "ctx.candidate_workflow",
    "CandidateRun",
    "ctx.adopt_candidate",
    "preserve_paths"
  ],
  "questions": [
    "怎么在一个 Workflow 里复用另一个 Workflow？",
    "怎么让 Agent 先在隔离环境里改代码，确认后再合并？",
    "candidate_workflow 和 candidate_agent 有什么区别？",
    "采用候选改动时发生冲突怎么办？"
  ],
  "related": [
    "author-custom-workflow",
    "team-run-workflow",
    "workflow-parallel-pipeline",
    "duo-workflow",
    "adopt-team-changes",
    "candidate-delivery",
    "workspace-recovery",
    "native-test-evidence"
  ],
  "verified_ref": "0c47f74a"
}
---

# 复用子 Workflow，并在候选环境里试改动

OC 的 Workflow 函数可以直接互相调用：把 `ctx` 传下去，就是普通 Python 函数调用，共享预算和环境。如果不想让子流程直接改源工作区，可以用 `ctx.candidate_agent`（单个会话）或 `ctx.candidate_workflow`（整套子流程）在候选环境里运行。候选环境是源工作区的隔离副本，默认用 Git 工作树（worktree）实现；运行结束后得到 `CandidateRun`（diff、输出、测试证据），由你检查后调 `ctx.adopt_candidate` 把改动应用回源工作区。

这种“先隔离生成，再显式采用”的做法是 OC 和多数直接改工作区的 agent 框架不同的地方。Duo 等内置流程也建立在它上面。

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

要点：

- 两个函数可以放在同一个模块里，`shared_flow` 可以直接交给 SDK 运行。
- `summarize` 里的会话用的是同一个上下文，环境、预算和容量都共享。
- 装饰器只用于按名称查找 Workflow。函数之间组合时，直接当普通函数调用即可。
- 输入和返回值的格式由你自己约定。

## 方式二：只跑一个候选 Agent

只需要一次编码或检查会话时，用 `candidate_agent`。下面是一个完整的工作流函数：

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

说明：

- 示例里的 shell 默认要求进程隔离。如果你的工作区在可信的宿主机上，可以传 `allow_unisolated_shell=true` 放开这个限制。
- `evidence_tools` 在原生工具上加了记录功能，能识别测试执行记录，供比较候选时使用。
- 这个函数只准备候选并返回证据，不会改动你的工作区。要交付改动，需要在还持有候选对象的函数里调用 `adopt_candidate`，见下文“采用候选改动”。

## 方式三：把整套子流程作为候选

下面的例子把一次修改和它的报告放进候选环境。`proposal` 可以换成包含多次 Agent 调用的流程。

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

`candidate_workflow` 会为子流程准备一个候选环境和一个子上下文。子函数里的多次会话都在这个环境里工作。

资源怎么算：

- 候选和父运行共享 Agent 并发容量、集合任务容量和绝对期限（到点必须结束的时间）。
- 候选的有限预算从父运行的授权里出。

能不能再嵌套：

- 默认候选子上下文没有候选后端，所以不能再开一层候选环境。
- 子流程可以继续调用普通子函数，也可以为单次会话选择隔离策略。
- 确实要再建一层候选环境，需要集成方提供对应后端。

## 读取候选结果

`candidate_agent` 和 `candidate_workflow` 都返回 `CandidateRun`。常用字段：

| 字段 | 含义 |
| --- | --- |
| `label` | 候选的名字 |
| `output` | 文本输出，或子流程的返回值 |
| `diff` | 捕获到的文件改动 |
| `test_records`、`verified_targets` | 可用的测试证据 |
| `lifecycle_errors` | 清理等生命周期问题 |
| `source_revision` | 源版本，后端支持时才有 |
| `delivery_diff`、`delivery_paths`、`delivery_source_diff` | 额外的交付材料 |
| `effective_diff` | 按捕获关系选出的有效差异 |

注意测试证据的来源不同：

- 单会话候选可以通过证据工具自动收集测试记录。
- `candidate_workflow` 返回的顶层 `test_records` 和 `verified_targets` 目前初始为空。子流程要把自己的验证结果放进 `output` 或运行记录里。

挑选候选时，结合实际测试记录、文件差异和报告来判断它是否完成了任务。

## 采用候选改动

选好之后，在还持有 `CandidateRun` 的异步函数里调用 `adopt_candidate`。这里的 `candidate` 是前面方法返回的对象。

```python
async def deliver_selected(ctx, candidate):
    # preserve_paths 里的文件在采用时保持源工作区的版本不被覆盖
    await ctx.adopt_candidate(candidate, preserve_paths=('tests/reference.py',))
    return {'adopted': candidate.label}
```

- `preserve_paths` 列出采用时要保留的源路径，常用于集成方自己维护的测试文件。
- 默认的 Git 后端把候选的增量改动应用到源工作区。你在源工作区里另外做的修改和源索引都会保留。
- 全环境后端可以接收完整的候选对象，按候选身份交付文件和服务状态。

推荐顺序：捕获候选，调用采用接口交付，再运行任务检查。把每一步的结果写进最终返回值，调用方就能看到交付进行到哪一步。

## 常见问题

如果采用时报冲突，就读异常信息和恢复材料，看哪些文件冲突。

以下情况都可能导致无法交付：

- 候选运行期间，源工作区发生了变化。
- 采用之前，源版本变了。
- 改动捕获失败。

发生这些情况时，运行时会保留候选环境，并告诉你恢复位置。

如果想看原始文件，Git 候选工作区里的 `refs/worktree/opencollab-source` 指向原始源树，可以从这里读。

如果改了子模块里的内容，就会触发捕获错误，候选工作区会被保留。默认的仓库补丁交付只覆盖父仓库的文件，子模块交付需要合适的后端来负责。

更多恢复方法见[找回隔离任务的改动并清理资源](../isolation/workspace-recovery.md)。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L280)、[opencollab/workflows.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/workflows.py#L83)、[opencollab/application/workflow_candidates.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_candidates.py#L471)、[docs/duo.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/duo.md#L193)。

[opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/tools.py)。

## 相关章节

[编写第一个自定义 Workflow](author-custom-workflow.md)、[让 Team 成员调用已注册的 Workflow](team-run-workflow.md)、[在 Workflow 中并行处理多项任务](workflow-parallel-pipeline.md)、[用 Duo 跑两个候选方案并选出一个](duo-workflow.md)、[采用队友改动并验证交付](../team/adopt-team-changes.md)、[把候选结果采纳到交付目录](../isolation/candidate-delivery.md)、[找回隔离任务的改动并清理资源](../isolation/workspace-recovery.md)、[用实际测试验证 Agent 的交付](../observability/native-test-evidence.md)。
