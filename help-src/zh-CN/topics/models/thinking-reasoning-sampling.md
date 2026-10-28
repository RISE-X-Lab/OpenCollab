---
{
  "id": "thinking-reasoning-sampling",
  "title": "深度思考与采样参数",
  "category": "models",
  "summary": "打开或关闭深度思考、设置推理强度和 temperature/top_p，并理解 OC 为什么有时会替你省略某些字段。",
  "aliases": [
    "深度思考",
    "推理强度",
    "关闭思考",
    "reasoning",
    "enable_thinking",
    "budget_tokens",
    "adaptive",
    "output_config",
    "temperature",
    "top_p",
    "max_output_tokens",
    "思考参数不生效"
  ],
  "keywords": [
    "OPENCOLLAB_THINKING",
    "OPENCOLLAB_THINKING_PARAMS",
    "OPENCOLLAB_REASONING_EFFORT",
    "thinking",
    "thinking_params",
    "reasoning_effort",
    "OPENCOLLAB_TEMPERATURE",
    "OPENCOLLAB_TOP_P"
  ],
  "questions": [
    "怎样给 Qwen 或 DeepSeek 打开深度思考？",
    "原生 Anthropic 的 budget_tokens 和 adaptive thinking 怎么配？",
    "为什么设置了 temperature 请求里却没有？",
    "按当前 provider、协议和模型能力选择 thinking、reasoning effort 与采样设置？"
  ],
  "related": [
    "provider-endpoint-credentials",
    "token-step-concurrency-limits",
    "configuration-precedence"
  ],
  "verified_ref": "v0.9.6"
}
---

# 深度思考与采样参数

OC 默认关闭深度思考（`thinking=false`）。打开它要两步：设 `thinking=true`，再用目标服务认可的**原生格式**写参数：Chat 和原生 Anthropic 用 `thinking_params`，Responses 用 `reasoning_effort`。

先理解 OC 的设计取舍：它**不做跨厂商的统一抽象**。参数基本原样透传，最终发出哪些字段，由协议和 OC 内置的模型能力表决定；必要时 OC 会自动省略 `temperature`、`top_p`。所以“设了却没发出去”，可能是 OC 有意为之。

## 关键设置一览

| 环境变量 | SDK 字段 | 默认值 | 用于 |
| --- | --- | --- | --- |
| `OPENCOLLAB_THINKING` | `thinking` | `false` | 所有协议的开关 |
| `OPENCOLLAB_THINKING_PARAMS` | `thinking_params` | `{"enable_thinking": true}` | Chat（经 `extra_body` 发出）、原生 Anthropic |
| `OPENCOLLAB_REASONING_EFFORT` | `reasoning_effort` | 未设置 | Responses |
| `OPENCOLLAB_TEMPERATURE` | `temperature` | `0.2`，范围 0 到 2 | 采样 |
| `OPENCOLLAB_TOP_P` | `top_p` | 未设置（用服务端默认），范围 0 到 1 | 采样 |

先确认物理模型和协议，再写该服务支持的原生格式。SDK 里对应 `config` 的小写字段 `thinking`、`thinking_params`、`reasoning_effort`。

## OpenAI 兼容 Chat 和 Responses

**Chat**：`thinking_params` 会通过 `extra_body` 发出去。服务接受 `enable_thinking` 时可以这样写：

```dotenv
OPENCOLLAB_THINKING=true
OPENCOLLAB_THINKING_PARAMS={"enable_thinking":true}
```

**Responses**：用 `OPENCOLLAB_REASONING_EFFORT` 设推理强度。OC 接受 `none`、`minimal`、`low`、`medium`、`high`、`xhigh` 和 `max`，具体模型支持哪些值以服务为准。

```dotenv
OPENCOLLAB_WIRE_PROTOCOL=responses
OPENCOLLAB_REASONING_EFFORT=high
```

格式要求：`thinking_params` 必须是能序列化成 JSON 的对象，环境变量里也必须写成 JSON 对象，数组或无效 JSON 会在配置阶段报错。切换协议或物理模型后要重新检查这些参数：上一个服务的字段留着不改，服务可能直接拒绝，也可能按它自己的默认处理。

## 原生 Anthropic thinking

**手动 thinking（`budget_tokens`）**：thinking token 至少 1,024，并且要小于单次 `max_output_tokens`。下面预留 16,000 thinking token，并把总输出调大，模型名和凭据沿用已配置的服务：

```dotenv
OPENCOLLAB_PROVIDER=anthropic
OPENCOLLAB_MAX_OUTPUT_TOKENS=32768
OPENCOLLAB_THINKING=true
OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","budget_tokens":16000}}
```

**adaptive thinking**：支持它的模型，用原生 adaptive 结构加该模型认可的 effort：

```dotenv
OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}
```

OC 对采样字段的处理：

- 原生 thinking 请求会按模型能力省略 `temperature`。
- 以下模型可按能力使用 0.95 到 1.0 的 `top_p`：支持的较早 Claude 4 手动 thinking 模型、Claude Opus 4.6 与 Sonnet 4.6 的 adaptive thinking，以及已声明的 DeepSeek 原生 Anthropic 模型。更新的模型遵守各自的采样要求。
- 工具调用后的续轮需要保留原生 thinking blocks 和签名数据，OC 的标准传输会处理这件事，见 [续轮数据](provider-endpoint-credentials.md#工具调用之后的续轮数据)。

## 采样参数与角色覆盖

- `temperature` 默认 0.2，范围 0 到 2；显式写 0.0 是有效值，会正常覆盖。
- `top_p` 范围 0 到 1，不写就用服务端的默认值。在支持采样的原生 Claude 4 普通请求里，显式 `top_p` 优先于温度。
- 模型或 thinking 模式有要求时，请求构造器会省略对应的采样字段。

团队 YAML 里，角色可以单独把 `temperature` 设为 0.0、把 `thinking` 设为 true，不写就继承全局值。原生 thinking 的详细参数通过客户端配置传入；角色能写哪些字段，见 [Team 入门](../team/configure-team.md#字段说明)。

## 内置的模型默认行为

部分模型在 OC 能力表里有专门的默认值：

| 模型 | 默认行为 |
| --- | --- |
| `qwen3.8-flash` | Chat 请求显式发送 `enable_thinking=false` |
| `deepseek-flash`、`deepseek-v4-pro`、`deepseek-v4-flash` | Chat 与 Anthropic 路径使用 disabled thinking；Responses 把 `reasoning` 的 `effort` 设为 `none` |

显式设置的 Responses reasoning effort 优先于上面的默认值，其他模型按 provider 能力处理。用 `inspect_model_runtime` 查看当前安装版本的模型能力，用 `client.configuration` 查看你显式覆盖了哪些值，见 [启动出错怎么查](configuration-precedence.md#启动出错按阶段排查)。本地能力表只用来构造请求，参数是否真的被支持，以你用的端点为准。

## 给思考留出额度

单次输出上限 `max_output_tokens` 和整次会话的 `budget` 是分开控制的。会话预算有限时，先给输入留出 token，再定输出空间；手动 thinking 还要同时留出思考额度和回答空间。剩余额度低于最低需求时，运行会以预算用尽为原因停止，已完成的工具结果和用量会保留。见 [预算、步数与并发](../operations/token-step-concurrency-limits.md)。

报参数错误，就检查模型和参数格式；报额度不够，就检查会话预算、输入长度和单次输出上限。

## 延伸阅读

- 源码依据：[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/configs/README.md) · [opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/config.py) · [docs/model-inspection.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/model-inspection.md)
- 相关指南：[接入模型服务](provider-endpoint-credentials.md) · [预算、步数与并发](../operations/token-step-concurrency-limits.md) · [配置从哪来、哪个生效](configuration-precedence.md)
