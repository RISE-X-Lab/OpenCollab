---
{
  "id": "context-compaction",
  "title": "上下文窗口与自动压缩",
  "category": "operations",
  "summary": "OC 完整保存会话，但发给模型的是按窗口整理过的视图；这里说明怎样确认窗口、何时压缩、怎样调整策略和排查溢出。",
  "aliases": [
    "自动总结",
    "爆上下文",
    "context overflow",
    "长对话",
    "工具输出截断",
    "记忆丢了",
    "RunControl",
    "context_tokens"
  ],
  "keywords": [
    "上下文压缩",
    "OPENCOLLAB_CONTEXT_WINDOW",
    "context_window",
    "context",
    "no_history_compaction",
    "tool_result_budget",
    "history_trigger_tokens",
    "session.history_compaction"
  ],
  "questions": [
    "OC 什么时候会自动压缩上下文？",
    "怎么设置或覆盖模型的上下文窗口？",
    "怎么关闭历史压缩或调低触发阈值？",
    "遇到 context length exceeded 后 OC 会怎么处理？"
  ],
  "related": [
    "thinking-reasoning-sampling",
    "token-step-concurrency-limits",
    "session-save-resume",
    "artifacts-and-history"
  ],
  "verified_ref": "v0.9.6"
}
---

# 上下文窗口与自动压缩

模型一次能读的内容有上限，这就是上下文窗口。长任务的历史迟早会超过它。OC 的做法是：**完整的会话始终保存在消息记录里，每次请求发给模型的，是按窗口整理过的一份视图**。接近窗口上限时，默认策略按顺序做三件事：缩减旧的工具结果、移除可清理的旧工具交换组、必要时生成总结。

通常你只需要确认窗口值是对的；需要时，再调团队的 `context` 策略或 `RunControl.history_trigger_tokens`。排查“模型漏了信息”或溢出停止时，要同时看保存下来的完整历史和本次请求的整理记录。

## 最小用法：确认实际窗口

窗口默认从模型能力表推断，显式的 `context_window` 可以覆盖：

```python
from opencollab import OpenCollab

client = OpenCollab(".", config={"context_window": 131072})
print(client.configuration["context_window"])
```

- 这个值要和真实的物理模型服务匹配。声明得比实际大，只会让 OC 更晚整理历史，后端的真实窗口不会变。
- env 文件里对应 `OPENCOLLAB_CONTEXT_WINDOW`；逻辑路由也能给每个物理模型单独设置窗口。
- `inspect_model_runtime` 显示安装版本内置的默认能力，客户端的显式覆盖要读 `configuration`，见 [离线查看模型能力](../models/configuration-precedence.md#离线查看模型能力)。

## 关键参数

| 参数 | 位置 | 作用 |
| --- | --- | --- |
| `context_window` | SDK `config` / `OPENCOLLAB_CONTEXT_WINDOW` | 覆盖推断出的窗口 |
| `context.policy` | 团队 YAML 顶层 | `default` 或 `no_history_compaction` |
| `context.tool_result_budget` | 团队 YAML 顶层 | 单个工具结果的字符上限，正整数 |
| `RunControl.history_trigger_tokens` | 单次运行 | 在已知窗口下调低压缩的触发值，至少为 2 |

## 调整团队和单 Agent 的策略

在已有团队 YAML 的顶层加入下面的片段，原有角色和关系保持不变：

```yaml
context:
  policy: default
  tool_result_budget: 20000  # 单位是字符
```

- `default`：低压力时保留历史，一般更适合持续编辑和验证。
- `no_history_compaction`：保留单个工具结果的大小限制，但关闭历史压缩。用它研究完整的请求历史时，输入总量必须落在后端窗口内。

`RunControl.history_trigger_tokens` 可以把触发值调得更低。下面的片段放在已有 `async` 函数里，`client` 是前面创建的客户端：

```python
from opencollab import RunControl

result = await client.agent2(
    "阅读大型项目，逐步收集主要模块的证据并给出说明。",
    tools="read",
    budget=150_000,
    run_control=RunControl(history_trigger_tokens=60_000),
)
```

整理的目标值仍是有效触发值的 75%。窗口未知时这个设置不生效，沿用固定的默认阈值。

## 遇到溢出时看什么

- 被分类为上下文溢出的 provider 错误，会触发一次强制压缩和一次重试；再次溢出就保留原因并停止。
- 受保护的输入本身过大时，仍可能占满窗口。这时要缩小任务输入，或修正窗口声明。
- 查看 `context_shaping` 事件和压缩轨迹，再结合 `reason` 判断，见 [运行记录](../observability/artifacts-and-history.md)。
- 总结本身也要调用模型、消耗 token，单独计入用量。
- 模型返回的总结因输出上限被截断时，OC 不采用这份总结，按总结失败处理。
- `context_tokens` 是单次调用的输入大小，用来看窗口压力；`used_tokens` 是累计的输入加输出，用来统计消耗。
- 要追溯完整的工具内容，回看保存的会话和工具记录。

## 整理的具体规则

处理顺序：先限制单个工具结果的大小 → 接近窗口时清理旧的可压缩工具输出 → 再移除可清理的旧工具交换组 → 最后总结可压缩的区段。每一步都在上一步的结果上继续，腾出足够空间就停。

不会被动的内容：

- 模型的工具调用和对应的工具结果成对保留。
- 近期证据限制了可移除的范围。
- 高优先级的启动上下文受保护。
- 原生签名的 thinking blocks 和加密 reasoning 按原样重放。

阈值怎么算：

- 已知窗口：扣除输出余量和安全空间后得到触发阈值，目标通常是触发值的 75%。小窗口的余量和近期证据保留数按窗口缩放。
- 未知窗口：固定触发值 120,000 token，目标 90,000 token。
- token 估算用 provider 报告的输入输出来校准；新加入的内容和恢复后的会话，先用相应的估算方式。
- 轨迹里的 `session.history_compaction` 记录实际数值和来源，比较长任务时以它为准。

## 延伸阅读

- 源码依据：[docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/runtime-design.md) · [configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/configs/README.md) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/evaluation-runtime.md)
- 相关指南：[深度思考与采样](../models/thinking-reasoning-sampling.md) · [预算、步数与并发](token-step-concurrency-limits.md) · [保存与恢复会话](session-save-resume.md) · [原理：一次运行在内部怎样推进](../architecture/runtime-execution-model.md)
