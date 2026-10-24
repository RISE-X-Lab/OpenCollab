---
{
  "id": "configuration-diagnostics",
  "title": "检查生效配置与排查启动故障",
  "category": "operations",
  "summary": "按报错阶段选检查入口：读客户端生效配置、离线查模型能力、检查团队和工作流加载、发一次真实请求测连接。",
  "aliases": [
    "怎么排错",
    "缺 API key",
    "模型不支持",
    "参数没生效",
    "unknown workflow",
    "unknown configuration",
    "连接测试",
    "context length exceeded",
    "exception_chain"
  ],
  "keywords": [
    "OpenCollab.configuration",
    "inspect_model_runtime",
    "profile_tool_names",
    "team show",
    "check_dashscope.py",
    "Cannot load configuration",
    "RunResult.agent_failures"
  ],
  "questions": [
    "改了配置为什么没生效？",
    "怎么不发请求就查看某个模型的上下文窗口和错误分类？",
    "unknown workflow 或根选项位置错误怎么处理？",
    "怎么测试端点和 API key 能不能连通？"
  ],
  "related": [
    "configuration-precedence",
    "provider-endpoint-credentials",
    "thinking-reasoning-sampling",
    "timeouts-retries",
    "trace-and-events",
    "run-results",
    "collaboration-adherence"
  ],
  "verified_ref": "0c47f74a"
}
---

# 检查生效配置与排查启动故障

这篇是 OC 启动失败时的排错索引。先看错误出在哪个阶段，再用对应的本地检查入口：读客户端生效配置、离线查模型能力表、检查团队和工作流加载、最后才发一次真实请求测连接。执行开始之后的问题，看 `RunResult.reason`、子 Agent 故障记录和轨迹。

## 按症状找入口

| 症状 | 先做什么 |
| --- | --- |
| `Cannot load configuration` 或参数没生效 | [读取客户端生效配置](#先读取客户端生效设置)，再看[格式错误](#处理常见格式与入口错误) |
| 根选项位置错误 | 把模型、预算选项放到 `workflow run NAME` 后面 |
| `unknown workflow` | 用同一工作区执行 `workflow list`，核对 `OPENCOLLAB_WORKFLOWS_DIR` |
| 模型不支持、`context length exceeded`、限流 | [离线查看模型能力](#离线查看模型和角色能力) |
| 配置看起来都对但请求失败 | [显式执行一次连接检查](#显式执行一次连接检查) |
| 运行中途失败 | [保留故障记录](#保留故障记录) |

## 先读取客户端生效设置

构造 `OpenCollab` 客户端时会合并工作区、进程环境变量、env 文件和显式覆盖。下面的程序只查询本地配置，不发请求，适合确认修改是否进入了新客户端。

```python
from opencollab import OpenCollab

client = OpenCollab(".")
settings = client.configuration  # 只读、已筛选的元数据
for key in (
    "provider", "model", "wire_protocol", "context_window",
    "llm_timeout", "llm_max_retries", "provider_error_time_budget",
):
    print(key, settings.get(key))
```

- `configuration` 不含凭据。端点用 `base_url_sha256` 标识。嵌套值与内部配置隔离。
- 改了文件但值没变，依次检查：进程里是否有同名环境变量、工作区搜索顺序、客户端是否还是修改前构造的旧实例。

## 离线查看模型和角色能力

`inspect_model_runtime` 查询已安装版本的本地能力表，不发请求。它还能把错误样例交给现有分类器，看 OC 会把某条报错归为上下文溢出还是可重试错误。下面脚本输出窗口、历史阈值、错误分类和 Base profile 的默认工具。

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

能力字段描述的是安装版本的默认值。显式设置的窗口和本次 reasoning 配置仍以客户端 `configuration` 为准。实际运行时用到的工具从 `metrics["agent_tool_names"]` 核对。

团队角色、关系和工作流可以先用本地命令检查。工作流列表按工作区目录加载调用方模块，名字冲突会报错。

```bash
opencollab team show --team-config team.yaml
opencollab workflow list --workspace /path/to/application
opencollab workflow run duo --help
```

在自己的 Python 程序里读团队声明，用 `opencollab.teams`。公开查询函数按团队加载规则返回角色名、声明工具、profile、角色额度和上下文策略，入口角色排第一。配置编辑器可以用它展示待运行的团队。实际运行后，再从成员记录确认工具过滤和交接行为，完整示例见[检查团队是否真的按分工协作](../observability/collaboration-adherence.md)。

## 处理常见格式与入口错误

- SDK `config` 用小写字段名，未知字段直接拒绝。
- 团队 YAML 按 schema 检查，字段拼错就改正。
- `thinking_params` 和 CLI `--args` 必须是 JSON 对象。数组和残缺 JSON 会失败。
- 预算是正整数，超时是有限正数。
- 空凭据、空地址都当作未设置。
- 模型和预算选项属于具体运行入口。`workflow run NAME --workspace PATH --model MODEL` 是正确位置。把它们放在 `workflow` 前面会触发根选项位置错误。
- 工作流名称未知时，先用相同工作区执行 `workflow list`，再核对 `OPENCOLLAB_WORKFLOWS_DIR`。

## 显式执行一次连接检查

端点和凭据都准备好后，用源码里的 `scripts/check_dashscope.py` 按常规配置发一次真实 completion。脚本名来自最初用途，实际走的是当前选中的 provider。在 OpenCollab 源码根目录执行，`--workspace` 指向要读取配置的项目：

```bash
uv run python scripts/check_dashscope.py --workspace /path/to/application
```

它会打印服务返回的一段文本，结束时关闭客户端。注意这一步会产生真实请求和用量，前面的离线能力查询只读本地元数据。

## 保留故障记录

运行结束后保留 `status`、`reason`、`metrics` 和产物路径。

- `agent_failures`：已脱敏的子 provider 故障类型、HTTP 状态等。
- `exception_chain`：类型、模块、整数 errno 和状态码，适合定位嵌套异常或 OS 故障。

用同一个 `run_id` 关联会话、manifest 和轨迹，再检查对应的模型调用和清理记录。

## 来源

[docs/model-inspection.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/model-inspection.md#L1)　[opencollab/sdk/client.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/sdk/client.py#L156)　[scripts/check_dashscope.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/scripts/check_dashscope.py#L24)　[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md#L367)

## 相关指南

[配置从哪里读取以及哪一项生效](../models/configuration-precedence.md)　[接入模型端点与认证凭据](../models/provider-endpoint-credentials.md)　[配置深度思考与采样参数](../models/thinking-reasoning-sampling.md)　[超时、重试与请求卡住诊断](timeouts-retries.md)　[查看 Agent 做了什么](../observability/trace-and-events.md)　[读取运行结果与判断任务是否完成](../observability/run-results.md)
