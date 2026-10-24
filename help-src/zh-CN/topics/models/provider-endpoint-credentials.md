---
{
  "id": "provider-endpoint-credentials",
  "title": "接入模型端点与认证凭据",
  "category": "models",
  "summary": "把 OpenAI 兼容服务或原生 Anthropic 服务接到 OC 上，并弄清 OC 会用哪个 API key。",
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
    "接本地 vLLM"
  ],
  "keywords": [
    "OPENCOLLAB_PROVIDER",
    "OPENCOLLAB_BASE_URL",
    "OPENCOLLAB_MODEL",
    "OPENCOLLAB_API_KEY",
    "api_key",
    "auth_token",
    "base_url"
  ],
  "questions": [
    "怎样把自己的 OpenAI 兼容服务接进 OpenCollab？",
    "设置了 API key 还是 401，OC 到底用的是哪个 key？",
    "将 OpenAI 兼容或原生 Anthropic 端点接入 OpenCollab，并选对模型和认证方式？"
  ],
  "related": [
    "configuration-precedence",
    "wire-protocol-streaming",
    "model-routing-request-capacity",
    "configuration-diagnostics"
  ],
  "verified_ref": "0c47f74a"
}
---

# 接入模型端点与认证凭据

OC 只有两种 provider：`openai` 覆盖所有 OpenAI 协议的服务（官方、第三方中转、本地兼容服务），`anthropic` 走原生 Anthropic Messages。接入一个端点只需四项：provider、base URL、模型名、API key。要注意的是 OC 自动挑选 API key 时按 provider 有固定的变量顺序，而且专用变量（如 `DASHSCOPE_API_KEY`）可以压过 shell 里的通用 key。

## 配置一个端点

1. 确认服务提供的是哪种接口，选 `openai` 或 `anthropic`。
2. 把下面四行写进当前选中的 `configs/.env`，替换占位值。
3. `base_url` 填服务文档给的 API 基地址。模型名必须是该端点实际开放的名称，OC 不会替你映射。

```dotenv
OPENCOLLAB_PROVIDER=openai
OPENCOLLAB_BASE_URL=https://your-endpoint.example/v1
OPENCOLLAB_MODEL=your-model-id
OPENCOLLAB_API_KEY=your-api-key
```

OC 会按 provider、协议和模型能力来构造请求。

### 默认地址和备用变量

| provider | 不写 base URL 时的默认值 | 备用变量 |
| --- | --- | --- |
| `openai` | [OpenAI API 基地址](https://api.openai.com/v1) | `OPENAI_BASE_URL` |
| `anthropic` | [Anthropic API 基地址](https://api.anthropic.com) | `ANTHROPIC_BASE_URL` |

通用的 `OPENCOLLAB_BASE_URL` 优先于备用变量。

### 换成原生 Anthropic 或 Responses

- 原生 Anthropic：把 provider 改成 `anthropic`，填服务支持的 Anthropic 模型名，按下一节的表配置凭据。
- Responses：需要 OpenAI 兼容端点明确提供 Responses API。协议设置见 [选择调用协议与流式响应](wire-protocol-streaming.md)。

## 选定认证来源

没有显式传 key 时，OC 按下表顺序自动挑选。空字符串和空白值当作没设置。

| 使用路径 | 自动选择顺序 |
| --- | --- |
| 普通 OpenAI 兼容端点 | `OPENCOLLAB_API_KEY`，再 `OPENAI_API_KEY` |
| DashScope 兼容地址 | `DASHSCOPE_API_KEY`，再 `OPENCOLLAB_API_KEY` |
| 原生 Anthropic | `ANTHROPIC_API_KEY`，再 `ANTHROPIC_AUTH_TOKEN`，再 `OPENCOLLAB_API_KEY` |

比较规则：

- 同一个变量同时在 shell 和文件里，用 shell 的值。
- 不同变量之间，先按上表的顺序比。这个顺序跨来源生效，所以文件里的专用 key 可以压过 shell 里的通用 key。
- 切换服务后出现认证错误，先查旧服务的专用变量是否还在。

显式设置的优先级更高：

1. CLI `--api-key` 或 SDK `api_key=`，最高。
2. 其次是 SDK `config` 中显式的 `auth_token`，它优先于环境里的凭据。`auth_token` 用于原生 Anthropic 的 bearer 认证，会放进 `Authorization` 请求头。
3. 最后才是上表的自动选择。

凭据文件留在本地。排查时用 OC 输出的有效配置，它已去掉秘密字段。

## 在程序里连接本地兼容服务

本地服务和远程服务的配置方式相同，都是 OpenAI 兼容。先启动服务、设好环境变量，再创建客户端。地址、模型名和认证要与你的部署一致。

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

要同时接多个服务，用 `model_routes` 给每个服务定义一条逻辑路由，各自带端点和凭据。角色填逻辑名称，实际请求发送的仍是路由里声明的物理模型名。还可以按后端限制请求并发。详见 [逻辑模型路由与共享请求容量](model-routing-request-capacity.md)。

## 遇到启动和请求错误

| 现象 | 先查什么 |
| --- | --- |
| CLI 启动时提示缺少凭据 | 提示里会说明所选 provider 缺哪个凭据 |
| 返回 401 | 实际被选中的认证变量是否符合服务要求（对照上面的表） |
| 返回模型未知或接口不存在 | 模型名和协议路径 |

各阶段能查出什么：

- 创建客户端：检查配置类型。
- 离线模型检查：读取内置能力表。
- 能不能连上：只能通过实际请求观察。

报告问题时，记下状态码、出错阶段，以及去掉凭据后的有效配置。

## 成员自编 Workflow 与远端环境

通过 `manage_workflow` 启动源码时，桥接层的模型端点、认证凭据和请求槽位保留在控制端，子进程接收模型名称和非敏感生成设置。工作区文件会按快照规则复制，把认证 env 文件放在项目之外，并通过 `OPENCOLLAB_CONFIG_FILE` 指定控制端的绝对路径。SSH 私钥由控制端 OpenSSH 使用，`identity_file` 引用该本地文件。操作步骤见 [VM 指南](../isolation/vm-environment-extension.md)。

## 来源

[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L31)　[opencollab/adapters/cli/main.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/cli/main.py#L61)　[opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/bootstrap/config.py#L124)　[docs/model-routing.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/model-routing.md#L47)

## 相关指南

[配置从哪里读取以及哪一项生效](configuration-precedence.md)　[选择调用协议与流式响应](wire-protocol-streaming.md)　[逻辑模型路由与共享请求容量](model-routing-request-capacity.md)　[检查生效配置与排查启动故障](../operations/configuration-diagnostics.md)
