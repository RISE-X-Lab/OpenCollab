---
{
  "id": "base-single2",
  "title": "使用 Base 单 Agent 与 Single2 配置",
  "category": "setup",
  "summary": "在 Python 里让一个 Agent 独立完成任务，并按需要给它读、写或执行命令的工具。",
  "aliases": [
    "单智能体",
    "single agent",
    "agent2",
    "一个人做任务",
    "BASE_PROFILE",
    "resolve_profile_name",
    "单个 Agent"
  ],
  "keywords": [
    "Base",
    "Single2",
    "单Agent",
    "profile",
    "tools=\"read\"",
    "tools=\"coding\""
  ],
  "questions": [
    "直接运行一个 Agent，理解 Base 默认配置并按任务选择工具和执行环境？",
    "怎么用 Python 跑一个 Agent？",
    "Base 和 Single2 是什么关系？",
    "怎么让 Agent 在本机直接执行命令？"
  ],
  "related": [
    "python-sdk-entrypoints",
    "tool-selection",
    "isolation-overview",
    "run-results"
  ],
  "verified_ref": "0c47f74a"
}
---

# 使用 Base 单 Agent 与 Single2 配置

`OpenCollab.agent()` 在 SDK 里运行一个单 Agent，不涉及团队调度。它用的是 Base profile。OC 里 profile 指一套预设的提示词、默认工具和限额；Base 是默认别名，当前指向 Single2。下面先跑一次只读任务，再放开写文件和执行命令。

和常见 agent 框架不同的一点：默认的 `bash` 工具要求执行环境有进程隔离（例如 Docker），本地直接跑需要显式改配置，见第二步。

## 第一步：跑一次只读任务

前提：已经按安装指南装好 OpenCollab，并配置好模型。

1. 把下面的脚本保存到你想让 Agent 阅读的项目根目录。
2. 在装了 OpenCollab 的 Python 环境里运行它。

```python
import asyncio
from opencollab import OpenCollab

async def main() -> None:
    # 把当前目录作为工作区；模型设置沿用构造客户端时读到的配置
    client = OpenCollab(".")
    result = await client.agent(
        "阅读项目入口和测试配置，说明主要模块及建议执行的测试命令。",
        tools="read",          # 只读工具：file_read、grep、git_diff
        budget=100_000,        # token 预算
        max_steps=40,          # 最多执行 40 步
        artifacts="artifacts/first-agent",  # 运行记录保存位置，必须是新目录或空目录
    )
    print(result.status, result.reason)
    print(result.output)
    print(result.metrics.get("agent_profile"))  # 实际用到的 profile

asyncio.run(main())
```

`tools="read"` 只给 Agent `file_read`、`grep` 和 `git_diff` 三个工具。它只能看、不能改，适合第一次先了解项目结构。

程序会打印状态、回答和实际用到的 profile。按状态这样判断：

- 如果是 `completed`：执行正常结束。注意这不代表测试通过，测试结果要看实际命令的输出。
- 如果是 `stopped`：停止原因写在 `reason` 里。可以据此调大步数、token 预算或期限。
- 如果是 `failed`：查看错误信息和已经生成的产物。

`artifacts` 目录必须是新目录或空目录。每次运行请换一个新目录。

## 第二步：让 Agent 改代码、跑命令

默认的 `tools="coding"` 按顺序提供这些工具：`bash`、`file_read`、`file_write`、`apply_patch`、`git_diff` 和 `grep`。

默认的 `bash` 工具要求执行环境能做操作系统级的进程隔离，例如 Docker。在普通本地环境里，可以用文件读写工具，但默认的 `bash` 跑不起来。

如果你确实想让 Agent 直接在本机项目里执行命令，按下面的方式自己构造工具。这段代码要放在一个已有的 `async` 函数里，`client` 沿用前面创建的客户端。

```python
from opencollab.tools import builtin_tools, profile_tool_limits

tools = builtin_tools(
    "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
    headless=False,                      # 允许使用本机 shell
    limits=profile_tool_limits("base"),  # 沿用 Base 的工具输出限额
)
result = await client.agent2(
    "修复目标测试，运行仓库规定的验证命令，并说明改动。",
    tools=tools,
    budget=150_000,
    max_steps=80,
)
```

`headless=False` 让这些工具使用本机 shell。命令会直接影响你的电脑，请确认任务内容可信。

两件事别混在一起：Git 工作树只负责把文件改动分开；进程隔离要靠执行环境提供。怎么选，见隔离指南。

## Base、Single2 和 `agent2()` 的关系

- `agent()`：使用 Base。当前 Base 映射到 Single2。
- `agent2()`：明确选择 Single2。
- 也可以给 `agent()` 传 `profile="single2"`，效果是明确选择 Single2。

这个映射从 OpenCollab 0.8 开始采用。查看更旧的版本时，请读对应标签下的文档。

想确认你装的版本里 Base 指向什么，可以用公开的 `opencollab.profiles` 模块。下面的程序会打印三行，分别是 `single2`、`single2` 和 `single2`。

```python
from opencollab.profiles import BASE_PROFILE, resolve_profile_name

print(BASE_PROFILE)
print(resolve_profile_name("base"))
print(resolve_profile_name("single2"))
```

`None`、`"base"`、`"default"` 和 `"single"` 都会解析成当前 Base 指向的那个 profile。怎么选：

- 如果你希望程序跟着框架的默认实现一起升级：用这些别名。
- 如果你的集成必须固定用 Single2：传 `profile="single2"`，并在运行记录里保存所用的 OpenCollab 版本。

## 进阶：默认限额

- Single2 默认最多运行 200 步。
- Bash 的每个输出流（标准输出、标准错误）最多保留 10,000 字符。
- 较大的工具结果，在送给模型的上下文里只保留开头和结尾，总字符上限是 16,000。

## 进阶：调整提示词和执行额度

| 参数 | 作用 |
| --- | --- |
| `system_prompt=` | 替换这次调用的默认系统提示。 |
| `budget` | 限制整个会话累计的输入加输出 token。 |
| `max_steps` | 限制执行步数。 |
| `max_output_tokens` | 限制模型单次输出的长度。 |
| `steps` | 旧参数，是 `max_steps` 的别名。同一次调用里同时传它和 `max_steps` 会报错。 |

另外几点：

- 你显式传入的工具对象会保留它自己的配置。
- 模型、采样参数和请求超时由客户端设置，不在这里改。
- 如果设置了 `OPENCOLLAB_UNBOUNDED_LIMITS=true`：Base 和 Single2 仍然遵守你在调用里显式给出的正数预算和步数；没给的限制就不设上限。

要区分"正常完成""额度用完停下"和"失败"，看 `result.status`、`result.reason` 和保存下来的记录。

## 下一步

- 想让几个成员自己决定交接：去看 Team。
- 想用 Python 明确控制每个阶段：去看 Workflow。

## 来源

[docs/single2.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/single2.md)　[opencollab/profiles.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/profiles.py)　[opencollab/bootstrap/agent_profiles.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/agent_profiles.py)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py)

## 相关指南

[从 Python 调用 OpenCollab](python-sdk-entrypoints.md)　[选择 Agent 能使用的工具](../extensions/tool-selection.md)　[隔离怎么选](../isolation/isolation-overview.md)　[读取运行结果与判断任务是否完成](../observability/run-results.md)
