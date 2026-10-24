---
{
  "id": "run-registered-workflow",
  "title": "运行一个工作流（Workflow）",
  "category": "workflow",
  "summary": "用命令行或 Python 找到一个工作流，交给它一项任务，然后查看结果。",
  "aliases": [
    "运行工作流",
    "工作流在哪",
    "--task",
    "--task-file",
    "--args",
    "OPENCOLLAB_WORKFLOWS_DIR",
    "@workflow",
    "工作流重名",
    "怎么跑工作流",
    "workflow 入门"
  ],
  "keywords": [
    "workflow list",
    "workflow run",
    "OpenCollab.workflow",
    "工作流运行"
  ],
  "questions": [
    "Workflow 是什么，怎样启动？",
    "自己写的工作流为什么没有出现在列表里？",
    "Duo、Weave 和 Reconstruct 分别适合什么任务？",
    "怎样用命令行运行一个工作流？",
    "运行完去哪里看结果？"
  ],
  "related": [
    "author-custom-workflow",
    "team-run-workflow",
    "duo-workflow",
    "weave-workflow",
    "reconstruct-workflow",
    "run-results"
  ],
  "verified_ref": "0c47f74a"
}
---

# 运行一个工作流（Workflow）

OC 的 Workflow 是用 Python 显式编排 Agent 调用的方式：一个 `async def` 函数决定何时开会话、怎样用返回值、哪些分支并发。它不像 LangGraph 那样先声明图，流程就是函数里的普通控制流。与之相对，OC 的 Team 是由模型自主派发的多 Agent。每次 `ctx.agent` 调用都是一个独立会话，有自己的历史，步骤之间靠 Python 变量传递信息。

最小用法：`opencollab workflow run duo --workspace DIR --task '...'`，或在 Python 里 `await OpenCollab(DIR).workflow('duo', {'goal': '...'})`。下文依次讲怎样找到工作流、传参、读结果。

## 先选一个现成的工作流

OC 自带几个工作流，可以直接用：

| 名称 | 适合什么任务 |
| --- | --- |
| Duo | 想要两个候选方案，再比较选出一个 |
| Weave | 任务能拆成几组，分组执行，最后检查交付 |
| Reconstruct | 已有输入和输出样例，想重建出对应程序 |

如果这些都不合适，而你已经清楚每一步该怎么做，可以自己写一个异步函数，见 [编写第一个自定义 Workflow](author-custom-workflow.md)。

## 第 1 步：列出可用的工作流

在已经配置好模型的环境里运行下面的命令。把示例路径换成你自己已有的目录。

```bash
# 列出内置工作流和工作区里自己写的工作流
opencollab workflow list --workspace /path/to/workspace
# 查看 duo 接受哪些参数
opencollab workflow run duo --help
```

列表里有两类工作流：

1. 安装 OC 时自带的内置工作流。
2. 工作区（`--workspace` 指定的项目目录）下 `workflows/` 目录里的工作流。

OC 只加载 `workflows/` 顶层的 Python 文件，并读取每个函数上 `@workflow` 写的名称和说明。

如果想换一个目录存放工作流，设置环境变量 `OPENCOLLAB_WORKFLOWS_DIR`。写相对路径时，它仍按工作区来解析。

## 第 2 步：用命令行交给它一项任务

```bash
opencollab workflow run duo \
  --workspace /path/to/workspace \
  --task '修复配置读取错误并运行相关测试' \
  --args '{"allow_unisolated_shell":true}' \
  --budget 100000 --concurrency 2 --task-concurrency 2
```

各参数的意思：

| 参数 | 作用 |
| --- | --- |
| `--task` | 任务文字，会写进工作流输入的 `goal` 字段 |
| `--task-file task.md` | 从 UTF-8 文件读取任务，同样写进 `goal`，文件最大 4 MiB |
| `--args` | 其它输入，必须是一个 JSON 对象 |
| `--budget` | 本次运行最多用多少 token |
| `--concurrency` | 同时活跃的 Agent 会话上限 |
| `--task-concurrency` | `parallel` 和 `pipeline` 最多同时处理几项，默认等于 `--concurrency` |
| `--isolation` | 选择角色在什么环境里执行 |
| `--agent-profile` | 选择所有角色使用的 Agent 配置 |

几条使用规则：

- `--task` 和 `--task-file` 只能二选一。如果 `--args` 里已经有 `goal`，这两个也都不能再用。
- 模型、工作区、额度和并发这些选项，都写在 `workflow run NAME` 后面。
- 示例里的 `allow_unisolated_shell` 表示允许 Agent 直接用本机的 shell。只在你信任的本地工作区这样做。在容器里运行时，保持默认的隔离要求即可。
- 自己写的工作流可能不用 `goal`，而用 `task`、`items` 等字段。这时请把这些字段写在 `--args` 里。`--task` 只会填 `goal`，字段怎么用由工作流自己决定。

## 第 3 步（可选）：用 Python 启动

下面是完整的 Python 脚本。工作区目录要事先存在，模型配置由 `OpenCollab` 自己读取。

```python
import asyncio
from opencollab import OpenCollab

async def main():
    result = await OpenCollab('/path/to/workspace').workflow(
        'duo',                       # 工作流名称
        {'goal': '修复配置读取错误并验证', 'allow_unisolated_shell': True},  # 输入
        budget=100000,               # token 额度
        concurrency=2,               # 同时活跃的 Agent 会话数
        timeout=900,                 # 超时秒数
        artifacts='/path/to/new-run-artifacts',  # 产物目录，应为新目录或空目录
        limit_mode='explicit',       # 按这里显式写的额度执行
    )
    print(result.status, result.reason, result.tokens)
    print(result.agent_failures)
    print(result.raise_for_status().output)

asyncio.run(main())
```

补充说明：

- 第一个参数 `flow` 可以是名称，也可以是普通异步函数、加了装饰器的异步函数，或带有可调用 `fn` 的 `WorkflowSpec`。
- 第二个参数 `inputs` 是键为字符串的 Mapping，会转成字典交给工作流。
- `limit_mode='explicit'` 表示额度以你写的为准。默认的 `environment` 模式会读取环境里的“无限额度”开关。

## 第 4 步：看结果

Python 调用返回一个 `RunResult`，常用字段如下：

| 字段 | 含义 |
| --- | --- |
| `output` | 工作流函数返回的值 |
| `status` | `completed`（完成）、`stopped`（被停下）或 `failed`（失败） |
| `reason` | 为什么停下 |
| `agent_failures` | 过程中失败过的 Agent 调用 |
| `tokens` | 用掉的 token |

`raise_for_status()` 的行为：状态是完成时，返回结果本身；其它状态会抛出 `RunError`，异常里带着这份结果。

注意：工作流可能在某个 Agent 失败后，仍返回手头已有的数据。所以 `status` 是完成，不代表每一步都成功。请同时检查 `agent_failures`、交付的文件，并实际跑一遍测试。

命令行这边：

- 运行结束后会打印 `output` 的 JSON。
- 如果工作流有停止原因，命令返回非零退出码。

## 运行记录保存在哪里

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `--save` / `--no-save` | 保存 | 把本次运行目录存到 `.opencollab/sessions/`，里面有会话和工作流清单 |
| `--trace` / `--no-trace` | 记录 | 在保存目录写 `orchestration.jsonl`，可以对照每个阶段、日志和每步消耗 |

Python 里对应的是 `artifacts` 和 `trace` 参数。

这些记录适合事后调试。如果你想在另一个进程里接着上次运行继续做，需要的业务状态要由工作流作者自己保存。

## 常见问题

- 如果自己写的工作流没出现在列表里：确认文件放在 `workflows/` 顶层（或 `OPENCOLLAB_WORKFLOWS_DIR` 指定的目录），并且函数上有 `@workflow`。
- 如果报注册错误：可能是你的工作流和内置工作流重名。改一下装饰器里的 `name` 即可。
- 如果没写 `--budget`：命令行会把配置里的额度提高到至少 1,000,000。想控制本次规模，就显式写上 `--budget`。
- 如果调高了并发，速度却没变：并发只影响同时执行的部分。代码里一步接一步写的步骤，仍然逐个执行，顺序由 Python 代码决定。

## 源码依据

[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md#L61)、[opencollab/adapters/cli/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/workflow.py#L38)。

## 相关章节

[编写第一个自定义 Workflow](author-custom-workflow.md)、[让 Team 成员调用已注册的 Workflow](team-run-workflow.md)、[用 Duo 跑两个候选方案并选出一个](duo-workflow.md)、[用 Weave 分组接力完成任务并检查交付](weave-workflow.md)、[用 Reconstruct 从输入输出反推程序](reconstruct-workflow.md)、[读取运行结果与判断任务是否完成](../observability/run-results.md)。
