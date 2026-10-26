---
{
  "id": "team-run-workflow",
  "title": "让 Team 成员运行 Workflow",
  "category": "workflow",
  "summary": "让 Team 成员运行 Workflow 的两种工具：run_workflow 同步运行已注册的 Workflow；manage_workflow 让成员现场写 Python 子 Workflow、异步启动并按 id 管理，可在成员自己的 VM 里并行调用多个 Agent。",
  "aliases": [
    "队友跑工作流",
    "Team → Workflow",
    "工作流工具",
    "调用 Duo",
    "调用 Weave",
    "PR237",
    "嵌套工作流",
    "让 Team 成员调用已注册 Workflow",
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
    "run_workflow",
    "Team 调用 Workflow",
    "子 Workflow",
    "注册工作流",
    "workflow-agent",
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
    "Team 成员怎样调用 Duo？",
    "run_workflow 会等到运行结束吗？",
    "子 Workflow 花费算到谁的预算？",
    "run_workflow 能传哪些参数？",
    "Team 成员怎样发现 Duo、Weave 或自定义流程的输入？",
    "Agent 能自己写一个工作流再运行吗？",
    "启动后怎样查询、等待和停止子 Workflow？",
    "父 Agent 结束后子 Workflow 会怎样？",
    "子 Workflow 的预算默认是多少？",
    "成员怎样现场编写一个 DW 并并行调用多个 Agent？",
    "使用 manage_workflow 前，人需要配置哪些资源和工具？",
    "VM 成员怎样在自己的 guest 内创建子工作树？"
  ],
  "related": [
    "configure-team",
    "dispatch-team-agents",
    "run-registered-workflow",
    "vm-environment-extension",
    "model-routing-request-capacity"
  ],
  "verified_ref": "0c47f74a"
}
---

# 让 Team 成员运行 Workflow

Team 成员除了派活和发消息，还可以把一段工作交给 Workflow 来做。OC 提供两个工具：`run_workflow` 运行一个**已经注册好的** Workflow，跑完才返回；`manage_workflow` 让成员**现场写一段 Python** 作为子 Workflow，启动后立刻返回一个 id，之后按 id 管理。这篇先对比两者，再分别讲用法。

## 两种工具怎么选

|  | `run_workflow` | `manage_workflow` |
| --- | --- | --- |
| 运行什么 | 已注册的 Workflow 名称（如 `duo`） | 成员自己写的 Python 源文件 |
| 怎么等 | 同步：整个 Workflow 结束才返回 | 异步：`launch` 立刻返回 id，之后用 `status`、`wait`、`result`、`stop` 管理 |
| 在哪执行 | 借用调用成员当前的环境和工作目录 | 成员所在环境里的一个独立 Python 子进程 |
| 额度 | 子 Workflow 的用量记到调用成员名下 | 从父成员的余额里为子运行预留 |
| 角色需要 | `run_workflow` | `manage_workflow` 和 `bash`，目标环境里有装了 OC 的 Python |

典型用法：修复负责人在需要时调用 Duo，读完交付结果再继续协作，用 `run_workflow`；成员要按任务现场编排几个 Agent 并行处理输入，或者启动后想先去做别的事，用 `manage_workflow`。

## 方式一：`run_workflow` 运行已注册的 Workflow

### 最小用法

1. 在团队文件里，把 `run_workflow` 加到需要它的角色上：

   ```yaml
   entry: coordinator
   roles:
     coordinator:
       prompt: 先用 run_workflow describe 读取输入说明，再按任务调用流程并核对交付结果。
       tools: [file_read, bash, run_workflow]
   ```

2. 确认可用的 Workflow 名称：`opencollab workflow list --workspace ...`。注册来源和普通 Workflow 入口相同（内置的加上工作区目录里的），运行时会根据已发现的 Workflow 自动生成 `name` 的可选值和说明。

3. 成员先读取所选 Workflow 的输入说明：

   ```json
   {"action": "describe", "name": "duo"}
   ```

   返回 `name`、`description`、`inputs` 和 `phases`。`inputs` 取自流程函数的 docstring，没有 docstring 时用流程说明代替。所以自定义 Workflow 要在 docstring 里写清必填字段、类型和输入示例。Duo、Weave、Reconstruct 要的输入各不相同，每换一个流程都要先 `describe`。

4. 成员按说明用 `run` 执行。下面是一次 Duo 调用：

   ```json
   {
     "action": "run",
     "name": "duo",
     "args": {"goal": "修复配置读取错误并验证"},
     "budget": 50000,
     "concurrency": 2
   }
   ```

### 参数

| 字段 | 要求 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `action` | 可选，`describe` 或 `run` | `run` | 读取说明或执行流程 |
| `name` | 必填，注册名称 | 无 | 选择要跑的 Workflow |
| `args` | `run` 时必填，JSON 对象 | 无 | 原样作为 Workflow 输入 |
| `budget` | 可选，正整数 | 成员剩余额度 | 本次上限，与剩余额度取较小值 |
| `concurrency` | 可选，正整数 | 4 | Workflow 内同时活跃的 Agent 会话数 |

工具只开放这些字段，`budget` 和 `concurrency` 只在 `run` 时起作用。SDK 的其他运行参数，要在宿主调用或 Workflow 源码里设置。

### 环境、额度和权限

- 子 Workflow 借用调用成员当前的执行环境和工作目录、有效模型配置和权限策略；环境的创建和释放仍由 Team 运行管理。
- 子 Workflow 内部会话的 token 记到调用成员名下，成员之后的请求会看到扣减后的余额；成员额度已经用完时，工具直接返回额度耗尽错误。
- Workflow 作者为每次 `ctx.agent` 指定工具，成员的权限策略照样参与执行检查；Workflow 也继承父成员的步数上限。

### 读取返回值

返回值是 JSON 文本：

| 字段 | 含义 |
| --- | --- |
| `output` | Workflow 入口函数的返回值 |
| `tokens` | 本次消耗 |
| `stop_reason` | 停止原因，例如额度耗尽 |
| `agent_failures` | 内部会话的失败信息 |
| `artifacts` | 保存目录 |

工具成功返回不等于任务完成。还要读 Workflow 自己的交付字段，例如 Duo 的 `adopted`、Weave 的 `delivery_ok`，并检查实际文件或验证结果。

Team 设置了保存目录时，子 Workflow 的记录在其中单独的 `workflow-agent-...` 子目录里，可以用来查某个成员调用了哪个具体角色。用户停止父成员时，取消信号会打断正在等待的 Workflow；工具会等它清理完，再返回中断提示。

## 方式二：`manage_workflow` 让成员自己写子 Workflow

成员用文件工具写出一个 Python 文件，再调用 `launch` 启动它。OC 在成员自己的执行环境里起一个独立的 Python 进程导入这个文件，入口函数和普通 Workflow 一样接收 `ctx` 和 `args`。这样一段由成员现场编排的子 Workflow，团队内部也叫 DW（动态工作单元）。

版本要求：需要源码提交 `0c47f74a` 对应的 OC，按 [安装指南](../setup/first-run-workspace.md#第一步安装) 准备解释器。包内版本号和实际安装的源码提交要分别核对。

### 开始前，你（人类调用方）要准备好

- 模型服务和额度。模型配置放在控制端进程的环境变量里，或者放在工作区**之外**的配置文件里（原因见 [凭据和配置放在哪](#凭据和配置放在哪)）。
- 执行环境。要用逻辑模型路由、共享请求容量、Docker 镜像或 VM，也由你提前配好。
- 角色的工具授权：

  ```yaml
  entry: author
  roles:
    author:
      prompt: 编写前先调用 manage_workflow describe，再按任务需要写出 Workflow，启动并读取结果。
      tools: [file_write, file_read, bash, manage_workflow]
  ```

- 运行环境：角色必须有 `bash`，因为 `manage_workflow` 用成员的 shell 权限执行源码；目标环境里要有装了 OC 的 Python（默认 `python3`，可以用参数 `python` 指定别的路径）。在宿主机上直接执行时，Team 调用方要显式允许 `allow_unisolated_shell`。容器或 VM 成员在自己的环境里执行源文件，那里的解释器也要装同一个 OC 源码提交，工具原有的进程隔离要求和命令安全检查照常生效。

### 第 1 步：看看能用什么

成员先调用：

```json
{"action": "describe"}
```

| 返回字段 | 含义 |
| --- | --- |
| `default_isolation` | 默认隔离策略 |
| `strategies` | 当前配置了哪些隔离策略 |
| `launch_allowed_in_current_environment` | 当前环境能不能 `launch` |
| `allowed_child_tools` | 子会话可以用哪些工具 |
| `builtin_child_tools` | 其中哪些可以用 `builtin_tools` 构造 |

返回值里还有入口说明和一段最小源码。每个策略都标着 `checked_on_allocation`：`describe` 只列出配置、不占用资源，连接是否可用、资源够不够，要等实际分配时才检查。

### 第 2 步：写出子 Workflow 文件

成员用编辑工具在工作区写入 `generated.py`。下面这个文件先算出输入的总和，再开一个 Agent 会话解释这个数：

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

- OC 按 `entry` 加载入口函数，默认是 `main`，不需要装饰器。
- 子会话请求的工具名必须在父成员的可用工具里；子会话还继承父成员的输出上限、shell 隔离要求和文件创建限制。

### 第 3 步：启动

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

| 参数 | 说明 |
| --- | --- |
| `source` | 成员工作区里的 Python 文件，必填 |
| `entry` | 入口函数名，默认 `main` |
| `args` | 传给入口的输入，要能跨进程序列化，用字符串、数字、列表和字典 |
| `python` | 目标环境里的解释器，默认 `python3` |
| `budget` | 子运行的 token 预算，见 [预算](#预算) |
| `concurrency` | 子运行里同时活跃的 Agent 会话上限，默认 4 |
| `isolation` | 子运行里 Agent 会话的默认隔离策略，省略时继承成员的策略 |
| `cleanup_timeout` | 清理宽限秒数，正数，省略时继承父运行配置 |

`isolation` 和 `cleanup_timeout` 只能在 `launch` 里用；代码里单次调用 `ctx.agent` 或 `ctx.agent_run` 时，还可以再覆盖 `isolation`。

### 第 4 步：查询、等待与停止

之后的操作都用 `launch` 返回的 id：

| `action` | 需要的参数 | 返回什么 |
| --- | --- | --- |
| `describe` | 无 | 当前策略、权限、启动条件和入口示例 |
| `launch` | `source`，可加入口和输入 | 马上返回 id 和当前状态 |
| `status` | `id` | 状态、已用 token 和产物 |
| `wait` | `id`，可加正数 `timeout` | 等到完成，或本次等待超时 |
| `result` | `id` | 读取保留的结果 |
| `stop` | `id` | 停止子运行，并等清理完成 |

例如 `{"action": "wait", "id": "<child id>", "timeout": 30}`。

- `wait` 超时只是结束这一次等待，子运行还在跑。这时返回记录里的 `result` 可能是空的，要结合 `status` 决定下一步。
- `wait` 和 `result` 返回的 `result` 字段里有 `output`（入口函数的返回值）、`reason`（停止或失败原因）和 `agent_failures`（内部会话的失败记录）。
- `completed` 只说明 Python 编排代码返回了。项目是否真的交付，要靠实际文件、测试或应用来验证。
- 子运行 id 只属于创建它的成员，只有创建者能查询和停止它。其他成员想操作，要通过 Team 已授权的通信关系联系创建者。

### 并行跑多个 Agent 的子 Workflow

成员可以按任务现场生成三项、一百项任务，交给 `ctx.parallel`。成员调用 `file_write`，`path="generated.py"`、`mode="create"`，`content` 放下面的源码（源码由成员按任务自己写，`manage_workflow` 只负责加载和管理）：

```python
async def main(ctx, args):
    # 工厂函数让每个任务保留自己的 index 和 task
    def make_task(index, task):
        async def run_one():
            answer = await ctx.agent(
                task,
                label=f"task-{index}",
                tools=[],  # 只分析输入文字，不需要工具
            )
            return {"task": task, "answer": answer}
        return run_one

    tasks = [make_task(i, task) for i, task in enumerate(args["tasks"])]
    results = await ctx.parallel(tasks)  # 结果按输入顺序返回
    return {"results": results, "spent_tokens": ctx.tokens_spent()}
```

然后启动：

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

`concurrency=2` 表示这个子 Workflow 里同时最多两个活跃的 Agent 会话，`ctx.parallel` 的任务容量默认也用这个值。所以三个任务会先跑两个，空出位置再跑第三个。同一段代码也能处理一百项输入，按预算和模型服务的容量选并发数即可。成员检查 `output` 里的三项 `results` 和每项的 `answer`。

### 子 Agent 在哪里执行

子 Workflow 的编排代码跑在成员所在的环境里；子 Agent 的工具在哪里执行，按三层决定，后面覆盖前面：

1. 成员自己的策略：`launch` 不写 `isolation` 时，子运行继承它。
2. `launch.isolation`：整个子运行的默认策略。
3. 单次 `ctx.agent(..., isolation=...)`：只对这一次调用生效。

可选的值以 `describe` 返回的列表为准，常见的有：

| 策略 | 子 Agent 在哪执行 |
| --- | --- |
| `shared` | 和成员共用执行环境和工作区 |
| `worktree` | 在继承来的执行环境里，各自用独立工作树 |
| `docker-group` | 共用一个专属容器，各自用独立工作树 |
| `docker-session` | 每个子 Agent 一个专属容器和工作区 |
| `vm-group` | 租用一台已配置的 Linux VM，各自用独立工作树 |
| `vm-session` | 每个子 Agent 租用一台已配置的 Linux VM |

`group` 的范围是“一次运行里的一个环境池”，而子运行有自己的环境池。所以想让子 Agent 留在成员当前的容器或 VM 里，子运行要选 `worktree`；子运行再选 `vm-group`，可能领到另一台 VM。

### 在 VM 成员里用独立工作树

一个常见组合：父 Team 使用 `isolation="vm-session"`，author 成员拿到一台 VM；成员把源码写进这台 VM 的工作区，再用 `worktree` 启动子运行，子会话就在同一台来宾机里各自创建独立工作树：

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

- `python` 指向准备 VM 时装了 OC 的来宾解释器。VM、SSH、来宾里同一提交的 OC 和管理员重置程序，都要先按 [在现有 Linux VM 中运行任务](../isolation/vm-environment-extension.md) 准备好。
- 子 Agent 要查看或修改文件，父角色得先有对应工具，再在源码里用 `builtin_tools` 传给会话，比如给一个明确的文件检查任务配上 `builtin_tools("bash")`。
- 某次调用选 `isolation="vm-session"` 时，宿主会再为它申请一台空闲 VM。父成员手里那台也算在占用里，规划容量时要给额外的来宾留出位置。

### 从宿主启动这个 Team

上面是成员这一侧的工具调用。在宿主这一侧，你只需要启动父 Team，成员会在任务里自己写并启动子 Workflow。配好模型服务后，把下面的代码存为工作区里的 `run_authored.py`，用装了指定提交的解释器运行。示例里的 `sys.executable` 只适用于父成员就在当前本地环境执行的情况。

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

这里有两个容量设置，管的是不同的东西：

| 设置 | 在哪设 | 限制什么 |
| --- | --- | --- |
| `request_capacity=2` | 宿主的 `OpenCollab` 配置 | 共享模型客户端同时发出的模型请求数 |
| `concurrency=2` | `launch` 参数 | 单个子 Workflow 里同时活跃的 Agent 会话数 |

`request_pool` 和 `request_capacity` 要一起设置。多个 Team 或子 Workflow 用同一个模型服务时，让它们使用同一个 `request_pool` 绝对目录和同样的容量。一次模型请求结束、开始执行工具之前，OC 就会释放名额，所以父成员在等子运行时，子会话照样可以请求模型。见 [模型路由与共享请求容量](../models/model-routing-request-capacity.md)。

宿主可以通过 `client.configuration` 查看公开配置；成员则通过工具 schema 和 `describe` 了解自己能做什么。模型路由和容量由你提前配好，成员只按拿到的接口组织任务。

### 先跑一下仓库自带的离线演示

在装好当前源码的 checkout 里运行：

```bash
python examples/member-authored-workflow/demo.py
```

演示在回环地址上启动一个脚本化的 HTTP 模型（所有请求都留在本机），父成员真的写出 `generated.py`，再启动 Python 子进程收集结果。输出里会有 `Local answer`、计算值 `15` 和总用量 `90`，程序会打印保留了源码和运行产物的工作区路径。跑通之后，再用上面的三任务源码和 Team 调用接上你自己的模型服务。仓库还有一个 [双 VM 离线示例](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/vm-authored-workflow/demo.py)，用法见 VM 指南。

### 预算

| 情况 | 子运行拿到的预算 |
| --- | --- |
| 省略 `budget` | 父成员尚未预留余额的一半，给父成员留出协调空间 |
| 显式 `budget` | 不超过可用余量 |
| 父运行额度无限 | 可以启动无限额度的子运行 |

有限预算会为活跃的子运行预留，这部分父成员后续的模型请求也不能用。子运行的用量按实际消耗计入父成员，只计一次。

### 子运行什么时候结束

入口返回、执行失败或父成员停止，子运行就结束：

- 父成员正常完成时，OC 先停止还在跑的子运行，再保存最终的父快照。
- 取消时，OC 先请子运行停止会话并保存状态；它迟迟不结束，就由所属的执行环境取消命令并清理进程。
- 普通 `bash` 命令的超时和这套生命周期互不影响。

### 凭据和配置放在哪

- 模型凭据、端点路由、请求池路径和共享请求名额都留在宿主（控制端）的桥接层。子进程要请求模型时，经由桥接层交给宿主的模型客户端。
- 子进程的标准输入只收到非敏感的生成设置（如模型名、温度、思考设置）、输入和工具限制。
- OC 会按快照规则把工作区文件复制给子运行。所以**不要把含密钥的 env 文件放进项目目录**：放到项目外面，再用 `OPENCOLLAB_CONFIG_FILE` 指向它在控制端的绝对路径。

### 排查错误

- 环境里的 `.opencollab/child-runs/<id>` 保存运行中的控制文件。父运行设置了产物目录时，OC 会收集子运行的 Workflow 清单、会话、结果和命令输出，完成后 `artifacts` 指向宿主上的持久目录。父运行关闭时，OC 先清理环境里的控制文件，再捕获成员的改动。
- 源文件导入错误、执行错误、输出序列化错误都会写进结果；某个 Agent 会话失败了、但子 Workflow 仍然返回了数据时，`agent_failures` 照样会记下来。
- 排查顺序：先读结果和进程输出，再核对入口名和父成员的工具授权。
- 子会话的步数上限默认取父成员的上限，作者也可以用 `ctx.agent_run(max_steps=...)` 指定单次步数。

## 延伸阅读

- 源码依据：[opencollab/bootstrap/team_workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/team_workflow.py) · [能力发现与子运行启动](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow.py) · [宿主模型桥接](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow_bridge.py) · [子会话权限继承](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/authored_workflow_runner.py) · [examples/member-authored-workflow/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/examples/member-authored-workflow/README.md) · [tests/interfaces/test_team_workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/tests/interfaces/test_team_workflow.py)
- 相关指南：[Team 协作](../team/dispatch-team-agents.md) · [运行一个 Workflow](run-registered-workflow.md) · [在现有 Linux VM 中运行任务](../isolation/vm-environment-extension.md) · [模型路由与共享请求容量](../models/model-routing-request-capacity.md)
