---
{
  "id": "service-integration-lifecycle",
  "title": "把 OC 接进自己的服务",
  "category": "architecture",
  "summary": "在后端服务里调用 SDK：一个请求对应一次运行，处理 RunResult 和 RunError，设好超时，并按“谁创建谁清理”释放环境和模型客户端。",
  "aliases": [
    "Web 服务",
    "嵌入应用",
    "RunResult",
    "RunError",
    "run_id",
    "artifacts",
    "CancelledError",
    "cleanup_timeout",
    "session_quiesced",
    "create_model_client"
  ],
  "keywords": [
    "服务集成",
    "SDK 接入",
    "资源所有权",
    "run_id 关联",
    "超时设置",
    "取消与清理"
  ],
  "questions": [
    "把一次 SDK 演示接入业务服务，并明确运行身份、结果处理与资源清理责任？",
    "怎样把业务请求 ID 和 OC 运行对应起来？",
    "stopped 和 failed 分别怎么处理？",
    "timeout 和 cleanup_timeout 有什么区别？",
    "自己传入的 environment 由谁清理？"
  ],
  "related": [
    "python-sdk-entrypoints",
    "timeouts-retries",
    "execution-environments",
    "run-results",
    "artifacts-and-history"
  ],
  "verified_ref": "v0.9.6"
}
---

# 把 OC 接进自己的服务

在 Web 服务或后台任务系统里用 OC，核心做法只有三条：

1. **一个业务请求对应一次运行**，用 `run_id` 关联，每次运行用一个新的空 `artifacts` 目录。
2. **拿到 `RunResult` 后，再用业务自己的方式验收**，把“运行成功”和“业务验收通过”分开记录。
3. **谁创建谁清理**：你自己传入的执行环境和模型客户端，OC 不会替你关闭。

三种入口按需选：一次独立任务用 `agent`；多个角色按进展协作用 `team`；由服务代码规定步骤、分支和重试用 `workflow`。下面先给一个可以直接改用的服务函数，再讲结果、超时、取消和清理。

## 最小例子：只读的项目分析服务

这个函数只给读取工具，为每次请求建一个独立的产物目录，并在结束时清理自己创建的环境：

```python
from pathlib import Path
from tempfile import mkdtemp

from opencollab import OpenCollab
from opencollab.environments import local_environment
from opencollab.tools import builtin_tools

async def inspect_request(project, request_id, task):
    artifact_root = Path("artifacts/service").resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    # 每次运行一个新的空目录
    artifacts = mkdtemp(prefix="inspection-", dir=artifact_root)
    # 环境由服务创建，所以由服务负责 setup 和 cleanup
    environment = local_environment(project)
    try:
        await environment.setup()
        client = OpenCollab(project, environment=environment)
        result = await client.agent(
            task,
            tools=builtin_tools("file_read", "grep"),
            budget=50_000,
            max_steps=40,
            timeout=900,
            cleanup_timeout=10,
            artifacts=artifacts,
            run_id=request_id,  # 用业务请求 ID 作为运行 ID
        )
        return {
            "run_id": result.metrics["run_id"],
            "status": result.status,
            "reason": result.reason,
            "output": result.output,
            "artifacts": str(result.artifacts),
            "agent_failures": result.agent_failures,
        }
    finally:
        await environment.cleanup()
```

- `project`：服务允许使用的项目目录。
- `request_id`：必须是非空文本；不传时，每次 SDK 运行也会自动生成 `run_id`。`metrics["run_id"]` 和会话快照、团队或 Workflow 清单、已开启的轨迹都对得上。
- `artifacts`：需要新建或为空，这样一个请求产生的记录都放在一起。

修改类任务要改用独立的工作树或容器，并明确候选结果怎样采纳到业务目录；多个请求共享同一个可写目录时，要按业务需要安排修改顺序，见 [隔离](../isolation/isolation-overview.md)。

## 处理运行结果

| `RunResult.status` | 含义 | 怎么处理 |
| --- | --- | --- |
| `completed` | 运行正常完成，`ok=True` | 再做业务验收 |
| `stopped` | 触发限制，`reason` 例如 `timeout`、预算或步数限制 | 可能带部分工作，按需保存 |
| `failed` | 运行失败 | 读取错误信息 |

- `agent_failures` 保存经过脱敏的子成员失败摘要，用它判断 Team 或 Workflow 内部是否有成员出错。
- 可以自己判断 `status`，也可以调用 `raise_for_status()` 把 stopped 或 failed 变成带原结果的 `RunError`。
- 另外要捕获直接抛出的 `RunError`：参数错误通常在运行开始前就抛出，生命周期或持久化故障也可能直接抛出。服务应把这些错误和请求 ID 一起保存。

`completed` 只说明运行结束。按任务类型做业务验收：代码任务在最终交付目录跑项目测试；业务工具任务读取目标系统的最终状态；抽取任务核对要求的字段和来源。见 [判断任务是否真的完成](../observability/run-results.md)。

## 超时：几个限制各管什么

| 设置 | 限制什么 |
| --- | --- |
| SDK `timeout` | 整次运行 |
| Workflow 中单次角色调用的 `timeout` | 那一次角色调用 |
| 传输层设置 | 模型请求、连接、首个流事件、流空闲 |
| `cleanup_timeout` | 执行结束后框架清理自身资源的等待时间 |

模型请求同时受整次运行期限约束，各层的默认值和调法见 [运行怎样停下](../operations/timeouts-retries.md)。Workflow 要严格使用调用方传入的 token 和步数额度时，选 `limit_mode="explicit"`；默认的 environment 模式会参考 `OPENCOLLAB_UNBOUNDED_LIMITS`。

接入时至少跑一次真实请求，确认等待、停止和部分产物的处理符合服务需要。

## 取消时发生什么

- 调用方取消任务：框架先清理这次运行拥有的资源，再继续抛出 `asyncio.CancelledError`。
- 达到运行期限：返回 `reason="timeout"` 的停止结果。
- 已发出的模型请求可能稍后才返回用量，框架会在清理等待阶段结算一次。统计费用时读取最终保存的用量和清理记录。

几个清理字段：`session_quiesced` 表示会话和持久化是否已结束。你注入了执行环境时，`environment_quiesced`、`cleanup_quiesced` 和 `execution_quiesced` 都是 `None`，环境是否已停止，由你在清理后自己验证。见 [停止之后：确认清理完成](../operations/timeouts-retries.md#停止之后确认清理完成)。

## 注入自己的模型客户端

要观察或适配模型传输，可以先调用 `create_model_client()`，再通过 `agent(llm=...)` 传入。传入的客户端需要满足模型调用协议。下面的片段放在已创建 `client` 的异步函数里：

```python
model = client.create_model_client()
try:
    result = await client.agent(
        "说明项目的公开入口",
        tools=builtin_tools("file_read", "grep"),
        llm=model,
        budget=50_000,
        max_steps=40,
        timeout=900,
    )
finally:
    # 客户端由你创建，所以由你关闭
    await model.close()
```

`create_model_client()` 沿用这个 `OpenCollab` 对象解析后的配置。创建者负责在全部使用结束后关闭它。

## 谁负责清理

| 资源 | 谁负责 |
| --- | --- |
| SDK 自己创建的运行资源 | 框架，按运行生命周期清理 |
| 应用注入的执行环境 | 应用：setup、cleanup 和最终的资源检查 |
| 应用创建的模型客户端或传输层 | 应用，全部使用结束后关闭 |

已有容器、VM 等资源归谁，见 [使用自己的执行环境](../isolation/execution-environments.md#谁负责清理)。

## 配置、记录和成本

- **配置**：客户端在创建时读取生效配置。服务更新模型路由或环境变量后，要按自己的配置更新流程重新创建客户端，后续请求才会用新设置。模型端点、实际请求并发、任务隔离是分开配置的；容器内能否联网，要按工具实际访问的目标验证。
- **记录**：完整会话可能包含业务输入和文件内容，按服务自己的访问权限和保留规则存放；给用户看的摘要和排错材料分别设访问范围。想少存轨迹，用 `trace=False`，保存的会话和运行清单仍按各入口的产物行为保留。
- **成本**：运行的 tokens 是累计的输入加输出，缓存读取已计入输入，推理 token 按 provider 的输出统计处理。失败或超时的运行也可能已经产生用量。算费用见 [用量与费用](../observability/token-cache-cost.md)。

## 部署平台要求

本机文件适配器支持 Linux 和 macOS，依赖平台的描述符相对操作、不跟随链接的读取、文件锁和原子重命名，部署目标要满足这些条件（见 [常见问题：capability 错误](../setup/first-run-workspace.md#常见问题)）。处理不可信代码时，放到可以丢弃的隔离环境里。

## 延伸阅读

- 源码依据：[公开运行结果](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/sdk/result.py) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/sdk/client.py) · [运行参考：取消、运行身份与资源归属](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/evaluation-runtime.md)
- 相关指南：[从 Python 调用 OC](../setup/python-sdk-entrypoints.md) · [运行怎样停下](../operations/timeouts-retries.md) · [使用自己的执行环境](../isolation/execution-environments.md) · [运行记录](../observability/artifacts-and-history.md)
