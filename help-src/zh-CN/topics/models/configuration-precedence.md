---
{
  "id": "configuration-precedence",
  "title": "配置从哪来、哪个生效、出错怎么查",
  "category": "models",
  "summary": "配置从哪里读取、多个来源谁优先、为什么改了不生效；以及启动出错时按阶段排查：读生效配置、离线查模型能力、检查团队和 Workflow、再发一次真实请求。",
  "aliases": [
    "改了配置没生效",
    "环境变量覆盖",
    "读错 env",
    "当前目录",
    "workspace",
    "空字符串",
    "unknown configuration",
    "配置不生效",
    "指定配置文件",
    "怎么排错",
    "缺 API key",
    "模型不支持",
    "参数没生效",
    "unknown workflow",
    "连接测试",
    "context length exceeded",
    "exception_chain"
  ],
  "keywords": [
    "配置优先级",
    "OPENCOLLAB_CONFIG_FILE",
    "configs/.env",
    ".env",
    "OpenCollab.configuration",
    "config",
    "build_config",
    "inspect_model_runtime",
    "profile_tool_names",
    "team show",
    "check_dashscope.py",
    "Cannot load configuration",
    "RunResult.agent_failures"
  ],
  "questions": [
    "我改了 .env 里的模型，为什么还是用旧模型？",
    "怎样让 OpenCollab 只读我指定的那个配置文件？",
    "SDK 里传的参数和环境变量谁优先？",
    "解释某项配置为什么生效，并稳定地让 CLI 或 SDK 读取选定配置？",
    "改了配置为什么没生效？",
    "怎么不发请求就查看某个模型的上下文窗口和错误分类？",
    "unknown workflow 或根选项位置错误怎么处理？",
    "怎么测试端点和 API key 能不能连通？"
  ],
  "related": [
    "configuration-reference",
    "provider-endpoint-credentials",
    "model-routing-request-capacity",
    "report-problem-and-request-help"
  ],
  "verified_ref": "v0.9.6"
}
---

# 配置从哪来、哪个生效、出错怎么查

OC 的客户端配置可以来自四个地方：CLI 选项或 SDK 参数、进程环境变量、env 文件、内置默认值。最终每一项只取一个值。这篇讲清楚取值顺序、OC 默认会读哪些 env 文件，以及启动出错时怎样一层层排查。

先记住一个和很多工具不同的地方：**OC 默认会把多个 env 文件按变量逐个合并**。所以“改了 `configs/.env` 却没生效”，通常是别处有同名变量。

## 先分清设置写在哪一层

OC 的设置分三层，写错层会报错或不生效：

| 层级 | 管什么 | 写在哪 |
| --- | --- | --- |
| 客户端配置 | 连哪个模型、怎么认证、请求怎么发 | 环境变量、env 文件，或 `OpenCollab(config=...)` |
| 团队声明 | 有哪些角色、谁能联系谁 | 团队 YAML |
| 一次运行的参数 | 这次任务的预算、期限等 | `agent()`、`team()` 或 `workflow()` 的调用参数 |

本文讲的是第一层。每个配置项的名字和默认值，查 [配置参数速查](configuration-reference.md)。

## 优先顺序

从高到低：

| 顺序 | 来源 | 例子 |
| --- | --- | --- |
| 1 | 显式的 CLI 选项、SDK 构造参数 | `--model`、`OpenCollab(model=...)` |
| 2 | 进程环境变量（shell 里 `export` 的变量） | `export OPENCOLLAB_MODEL=...` |
| 3 | env 文件里的值 | `configs/.env` 中的 `OPENCOLLAB_MODEL=...` |
| 4 | 内置默认值 | 见 [配置参数速查](configuration-reference.md) |

还有一点：`OpenCollab(...)` 在**创建客户端的那一刻**读取配置。之后再改环境变量或文件，已有的客户端不会变，要重新创建一个。

## OC 会读哪些 env 文件

工作区（workspace）是 OC 运行时操作的项目目录：CLI 用 `--workspace` 指定，SDK 是 `OpenCollab(...)` 的第一个参数。它可以和进程的当前目录不同。

没有指定配置文件时，OC 按这个顺序找：

1. 工作区的 `configs/.env`
2. 工作区的 `.env`（兼容旧写法）
3. 当前目录与工作区不同时：当前目录的 `configs/.env`
4. 再然后：当前目录的 `.env`

**每个变量取第一次出现的非空值**，后面的文件只补充前面没设置过的变量。所以一次运行可能把几个文件里的设置拼在一起用。例如在 OC 源码目录执行 `opencollab --workspace /path/to/application`：项目里的模型名，可以和源码目录文件里的超时设置同时生效；shell 里导出的同名变量又比两者都优先。

### 只读一个指定的文件

想避免多个文件混在一起，就用 `OPENCOLLAB_CONFIG_FILE` 指定一个文件。它必须已经存在，是普通的 UTF-8 文本文件。

```bash
# 只读取这一个 env 文件，跳过默认的文件搜索
OPENCOLLAB_CONFIG_FILE=/absolute/path/project.env \
  opencollab --workspace /path/to/application
```

这样设置后，默认的文件搜索不再进行，进程环境变量和显式参数仍按各自的优先级生效。相对路径按进程的当前目录解释，自动化脚本里建议写绝对路径。把凭据文件放在项目之外、用这个变量指向它，也能避免凭据被复制进 Agent 的执行环境。

## 在 SDK 里明确覆盖

`OpenCollab` 的构造参数 `model`、`provider`、`api_key`、`base_url`（以及 `model_routes`）会覆盖 `config` 里的同名值；其余设置放进 `config` 字典：

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

- `config` 用**字段名**（小写），比如 `llm_timeout`；env 文件用**环境变量名**，比如 `OPENCOLLAB_LLM_TIMEOUT`。把环境变量名直接写进 `config`，会报未知字段错误。
- 创建客户端时只检查本地配置；真正调用运行入口时，才会向模型端点发请求。

## 特殊规则：空值、取值范围和凭据

- 空白的凭据、空白的地址都当作没设置；文件里值为空白的变量会被跳过，继续往后找。
- `budget` 必须是正整数；模型请求超时必须是有限的正数；`temperature` 范围是 0 到 2。
- SDK 的 `config` 和团队 YAML 遇到未知字段会报错；env 文件里可以有其他程序的变量，OC 会忽略。
- API key 还有一套按 provider 区分的变量顺序，而且这个顺序**跨来源**生效：比如文件里的 `DASHSCOPE_API_KEY` 会优先于 shell 里导出的通用 key。显式传入的 `api_key` 优先级最高。完整规则见 [OC 用哪个 API key](provider-endpoint-credentials.md#oc-用哪个-api-key)。

## 改了配置没生效，按这几步查

1. shell 里导出过同名变量吗（例如 `echo $OPENCOLLAB_MODEL` 有输出）？它会压过文件里的值，先 `unset` 掉，或者直接改它。
2. 用的是 SDK 的话，代码里是否给 `OpenCollab(...)` 传了同名参数？
3. 是不是有多个 env 文件同时参与？
4. 客户端是不是修改之前创建的旧实例？
5. 打印最终结果核对，见下一节。

## 查看最终生效的配置

`client.configuration` 显示客户端最终采用的设置。它是只读的，改它不会影响客户端内部。凭据会被去掉，端点地址用 `base_url_sha256`（地址的哈希值）表示；配置了逻辑路由时，还能看到逻辑名称对应哪个实际模型。

```python
from opencollab import OpenCollab

client = OpenCollab(".")
settings = client.configuration  # 只读、已去掉凭据
for key in (
    "provider", "model", "wire_protocol", "context_window",
    "llm_timeout", "llm_max_retries", "provider_error_time_budget",
):
    print(key, settings.get(key))
```

这段程序只读本地配置，不发请求，适合确认修改是否进入了新客户端。

## 启动出错：按阶段排查

先看错误出在哪个阶段，再用对应的检查入口。原则是：能离线查的先离线查，最后才发真实请求。

| 症状 | 先做什么 |
| --- | --- |
| `Cannot load configuration`，或参数没生效 | [读生效配置](#查看最终生效的配置)，再看下面的格式错误 |
| 提示根选项位置错误 | 把模型、预算等选项放到 `workflow run NAME` 后面 |
| `unknown workflow` | 用同一工作区执行 `workflow list`，核对 `OPENCOLLAB_WORKFLOWS_DIR` |
| 模型不支持、`context length exceeded`、限流 | 离线查看模型能力（下文） |
| 配置看起来都对，但请求失败 | 显式发一次连接检查（下文） |
| 运行到一半失败 | 保留故障记录（下文） |

### 常见的格式与入口错误

- SDK `config` 用小写字段名，未知字段直接拒绝；团队 YAML 按 schema 检查，字段拼错就改正。
- `thinking_params` 和 CLI `--args` 必须是 JSON 对象，数组和残缺 JSON 会失败。
- 预算是正整数，超时是有限正数；空凭据、空地址都当作未设置。
- 模型和预算选项属于具体的运行入口：`workflow run NAME --workspace PATH --model MODEL` 才是正确位置。

### 离线查看模型能力

`inspect_model_runtime` 查询已安装版本的本地能力表，不发请求。它还能把错误样例交给 OC 的分类器，看某条报错会被归为上下文溢出还是可重试错误。下面的脚本输出窗口、历史阈值、错误分类和 Base 配置的默认工具：

```python
from opencollab.models import inspect_model_runtime
from opencollab.tools import profile_tool_names

settings = inspect_model_runtime(
    "gpt-4o",  # 换成你的物理模型名
    overflow_samples=[{
        "label": "context",
        "message": "context length exceeded",
        "status": 400,
    }],
    retry_samples=[{
        "label": "concurrency",
        "class_name": "APIError",
        "message": "Concurrency limit exceeded for user",
    }],
)
print(settings["capabilities"])
print(settings["history"])
print(settings["overflow"])
print(settings["retry"])
print(profile_tool_names("base"))
```

能力字段描述的是安装版本的默认值；显式设置的窗口和本次 reasoning 配置仍以 `client.configuration` 为准。实际运行时用到的工具，从运行结果的 `metrics["agent_tool_names"]` 核对。

### 检查团队和 Workflow 能不能加载

```bash
opencollab team show --team-config team.yaml
opencollab workflow list --workspace /path/to/application
opencollab workflow run duo --help
```

Workflow 列表按工作区目录加载模块，名字冲突会报错。在自己的程序里读团队声明，用 `opencollab.teams` 的公开查询函数，它按团队加载规则返回角色名、声明工具、配置、角色额度和上下文策略，入口角色排第一，见 [检查团队是否真的按分工协作](../observability/collaboration-adherence.md)。

### 显式发一次连接检查

端点和凭据都准备好后，用源码里的 `scripts/check_dashscope.py` 按常规配置发一次真实请求。脚本名来自最初的用途，实际走的是当前选中的 provider。在 OC 源码根目录执行，`--workspace` 指向要读取配置的项目：

```bash
uv run python scripts/check_dashscope.py --workspace /path/to/application
```

它会打印服务返回的一段文本，结束时关闭客户端。注意这一步会产生真实请求和用量，而前面的离线查询只读本地数据。

### 保留故障记录

运行开始之后的问题，看 `RunResult.reason`、子 Agent 的故障记录和轨迹。运行结束后保留 `status`、`reason`、`metrics` 和产物路径：

- `agent_failures`：已脱敏的子 provider 故障类型、HTTP 状态等。
- `exception_chain`：类型、模块、整数 errno 和状态码，适合定位嵌套异常或系统故障。

用同一个 `run_id` 关联会话、清单和轨迹，再检查对应的模型调用和清理记录，见 [运行记录](../observability/artifacts-and-history.md)。需要求助时，见 [反馈问题](../architecture/report-problem-and-request-help.md)。

## 延伸阅读

- 源码依据：[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/configs/README.md) · [opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/sdk/client.py) · [opencollab/bootstrap/config.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/opencollab/bootstrap/config.py) · [docs/model-inspection.md](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/docs/model-inspection.md) · [scripts/check_dashscope.py](https://github.com/RISE-X-Lab/OpenCollab/blob/v0.9.6/scripts/check_dashscope.py)
- 相关指南：[配置参数速查](configuration-reference.md) · [接入模型服务](provider-endpoint-credentials.md) · [模型路由与共享请求容量](model-routing-request-capacity.md) · [运行怎样停下](../operations/timeouts-retries.md)
