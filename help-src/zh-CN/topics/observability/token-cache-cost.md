---
{
  "id": "token-cache-cost",
  "title": "统计 token、缓存命中和费用",
  "category": "observability",
  "summary": "开启用量账本，正确累加 token、算缓存命中率和估算花费，不重复也不漏算。",
  "aliases": [
    "usage",
    "成本",
    "reasoning_tokens",
    "cached_tokens",
    "API_USAGE_LOG",
    "输入输出token",
    "花了多少钱",
    "OPENCOLLAB_API_USAGE_LOG"
  ],
  "keywords": [
    "token统计",
    "缓存命中",
    "费用",
    "cost_usd"
  ],
  "questions": [
    "从实际用量记录计算开销，理解推理、缓存和重试对总量的影响？",
    "这次运行一共花了多少 token 和多少钱？",
    "缓存命中率怎么算？"
  ],
  "related": [],
  "verified_ref": "0c47f74a"
}
---

# 统计 token、缓存命中和费用

OC 区分两种用量数据：会话指标与预算（决定是否继续执行），以及提供方用量账本（`OPENCOLLAB_API_USAGE_LOG` 指定的 JSONL，每次提供方调用追加一行，含缓存、推理、估算费用和缺失标志）。统计费用以账本为准。本文给出开启方式、字段表、离线汇总脚本、价格配置变量，以及缓存、推理、重试、摘要和迟到用量的计算口径。

## 第一步：开启用量账本

用量账本是一个 JSONL 文件，每次提供方调用追加一行。在启动进程前设置写入位置：

```bash
export OPENCOLLAB_API_USAGE_LOG=/absolute/path/to/usage.jsonl
```

每行包含模型、协议、用量、`estimated`，以及可用时的 `raw_usage`。

其中 `usage` 里有这些字段：

| 字段 | 含义 |
| --- | --- |
| `input_tokens` | 输入 token |
| `output_tokens` | 输出 token |
| `total_tokens` | 输入加输出 |
| `cached_input_tokens` | 命中缓存的输入 token |
| `cache_creation_tokens` | 写入缓存的 token |
| `reasoning_tokens` | 推理 token |
| `cost_usd` | 估算费用（美元） |
| `pricing` | 计价所用的价格 |

- 值为 `None` 表示这项信息缺失。
- `estimated=True` 表示这条记录包含估算。
- trace 里对应缓存读取的字段名是 `cache_read_tokens`，和账本不同，解析时按各自结构读。

## 第二步：给记录打上标签

命令行批次用环境变量标记：

- `OPENCOLLAB_RUN_ID` 或 `OPENCOLLAB_USAGE_RUN_ID`：标记这次运行。
- `OPENCOLLAB_USAGE_LABEL`：追加实验标签。

这些值来自进程环境。

并发服务可以用每次 SDK 调用的 `run_id` 和 `RunControl.on_event` 关联业务请求。原运行结束后，迟到用量结算期间，回调仍会继续收到事件。用法见 [查看 Agent 做了什么](trace-and-events.md)。

## 第三步：离线汇总

下面脚本读取一份账本，报告记录数、累计 token 和缺失情况。想只看某个模型或时间段，先按每行的 `model`、`timestamp`、`run_id` 或 `label` 筛选。

```python
import json
from pathlib import Path

usage_path = Path("artifacts/usage.jsonl")
with usage_path.open(encoding="utf-8") as stream:
    records = [json.loads(line) for line in stream if line.strip()]
usages = [record["usage"] for record in records]
print("records", len(usages))
print("input_tokens", sum(usage["input_tokens"] for usage in usages))
print("output_tokens", sum(usage["output_tokens"] for usage in usages))
print("total_tokens", sum(usage["total_tokens"] for usage in usages))
print("estimated_records", sum(usage["estimated"] for usage in usages))
print("missing_cache_records", sum(usage["cached_input_tokens"] is None for usage in usages))
print("missing_cost_records", sum(usage["cost_usd"] is None for usage in usages))
```

汇总费用前：

1. 检查每条记录的 `pricing`。
2. 只汇总价格和用量都完整的记录。
3. 保留缺失记录数，说明汇总覆盖了多少次调用。

通用价格默认是零，所以看到零费用，要结合实际配置判断。缓存读取或写入字段缺失时，费用可能是 `None`。

## 这些数怎样相加

最容易出错的是重复计算：

- `total_tokens` 就是输入加输出。
- 命中缓存的输入已经算在输入里，不要再加一遍。
- 推理 token 通常是输出的一部分，也不要再加一遍。
- 提供方可能只返回部分字段，统计时保留缺失值。

缓存命中率 = 累计 `cached_input_tokens` ÷ 累计 `input_tokens`。

求多个任务的平均开销时，写明分母包含哪些任务、哪些尝试。

账本和会话指标是两个层面的记录。汇总前选定一层，别把同一次请求从两处各算一次。

## 配置自己的价格

账本估算费用时用这些环境变量，单位是每百万 token 多少美元。填你实际用的服务的价格：

| 变量 | 对应部分 |
| --- | --- |
| `OPENCOLLAB_INPUT_USD_PER_MTOK` | 普通输入 |
| `OPENCOLLAB_CACHED_INPUT_USD_PER_MTOK` | 缓存读取的输入 |
| `OPENCOLLAB_CACHE_CREATION_USD_PER_MTOK` | 缓存写入 |
| `OPENCOLLAB_OUTPUT_USD_PER_MTOK` | 输出 |

特定模型可能有单独的价格配置，以账本里的 `pricing` 为准。

如果服务把输入分成普通、缓存读取、缓存写入三类，就各乘各的价格，再加上输出费用。准确与否取决于字段是否齐全和提供方怎么计费。拿估算值和实际账单对比时，用相同的时间范围。

## 常见问题

- 如果总量里有重试：内部传输重试可能合并进外层调用的累计用量。看上下文压力时关注最终请求的输入大小，算花费时关注所有已返回的尝试。
- 如果用了上下文摘要：摘要也要调用模型，要算进真实开销。
- 如果中途取消了：后台请求可能在取消后才返回最后的用量。结合 late 和清理完成的记录，确定最终累计值。实际用量缺失的调用，费用仍是未知，要对照服务商账单。
- 如果用量突然变大：先分清是请求次数变多、单次输入变长，还是输出变长，再看缓存比例、重试和摘要次数。加密内容的字符数只是传输长度，计费 token 以提供方返回的用量为准。相关机制见上下文压缩与深度思考指南。

## 源码与进一步阅读

[configs/README.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/configs/README.md) · [opencollab/adapters/llm/usage_ledger.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/llm/usage_ledger.py) · [opencollab/adapters/llm/types.py](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/opencollab/adapters/llm/types.py) · [docs/evaluation-runtime.md](https://github.com/RISE-X-Lab/OpenCollab/blob/e7946a42be378e0bf6dc02e24f9ded9752ce2a41/docs/evaluation-runtime.md)
