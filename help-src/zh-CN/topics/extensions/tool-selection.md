---
{
  "id": "tool-selection",
  "title": "选择 Agent 能使用的工具",
  "category": "extensions",
  "summary": "用 builtin_tools 挑选普通编码工具并设置输出上限；Team 协作工具只能在团队角色里声明。",
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
    "agent_tool_names"
  ],
  "keywords": [
    "工具",
    "工具列表",
    "工具权限"
  ],
  "questions": [
    "按任务给 Agent 配置文件、命令、协作或技能工具并理解权限边界？",
    "怎样做一个只读的 Agent？",
    "为什么 builtin_tools 传 spawn_agent 会报错？",
    "怎样调整工具输出的字符上限？",
    "怎样确认 Agent 实际拿到了哪些工具？"
  ],
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 选择 Agent 能使用的工具

在 OC 里，Agent 能请求哪些操作完全由它的工具列表决定。OC 把工具分成两组分别组装：普通编码工具用公开函数 `builtin_tools(...)` 构造，可以直接传给 SDK；需要调度器、技能存储等运行时能力的工具（如 `spawn_agent`、`use_skill`），由 Team 运行环境提供，只能在团队角色的 `tools` 里按注册名声明。最小用法是 `builtin_tools("file_read", "grep", "git_diff")`，得到一个只读 Agent。

## 工具一览

| 类别 | 名称 | 作用 | 怎么给 |
| --- | --- | --- | --- |
| 文件与检索 | `file_read`、`grep` | 读项目文件、搜索 | `builtin_tools` 或角色 `tools` |
| 编辑与差异 | `file_write`、`apply_patch`、`git_diff` | 修改文件、查看变化 | `builtin_tools` 或角色 `tools` |
| 命令执行 | `bash` | 在指定环境执行命令 | `builtin_tools` 或角色 `tools` |
| Team 协作 | `spawn_agent`、`message_agent`、`team_status`、`adopt_agent` | 派发、通信、状态、交付 | 只能在角色 `tools` 里声明 |
| 扩展与组合 | `use_skill`、`run_workflow`、`manage_workflow` | 载入指导、执行工作单元 | 在角色 `tools` 里声明 |

## 最小示例：只读 Agent

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

确认实际选中了哪些工具，看运行指标 `agent_tool_names`。`profile_tool_names("base")` 返回的是 profile 的默认列表，显式传入工具后，以实际运行记录为准。

## 有工具不等于能执行

- `builtin_tools` 的 `headless=True` 要求 shell 工具运行在有进程隔离的环境里。明确传 `headless=False` 才允许宿主 shell，要结合实际运行环境确认。
- 工作树只解决目录分离，宿主权限和网络仍由父环境决定。
- `allow_file_creation=False` 只关闭原生文件工具的创建能力。Bash 能做什么是另一回事，要通过独立的权限和环境策略管理，见 [管理终端权限并确认命令结果](shell-permissions-execution.md)。

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
print(profile_tool_names('single2'))    # 查询 profile 的默认工具顺序
print(profile_tool_limits('single2'))   # 查询 profile 的默认上限，返回独立字典
```

| 工具 | 字段 | 含义 |
| --- | --- | --- |
| `bash` | `max_output_chars` | 单条命令输出的字符上限 |
| `file_read` | `max_read_chars` | 文件读取内容的字符上限 |
| `grep` | `max_grep_chars` | 搜索结果的字符上限 |
| `git_diff` | `max_diff_chars`、`max_status_chars` | 差异和状态各自的字符上限 |

规则：

- 取值是 1 到 10,000,000 的整数。
- 工作流启用了 profile 时，`builtin_tools` 先继承该 profile 的默认上限，再应用显式的 `limits`。
- `evidence_tools` 接受同样的参数，并额外记录真实测试结果。

上限只决定一次工具结果显示多少。与其一味调大，不如缩小范围：大文件用 `offset` 和 `limit` 分页读，搜索缩小 `path` 和 `glob`，差异按文件看或用 `stat_only`。

## 常见问题

| 报错或现象 | 原因 |
| --- | --- |
| 工具名称重复被拒绝 | 同一个工具列了两次 |
| `limits` 报错 | `limits` 里出现了没有选中的工具 |
| `builtin_tools` 传 `spawn_agent` 报错 | `builtin_tools` 只接受普通编码工具。协作工具要在 Team 角色里用注册名声明，并确保拓扑允许相关操作 |
| 模型只输出文字，不调用工具 | 先检查实际发出的工具 schema，再看提示词和工具选择设置 |

工具列表决定能请求什么，工具回执记录实际执行了什么。

## 源码与进一步阅读

[opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/tools.py) · [opencollab/bootstrap/tool_registry.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/tool_registry.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md)
