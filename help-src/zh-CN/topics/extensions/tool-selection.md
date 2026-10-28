---
{
  "id": "tool-selection",
  "title": "内置工具：怎么选、文件怎么改、命令怎么跑",
  "category": "extensions",
  "summary": "Agent 能做什么完全由工具列表决定：怎样挑选和限制内置工具，文件工具怎样安全地读写，bash 在哪里执行、谁允许它执行，以及怎样确认命令真的成功。",
  "aliases": [
    "tools",
    "builtin_tools",
    "evidence_tools",
    "六工具",
    "只读Agent",
    "工具没出现",
    "limits",
    "profile_tool_names",
    "profile_tool_limits",
    "agent_tool_names",
    "越界路径",
    "符号链接",
    "file_read",
    "file_write",
    "grep",
    "apply_patch",
    "str_replace",
    "line_replace",
    "unified_diff",
    "overwrite",
    "relocate_expected",
    "normalize_hunks",
    "git_diff",
    "风险命令",
    "自动批准",
    "命令超时",
    "输出截断",
    "--yolo",
    "allow_unisolated_shell",
    "require_process_isolation",
    "SandboxInterceptor",
    "timeout",
    "max_output_chars",
    "--allow-local-child-shell",
    "headless"
  ],
  "keywords": [
    "工具",
    "工具列表",
    "工具权限",
    "文件工具",
    "读写文件",
    "路径限制",
    "终端命令",
    "命令确认",
    "bash"
  ],
  "questions": [
    "按任务给 Agent 配置文件、命令、协作或技能工具并理解权限边界？",
    "怎样做一个只读的 Agent？",
    "为什么 builtin_tools 传 spawn_agent 会报错？",
    "怎样调整工具输出的字符上限？",
    "怎样确认 Agent 实际拿到了哪些工具？",
    "在 Agent 实际工作区内定位文件、准确编辑内容并理解路径或覆盖错误？",
    "file_write 和 apply_patch 各有哪些模式？",
    "str_replace 为什么报找不到或不唯一？",
    "Agent 说改好了，为什么源目录没变？",
    "为什么路径被拒绝了？",
    "让必要命令真正运行，理解确认提示、超时和截断，并据实际输出判断结果？",
    "bash 命令为什么被拒绝或要求确认？",
    "bash 工具默认超时多少？",
    "为什么上一条命令的 cd 没生效？"
  ],
  "related": [
    "custom-tools",
    "isolation-overview",
    "run-results",
    "configure-team"
  ],
  "verified_ref": "v0.9.6"
}
---

# 内置工具：怎么选、文件怎么改、命令怎么跑

在 OC 里，Agent 能请求哪些操作，**完全由它的工具列表决定**；不在列表里的操作，模型想做也做不了。这篇先讲怎样挑选和限制工具，再分别讲文件工具和 `bash`：它们在哪里执行、有哪些安全检查、怎样确认操作真的成功。

## 工具一览

OC 把工具分成两组，组装方式不同：

| 类别 | 名称 | 作用 | 怎么给 |
| --- | --- | --- | --- |
| 文件与检索 | `file_read`、`grep` | 读项目文件、搜索 | `builtin_tools` 或角色 `tools` |
| 编辑与差异 | `file_write`、`apply_patch`、`git_diff` | 修改文件、查看变化 | `builtin_tools` 或角色 `tools` |
| 命令执行 | `bash` | 在指定环境执行命令 | `builtin_tools` 或角色 `tools` |
| 提交 | `submit` | 结束当前回合并交付 | `builtin_tools` 或角色 `tools` |
| Team 协作 | `spawn_agent`、`message_agent`、`team_status`、`adopt_agent` | 派发、通信、状态、交付 | 只能在团队角色的 `tools` 里声明 |
| 扩展与组合 | `use_skill`、`run_workflow`、`manage_workflow` | 载入说明、运行 Workflow | 在团队角色的 `tools` 里声明 |

普通编码工具用公开函数 `builtin_tools(...)` 构造，可以直接传给 SDK。需要调度器、技能存储等运行时能力的工具（如 `spawn_agent`、`use_skill`），由 Team 运行环境提供，只能在团队角色的 `tools` 里按名称声明。想加自己的工具，见 [自定义工具](custom-tools.md)。

## 给 Agent 挑工具

最小用法：只给读取和差异工具，得到一个只读 Agent。

```python
import asyncio
from opencollab import OpenCollab
from opencollab.tools import builtin_tools

async def main():
    tools = builtin_tools("file_read", "grep", "git_diff")   # 只授予读取和差异工具
    result = await OpenCollab(".").agent(
        "阅读项目并说明测试入口，保留现有文件。",
        tools=tools,
        budget=50_000,
    )
    print(result.status, result.output)

asyncio.run(main())
```

想确认实际用了哪些工具，看运行指标 `agent_tool_names`。`profile_tool_names("base")` 返回的是 Agent 配置的默认列表；显式传入工具之后，以运行记录为准。

**有工具不等于能执行。** 有三件事要分开看：

- `builtin_tools` 默认 `headless=True`，要求 shell 运行在有进程隔离的环境里；明确传 `headless=False` 才允许本机 shell。
- 工作树只解决目录分离，本机权限和网络仍由父环境决定。
- `allow_file_creation=False` 只关闭原生文件工具的“新建文件”能力，`bash` 能做什么是另一回事，见下文的 [bash](#bash命令在哪里执行谁允许它执行)。

## 调整工具输出上限

大文件、长测试日志和大范围差异会占满上下文。构造工具时用 `limits` 设置上限：

```python
from opencollab.tools import builtin_tools, profile_tool_limits, profile_tool_names

tools = builtin_tools(
    'file_read', 'grep', 'git_diff',
    limits={
        'file_read': {'max_read_chars': 32000},                        # 读取窗口放大
        'grep': {'max_grep_chars': 12000},                             # 限制搜索结果
        'git_diff': {'max_diff_chars': 24000, 'max_status_chars': 4000},
    },
)
print(profile_tool_names('single2'))    # 查询 Agent 配置的默认工具顺序
print(profile_tool_limits('single2'))   # 查询默认上限，返回独立字典
```

| 工具 | 字段 | 含义 |
| --- | --- | --- |
| `bash` | `max_output_chars` | 单条命令输出的字符上限 |
| `file_read` | `max_read_chars` | 文件读取内容的字符上限 |
| `grep` | `max_grep_chars` | 搜索结果的字符上限 |
| `git_diff` | `max_diff_chars`、`max_status_chars` | 差异和状态各自的字符上限 |

- 取值是 1 到 10,000,000 的整数。
- Workflow 启用了 Agent 配置时，`builtin_tools` 先继承该配置的默认上限，再应用显式的 `limits`。
- `evidence_tools` 接受同样的参数，并额外记录真实测试结果，见 [测试证据](../observability/run-results.md#用测试证据确认做对了)。

上限只决定一次工具结果显示多少。与其一味调大，不如缩小范围：大文件用 `offset` 和 `limit` 分页读，搜索缩小 `path` 和 `glob`，差异按文件看或用 `stat_only`。

## 文件工具：读、搜、改

先记住一个和直觉不同的地方：**所有路径都相对于 Agent 自己的执行工作区**，而不是你启动进程的目录。Agent 在工作树或容器里时，改的是那里的副本。读写时还会检查路径越界、符号链接和文件类型。

| 想做什么 | 工具与模式 | 关键参数 |
| --- | --- | --- |
| 分页读文件 | `file_read` | `path`、`offset`、`limit` |
| 搜索 | `grep` | `pattern`（正则）、`path`、`glob`、`max_results`（默认 50） |
| 新建或整体替换 | `file_write` 的 `create` | `content`；大幅缩短已有长文件需 `overwrite=true` |
| 替换一段唯一的原文 | `file_write` 的 `str_replace` | `old_str`、`new_str` |
| 按行替换或插入 | `apply_patch` 的 `line_replace` | `start_line`、`end_line`、`expected_str`、`new_str` |
| 一次改多处 | `apply_patch` 的 `unified_diff` | `patch` |
| 查看改动 | `git_diff` | `path`、`staged`、`stat_only`、`include_status` |

具体参数以所选工具的 schema 为准。

### 读取与定位

```json
{"path": "src/parser.py", "offset": 1, "limit": 120}
```

大文件分段返回，看结果里实际显示的范围和下一段的位置再继续读。可以先用 `grep` 找到函数，再读附近的内容。单个工具的输出上限和历史压缩可能让模型看不到全部内容，但文件本身仍完整地在执行环境里，用更窄的范围重读即可。

### 四种编辑方式

**整体写入 `create`**：完整内容放进 `content`，也能整体替换已有文件。新内容让一个已有的长文件大幅变短时，必须显式传 `overwrite=true`，这是防止误覆盖的保护。

```json
{"path":"settings.py","mode":"create","content":"RETRIES = 2\n"}
```

**局部替换 `str_replace`**：`old_str` 必须在文件里恰好出现一次；不唯一时，多带几行邻近代码。

```json
{"path":"settings.py","mode":"str_replace","old_str":"RETRIES = 2","new_str":"RETRIES = 3"}
```

**按行替换 `line_replace`**：

```json
{"path":"settings.py","mode":"line_replace","start_line":1,"end_line":1,"expected_str":"RETRIES = 3","new_str":"RETRIES = 4","relocate_expected":true}
```

行号从 1 开始，`end_line` 包含在范围内；`end_line` 等于 `start_line - 1` 时表示在该行之前插入。`expected_str` 用来核对当前范围的原文，适合读过文件之后再编辑；行号过期、但原文在文件里唯一时，显式开启 `relocate_expected` 让工具先找到原文再改。

**一次改多处 `unified_diff`**：

```json
{"path":"settings.py","mode":"unified_diff","patch":"@@ -1 +1 @@\n-RETRIES = 4\n+RETRIES = 5\n"}
```

`patch` 接受标准的 `@@ -a,b +c,d @@` hunk，可以省略文件头。工具按上下文和旧内容匹配，容许行号小幅漂移；显式 `normalize_hunks=true` 会重算 hunk 行数，并允许上下文唯一的裸 `@@` 标头。只要有一个 hunk 匹配失败，整个编辑都不生效，文件保持原样。

用 `FileWriteTool(allow_create=False)` 禁止新建文件时，模型看到的 `mode` 只剩 `str_replace`；成员自己写的子 Workflow 也会继承这项限制。

### 改完之后

用 `git_diff` 或原生 Git 命令确认改动范围，再跑相关测试。写入成功只说明文件写好了，功能对不对要靠项目的实际验证。

| `git_diff` 参数 | 作用 |
| --- | --- |
| `path` | 只看某个文件 |
| `staged=true` | 看暂存区内容 |
| `stat_only=true` | 只看文件级统计 |
| `include_status=false` | 省略状态部分 |

默认显示相对 HEAD 的未提交改动，并附上未被忽略的未跟踪文件。结果可能被截断，之后可以按文件继续看。

### 路径为什么被拒绝

内置的本地文件适配器会检查路径、链接和是否为普通文件；容器里的路径由对应的环境检查。遇到链接或越界路径被拒绝时，先确认目标文件确实是任务需要的；是的话，通过环境配置安排正确的工作目录或挂载，而不是绕过检查。

注意：这些路径限制**只约束原生文件工具**。本机上的 `bash` 仍然拥有本机进程的全部权限，两者要分开看。

### 多个 Agent 同时修改

共享目录里的成员看到的是同一份文件。文件写锁能保护单次原生编辑，但多个任务的修改仍可能互相影响。想让各自独立实现、再挑结果，就用工作树或候选环境，再通过明确的采纳交付。

出现“Agent 已经改好，但源目录没变”时，先看当前环境和候选的交付状态，再怀疑编辑工具：隔离任务在自己的目录里修改后，要通过采纳或恢复补丁把结果带回目标目录，见 [隔离改动怎样回到项目](../isolation/candidate-delivery.md)。

## bash：命令在哪里执行、谁允许它执行

`bash` 在 Agent 绑定的执行环境里运行命令：本地环境和本地工作树用本机进程权限，Docker 环境在容器里，VM 策略通过 SSH 在来宾机里执行（网络和系统权限由来宾机自己的设置决定）。

最小调用：

```json
{"command":"python -m pytest tests/test_parser.py -q","timeout":180}
```

| 参数 | 说明 |
| --- | --- |
| `command` | 要执行的命令 |
| `timeout` | 秒数，默认 120 |

`max_output_chars` 不是调用参数，而是构造工具时通过 `limits` 设置。两条执行规则：

- 命令在非交互环境运行。带分页器或确认提示的命令，要加上对应的非交互选项，否则无法完成。
- 每次调用都从绑定的工作区开始，上一次的 `cd` 不会保留。要进子目录，就在同一条命令里完成切换和执行，或者用绝对路径。

### 一条命令要过三道关

一条命令最终能不能跑、跑在哪里，由三层共同决定：执行环境、角色的工具授权、风险命令确认策略。OC 默认偏保守，在本机直接执行 shell 需要显式开关：

| 设置 | 管理的行为 |
| --- | --- |
| CLI `--yolo` | 自动批准被判定为需要确认的风险命令 |
| CLI `--allow-local-child-shell` | 允许运行中派生的子成员在没有系统进程沙箱时执行本机 shell |
| SDK 或 Workflow 的 `allow_unisolated_shell` | 显式允许这次运行使用本机 shell |
| `builtin_tools(..., headless=False)` | 让构造出的 shell 工具允许本机执行方式 |

这些开关只放开权限，**不会自动加工具**：团队配置和工具集合里仍然要有 `bash`。还有一点容易忽略：Workflow 的 Python 编排代码本身跑在调用方进程里，你在业务代码里直接调用本机文件或子进程操作，不经过 `bash` 工具，要单独考虑影响范围。

执行不可信的项目代码时，优先配置进程和网络隔离，见 [隔离](../isolation/isolation-overview.md)。

### 怎样确认命令真的成功

1. 读工具返回的退出状态、标准输出、错误输出和超时标志。
2. 测试类命令，要确认预期的测试目标确实执行了：`--help` 的输出只说明用法，测试收集结果只说明有哪些待执行目标，都不代表通过。
3. 需要机器可读的测试证据时，用 `evidence_tools` 和支持的 runner，见 [测试证据](../observability/run-results.md#用测试证据确认做对了)。

长命令要区分三种期限：命令自身的 `timeout`、Agent 的运行期限、模型请求超时。调大模型超时，没法延长一个已经被 `bash` 工具终止的进程。调整方法见 [工具执行卡住时调哪一层](../operations/timeouts-retries.md#工具执行卡住时调哪一层)。取消运行时，框架清理它自己创建的命令和环境；调用方传入的环境由调用方继续管理。

## 常见问题

| 报错或现象 | 原因与方向 |
| --- | --- |
| 工具名称重复被拒绝 | 同一个工具列了两次 |
| `limits` 报错 | `limits` 里出现了没有选中的工具 |
| `builtin_tools` 传 `spawn_agent` 报错 | `builtin_tools` 只接受普通编码工具；协作工具要在 Team 角色里声明，并确保拓扑允许 |
| 模型只输出文字，不调用工具 | 先检查实际发出的工具 schema，再看提示词和工具选择设置 |
| 命令找不到依赖 | 检查执行环境或镜像里是否安装 |
| 命令的工作目录不对 | 每次调用都从工作区开始，`cd` 不保留 |
| 环境已被撤销 | 环境已经释放，查看清理状态 |
| 权限被拒绝 | 先判断是哪一层拦的：角色工具、命令授权（上面的开关和 `headless`）、路径检查，还是环境限制 |
| 测试断言失败 | 这是真实的测试结果，按项目问题处理 |

保留原命令、退出状态和相关环境设置，比重新启动整个任务更容易定位原因。工具列表决定能请求什么，工具回执记录实际执行了什么。

## 延伸阅读

- 源码依据：[opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/tools.py) · [opencollab/bootstrap/tool_registry.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/tool_registry.py) · [opencollab/adapters/tools/fs.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/fs.py) · [opencollab/adapters/tools/apply_patch.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/apply_patch.py) · [opencollab/adapters/safe_anchored_files.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/safe_anchored_files.py) · [opencollab/adapters/tools/bash.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/bash.py) · [opencollab/adapters/safety.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/safety.py)
- 相关指南：[自定义工具](custom-tools.md) · [隔离](../isolation/isolation-overview.md) · [判断任务是否真的完成](../observability/run-results.md) · [Team 入门](../team/configure-team.md)
