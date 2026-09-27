# Duo

[English guide](../duo.md)

Duo 为同一任务生成两个隔离候选，比较证据后采用一个结果。通用角色提示覆盖源码修改、配置、数据和任务要求的其他产物。运行时提供候选环境与实际交付方式。

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

A 寻找最简单、完整的解决办法。B 检查依赖、交互和边界条件，确保结果整体可用。B 收到 A 实际观察到的公开验证命令，在相关且可用时执行同一检查。角色报告实际完成的工作、执行过的检查与剩余限制。

裁决者依据任务的明确要求比较结果与验证证据。模型撰写的结果报告标为候选陈述。测试记录在目标、runner 和命令一致时参与比较。证据不足的要求保持 unclear，原有保守选择与回退规则继续生效。

提示集中放在 [`_prompts.py`](../../opencollab/builtin_workflows/_prompts.py)。内部修订号为 4，结果中的 `prompt_revision` 记录该值，对外统一使用 `duo`。

## 完整证据读取

机械选择仍未确定候选时，Duo 保存完整证据，并向只读裁决者提供 `read_candidate_evidence`。索引记录原始变更路径和字符范围，完整文本或二进制 diff 保持可读。公开测试证据与模型结果报告分别存放。工具通过 `next_offset` 和 `eof` 支持继续读取，并限定在当次登记的证据文件内。

`candidate_evidence_dir` 指定宿主侧父目录，每次裁决创建独立子目录并记录位置。调用方负责文件保留。候选运行在其他环境中时，裁决者仍可读取这些文件。

## 候选交付与结果

Duo 通过现有候选工作区接口完成隔离、变更捕获与采用。默认 Git 后端交付仓库改动。完整环境集成提供对应候选后端，按候选身份采用实际环境，并保留必要文件和服务。运行时继续检查候选有效性与采用结果，现有补丁选择规则把空改动记为未完成。

`goal` 或 `description` 提供任务。评测集成可通过 `injected_test_paths` 保留受保护测试。预算、模型设置和角色截止时间沿调用方配置传递。

结果分别记录 `winner` 与 `adopted`，采用失败时可能回退另一候选。成功采用后 `status` 为 `done`，没有候选被采用时为 `incomplete`，缺少任务时为 `error`。结果同时保留选择理由、证据、采用尝试和 token 消耗。正确性由实际检查及评测器的正式评分确定。

OpenCollab-Eval 负责题面、环境准备、隐藏测试隔离、候选捕获和正式评分，运行方式见[评测快速开始](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start)。
