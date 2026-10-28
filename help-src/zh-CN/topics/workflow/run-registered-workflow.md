---
{
  "id": "run-registered-workflow",
  "title": "运行一个 Workflow",
  "category": "workflow",
  "summary": "用命令行或 Python 找到一个 Workflow，交给它一项任务，读懂结果，并知道运行记录保存在哪里。",
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
    "workflow-programming-model",
    "duo-workflow",
    "weave-workflow",
    "reconstruct-workflow",
    "team-run-workflow",
    "run-results"
  ],
  "verified_ref": "v0.9.6"
}
---

# 运行一个 Workflow

Workflow 是用 Python 代码编排 Agent 的方式：一个 `async` 函数决定什么时候开会话、怎样用返回值、哪些步骤并行。这篇只讲“用”：怎样找到一个现成的 Workflow，交给它一项任务，再读懂结果。怎样自己写，见 [编写 Workflow](workflow-programming-model.md)。

最小用法：命令行 `opencollab workflow run duo --workspace DIR --task '...'`，或者 Python 里 `await OpenCollab(DIR).workflow('duo', {'goal': '...'})`。

## 先选一个现成的 Workflow

OC 自带三个，可以直接用：

| 名称 | 适合什么任务 |
| --- | --- |
| [Duo](duo-workflow.md) | 想要两份候选方案，比较后选一个 |
| [Weave](weave-workflow.md) | 任务能拆成几组，分组执行，最后用检查命令判定交付 |
| [Reconstruct](reconstruct-workflow.md) | 手上有一个能查询的隐藏程序，想重建出行为相同的代码 |

这些都不合适、而你又清楚每一步该怎么做，就自己写一个。

## 第 1 步：列出可用的 Workflow

在配好模型的环境里运行（路径换成你自己的项目）：

```bash
# 列出内置的和工作区里自己写的 Workflow
opencollab workflow list --workspace /path/to/workspace
# 查看 duo 接受哪些参数
opencollab workflow run duo --help
```

列表里有两类：安装 OC 时自带的内置 Workflow，以及工作区 `workflows/` 目录里你自己写的 Workflow。OC 只加载 `workflows/` 顶层的 Python 文件，读取每个函数上 `@workflow` 写的名称和说明。想换个目录，设置环境变量 `OPENCOLLAB_WORKFLOWS_DIR`；写相对路径时，仍按工作区解析。

## 第 2 步：用命令行交给它一项任务

```bash
opencollab workflow run duo \
  --workspace /path/to/workspace \
  --task '修复配置读取错误并运行相关测试' \
  --args '{"allow_unisolated_shell":true}' \
  --budget 100000 --concurrency 2 --task-concurrency 2
```

| 参数 | 作用 |
| --- | --- |
| `--task` | 任务文字，写进 Workflow 输入的 `goal` 字段 |
| `--task-file task.md` | 从 UTF-8 文件读任务，同样写进 `goal`，最大 4 MiB |
| `--args` | 其他输入，必须是一个 JSON 对象 |
| `--budget` | 本次运行最多用多少 token |
| `--concurrency` | 同时活跃的 Agent 会话上限 |
| `--task-concurrency` | `parallel` 和 `pipeline` 最多同时处理几项，默认等于 `--concurrency` |
| `--isolation` | Agent 在什么环境里执行，见 [隔离](../isolation/isolation-overview.md) |
| `--agent-profile` | 所有角色使用的 Agent 配置 |
| `--run-id` | 沿用原运行的 `run_id`，用于继续之前的运行，例如 [Weave 从状态快照继续](weave-workflow.md#中断后继续) |

几条规则：

- `--task` 和 `--task-file` 二选一；`--args` 里已经有 `goal` 时，两个都不能再用。
- 模型、工作区、额度和并发这些选项，都写在 `workflow run NAME` 后面。
- `allow_unisolated_shell` 表示允许 Agent 直接用本机 shell，只在你信任的项目上这样做。在容器里运行时保持默认的隔离要求即可。
- 自己写的 Workflow 可能不用 `goal`，而用 `task`、`items` 等字段，这时把它们写进 `--args`。字段的含义由 Workflow 自己决定。

## 第 3 步（可选）：用 Python 启动

工作区目录要事先存在，模型配置由 `OpenCollab` 自己读取：

```python
import asyncio
from opencollab import OpenCollab

async def main():
    result = await OpenCollab('/path/to/workspace').workflow(
        'duo',                       # Workflow 名称
        {'goal': '修复配置读取错误并验证', 'allow_unisolated_shell': True},  # 输入
        budget=100000,               # token 额度
        concurrency=2,               # 同时活跃的 Agent 会话数
        timeout=900,                 # 期限（秒）
        artifacts='/path/to/new-run-artifacts',  # 产物目录，新目录或空目录
        limit_mode='explicit',       # 严格按这里写的额度执行
    )
    print(result.status, result.reason, result.tokens)
    print(result.agent_failures)
    print(result.raise_for_status().output)

asyncio.run(main())
```

- 第一个参数可以是名称，也可以是普通 `async` 函数、加了装饰器的函数，或带可调用 `fn` 的 `WorkflowSpec`。
- 第二个参数是键为字符串的 Mapping，会转成字典交给 Workflow。
- `limit_mode='explicit'` 表示额度以你写的为准。默认的 `environment` 模式会读取环境里的“无限额度”开关，见 [预算、步数与并发](../operations/token-step-concurrency-limits.md#无限制模式)。

## 第 4 步：读结果

| 字段 | 含义 |
| --- | --- |
| `output` | Workflow 函数的返回值 |
| `status` | `completed`、`stopped` 或 `failed` |
| `reason` | 为什么停下 |
| `agent_failures` | 过程中失败过的 Agent 调用 |
| `tokens` | 用掉的 token |

`raise_for_status()` 在完成时返回结果本身，其他状态抛出带着结果的 `RunError`。

注意：Workflow 可能在某个 Agent 失败后，仍返回手头已有的数据。所以 `status` 是 `completed` 不代表每一步都成功。请同时检查 `agent_failures`、交付的文件，并实际跑一遍测试。见 [判断任务是否真的完成](../observability/run-results.md)。

命令行运行结束后会打印 `output` 的 JSON；Workflow 有停止原因时，命令返回非零退出码。

## 运行记录保存在哪里

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `--save` / `--no-save` | 保存 | 把本次运行目录存到 `.opencollab/sessions/`，里面有各会话和 Workflow 清单 |
| `--trace` / `--no-trace` | 记录 | 在保存目录写 `orchestration.jsonl`，可以对照每个阶段、日志和每步消耗 |

Python 里对应 `artifacts` 和 `trace` 参数。这些记录用于事后调试；想在另一个进程里接着上次的进度做，需要的业务状态要由 Workflow 作者自己保存，见 [Workflow 的失败处理](workflow-failure-recovery-lifecycle.md#进程退出后能接着跑吗)。

## 常见问题

- **自己写的 Workflow 没出现在列表里**：确认文件放在 `workflows/` 顶层（或 `OPENCOLLAB_WORKFLOWS_DIR` 指定的目录），函数上有 `@workflow`。
- **报注册错误**：你的 Workflow 和已有的重名了，改一下装饰器里的 `name`。
- **没写 `--budget`**：命令行会把配置里的额度提高到至少 1,000,000。想控制规模，就显式写上。
- **调高了并发，速度却没变**：并发只影响同时执行的部分。代码里一步接一步写的步骤，仍然逐个执行。

## 延伸阅读

- 源码依据：[opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/README.md) · [opencollab/adapters/cli/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/adapters/cli/workflow.py)
- 相关指南：[编写 Workflow](workflow-programming-model.md) · [让 Team 成员运行 Workflow](team-run-workflow.md) · [判断任务是否真的完成](../observability/run-results.md)
