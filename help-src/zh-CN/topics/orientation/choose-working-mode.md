---
{
  "id": "choose-working-mode",
  "title": "我该用默认 CLI、Base、Team 还是 Workflow",
  "category": "orientation",
  "summary": "按你想怎么推进任务，从终端、单个 Agent、团队和工作流里挑一个起点。",
  "aliases": [
    "单智能体还是多智能体",
    "Team vs Workflow",
    "默认模式",
    "Duo还是Weave",
    "该选哪种方式"
  ],
  "keywords": [
    "模式选择",
    "用哪个模式",
    "CLI",
    "Base",
    "Team",
    "Workflow"
  ],
  "questions": [
    "根据交互需求、可重复流程和协作方式选择最合适的启动入口？",
    "我只想让 AI 帮我改代码，该用哪个？",
    "Team 和 Workflow 有什么区别？",
    "Duo、Weave、Reconstruct 分别适合什么任务？"
  ],
  "related": [
    "interactive-cli-controls",
    "base-single2",
    "configure-team",
    "run-registered-workflow",
    "duo-workflow",
    "weave-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# 我该用默认 CLI、Base、Team 还是 Workflow

OC 有四个起点：默认 CLI（交互式终端，默认启动一个内置团队）、Base（SDK 里的单 Agent）、Team（模型自主派发的多 agent）和 Workflow（用 Python 显式编排 agent 调用）。区别在于"下一步由谁决定"。这篇用一张表帮你按任务挑起点。

## 一句话选择

- 想在终端里交互式地改代码：用默认 CLI。注意它默认启动的是内置 Self-Collaboration 团队，不是单 Agent。
- 想在自己的 Python 程序里跑一个 Agent，拿到一次结果：用 Base（`oc.agent(...)`）。
- 想让几个角色自己决定"下一步交给谁"：用 Team。
- 想用代码规定"先做什么、再做什么、失败了怎么办"：写一个 Workflow。

拿不准时，先从一个 Agent 开始。简单任务一个 Agent 通常就够了，执行过程也更容易看清楚。角色越多，越要操心成员之间怎么交接、怎么传上下文、改动最后怎么交到目标目录。

## 按目标查表

| 你的目标 | 推荐起点 | 接下来还要决定 |
| --- | --- | --- |
| 在终端里持续处理一个项目 | 默认 CLI | 工作目录、模型、命令权限 |
| 从程序里拿到一次编码结果 | `oc.agent(...)` | 工具、额度、产物目录 |
| 让分析、实现、测试几个角色自己交接 | `oc.team(...)` 或团队 CLI | 角色、谁能给谁发消息、采用谁的改动 |
| 强制先实现、再验证 | 自定义 Workflow | 输入输出、怎么验证、失败怎么处理 |
| 让两份方案各写一遍，再挑一份 | Duo | 工具环境、候选的证据、怎么交付 |
| 把应用开发拆成几组，每组都有检查 | Weave | 组之间的依赖、检查器、修复次数上限 |
| 只能看输入输出，猜出一个隐藏程序的逻辑 | Reconstruct | 查询接口、候选执行器、什么时候停 |

Duo、Weave、Reconstruct 是 OC 已经写好的三个 Workflow，可以直接用。

## Team 和 Workflow 的区别

两者都能让多个 Agent 一起干活，区别在于"下一步由谁决定"。

- Team：由成员自己决定下一步找谁。拓扑（topology）规定谁可以给谁派任务、发消息。每个角色配了哪些工具，决定它能做哪些操作。
- Workflow：由你写的 Python 代码决定。比如先让两个候选各自实现，再跑检查，最后按结果采用其中一个。流程结构就是普通的 Python 条件和循环，OC 提供调用 Agent、管理候选、并发和用量统计这些操作。

两者可以组合使用：

- 给某个 Team 成员注册 `run_workflow` 工具，它就能在需要时调用已安装的工作流。
- 成员也可以用 `manage_workflow` 自己编写并启动一个 Python 工作流文件。它和 `run_workflow` 在启动、停止、清理上的规定不一样，具体用法见对应指南。

## 进阶：隔离方式要单独选

Team 和 Workflow 只管"怎么组织任务"。文件和命令实际在哪里运行，由执行环境和隔离策略决定，这是另一项设置。

- 选好组织方式后，再选隔离策略：`shared`（共用同一个目录）、`worktree`（每个成员一个独立的 Git 工作树）或 Docker，并设置命令权限。
- Workflow 的 Python 调度代码本身运行在调用它的那个进程里。

几条实用建议：

1. 面对陌生仓库，先只给只读工具，让 Agent 调查。
2. 需要执行项目代码时，先确定在什么环境里跑，以及网络和挂载怎么设。
3. 一开始就打开运行记录。出问题时能看到实际调了什么、为什么停下、改动去了哪里。

## 下一步读什么

- 用终端：从"第一次运行"和"默认终端交互"开始。
- 接入自己的应用：从"Python SDK"和"运行结果"开始。
- 设计新的协作流程：先读 Workflow 编程模型，再查公开 API、数据流和调试方法。

Skill 只提供任务上的指导。真正的流程要通过程序和运行接口来实现。

隔离策略还包括 `vm-group` 和 `vm-session`，分别让一次运行共用 VM 或每个 Session 领取独立 VM。成员自编 DW 可以通过 `worktree` 在自己的 VM 内创建子工作区，具体见 [成员自编 Workflow](../workflow/member-authored-workflow.md)。

## 源码与进一步阅读

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md) · [docs/single2.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/single2.md) · [docs/duo.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/duo.md) · [docs/weave.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/weave.md)

[在终端里和 Agent 对话，以及命令参数该写在哪](../setup/interactive-cli-controls.md) · [使用 Base 单 Agent 与 Single2 配置](../setup/base-single2.md) · [Team 入门：配置你的多 Agent 团队](../team/configure-team.md) · [运行一个工作流（Workflow）](../workflow/run-registered-workflow.md) · [用 Duo 跑两个候选方案并选出一个](../workflow/duo-workflow.md) · [用 Weave 分组接力完成任务并检查交付](../workflow/weave-workflow.md)
