---
{
  "id": "workflow-capabilities-boundaries",
  "title": "工作流能做什么、不能做什么",
  "category": "workflow",
  "summary": "判断你的需求用普通 Python 就能写、要用 OC 的 ctx 功能，还是要自己搭配外部服务。",
  "aliases": [
    "任意 Python 都能用吗",
    "能不能写 DAG",
    "支持分布式多机吗",
    "有没有自动断点恢复",
    "Docker 隔离到哪里",
    "能在运行时生成代码吗",
    "工作流能力范围",
    "工作流限制"
  ],
  "keywords": [
    "Workflow 能力范围",
    "执行边界",
    "Python 编排",
    "环境扩展",
    "动态 DW",
    "多个 Agent",
    "request_capacity",
    "VM"
  ],
  "questions": [
    "工作流里能用任意 Python 库吗？",
    "能写 DAG 依赖图吗？",
    "支持多台机器分布式运行吗？",
    "运行中断后能自动接着跑吗？",
    "Docker 隔离到底隔离了哪些东西？",
    "Team 成员能在运行时自己写工作流再启动吗？",
    "成员能在现场编写 DW 并调度多个 Agent 吗？",
    "Python 编排、工具和模型请求分别在哪里执行？",
    "工作流局部并发与模型共享容量有什么关系？"
  ],
  "related": [
    "workflow-programming-model",
    "workflow-composition-hierarchy",
    "workflow-failure-recovery-lifecycle",
    "member-authored-workflow",
    "workflow-parallel-pipeline",
    "vm-environment-extension"
  ],
  "verified_ref": "0c47f74a"
}
---

# 工作流能做什么、不能做什么

本篇是一张需求对照表：你的流程哪部分用普通 Python 就能写，哪部分要用 OC 的 `ctx` 功能，哪部分 OC 不提供、需要调用方自己搭。概括地说，OC 负责单次运行内的会话、并发、额度、候选环境和隔离；不负责跨进程自动续跑和多主机调度。例如“三个 Agent 并行调研，再按结论决定是否追加”用 Python 加 `ctx.parallel` 即可；“分发到十台机器”需要你自己的任务队列。

## 先问三个问题

1. 步骤的先后顺序怎么写？通常用普通 Python：分支、循环、函数组合、动态依赖都可以。
2. 模型和工具怎么执行？交给 OC 的 `ctx`：它管理会话、并发容量、额度和候选环境。
3. 运行之外的业务状态谁来管？由你的应用负责：自己的数据库、外部系统、跨主机协调。

确定每部分由谁负责，就能选对接口。

## 能力速查表

| 你想做的事 | 用什么 | 怎么用、有什么限制 |
| --- | --- | --- |
| 条件分支 | Python 原生 | 根据输入或结构化结果写 `if`、`elif`、`match` |
| 循环与反馈 | Python 原生 | `for`、`while`，写清轮数、资源和结束条件 |
| 普通模块和业务逻辑 | Python 原生 | 导入当前环境已装的库，受解释器权限限制 |
| 动态生成任务 | Python 原生 | 根据输入和反馈临时构造异步任务函数 |
| 独立任务并行 | OC 内置 | `ctx.parallel`，共用集合容量和会话容量 |
| 每一项走多个阶段 | OC 内置 | `ctx.pipeline`，每项按阶段顺序各自推进 |
| 依赖图和 DAG | Python 或 Weave 分组 | 自己写依赖执行逻辑，或用 Weave 的分组规划 |
| Agent 返回结构化结果 | OC 内置 | `ctx.agent` 的 schema 捕获和校验 |
| 子函数和子流程 | Python 与候选接口 | 共用 `ctx`，或用 `candidate_workflow` 建立候选环境 |
| Team 调用已注册工作流 | OC 内置 | `run_workflow describe` 读取输入，`run` 等完整结果返回，角色显式获权 |
| 成员运行自己生成的源文件 | OC 内置 | `manage_workflow describe` 读取能力，`launch` 创建归成员管理的子进程 |
| 工作流里组合独立 Team | 宿主 SDK | 显式调用 `OpenCollab.team`，自己连接资源和取消 |
| 内置和自定义工具 | OC 扩展 | `builtin_tools`、公开的 `Tool` 和 `VerificationTool` |
| 工作区、容器与 VM 策略 | OC 环境策略 | `shared`、`worktree`、Docker、已配置 VM 或自定义 provider |
| 共享模型服务容量 | 宿主模型配置 | `request_pool` 与 `request_capacity` 约束原生请求 |
| 业务失败重试 | Python 与传输层各管一部分 | 步骤按副作用自己决定是否重试；模型客户端处理已分类的传输重试 |
| 调试与交付验证 | OC 观察工具加应用检查 | `phase`、`log`、`label`、trace、实际工具和验证器 |
| 中断后跨进程自动接着跑 | 调用方自己设计 | 保存已完成步骤和业务状态，重建依赖后继续 |
| 多台主机分布式调度 | 调用方服务 | 任务队列、远端资源、跨主机状态和结果协调 |

## 怎样把想法写成程序

只要当前解释器、依赖和权限做得到，工作流函数就可以：

- 导入模块、调用业务函数、处理数据；
- 根据输入或中间结果，临时创建后续步骤。

举个探索流程的例子：

1. 先让 Agent 阅读资料。
2. 判断还缺哪些信息。
3. 动态生成一份任务列表。
4. 汇总结果。

分支用 `if`，有限次数的迭代用 `for`，反馈循环用 `while`。

设计时先写清三件事：输入是什么、要做哪些操作、什么时候停。工作流代码读取模型结果，决定下一步，并通过文件、工具执行记录和实际检查确认结果。

耗时较长的阻塞操作，应交给异步库、线程，或应用自己开的独立进程。这样不会卡住并发和取消。

## 进阶：依赖图与嵌套深度

### 自己维护依赖图

工作流可以自己维护三样东西：节点表、已完成的结果、依赖集合。每轮挑出依赖都已满足的节点交给 `parallel`，再把结果送进下一批。节点代表什么、失败了怎样影响其它节点、什么时候停，都由作者定义。

Weave 已经提供了分组依赖规划。依赖成环的几个组会合并到同一个会话里。如果你的任务能套用它的分组方式，可以直接用。

### 子函数与候选子流程

- 普通异步子函数：按业务拆分，共用 `ctx`。
- 候选子流程：建立独立的候选环境，但仍共用父级的会话容量、集合容量和最终截止时间。

当前默认的候选子上下文没有再注入下一层候选后端。所以在子流程里再建候选，需要集成方额外提供这项能力。简单说，拆逻辑用子函数；要分配额外环境并交付结果，靠候选后端。

## 进阶：代码和工具分别在哪里执行

| 执行方式 | Python 编排代码在哪 | Agent 的工具在哪 |
| --- | --- | --- |
| 宿主 SDK + `shared` | 调用方进程 | 直接用当前环境和目录 |
| 宿主 SDK + `worktree` | 调用方进程 | 父环境中的一个独立 checkout |
| 宿主 SDK + `docker-group` | 调用方进程 | 本次策略运行的容器，每个会话有私有工作区 |
| 宿主 SDK + `docker-session` | 调用方进程 | 每个会话一个独立容器和工作区 |
| 宿主 SDK + `vm-group` | 调用方进程 | 同一次策略运行租用一个 Linux VM，各会话私有工作区 |
| 宿主 SDK + `vm-session` | 调用方进程 | 每个会话分别租用 Linux VM |
| `manage_workflow` 启动源文件 | 成员环境里的 Python 子进程 | 按继承、`launch.isolation` 或单次指定的策略执行 |

术语说明：

- `worktree` 策略基于 git worktree，和原目录共用操作系统和 Git 对象存储。在容器里创建的 worktree 仍在那个容器内，在 VM 内创建则继续位于该 guest。
- Docker 策略需要可用的 Docker daemon 和合适的镜像。默认网络为 `none`，挂载和网络按 provider 配置。

一个容易误会的点：Docker 配置主要决定 Agent 工具的环境。你在宿主 Python 里直接用 `Path` 或 `subprocess`，操作的仍是宿主进程所在的位置。

自定义 provider：它的工厂每次为一次运行和一种策略创建一个 `EnvironmentPool`，负责 acquire（取用）、release（归还）和 adopt（接管）。自定义 provider 同样要遵守工具权限、工作区检查和输出限制。完整环境的候选后端还可以决定运行中的服务和文件怎样一起交付。

## 进阶：运行时生成的源文件与业务服务

### 成员自己写工作流再启动

Team 成员可以用编辑工具写一个 Python 文件，再用 `manage_workflow launch` 在自己的环境里启动。前提条件：

- 成员有 bash 权限；
- 有装了 OpenCollab 的 Python；
- 子会话要用的工具已获授权。

启动后立刻返回一个 id。创建者之后可以查询、等待或停止它。父成员结束时，会清理还在运行的子运行。

### 多个 Agent、VM 与模型请求容量

成员先调用 `manage_workflow describe`，读取当前配置的策略名称、工具权限和入口示例。可以现场生成三项任务并交给 `ctx.parallel`，各任务是否调用 Agent 由 Python 代码决定。`launch.concurrency` 限制这个子 DW 的活跃会话，集合容量默认沿用它。宿主 `request_capacity` 管理共享模型服务的原生请求容量，多个 Team 和 DW 可以共用同一个请求池。

父成员使用 `vm-session` 时，子 DW 以 `isolation="worktree"` 启动，Agent 工具在同一 guest 的独立工作树中执行。另选 `vm-session` 的子会话需要另一台可租用 VM。VM 资源、SSH、guest 软件和 reset 由人类调用方准备，见[VM 环境扩展](../isolation/vm-environment-extension.md)。

桥接层模型凭据、端点和请求池由宿主管理。标准输入传入非敏感生成设置、输入和工具限制。项目文件按快照规则复制到子环境，凭据配置应放在控制端进程环境或工作区之外。完整用法见[成员自编 DW 教程](member-authored-workflow.md)。

### 需要你自己搭的部分

普通 SDK 工作流的并发参数，只管理本次运行内的会话和集合任务。下面这些需要应用组件来连接：

- 多主机任务队列；
- 远端 worker 注册；
- 全局状态；
- 跨系统资源归属。

保存下来的 `workflow.json`、会话和轨迹可以追溯执行过程。但要让通用控制流在中断后接着跑，所需的状态和副作用处理由应用设计。Weave 的显式状态续跑可以作为一个范围明确的参考实现。

## 怎样验证方案可行

1. 用一个小任务运行工作流。
2. 查看每次 Agent 调用和工具执行。
3. 检查生成的文件、服务行为或返回值。

`completed` 只表示工作流函数跑完了，任务是否真的完成，要靠项目测试或应用的验证器。根据检查结果再决定：是改流程代码、加 `ctx` 调用，还是接入应用服务。

## 源码依据

[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py#L408)、[docs/session-isolation.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/session-isolation.md#L5)、[examples/member-authored-workflow/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/member-authored-workflow/README.md#L3)。

## 相关章节

[工作流就是普通 Python：编程模型入门](workflow-programming-model.md)、[Agent、Team 和 Workflow 怎么互相调用](workflow-composition-hierarchy.md)、[Workflow 出错了：失败、重试与恢复](workflow-failure-recovery-lifecycle.md)、[让成员自己写并运行子 Workflow](member-authored-workflow.md)。
