# Duo

[English guide](../duo.md)

Duo V8 为同一任务生成两个隔离候选，比较证据后采用一个结果。通用角色提示覆盖源码修改、配置、数据和任务要求的其他产物。运行时提供候选环境与实际交付方式。

## 运行 Duo

按[配置说明](../../configs/README.md)设置模型入口，再指定工作目录。安装 OpenCollab 后即可发现 Duo。

```bash
opencollab workflow list --workspace /path/to/workspace
opencollab workflow run duo --workspace /path/to/workspace \
  --args '{"goal":"Complete the task described here.","allow_unisolated_shell":true}'
```

上例显式允许在可信本地工作区执行 shell。默认设置要求进程隔离。容器调用方可以保留默认设置，并通过 SDK 传入执行环境。

```python
import asyncio

from opencollab import OpenCollab


async def main():
    client = OpenCollab("/path/to/workspace")
    result = await client.workflow(
        "duo",
        {
            "goal": "Produce the requested data files and update their configuration.",
            "candidate_evidence_dir": "artifacts/duo-evidence",
            "allow_unisolated_shell": True,
        },
        budget=1_000_000,
        artifacts="artifacts/duo-run",
    )
    print(result.raise_for_status().output)

asyncio.run(main())
```

公共函数通过 `from opencollab.builtin_workflows import duo` 导入，SDK 也接受该函数。`get_builtin_workflows()` 返回只含 `duo` 的新注册表。CLI 与 SDK 名称查找同时读取工作区的 `workflows/` 或 `OPENCOLLAB_WORKFLOWS_DIR`，同名冲突沿用现有错误处理。

显式指定 `agent_profile` 会选择相应的基础系统提示、历史整形、工具限制与安全行为。其中 `agent_profile="base"` 跟随 Base 映射，当前为 Single2，`agent_profile="single2"` 则直接选择该实现。既有角色权限说明让工作流职责优先于通用修复与提交指引。省略该参数时使用工作流自身的角色配置。

## 面向任务的角色

共同提示要求遵循任务和运行时的交付说明，保留无关用户工作，以及任务需要的产物和服务。任务涉及配置、依赖、构建、资源或公开测试更新时，角色可以完成相应修改。独立评测材料和未公开参考答案继续受到保护。Git 提交与其他提交机制按任务或运行时要求执行。

V8 要求通过任务预期的入口或输出验证最终交付物，并检查改动直接影响的既有行为。coder 在最后一次相关修改或清理之后补跑受影响的检查，区分原有检查与候选自行新增或修改的检查。这段共同提示覆盖两种提交模式，适用于源码、文件和服务状态，具体交付要求继续由任务与运行时决定。

A 寻找最简单、完整的解决办法。B 检查依赖、交互和边界条件，确保结果整体可用。B 收到 A 实际观察到的公开验证命令，在相关且可用时执行同一检查。角色报告实际完成的工作、执行过的检查与剩余限制。

Duo 先运行 A，再运行 B，随后完成机械选择、必要的裁决和采用。SDK 的 `concurrency` 与 CLI 的 `--concurrency` 限制活跃 Agent 会话。`task_concurrency` 与 `--task-concurrency` 单独限制 `parallel` 和 `pipeline` 单元，涵盖候选子工作流，默认继承会话并发值。调高这两个值后，Duo 仍按 A 到 B 的顺序执行。有限的工作流 `budget` 由各角色共享，角色沿用剩余额度。自定义工作流可以另设每次调用的上限。

裁决者先分别检查候选中具体的失败路径，从任务入口或产物追踪输入、状态和触发条件，直到结果与使用方。每项需求的 `a_evidence` 与 `b_evidence` 引用原始改动路径，并解释这些改动如何实现行为。模型撰写的结果报告标为候选陈述。测试记录在目标、runner 和命令一致，且仍适用于最终候选时参与比较。

识别到的 Bash 测试执行产生 `applicability="current"` 记录。随后完成的 `file_write` 或 `apply_patch` 若观察到内容变化，会将此前记录改为 `unknown`，并按完成顺序写入 `post_test_edits`。原始退出码与 `verified` 值继续保留。识别范围外的 Bash 命令也会让已有记录变为 `unknown`，命令保存在 `post_test_commands`，包括执行失败或被中断的命令。内容未变的写入和原生只读工具保留既有适用状态，重新执行测试会增加一条当前记录。机械比较使用当前记录，裁决者同时读取历史记录和后续修改。

比较依据是正确性与兼容性。证据引用改动路径并解释具体行为时，`covered` 对 `not_covered` 或 `unclear` 构成优势。A 与 B 使用相同规则。两者证据相当时默认选 B。缺项、矛盾或缺乏路径依据的裁决最多补做一次会话，仍无法使用或模型调用异常时默认选 B。已经明确观察到 B 缺少某项而 A 覆盖时，后续复查失败仍保留 A。空候选处理与可比较的公开测试失败优先规则先于裁决，相同 diff 选择 B。

提示集中放在 [`_prompts.py`](../../opencollab/builtin_workflows/_prompts.py)。内部修订号为 8，结果中的 `prompt_revision` 记录该值，对外统一使用 `duo`。

`requirements_complete` 表示裁决者已经逐项审阅所有明确需求。候选尚未覆盖的需求仍记录为 `not_covered` 或 `unclear`，需求清单完整时该字段仍为 true，裁决者据此比较候选间的具体差异。

调用方负责提取选定工作树并完成后续提交时，传入 `submission_mode="working_tree"`。两个 coder 保留完整改动供调用方捕获，裁决者按这一委托关系判断交付职责。测试是否通过仍依据实际证据。默认值 `submission_mode="task"` 沿用任务本身的交付要求，结果会记录此次调用采用的模式。

## 完整证据读取

机械选择仍未确定候选时，Duo 保存完整证据。完整比较数据在 128,000 个 UTF-8 字节以内时，只读裁决者直接收到两个完整 diff、各自的公开测试记录、可比较的共同记录和候选报告。候选报告标为模型陈述，原始文件也继续保留。

超过这一大小时，裁决者使用 `read_candidate_evidence` 分页读取完整内容。索引记录原始变更路径和字符范围，完整文本或二进制 diff 保持可读。公开测试证据与模型结果报告分别存放。工具通过 `next_offset` 和 `eof` 支持继续读取，并限定在当次登记的证据文件内。

`candidate_evidence_dir` 指定宿主侧父目录，每次裁决创建独立子目录并记录位置。调用方负责文件保留。候选运行在其他环境中时，裁决者仍可读取这些文件。

## 候选交付与结果

Duo 通过现有候选工作区接口完成隔离、变更捕获与采用。默认 Git 后端交付仓库改动。完整环境集成提供对应候选后端，按候选身份采用实际环境，并保留必要文件和服务。运行时继续检查候选有效性与采用结果，现有补丁选择规则把空改动记为未完成。

Git 候选从源目录当前受跟踪文件和未被忽略的未跟踪文件开始，交付补丁表示候选随后产生的增量。采用时将增量应用到当前源文件，保留独立用户修改和源索引，冲突会保留当前源目录。修改忽略规则后，已从源目录复制的文件和候选中已加入索引或提交的文件仍进入交付。新建的忽略文件经 `git add --force` 加入后进入交付。

候选运行期间源目录发生变化，运行时会报告错误并保留候选工作树。错误包含初始 Git tree，工作树中的 `refs/worktree/opencollab-source` 继续引用它，可以通过 `git show <tree>:<path>` 恢复文件。后端提供源版本时，`CandidateRun.source_revision` 记录源 `HEAD`，采用前再次核对。源 diff 相同而 `HEAD` 已变化时，采用也会被拒绝。

本地 Git 候选初始化源目录中已有的干净子模块，包含选定工作区内的嵌套依赖。尚未初始化的可选子模块保持空目录。源子模块的变化会在候选获取前报告。候选修改子模块文件、提交或 gitlink 时，`CandidateCaptureError` 会保留完整工作树供恢复。仓库补丁后端交付父仓库的改动，子模块修改需要对应的交付后端。

`goal` 或 `description` 提供任务。评测集成可通过 `injected_test_paths` 保留受保护测试。预算、模型设置和角色截止时间沿调用方配置传递。

结果分别记录 `winner` 与 `adopted`，采用失败时可能回退另一候选。成功采用后 `status` 为 `done`，没有候选被采用时为 `incomplete`，缺少任务时为 `error`。结果同时保留选择理由、证据、采用尝试和 token 消耗。正确性由实际检查及评测器的正式评分确定。

OpenCollab-Eval 负责题面、环境准备、隐藏测试隔离、候选捕获和正式评分，运行方式见[评测快速开始](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start)。
