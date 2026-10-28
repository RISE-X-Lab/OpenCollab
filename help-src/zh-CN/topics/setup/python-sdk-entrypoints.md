---
{
  "id": "python-sdk-entrypoints",
  "title": "从 Python 调用 OC",
  "category": "setup",
  "summary": "用 OpenCollab 客户端在 Python 里运行单 Agent、Team 或 Workflow：先跑通只读 Agent，再放开写文件和命令，并理解 Base 与 Single2 的关系。",
  "aliases": [
    "代码调用",
    "嵌入自己的程序",
    "异步调用",
    "Base",
    "Single2",
    "config=",
    "model_routes=",
    "create_model_client",
    "单智能体",
    "single agent",
    "agent2",
    "一个人做任务",
    "BASE_PROFILE",
    "resolve_profile_name",
    "单个 Agent"
  ],
  "keywords": [
    "Python SDK",
    "OpenCollab",
    "agent",
    "agent2",
    "team",
    "workflow",
    "asyncio.run",
    "configuration",
    "Base",
    "Single2",
    "单Agent",
    "profile",
    "tools=\"read\"",
    "tools=\"coding\""
  ],
  "questions": [
    "在现有 Python 程序中构造客户端，调用单 agent、团队或工作流，并接入程序的结果处理？",
    "怎么在自己的 Python 程序里调用 OC？",
    "在 Notebook 或 Web 服务里怎么用？",
    "team() 的 config 参数和客户端的 config 有什么不同？",
    "直接运行一个 Agent，理解 Base 默认配置并按任务选择工具和执行环境？",
    "怎么用 Python 跑一个 Agent？",
    "Base 和 Single2 是什么关系？",
    "怎么让 Agent 在本机直接执行命令？"
  ],
  "related": [
    "configuration-precedence",
    "tool-selection",
    "run-results",
    "token-step-concurrency-limits",
    "service-integration-lifecycle"
  ],
  "verified_ref": "v0.9.6"
}
---

# 从 Python 调用 OC

在自己的程序里用 OC，只需要记住一个对象和三个方法：`OpenCollab(workspace)` 是客户端，它的 `agent()`、`team()`、`workflow()` 分别运行单 Agent、团队和 Workflow，三者都返回同一种 `RunResult`，所以应用可以用一套代码处理结果。这篇先跑通一个只读的单 Agent，再放开写文件和执行命令，然后说明团队、Workflow 和结果处理。

## 先理解客户端

- `OpenCollab(workspace)` 的第一个参数是工作区，也就是 Agent 要处理的项目目录，必须已经存在。
- 模型、凭据、超时这些设置在**创建客户端时**读取一次。之后再改环境变量或配置文件，已有的客户端不会变，要重新创建。见 [配置从哪来、哪个生效](../models/configuration-precedence.md)。
- 三个方法都是 `async` 的。脚本里用 `asyncio.run(main())`；如果你的程序已经有事件循环（Web 服务、Notebook），就直接 `await`，不要再套 `asyncio.run`。

## 第一步：跑一个只读的单 Agent

把下面的脚本放在项目根目录，在装了 OC 的 Python 环境里运行：

```python
import asyncio
from opencollab import OpenCollab, RunError

async def main() -> None:
    client = OpenCollab(".")  # 当前目录作为工作区
    result = await client.agent(
        "阅读项目入口和测试配置，说明主要模块及建议执行的测试命令。",
        tools="read",          # 只读工具：file_read、grep、git_diff
        budget=100_000,        # token 预算
        max_steps=40,          # 最多执行 40 步
        timeout=300,           # 整次任务期限（秒）
        artifacts="artifacts/first-agent",  # 运行记录目录，必须是新目录或空目录
    )
    try:
        # 完成时返回结果自身，其他状态抛出 RunError
        print(result.raise_for_status().output)
    except RunError as exc:
        outcome = exc.result
        if outcome is not None:
            print(outcome.status, outcome.reason, outcome.output)
        raise
    print(result.metrics.get("agent_profile"))  # 实际用到的 Agent 配置

asyncio.run(main())
```

`tools="read"` 只给 Agent 读文件、搜索和看差异三个工具。它只能看、不能改，适合第一次了解项目。`artifacts` 每次运行都要换一个新目录。

## 第二步：让 Agent 改代码、跑命令

不传 `tools` 时，默认是 `tools="coding"`，依次提供 `bash`、`file_read`、`file_write`、`apply_patch`、`git_diff` 和 `grep`。

这里有一点和多数框架不同：**默认的 `bash` 要求执行环境有操作系统级的进程隔离**，例如 Docker。在普通的本机环境里，文件工具能用，默认的 `bash` 却跑不起来。这是 OC 有意的保守设置。

如果你确实要让 Agent 直接在本机项目里执行命令，就自己构造工具并显式放开。下面的代码放在一个已有的 `async` 函数里，`client` 沿用前面创建的客户端：

```python
from opencollab.tools import builtin_tools, profile_tool_limits

tools = builtin_tools(
    "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
    headless=False,                      # 允许使用本机 shell
    limits=profile_tool_limits("base"),  # 沿用 Base 的工具输出上限
)
result = await client.agent2(
    "修复目标测试，运行仓库规定的验证命令，并说明改动。",
    tools=tools,
    budget=150_000,
    max_steps=80,
)
```

`headless=False` 意味着命令直接作用在你的电脑上，请确认任务内容可信。想要进程隔离，就选一个容器或 VM 执行环境，见 [隔离](../isolation/isolation-overview.md)。

### `agent()` 的常用参数

| 参数 | 作用 |
| --- | --- |
| `tools` | `"read"`、`"coding"`（默认），或自己构造的工具列表，见 [内置工具](../extensions/tool-selection.md) |
| `system_prompt` | 替换这次调用的默认系统提示 |
| `budget` | 整个会话累计的输入加输出 token 上限 |
| `max_steps` | 执行步数上限。旧参数 `steps` 是它的别名，两者同时传会报错 |
| `timeout` | 整次任务期限（秒） |
| `artifacts` | 运行记录目录，必须是新目录或空目录 |
| `profile` | 选择 Agent 配置，见下一节 |
| `run_control` | 运行中调整软额度、接收事件，见 [预算、步数与并发](../operations/token-step-concurrency-limits.md) |

你显式传入的工具对象会保留它自己的配置。模型、采样参数、单次输出上限 `max_output_tokens` 和请求超时属于客户端配置，不在这里改。

## Base、Single2 和 `agent2()` 的关系

OC 把“提示词 + 默认工具 + 默认限额”的一套预设叫作 profile（Agent 配置）。

- `agent()` 使用 Base。Base 是一个别名，从 OpenCollab 0.8 起指向 Single2。
- `agent2()` 明确选择 Single2，等同于 `agent(..., profile="single2")`。
- `None`、`"base"`、`"default"`、`"single"` 都会解析成 Base 当前指向的配置。

怎么选：希望跟着框架的默认实现一起升级，就用别名；集成必须固定用 Single2，就传 `profile="single2"`，并在运行记录里保存 OC 版本。

想确认你装的版本里 Base 指向什么，可以运行下面的程序，它会打印三行 `single2`：

```python
from opencollab.profiles import BASE_PROFILE, resolve_profile_name

print(BASE_PROFILE)
print(resolve_profile_name("base"))
print(resolve_profile_name("single2"))
```

Single2 的默认限额：

- 默认最多运行 200 步。
- `bash` 的标准输出和标准错误各最多保留 10,000 字符。
- 较大的工具结果，在送给模型的上下文里只保留开头和结尾，总字符上限 16,000。
- 设置了 `OPENCOLLAB_UNBOUNDED_LIMITS=true` 时，你在调用里显式给出的正数预算和步数仍然生效；没给的限制不设上限。

## 运行团队：`team()`

`team()` 的 `config=` 是**团队 YAML 文件的路径**，和创建客户端时 `OpenCollab(..., config={...})` 里的模型运行设置不是一回事，别混淆。

先用 `opencollab team init team.yaml` 导出一份团队文件，检查角色工具和执行环境，再调用（放在已有的 `async` 函数里）：

```python
result = await client.team(
    "分析目标问题，安排实现与验证，并提交结果。",
    config="team.yaml",
    budget=200_000,
    timeout=900,
)
```

团队文件怎么写见 [Team 入门](../team/configure-team.md)。

## 运行 Workflow：`workflow()`

`workflow()` 的第一个参数可以是三种东西：

- 名称：内置 Workflow，或工作区 `workflows/` 目录下按名称注册的 Workflow。相对路径的 `OPENCOLLAB_WORKFLOWS_DIR` 也按工作区解析。
- 函数：普通的 `async` 函数，或用 `@workflow` 装饰过的函数。
- spec：带可调用 `fn` 的 `WorkflowSpec`。

下面的完整例子用 Python 安排一次只读的角色调用：

```python
import asyncio
from opencollab import OpenCollab, workflow
from opencollab.tools import builtin_tools

@workflow(name="project-summary")
async def project_summary(ctx, inputs):
    # 只给这个角色只读工具
    tools = builtin_tools("file_read", "grep", "git_diff")
    return await ctx.agent(inputs["task"], tools=tools, label="reader")

async def main() -> None:
    result = await OpenCollab(".").workflow(
        project_summary,
        {"task": "阅读项目入口，说明主要模块之间的关系。"},
        budget=100_000,
        concurrency=1,
    )
    print(result.status, result.output)

asyncio.run(main())
```

怎样写 Workflow 见 [编写 Workflow](../workflow/workflow-programming-model.md)。

## 处理结果和错误

| `status` | 含义 |
| --- | --- |
| `completed` | 正常结束 |
| `stopped` | 提前停下，原因在 `reason`（如超时、预算或步数用完）；`output` 里可能有部分工作 |
| `failed` | 失败，`output` 里可能有部分工作 |

- `raise_for_status()`：完成时返回结果自身；其他状态抛出 `RunError`，异常的 `result` 属性带着原始结果。
- 参数错误在执行前就会抛出；生命周期或持久化故障也可能直接抛出 `RunError`。应用应在调用处统一处理。
- `completed` 只说明运行结束，不说明任务做对了。字段含义和验收方法见 [判断任务是否真的完成](../observability/run-results.md)。

## 其他公开入口

- 扩展模块：`opencollab.tools`、`opencollab.environments`、`opencollab.workflows`、`opencollab.profiles`。
- `client.configuration`：只读的生效配置，凭据已经去掉，适合做配置检查。
- `client.create_model_client()`：用同一份配置创建原生模型客户端，用于自定义模型调用。用完后由你执行 `await model.close()`。见 [把 OC 接进自己的服务](../architecture/service-integration-lifecycle.md)。

## 延伸阅读

- 源码依据：[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/sdk/client.py) · [opencollab/profiles.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/profiles.py) · [opencollab/bootstrap/agent_profiles.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/agent_profiles.py) · [docs/single2.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/single2.md) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/README.md)
- 相关指南：[内置工具](../extensions/tool-selection.md) · [判断任务是否真的完成](../observability/run-results.md) · [预算、步数与并发](../operations/token-step-concurrency-limits.md) · [把 OC 接进自己的服务](../architecture/service-integration-lifecycle.md)
