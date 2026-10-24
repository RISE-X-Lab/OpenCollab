---
{
  "id": "native-test-evidence",
  "title": "用实际测试验证 Agent 的交付",
  "category": "observability",
  "summary": "让 Agent 真的跑项目测试并留下记录，读懂哪些算通过、哪些不算、哪些已过期。",
  "aliases": [
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
    "测试证据",
    "验证交付",
    "pytest",
    "go test"
  ],
  "questions": [
    "让工作流获得可执行的测试证据，读懂通过、未验证和过期记录？",
    "Agent 说测试通过了，我怎么确认它真的跑过？",
    "测试通过后 Agent 又改了文件，记录还算数吗？"
  ],
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 用实际测试验证 Agent 的交付

`evidence_tools` 是 OC 的一组带观察器的工具：Agent 通过它执行 pytest、`go test -json` 或 Django 测试时，OC 自动记录命令、执行目录、退出码和结果，并在之后观察到文件修改时让旧记录失效。这样选候选时看的是实际执行过的测试目标（`verified_targets`），而不是模型自述。应用自己执行的测试可以用 `has_pass_evidence` 按同样规则解释。

## 第一步：给 Agent 配上带记录的工具

```python
from opencollab.tools import evidence_tools

# 这些工具在执行时会顺便记录测试证据
tools = evidence_tools("bash", "file_read", "file_write", "apply_patch", "git_diff", "grep")
```

把这些工具交给工作流的候选 Agent。默认 shell 需要进程隔离环境。如果要在宿主机上执行，按终端权限指南配置授权。

## 第二步：读取测试记录

在工作流的异步函数里：

```python
candidate = await ctx.candidate_agent(
    "修复公开问题并执行相关测试", tools=tools, label="implementation"
)
print(candidate.test_records)      # 所有测试记录
print(candidate.verified_targets)  # 当前确认通过的测试目标
```

## 什么算通过，什么不算

支持的测试形式：

- 规定形式的 pytest。加 `-rA` 能保留每一项的通过记录。
- `go test -json`
- Django 测试入口

解释结果时会看退出码、测试目标、执行目录，以及输出有没有被截断。

以下情况记为"尚未验证"：

- 只输出了帮助信息
- 收集到零个测试
- 只做了 collection
- 输出被截断，剩下的内容不足以判断结果

简单说：测试目标必须真的执行了，并且有对应结果，才算通过。

## 测试后又改了文件怎么办

记录会跟着后续动作变化：

| 发生了什么 | 对记录的影响 |
| --- | --- |
| 同一目标重跑 | 更新当前证据 |
| 重跑失败或中断 | 影响之前的通过状态 |
| 观察到文件被修改 | 清除当前通过目标，旧记录标为"适用性未知" |
| 执行了无法识别为测试的 Bash 命令 | 也可能有副作用，需要重新建立证据 |

历史记录会保留原始退出状态和 verified 信息。判断一条记录是否适用于当前候选，看 applicability 字段和之后有没有修改。交付前确认：你采用的代码版本，就是被测试的那个版本。

## 进阶：接入应用自己跑的测试

如果应用通过自己的执行服务跑了命令，可以用公开的 `has_pass_evidence` 解释输出。它只读已有输出，不负责执行命令。下面的 `result` 是刚完成的命令结果，保留原始退出码、标准输出、标准错误和截断标志。

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

要传入实际的 runner 和目标，才能按对应测试规则解释结果。

如果还想自动记录命令、执行目录和后续修改的影响：

- 用前面的 `evidence_tools`。
- 或用 `BashEvidence` 包装你自己的 Bash 工具。
- `VerificationTool` 协议允许扩展工具暴露当前的 `verified_targets`。自定义测试的结果仍由该工具自己的解析器提供。

## 进阶：执行目录和同名文件

测试路径按实际执行的工作区来解释。`cd tests && pytest -rA test_example.py` 和根目录下的对应路径是什么关系，要按环境解析。两个目录里的同名文件也可能是不同的测试目标。读记录时保留原命令和执行目录，方便复核。

正式 benchmark 由 OCE 和对应验证器评分。这里的测试记录适合开发时选候选、发现问题。

## 进阶：查看补丁改了哪些文件

想把测试目标和候选改动对上，可以用公开的 `opencollab.patches` 解析 Git 补丁。

```python
from pathlib import Path

from opencollab.patches import patch_entries, patch_paths

patch = Path("candidate.patch").read_text(encoding="utf-8")
print(patch_entries(patch))  # 每个文件的旧路径、新路径
print(patch_paths(patch))    # 涉及的全部路径
```

- `patch_entries` 返回每个文件的旧、新路径。新增或删除那一侧的 `/dev/null` 会表示为空字符串。
- `patch_paths` 按首次出现顺序保留两端路径，便于处理重命名。
- 解析器支持 Git 的引号与转义文件名、UTF-8 和带空格的文件名。
- 需要逐块处理时，用同模块的 `split_patch_blocks` 与 `patch_block_target_path`。

这些结果适合生成变更视图或挑选要跑的测试。真正采纳补丁、交付候选，仍用 [把候选结果采纳到交付目录](../isolation/candidate-delivery.md)里的操作。

对接已有的补丁读取器时，还有这些工具函数：

| 函数 | 作用 |
| --- | --- |
| `git_header_tokens` | 拆分 `diff --git` 文件头 |
| `git_diff_endpoint` | 处理某一侧端点和 `a/`、`b/` 前缀 |
| `diff_target_path` | 从文件头选出目标路径 |
| `decode_git_c_path` | 解码 Git 的引号与转义路径 |
| `normalize_patch_path` | 统一路径分隔符 |

完整签名和实现说明见下方的 `opencollab.patches` 模块。

## 源码与进一步阅读

[docs/test-evidence.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/test-evidence.md) · [opencollab/adapters/tools/evidence/bash_evidence.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/evidence/bash_evidence.py) · [opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/tools.py) · [opencollab/patches.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/patches.py)
