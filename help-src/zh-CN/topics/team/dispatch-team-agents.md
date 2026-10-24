---
{
  "id": "dispatch-team-agents",
  "title": "派发 Agent 任务并安排审阅",
  "category": "team",
  "summary": "把一项具体任务交给合适的角色去做，还可以安排“写完自动审阅”的循环。",
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
    "max_iterations"
  ],
  "keywords": [
    "spawn_agent",
    "spawn_with_review",
    "派发任务",
    "子 Agent"
  ],
  "questions": [
    "怎样派一个子 Agent 去改代码？",
    "spawn_agent 与发消息有什么区别？",
    "子 Agent 的改动怎样回到父任务？",
    "怎样让代码写完后自动找人审阅？"
  ],
  "related": [
    "configure-team",
    "wait-for-team-results",
    "adopt-team-changes",
    "message-team-agents",
    "team-run-workflow",
    "member-authored-workflow",
    "isolation-overview"
  ],
  "verified_ref": "0c47f74a"
}
---

# 派发 Agent 任务并安排审阅

`spawn_agent` 是 Team 成员创建子成员的工具，用法类似 Claude Code 的 Task 工具：父成员给出 `role`、`task` 和可选的 `context`，子成员在全新的对话里执行，完成结果直接作为这次工具调用的返回值交回父成员。OC 另外提供 `spawn_with_review`，把"Coder 实现 → Reviewer 审阅 → 不通过就重做"打包成一次调用。

派发和 `message_agent` 的区别：派发会新建成员，父成员挂起直到拿到结果；发消息只是给已有成员排入一条消息，立即返回，见 [向队友发消息并继续协作](message-team-agents.md)。

## 怎么派发一个任务

工具参数是 JSON。`role` 和 `task` 必填，`context` 可以省略。下面是一个允许派发 coder 的入口成员可以发出的调用：

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
- 子成员不继承父成员的对话历史。只有 `task` 和 `context` 会成为它的起始任务。
- 子成员能用哪些文件和命令，取决于它的角色工具和执行环境。

能派给谁，由角色的工具白名单和团队拓扑（有向图）共同决定。默认团队允许 Analyst 派发 Coder 和 Tester。新增角色时，要同时声明职责、工具和拓扑里的连线，见 [Team 入门：配置你的多 Agent 团队](configure-team.md)。

## 派发后会发生什么

1. 父成员暂停，等子任务的结果。工具内部返回一个延迟结果（`DeferredCall`），子成员完成后才填回。
2. 同一批发出的多个派发可以并行执行。
3. 整批结果都到齐后，父成员继续处理。

如果想并行处理两件事，就分别派发两个任务，并写清各自负责哪些文件、最后怎样汇总。实际能并发多少，仍受预算、调度和模型请求能力限制。

## 安排"实现 + 审阅"循环

如果希望代码写完后自动有人审阅，给入口配置 `spawn_with_review` 工具，并让拓扑允许它到达 `coder` 和 `reviewer`。内置默认团队没有这些角色和工具，需要你自己扩展。

```json
{
  "task": "修复 parser 的空输入问题，并让审阅者核对回归测试和兼容性。",
  "context": "以项目现有测试约定为准，报告实际执行的测试。",
  "max_iterations": 2
}
```

执行过程：

1. 先创建 Coder 去实现。
2. Coder 成功完成后，创建 Reviewer 审阅。
3. 根据审阅返回的 verdict（结论）决定是否再来一轮。没通过时，新一轮 Coder 会带着之前的实现和审阅反馈，在新的工作区继续改。

`max_iterations` 是最多几轮，接受 1 到 3 的整数，默认 3。如果 Coder 异常停止或审阅者创建失败，结果会保留具体在哪一步失败，以及已有的产物。

审阅通过不等于改动已经进入你的项目。入口声明了采纳工具时，要采用成功的直接 Coder 的结果，再派一个新的 Tester 检查交付目录，见 [采用队友改动并验证交付](adopt-team-changes.md)。完成结果里的 `aid`（成员编号）可以把成员、它的回答和文件变化对应起来。

## 常见问题

如果同一任务正在处理中又派发了一次：会返回原来那个成员的 `aid` 和说明，原任务的结果仍然交回原来的工具调用。先等原任务结果出来，再决定是否需要修订。需要补充信息，就给这个 aid 发消息。

如果用了固定名册 `prebuild_team=True`：成员在运行开始时就建好了，再创建新成员会被拒绝。这时先用 `team_status` 找到现有成员，再用 `message_agent` 交接工作。等待方式见 [等待队友结果并读懂团队状态](wait-for-team-results.md)。

## 源码与进一步阅读

参数与延迟行为见[派发工具](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/tools/spawn.py)，角色组织方法见 [Team 入门：配置你的多 Agent 团队](configure-team.md)。
