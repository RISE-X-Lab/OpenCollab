---
{
  "id": "candidate-delivery",
  "title": "把候选结果采纳到交付目录",
  "category": "isolation",
  "summary": "Agent 在隔离环境里改的代码不会自动回到项目，本文教你挑一个结果并采纳回来。",
  "aliases": [
    "改动去哪了",
    "结果没有写回",
    "candidate_agent",
    "candidate_workflow",
    "CandidateRun",
    "adopt_candidate",
    "adopt_agent",
    "entry_patch"
  ],
  "keywords": [
    "候选结果",
    "采纳改动",
    "补丁交付",
    "写回项目"
  ],
  "questions": [
    "找到 Agent 的实际改动，选择要保留的候选，并在交付目录验证结果？",
    "Agent 改完了，为什么我的项目里没变化？",
    "怎么从几个候选里挑一个合并进来？",
    "Team 里 Coder 的改动怎么写回？"
  ],
  "related": [
    "worktree-workspaces",
    "workspace-recovery",
    "shell-permissions-execution",
    "adopt-team-changes",
    "compose-workflow-candidates"
  ],
  "verified_ref": "0c47f74a"
}
---

# 把候选结果采纳到交付目录

在 OC 里，Agent 在工作树或容器里的改动不会自动写回项目。它们先作为候选保存，由代码或入口 Agent 显式采纳。这样可以并行跑几种实现再挑一个，代价是多一步采纳。

OC 名词：

- 候选（candidate）：一次在独立工作区里完成的实现，带着它的改动。
- 采纳（adopt）：把候选的文件改动应用到交付目录。
- 交付目录：最终要拿到改动的目录，通常就是你的项目。

最小用法：Workflow 里 `ctx.candidate_agent(...)` 加 `ctx.adopt_candidate(candidate)`；Team 里入口角色用 `adopt_agent` 工具。

## Workflow：先运行候选，再采纳

```python
from opencollab import workflow
from opencollab.tools import builtin_tools

@workflow(name="inspect-and-adopt")
async def inspect_and_adopt(ctx, inputs):
    # 1. 在候选工作区里跑一次实现
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
    # 4. 采纳到交付目录
    await ctx.adopt_candidate(candidate)
    return {"adopted": True, "output": candidate.output}
```

`ctx.candidate_agent` 返回一个 `CandidateRun`，常用字段：

| 字段 | 含义 |
| --- | --- |
| `output` | Agent 的输出 |
| `diff` | 完整的文件改动 |
| `test_records` | 测试记录 |
| `verified_targets` | 支持验证观察的工具记录下的验证目标；空集合表示没有记录到验证 |
| `lifecycle_errors` | 生命周期错误 |

它还带来源版本等信息。

`ctx.candidate_workflow` 可以把整个异步子工作流放进一份候选工作区。

示例只在“有输出且 `lifecycle_errors` 为空”时采纳。你也可以按测试记录、人工选择或多个候选的比较结果来决定。判断候选好不好，要结合命令输出和交付文件一起看。

采纳完，在交付目录里运行项目验证，检查最终文件是否满足任务要求。

## 怎样比较几个候选

两个候选回答可能差不多，改的文件却不同。也可能补丁一样，执行环境不同。比较步骤：

1. 保留每个候选对象。
2. 查看完整差异、测试记录和生命周期信息。
3. 把选中的那个对象传给 `adopt_candidate`。

如果候选工作流默认返回的测试字段是空的，可以让业务工作流在 output 里一起返回它实际执行的检查结果。

有多个候选时，先比较完，再按选定的顺序采纳。

## 采纳时保留某些文件

传 `preserve_paths=("notes/local.txt",)` 可以保留指定路径。

## Team：入口成员采纳 Coder 的结果

1. 入口角色声明 `adopt_agent` 工具。
2. 成功的 Coder 完成结果会带 `aid`，例如显示 `Completed coder aid=3`。
3. 入口调用 `adopt_agent`，传 `{"aid": 3}`。
4. 采纳后派一个新的 Tester，从更新后的交付内容开始测试。

注意：

- 这个工具只采用当前入口轮次里成功完成的、直接下属的 Coder 子任务。
- 采用会保留源索引和你独立做的修改。有冲突时，保证交付文件完整。
- 采纳前派的旧 Tester，它的结果对应的是采纳前的文件。

完整流程和修订处理见 [采用队友改动并验证交付](../team/adopt-team-changes.md)。

## 入口自己被隔离时怎么拿到改动

入口 Agent 自己也可以跑在工作树或容器里。它的改动会在资源回收前保存下来。

1. 从 `result.metrics["entry_patch"]` 拿到补丁位置。
2. 先通读完整改动。
3. 在源仓库里显式应用这个补丁。
4. 运行项目测试。

即使关闭了轨迹记录，入口补丁仍会保存。没指定产物目录时，会自动创建一个恢复目录。

## 常见问题

- 如果 Agent 改完了，项目里却没变化：改动还在候选里，需要采纳。入口被隔离时，去 `entry_patch` 拿补丁。
- 如果看到的 diff 不完整：显示用的 diff 有长度限制。完整采纳和恢复用的是另外捕获的完整材料。
- 如果报 source tracking 错误：候选执行时会跟踪源工作区，采纳时也会检查来源版本。运行期间改了源目录就可能触发这个错误。候选位置会保留下来，方便恢复。来源变了以后再采纳，可能覆盖后来的修改。先比较当前源文件和候选起点，再决定重跑还是重新准备候选。
- 如果出现捕获错误：按 [找回隔离任务的改动并清理资源](workspace-recovery.md) 找到保留的目录或容器。

## 参考

候选 API 见[Workflow 候选实现](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow_candidates.py)。Team 交付见[调度器采纳逻辑](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/_scheduler_team.py)。
