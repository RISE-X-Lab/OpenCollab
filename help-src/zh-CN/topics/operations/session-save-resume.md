---
{
  "id": "session-save-resume",
  "title": "保存与恢复会话",
  "category": "operations",
  "summary": "终端团队自动保存每个成员的会话；用 --session 恢复主会话继续对话，累计用量随之恢复，正在进行的请求和工具不会续上。",
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
    "runtime-execution-model",
    "artifacts-and-history",
    "timeouts-retries"
  ],
  "verified_ref": "v0.9.6"
}
---

# 保存与恢复会话

OC 的终端团队会自动把每个成员的会话存成 JSON 文件。下次启动时用 `--session PATH`（简写 `-s`）恢复主会话的历史，就能接着对话。

恢复时要分清“回来的”和“回不来的”：token 和步数的累计用量、结束状态、可恢复的等待关系会一起带回来；但原进程里**正在进行**的模型请求或工具执行不会续上，会话从空闲状态（`IDLE`）重新开始。为什么是这样，见 [原理：Session 的状态](../architecture/runtime-execution-model.md#session-里存了什么)。

## 最小用法

```bash
# 恢复入口成员（lead）的会话，然后继续对话
opencollab --workspace /path/to/application \
  --session /absolute/path/agent_0_analyst.json
```

- 也可以传手动 `/save` 导出的 JSONL，这个选项接受旧的 JSONL 和 Agent JSON。
- 找不到快照及其可恢复记录时，报告文件不存在；文件结构或记录损坏时，报告读取错误。
- 恢复入口只读**主会话**历史。团队成员和 Workflow 各会话的记录适合分别检查；团队结构仍由当前选中的团队配置创建。
- 要沿用正确的项目工作区和模型设置。恢复后先看历史里最后的工具结果和待办，再提交一个范围明确的后续任务。

## 文件存在哪里

| 运行类型 | 目录 | 文件 |
| --- | --- | --- |
| 终端团队 | `<workspace>/.opencollab/sessions/`，名称含时间和随机后缀 | 成员 `agent_<aid>_<role>.json`（入口可能是 `agent_0_analyst.json`），清单 `team.json` |
| Workflow | 带 `wf-` 前缀的目录 | 顺序命名的 `000_<role>.json` 等，`workflow.json`；启用轨迹时还有 `orchestration.jsonl` |
| 手动 `/save` | 当前启动目录 | `session-<随机值>.jsonl` |

- 实际目录以终端显示的路径为准。一次运行的多个会话靠清单关联，查看时先用清单确认运行标识。
- `/save` 总是导出 lead 主会话：切换到其他成员后执行，保存的仍是 lead。需要完整的团队历史，就保留自动保存的目录。
- SDK 运行的产物目录结构见 [运行记录](../observability/artifacts-and-history.md)。

## 恢复之后，预算和执行状态怎样

- token 和步数按会话的整个生命周期计。快照恢复累计用量、结束状态和可恢复的等待事件。
- 新一轮用户输入会重置当前轮的局部执行状态，但继续使用会话的累计额度。上次已经用完额度时，要结合原有的总消耗设定新的限制。
- 正在进行的 provider 或工具协程属于原进程，恢复时从 `IDLE` 重新进入执行。重启前先检查原任务对应的进程和文件状态。
- 快照里已有的工具结果、待配对的结果和后续消息，按存储规则恢复。

## 用 SDK 离线读取

下面的脚本只读保存的内容，会合并存储适配器认可的已完成日志记录，适合先确认消息数量和会话状态，再安排恢复运行：

```python
from pathlib import Path
from opencollab import OpenCollab

path = Path("/absolute/path/agent_0_analyst.json")
snapshot = OpenCollab.read_session_snapshot(path)
print(len(snapshot["messages"]))
print(snapshot.get("session_state", {}))
print(snapshot.get("run_id"))
```

- 迁移或备份运行目录时，连同已有的附属文件一起保留，读取时走公开的存储入口。
- `run_id` 用来关联会话、清单和轨迹；调用方也可以为新的 SDK 运行指定这个值。

## Workflow 的保存设置

Workflow CLI 默认开启 `--save` 和 `--trace`：

| 选项 | 效果 |
| --- | --- |
| `--no-trace` | 保留会话和清单，不记录编排轨迹 |
| `--no-save` | 关闭运行产物；轨迹依赖保存目录，所以也不会有轨迹 |

这些参数放在 `workflow run NAME` 后面。下面的 Duo 示例显式允许在本机工作区执行 shell：

```bash
opencollab workflow run duo --workspace /path/to/application \
  --task '完成指定任务。' --args '{"allow_unisolated_shell":true}' \
  --save --no-trace
```

注意：保存下来的是**执行证据**，不是 Workflow 的程序状态，Workflow 不能从任意一行代码自动续跑，见 [进程退出后能接着跑吗](../workflow/workflow-failure-recovery-lifecycle.md#进程退出后能接着跑吗)。需要事后复查长任务时保留保存目录；每次继续前，确认工作区文件和最后的记录一致。

## 延伸阅读

- 源码依据：[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/cli/main.py) · [opencollab/bootstrap/scheduler_factory.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/scheduler_factory.py) · [docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/runtime-design.md)
- 相关指南：[在终端里使用 OC](../setup/interactive-cli-controls.md) · [原理：一次运行在内部怎样推进](../architecture/runtime-execution-model.md) · [运行记录](../observability/artifacts-and-history.md) · [运行怎样停下](timeouts-retries.md)
