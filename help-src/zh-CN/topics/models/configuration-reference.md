---
{
  "id": "configuration-reference",
  "title": "配置参数速查",
  "category": "models",
  "summary": "一张表查清每个配置项叫什么、默认值是多少、该写在哪一层。",
  "aliases": [
    "参数表",
    "配置项大全",
    "环境变量大全",
    "默认值",
    "config reference",
    "configuration reference",
    "配置写在哪"
  ],
  "keywords": [
    "OpenCollabConfig",
    "OPENCOLLAB_CONFIG_FILE",
    "OPENCOLLAB_MODEL_ROUTES",
    "配置参数",
    "SDK config",
    "运行参数"
  ],
  "questions": [
    "有哪些配置项以及默认值是什么",
    "环境变量和 SDK 参数怎样对应",
    "这个设置应该写在 env 文件、Team YAML 还是函数参数里"
  ],
  "related": [
    "configuration-precedence",
    "provider-endpoint-credentials",
    "thinking-reasoning-sampling",
    "timeouts-retries",
    "token-step-concurrency-limits"
  ],
  "verified_ref": "0c47f74a"
}
---

# 配置参数速查

这是一张速查表：每个客户端配置项的环境变量名、SDK `config` 字段名、内置默认值，以及其他设置该去哪个入口配。例如模型请求超时，在 env 文件里是 `OPENCOLLAB_LLM_TIMEOUT`，在 SDK 里是 `llm_timeout`，默认 600 秒。

读表前记住三点：

- 表里列的是当前源码的**内置**默认值。如果你复制了 `configs/.env.example`，示例文件里写好的模型、端点和预算就成了显式配置，会替代这些默认值。
- SDK 的 `config` 用表中的小写字段名，env 文件用大写的环境变量名。
- 多个来源谁优先、怎样查看实际生效的值，见 [配置从哪来、哪个生效](configuration-precedence.md)。

## 模型与认证

物理模型名是服务端实际接受的模型名；逻辑路由名是你在 OC 里给一整套连接设置起的名字，见 [模型路由](model-routing-request-capacity.md)。

| 环境变量 | SDK 字段 | 默认值与作用 |
| --- | --- | --- |
| `OPENCOLLAB_MODEL` | `model` | `gpt-4o`，可填物理模型名或已声明的逻辑路由名 |
| `OPENCOLLAB_PROVIDER` | `provider` | `openai`；原生 Messages 用 `anthropic` |
| `OPENCOLLAB_BASE_URL` | `base_url` | 未设置时用所选 provider 的默认端点 |
| `OPENCOLLAB_API_KEY` | `api_key` | 默认未设置，按 provider 与端点选择认证来源 |
| `ANTHROPIC_AUTH_TOKEN` | `auth_token` | 默认未设置，原生 Anthropic bearer 认证 |
| `OPENCOLLAB_WIRE_PROTOCOL` | `wire_protocol` | `chat_completions`；OpenAI 兼容端点也可选 `responses` |
| `OPENCOLLAB_MODEL_ROUTES` | `model_routes` | 默认空映射，声明逻辑模型到实际端点的路由 |
| `OPENCOLLAB_REQUEST_POOL` | `request_pool` | 默认未设置，同机共享请求池的绝对目录 |
| `OPENCOLLAB_REQUEST_CAPACITY` | `request_capacity` | 默认未设置，正整数，和请求池目录成对设置 |

`model`、`provider`、`base_url`、`api_key`、`model_routes` 也可以直接作为 `OpenCollab(...)` 的参数传入；同一项既作为参数传入、又写在 `config` 里时，以参数为准。`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DASHSCOPE_API_KEY` 等备用变量及其优先级，见 [OC 用哪个 API key](provider-endpoint-credentials.md#oc-用哪个-api-key)。

## 生成与上下文

| 环境变量 | SDK 字段 | 内置默认值 |
| --- | --- | --- |
| `OPENCOLLAB_BUDGET` | `budget` | `1000000` tokens |
| `OPENCOLLAB_MAX_OUTPUT_TOKENS` | `max_output_tokens` | 每次模型调用 `8192` tokens |
| `OPENCOLLAB_CONTEXT_WINDOW` | `context_window` | 不覆盖时按模型能力和运行时回退规则确定 |
| `OPENCOLLAB_TEMPERATURE` | `temperature` | `0.2`，范围 `0.0` 到 `2.0` |
| `OPENCOLLAB_TOP_P` | `top_p` | 未设置，用服务端默认值；显式值范围 `0.0` 到 `1.0` |
| `OPENCOLLAB_THINKING` | `thinking` | `false` |
| `OPENCOLLAB_THINKING_PARAMS` | `thinking_params` | `{"enable_thinking": true}`，按 thinking 与协议条件使用 |
| `OPENCOLLAB_REASONING_EFFORT` | `reasoning_effort` | 未设置；支持 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max` |
| `OPENCOLLAB_LLM_STREAM_CHAT` | `llm_stream_chat` | `false`，控制 Chat Completions 流式接收 |

这三项要分开理解：`budget` 是整次会话的 token 用量上限；`max_output_tokens` 是单次模型响应的输出上限；`context_window` 是 OC 据此决定历史容量和何时压缩的窗口。见 [预算、步数与并发](../operations/token-step-concurrency-limits.md) 和 [上下文窗口与压缩](../operations/context-compaction.md)。服务端最终收到哪些字段，还取决于协议、模型能力和原生 thinking 的要求，见 [深度思考与采样](thinking-reasoning-sampling.md)。

## 模型请求的超时与重试

| 环境变量 | SDK 字段 | 内置默认值 |
| --- | --- | --- |
| `OPENCOLLAB_LLM_TIMEOUT` | `llm_timeout` | `600` 秒 |
| `OPENCOLLAB_LLM_CONNECT_TIMEOUT` | `llm_connect_timeout` | `30` 秒 |
| `OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT` | `llm_first_event_timeout` | `180` 秒 |
| `OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT` | `llm_stream_idle_timeout` | `180` 秒 |
| `OPENCOLLAB_LLM_MAX_RETRIES` | `llm_max_retries` | `3` 次重试 |
| `OPENCOLLAB_PROVIDER_ERROR_TIME_BUDGET` | `provider_error_time_budget` | `0`；设为正数时按秒限制失败尝试和重试等待 |

这些是**单次模型请求**的设置。整次任务的期限、工具执行的期限在别处设置，见 [运行怎样停下](../operations/timeouts-retries.md)。

## VM 资源

环境变量 `OPENCOLLAB_VM` 对应 SDK 的 `config["vm"]`，默认不设置。给它一段 JSON 后，OC 会注册 `vm-group` 和 `vm-session` 两种隔离策略。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `resources` | 必填 | 可用的 VM 列表，至少一台 |
| `lease_database` | 必填 | 记录 VM 占用情况的本地文件，必须是绝对路径 |
| `wait_timeout` | `0` 秒 | 没有空闲 VM 时等多久。`0` 表示不等、直接抛出 `VMCapacityError`；正数是最多等待的秒数；JSON 里写 `null` 表示一直等到有空闲 VM 或任务被取消 |
| `cleanup_timeout` | `60` 秒 | 运行结束后等待 VM 清理完成的时间 |

完整字段和重置要求见 [在现有 Linux VM 中运行任务](../isolation/vm-environment-extension.md#第二步写-vm-配置)。

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

这段代码只解析本地配置。调用 `agent()`、`team()` 或 `workflow()` 才会真正开始任务。配置值在创建客户端时就定了，修改文件后要新建一个客户端实例才能读到新设置。

## 其他设置的入口

| 要调整的内容 | 配置位置 | 详细指南 |
| --- | --- | --- |
| 选择 env 文件 | 进程变量 `OPENCOLLAB_CONFIG_FILE` | [配置从哪来](configuration-precedence.md#oc-会读哪些-env-文件) |
| 选择团队 | CLI `--team-config`、SDK `team(config=...)`、进程变量 `OPENCOLLAB_TEAM_FILE` | [Team 入门](../team/configure-team.md) |
| 角色模型、工具与提示词 | 团队 YAML 的 `roles` | [Team 入门](../team/configure-team.md#字段说明) |
| 角色额度、串行执行和并发 | 团队 YAML 的 `budget`，以及 SDK 运行参数 | [预算、步数与并发](../operations/token-step-concurrency-limits.md) |
| 上下文清理策略 | 团队 YAML 的 `context` | [上下文窗口与压缩](../operations/context-compaction.md) |
| Workflow 发现目录 | 进程变量 `OPENCOLLAB_WORKFLOWS_DIR` | [运行一个 Workflow](../workflow/run-registered-workflow.md) |
| Workflow 输入、Agent 和集合并发 | `workflow(inputs=..., concurrency=..., task_concurrency=...)` | [Workflow API 速查](../workflow/workflow-public-api-map.md) |
| 执行环境和隔离策略 | 客户端 `environment`、`environment_providers`，角色或 Workflow 的 `isolation` | [隔离](../isolation/isolation-overview.md) |
| 任务期限、外部取消和清理 | 运行参数 `timeout`、`run_control`、`cleanup_timeout` | [运行怎样停下](../operations/timeouts-retries.md) |
| 无限预算与显式额度 | 进程变量 `OPENCOLLAB_UNBOUNDED_LIMITS`、Workflow 的 `limit_mode` | [无限制模式](../operations/token-step-concurrency-limits.md#无限制模式) |
| 预算提示与写入提示 | `OPENCOLLAB_BUDGET_NUDGE_MODE`、`OPENCOLLAB_WRITE_NUDGE_MODE` | [预算、步数与并发](../operations/token-step-concurrency-limits.md#注入给模型的提示) |
| 工具执行期限 | `OPENCOLLAB_TOOL_EXECUTION_TIMEOUT`、`OPENCOLLAB_TOOL_EXECUTION_MAX_TIMEOUT` | [运行怎样停下](../operations/timeouts-retries.md#工具执行卡住时调哪一层) |
| 逐请求用量记录与价格 | `OPENCOLLAB_API_USAGE_LOG` 与 `OPENCOLLAB_*_USD_PER_MTOK` | [用量与费用](../observability/token-cache-cost.md) |
| 运行文件与轨迹 | SDK 的 `artifacts`、`trace`、`run_id` | [运行记录](../observability/artifacts-and-history.md) |
| 旧版显示兼容项 | `OPENCOLLAB_FILTER_MESSAGES`，默认 `false` | [在终端里使用 OC](../setup/interactive-cli-controls.md) |

补充说明：

- 有些运行时控制变量由进程直接读取，按各篇指南说的位置设置即可。
- `filter_messages` 是旧版兼容字段，当前终端会为每个成员保留完整历史。
- `logical_model` 和 `thinking_params_explicit` 是运行时记录的解析状态，不需要手动设置；平时用模型路由和 thinking 参数表达你的意图。

## 延伸阅读

- 源码依据：[客户端配置与校验](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/config.py) · [公开客户端参数](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/sdk/client.py) · [配置指南](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/README.md)
- 相关指南：[配置从哪来、哪个生效](configuration-precedence.md) · [接入模型服务](provider-endpoint-credentials.md) · [运行怎样停下](../operations/timeouts-retries.md)
