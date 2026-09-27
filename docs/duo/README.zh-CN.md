# Duo

[English guide](../duo.md)

Duo 是 OpenCollab 内置的双 coder 工作流，原名为 OpenCollab-Eval 中的 G22。
安装 OpenCollab 后即可通过 CLI 或 Python SDK 调用。默认 Agent 与 Single2
均可驱动同一个 Duo 工作流。

## 运行 Duo

按[配置说明](../../configs/README.md)设置模型服务，将工作目录指向待修复的 Git 仓库。
仓库缺少 `workflows/` 目录时，CLI 和 SDK 仍能发现内置 Duo。

```bash
opencollab workflow list --workspace /path/to/repository
```

Duo 默认要求 Bash 在具备进程隔离能力的环境中执行。在本地 Git 仓库运行可信任务时，
调用者可通过工作流输入显式允许本地 shell 执行。

```bash
opencollab workflow run duo --workspace /path/to/repository \
  --args '{"goal":"修复这里完整描述的公开问题。","allow_unisolated_shell":true}'
```

添加 `--agent-profile single2` 即可让所有角色使用 Single2。

```bash
opencollab workflow run duo --workspace /path/to/repository \
  --agent-profile single2 \
  --args '{"goal":"修复这里完整描述的公开问题。","allow_unisolated_shell":true}'
```

Python SDK 接受内置名称，也接受公开工作流函数。模型、额度、执行环境和产物目录沿用
其他工作流的调用参数。

```python
from opencollab import OpenCollab

client = OpenCollab("/path/to/repository")
inputs = {"goal": "修复完整描述的公开问题。", "allow_unisolated_shell": True}
result = await client.workflow("duo", inputs, budget=1_000_000)
print(result.raise_for_status().output)

```

从 `opencollab.builtin_workflows` 导入 `duo` 后，可将该函数作为第一个参数直接传入。
在工作流调用中添加 `agent_profile="single2"` 即可让所有角色使用 Single2。

容器任务通过 `OpenCollab(..., environment=environment)` 传入公开环境对象，保留默认
shell 设置。候选工作区由该环境提供。集成方也可以通过 `candidate_workspace=` 传入
自己的候选工作区接口实现。

## 候选生成与选择

A 在独立候选工作区内寻找最小完整修复。B 从相同源状态建立另一工作区，沿生产者、
消费者、公开接口和生命周期检查需求，并收到 A 实际执行过的公开测试命令。
两个角色分别保存完整 diff 和[原生测试证据](../test-evidence.md)。

空候选和相同 diff 优先机械处理。公开测试记录在目标、runner 和命令相同时才能比较。
相同命令下一方有通过证据、另一方执行失败时，可直接选择候选。
其他情况交由结构化裁决者根据完整公开任务和候选证据逐项比较。

证据能证明 B 覆盖了 A 缺失的需求，并且保留 A 更好覆盖的其他需求时，裁决才选择 B。
裁决结构错误、证据不足或角色调用失败时回退 A。采用首先尝试选中候选，失败后再尝试
另一非空候选。

`goal` 承载公开任务，`description` 保留为兼容参数。评测集成通过
`injected_test_paths` 保留预先准备的测试文件。额度、步数、模型超时和 Agent profile
由调用方设置，角色的 `budget=None` 沿用现有工作流共享额度行为。

## Duo v3 与旧名称

`duo` 保留原 G22 v2 行为，裁决者通过提示接收完整候选 diff 和公开证据，工具为结构化
提交工具。`duo-v3` 对应原 G22 v3，保存完整候选证据文件，并提供
`read_candidate_evidence` 供裁决者分页读取索引、公开记录和精确 diff 范围。

```python
from opencollab.builtin_workflows import duo_v3

result = await client.workflow(
    duo_v3,
    {
        "goal": task,
        "candidate_evidence_dir": "artifacts/candidate-evidence",
        "allow_unisolated_shell": True,
    },
    agent_profile="single2",
)
```

`candidate_evidence_dir` 指定运行主机上的父目录。每次 v3 裁决新建独立子目录，并将路径
写入工作流日志，文件由调用方保存。

| CLI 与 SDK 名称 | 公开函数 | 行为 |
| --- | --- | --- |
| `duo` | `duo` | 原 G22 v2 选择策略 |
| `duo-v3` | `duo_v3` | 原 G22 v3 完整文件证据策略 |
| `validation-council-dual-coder-selection-v2` | `validation_council_dual_coder_selection_v2` | `duo` 兼容名称 |
| `validation-council-dual-coder-selection-v3` | `validation_council_dual_coder_selection_v3` | `duo-v3` 兼容名称 |

表中的函数均由 `opencollab.builtin_workflows` 导出。
`get_builtin_workflows()` 每次返回包含这些名称的新注册表。
CLI 和 SDK 名称查找会合并内置注册表与工作目录下 `workflows/` 内的用户模块。
`OPENCOLLAB_WORKFLOWS_DIR` 可选择其他目录，相对路径从工作目录解析。
用户模块与内置名称冲突时，沿用注册表的重复名称错误。直接传入函数或 spec 时执行指定对象。

## 阅读结果

`RunResult` 描述框架运行状态，Duo 输出单独保存任务状态。
采用成功时 `status` 为 `done`，未采用补丁时为 `incomplete`，缺少任务时为 `error`。
`winner` 表示选择结果，`adopted` 表示实际采用的候选，采用回退时二者可能不同。
`selection_reason`、`judge_used`、`judge_result`、`adoption_attempts`、
`shared_public_command` 和逐候选 diff 路径与公开测试记录共同说明选择依据。

OpenCollab-Eval 负责题面与环境准备、隐藏测试隔离、最终补丁捕获和正式评分。
现有 `oc-eval g22` 命令继续调用由 OpenCollab 拥有的工作流。
[评测快速开始](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start)
说明 benchmark 配置与正式测试报告的运行方式。
