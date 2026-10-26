---
{
  "id": "team-hooks",
  "title": "Hook：在工具调用和成员生命周期里运行命令",
  "category": "extensions",
  "summary": "在 team.yaml 的 hooks 下配置命令，在工具调用前后或成员启动、结束时运行；Hook 只观察，失败只留警告，不会拦截工具。",
  "aliases": [
    "PreToolUse",
    "PostToolUse",
    "SubagentStop",
    "执行前检查",
    "执行后处理",
    "SessionStart",
    "Stop",
    "Notification",
    "OPENCOLLAB_HOOK_EVENT"
  ],
  "keywords": [
    "Hook",
    "钩子"
  ],
  "questions": [
    "在规定的运行事件上执行自定义命令，并理解命令在哪个环境运行？",
    "Hook 能阻止工具执行吗？",
    "Hook 命令在宿主还是容器里运行？",
    "Hook 脚本怎样拿到事件数据？"
  ],
  "related": [
    "configure-team",
    "tool-selection",
    "artifacts-and-history"
  ],
  "verified_ref": "0c47f74a"
}
---

# Hook：在工具调用和成员生命周期里运行命令

Hook 让你在特定时刻自动运行一条 shell 命令：工具执行前后、成员创建时、成员结束时。OC 的事件名和 Claude Code 同名（`PreToolUse`、`PostToolUse`、`SubagentStop` 等），配置写在团队 YAML 顶层的 `hooks` 下，事件数据以 JSON 从标准输入传给命令。

和 Claude Code 最大的不同是：**OC 的 Hook 只用于观察和辅助动作**。即使 `PreToolUse` 的命令返回非零，也只留下一条警告，工具调用照常执行。要拦截危险操作，请用工具授权、权限策略和隔离环境，见 [内置工具](tool-selection.md#一条命令要过三道关)。

## 最小用法

1. 在团队 YAML 顶层加入（示例假定任务环境里有 `python3` 和这个脚本）：

   ```yaml
   hooks:
     PreToolUse:
       - matcher: file_write
         command: python3 scripts/observe_tool.py
         timeout: 20
   ```

2. 写一个只打印摘要的脚本，先确认事件和执行目录对不对，再接入自己的记录系统：

   ```python
   import json
   import os
   import sys

   payload = json.load(sys.stdin)              # 事件 JSON 从标准输入读
   print(os.environ.get("OPENCOLLAB_HOOK_EVENT"))
   print(payload.get("tool"), payload.get("aid"))
   ```

3. 用一个真实事件跑一遍，确认脚本读到了标准输入、进入了预期目录、写出了需要的记录，再添加其他操作。

## 动作配置

| 字段 | 说明 |
| --- | --- |
| `matcher` | 工具名 glob，例如 `file_*` 同时匹配 `file_read` 和 `file_write`。省略时匹配该事件的所有调用。生命周期事件应省略，因为它们的数据里没有工具名 |
| `command` | 要执行的命令 |
| `timeout` | 秒数，默认 30，必须是有限正数 |
| `type` | 默认 `command`，当前只接受这一种 |

匹配到的多条命令按配置顺序依次执行并等待完成，然后事件处理才继续推进。

## 事件一览

| 事件 | 触发时机 | 常用数据 | 在哪里运行 |
| --- | --- | --- | --- |
| `PreToolUse` | 工具开始执行 | `aid`、`tool`、`args` | 调用工具的 Agent 的执行环境 |
| `PostToolUse` | 工具执行结束 | Agent 与工具信息及执行结果 | 调用工具的 Agent 的执行环境 |
| `SessionStart` | Team 成员创建 | 新成员信息 | 宿主工作区 |
| `Stop` | 入口成员完成、失败或取消 | `disposition` 为 `completed`、`failed` 或 `cancelled` | 宿主工作区 |
| `SubagentStop` | 子成员完成、失败或取消 | 成员信息与终止状态 | 宿主工作区 |
| `Notification` | Team 发出错误事件 | 对应的错误信息 | 宿主工作区 |

运行位置要特别注意：

- **工具 Hook 跟着 Agent 走。** Agent 在工作树或容器里，Hook 也在那里运行，脚本和解释器必须在那个环境里可用。环境已释放或不可用时，工具 Hook 会被跳过并留下警告。
- `SubagentStop` 发生在子任务改动提取之后，适合观察已经生成的交付信息。

除了标准输入，命令还能读这些环境变量：`OPENCOLLAB_HOOK_EVENT`（事件名）、`OPENCOLLAB_TOOL`（工具名）、`OPENCOLLAB_AID`（成员编号）。各事件提供的字段不同，脚本要按对应事件读取。

## 失败与超时

- 普通命令失败或超时，只留下警告。
- 命令末尾带后台符号 `&` 时，这个动作会被跳过并留下警告。长驻的后台任务应由你的应用自己的服务生命周期管理。
- 取消和进程清理出错会影响任务结束和资源释放，具体原因记录在运行日志里。

## 延伸阅读

- 源码依据：[configs/team.example.yaml](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/team.example.yaml) · [configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/README.md) · [opencollab/adapters/hooks.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/hooks.py) · [opencollab/domain/hooks.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/domain/hooks.py)
- 相关指南：[Team 入门](../team/configure-team.md) · [内置工具](tool-selection.md) · [运行记录](../observability/artifacts-and-history.md)
