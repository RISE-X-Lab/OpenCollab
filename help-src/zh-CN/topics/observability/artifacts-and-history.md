---
{
  "id": "artifacts-and-history",
  "title": "找到运行记录、会话和交付文件",
  "category": "observability",
  "summary": "任务跑完后，去哪个目录找结果、对话记录和执行日志，哪些文件可以放心删。",
  "aliases": [
    "artifacts",
    "会话文件",
    "日志目录",
    "workflow.json",
    "team.json",
    "no-save",
    "no-trace",
    "运行记录在哪",
    "产物文件夹"
  ],
  "keywords": [
    "运行文件",
    "产物目录",
    "记录在哪里",
    "trajectory.jsonl",
    "orchestration.jsonl"
  ],
  "questions": [
    "运行结束后定位对应目录，区分产物、原始会话、轨迹和可再生缓存？",
    "Agent 跑完了，它的对话记录和日志存在哪里？",
    "哪些运行文件可以删，哪些要留着？"
  ],
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 找到运行记录、会话和交付文件

OC 把每次运行的产物写进一个目录：Session 快照（`agent.json` 等，含完整模型与工具历史）、可选的 JSONL 轨迹，以及 Team/Workflow 的清单文件（`team.json`、`workflow.json`）。SDK 用 `artifacts=` 指定目录，必须是新目录或空目录。Workflow 默认保存会话并记录编排轨迹。本文给出各入口的文件对照、CLI 保存开关，以及哪些文件可以清理。

## 第一步：指定保存目录

SDK 用 `artifacts=` 指定本次运行的保存目录。这个目录必须是新目录或空目录。

```python
# 放在异步函数中运行
result = await oc.agent(
    "阅读项目并给出修改建议",
    artifacts="artifacts/review-parser",
)
print(result.artifacts)  # 实际保存位置
```

建议：

1. 按任务标识给每次运行分配独立目录，避免两个运行写同一份结果文件。
2. 运行结束后，以 `RunResult.artifacts` 返回的位置为准。
3. 想重复使用某个目录，先处理里面已有的内容，保留正式结果和待恢复材料。

## 第二步：认出目录里的文件

| 入口 | 常见保存文件 | 用来看什么 |
| --- | --- | --- |
| SDK Agent | `agent.json`，启用轨迹时的 `trajectory.jsonl` | 单次会话快照与执行事件 |
| Team | `team.json`、`agent_<aid>_<role>.json`，SDK 启用轨迹时的 `trajectory.jsonl` | 实际成员名册、每个成员的会话、协作事件 |
| Workflow | `workflow.json`、`<seq>_<role>.json`，启用轨迹时的 `orchestration.jsonl` | 入口与参数、每个步骤的会话、编排事件 |

文件名里的角色或步骤标签会被转换成适合做文件名的形式。

完整会话可能另有一份持久化日志。读取时用公开的 `OpenCollab.read_session_snapshot(path)`，它会把快照和已完成的日志条目合并起来。示例见 [Session 怎样保存进度和继续执行](../architecture/session-state-lifecycle.md)。

## 第三步：按标识串起记录

1. 先找到这次运行的 `run_id`。
2. 再按 Agent 或 Session 标识，把模型调用和工具回执对上。
3. 同一个角色跑了多次时，用会话标识区分每一次。角色名只说明它负责什么。

各类清单记了什么：

- Team 清单保留所选的团队文件和实际成员。完整的通信拓扑要从对应 YAML 读取。
- Workflow 清单描述工作流本身和它的执行记录。
- 动态子 Workflow 的产物归入父运行，同时保留子运行自己的身份。

## 保存开关怎么选

| 场景 | 做法 |
| --- | --- |
| Workflow 默认 | 保存会话，并记录编排过程 |
| CLI 不要轨迹 | `--no-trace`：仍保留会话与工作流清单 |
| CLI 基本不保存 | `--no-save`：关闭通常的运行产物保存 |
| 交互 CLI 要轨迹 | `--trace` |
| 交互 CLI 保存主会话 | `/save` |

隔离入口或候选交付失败时，恢复机制会按需要单独保留候选。这和普通轨迹开关是两回事，分别处理。恢复目录在哪里，以结果和错误信息中返回的实际路径为准。

## 记录缺失时先看什么

`workflow.json` 里除了运行状态、原因、已用 token 和预算，还有这些字段：

| 字段 | 说明 |
| --- | --- |
| `evidence_complete` | 运行记录与清理是否完成 |
| `trace_enabled` | 是否开启了轨迹 |
| `tracer_write_error` | 轨迹写入出错的信息 |
| `tracer_dropped_steps` | 轨迹丢掉的记录 |
| `tracer_failure` | 轨迹保存失败情况 |

如果发现记录缺了，就先读这几个字段，再去查对应会话和原始工具结果。

注意：`evidence_complete` 只说明记录和清理是否完整，不代表项目做对了。验收项目时，要另外读实际测试或验证器的结果。汇总费用时，也别把同一份记录的不同展示形式重复累加。

## 哪些该留，哪些能删

值得保留（恢复和复核要用）：

- 源码
- 候选补丁
- 正式测试结果
- 你的结论所依赖的原始记录

通常可以重新生成：

- `.pytest_cache`、`.ruff_cache`
- 有对应源码的字节码
- 能从锁文件重建的旧依赖环境

清理前按顺序确认：

1. 运行已经结束。
2. 目录没有被程序占用。
3. 只删明确指定的路径。
4. 如果隔离资源还处于保留状态，先导出恢复补丁或做完必要核对，再清理。具体步骤见恢复与清理指南。

## 源码与进一步阅读

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md) · [opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/result.py) · [opencollab/bootstrap/workflow_runtime.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/workflow_runtime.py)
