---
{
  "id": "workflow-dataflow-structured-io",
  "title": "Workflow 里的数据怎么传：输入、步骤结果、文件与输出",
  "category": "workflow",
  "summary": "说明 args 从哪来、上一步结果怎样交给下一步、Agent 怎样返回 JSON，以及最终输出放在哪。",
  "aliases": [
    "args 和 inputs 有什么区别",
    "上一步结果怎么交给下一步",
    "Agent 怎么返回 JSON",
    "文件和返回值怎么传",
    "怎么检查工作流最终结果",
    "步骤间传值",
    "Agent 返回字典"
  ],
  "keywords": [
    "Workflow 输入输出",
    "数据流",
    "schema",
    "RunResult"
  ],
  "questions": [
    "args 和 inputs 有什么区别？",
    "上一步的结果怎么交给下一步？",
    "怎样让 Agent 返回 JSON 字典？",
    "schema 校验失败会怎样？",
    "Agent 写的文件和函数返回值分别怎么拿到？",
    "哪些数据能跨进程传递？"
  ],
  "related": [
    "workflow-programming-model",
    "workflow-agent-calls",
    "compose-workflow-candidates",
    "reconstruct-workflow"
  ],
  "verified_ref": "0c47f74a"
}
---

# Workflow 里的数据怎么传：输入、步骤结果、文件与输出

OC Workflow 里有四类数据，各走各的路：

| 数据 | 怎么传 |
| --- | --- |
| 运行输入 | SDK 的 `inputs` 或 CLI 的 `--args`，变成函数的 `args` 字典 |
| 步骤之间的结果 | 普通 Python 变量，需要时显式拼进下一次提示词 |
| 真实产物 | 工作区文件，由 Agent 用工具写入 |
| 最终输出 | 函数 `return` 的值，成为 `RunResult.output` |

关键的 OC 默认行为：每次 `ctx.agent` 都是新会话，不共享对话历史，所以下一步需要的信息必须由你显式传递。本篇给出一个带结构化输出的完整例子，再说明文件、候选和跨进程数据的规则。

## 最小示例：结构化结果传给下一步

下面的异步函数可以直接传给 SDK。它先检查输入，再用 schema 拿到一个字典，然后把字典作为下一次提示词的一部分。

```python
import json

async def classify_and_describe(ctx, args):
    # 工作流整体输入由入口自己校验
    text = args.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be non-empty text')
    # schema 只约束这一次 Agent 输出的结构
    schema = {
        'type': 'object',
        'properties': {
            'category': {'type': 'string', 'enum': ['question', 'statement']},
            'summary': {'type': 'string'},
        },
        'required': ['category', 'summary'],
        'additionalProperties': False,
    }
    item = await ctx.agent(
        f'归类并概括这段文字\n{text}',
        schema=schema, tools=[], label='classify',
    )
    # 纠正一次后仍不满足 schema 时返回 None
    if item is None:
        return {'item': None, 'description': None}
    # 新会话看不到上一次对话，显式把结果传进去
    description = await ctx.agent(
        '解释以下分类结果\n' + json.dumps(item, ensure_ascii=False),
        tools=[], label='describe',
    )
    return {'item': item, 'description': description}
```

## 运行输入：`inputs` 与 `args`

- SDK 的 `workflow(flow, inputs)` 接受键为字符串的 Mapping。运行时把它转成普通字典，再调用 `flow(ctx, args)`。所以 `inputs` 和 `args` 是同一份数据，一个在调用方，一个在函数里。
- CLI 的 `--args` 提供同一个字典。`--task` 和 `--task-file` 只填 `goal` 字段。
- 字段名和含义由工作流作者定义。
- 输入可以用普通 Python 检查，也可以用应用已有的数据模型校验。

## 结构化输出：`ctx.agent(schema=...)`

OC 的实现方式：

1. 要求角色调用一个结构化输出工具。
2. 工具校验提交的对象，成功就返回字典。
3. 第一次没拿到符合 schema 的对象时，运行时带着已有历史再纠正一次。
4. 仍不满足就返回 `None`。所以业务代码要检查空值。

JSON Schema 能约束字段类型、必填字段、枚举和是否允许多余字段。它只校验结构，字段内容对不对，还要靠提示词、工具结果和业务验证一起确认。

设计建议：

- 要根据结果决定下一步时，用明确的字段，例如布尔值、枚举或对象列表，再用 Python 分支读取。
- 自由文本适合报告、摘要和下一步提示。如果要把文本当成命令、路径或状态使用，应用要先按用途解析它。
- 下一个会话需要的文字，要么显式传入，要么让它用工具读取指定文件。

## 文件与改动

Agent 通过 `file_write`、`apply_patch`、`bash` 等工具操作工作区。

- 想确认模型说写了什么、写了没有，就用工具或应用检查去读实际文件。
- `ctx.diff()` 读取可观察到的差异，`ctx.source_changed()` 读取变化探测。探测不可用时返回 `None`，这表示“未知”，应保留这个状态。

路径注意事项：

- 在 Python 入口里直接用 `Path.write_text`，操作发生在当前编排进程。
- Docker 会话里的工具使用容器内路径。两边路径的对应关系由环境决定。
- 想在绑定环境里操作文件，就用对应的工具或环境接口，这样还能留下执行记录。

## 候选结果

候选结果用 `CandidateRun` 表示：

- `output`：子会话或子函数的返回值。
- `diff`：捕获到的文件变化。

调用 `adopt_candidate` 之后，选中的变化才会进入源工作区。如果要交付运行中的服务或整个环境，由候选后端决定对应的交付方式。

## 最终输出

入口函数 `return` 的对象成为 `RunResult.output`。`status`、`reason`、`tokens` 和 `agent_failures` 记录执行情况。

工作流可以在局部失败后返回部分数据。所以调用方要同时看业务字段、失败记录和实际交付检查。

## 进阶：哪些数据能跨进程

- 开启产物保存时，输入必须能序列化成 JSON。
- 经过 `manage_workflow` 子进程的输入和输出，也必须可序列化。适合传路径、业务 id、数字和普通集合。
- 活的工具对象、数据库连接或函数，留在所属进程里，由入口通过依赖连接取得。

Reconstruct 的 `register_task` 是一个例子：

- 活的 oracle、执行器和工具保存在当前进程，输入里只传一次性的句柄。
- CLI 则用可导入的工厂名称加 `task_args` 重建这些对象。

如果要在另一个进程里恢复业务，应保存两样东西：能重新建立依赖的输入，和已完成步骤的结果。

## 源码依据

[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L408)、[opencollab/application/workflow.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/workflow.py#L503)、[opencollab/sdk/result.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/result.py#L18)、[docs/reconstruct.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/reconstruct.md#L76)。

## 相关章节

[工作流就是普通 Python：编程模型入门](workflow-programming-model.md)、[在 Workflow 中调用 Agent：ctx.agent 与 ctx.agent_run](workflow-agent-calls.md)、[复用子 Workflow，并在候选环境里试改动](compose-workflow-candidates.md)、[用 Reconstruct 从输入输出反推程序](reconstruct-workflow.md)。
