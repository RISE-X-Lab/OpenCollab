---
{
  "id": "run-results",
  "title": "读取运行结果与判断任务是否完成",
  "category": "observability",
  "summary": "看懂 SDK 返回的 RunResult，处理 RunError，并用测试确认任务真的做好了。",
  "aliases": [
    "completed",
    "stopped",
    "failed",
    "raise_for_status",
    "RunError",
    "ok",
    "运行状态"
  ],
  "keywords": [
    "运行结果",
    "任务完成",
    "RunResult",
    "agent_failures"
  ],
  "questions": [
    "正确处理 RunResult 和 RunError，把框架完成状态与实际任务验证分开理解？",
    "status 是 completed，是不是说明任务做对了？",
    "怎么让运行失败时直接抛异常？"
  ],
  "related": [
    "artifacts-and-history",
    "native-test-evidence",
    "cancellation-cleanup"
  ],
  "verified_ref": "0c47f74a"
}
---

# 读取运行结果与判断任务是否完成

SDK 的 `agent`、`team`、`workflow` 都返回 `RunResult`，统一用 `status`（`completed`、`stopped`、`failed`）加 `reason` 描述运行怎样结束。`stopped` 表示运行因限制、超时或停止而提前结束，具体原因看 `reason`。希望非完成状态直接抛 `RunError`，显式调用 `raise_for_status()`。`completed` 只说明运行入口正常结束，任务是否做对由应用自己的测试或验收决定。

## 最小示例

下面片段放在已构造 `oc` 的异步函数中。

```python
result = await oc.agent("分析当前项目的测试入口", budget=50_000)
if result.ok:
    print(result.output)
else:
    print(result.status, result.reason)  # 为什么没完成
    print(result.artifacts)              # 记录保存在哪里
```

先看 `status` 和 `reason`，再处理输出和产物。

如果希望失败时直接进入异常处理，就调用 `result.raise_for_status()`：

- 结果是完成状态，返回它自己。
- 其它状态，抛出 `RunError`。异常的 `result` 属性保留对应的结果。

## 字段一览

| 字段 | 含义 |
| --- | --- |
| `output` | 当前入口返回的文字或结构化结果 |
| `status` | `completed`、`stopped` 或 `failed` |
| `reason` | 停止或失败的原因 |
| `tokens` | 已记录的 token 用量，可能没有 |
| `artifacts` | 已保存产物的目录 |
| `error` | 保留的异常对象，适合在本进程里诊断 |
| `metrics` | 运行指标，以及补充的恢复、清理信息 |
| `agent_failures` | 工作流里记录的子 Agent 失败 |

`ok` 在 `status == "completed"` 时为真。

工作流可能处理掉某个子 Agent 的失败后照常返回。所以即使 `ok` 为真，也要看一眼 `agent_failures`。

## 调用本身也可能抛错

不是所有问题都体现在 `RunResult` 里：

- 参数无效，可能在执行前就报错。
- 生命周期或保存出故障，可能直接抛出 `RunError`。
- 用户取消走的是异步取消机制，要在应用的取消处理里等待清理完成，见 [停止任务并确认清理完成](../operations/cancellation-cleanup.md)。

出错时，把已有的输出、用量和恢复信息连同错误一起保存。

## 交付前还要检查什么

`completed` 和 `ok` 适合驱动应用流程。任务算不算真做好，要由应用自己定义验收条件。

编码任务至少确认：

1. 修改落在预期目录。
2. 记录里的候选和最终交付一致。
3. 跑过相关测试，见 [用实际测试验证 Agent 的交付](native-test-evidence.md)。

调查任务：检查结论有没有引用材料作支撑。

## 进阶：把结果存进业务系统

`RunResult` 不能直接写成 JSON，因为 `error`、`Path` 和自定义 Workflow 输出不一定能序列化。服务可以只挑这些字段显式保存：

- `status`
- `reason`
- `tokens`
- `str(artifacts)`
- `metrics["run_id"]`
- 必要的输出字段

示例见 [把 OpenCollab 接进自己的服务](../architecture/service-integration-lifecycle.md)。失败诊断要保留异常类型和关联记录，并按服务的访问权限保存。

## 源码与进一步阅读

[opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/result.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md)

[找到运行记录、会话和交付文件](artifacts-and-history.md) · [用实际测试验证 Agent 的交付](native-test-evidence.md) · [停止任务并确认清理完成](../operations/cancellation-cleanup.md)
