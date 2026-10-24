---
{
  "id": "custom-tools",
  "title": "把自己的函数接成工具",
  "category": "extensions",
  "summary": "实现 opencollab.tools.Tool 结构接口，把业务函数变成 Agent 或 Workflow 可调用的工具，并通过 runtime 在 Agent 所在环境里读写文件和跑命令。",
  "aliases": [
    "Tool",
    "execute_with_runtime",
    "JSON Schema",
    "接入自己的API",
    "自定义 tool",
    "runtime.environment"
  ],
  "keywords": [
    "自定义工具",
    "工具扩展"
  ],
  "questions": [
    "通过公开 Tool 接口加入业务能力并让 Agent 在正确环境中调用？",
    "怎样写一个自定义工具给 Agent 用？",
    "自定义工具里为什么不能直接用 open() 或 subprocess？",
    "工具的超时怎么设置？"
  ],
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 把自己的函数接成工具

OC 的自定义工具是一个满足 `opencollab.tools.Tool` 结构接口的普通类，不需要继承基类或注册装饰器（鸭子类型即可）。它提供名称、JSON Schema 参数和一个异步方法 `execute_with_runtime(params, runtime)`。和常见框架的主要区别在 `runtime` 参数：它带着当前 Agent 的执行环境，工具通过它读写文件、跑命令，操作才会落在 Agent 实际所在的工作树或容器里。最小用法是把实例放进 `tools=[...]`。

## 最小示例：只读工具

下面的工具按语言返回固定的测试命令，存成普通 Python 模块即可。

```python
class ProjectPolicy:
    name = "project_policy"                    # 工具名，模型用它调用
    description = "查询项目建议使用的测试命令。"
    parameters = {                             # JSON Schema，OC 分派时据此校验参数
        "type": "object",
        "properties": {"language": {"type": "string", "enum": ["python", "javascript"]}},
        "required": ["language"],
        "additionalProperties": False,
    }
    default_timeout = 30.0                     # 默认执行时限（秒）
    disable_outer_timeout = False              # False：由运行时负责外层超时、取消与清理

    def to_openai_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute_with_runtime(self, params, runtime):
        commands = {"python": "python -m pytest", "javascript": "npm test"}
        return commands[params["language"]]
```

使用方式：

- 在 SDK 里：在已有的异步函数中调用 `await oc.agent("找出测试命令", tools=[ProjectPolicy()])`。
- 在 Workflow 里：把同一个工具实例传给 `ctx.agent`。

模型提交的参数先按 `parameters` 校验，执行结果以文字返回会话。

## 接口字段一览

| 成员 | 说明 |
| --- | --- |
| `name` | 稳定、唯一，方便在日志里识别 |
| `description` | 说明什么时候用、会返回什么 |
| `parameters` | JSON Schema。尽量用类型、必填字段和枚举表达约束 |
| `default_timeout` | 默认执行秒数 |
| `disable_outer_timeout` | 是否由工具自己管理外层期限。普通短工具用 `False` |
| `to_openai_schema()` | 返回发给模型的函数定义 |
| `execute_with_runtime(params, runtime)` | 异步执行，返回字符串 |

返回值用字符串。业务对象可以先用 `json.dumps` 序列化。

## 操作文件或命令时用 runtime

纯内存的查询可以不碰 `runtime`。但只要涉及文件或命令，就要用 `runtime.environment`。

原因：直接用宿主的 `open()` 或 `subprocess`，操作发生在 Python 进程所在的机器上。Agent 如果在工作树或容器里运行，那里的目录和宿主不是同一个。

下面的工具继承前一个类（复用 `to_openai_schema`），从 Agent 绑定的环境读取文件。两个类放在同一个模块里，就可以把 `ReadProjectPolicy()` 直接放进工具列表。

```python
class ReadProjectPolicy(ProjectPolicy):
    name = 'read_project_policy'
    description = '读取当前任务工作区的 PROJECT_POLICY.md。'
    parameters = {'type': 'object', 'properties': {}, 'additionalProperties': False}

    async def execute_with_runtime(self, params, runtime):
        # 没有绑定执行环境时返回错误文字，而不是抛异常
        if runtime.environment is None:
            return 'Error: no execution environment available.'
        return await runtime.environment.read_file('PROJECT_POLICY.md')
```

`runtime` 上常用的成员：

| 成员 | 用途 |
| --- | --- |
| `runtime.environment` | 执行入口，提供 `read_file`、`write_file`、`exec_cmd` 等 |
| `runtime.aid` | 当前调用者的成员编号 |
| `runtime.tool_call_id` | 当前这次工具调用的编号 |

## 进阶与注意事项

- 参数校验只在 OC 分派工具调用时进行。你的代码直接调用 `execute_with_runtime` 时，要自己传入已校验的参数。
- 有状态的工具如果在多个会话或候选里复用，状态归谁由实现者负责管理。
- 需要候选测试记录的工具，要按公开的 `VerificationTool` 提供 `verified_targets`，并用真实验证来填充记录。
- 权限、外部请求和副作用由工具自己负责。涉及写入时，要在实现里定好：作用范围、失败后数据怎样保留、重复执行时怎样处理已有结果。需要隔离或回滚的操作，也由实现者接入相应机制。
- 较长的使用说明可以放进项目 Skill（见 [使用与编写 Skill](skills-on-demand.md)），工具本身只负责执行。

## 怎样测试一个工具

1. 用合法参数直接调用，检查返回结果。
2. 覆盖缺字段、类型错误、执行异常和超时。
3. 用本地模型替身或固定的工具调用，确认 Agent 收到了 schema、调用成功、结果进入了下一轮会话。
4. 用真实任务观察模型是否会在合适的时机选这个工具。

想把新工具贡献为内置能力，要遵循仓库的依赖方向和工具注册约定。应用私有的工具通常直接通过公开接口组合即可。

## 源码与进一步阅读

[opencollab/tools.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/tools.py) · [opencollab/application/tool_execution.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/application/tool_execution.py) · [opencollab/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/README.md)
