---
{
  "id": "model-routing-request-capacity",
  "title": "模型路由与共享请求容量",
  "category": "models",
  "summary": "给一整套模型连接设置起个名字，让不同角色用不同服务；再用请求池限制同一台机器上同时发给模型服务的请求数。",
  "aliases": [
    "模型别名",
    "角色用不同模型",
    "多个本地模型",
    "共享并发",
    "多进程限流",
    "请求池",
    "多机网关",
    "容量冲突",
    "容器挂载",
    "逻辑模型",
    "限制并发请求"
  ],
  "keywords": [
    "model_routes",
    "OPENCOLLAB_MODEL_ROUTES",
    "logical_model",
    "api_key_env",
    "request_pool",
    "request_capacity",
    "OPENCOLLAB_REQUEST_POOL",
    "OPENCOLLAB_REQUEST_CAPACITY"
  ],
  "questions": [
    "怎样让规划角色和执行角色用不同的模型？",
    "多个进程同时跑，怎样限制同时打到本地模型的请求数？",
    "让不同角色或进程选择明确后端，并协调同一台机器上的模型请求容量？"
  ],
  "related": [
    "provider-endpoint-credentials",
    "configuration-precedence",
    "token-step-concurrency-limits",
    "team-run-workflow"
  ],
  "verified_ref": "v0.9.6"
}
---

# 模型路由与共享请求容量

这篇解决两个问题。第一，**让不同角色用不同的模型服务**：OC 的 `model_routes` 是一张逻辑模型表，你给一整套连接设置（物理模型、provider、协议、地址、凭据）起个名字，Team 角色或客户端用这个名字引用它。第二，**保护承受不了太多并发的模型服务**：每条路由可以挂一个请求池（`request_pool` + `request_capacity`），让同一台机器上多个独立的 Team 或 Workflow 进程，靠文件锁共享固定数量的并发请求名额。

请求记录和用量记录里写的是物理模型名，方便你看清实际调用了哪个模型。

## 定义一条逻辑路由

下面的代码创建一条名为 `worker` 的路由，并准备一个同机请求池目录。`MODEL_BASE_URL`、`MODEL_NAME` 和 `MODEL_API_KEY` 需要你事先设置好，创建客户端时会读取并检查凭据。

```python
import os
from pathlib import Path
from opencollab import OpenCollab

# 请求池目录：必须是已存在的绝对路径
pool = Path(".opencollab/request-pool").resolve()
pool.mkdir(parents=True, exist_ok=True)
client = OpenCollab(
    ".",
    model="worker",  # 默认使用名为 worker 的逻辑路由
    model_routes={
        "worker": {
            "model": os.environ["MODEL_NAME"],
            "provider": "openai",
            "wire_protocol": "chat_completions",
            "base_url": os.environ["MODEL_BASE_URL"],
            "api_key_env": "MODEL_API_KEY",  # pragma: allowlist secret
            "context_window": 131072,
            "request_pool": str(pool),
            "request_capacity": 2,  # 最多同时 2 个模型请求
        }
    },
)
print(client.configuration["model_routes"])
```

| 字段 | 作用 |
| --- | --- |
| `model` | 实际发给服务的物理模型名 |
| `provider`、`wire_protocol`、`base_url` | 这条路由用的服务类型、协议和地址 |
| `api_key_env` | 从哪个环境变量读 API key；会从进程环境或选中的 env 文件里读，读不到就报错 |
| `context_window` | 后端实际支持的 token 窗口，示例数值要按你的服务调整 |
| `request_pool`、`request_capacity` | 请求池目录和容量，见下一节 |

认证补充：路由里直接写的 `api_key` 优先于 `api_key_env`；原生 Anthropic 路由还可以用 `auth_token`；每条路由有自己独立的端点和凭据。

其他写法和规则：

- 同样的映射也可以放在 SDK `config` 的 `model_routes` 字段里，或者把 JSON 对象写进环境变量 `OPENCOLLAB_MODEL_ROUTES`。直接传给构造函数的参数优先。
- Team 角色的 `model` 可以填逻辑名称。名称不在映射里时，就当作普通物理模型名，用默认的 provider 配置。
- 路由只解析一次。即使某条路由的物理模型名恰好和另一条路由的逻辑名相同，也不会再跳转。

## 限制同一台机器上的请求数

### 怎么设置

1. 同时提供 `request_pool` 和 `request_capacity`，两者缺一不可。
2. `request_pool` 必须是已经存在的本地绝对路径目录。
3. 让所有要共享名额的进程指向同一个目录，它们靠操作系统文件锁协调。

`request_capacity=2` 表示最多同时有两个模型请求占着名额，还在排队的调用不会把请求发到后端。

也可以在顶层设置，对没有自己设置的模型生效：环境变量 `OPENCOLLAB_REQUEST_POOL` 和 `OPENCOLLAB_REQUEST_CAPACITY`，或 SDK 里同名的小写字段。路由里成对设置的值优先。多个模型名称指向同一个后端时，可以共用一个池。

### 名额什么时候占用、什么时候释放

- 请求拿到名额后，provider 重试期间一直占着。
- 模型请求完成，或者取消后的清理结束，才释放。
- **执行工具之前会先释放名额**。所以父 Agent 在等子任务时，子任务仍然能用上模型容量。
- 进程被终止时，操作系统会自动释放它持有的锁。

### 它和 Workflow 的 `concurrency` 不是一回事

| 设置 | 限制什么 | 按什么来设 |
| --- | --- | --- |
| Workflow 的 `concurrency` | 同时运行的 Agent 会话数 | 任务需要 |
| 请求池容量 | 同时进到后端的模型请求数 | 模型服务能承受的量 |

一个会话大部分时间可能在执行工具，并不占用模型请求，所以两者要分开配置。

## 修改容量，以及在容器里使用

**修改容量**：池目录里有一个 `capacity` 文件记录容量。不同进程给同一个池配置了不同容量，会报冲突错误。运行期间不要删池目录和其中的锁文件。要改容量：停掉所有用这个池的进程，清空池目录，再用新容量重新启动。

**在容器里使用**：

- 运行模型客户端的容器，必须把同一个宿主机目录挂载到配置里写的那个绝对路径。
- 两个容器各自建了一个同名目录，它们就是两个独立的池，名额不共享。
- 池路径是给运行 Python 模型客户端的进程用的。工具可能在另一个容器里执行，挂载时以模型客户端所在的环境为准。

## 多台机器共用一个服务

本地请求池只适用于同一台机器上、文件锁可靠的文件系统。多台机器之间的协调，要交给共用的后端或外部网关：

1. 让各路由的 `base_url` 都指向同一个网关。
2. 在网关上设置后端容量。
3. 确认网关支持你选的协议（Chat Completions、Responses 或 Messages），并原样保留模型身份、推理参数和续轮数据。

网络断开后服务端还在继续跑的任务，由后端自己的取消策略处理。

## 延伸阅读

- 源码依据：[docs/model-routing.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/model-routing.md) · [opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/config.py)
- 相关指南：[接入模型服务](provider-endpoint-credentials.md) · [配置从哪来、哪个生效](configuration-precedence.md) · [预算、步数与并发](../operations/token-step-concurrency-limits.md) · [让 Team 成员运行 Workflow](../workflow/team-run-workflow.md)
