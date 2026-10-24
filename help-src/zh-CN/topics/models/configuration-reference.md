---
{
  "id": "configuration-reference",
  "title": "配置参数与环境变量速查",
  "category": "models",
  "summary": "一张表查清每个配置项叫什么、默认值是多少、该写在哪里。",
  "aliases": ["参数表", "配置项大全", "环境变量大全", "默认值", "config reference", "configuration reference", "配置写在哪"],
  "keywords": ["OpenCollabConfig", "OPENCOLLAB_CONFIG_FILE", "OPENCOLLAB_MODEL_ROUTES", "配置参数", "SDK config", "运行参数"],
  "questions": ["有哪些配置项以及默认值是什么", "环境变量和 SDK 参数怎样对应", "这个设置应该写在 env 文件、Team YAML 还是函数参数里"],
  "related": ["configuration-precedence", "provider-endpoint-credentials", "model-routing-request-capacity", "thinking-reasoning-sampling", "configuration-diagnostics", "token-step-concurrency-limits"],
  "verified_ref": "0c47f74a"
}
---

# 配置参数与环境变量速查

速查页：每个配置项的环境变量名、SDK `config` 字段名、内置默认值，以及其它设置该去哪个入口配。例如模型请求超时在 env 文件里是 `OPENCOLLAB_LLM_TIMEOUT`，在 SDK 里是 `llm_timeout`，默认 600 秒。

## 先分清设置写在哪一层

OpenCollab 的设置分三层，写错层会报错或不生效：

| 层级 | 管什么 | 写在哪 |
| --- | --- | --- |
| 客户端配置 | 连哪个模型、怎么认证、请求怎么发 | 环境变量 / env 文件，或 `OpenCollab(config=...)` |
| 团队声明 | 有哪些角色、角色之间怎么协作（拓扑） | Team YAML |
| 一次运行的参数 | 这次任务的预算、超时等 | `agent()`、`team()` 或 `workflow()` 的调用参数 |

下面各表列的是当前源码的内置默认值。如果你复制了 `configs/.env.example`，示例文件里写好的模型、端点和预算就成了显式配置，会替代这些默认值。实际取值可以用 [有效配置检查](../operations/configuration-diagnostics.md) 核对。多个来源谁优先，见 [配置从哪里读取以及哪一项生效](configuration-precedence.md)。

## 模型与认证

物理模型名：服务端实际接受的模型名。逻辑路由名：OC 里自定义的名字，对应一整套连接设置（模型、provider、协议、地址、凭据）（见 [逻辑模型路由与共享请求容量](model-routing-request-capacity.md)）。

| 环境变量 | SDK 配置字段 | 默认值与作用 |
| --- | --- | --- |
| `OPENCOLLAB_MODEL` | `model` | `gpt-4o`，可填物理模型名或已声明的逻辑路由名 |
| `OPENCOLLAB_PROVIDER` | `provider` | `openai`，原生 Messages 使用 `anthropic` |
| `OPENCOLLAB_BASE_URL` | `base_url` | 未设置时采用所选 provider 的默认端点 |
| `OPENCOLLAB_API_KEY` | `api_key` | 默认未设置，按 provider 与端点选择认证来源 |
| `ANTHROPIC_AUTH_TOKEN` | `auth_token` | 默认未设置，原生 Anthropic bearer 认证 |
| `OPENCOLLAB_WIRE_PROTOCOL` | `wire_protocol` | `chat_completions`，OpenAI 兼容端点也可选 `responses` |
| `OPENCOLLAB_MODEL_ROUTES` | `model_routes` | 默认空映射，声明逻辑模型到实际端点的路由 |
| `OPENCOLLAB_REQUEST_POOL` | `request_pool` | 默认未设置，同机共享请求池的绝对目录 |
| `OPENCOLLAB_REQUEST_CAPACITY` | `request_capacity` | 默认未设置，正整数，与请求池目录成对设置 |

`model`、`provider`、`base_url`、`api_key`、`model_routes` 也可以直接作为 `OpenCollab(...)` 的参数传入。如果同一项既作为参数传入、又写在 `config` 里，以参数为准。`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DASHSCOPE_API_KEY` 等备用变量及其优先级，见 [端点与认证](provider-endpoint-credentials.md)。逻辑路由的字段、容器中共享目录和多台机器的安排，见 [逻辑模型路由与共享请求容量](model-routing-request-capacity.md)。

## 生成与上下文

| 环境变量 | SDK 配置字段 | 内置默认值 |
| --- | --- | --- |
| `OPENCOLLAB_BUDGET` | `budget` | `1000000` tokens |
| `OPENCOLLAB_MAX_OUTPUT_TOKENS` | `max_output_tokens` | 每次模型调用 `8192` tokens |
| `OPENCOLLAB_CONTEXT_WINDOW` | `context_window` | 未显式覆盖，按模型能力与运行时回退规则确定 |
| `OPENCOLLAB_TEMPERATURE` | `temperature` | `0.2`，取值范围 `0.0` 到 `2.0` |
| `OPENCOLLAB_TOP_P` | `top_p` | 未设置，采用服务端默认值，显式值范围 `0.0` 到 `1.0` |
| `OPENCOLLAB_THINKING` | `thinking` | `false` |
| `OPENCOLLAB_THINKING_PARAMS` | `thinking_params` | 默认对象 `{"enable_thinking": true}`，按 thinking 与协议条件使用 |
| `OPENCOLLAB_REASONING_EFFORT` | `reasoning_effort` | 未设置，支持 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max` |
| `OPENCOLLAB_LLM_STREAM_CHAT` | `llm_stream_chat` | `false`，控制 Chat Completions 流式接收 |

服务端最终收到哪些字段，还取决于协议、模型能力和原生 thinking 的要求，具体组合见 [配置深度思考与采样参数](thinking-reasoning-sampling.md)。

OC 里这三项分开控制：

- `budget`：整次会话的 token 用量上限。
- `max_output_tokens`：单次模型响应的输出上限。
- `context_window`：OC 据此决定历史容量和何时压缩。

三者的实际关系见 [预算与并发](../operations/token-step-concurrency-limits.md) 和 [上下文压缩](../operations/context-compaction.md)。

## 模型请求的超时与重试

| 环境变量 | SDK 配置字段 | 内置默认值与单位 |
| --- | --- | --- |
| `OPENCOLLAB_LLM_TIMEOUT` | `llm_timeout` | `600` 秒 |
| `OPENCOLLAB_LLM_CONNECT_TIMEOUT` | `llm_connect_timeout` | `30` 秒 |
| `OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT` | `llm_first_event_timeout` | `180` 秒 |
| `OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT` | `llm_stream_idle_timeout` | `180` 秒 |
| `OPENCOLLAB_LLM_MAX_RETRIES` | `llm_max_retries` | `3` 次重试 |
| `OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET` | `provider_error_time_budget` | `0`，启用正数时按秒限制失败尝试和重试等待 |

这些是单次模型请求的设置。整次任务的期限、工具执行的时限在别处设置。流式请求的各个等待阶段、停止原因和工具清理参数见 [超时、重试与请求卡住诊断](../operations/timeouts-retries.md)。

## 创建客户端并核对设置

```python
from opencollab import OpenCollab

client = OpenCollab(
    ".",
    model="gpt-4o",
    config={
        "budget": 100000,
        "max_output_tokens": 8192,
        "temperature": 0.2,
        "llm_timeout": 300,
        "llm_max_retries": 2,
    },
)
# 只读取并打印几个字段，不会发起任务
for name in ("model", "budget", "max_output_tokens", "llm_timeout"):
    print(name, client.configuration[name])
```

这段代码只解析本地配置并读取字段。调用 `agent()`、`team()` 或 `workflow()` 才会真正开始任务。

- SDK 的 `config` 用表中的小写字段名。env 文件用大写的环境变量名。
- 配置值在创建客户端时就定了。修改文件后，要新建一个客户端实例才能读到新设置。

## 其他设置的入口

| 要调整的内容 | 配置位置 | 详细指南 |
| --- | --- | --- |
| 选择 env 文件 | 进程变量 `OPENCOLLAB_CONFIG_FILE` | [配置从哪里读取以及哪一项生效](configuration-precedence.md) |
| 选择团队 | CLI `--team-config`、SDK `team(config=...)`、进程变量 `OPENCOLLAB_TEAM_FILE` | [团队配置](../team/configure-team.md) |
| 角色模型、工具与提示词 | Team YAML 的 `roles` | [团队配置](../team/configure-team.md) |
| 角色额度、串行执行和并发 | Team YAML 的 `budget`，以及 SDK 运行参数 | [预算与并发](../operations/token-step-concurrency-limits.md) |
| 上下文清理策略 | Team YAML 的 `context` | [上下文压缩](../operations/context-compaction.md) |
| 工作流发现目录 | 进程变量 `OPENCOLLAB_WORKFLOWS_DIR` | [运行一个工作流（Workflow）](../workflow/run-registered-workflow.md) |
| 工作流输入、Agent 和集合并发 | `workflow(inputs=..., concurrency=..., task_concurrency=...)` | [Workflow 公开 API 速查](../workflow/workflow-public-api-map.md) |
| 执行环境和隔离策略 | 客户端 `environment`、`environment_providers`，角色或工作流 `isolation` | [隔离配置](../isolation/isolation-configuration.md) |
| 任务期限、外部取消和清理 | 运行参数 `timeout`、`run_control`、`cleanup_timeout`，按入口签名使用 | [中断与清理](../operations/cancellation-cleanup.md) |
| 无限预算与显式额度 | 进程变量 `OPENCOLLAB_UNBOUNDED_LIMITS`、Workflow `limit_mode` | [预算与并发](../operations/token-step-concurrency-limits.md) |
| 历史预算提示与写入提示 | `OPENCOLLAB_BUDGET_NUDGE_MODE`、`OPENCOLLAB_WRITE_NUDGE_MODE` | [预算与并发](../operations/token-step-concurrency-limits.md) |
| 逐请求用量记录与价格 | `OPENCOLLAB_API_USAGE_LOG` 与 `OPENCOLLAB_*_USD_PER_MTOK` | [用量与费用](../observability/token-cache-cost.md) |
| 运行文件与轨迹 | SDK `artifacts`、`trace`、`run_id` | [找到运行记录、会话和交付文件](../observability/artifacts-and-history.md) |
| 旧版显示兼容项 | `OPENCOLLAB_FILTER_MESSAGES`，默认 `false` | [默认 CLI](../setup/interactive-cli-controls.md) |

补充说明：

- 有些运行时控制变量由进程直接读取，按各篇指南说的位置设置即可。
- `filter_messages` 是旧版兼容字段。当前终端会为每个 Agent 保留完整历史。
- `logical_model` 和 `thinking_params_explicit` 是运行时记录的解析状态，不需要手动设置。平时用模型路由和 thinking 参数表达你的意图。

## VM 资源配置

`OPENCOLLAB_VM` 对应 SDK `config["vm"]`，默认未设置，传入 JSON 后注册 `vm-group` 和 `vm-session`。字段包含 `resources`、本地绝对路径 `lease_database`、默认 `0` 秒的 `wait_timeout` 和默认 `60` 秒的 `cleanup_timeout`。JSON 中的 `null` 等待时限表示等待空闲资源或取消。完整字段与重置要求见 [VM 指南](../isolation/vm-environment-extension.md)。

## 来源

[客户端配置与校验](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/config.py) · [公开客户端参数](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py) · [配置指南](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md)
