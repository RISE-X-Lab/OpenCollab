---
{
  "id": "duo-workflow",
  "title": "用 Duo 跑两个候选方案并选出一个",
  "category": "workflow",
  "summary": "Duo 是内置 Workflow：让两个 coder 在各自的候选环境里依次完成同一任务，按改动和测试证据选出一个，再把它采用到源工作区。",
  "aliases": [
    "Duo V8",
    "双 coder",
    "A B 方案",
    "adjudicator",
    "winner",
    "adopted",
    "submission_mode",
    "candidate_evidence_dir",
    "使用 Duo 生成并选择双候选方案"
  ],
  "keywords": [
    "Duo",
    "duo",
    "双候选",
    "候选选择",
    "run_dual_coder",
    "CONTRACT_PROMPT",
    "仲裁"
  ],
  "questions": [
    "Duo 是什么，怎么运行？",
    "Duo 的两个候选有什么区别？",
    "Duo 怎么决定选哪个候选？",
    "winner 和 adopted 有什么区别？",
    "怎么换掉 Duo 的 coder 提示词或仲裁器？"
  ],
  "related": [
    "run-registered-workflow",
    "team-run-workflow",
    "compose-workflow-candidates",
    "weave-workflow",
    "native-test-evidence",
    "candidate-delivery"
  ],
  "verified_ref": "0c47f74a"
}
---

# 用 Duo 跑两个候选方案并选出一个

Duo 是 OC 内置的 Workflow，名称 `duo`。它对同一个任务依次跑两个独立的 coder 候选（best-of-2）：候选 A 追求简单完整的实现，候选 B 关注端到端行为、依赖和边界情况。两份结果各自在候选环境里生成，然后按改动和验证证据选出一个，必要时交给仲裁角色判断，最后把选中的改动采用到源工作区。

适合目标明确、想比较两份完整交付的任务。源代码、配置和数据文件都可以是产物。

## 最小用法

CLI：

```bash
opencollab workflow run duo \
  --workspace /path/to/workspace \
  --task '修复配置读取问题，保留旧格式并运行受影响测试' \
  --args '{"allow_unisolated_shell":true,"submission_mode":"working_tree"}' \
  --budget 100000
```

SDK：

```python
import asyncio
from opencollab import OpenCollab

async def main():
    result = await OpenCollab('/path/to/workspace').workflow(
        'duo',
        {
            'goal': '修复配置读取问题并验证兼容性',
            'submission_mode': 'working_tree',
            'allow_unisolated_shell': True,
            # 仲裁证据保存到这个宿主目录下
            'candidate_evidence_dir': '/path/to/duo-evidence',
        },
        budget=100000,
        artifacts='/path/to/new-duo-run',
        limit_mode='explicit',
    )
    result.raise_for_status()
    print(result.output)

asyncio.run(main())
```

也可以从 `opencollab.builtin_workflows` 导入 `duo` 函数，直接传给 SDK。

## 常用输入

| 输入 | 说明 |
| --- | --- |
| `goal` | 任务描述。也可以用 `description` 代替 |
| `--task-file` | CLI 从文件读取长任务 |
| `allow_unisolated_shell` | 允许 shell 不在隔离进程里运行。示例是可信本地工作区才这么设；用容器时按实际隔离要求设置 |
| `submission_mode` | 默认 `task`：按任务要求处理交付。`working_tree`：只把改动留在工作区，由调用方自己提交 |
| `candidate_evidence_dir` | 仲裁证据的宿主父目录，每次仲裁建一个子目录，保存期限由调用方管理 |
| `agent_profile` | SDK 或 CLI 选择角色使用的 Agent 实现。省略时沿用工作流的角色配置 |

## 执行过程

1. 运行候选 A。
2. 运行候选 B。B 会拿到 A 观察到的公开验证命令，相关且可用时重跑。
3. 按预设规则比较证据，需要时交给仲裁角色。
4. 采用选中的候选。

几个默认行为：

- 两个候选共享这一次工作流的有限 token 额度。
- A 总是先于 B。提高 `concurrency` 或 `task_concurrency` 也不会让它们并行。
- Git 候选从源工作区当时的状态开始：已跟踪的文件，加上未被忽略的未跟踪文件。之后产生的改动被捕获为候选增量。
- 最终改动通过采用过程交付到源工作区。任务对提交、产物和服务状态的要求会写进角色指令，实际怎么交付由环境后端决定。

## 怎么选候选

先走预设规则，处理这些情况：

- 某个候选是空改动
- 公开测试结果可以直接比较
- 两份 diff 相同

规则决定不了时，仲裁角色逐项看两份候选怎么实现任务要求。它会结合修改路径、测试和报告判断完成情况，并用实际检查结果和源码核实报告里的说法。

证据等价时默认偏向 B。但具体失败和已有规则可能先做出决定。结果里的选择原因可以追溯采用依据。

### 测试记录什么时候算数

- 测试执行时，记录标记为“当前适用”。
- 之后如果有文件编辑改变了内容，旧记录的适用性变为“未知”，并记下编辑路径。
- 执行其他 Bash 命令也可能让旧记录变为“未知”。
- 重新跑测试会产生新的当前记录。

所以最后一次相关修改之后再跑的检查最有价值。

### 仲裁看到多少材料

- 材料较小时，仲裁直接拿到完整 diff、公开测试记录和候选报告。
- 材料较大时，通过只读证据工具分页读取，原始证据文件会保留。

## 返回字段

| 字段 | 含义 |
| --- | --- |
| `winner` | 选择过程推荐的候选 |
| `adopted` | 实际采用的候选。第一次采用失败时可能和 `winner` 不同 |
| `status` | `done`：采用完成。`incomplete`：还没有候选交付。`error`：可能是任务缺失 |
| `selection_reason`、`judge_used`、`judge_result` | 选择依据、是否用了仲裁、仲裁对象 |
| `adoption_attempts` | 每次采用的结果 |
| `candidates`、`shared_public_command` | 两份候选的路径和测试记录，以及共享的公开命令 |
| `tokens_spent`、`prompt_revision` | 流程消耗，以及内置提示文本的修订号 |

两个状态不要混淆：

- SDK 的 `RunResult.status`：整个流程有没有执行完。
- Duo 的 `output.status`：候选交付进行到哪一步。

两者都要和应用检查一起看。任务最终是否正确，要由真实入口、原生测试或评测器确认。

## 常见问题

如果采用失败，可能的原因有：

- 候选执行期间源工作区变了
- 采用前源版本变了
- 补丁冲突

如果捕获失败，候选工作区和恢复位置会被保留。先保住已有材料，再对照源版本和候选 diff 处理。见[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)。

如果是评测集成，可以用 `injected_test_paths` 指定采用时要保留的保护测试路径。全环境产物则由调用方注入的候选后端交付。

## 进阶：定制两个 coder 角色

想换掉两个 coder 的分工，可以在自己的包装 Workflow 里调用公开的 `run_dual_coder`。下面的函数保留共享规则和默认选择提示，只改两个角色的任务说明：

```python
from opencollab.builtin_workflows import run_dual_coder

async def compatibility_duo(ctx, args):
    return await run_dual_coder(
        ctx, args,
        coder_prompts=(
            # 候选 A 的提示
            '{rules}\n\nTask\n{goal}\n\n'
            'Implement the smallest complete fix and run relevant public checks.',
            # 候选 B 的提示，{public_command} 是 A 观察到的命令
            '{rules}\n\nTask\n{goal}\n\n'
            'Inspect callers and compatibility, then implement and verify the fix.\n'
            'Previously observed public command\n{public_command}',
        ),
    )
```

可定制的参数：

| 参数 | 说明 |
| --- | --- |
| `coder_prompts` | 两个格式字符串，支持 `rules`、`goal`、`public_command` |
| `selector_prompt` | 默认是公开的 `CONTRACT_PROMPT`，接受 `rules`、`goal`、`candidates` |
| `role_rules` | 替换共享角色规则。请保留任务需要的执行、安全和证据要求 |
| `adjudicator` | 自定义仲裁器，见下文 |
| `source_reader`、`physical_source_reader`、`source_tracker` | 后端集成用的源观察回调 |

自定义仲裁器 `adjudicator`：

- 接收 `ctx`，以及关键字参数 `goal`、`candidate_a`、`candidate_b`、`selector_prompt`、`rules`。
- 异步返回 `(winner, judge_result, reason)`，候选标签用 `A` 或 `B`。
- 运行时先按预设规则选，出现需要进一步判断的分歧时才调用它。

内置 `duo` 已经接好了完整证据文件、分页读取和源追踪。直接用 `run_dual_coder` 的应用，需要自己为仲裁材料和后端接上相应的连接。

## 源码依据

[docs/duo.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/duo.md#L5)。

[opencollab/builtin_workflows/_dual_coder.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/builtin_workflows/_dual_coder.py#L128) 提供公开 helper 的实际签名和选择顺序。

## 相关章节

[运行一个工作流（Workflow）](run-registered-workflow.md)、[让 Team 成员调用已注册的 Workflow](team-run-workflow.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[用 Weave 分组接力完成任务并检查交付](weave-workflow.md)、[用实际测试验证 Agent 的交付](../observability/native-test-evidence.md)、[把候选结果采纳到交付目录](../isolation/candidate-delivery.md)。
