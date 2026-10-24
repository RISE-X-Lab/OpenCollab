---
{
  "id": "wait-for-team-results",
  "title": "等待队友结果并读懂团队状态",
  "category": "team",
  "summary": "派发的结果会自动填回工具调用，消息的回复要靠结束回合等待；再用 team_status 看各成员在干什么。",
  "aliases": [
    "怎么等子 agent",
    "队友没回复",
    "busy",
    "queued",
    "awaiting",
    "idle",
    "完成后唤醒",
    "spawn_agent",
    "message_agent",
    "stopped",
    "error"
  ],
  "keywords": [
    "等待结果",
    "team_status",
    "submit",
    "协作状态"
  ],
  "questions": [
    "区分派发结果、消息回复和团队结束条件，采用适合当前交接方式的等待操作？",
    "派发子 Agent 后需要手动等待吗？",
    "发了消息之后怎么等回复？",
    "team_status 里的 busy、queued、awaiting 是什么意思？",
    "团队什么时候算运行结束？"
  ],
  "related": [
    "dispatch-team-agents",
    "message-team-agents",
    "team-run-workflow",
    "member-authored-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 等待队友结果并读懂团队状态

OC 的 Team 里有两种等待方式，对应两种交接工具：

| 交接方式 | 怎么等 | 结果从哪里来 |
| --- | --- | --- |
| `spawn_agent` | 不用做任何事，运行时自动挂起父成员 | 子成员完成后，结果填回原来那次工具调用 |
| `message_agent` | 结束自己的当前回合 | 对方回信作为新消息到达，重新唤起发送方 |

OC 没有专门的 "wait" 工具。查看进度用 `team_status`，它返回每个成员的状态。

## 派发：运行时替你等

父成员调用 `spawn_agent` 后：

1. 运行时记下这次工具调用和子成员 aid 的对应关系，父成员进入等待事件状态。
2. 同一批可以派发多个子任务。
3. 整批结果都填齐后，父成员从保存点继续调用模型。

所以一次派发本身已经包含了等待。

完成结果里可能同时有回答、工作树变化和错误原因。先确认对应的 aid 和最终状态，再决定采纳还是修订。子成员的文件改动按 [采用队友改动并验证交付](adopt-team-changes.md) 处理。文字里的"已完成"要结合实际产物确认。

## 消息：结束回合来等

发出消息后，发送方可以先做与回复无关的工作。没别的事可做时，结束当前回合，把执行机会交出去。对方的回复会进入发送方的对话，重新开启它的回合。

开启串行回合（`serialize_turns=True`）时，这一步必须做：当前成员不退出回合，队友就无法开始。

例子：入口给已有的 coder 发消息

```json
{"to_aid": 1, "summary": "补做测试", "content": "请执行目标测试，并回信给出退出码和失败摘要。"}
```

然后调用 `submit` 提交自己的当前进展。coder 完成并回信后，入口会再次获得执行机会，读结果并安排后续动作。

## 读懂 team_status

参数是空对象 `{}`。

| 状态 | 含义 | 接下来看什么 |
| --- | --- | --- |
| `busy` | 该成员的执行任务还在运行 | 当前工具、模型请求或执行记录 |
| `queued` | 在排队等串行回合 | 占用回合的成员是否已经结束 |
| `awaiting` | 在等延迟事件的结果 | 它派发的子成员及对应结果 |
| `idle` | 已完成当前回合 | 是否会收到新消息并开始下一轮 |
| `stopped` 或 `error` | 已停止或出错 | 停止原因、可用改动与失败记录 |

## 团队什么时候结束

所有活动任务和待投递消息都处理完后，团队进入静止状态，运行结束。

- 入口先写了一段交接说明，随后又被队友回信唤起，这是正常协作，不是重复运行。
- 消息队列里还有内容时，团队会继续处理这些消息。

## 常见问题：等得比预期久

按顺序排查：

1. 等的是哪个 aid？它是否存在，角色对不对？
2. 通信方向是否允许？有没有拿到入队回执？对方有没有回信能力（`message_agent` 和反向拓扑）？
3. 对方现在是在执行、排队，还是在等它自己的子任务？
4. 结合终止原因和执行轨迹，判断是否需要修订任务、补充输入，或恢复保留的工作区。

## Team 成员调用 Workflow 时

Workflow 内部的会话由它自己的运行时负责等待和清理。子 Workflow 结束后，Team 的工具调用才返回结果。使用方式见 [让 Team 成员调用已注册的 Workflow](../workflow/team-run-workflow.md) 和 [让成员自己写并运行子 Workflow](../workflow/member-authored-workflow.md)。

## 源码与进一步阅读

等待语义见[派发工具](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/spawn.py)、[消息工具](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/message.py)及[回复唤醒测试](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/tests/workflows/test_message_wait_semantics.py)。
