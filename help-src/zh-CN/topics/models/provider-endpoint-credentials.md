---
{
  "id": "provider-endpoint-credentials",
  "title": "接入模型服务：端点、凭据、协议与流式",
  "category": "models",
  "summary": "把 OpenAI 兼容服务或原生 Anthropic 接到 OC 上：填好端点和模型名，弄清 OC 会选哪个 API key，选对调用协议，并按需打开流式接收。",
  "aliases": [
    "换模型",
    "第三方 API",
    "本地模型",
    "中转地址",
    "OpenAI compatible",
    "Anthropic",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DASHSCOPE_API_KEY",
    "--model",
    "--provider",
    "--base-url",
    "401",
    "接本地 vLLM",
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
    "OPENCOLLAB_PROVIDER",
    "OPENCOLLAB_BASE_URL",
    "OPENCOLLAB_MODEL",
    "OPENCOLLAB_API_KEY",
    "api_key",
    "auth_token",
    "base_url",
    "OPENCOLLAB_WIRE_PROTOCOL",
    "wire_protocol",
    "chat_completions",
    "responses",
    "OPENCOLLAB_LLM_STREAM_CHAT",
    "llm_stream_chat"
  ],
  "questions": [
    "怎样把自己的 OpenAI 兼容服务接进 OpenCollab？",
    "设置了 API key 还是 401，OC 到底用的是哪个 key？",
    "将 OpenAI 兼容或原生 Anthropic 端点接入 OpenCollab，并选对模型和认证方式？",
    "我的服务该用 chat_completions 还是 responses？",
    "怎样在 OC 里看到流式的 reasoning 文本？",
    "流式输出停住不动，该调哪个超时？",
    "为端点选择正确的 Chat Completions、Responses 或 Anthropic Messages 路径，并配置流式接收？"
  ],
  "related": [
    "configuration-precedence",
    "thinking-reasoning-sampling",
    "model-routing-request-capacity",
    "timeouts-retries"
  ],
  "verified_ref": "0c47f74a"
}
---

# 接入模型服务：端点、凭据、协议与流式

OC 只认两种服务类型（provider）：`openai` 覆盖所有说 OpenAI 协议的服务（官方、第三方中转、本地兼容服务），`anthropic` 走原生 Anthropic Messages。接入一个服务只需要四项：provider、地址（base URL）、模型名、API key。这篇依次讲这四项怎么填、OC 怎样挑选 API key、用哪种调用协议，以及什么时候打开流式接收。

## 配置一个端点

1. 确认服务提供的是哪种接口，选 `openai` 或 `anthropic`。
2. 把下面四行写进当前使用的 `configs/.env`，替换占位值。
3. `base_url` 填服务文档给的 API 基地址。模型名必须是该端点实际开放的名称，OC 不会替你映射。

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_BASE_URL=https://your-endpoint.example/v1
OPENCOLLAB_MODEL=your-model-id
OPENCOLLAB_API_KEY=your-api-key
```

不写地址时用 provider 的默认值，也可以用备用变量；通用的 `OPENCOLLAB_BASE_URL` 优先于备用变量：

| provider | 不写地址时的默认值 | 备用变量 |
| --- | --- | --- |
| `openai` | [OpenAI API 基地址](https://api.openai.com/v1) | `OPENAI_BASE_URL` |
| `anthropic` | [Anthropic API 基地址](https://api.anthropic.com) | `ANTHROPIC_BASE_URL` |

换成原生 Anthropic：把 provider 改成 `anthropic`，填服务支持的 Anthropic 模型名，按下一节配置凭据。

### 在程序里连接本地兼容服务

本地服务和远程服务的配置方式一样，都是 OpenAI 兼容。先启动服务、设好环境变量，再创建客户端：

```python
import os
from opencollab import OpenCollab

client = OpenCollab(
    ".",
    provider="openai",
    base_url=os.environ["MODEL_BASE_URL"],
    model=os.environ["MODEL_NAME"],
    api_key=os.environ.get("MODEL_API_KEY"),
)
print(client.configuration["provider"])
print(client.configuration["model"])
```

要同时接多个服务，用逻辑路由给每个服务起个名字，各自带端点和凭据，见 [模型路由与共享请求容量](model-routing-request-capacity.md)。

## OC 用哪个 API key

没有显式传 key 时，OC 按下表的顺序自动挑选，空字符串和空白值当作没设置：

| 使用路径 | 自动选择顺序 |
| --- | --- |
| 普通 OpenAI 兼容端点 | `OPENCOLLAB_API_KEY`，再 `OPENAI_API_KEY` |
| DashScope 兼容地址 | `DASHSCOPE_API_KEY`，再 `OPENCOLLAB_API_KEY` |
| 原生 Anthropic | `ANTHROPIC_API_KEY`，再 `ANTHROPIC_AUTH_TOKEN`，再 `OPENCOLLAB_API_KEY` |

这里有一个容易踩的坑：**表里的变量顺序跨来源生效**。同一个变量同时在 shell 和文件里时，用 shell 的值；但不同变量之间先按表里的顺序比，所以文件里的专用 key（如 `DASHSCOPE_API_KEY`）可以压过 shell 里导出的通用 key。切换服务后出现认证错误，先查旧服务的专用变量是否还在。

显式设置的优先级更高：

1. CLI 的 `--api-key` 或 SDK 的 `api_key=`，最高。
2. 其次是 SDK `config` 里显式的 `auth_token`，它优先于环境里的凭据。`auth_token` 用于原生 Anthropic 的 bearer 认证，会放进 `Authorization` 请求头。
3. 最后才是上表的自动选择。

凭据文件留在本地。排查时用 OC 输出的生效配置，它已经去掉了秘密字段。成员在容器或 VM 里运行自己写的子 Workflow 时，凭据要留在控制端，见 [凭据和配置放在哪](../workflow/team-run-workflow.md#凭据和配置放在哪)。

## 选择调用协议

`wire_protocol` 决定 OC 用哪种 API 跟模型说话：

| 你的服务 | `provider` | `wire_protocol` | 实际使用 |
| --- | --- | --- | --- |
| OpenAI 兼容（默认） | `openai` | `chat_completions` | Chat Completions |
| OpenAI 兼容，且提供 Responses API | `openai` | `responses` | Responses |
| 原生 Anthropic | `anthropic` | 保持 `chat_completions` | Messages（OC 按 provider 自动选择） |

原生 Anthropic 不靠这个字段，配 `responses` 会在配置校验时报错。切到 Responses：把 `OPENCOLLAB_WIRE_PROTOCOL` 改成 `responses`，并确认端点确实支持这个 API。SDK 里写作 `config={"wire_protocol": "responses"}`：

```python
from opencollab import OpenCollab

client = OpenCollab(
    ".",
    provider="openai",
    config={"wire_protocol": "responses"},
)
print(client.configuration["wire_protocol"])
```

逻辑路由也有自己的 `wire_protocol` 字段，每条路由可以选不同的协议。

协议相关的报错：服务返回“接口不存在”，先检查基地址和协议选择；错误说某个请求字段无效，再检查该模型在目标端点上的能力和覆盖设置。

## 开启 Chat 流式接收

OC 的 Chat 请求默认**不用**流式。需要接收流式的 reasoning 文本时，加上：

```dotenv
OPENCOLLAB_LLM_STREAM_CHAT=true
OPENCOLLAB_LLM_FIRST_EVENT_TIMEOUT=180
OPENCOLLAB_LLM_STREAM_IDLE_TIMEOUT=180
```

开启后：

- Chat 请求会带上 `stream` 和 `stream_options`，服务必须接受这两个字段。
- 收到的 reasoning 文本会写进轨迹。
- 只有同时收到终止的 `finish_reason` 和 token 用量，OC 才认为这次流正常结束。终端已经显示出文字时，可能还在等尾部的统计数据，以运行状态为准。
- 这个开关只对 Chat Completions 有效；Responses 的流式行为由所选模型的能力设置控制。

打开流式之后才出错，先检查服务是否支持 `stream_options`、尾部用量和终止事件，再结合停止原因和轨迹定位。流在哪个阶段卡住、该调哪个超时，见 [按卡住的位置选超时](../operations/timeouts-retries.md#模型请求按卡住的位置选超时)。

## 工具调用之后的续轮数据

工具返回后模型要接着生成，OC 需要按协议把上一轮的推理数据回传：

| 协议 | OC 保留什么 |
| --- | --- |
| Chat Completions | 启用 thinking 续轮、或模型协议要求回传推理内容时，保留历史 `reasoning_content`。已声明需要回传的 DeepSeek 模型，即使关掉了新一轮 thinking 也照样保留。其他 Chat 请求只用普通 assistant 内容和工具调用 |
| Responses | response items、provider 身份和对应的 response session 信息 |
| 原生 Anthropic | 带签名的 thinking blocks 及原始顺序，工具返回后按协议重放 |

如果你自己写了传输包装器或网关，也要完整保留这些数据，否则模型没法在工具返回后接着生成。

## 遇到启动和请求错误

| 现象 | 先查什么 |
| --- | --- |
| CLI 启动时提示缺少凭据 | 提示会说明所选 provider 缺哪个凭据 |
| 返回 401 | 实际被选中的认证变量是否符合服务要求（对照上面的表） |
| 返回模型未知或接口不存在 | 模型名和协议路径 |

各阶段能查出什么：创建客户端时只检查配置类型；离线模型检查读取内置能力表；能不能连上，只能通过一次真实请求观察。排查步骤见 [启动出错怎么查](configuration-precedence.md#启动出错按阶段排查)。报告问题时，记下状态码、出错阶段，以及去掉凭据后的生效配置。

## 延伸阅读

- 源码依据：[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/configs/README.md) · [opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/bootstrap/config.py) · [opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/opencollab/adapters/cli/main.py) · [docs/model-routing.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/model-routing.md) · [docs/model-inspection.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/model-inspection.md) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/0c47f74a27441888286a22012b23b658185f8b8f/docs/evaluation-runtime.md)
- 相关指南：[配置从哪来、哪个生效](configuration-precedence.md) · [深度思考与采样](thinking-reasoning-sampling.md) · [模型路由与共享请求容量](model-routing-request-capacity.md) · [运行怎样停下](../operations/timeouts-retries.md)
