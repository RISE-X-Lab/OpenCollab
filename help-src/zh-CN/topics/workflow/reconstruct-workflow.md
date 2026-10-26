---
{
  "id": "reconstruct-workflow",
  "title": "Reconstruct：从输入输出反推一个程序",
  "category": "workflow",
  "summary": "Reconstruct 是内置 Workflow：反复向一个可查询的隐藏程序要输入输出样例，让 Agent 写出行为相同的 candidate.py。",
  "aliases": [
    "根据输入输出写程序",
    "程序反推",
    "黑盒复现",
    "主动探索",
    "oracle",
    "register_task",
    "candidate.py",
    "IO2CodeBench",
    "FromIO",
    "使用 Reconstruct 从输入输出重建程序"
  ],
  "keywords": [
    "Reconstruct",
    "黑盒程序重建",
    "ReconstructionTask",
    "register_task",
    "promotion",
    "coverage_gate",
    "stop_reason"
  ],
  "questions": [
    "Reconstruct 是做什么的？",
    "怎么把自己的 oracle 和候选执行器接进 Reconstruct？",
    "Reconstruct 什么时候停止？",
    "怎么用 CLI 运行 Reconstruct？",
    "Reconstruct 有哪些探索开关？"
  ],
  "related": [
    "run-registered-workflow",
    "workflow-agent-calls",
    "workflow-programming-model"
  ],
  "verified_ref": "0c47f74a"
}
---

# Reconstruct：从输入输出反推一个程序

Reconstruct 是 OC 内置的 Workflow，名称 `reconstruct`，用于黑盒程序重建：你手上有一个看不到源码、但能查询的隐藏程序，想写出一个行为相同的程序（类似 IO2CodeBench 这类“只给输入输出、写出程序”的任务）。

它的工作方式：你提供隐藏程序的查询函数（oracle），Workflow 逐批向它要输入输出样例，让编码 Agent 写一个行为相同的 `candidate.py`，在已知样例上反复验证，并补充能区分不同猜测的新输入。

一个关键的设计取舍：**oracle 只由 Workflow 调用，留在调用方进程里，Agent 看不到它**，只能看到公开的样例和反馈。所以你要提供三样东西：oracle、在受控环境里执行候选程序的执行器，以及独立的最终评分方式。

## 最小用法

### 用 SDK 绑定任务

下面的函数把应用已有的 oracle、候选执行器和检查工具接进 `ReconstructionTask`：

```python
from pathlib import Path
from opencollab import OpenCollab
from opencollab.builtin_workflows import ReconstructionTask, register_task
from opencollab.tools import builtin_tools

async def reconstruct_task(workspace, environment, oracle, evaluate, check_candidate):
    task = ReconstructionTask(
        input_type='list of integers',
        input_constraints='输入为整数列表，每个整数在 -100 到 100 之间。',
        function_name='solve',
        workspace=Path(workspace),
        oracle=oracle,
        evaluate=evaluate,
        tools=[
            *builtin_tools('bash', 'file_read', 'file_write', 'apply_patch'),
            check_candidate,
        ],
        kind='list', lo=-100, hi=100,
    )
    result = await OpenCollab(workspace, environment=environment).workflow(
        'reconstruct',
        {'task': register_task(task), 'max_iterations': 10, 'domain_check': True},
        budget=100000, max_steps=50, limit_mode='explicit',
    )
    return result.raise_for_status().output
```

| 你要准备的对象 | 要求 |
| --- | --- |
| `oracle(input)` | 返回隐藏程序的输出；输入无效时抛异常 |
| `evaluate(program_path, inputs)` | 异步函数。每个输入返回一条字典：成功为 `ok=True` 加 `output`，失败为 `ok=False` 加 `error`。应在同一个受控候选环境里执行生成的程序 |
| `environment` | 供 Agent 执行工具的环境对象 |
| `check_candidate` | 给编码角色用的实际检查工具 |

候选代码在 Agent 所在的受控环境里执行，oracle 留在调用方。`register_task` 把本进程里的这些活对象存起来，返回一个可序列化的一次性句柄，Workflow 解析时会消费掉它，所以每次运行都要重新注册。

### 或者用 CLI

CLI 用 `module:callable` 工厂的方式，用 `task_args` 里的关键字参数构造绑定对象：

```bash
opencollab workflow run reconstruct \
  --workspace /path/to/workspace \
  --args '{"task":"my_bench.binding:make_task","task_args":{"name":"example"},"max_iterations":10}' \
  --budget 100000
```

这要求环境里有 `my_bench.binding.make_task`，并返回 `ReconstructionTask`。工厂负责接好具体任务的查询器、执行环境和检查工具。

## 每一轮怎么推进

1. 获取初始样例，开启编码会话。
2. 候选写完后，在所有已知样例上执行。
3. 全部通过：记一次晋级（promotion），接着请求更多能区分不同猜测的输入。
4. 有失败：把实际失败反馈送回编码阶段，继续用已有样例。

输入提议必须满足数量、去重、输入约束，并且被 oracle 接受；提议失败时最多按 `propose_attempts` 次重试。

每轮候选都归档在 `.oc_candidates/`。结束时，按最终样例集上的阶段得分，从本次归档中选一个复制为 `candidate.py`。之前运行留在目录里的文件不算进本次候选集合。

## 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `initial_batch` | 2 | 第一批样例数量 |
| `batch_step` | 2 | 后续批次的增长量 |
| `max_iterations` | 50 | 编码迭代上限 |
| `early_stop` | 5 | 连续晋级达到这个次数后结束 |
| `propose_attempts` | 3 | 一批输入提议的尝试上限 |
| `examples_shown` | 12 | 输入提议角色看到的最近样例数 |
| `random_share` | 0.0 | 从独立随机源取得的样例比例 |
| `coverage_gate` | false | 让覆盖类别参与晋级判断 |
| `memorisation_gate` | false | 识别把已知输入直接写进代码的做法 |
| `complexity_gate` | false | 让高复杂度惩罚影响晋级 |
| `domain_check` | false | 检查提议的数值范围 |

- 前六个计数参数是正整数，`random_share` 在 0 到 1 之间，开关在 JSON 里用布尔值。
- `examples_shown` 只缩小输入提议角色看到的样例窗口；编码角色和候选验证仍用全部已知样例。
- 设了 `random_share` 时，还要提供 `random_inputs(k, exclude)`，返回独立生成、尚未用过的输入。`exclude` 是输入的字符串表示，生成器可以先过滤已有值，Workflow 还会再校验新输入并排除等价的重复。

### 描述输入域

`kind` 可选 `list`、`two_lists`、`int`、`pair`、`string`、`points` 或 `other`，配合 `lo`、`hi` 和 `is_float` 描述输入域。

### 四个探索开关

| 开关 | 开启后的行为 |
| --- | --- |
| `coverage_gate` | 按输入种类检查空值、长度、正负数、重复值等结构类别。同一个缺失类别连续挡住三批后，允许继续 |
| `memorisation_gate` | 候选里嵌入了至少三个已知输入时，先用同一批证据要求重写，最多等两轮再取新样例 |
| `complexity_gate` | 复杂度惩罚至少 0.8 的候选会继续补样例，前三次暂不计入晋级 |
| `domain_check` | 按 `lo` 和 `hi` 检查提议里的数值 |

`kind='other'` 时跳过结构覆盖和数值域检查。这些开关只影响探索和提前停止，最终评分仍用应用自己的独立样例。

## 什么时候停，返回什么

预算判断放在当前候选执行和归档之后，这样最后一轮的工作能保留下来。

| `stop_reason` | 含义 |
| --- | --- |
| `early_stop` | 连续晋级次数达到 `early_stop` |
| `max_iterations` | 达到编码迭代上限 |
| `proposer_failed` | 输入提议失败 |
| `budget_exhausted` | 预算用完 |

输出还包含：迭代次数、晋级次数、已知样例、逐轮事件、`final_program` 和 `final_score`。注意 `final_score` 是**阶段得分**，根据已知样例和协议惩罚计算；用独立的隐藏测试评分，要由调用方在运行结束后自己做。

## 定制绑定和样例记录

`ReconstructionTask` 还有几个可选字段：

| 字段 | 作用 |
| --- | --- |
| `candidate_file` | 把默认的 `candidate.py` 改成任务需要的相对路径 |
| `outputs_match(actual, expected)` | 自定义输出相等规则，适合业务确实允许浮点误差或等价结构的任务 |
| `on_example(input, output, source)` | 收到每条实际被接纳的样例和来源，可以接到应用日志 |

```python
observed = []

def record_example(input_value, output_value, source):
    observed.append({'input': input_value, 'output': output_value, 'source': source})

def numeric_match(actual, expected):
    return abs(actual - expected) <= 1e-8
```

数值标量任务可以在构造绑定时传入 `outputs_match=numeric_match, on_example=record_example`；列表或嵌套结构要用符合自身数据形状的比较函数。

## 延伸阅读

- 源码依据：[docs/reconstruct.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/reconstruct.md) · [opencollab/builtin_workflows/reconstruct.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/builtin_workflows/reconstruct.py) · [opencollab/builtin_workflows/__init__.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/builtin_workflows/__init__.py)
- 相关指南：[运行一个 Workflow](run-registered-workflow.md) · [在 Workflow 里调用 Agent](workflow-agent-calls.md#哪些数据能跨进程) · [编写 Workflow](workflow-programming-model.md)
