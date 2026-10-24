---
{
  "id": "configuration-precedence",
  "title": "配置从哪里读取以及哪一项生效",
  "category": "models",
  "summary": "改了配置却没生效时，看这篇弄清 OpenCollab 按什么顺序读取设置。",
  "aliases": [
    "改了配置没生效",
    "环境变量覆盖",
    "读错 env",
    "当前目录",
    "workspace",
    "空字符串",
    "unknown configuration",
    "配置不生效",
    "指定配置文件"
  ],
  "keywords": [
    "配置优先级",
    "OPENCOLLAB_CONFIG_FILE",
    "configs/.env",
    ".env",
    "OpenCollab.configuration",
    "config",
    "build_config"
  ],
  "questions": [
    "我改了 .env 里的模型，为什么还是用旧模型？",
    "怎样让 OpenCollab 只读我指定的那个配置文件？",
    "SDK 里传的参数和环境变量谁优先？",
    "解释某项配置为什么生效，并稳定地让 CLI 或 SDK 读取选定配置？"
  ],
  "related": [
    "configuration-reference",
    "provider-endpoint-credentials",
    "model-routing-request-capacity",
    "configuration-diagnostics"
  ],
  "verified_ref": "0c47f74a"
}
---

# 配置从哪里读取以及哪一项生效

OC 的客户端配置可以来自 CLI 选项、SDK 构造参数、进程环境变量和 env 文件，最终每项只取一个值。这篇说明取值顺序、默认会搜索哪些 env 文件，以及怎样用 `OPENCOLLAB_CONFIG_FILE` 锁定单个文件。和很多工具不同的是，OC 默认会把多个 env 文件按变量逐个合并，所以“改了 `configs/.env` 却没生效”通常是别处有同名变量。

## 优先顺序一览

从高到低：

| 顺序 | 来源 | 例子 |
| --- | --- | --- |
| 1 | 显式 CLI 选项、SDK 构造参数 | `--model`、`OpenCollab(model=...)` |
| 2 | 进程环境变量（shell 里 `export` 的变量） | `export OPENCOLLAB_MODEL=...` |
| 3 | 配置文件里的值 | `configs/.env` 中的 `OPENCOLLAB_MODEL=...` |
| 4 | 内置默认值 | 见 [配置参数与环境变量速查](configuration-reference.md) |

排查时先确认你是怎么启动的（CLI 还是 SDK、带了哪些选项），再逐个变量看它来自哪一层。

注意：`OpenCollab(...)` 在创建客户端那一刻读取配置。之后再改环境变量或文件，已有的客户端不会变，需要重新创建一个。

## 改了配置没生效，按这几步查

1. 如果 shell 里导出过同名变量（例如 `echo $OPENCOLLAB_MODEL` 有输出），它会压过文件里的值。先 `unset` 掉，或者直接改它。
2. 如果你用的是 SDK，检查代码里是否给 `OpenCollab(...)` 传了同名参数。
3. 检查是不是有多个配置文件同时参与（见下一节）。
4. 打印最终结果核对（见 [查看最终结果](#查看最终结果)）。

## 工作区和当前目录共同参与搜索

工作区（workspace）：OC 运行时操作的项目目录，CLI 用 `--workspace` 指定，SDK 是 `OpenCollab(...)` 的第一个参数。它可以和进程当前目录不同。

没有指定配置文件时，OpenCollab 按下面的顺序找文件：

1. 工作区的 `configs/.env`
2. 工作区的 `.env`（兼容旧写法）
3. 如果当前目录与工作区不同：当前目录的 `configs/.env`
4. 再然后：当前目录的 `.env`

每个变量取第一次出现的非空值。后面的文件只补充前面没设置过的变量。所以一次运行可能把几个文件里的设置拼在一起用。

举例：在 OpenCollab 源码目录执行 `opencollab --workspace /path/to/application`。项目里的模型名，可以和源码目录文件里的超时设置同时生效。shell 里已经导出的同名变量优先级更高。

### 只读一个指定的文件

如果想避免多个文件混在一起，就用 `OPENCOLLAB_CONFIG_FILE` 指定一个文件。这个文件必须已经存在，是普通的 UTF-8 文本文件。

```bash
# 只读取这一个 env 文件，跳过默认的文件搜索
OPENCOLLAB_CONFIG_FILE=/absolute/path/project.env \
  opencollab --workspace /path/to/application
```

这样设置后，默认的文件搜索就不再进行。进程环境变量和显式参数仍按各自的优先级生效。相对路径会按进程的当前目录来解释，自动化脚本里建议写绝对路径，方便核对。

## 在 SDK 中明确覆盖

`OpenCollab` 构造参数中的 `model`、`provider`、`api_key` 和 `base_url` 会覆盖 `config` 里的同名值。其余运行设置放进 `config` 字典传入。

下面的代码创建一个客户端并打印最终配置。把占位模型名换成你所用服务支持的名称。

```python
from opencollab import OpenCollab

client = OpenCollab(
    ".",
    model="your-model-id",  # 覆盖 env 文件和环境变量里的模型
    config={
        "temperature": 0.0,  # 0 也是有效值，会正常覆盖
        "budget": 100_000,
        "llm_timeout": 300,
    },
)
print(dict(client.configuration))
```

几点要注意：

- `config` 里用的是字段名（小写），比如 `llm_timeout`。env 文件里用的是环境变量名，比如 `OPENCOLLAB_LLM_TIMEOUT`。
- 如果把环境变量名直接写进 `config`，会报未知字段错误。
- 创建客户端时只检查本地配置。真正调用运行入口时，才会向模型端点发请求。

## 空值、类型和认证的特殊情况

### 空值和取值范围

- 空白的凭据、空白的 base URL 都当作没设置。
- 文件里值为空白的变量会被跳过，继续往后找。
- `budget` 必须是正整数。
- 模型请求超时必须是有限的正数。
- `temperature` 范围是 0 到 2。
- SDK 的 `config` 和团队 YAML 遇到未知字段会报错。env 文件里可以有其它程序的变量，OpenCollab 会忽略它们。

### 认证凭据有自己的顺序

API key 还会按 provider 走一套专门的顺序：

- 普通 OpenAI 兼容端点：先通用 key，再 `OPENAI_API_KEY`。
- DashScope：优先 `DASHSCOPE_API_KEY`。
- 原生 Anthropic：优先它自己的 key 或 bearer token。

这个变量顺序跨来源生效。例如文件里的 `DASHSCOPE_API_KEY`，会优先于 shell 里导出的通用 key。显式传入的 `api_key` 优先级最高。完整规则见 [接入模型端点与认证凭据](provider-endpoint-credentials.md)。

## 查找参数名与默认值

[配置参数与环境变量速查](configuration-reference.md) 对照列出了环境变量、SDK 字段和内置默认值，并指向各运行入口的详细指南。要设置角色工具、Team 上下文或一次任务的期限，从那张表里找到对应入口继续配置。

## 查看最终结果

`client.configuration` 显示客户端最终采用的设置。它是只读的，改它不会影响客户端内部配置。

它包含模型、provider、协议、预算、窗口、采样和传输设置。凭据会被去掉，不会打印出来。端点地址用 `base_url_sha256`（地址的哈希值）表示。打印你关心的字段，就能看到新客户端用了哪些值。如果配置了逻辑路由，还能看到逻辑名称对应哪个实际模型。

## 来源

[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L19)　[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L47)　[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L367)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L110)

## 相关指南

[接入模型端点与认证凭据](provider-endpoint-credentials.md)　[逻辑模型路由与共享请求容量](model-routing-request-capacity.md)　[检查生效配置与排查启动故障](../operations/configuration-diagnostics.md)
