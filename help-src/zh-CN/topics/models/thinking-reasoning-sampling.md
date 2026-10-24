---
{
  "id": "thinking-reasoning-sampling",
  "title": "配置深度思考与采样参数",
  "category": "models",
  "summary": "在 OC 里打开或关闭深度思考、设置推理强度和 temperature/top_p，并知道 OC 会替你省略哪些字段。",
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
    "wire-protocol-streaming",
    "token-step-concurrency-limits",
    "configuration-diagnostics"
  ],
  "verified_ref": "0c47f74a"
}
---

# 配置深度思考与采样参数

OC 默认 `thinking=false`。打开思考要设 `thinking=true`，再用 `thinking_params`（Chat、原生 Anthropic）或 `reasoning_effort`（Responses）写目标服务认可的原生格式。OC 不做跨厂商的统一抽象：参数基本原样透传，最终发哪些字段由协议和 OC 内置的模型能力表决定，必要时 OC 会自动省略 `temperature`、`top_p`。团队角色可以单独覆盖 `thinking` 与 `temperature`。

## 关键设置一览

| 环境变量 | SDK 字段 | 默认值 | 用于 |
| --- | --- | --- | --- |
| `OPENCOLLAB_THINKING` | `thinking` | `false` | 所有协议的开关 |
| `OPENCOLLAB_THINKING_PARAMS` | `thinking_params` | `{"enable_thinking": true}` | Chat（经 `extra_body`）、原生 Anthropic |
| `OPENCOLLAB_REASONING_EFFORT` | `reasoning_effort` | 未设置 | Responses |
| `OPENCOLLAB_TEMPERATURE` | `temperature` | `0.2`，范围 0 到 2 | 采样 |
| `OPENCOLLAB_TOP_P` | `top_p` | 未设置（用服务端默认），范围 0 到 1 | 采样 |

先确认物理模型和协议，再写该服务支持的原生格式。

## 为 Chat 和 Responses 设置推理

### OpenAI 兼容 Chat

`thinking_params` 会通过 `extra_body` 发出去。服务接受 `enable_thinking` 时可以这样写：

```dotenv
OPENCOLLAB_THINKING=true
OPENCOLLAB_THINKING_PARAMS={"enable_thinking":true}
```

### Responses

用 `OPENCOLLAB_REASONING_EFFORT` 设推理强度。OC 接受 `none`、`minimal`、`low`、`medium`、`high`、`xhigh` 和 `max`，具体模型支持哪些值以服务为准。

```dotenv
OPENCOLLAB_WIRE_PROTOCOL=responses
OPENCOLLAB_REASONING_EFFORT=high
```

### 格式要求

- SDK 里对应 `config` 的小写字段 `thinking`、`thinking_params`、`reasoning_effort`。
- `thinking_params` 必须是能序列化成 JSON 的对象。环境变量里也必须是 JSON 对象。数组或无效 JSON 会在配置阶段报错。
- 切换协议或物理模型后，重新检查这些参数。上一个服务的字段留着不改，会改变请求内容：服务可能直接拒绝，也可能按它自己的默认处理。

## 使用原生 Anthropic thinking

### 手动 thinking（`budget_tokens`）

thinking token 至少 1,024，并且要小于单次 `max_output_tokens`。下面预留 16,000 thinking token，并把总输出调大。模型名和凭据沿用已配置的服务。

```dotenv
OPENCOLLAB_PROVIDER=anthropic
OPENCOLLAB_MAX_OUTPUT_TOKENS=32768
OPENCOLLAB_THINKING=true
OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"enabled","budget_tokens":16000}}
```

### adaptive thinking

支持 adaptive thinking 的模型，可以用原生 adaptive 结构加该模型认可的 effort：

```dotenv
OPENCOLLAB_THINKING_PARAMS={"thinking":{"type":"adaptive"},"output_config":{"effort":"high"}}
```

### OC 对采样字段的处理

- 原生 thinking 请求会按模型能力省略 `temperature`。
- 以下模型可按能力使用 0.95 到 1.0 的 `top_p`：支持的较早 Claude 4 手动 thinking 模型、Claude Opus 4.6 与 Sonnet 4.6 的 adaptive thinking，以及已声明的 DeepSeek 原生 Anthropic 模型。
- 更新的模型遵守各自的采样要求。
- 工具调用后的续轮需要保留原生 thinking blocks 和签名数据。OC 的标准传输会处理这件事。

## 调整采样与角色覆盖

全局采样：

- `temperature` 默认 0.2，范围 0 到 2。显式写 0.0 是有效值。
- `top_p` 范围 0 到 1，不写就用 provider 的默认值。
- 在支持采样的原生 Claude 4 普通请求里，显式 `top_p` 优先于温度。
- 如果模型或 thinking 模式有要求，请求构造器会省略对应的采样字段。所以“设了却没发出去”可能是 OC 有意为之。

团队 YAML 里的角色覆盖：

- 角色可以把 `temperature` 设为 0.0，把 `thinking` 设为 true。不写就继承全局值。
- 原生 thinking 的详细参数通过客户端模型设置传入。角色能写哪些字段，见团队 schema。

## 查看默认行为

部分模型在 OC 能力表里有专门的默认值：

| 模型 | 默认行为 |
| --- | --- |
| `qwen3.8-flash` | Chat 请求显式发送 `enable_thinking=false` |
| `deepseek-flash`、`deepseek-v4-pro`、`deepseek-v4-flash` | Chat 与 Anthropic 路径使用 disabled thinking。Responses 把 `reasoning` 的 `effort` 设为 `none` |

显式设置的 Responses reasoning effort 优先于上面的默认值。其它模型的默认行为按 provider 能力处理。

查看方法：

- `inspect_model_runtime`：显示当前安装版本的模型能力。
- `OpenCollab.configuration`：显示你显式覆盖了哪些值。

本地能力表只用来构造请求。参数是否真的被支持，以你用的端点为准。

## 留出会话额度

单次输出上限 `max_output_tokens` 与整次会话 `budget` 是分开控制的。会话预算有限时，先给输入留出 token，再定输出空间。手动 thinking 要同时留出思考额度和回答空间。

如果剩余额度低于最低需求，运行会以 budget-exceeded 原因停止，已完成的工具结果和用量会保留。

排错：

- 如果报参数错误，就检查模型和参数格式。
- 如果额度不够，就检查会话预算、输入长度和单次输出上限。

## 来源

[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L140)　[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L162)　[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L192)　[opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/config.py#L176)

## 相关指南

[选择调用协议与流式响应](wire-protocol-streaming.md)　[设置 token 预算、步数与运行并发](../operations/token-step-concurrency-limits.md)　[检查生效配置与排查启动故障](../operations/configuration-diagnostics.md)
