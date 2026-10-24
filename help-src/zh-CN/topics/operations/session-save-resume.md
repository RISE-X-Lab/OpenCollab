---
{
  "id": "session-save-resume",
  "title": "保存会话与恢复历史",
  "category": "operations",
  "summary": "OC 终端团队自动保存成员会话；用 --session 恢复主会话历史，累计 token 和步数随之恢复，未完成的协程不会续跑。",
  "aliases": [
    "断点续跑",
    "继续上次对话",
    "会话 JSON",
    "旧 JSONL",
    "自动保存",
    "恢复预算",
    "workflow.json",
    "team.json"
  ],
  "keywords": [
    "/save",
    "--session",
    "-s",
    "OpenCollab.read_session_snapshot",
    ".opencollab/sessions",
    "agent_0",
    "--save",
    "--no-save"
  ],
  "questions": [
    "上次的会话保存在哪里？",
    "怎么恢复上次的对话继续输入？",
    "恢复会话后预算会重置吗？",
    "工作流怎么关闭保存或轨迹？"
  ],
  "related": [
    "interactive-cli-controls",
    "token-step-concurrency-limits",
    "cancellation-cleanup",
    "configuration-diagnostics",
    "artifacts-and-history",
    "trace-and-events"
  ],
  "verified_ref": "0c47f74a"
}
---

# 保存会话与恢复历史

OC 的终端团队会自动把每个成员的会话存成 JSON。下次启动时用 `--session PATH`（简写 `-s`）恢复主会话历史，接着输入。恢复时 token 和步数的累计用量会一起带回来，但原进程里正在跑的 provider 或工具协程不会续上，会话从 `IDLE` 重新开始。要沿用正确的项目工作区和模型设置。

## 最小用法

```bash
# 恢复入口成员（lead）的会话，然后继续对话
opencollab --workspace /path/to/application \
  --session /absolute/path/agent_0_analyst.json
```

- 也可以传手动 `/save` 导出的 JSONL。该选项接受旧的 JSONL 和 Agent JSON。
- 找不到快照及其可恢复记录时，报告文件不存在。文件结构或记录损坏时，报告读取错误。
- 恢复入口只读主会话历史。团队成员和工作流各会话的记录适合分别检查。团队结构仍由当前选中的团队配置创建。
- 恢复后先看历史里最后的工具结果和待办，再提交一个范围明确的后续任务。

## 文件存在哪里

| 运行类型 | 目录 | 文件 |
| --- | --- | --- |
| 终端团队 | `<workspace>/.opencollab/sessions/`，名称含时间和随机后缀 | 成员 `agent_<aid>_<role>.json`（入口可能是 `agent_0_analyst.json`），manifest `team.json` |
| 工作流 | 带 `wf-` 前缀的目录 | 顺序命名的 `000_<role>.json` 等，`workflow.json`；启用轨迹时还有 `orchestration.jsonl` |
| 手动 `/save` | 当前启动目录 | `session-<随机值>.jsonl` |

- 实际目录以终端显示的路径为准。
- 一次运行的多个会话靠 manifest 关联。查看时先用 manifest 确认运行标识。
- `/save` 总是导出 lead 主会话。切换到其他成员后执行，保存的仍是 lead。需要完整团队历史，就保留自动保存目录。

## 恢复后预算和执行状态

- token 和步数按会话整个生命周期计。快照恢复累计用量、结束状态和可恢复的等待事件。
- 新一轮用户输入会重置当前轮的局部执行状态，但继续使用会话累计额度。上次已经耗尽额度时，要结合原有总消耗设定新的限制。
- 正在进行的 provider 或工具协程属于原进程，恢复时从 `IDLE` 重新进入执行。重启前先检查原任务对应的进程和文件状态。
- 快照里已有的工具结果、待配对结果和后续消息按存储规则恢复。

## 用 SDK 离线读取

下面的脚本只读保存内容，会合并存储适配器认可的已完成 journal 记录。适合先确认消息数量和会话状态，再安排恢复运行。

```python
from pathlib import Path
from opencollab import OpenCollab

path = Path("/absolute/path/agent_0_analyst.json")
snapshot = OpenCollab.read_session_snapshot(path)
print(len(snapshot["messages"]))
print(snapshot.get("session_state", {}))
print(snapshot.get("run_id"))
```

- 迁移或备份运行目录时，连同已有的 sidecar 文件一起保留。读取时走公开存储入口。
- `run_id` 用来关联会话、manifest 和轨迹。调用方也可以为新的 SDK 运行指定这个值。

## 工作流的保存设置

工作流 CLI 默认开启 `--save` 和 `--trace`：

| 选项 | 效果 |
| --- | --- |
| `--no-trace` | 保留会话和 manifest，不记录编排轨迹 |
| `--no-save` | 关闭运行产物。轨迹依赖保存目录，所以也不会有轨迹 |

这些参数放在 `workflow run NAME` 后面。下面的 Duo 示例显式允许在宿主工作区执行 shell：

```bash
opencollab workflow run duo --workspace /path/to/application \
  --task '完成指定任务。' --args '{"allow_unisolated_shell":true}' \
  --save --no-trace
```

需要事后复查长任务时，保留保存目录。每次继续前，确认工作区文件和最后的记录一致。

## 来源

[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L69)　[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L176)　[opencollab/bootstrap/scheduler_factory.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/scheduler_factory.py#L83)　[docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/runtime-design.md#L8)

## 相关指南

[在终端里和 Agent 对话，以及命令参数该写在哪](../setup/interactive-cli-controls.md)　[设置 token 预算、步数与运行并发](token-step-concurrency-limits.md)　[停止任务并确认清理完成](cancellation-cleanup.md)　[检查生效配置与排查启动故障](configuration-diagnostics.md)　[找到运行记录、会话和交付文件](../observability/artifacts-and-history.md)　[查看 Agent 做了什么](../observability/trace-and-events.md)
