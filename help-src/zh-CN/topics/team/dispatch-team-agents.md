---
{
  "id": "dispatch-team-agents",
  "title": "Team 协作：派发、消息、等待与采纳",
  "category": "team",
  "summary": "Team 成员协作的完整循环：用 spawn_agent 派活、用 message_agent 发消息、等结果并读懂 team_status，最后用 adopt_agent 采纳改动并派新 Tester 验证。",
  "aliases": [
    "分配任务",
    "叫队友干活",
    "委派",
    "delegate",
    "specialist",
    "coder",
    "reviewer",
    "重复派发",
    "DeferredCall",
    "max_iterations",
    "给队友发消息",
    "补充上下文",
    "追加任务",
    "回复队友",
    "to_role",
    "to_aid",
    "异步消息",
    "通信拓扑",
    "入队回执",
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
    "error",
    "合入子 agent 改动",
    "应用补丁",
    "队友代码在哪",
    "worktree",
    "增量采用",
    "冲突恢复",
    "Tester",
    "already adopted"
  ],
  "keywords": [
    "spawn_agent",
    "spawn_with_review",
    "派发任务",
    "子 Agent",
    "message_agent",
    "team_status",
    "Agent 消息",
    "队友沟通",
    "等待结果",
    "submit",
    "协作状态",
    "adopt_agent",
    "采用改动",
    "团队交付",
    "Coder"
  ],
  "questions": [
    "怎样派一个子 Agent 去改代码？",
    "spawn_agent 与发消息有什么区别？",
    "子 Agent 的改动怎样回到父任务？",
    "怎样让代码写完后自动找人审阅？",
    "找到已有成员并发送补充信息、修改要求或协作回复？",
    "message_agent 发出后为什么没有立刻拿到回复？",
    "队友怎样回信给我？",
    "消息发不出去报拓扑错误怎么办？",
    "区分派发结果、消息回复和团队结束条件，采用适合当前交接方式的等待操作？",
    "派发子 Agent 后需要手动等待吗？",
    "发了消息之后怎么等回复？",
    "team_status 里的 busy、queued、awaiting 是什么意思？",
    "团队什么时候算运行结束？",
    "将已完成 Coder 的改动交付到团队工作区，再让新的 Tester 验证采用后的结果？",
    "Coder 说改好了，为什么我的目录里看不到改动？",
    "adopt_agent 冲突了怎么办？",
    "同一个 Coder 改了第二版，怎样再合一次？"
  ],
  "related": [
    "configure-team",
    "team-run-workflow",
    "candidate-delivery",
    "collaboration-adherence"
  ],
  "verified_ref": "v0.9.6"
}
---

# Team 协作：派发、消息、等待与采纳

Team 成员之间靠几个协作工具配合。它们组成一个完整的循环：**派活 → 沟通 → 等结果 → 收改动**。这篇按这个顺序讲，读完你就能看懂一次团队运行里每个工具调用在干什么。

## 先看全貌

| 工具 | 做什么 | 调用之后 |
| --- | --- | --- |
| `spawn_agent` | 新建一个成员去做一件事 | 父成员挂起；子成员完成后，结果直接填回这次工具调用 |
| `spawn_with_review` | 新建 Coder 实现，再自动派 Reviewer 审阅，不通过就重做 | 同上，整个循环结束才返回 |
| `message_agent` | 给已经存在的成员发一条消息 | 立即返回“已入队”回执，不等对方回复 |
| `team_status` | 查看名册和每个成员的状态 | 立即返回 |
| `adopt_agent` | 把已完成 Coder 的改动合入团队的交付目录 | 返回采纳结果 |

所有协作工具都要过两道独立的检查：调用者的 `tools` 里有这个工具，并且团队拓扑里有从调用者到目标角色的边。缺一个都会被拒绝。见 [拓扑：谁能联系谁](../team/configure-team.md#拓扑谁能联系谁)。

另一条贯穿全篇的规则：**成员之间不共享对话历史**。子成员只能看到你在任务里写给它的内容，你也只能看到它返回的结果和发来的消息。

## 第一步：派发任务

`spawn_agent` 的参数是 JSON，`role` 和 `task` 必填，`context` 可选。下面是一个允许派发 coder 的入口成员可以发出的调用：

```json
{
  "role": "coder",
  "task": "修复 parser 对空输入的处理，补充对应回归测试，并报告修改文件和执行结果。",
  "context": "入口位于 src/parser.py，项目测试命令为 python -m pytest -q。保留现有公开函数签名。"
}
```

写好任务的要点：

- `task` 写清要做什么、交付到哪里、怎样算验证通过。
- `context` 写已经确认的线索、必要的代码片段和约束。
- 子成员不继承父成员的对话。只有 `task` 和 `context` 会成为它的起始任务。
- 子成员能动哪些文件和命令，取决于它的角色工具和执行环境。

### 派发之后会发生什么

1. 父成员暂停。工具内部返回一个延迟结果（`DeferredCall`），子成员完成后才填回。
2. 同一批发出的多个派发可以并行执行。
3. 整批结果都到齐后，父成员从保存点继续调用模型。

所以派发本身就包含了等待。想并行做两件事，就分别派两个任务，并写清各自负责哪些文件、最后怎样汇总。实际能并行多少，还受预算、调度和模型请求能力限制。

完成结果里可能同时有回答、工作树变化和错误原因。先确认对应的 aid 和最终状态，再决定采纳还是修订；文字里说“已完成”，要结合实际产物确认。

两种常见情况：

- **同一个任务正在处理，又派了一次**：会返回原来那个成员的 aid 和说明，原任务的结果仍交回原来的工具调用。先等它出结果；需要补充信息，就给这个 aid 发消息。
- **用了固定名册（`prebuild_team=True`）**：成员在开始时就建好了，再派发创建新成员会被拒绝。先用 `team_status` 找到现有成员，再用 `message_agent` 交接。

### 让写完的代码自动有人审阅

给入口配上 `spawn_with_review` 工具，并让拓扑能到达 `coder` 和 `reviewer`。内置默认团队没有这些角色和工具，需要你自己加。

```json
{
  "task": "修复 parser 的空输入问题，并让审阅者核对回归测试和兼容性。",
  "context": "以项目现有测试约定为准，报告实际执行的测试。",
  "max_iterations": 2
}
```

执行过程：先创建 Coder 实现；Coder 成功后创建 Reviewer 审阅；根据审阅的结论（verdict）决定是否再来一轮。没通过时，新一轮 Coder 带着之前的实现和审阅意见，在新的工作区继续改。

`max_iterations` 是最多几轮，取 1 到 3 的整数，默认 3。Coder 异常停止或审阅者创建失败时，结果会保留失败发生在哪一步，以及已有的产物。

审阅通过不等于改动已经进入你的项目，还要做第四步的采纳。

## 第二步：和已有成员沟通

`message_agent` 把一条消息排进目标成员的队列，**立即返回**，不等回复。对方在之后的回合里把它当作一条用户消息读入。

最小用法：

1. 用 `team_status` 查名册，参数是空对象 `{}`。结果列出每个成员的 aid、角色、父成员和状态。
2. 发消息：

   ```json
   {
     "to_aid": 2,
     "summary": "补充边界输入",
     "content": "请把空列表和只有空格的输入加入回归测试，并回信报告实际执行命令与结果。"
   }
   ```

3. 要等回复，就结束自己当前的回合。回信到达时，你会被重新唤起。

| 参数 | 说明 |
| --- | --- |
| `to_aid` | 目标成员编号，整数 |
| `to_role` | 目标角色名，按规范化形式匹配 |
| `summary` | 必填，一行摘要，方便快速辨认 |
| `content` | 必填，正文，写清对方完成动作需要的全部信息 |

`to_aid` 和 `to_role` 必须恰好给一个。名册里某个角色只有一个成员时可以按角色寻址，例如 `{"to_role": "coder", "summary": "核对修复", "content": "请检查刚才提出的边界输入，并回信说明结果。"}`；有多个成员时，会列出相关的 aid 让你挑一个。按需团队里，配置声明了但还没被派发的角色，这时还没有能收消息的成员。

拓扑是有方向的。只写 `manager: [coder]` 时，manager 能给 coder 发消息；coder 要回信，还需要 `coder: [manager]`，并且 coder 自己也要有 `message_agent`。违反拓扑返回错误，目标成员不存在返回寻址错误。

发出之后：

- 回执只表示消息已交给调度器。队友的工作结果会通过之后的消息到达。
- 发送方可以继续做不依赖回复的工作。
- 派发结果、消息回执和队友回信都带 aid，用它把不同成员的说明对上，避免混在一起。

**什么时候用派发，什么时候用消息？** 要新建一个成员、拿到一次结果，用派发；要和已经存在的成员持续交接（包括固定名册），用消息。

## 第三步：等待结果

OC 没有专门的 “wait” 工具。两种交接方式，等待方式也不同：

| 交接方式 | 怎么等 | 结果从哪里来 |
| --- | --- | --- |
| `spawn_agent` | 什么都不用做，运行时自动挂起父成员 | 子成员完成后，结果填回原来那次工具调用 |
| `message_agent` | 结束自己的当前回合 | 对方回信作为新消息到达，重新唤起发送方 |

开启串行回合（`serialize_turns=True`）时，同一时间只运行一个成员。发送方不结束回合，接收方就没法开始。所以 `message_agent` 的回执会提醒你：先用一条不带工具调用的最终回复交出当前进展；角色有 `submit` 工具的话，也可以调用它结束回合。

### 读懂 `team_status`

| 状态 | 含义 | 接下来看什么 |
| --- | --- | --- |
| `busy` | 正在执行 | 当前工具、模型请求或执行记录 |
| `queued` | 在排队等串行回合 | 占着回合的成员是否已经结束 |
| `awaiting` | 在等延迟结果 | 它派发的子成员及对应结果 |
| `idle` | 已完成当前回合 | 会不会收到新消息并开始下一轮 |
| `stopped` 或 `error` | 已停止或出错 | 停止原因、可用改动和失败记录 |

所有活动任务和待投递的消息都处理完后，团队进入静止状态，运行才结束。入口先写了一段交接说明、随后又被队友回信唤起，这是正常协作，不是重复运行。

### 等得比预期久，按顺序查

1. 等的是哪个 aid？它存在吗，角色对不对？
2. 通信方向允许吗？拿到入队回执了吗？对方有没有回信能力（`message_agent` 加反向拓扑）？`content` 里明确要求回信了吗？
3. 对方现在是在执行、排队，还是在等它自己的子任务？
4. 结合停止原因和执行轨迹，判断要不要修订任务、补充输入，或恢复保留的工作区。

## 第四步：采纳改动，再验证

Coder 通常在独立工作区（工作树或容器）里改代码，改动**不会自动写回**团队的交付目录。`adopt_agent` 就是入口成员显式“合入”的那一步。推荐流程是先采纳，再派一个**新的** Tester，让测试跑在真正交付的文件上，而不是 Coder 自己的副本上。

1. 确认入口角色的 `tools` 里有 `adopt_agent`。
2. 等 Coder 成功完成。完成结果里会带可采纳的 aid，例如 `Completed coder aid=3`。
3. 入口调用 `adopt_agent`，只需要一个参数：`{"aid": 3}`。
4. 采纳成功后，派一个新的 Tester 检查交付目录：

   ```json
   {"role": "tester", "task": "验证刚采纳的 parser 修复，执行相关回归测试并报告命令、退出码和失败信息。", "context": "检查当前交付目录中的空输入处理。"}
   ```

   新 Tester 从已经更新的交付内容准备环境，所以测试结果对应的正是这次实际交付的内容。采纳之前派出的旧 Tester，它的结果对应的是采纳前的文件。

5. 最终报告写清三件事：改了哪些文件、采纳是否成功、执行了哪些项目验证（命令、退出码、测试目标是否真的执行）。

### 什么情况下能采纳

`aid` 必须是正整数，并且同时满足：调用者是拥有 `adopt_agent` 的入口成员；对象是它的直接 coder 子成员（角色名按规范化形式比对）；对象在当前这一轮入口任务里已经成功完成。对象失败了、被停止了、是别的角色，或者是之前某一轮的结果，都要先处理它的状态。

### 改动是怎样合进去的

声明了 `adopt_agent` 的 Team，按下面的过程处理：

1. 派发子任务时，用当前的源文件准备子任务环境，包括你还没提交的修改和之前已经采纳的实现。
2. Coder 在自己的环境里修改，完成后运行时记下它的全部变化。
3. 采纳时，把这份改动的增量合入交付目录，源仓库的 Git 暂存区保持不变，你自己独立做的修改也会保留。

| 环境 | 什么时候能在交付目录看到改动 |
| --- | --- |
| 共享环境 | Coder 已经直接改了交付目录，工具会告诉你这些变化已经存在 |
| 独立环境 | `adopt_agent` 成功返回之后 |
| 入口自己也被显式隔离 | 还要取出入口补丁，见 [隔离改动怎样回到项目](../isolation/candidate-delivery.md#入口成员自己被隔离时) |

同一轮里，同一个 Coder 又完成了新一版修订时，再次采纳只应用相对上次成功采纳的新增部分；重复采纳完全相同的结果会返回 already adopted，交付文件不变。

### 采纳失败怎么办

同一个文件发生冲突时，采纳会被拒绝，交付文件保持原样，错误信息会指出完整的候选材料保存在哪里。处理方法：先导出补丁，比较源目录和候选之间的实际差异再处理冲突；或者派一个新任务，让它从已更新的交付目录继续改。

采纳失败时，完整补丁保存在本次运行的产物里；没启用产物目录时，存进一个私有的临时恢复目录。要在释放运行资源之前把这些材料存好，步骤见 [找回改动并清理资源](../isolation/candidate-delivery.md#出了问题先找回改动再清理)。

## Team 成员调用 Workflow 时

成员也可以把一段工作交给 Workflow：`run_workflow` 运行已注册的 Workflow，等它结束才返回；`manage_workflow` 启动成员自己写的子 Workflow，立刻返回一个 id 供之后查询。Workflow 内部的会话由它自己的运行时负责等待和清理。见 [让 Team 成员运行 Workflow](../workflow/team-run-workflow.md)。

## 延伸阅读

- 源码依据：[派发工具](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/spawn.py) · [消息工具](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/message.py) · [采纳工具](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/tools/adopt_agent.py) · [调度器的采纳逻辑](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/application/_scheduler_team.py) · [消息唤醒测试](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/tests/workflows/test_message_wait_semantics.py)
- 相关指南：[Team 入门](configure-team.md) · [让 Team 成员运行 Workflow](../workflow/team-run-workflow.md) · [隔离改动怎样回到项目](../isolation/candidate-delivery.md) · [检查团队是否真的按分工协作](../observability/collaboration-adherence.md)
