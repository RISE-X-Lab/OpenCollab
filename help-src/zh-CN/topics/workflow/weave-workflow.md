---
{
  "id": "weave-workflow",
  "title": "Weave：分组接力完成任务，用检查命令判定交付",
  "category": "workflow",
  "summary": "Weave 是内置 Workflow：把任务拆成有依赖的组，每组开一个新会话在同一工作区接力，用真实检查命令判定交付，失败时做有限轮修复，还能从状态快照继续。",
  "aliases": [
    "有序会话",
    "新会话接力",
    "分组协作",
    "groups",
    "check_commands",
    "delivery_ok",
    "WeaveAdapter",
    "run_weave",
    "继续运行",
    "使用 Weave 分组执行并检查交付"
  ],
  "keywords": [
    "Weave",
    "工作组",
    "交付检查",
    "WeaveConfig",
    "WeaveGroup",
    "WeaveState",
    "max_repair_rounds",
    "save_state"
  ],
  "questions": [
    "Weave 是什么，怎么运行？",
    "Weave 的组之间怎么传递成果？",
    "delivery_ok 是怎么判定的？",
    "Weave 的修复轮数和预算怎么配置？",
    "Weave 中断后怎么从检查点继续？"
  ],
  "related": [
    "run-registered-workflow",
    "workflow-failure-recovery-lifecycle",
    "run-results",
    "duo-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# Weave：分组接力完成任务，用检查命令判定交付

Weave 是 OC 内置的 Workflow，名称 `weave`。它把任务拆成有先后依赖的组（group），每组开一个全新的 Agent 会话，在同一个工作区里接着干：后面的组能看到前面组改过的文件，但不继承会话历史。每组按权重分到一份 token、步数和时间。

Weave 的关键设计是：**交付成功与否不看模型自己的报告，而看你给的检查命令实际执行的结果**。检查失败时，它会做有限轮修复。适合任务能拆成几段、并且有可执行检查的场景。

## 最小可运行例子

下面的脚本先写好一个检查文件，再让 Weave 分两组创建 `greeting.py`。把工作区换成你自己的路径即可，工作区由脚本创建。

```python
import asyncio
import shlex
import sys
from pathlib import Path
from opencollab import OpenCollab

async def main():
    workspace = Path('/tmp/weave-help-example')
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / 'check_greeting.py').write_text(
        "from greeting import greet\n"
        "assert greet('Ada') == 'Hello, Ada!'\n"
        "assert greet('Lin') == 'Hello, Lin!'\n"
        "print('GREETING_CHECK_PASSED')\n",
        encoding='utf-8',
    )
    result = await OpenCollab(workspace).workflow(
        'weave',
        {
            'groups': [
                {
                    'id': 'function',
                    'prompt': "创建 greeting.py，greet(name) 返回 'Hello, ' 加名字加 '!'。",
                    'resources': ['greeting.py'],
                },
                {
                    'id': 'command',
                    'prompt': '加入命令入口，python greeting.py Ada 输出 Hello, Ada!。',
                    'dependencies': ['function'],
                    'resources': ['greeting.py'],
                },
            ],
            'check_commands': [
                {
                    'command': f'{shlex.quote(sys.executable)} check_greeting.py',
                    'expected_output': 'GREETING_CHECK_PASSED',
                },
                {
                    'command': f'{shlex.quote(sys.executable)} greeting.py Ada',
                    'expected_output': 'Hello, Ada!',
                },
            ],
            'config': {'max_repair_rounds': 2},
        },
        budget=30000, limit_mode='explicit', agent_profile='single2',
    )
    result.raise_for_status()
    assert result.output['delivery_ok'], result.output
    print(result.output)

asyncio.run(main())
```

- 第二组的 `dependencies` 写了 `function`，所以排在第一组之后。
- 两条检查命令都通过，`delivery_ok` 才为真。
- 检查命令要用装好了任务运行依赖的解释器，示例用的是 `sys.executable`。

用 CLI 运行：把同样的对象写进 `flow.json`，用 `--args "$(cat flow.json)"` 传入。检查命令在 `--workspace` 指定的目录下执行。

## 定义组

| 字段 | 必填 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `id` | 是 | 无 | 组的唯一标识 |
| `prompt` | 是，非空 | 无 | 这一组的任务说明 |
| `weight` | 否 | 1 | 分配份额时的权重 |
| `dependencies` | 否 | 无 | 前置组 id 或 target id |
| `resources` | 否 | 无 | 会被影响的共享文件或其他资源 |
| `targets` | 否 | 组 id | 检查和修复时用来定位 |

`plan_weave_groups` 按依赖安排组的顺序。组之间有依赖环时，环里的组会合并到同一个会话里，依赖改用 target id 表示。几处改动相互关联、需要一起考虑时，可以用这些字段表达关系。Workflow 按规划好的顺序运行各组，新会话从工作区读取前面组的成果。

## 检查与修复

### 检查命令怎么写

`check_commands` 的每一项可以是：

- 一个测试命令字符串。原生能识别 pytest、Go 和 Django 的测试结果。
- 一个含 `command` 和 `expected_output` 的对象。按输出子串匹配，同时要求退出码为零、执行完成、输出完整捕获。建议像示例那样把断言放在成功标记之前打印。

以下情况结果是“未验证”，不算通过：检查为空、命令只显示了帮助、只收集了测试没有执行、普通命令缺少可解析的测试证据。Weave 要求检查同时满足 `ok` 和 `executed`，`delivery_ok` 反映最终执行的检查是否通过。

默认的命令适配器在每次组检查和最终检查时，都会执行列出的全部命令。自定义适配器可以用传入的 `targets` 缩小验证范围。

### 修复轮数

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `max_repair_rounds` | 3 | 最多修复几轮。设为 0 时跳过修复，但保留最终检查 |
| `stagnant_round_limit` | 2 | 连续几轮没有可观察的进展就停止 |

检查返回的 `repair_targets` 指明接下来修哪里，`progress_markers` 表示可观察到的进展。共享额度、时间和实际授权也会限制还能继续多少工作。

## 预算

- SDK 的 `budget` 是真正的硬上限。
- `config.main_budget` 设一个初始软额度。有已完成的编辑和通过的检查作为证据时，软额度可以在授权范围内扩展。
- `config.output_reserve_tokens` 为模型响应预留决策空间；单次模型输出上限在客户端配置的 `max_output_tokens` 里设。
- `config.max_steps` 是主阶段各组累计的步数分配；SDK 的 `max_steps` 是各角色继承的默认步数。两者范围不同，分别设置。

## 完整配置

CLI 的 JSON `config` 字段和 Python 的 `WeaveConfig(...)` 字段相同。可选上限默认是 `None`，运行时会结合 SDK 剩余额度和期限算出实际份额。建议先定总预算和完成期限，再按任务耗时给检查和修复留出空间。

```json
{
  "config": {
    "main_budget": 18000,
    "repair_reserve": 4000,
    "max_steps": 30,
    "wall_seconds": 600,
    "group_check_timeout": 60,
    "max_repair_rounds": 2,
    "repair_steps": 12,
    "repair_timeout": 120,
    "final_check_timeout": 60
  }
}
```

| 配置字段 | 默认值与用途 |
| --- | --- |
| `budget` | 可选，限制 Weave 累计 token 消耗，仍受 SDK 授权限制 |
| `main_budget`、`main_hard_budget` | 可选，主阶段初始软额度和硬额度，按剩余组的权重分配 |
| `repair_reserve` | 0，为修复保留 token，有限总额下最多保留总额的四分之一 |
| `max_steps`、`repair_steps` | 可选，主阶段各组累计步数和每轮修复步数上限 |
| `minimum_group_tokens`、`minimum_group_steps` | 均为 1，开始下一组所需的最小份额 |
| `wall_seconds`、`agent_seconds` | 可选，累计运行时间上限和主阶段会话分配窗口 |
| `minimum_group_seconds`、`minimum_session_seconds` | 均为 0，组启动所需时间和会话最小分配时间 |
| `main_agent_time_reserve`、`main_wall_time_reserve` | 均为 0，计算主阶段窗口时分别预留的时间 |
| `group_check_fraction` | 0.25，从组窗口中预留给检查的比例 |
| `group_check_min_seconds`、`group_check_max_seconds` | 默认 0 与 `None`，预留的组检查时间范围 |
| `group_check_timeout`、`group_check_grace_seconds` | 默认 `None` 与 0，限制检查时长，并允许使用组窗口之后的额外时间 |
| `cleanup_seconds`、`tool_cleanup_seconds` | 默认 2 与 `None`，会话清理等待和单个工具取消清理 |
| `output_reserve_tokens`、`history_trigger_tokens` | 默认 1 与 `None`，软额度决策的输出预留和会话历史整理阈值 |
| `extension_tokens` | 1，观察到新进展时软额度至少增加的 token 数 |
| `extension_margin_fraction`、`extension_margin_tokens` | 默认 0.1 与 `None`，接近软额度时采用的余量，显式 token 值限制比例计算结果 |
| `budget_final_prompt`、`system_prompt` | 可选，额度即将用完时的提示和角色系统提示 |
| `max_repair_rounds`、`stagnant_round_limit` | 默认 3 与 2，修复轮数及连续无进展的轮数上限 |
| `repair_budget`、`repair_budget_fraction` | 默认 `None` 与 0.65，单轮修复额度上限和中间轮使用的剩余额度比例，最后一轮可用剩余全部额度 |
| `repair_time_reserve`、`repair_agent_time_reserve`、`minimum_repair_seconds` | 均为 0，修复可用时间、修复会话时间预留和启动条件 |
| `repair_timeout`、`final_check_timeout`、`final_check_time_reserve` | 默认 `None`、`None` 与 0，单轮修复时长及最终检查的时长和预留 |
| `heartbeat_seconds` | 60，活跃会话期间保存状态的间隔 |

取值规则：次数和 token 字段是整数；比例字段取大于 0 且不超过 1 的值；时间单位是秒；允许为零的预留和最小时间字段，设为 0 就关闭对应预留。

## 读取交付结果

| 字段 | 含义 |
| --- | --- |
| `status` | `completed`、`failed` 或 `unverified`，由最终执行的检查决定 |
| `delivery_ok` | 最终执行的检查是否通过 |
| `groups` | 每组的生成和检查结果 |
| `verification` | 最终检查报告 |
| `repair_rounds` | 每轮修复的结果和进展 |
| `main_stop_reason`、`repair_stop_reason`、`unstarted_ids` | 资源在哪里用尽，还有哪些目标没开始 |
| `total_tokens`、`elapsed_seconds` | 累计值，包含从状态快照继续之前的记录 |
| `state` | 可用于下一次恢复 |

## 接入自己的检查

如果你有自己的验证服务，在包装 Workflow 里调用 `run_weave`，传入 `WeaveGroup`、`WeaveConfig` 和自定义的 `WeaveAdapter`。覆盖 `verify(ctx, targets, seconds)` 并返回 `WeaveCheck`（是否执行、是否通过、报告、修复对象和进展标记），就能把应用的真实行为检查接进分组流程。

适配器还有这些可覆盖的钩子：

| 钩子 | 用途 |
| --- | --- |
| `save_state(data)` | 收到独立的 JSON 状态快照，可自行持久化 |
| `on_update(kind, state, data)`、`on_event(phase, event)` | 同步回调，适合保存短记录 |
| `source_snapshot()`、`progress()` | 用自定义工具时，报告文件变化和通过的检查；Weave 据此重新检查受影响的组并扩展软额度 |
| `group_prompt`、`repair_prompt`、`improved` | 调整任务说明、修复说明和进展比较方式 |
| `affects_prior_groups` | 声明某些变化需要扩大对先前组的检查 |

## 中断后继续

Weave 能从显式的状态快照继续，前提是：同一个工作区、相同的组定义、复用原来的 `run_id`。继续的方式有两种：把快照作为 JSON 输入 `state` 传入；或者构造 `WeaveState(saved_snapshot)` 传给 `run_weave`，并在 SDK 里复用保存的 `run_id`。

继续时：已经生成的组从检查那一步继续；已经检查过的组保留生成结果；已用掉的修复轮数继续累计。

下面的包装流程沿用原生命令检查，把状态写到调用方指定的宿主文件。`launch` 接受和前面示例相同的 `groups` 与 `check_commands`；再次传入同一个检查点时，会恢复原来的 `run_id`。

```python
import json
from pathlib import Path
from opencollab import OpenCollab
from opencollab.builtin_workflows import (
    WeaveAdapter, WeaveConfig, WeaveGroup, WeaveState, run_weave,
)

class SavedChecks(WeaveAdapter):
    def __init__(self, checkpoint, commands):
        super().__init__(check_commands=commands)
        self.checkpoint = Path(checkpoint)

    def save_state(self, data):
        self.checkpoint.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

async def saved_weave(ctx, args):
    return await run_weave(
        ctx,
        [WeaveGroup(**group) for group in args['groups']],
        config=WeaveConfig(max_repair_rounds=2),
        adapter=SavedChecks(args['checkpoint'], args['check_commands']),
        state=WeaveState(args['state'], run_id=ctx.run_id),
    )

async def launch(workspace, checkpoint, groups, check_commands):
    checkpoint = Path(checkpoint).resolve()
    saved = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
    result = await OpenCollab(workspace).workflow(
        saved_weave,
        {'groups': groups, 'check_commands': check_commands,
         'checkpoint': str(checkpoint), 'state': saved},
        run_id=saved.get('run_id'), budget=30000, limit_mode='explicit',
    )
    return result.raise_for_status().output
```

检查点的父目录要事先存在。恢复时会用原工作区里已经生成的文件。

## 延伸阅读

- 源码依据：[docs/weave.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/weave.md) · [WeaveConfig 与 WeaveAdapter](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/builtin_workflows/weave.py) · [命令检查实现](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/builtin_workflows/_weave_execution.py)
- 相关指南：[运行一个 Workflow](run-registered-workflow.md) · [失败处理、测试与调试](workflow-failure-recovery-lifecycle.md) · [判断任务是否真的完成](../observability/run-results.md) · [Duo](duo-workflow.md)
