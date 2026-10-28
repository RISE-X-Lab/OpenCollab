---
{
  "id": "run-results",
  "title": "判断任务是否真的完成：运行结果与测试证据",
  "category": "observability",
  "summary": "看懂 RunResult 并处理 RunError；再用真实执行的测试证据确认任务做对了，理解哪些算通过、哪些不算、改了文件后哪些记录过期。",
  "aliases": [
    "completed",
    "stopped",
    "failed",
    "raise_for_status",
    "RunError",
    "ok",
    "运行状态",
    "evidence_tools",
    "BashEvidence",
    "verified_targets",
    "has_pass_evidence",
    "opencollab.patches",
    "patch_paths",
    "只收集测试",
    "测试后又修改",
    "Agent 说测试过了"
  ],
  "keywords": [
    "运行结果",
    "任务完成",
    "RunResult",
    "agent_failures",
    "测试证据",
    "验证交付",
    "pytest",
    "go test"
  ],
  "questions": [
    "正确处理 RunResult 和 RunError，把框架完成状态与实际任务验证分开理解？",
    "status 是 completed，是不是说明任务做对了？",
    "怎么让运行失败时直接抛异常？",
    "让工作流获得可执行的测试证据，读懂通过、未验证和过期记录？",
    "Agent 说测试通过了，我怎么确认它真的跑过？",
    "测试通过后 Agent 又改了文件，记录还算数吗？"
  ],
  "related": [
    "artifacts-and-history",
    "candidate-delivery",
    "duo-workflow",
    "weave-workflow",
    "extension-and-evaluation-boundaries"
  ],
  "verified_ref": "v0.9.6"
}
---

# 判断任务是否真的完成：运行结果与测试证据

一次运行结束后，有两个问题要分开回答：**运行是怎样结束的？** 和 **任务做对了吗？** 前者看 `RunResult`；后者要看真正执行过的测试和实际交付的文件。OC 的 `completed` 只说明运行入口正常结束，不说明任务做对了。这篇先讲怎样读运行结果，再讲怎样用测试证据确认交付。

## 第一步：读运行结果

SDK 的 `agent`、`team`、`workflow` 都返回 `RunResult`，统一用 `status` 加 `reason` 描述运行怎样结束。下面的片段放在已创建 `oc` 的异步函数中：

```python
result = await oc.agent("分析当前项目的测试入口", budget=50_000)
if result.ok:
    print(result.output)
else:
    print(result.status, result.reason)  # 为什么没完成
    print(result.artifacts)              # 记录保存在哪里
```

| `status` | 含义 |
| --- | --- |
| `completed` | 运行入口正常结束，`ok` 为真 |
| `stopped` | 因限制、超时或主动停止而提前结束，原因看 `reason`；可能带部分工作 |
| `failed` | 运行失败；可能带部分工作 |

希望非完成状态直接进入异常处理，就调用 `result.raise_for_status()`：完成时返回结果自身，其他状态抛出 `RunError`，异常的 `result` 属性保留对应的结果。

### 字段一览

| 字段 | 含义 |
| --- | --- |
| `output` | 当前入口返回的文字或结构化结果 |
| `status` | `completed`、`stopped` 或 `failed` |
| `reason` | 停止或失败的原因 |
| `tokens` | 已记录的 token 用量，可能没有 |
| `artifacts` | 已保存产物的目录 |
| `error` | 保留的异常对象，适合在本进程里诊断 |
| `metrics` | 运行指标，以及补充的恢复、清理信息 |
| `agent_failures` | 内部子 Agent 的失败记录（已脱敏） |

Team 或 Workflow 可能处理掉某个子 Agent 的失败后照常返回，所以**即使 `ok` 为真，也要看一眼 `agent_failures`**。

### 调用本身也可能抛错

不是所有问题都体现在 `RunResult` 里：参数无效可能在执行前就报错；生命周期或保存出故障可能直接抛出 `RunError`；用户取消走异步取消机制，要在应用的取消处理里等清理完成，见 [运行怎样停下](../operations/timeouts-retries.md#主动停止ctrlc-与-taskcancel)。出错时，把已有的输出、用量和恢复信息连同错误一起保存。

### 把结果存进业务系统

`RunResult` 不能直接写成 JSON，因为 `error`、`Path` 和自定义 Workflow 的输出不一定能序列化。服务可以只挑这些字段显式保存：`status`、`reason`、`tokens`、`str(artifacts)`、`metrics["run_id"]` 和必要的输出字段。示例见 [把 OC 接进自己的服务](../architecture/service-integration-lifecycle.md)。

## 第二步：确认任务做对了

`completed` 和 `ok` 适合驱动应用流程；任务算不算真的做好，要由你定义验收条件。编码任务至少确认三件事：

1. 修改落在了预期的目录（隔离环境里的改动要经过采纳，见 [隔离改动怎样回到项目](../isolation/candidate-delivery.md)）。
2. 记录里的候选和最终交付一致。
3. 跑过相关测试，并且测试真的执行了。

调查类任务，则检查结论有没有引用材料作支撑。

为什么要这么较真？因为模型在回答里说“我已经跑过测试、全部通过”，不等于它真的跑过；工具列表里有测试命令，也不等于命令执行完了。可靠的依据只有**实际执行过的命令和它的输出**。

## 用测试证据确认做对了

`evidence_tools` 是 OC 的一组带观察器的工具。Agent 通过它执行 pytest、`go test -json` 或 Django 测试时，OC 会自动记录命令、执行目录、退出码和结果，并在之后观察到文件修改时让旧记录失效。这样挑选候选时，看的是实际执行过的测试目标（`verified_targets`），而不是模型的自述。

### 给 Agent 配上带记录的工具

```python
from opencollab.tools import evidence_tools

# 这些工具在执行时会顺便记录测试证据
tools = evidence_tools("bash", "file_read", "file_write", "apply_patch", "git_diff", "grep")
```

把这些工具交给 Workflow 里的候选 Agent，就能读取记录（在 Workflow 的异步函数里）：

```python
candidate = await ctx.candidate_agent(
    "修复公开问题并执行相关测试", tools=tools, label="implementation"
)
print(candidate.test_records)      # 所有测试记录
print(candidate.verified_targets)  # 当前确认通过的测试目标
```

默认的 shell 需要进程隔离环境；要在本机执行，按 [一条命令要过三道关](../extensions/tool-selection.md#一条命令要过三道关) 配置授权。

### 什么算通过，什么不算

支持的测试形式：规定形式的 pytest（加 `-rA` 能保留每一项的通过记录）、`go test -json`、Django 测试入口。解释结果时会看退出码、测试目标、执行目录，以及输出有没有被截断。

以下情况记为“尚未验证”：

- 只输出了帮助信息；
- 收集到零个测试，或只做了收集（collection）；
- 输出被截断，剩下的内容不足以判断结果。

简单说：**测试目标必须真的执行了，并且有对应的结果，才算通过。**

### 测试之后又改了文件

| 发生了什么 | 对记录的影响 |
| --- | --- |
| 同一目标重跑 | 更新当前证据 |
| 重跑失败或中断 | 影响之前的通过状态 |
| 观察到文件被修改 | 清除当前的通过目标，旧记录标为“适用性未知” |
| 执行了无法识别为测试的 Bash 命令 | 也可能有副作用，需要重新建立证据 |

历史记录会保留原始的退出状态和 verified 信息；判断一条记录是否适用于当前候选，看 applicability 字段和之后有没有修改。交付之前确认：**你采纳的代码版本，就是被测试的那个版本**。所以最后一次相关修改之后再跑的测试最有价值。

测试路径按实际执行的工作区解释。`cd tests && pytest -rA test_example.py` 和根目录下的路径是什么关系，要按环境解析；两个目录里的同名文件也可能是不同的测试目标。读记录时保留原命令和执行目录，方便复核。

### 接入应用自己跑的测试

如果应用通过自己的执行服务跑了命令，可以用公开的 `has_pass_evidence` 按同样的规则解释输出。它只读已有的输出，不负责执行命令。下面的 `result` 是刚完成的命令结果，保留原始退出码、标准输出、标准错误和截断标志：

```python
from opencollab.tools import has_pass_evidence

passed = has_pass_evidence(
    result.returncode,
    result.stdout + "\n" + result.stderr,
    runner="python -m pytest",                 # 填实际使用的 runner
    target="tests/test_example.py::test_case", # 填实际测试目标
    output_truncated=result.stdout_truncated or result.stderr_truncated,
)
print(passed)
```

还想自动记录命令、执行目录和后续修改的影响，就用前面的 `evidence_tools`，或用 `BashEvidence` 包装你自己的 Bash 工具。`VerificationTool` 协议允许扩展工具暴露当前的 `verified_targets`，自定义测试的结果仍由该工具自己的解析器提供。

### 查看补丁改了哪些文件

想把测试目标和候选改动对上，可以用公开的 `opencollab.patches` 解析 Git 补丁：

```python
from pathlib import Path

from opencollab.patches import patch_entries, patch_paths

patch = Path("candidate.patch").read_text(encoding="utf-8")
print(patch_entries(patch))  # 每个文件的旧路径、新路径
print(patch_paths(patch))    # 涉及的全部路径
```

- `patch_entries` 返回每个文件的旧、新路径，新增或删除那一侧的 `/dev/null` 表示为空字符串；`patch_paths` 按首次出现的顺序保留两端路径，便于处理重命名。
- 解析器支持 Git 的引号与转义文件名、UTF-8 和带空格的文件名。需要逐块处理时，用同模块的 `split_patch_blocks` 与 `patch_block_target_path`。
- 对接已有的补丁读取器时，还有 `git_header_tokens`（拆分 `diff --git` 文件头）、`git_diff_endpoint`（处理端点和 `a/`、`b/` 前缀）、`diff_target_path`（从文件头选出目标路径）、`decode_git_c_path`（解码引号与转义路径）、`normalize_patch_path`（统一路径分隔符）。

这些结果适合生成变更视图或挑选要跑的测试；真正采纳补丁，仍用 [候选采纳](../isolation/candidate-delivery.md) 的操作。

正式的 benchmark 由 OCE 和对应的验证器评分，这里的测试记录适合开发时挑选候选、发现问题，见 [OC 的边界与 OCE 评测](../architecture/extension-and-evaluation-boundaries.md#oc-和-oce-怎样分工)。

## 延伸阅读

- 源码依据：[opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/sdk/result.py) · [docs/test-evidence.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/test-evidence.md) · [opencollab/adapters/tools/evidence/bash_evidence.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/evidence/bash_evidence.py) · [opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/tools.py) · [opencollab/patches.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/patches.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/README.md)
- 相关指南：[运行记录](artifacts-and-history.md) · [隔离改动怎样回到项目](../isolation/candidate-delivery.md) · [Duo](../workflow/duo-workflow.md) · [Weave](../workflow/weave-workflow.md)
