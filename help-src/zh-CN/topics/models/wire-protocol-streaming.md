---
{
  "id": "wire-protocol-streaming",
  "title": "选择调用协议与流式响应",
  "category": "models",
  "summary": "选对 OC 发请求用的协议（Chat Completions、Responses 或 Anthropic Messages），并按需打开 Chat 流式接收。",
  "aliases": [
    "协议选哪个",
    "Responses API",
    "Messages API",
    "流式输出",
    "stream_options",
    "reasoning_content",
    "finish_reason",
    "首包超时",
    "接口不存在",
    "流式卡住"
  ],
  "keywords": [
    "OPENCOLLAB_WIRE_PROTOCOL",
    "wire_protocol",
    "chat_completions",
    "responses",
    "OPENCOLLAB_LLM_STREAM_CHAT",
    "llm_stream_chat"
  ],
  "questions": [
    "我的服务该用 chat_completions 还是 responses？",
    "怎样在 OC 里看到流式的 reasoning 文本？",
    "流式输出停住不动，该调哪个超时？",
    "为端点选择正确的 Chat Completions、Responses 或 Anthropic Messages 路径，并配置流式接收？"
  ],
  "related": [
    "provider-endpoint-credentials",
    "thinking-reasoning-sampling",
    "timeouts-retries",
    "configuration-diagnostics"
  ],
  "verified_ref": "0c47f74a"
}
---

# 选择调用协议与流式响应

`wire_protocol` 决定 OC 用哪种 API 跟模型说话。默认 `chat_completions`，OpenAI 兼容端点提供 Responses API 时可改成 `responses`。原生 Anthropic 不靠这个字段，而是由 `provider=anthropic` 自动走 Messages。OC 的 Chat 默认不用流式，需要流式 reasoning 增量或流事件时，单独打开 `llm_stream_chat`。

## 按服务选择协议

| 你的服务 | `provider` | `wire_protocol` | 实际使用 |
| --- | --- | --- | --- |
| OpenAI 兼容（默认） | `openai` | `chat_completions` | Chat Completions |
| OpenAI 兼容且提供 Responses API | `openai` | `responses` | Responses |
| 原生 Anthropic | `anthropic` | 保持 `chat_completions` | Messages（OC 按 provider 选择） |

原生 Anthropic 配 `responses` 会在配置校验时报错。

### env 文件写法

Chat Completions 是默认值，也可以显式写出来。模型名和地址换成你的服务提供的值。

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_WIRE_PROTOCOL=chat_completions
OPENCOLLAB_BASE_URL=https://your-endpoint.example/v1
OPENCOLLAB_MODEL=your-model-id
```

切到 Responses：把 `OPENCOLLAB_WIRE_PROTOCOL` 改成 `responses`，并确认端点支持这个 API。

### SDK 写法

下面的代码创建客户端并打印选中的协议。真正调用运行入口时，会用已配置的凭据。

```python
from opencollab import OpenCollab

client = OpenCollab(
    ".",
    provider="openai",
    config={"wire_protocol": "responses"},
)
print(client.configuration["wire_protocol"])
```

逻辑路由也有自己的 `wire_protocol` 字段，每条路由可以选不同的协议。模型能力在解析出物理模型之后才确定，端点实际支持哪些功能，还要对照服务配置确认。

### 协议相关的报错

- 如果服务返回接口不存在，先检查 API 基地址和协议选择。
- 如果错误说某个请求字段无效，再检查该模型在目标端点上的能力声明和覆盖设置。

## 开启 Chat 流式接收

需要接收流式 reasoning 文本时，加上下面的设置：

```dotenv
OPENCOLLAB_LLM_STREAM_CHAT=true
OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=180
OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=180
```

开启后的行为：

- Chat 请求会带上 `stream` 和 `stream_options`，服务必须接受这两个字段。
- 收到的 reasoning 文本会写进轨迹（trace）。
- 只有同时收到终止的 `finish_reason` 和 token usage，OC 才认为这次流正常结束。终端已经显示出文字时，可能还在等尾部的统计数据，以运行状态为准。
- 这个开关只对 Chat Completions 有效。Responses 的流式行为由所选模型的能力设置控制。

## 识别流式中断的位置

按卡住的阶段对应超时设置（单位都是秒）：

| 卡在哪个阶段 | 看哪个设置 |
| --- | --- |
| 一直连不上 | 连接超时 `llm_connect_timeout` |
| 已连接，但没收到第一个事件 | `llm_first_event_timeout` |
| 收到部分内容后不再推进 | `llm_stream_idle_timeout`，同时保留请求级 `llm_timeout` |

整次 SDK 任务的 `timeout` 另外设置，与上面这些无关。

进一步排查：

- 离线模型检查可以看安装版本里的能力默认值。
- `configuration` 显示客户端实际生效的协议和传输参数。
- 如果打开流式之后才出错，检查服务是否支持 `stream_options`、尾部 usage 和终止事件，再结合停止原因和轨迹定位。

## 保留工具调用之间的续轮信息

工具调用返回后，模型要继续生成，OC 需要把上一轮的推理相关数据按协议回传：

| 协议 | OC 保留什么 |
| --- | --- |
| Chat Completions | 启用 thinking 续轮，或模型协议要求回传推理内容时，保留历史 `reasoning_content`。已声明需要回传的 DeepSeek 模型，即使关掉了新一轮 thinking，也照样保留。其它 Chat 请求只用普通 assistant 内容和工具调用 |
| Responses | response items、provider 身份和对应的 response session 信息 |
| 原生 Anthropic | 带签名的 thinking blocks 及原始顺序，工具返回后按协议重放 |

如果你自己写了传输包装器或网关，也要完整保留这些数据，否则模型无法在工具返回后接着生成。

## 来源

[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L70)　[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L115)　[docs/model-inspection.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/model-inspection.md#L34)　[docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md#L182)

## 相关指南

[接入模型端点与认证凭据](provider-endpoint-credentials.md)　[配置深度思考与采样参数](thinking-reasoning-sampling.md)　[超时、重试与请求卡住诊断](../operations/timeouts-retries.md)　[检查生效配置与排查启动故障](../operations/configuration-diagnostics.md)
