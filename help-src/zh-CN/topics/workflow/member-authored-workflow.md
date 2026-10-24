---
{
  "id": "member-authored-workflow",
  "title": "让成员自己写并运行子 Workflow",
  "category": "workflow",
  "summary": "成员先发现能力，再现场编写 Python 子 Workflow，管理多个 Agent 的并发、隔离、预算与结果。",
  "aliases": [
    "动态工作单元",
    "动态 DW",
    "自己写自己跑",
    "launch",
    "status",
    "wait",
    "result",
    "stop",
    "member-authored Workflow",
    "让成员编写并管理自己的子 Workflow",
    "子工作流",
    "成员现场编排"
  ],
  "keywords": [
    "manage_workflow",
    "成员编写工作流",
    "子运行",
    "运行时工作流",
    "child id",
    "child-runs",
    "describe",
    "ctx.parallel",
    "request_capacity",
    "vm-session",
    "worktree"
  ],
  "questions": [
    "Agent 能自己写一个工作流再运行吗？",
    "启动后怎样查询、等待和停止子 Workflow？",
    "父 Agent 结束后子 Workflow 会怎样？",
    "子 Workflow 的预算默认是多少？",
    "成员怎样现场编写一个 DW 并并行调用多个 Agent？",
    "使用 manage_workflow 前，人需要配置哪些资源和工具？",
    "VM 成员怎样在自己的 guest 内创建子工作树？"
  ],
  "related": [
    "team-run-workflow",
    "author-custom-workflow",
    "workflow-agent-calls",
    "wait-for-team-results",
    "workflow-parallel-pipeline",
    "vm-environment-extension"
  ],
  "verified_ref": "0c47f74a"
}
---

# 让成员自己写并运行子 Workflow

`manage_workflow` 是 OC 给 Team 成员的另一个工具：成员先用文件工具写出一个 Python Workflow 文件，再用 `launch` 启动它。`launch` 立即返回 child id，不阻塞；成员可以继续干别的，之后用 `status`、`wait`、`result`、`stop` 按 id 管理这个子运行。

它和 `run_workflow` 的区别：`run_workflow` 只能跑已注册的名称，并且同步等待结束；`manage_workflow` 跑成员现写的代码，异步管理。源文件在成员执行环境里的一个独立 Python 进程中导入，入口函数接收普通的 `ctx` 和 `args`。

DW 指由程序安排执行步骤的工作单元。成员可以根据任务现场写出一个 DW，再让其中多个 Agent 分工处理输入。下面先从一个 Agent 的最小例子开始，再扩展为三个任务。

这套用法需要安装源码修订 `0c47f74a` 对应的 OpenCollab。按[从源码安装并核对当前 checkout](../setup/install-source-checkout.md)准备解释器，并以本页源码依据中的完整修订定位代码。包内版本号与已安装源码修订分别核对。

## 最小用法

人类调用方先配置模型服务、额度和执行环境，再授予角色所需工具。模型配置通过控制端进程环境或工作区之外的配置文件提供。项目文件会按快照规则复制到子环境，凭据文件应保存在待复制目录之外。逻辑模型路由、共享请求容量、Docker 镜像与 VM 资源也由调用方准备。

### 1. 给角色配工具

```yaml
entry: author
roles:
  author:
    prompt: 编写前先调用 manage_workflow describe，再按任务需要写出 Workflow，启动并读取结果。
    tools: [file_write, file_read, bash, manage_workflow]
```

环境要求：

- 目标环境里要有装了 OpenCollab 的 Python。默认解释器是 `python3`，可以用工具参数 `python` 指定别的路径。
- 工具用成员的 shell 权限执行源代码。在宿主机上执行时，Team 调用方要显式允许 `allow_unisolated_shell`。
- 容器或 VM 成员在自己的环境里执行源文件。目标解释器需要安装同一 OpenCollab 源码修订，工具原有的进程隔离要求和命令安全检查继续生效。

### 2. 成员写出 Workflow 文件

成员先读取当前接口和授权。

```json
{"action": "describe"}
```

返回值包括默认隔离策略 `default_isolation`、当前配置的 `strategies`、启动条件 `launch_allowed_in_current_environment`、可用工具 `allowed_child_tools`、可通过 `builtin_tools` 构造的 `builtin_child_tools`，以及入口说明和最小源码。成员按这些信息编写代码。策略标记为 `checked_on_allocation`，连接和资源余量在实际分配时检查，读取说明阶段保持现有资源分配。

成员用编辑工具在工作区写入 `generated.py`。下面是一个完整的子工作流文件：先算输入的总和，再开一个 Agent 会话。

```python
from opencollab.tools import builtin_tools

async def main(ctx, args):
    count = int(args.get('count', 6))
    if count < 0:
        raise ValueError('count must be nonnegative')
    total = sum(range(count))
    answer = await ctx.agent(
        f'用一句中文解释数字 {total}',
        label='explain',
        tools=builtin_tools('file_read'),  # 必须是父成员可用的工具
    )
    return {'total': total, 'answer': answer}
```

- 文件按 `entry` 加载，默认入口是 `main`。不需要装饰器，装饰器只用于按名称发现。
- 子会话请求的工具名必须在父成员的可用工具里。
- 输出上限、shell 隔离要求和文件创建限制，都从父成员继承。

### 3. 启动

```json
{
  "action": "launch",
  "source": "generated.py",
  "entry": "main",
  "args": {"count": 6},
  "python": "python3",
  "budget": 10000,
  "concurrency": 2,
  "isolation": "shared"
}
```

- `source`：成员工作区里的 Python 文件。
- `args`：要能跨子进程序列化，用字符串、数字、列表和字典即可。
- 标准输入传入非敏感生成设置、输入与工具限制。桥接层的模型凭据、端点路由、请求池路径和共享请求名额由宿主管理。

`isolation` 选择子 Workflow 的默认会话策略，省略时继承成员策略。单次 `ctx.agent` 或 `ctx.agent_run` 可以覆盖它。`cleanup_timeout` 可以在 `launch` 时设置正数清理宽限，省略时继承父运行配置。

### 4. 等待结果

之后所有操作都用返回的 id，例如：

```json
{"action": "wait", "id": "<child id>", "timeout": 30}
```

## 六种操作

| `action` | 需要的参数 | 返回什么 |
| --- | --- | --- |
| `describe` | 无额外参数 | 当前策略、权限、启动条件和入口示例 |
| `launch` | `source`，可加入口和输入 | 马上返回 id 和当前状态 |
| `status` | id | 状态、已用 tokens 和 artifacts |
| `wait` | id，可加正数 timeout | 等到完成，或本次等待超时 |
| `result` | id | 读取保留的结果 |
| `stop` | id | 停止子运行并等清理完成 |

注意：

- `wait` 超时只是结束这一次等待，子运行还在跑。这时返回记录里的 `result` 可能是空的，要结合 `status` 决定下一步。
- child id 按调用成员保存。查询和停止均由创建者执行，其他成员通过 Team 已授权的通信关系联系创建者。

## 进阶 让一个 DW 并行安排三个 Agent

成员通过 `file_write` 创建工作区内的 `generated.py`，使用 `path="generated.py"`、`mode="create"`，并将下面完整源码放入 `content`。源码由成员按任务编写，`manage_workflow` 负责加载文件和管理运行。

```python
async def main(ctx, args):
    def make_task(index, task):
        async def run_one():
            answer = await ctx.agent(
                task,
                label=f"task-{index}",
                tools=[],
            )
            return {"task": task, "answer": answer}
        return run_one

    tasks = [make_task(i, task) for i, task in enumerate(args["tasks"])]
    results = await ctx.parallel(tasks)
    return {"results": results, "spent_tokens": ctx.tokens_spent()}
```

入口默认名为 `main`，接收 `ctx` 和 JSON 对象 `args`，返回可 JSON 序列化的值。按源文件启动时可以直接指定异步函数。`ctx.parallel` 接收无参异步任务函数，工厂函数让每个任务保留自己的输入和索引。这里每个任务创建一个新 Agent 会话，`tools=[]` 适合只分析输入文字的任务。返回列表保持输入顺序。

成员再用下面的参数启动文件。

```json
{
  "action": "launch",
  "source": "generated.py",
  "entry": "main",
  "python": "python3",
  "args": {
    "tasks": [
      "用一句中文解释工作区的作用",
      "用一句中文解释会话的作用",
      "用一句中文解释并发的作用"
    ]
  },
  "budget": 12000,
  "concurrency": 2,
  "isolation": "shared"
}
```

`source` 必须是成员工作区内的 Python 文件。`entry` 选择文件内入口，`args` 提供运行输入。`concurrency=2` 让这个 DW 同时最多有两个活跃 Agent 会话，集合任务容量也沿用这个值。三个输入依次填入可用容量。后续处理一百项输入时，成员可以把一百项任务交给同一段代码，并按预算和服务容量选择并发数。

`isolation` 选择子 Workflow 的默认会话环境，省略时继承成员策略。单次 `ctx.agent` 或 `ctx.agent_run` 还能覆盖它。当前配置可以提供 `shared`、`worktree`、`docker-group`、`docker-session`、`vm-group`、`vm-session` 和自定义名称，以 `describe` 返回的目录为准。`cleanup_timeout` 可在 `launch` 时设置正数清理宽限，省略时继承父运行配置。

启动返回后保留实际 child id，成员可以继续其他工作，再用下面的请求观察与读取结果。各段 JSON 分别是一次工具调用。

```json
{"action": "status", "id": "<child id>"}
```

```json
{"action": "wait", "id": "<child id>", "timeout": 30}
```

```json
{"action": "result", "id": "<child id>"}
```

`wait` 和 `result` 的外层记录包含 `result`，其中 `output` 保存入口返回值，`reason` 记录停止或失败原因，`agent_failures` 记录内部会话失败。检查示例的三项 `results` 和各项 `answer`。入口 `completed` 表示 Python 编排已返回，项目交付由实际文件、测试或应用验证确认。需要停止时调用下面的请求并等待清理。

```json
{"action": "stop", "id": "<child id>"}
```

### 从宿主启动这个 Team

配置好模型服务后，将下面代码保存为工作区内的 `run_authored.py`，用安装了指定源码修订的解释器运行。脚本只启动父 Team，成员在任务中编写并启动自己的 DW。示例中的 `sys.executable` 适用于父成员在当前本地环境执行的情况。

```python
import asyncio
import sys
from pathlib import Path

from opencollab import OpenCollab

async def main():
    workspace = Path.cwd().resolve()
    pool = workspace / ".model-request-pool"
    pool.mkdir(exist_ok=True)
    client = OpenCollab(workspace, config={
        "request_pool": str(pool),
        "request_capacity": 2,
    })
    prompt = (
        "先调用 manage_workflow describe。用 file_write 创建 generated.py，"
        "定义 async main(ctx, args)，通过 ctx.parallel 为 args['tasks'] 的每项输入"
        "调用 ctx.agent，tools=[]，返回三项任务及其答案。"
        "launch 使用 entry=main、budget=12000、concurrency=2、isolation=shared，"
        f"python={sys.executable!r}。args 的 tasks 为"
        "['用一句中文解释工作区的作用', '用一句中文解释会话的作用', "
        "'用一句中文解释并发的作用']。保留启动返回的 id，查询状态，"
        "等待完成并读取结果，检查三项答案后提交。"
    )
    result = await client.team(
        prompt,
        config=workspace / "team.yaml",
        isolation="shared",
        allow_unisolated_shell=True,
        budget=20000,
        max_steps=16,
        timeout=180,
        artifacts=workspace / "authored-run",
        trace=False,
    )
    result.raise_for_status()
    print(result.output)

asyncio.run(main())
```

`request_capacity=2` 限制共享模型客户端同时进行的原生请求。`concurrency=2` 限制单个 DW 的活跃 Agent 会话，两项容量分别作用于模型服务和工作流。多个 Team 或 DW 使用同一服务时，宿主让它们共享同一 `request_pool` 绝对目录及容量配置。原生请求结束、进入工具执行前会释放请求名额，父成员等待子运行时，子会话可以继续请求模型。

宿主可以通过 `client.configuration` 检查模型、服务商、协议、路由映射、上下文与请求容量等公开配置。成员通过工具 schema 和 `describe` 发现当前执行能力。模型路由和容量由人类调用方提前配置，成员按获得的接口组织任务。

### 先运行仓库的本地演示

在已安装当前源码的 checkout 中，可以先执行仓库已有的离线演示。

```bash
python examples/member-authored-workflow/demo.py
```

演示启动回环地址上的脚本化 HTTP 模型，由父成员实际写入 `generated.py`，启动 Python 子进程并收集原生结果。输出包括 `Local answer`、计算值 `15` 和总用量 `90`，打印的工作区保留源码与运行产物。随后可用上面的三任务源码和 Team 调用连接自己的模型服务。

### 在 VM 成员内创建子工作区

按[VM 环境扩展](../isolation/vm-environment-extension.md)准备宿主 VM profile、SSH、guest 内的同修订 OpenCollab 与管理员 reset 操作。父 Team 使用 `isolation="vm-session"` 时，author 获得一个 VM。成员先 `describe`，再把源码写入该 VM 的工作区，使用下面的启动参数让子会话在这个 guest 内创建独立工作树。

```json
{
  "action": "launch",
  "source": "generated.py",
  "entry": "main",
  "python": "/home/worker/.venvs/oc/bin/python",
  "args": {"tasks": ["检查输入文件", "验证运行结果", "汇总观察记录"]},
  "concurrency": 2,
  "isolation": "worktree"
}
```

这里的 `python` 指向 VM 准备步骤中安装 OpenCollab 的来宾解释器。DW 编排子进程也在父成员 VM 内运行，子 Agent 的工具在该 VM 的独立工作树中执行。要让子 Agent 观察或修改文件，父角色先获得相应工具，再在 DW 源码中用 `builtin_tools` 传给会话。例如 `builtin_tools("bash")` 可以配合明确的文件检查任务使用。

单次调用选择 `isolation="vm-session"` 时，宿主会为它申请另一台空闲 VM。父成员已经持有的 VM 计入资源占用，容量规划需要为额外 guest 留出资源。桥接层的模型凭据、端点路由、请求池路径和共享请求名额由宿主管理。子进程标准输入传入非敏感生成设置、输入与工具限制，guest 经 bridge 请求宿主模型客户端。

## 预算

| 情况 | 子运行拿到的预算 |
| --- | --- |
| 省略 `budget` | 父成员尚未预留余额的一半，给父成员留出协调空间 |
| 显式 `budget` | 不超过可用余量 |
| 父运行额度无限 | 可以启动无限额度的子运行 |

- 有限额度会在活跃的子运行之间预留，也会影响父成员后续的模型请求。
- 用量汇总到父成员时，按实际消耗只计一次。

## 子运行什么时候结束

子运行持续到以下任一情况：入口返回、执行失败、父成员停止。

- 父成员正常完成时，会先停止还在跑的子运行，再保存最终的父快照。
- 取消时，先请求子运行停止会话并保存状态。如果迟迟不结束，由拥有它的 Environment 取消命令并清理进程。
- 普通 `bash` 命令的超时和这套生命周期是分开生效的。

## 排查错误

记录保存在哪：

- 环境里的 `.opencollab/child-runs/<id>` 保存活跃的控制文件。
- 父运行设置了产物目录时，会收集子运行的 Workflow 清单、会话、结果和命令输出。完成后 `artifacts` 指向宿主上的持久目录。
- 父运行关闭时，先清理环境里的控制文件，再捕获成员的改动。

错误会出现在哪：

- 源文件导入错误、执行错误、输出序列化错误，都会写进结果。
- 子工作流在会话失败后仍返回了数据时，`agent_failures` 照样保留失败信息。

排查顺序：先读结果和进程输出，再对照入口名和父成员的工具授权。

子工作流每次 Agent 调用都可以选择 `shared`、`worktree`、`docker-group`、`docker-session`、已配置的 `vm-group`、`vm-session` 或自定义策略，以 `describe` 返回的名称为准。父成员的步骤上限为子会话提供默认值，作者也可以通过 `ctx.agent_run(max_steps=...)` 指定单次步数。子环境分配与释放由宿主管理。

## 源码依据

[能力发现与子运行启动](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow.py#L63)、[宿主模型桥接](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow_bridge.py#L127)、[子会话权限继承](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow_runner.py#L38)。

[examples/member-authored-workflow/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/member-authored-workflow/README.md#L3)、[opencollab/bootstrap/authored_workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow.py#L52)。

## 相关章节

[让 Team 成员调用已注册的 Workflow](team-run-workflow.md)、[编写第一个自定义 Workflow](author-custom-workflow.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[等待队友结果并读懂团队状态](../team/wait-for-team-results.md)。
