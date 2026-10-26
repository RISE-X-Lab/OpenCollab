---
{
  "id": "token-step-concurrency-limits",
  "title": "预算、步数与并发",
  "category": "operations",
  "summary": "用 budget（累计 token）、max_steps 和 concurrency 分别限制一次运行；团队怎样从共享池分额度，以及怎样用软额度回调按进展放宽。",
  "aliases": [
    "token 上限",
    "最多跑几步",
    "并发数",
    "分角色额度",
    "共享预算",
    "预算不够",
    "RunControl",
    "initial_soft_budget_tokens",
    "BudgetDecision",
    "OPENCOLLAB_UNBOUNDED_LIMITS",
    "serialize_turns",
    "final_prompt",
    "OPENCOLLAB_BUDGET_NUDGE_MODE",
    "OPENCOLLAB_WRITE_NUDGE_MODE",
    "一直读不改",
    "预算提示频率"
  ],
  "keywords": [
    "--budget",
    "OPENCOLLAB_BUDGET",
    "budget",
    "max_steps",
    "steps",
    "concurrency",
    "task_concurrency",
    "--concurrency",
    "--task-concurrency",
    "limit_mode"
  ],
  "questions": [
    "怎么限制一次运行最多用多少 token 和多少步？",
    "团队成员的 token 额度是怎么分的？",
    "工作流的 concurrency 和 task_concurrency 有什么区别？",
    "OPENCOLLAB_UNBOUNDED_LIMITS 对不同入口有什么影响？",
    "怎么关掉每步的预算提示或读多了要求修改的提示？"
  ],
  "related": [
    "timeouts-retries",
    "context-compaction",
    "model-routing-request-capacity",
    "token-cache-cost",
    "workflow-public-api-map"
  ],
  "verified_ref": "0c47f74a"
}
---

# 预算、步数与并发

OC 用三个互相独立的旋钮限制一次运行：

| 旋钮 | 限制什么 |
| --- | --- |
| `budget` | 累计的输入加输出 token |
| `max_steps` | 会话执行的步数 |
| `concurrency` | Workflow 里同时运行的 Agent 会话数 |

有两点和很多框架不同，先记住：一是 token 预算按**整个会话累计**，每次重发历史都计入输入，所以长会话的消耗会越来越快；二是团队成员从**同一个池**里分额度。美元花费另外记录（见 [用量与费用](../observability/token-cache-cost.md)），单次输出上限 `max_output_tokens` 也是单独配置的客户端设置。

建议先按任务规模设一个有限额度，再按实际用量和完成证据调整。

## 最小用法：单 Agent

```python
import asyncio
from opencollab import OpenCollab

async def main() -> None:
    result = await OpenCollab(".").agent2(
        "阅读测试配置，说明应执行哪些验证以及依据。",
        tools="read",
        budget=80_000,  # 累计输入+输出 token
        max_steps=30,   # 只读任务用较小步数
        timeout=300,
    )
    print(result.status, result.reason, result.tokens)
    print(result.metrics.get("steps"))

asyncio.run(main())
```

## 参数一览

| 参数 | 默认值 / 取值 | 说明 |
| --- | --- | --- |
| `budget` / `--budget` / `OPENCOLLAB_BUDGET` | 正整数，客户端默认 1,000,000 | 累计输入加输出 token |
| `max_steps` | 正整数 | 会话执行步数。旧参数 `steps` 和它互斥 |
| `max_output_tokens` | 8,192（客户端配置） | 每次模型调用的输出上限。整次预算越大，能支撑的调用次数越多 |
| `concurrency` / `--concurrency` | SDK `workflow()` 默认 4 | Workflow 同时运行的 Agent 会话数 |
| `task_concurrency` / `--task-concurrency` | 省略时等于 `concurrency` | 活跃的 `parallel` 或 `pipeline` 单元数 |
| `limit_mode` | `workflow()` 默认 `"environment"` | 见 [无限制模式](#无限制模式) |
| `OPENCOLLAB_BUDGET_NUDGE_MODE` | `every-step` | 预算状态提示的频率 |
| `OPENCOLLAB_WRITE_NUDGE_MODE` | `on` | “一直在读、不动手”时的行动提示 |

## 团队怎样分配 token

**按需团队**（入口成员在运行中派生子成员）：

- 所有成员从同一个池里取额度。
- 创建子成员时同步为它保留至多 `min(total, max(10_000, total // 4))`，同时不超过尚未分配的余额。
- 入口成员的当前轮次可以用剩余的池；成员尚未结算的请求继续占着各自的保留量。

**固定名册**（`prebuild_team=True`，席位事先声明）：

- 共享池时，每个已声明的席位还有一个累计上限，目前是总池除以席位数。
- 也可以在团队文件顶层或角色的 `budget` 对象里设 `tokens`。只要有一个角色设了独立额度，所有角色都必须设，团队总额是各角色之和。这种按角色分额度的方式需要 `prebuild_team=True`，额度由文件提供，此时再传 SDK `budget=` 会报错。

两种写法如下（放在已有 `async` 函数里，`client` 是已创建的客户端，二选一）：

```python
# 共享池
result = await client.team(
    "完成指定实现并验证。",
    config="team.yaml",
    budget=300_000,
    max_steps=100,
)
```

```python
# 按角色声明额度，不传 budget
result = await client.team(
    "完成指定实现并验证。",
    config="team-with-role-budgets.yaml",
    prebuild_team=True,
    max_steps=100,
)
```

## Workflow 的两类并发

- `concurrency` 限制 Agent 会话；`task_concurrency` 单独限制活跃的 `parallel` 或 `pipeline` 单元，包括候选子流程。两者独立计数，混合运行时峰值可以达到两者之和。为什么这样设计，见 [两个并发上限](../workflow/workflow-agent-calls.md#两个并发上限)。
- 发往模型服务的请求数，还可以由请求池另外限制，见 [模型路由与共享请求容量](../models/model-routing-request-capacity.md)。

```bash
opencollab workflow run duo --workspace /path/to/application \
  --task '完成指定修改并运行验证。' \
  --args '{"allow_unisolated_shell":true}' \
  --budget 300000 --concurrency 2 --task-concurrency 2
```

（这条 Duo 命令显式允许在本机工作区执行 shell，执行环境请按任务需要选。）

- Duo 按先 A 后 B 的顺序跑候选，提高并发也不会改变这个顺序。
- Workflow 里的调用在执行前从 Workflow 的池里保留预算。其他调用占着份额时，实际授权可能低于请求值；清理完成后返还未用的保留量。
- “预算拒绝”表示拿不到执行额度，“等待并发槽”表示暂时没有空位，看结果状态区分这两种情况。

## 按进展扩展软额度

`RunControl` 可以在硬授权（`budget`）之内先给一个较小的**软额度**，再由回调 `decide_budget` 返回 `BudgetDecision` 决定保持还是增加。这样，任务有进展时可以多给额度，没进展时及早收尾。

- 回调能读到实际消耗、步数、输入保留量和最低输出需求，可以结合应用里的执行证据决定是否继续。
- 新额度必须覆盖已有消耗，且不超过硬授权；减少现有软额度会导致策略错误。硬额度用完后按预算限制停止。

下面的片段（放在已有 `async` 函数里，沿用 `client`）在消耗到阈值时，把软额度扩到硬额度一次。实际的触发条件可以按执行和验证进展来定：

```python
from opencollab import BudgetDecision, RunControl

def decide(snapshot):
    allowance = snapshot.soft_budget_tokens
    if snapshot.used_tokens >= 25_000:
        allowance = snapshot.hard_budget_tokens
    return BudgetDecision(allowance)

result = await client.agent2(
    "阅读项目并解释可执行的验证步骤。",
    tools="read",
    budget=100_000,
    run_control=RunControl(
        initial_soft_budget_tokens=50_000,
        decide_budget=decide,
    ),
)
```

### 在额度内安排最后一次交接

回调还可以用 `BudgetDecision(..., final_prompt=...)` 给当前模型请求追加交接要求。这段提示会重新计入输入预留量，所以要在还有输出空间时发出。下面的回调可以替换上例里的 `decide_budget`：保持软额度，接近额度时要求 Agent 交付已有证据。

```python
def finish_when_low(snapshot):
    allowance = snapshot.soft_budget_tokens
    reserve = snapshot.reserved_input_tokens + snapshot.minimum_output_tokens
    final_prompt = None
    if allowance - snapshot.used_tokens < reserve + 5_000:
        final_prompt = "完成当前验证并交付已有改动、实际测试结果和待处理事项。"
    return BudgetDecision(allowance, final_prompt=final_prompt)
```

- 只用于已设置有限软额度的调用。回调可以同步返回，也可以异步等待应用里的进展判断。
- 运行时在执行检查和估算请求输入时调用回调；单次响应超过软额度后，也可能以 `after_response` 为原因再调用一次。
- 最终提示加入当前请求。任务完成到什么程度，看保存的文件和验证结果。

在 Workflow 里对单次调用使用 `RunControl` 的完整例子，见 [Workflow API 速查](../workflow/workflow-public-api-map.md#调用进行中runcontrol)。

## 无限制模式

`OPENCOLLAB_UNBOUNDED_LIMITS=true` 对不同入口的效果不同：

| 入口 | 效果 |
| --- | --- |
| Base、Single2（`agent()`） | 显式传入的预算和步数仍然生效，省略的限制变为无限 |
| `workflow()` | 默认 `limit_mode="environment"`，即使传了数值也采用无限的 token 和角色步数。要保留明确限制，用 `limit_mode="explicit"` |
| `team()` | 继续使用共享或声明的额度及其 `max_steps` |

时间和清理限制在任何情况下都继续生效。另外，额度检查发生在调用之间，已经在进行中的请求可能超出剩余额度。停止后看 `reason`、部分产物和最终用量，再决定是否继续。

## 注入给模型的提示

下面两类提示是 OC 注入给模型的引导消息，和实际的 token、步数限制分开设置；限制始终按运行参数执行。

`OPENCOLLAB_BUDGET_NUDGE_MODE`（默认每步提示预算状态）：

| 值 | 行为 |
| --- | --- |
| `every-step` | 每步提示；无限预算的运行中也显示累计用量 |
| `thresholds` | 累计消耗跨过有限预算的 20%、40%、60%、80% 时提示；恢复会话后从已有消耗继续比较 |
| `off` | 不提示 |

`OPENCOLLAB_WRITE_NUDGE_MODE`（默认 `on`）：Agent 有写入工具却一直在读时，8 次读取后建议修改，16 次后要求一次工具调用；有结构化提交工具且适合提交时，8 次读取后可以要求结构化提交。设为 `off`，适合观察模型自己选择阅读、修改、提交时机的任务。

两个变量都从进程环境读取，未知值分别回到 `every-step` 和 `on`。例如启动前选择按阈值提示预算：

```bash
OPENCOLLAB_BUDGET_NUDGE_MODE=thresholds \
  opencollab --workspace /path/to/application --budget 200000
```

## 延伸阅读

- 源码依据：[运行提示策略](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/steering.py) · [运行控制](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/application/run_control.py) · [docs/runtime-design.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/runtime-design.md) · [configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/README.md) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py)
- 相关指南：[运行怎样停下](timeouts-retries.md) · [上下文窗口与压缩](context-compaction.md) · [模型路由与共享请求容量](../models/model-routing-request-capacity.md) · [用量与费用](../observability/token-cache-cost.md)
