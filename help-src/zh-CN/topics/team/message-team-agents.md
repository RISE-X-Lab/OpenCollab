---
{
  "id": "message-team-agents",
  "title": "向队友发消息并继续协作",
  "category": "team",
  "summary": "用 message_agent 给已存在的成员异步发消息，补充信息、提修改要求或回复对方。",
  "aliases": [
    "给队友发消息",
    "补充上下文",
    "追加任务",
    "回复队友",
    "to_role",
    "to_aid",
    "异步消息",
    "通信拓扑",
    "入队回执"
  ],
  "keywords": [
    "message_agent",
    "team_status",
    "Agent 消息",
    "队友沟通"
  ],
  "questions": [
    "找到已有成员并发送补充信息、修改要求或协作回复？",
    "message_agent 发出后为什么没有立刻拿到回复？",
    "队友怎样回信给我？",
    "消息发不出去报拓扑错误怎么办？"
  ],
  "related": [
    "configure-team",
    "dispatch-team-agents",
    "wait-for-team-results"
  ],
  "verified_ref": "0c47f74a"
}
---

# 向队友发消息并继续协作

`message_agent` 是 Team 成员之间的异步消息工具。它把一条消息排进目标成员的队列，立即返回入队回执，不会等对方回复。对方在之后的回合把消息当作一条 user 消息读入，如果它也有 `message_agent` 且拓扑允许，就能回信，回信会重新唤起已经结束回合的发送方。最小用法：`{"to_aid": 2, "summary": "...", "content": "..."}`。

和 `spawn_agent` 的分工：要新建成员并拿到一次结果，用派发，见 [派发 Agent 任务并安排审阅](dispatch-team-agents.md)。要和已存在的成员持续交接（包括 `prebuild_team=True` 的固定名册），用消息。

## 最小用法

1. 用 `team_status` 查名册。参数是空对象：

   ```json
   {}
   ```

   结果列出每个成员的 `aid`、角色、父成员和状态。

2. 发消息：

   ```json
   {
     "to_aid": 2,
     "summary": "补充边界输入",
     "content": "请把空列表和只有空格的输入加入回归测试，并回信报告实际执行命令与结果。"
   }
   ```

3. 如果要等回复，结束当前回合。回信到达时，你会被重新唤起。

## 参数

| 参数 | 说明 |
| --- | --- |
| `to_aid` | 目标成员编号，整数 |
| `to_role` | 目标角色名，按规范化形式匹配 |
| `summary` | 必填，一行摘要，方便快速辨认 |
| `content` | 必填，正文，写清对方完成动作需要的全部信息 |

`to_aid` 和 `to_role` 必须恰好提供一个。名册里同一角色只有一个成员时，可以按角色寻址：

```json
{"to_role": "coder", "summary": "核对修复", "content": "请检查刚才提出的边界输入，并回信说明结果。"}
```

同一角色有多个成员时，按角色寻址会列出相关的 aid，需要再挑一个具体成员。

按需团队里，配置已声明但还没被派发的角色可能出现在界面的组织信息里，但这时还没有可以收消息的活动成员。

## 两道检查：工具和拓扑

消息能否发出，要同时满足：

1. 发送方的 `tools` 里有 `message_agent`。
2. 团队拓扑里有从发送方角色到接收方角色的边。

拓扑是有向的。只写 `manager: [coder]` 时，manager 能给 coder 发消息。coder 要回信，还需要 `coder: [manager]`，并且 coder 也要有 `message_agent`。

违反拓扑会返回错误。目标成员不存在会返回寻址错误。

## 发出之后

- 入队回执只表示消息已交给调度器。队友的工作结果会通过后续消息到达。
- 发送方可以继续做不依赖回复的工作。
- 接收方在后续的安全交接点读入消息，作为 user 消息进入它的对话。
- 派发结果、消息回执和队友回信都能用 aid 对应，避免把不同成员的说明混在一起。

开启 `serialize_turns=True` 时，同一时间只运行一个成员的回合。发送方不结束回合，接收方就无法运行。工具回执提示用一条无工具调用的最终回复交出进展。角色具备 `submit` 时，也可以调用它结束回合。等回复时交出回合，接收方才能开始执行。

## 常见问题

如果一直没有回信：

1. 检查目标 aid 是否正确，拓扑是否允许对方回信。
2. 看名册里对方的状态是 `queued`、`busy` 还是已经终止。
3. 检查 `content` 是否明确要求了回信内容。必要时在下一次交互里补充材料。

状态含义见 [等待队友结果并读懂团队状态](wait-for-team-results.md)。

## 源码与进一步阅读

完整字段与回执说明见[消息工具](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/message.py)，消息唤醒行为有[运行测试](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/tests/workflows/test_message_wait_semantics.py)覆盖。
