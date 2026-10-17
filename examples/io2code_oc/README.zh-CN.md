# IO2Code 黑盒程序重建

[English](README.md)

只凭输入输出行为重建一个隐藏程序。benchmark 是 FromIO 论文
（[arXiv 2605.15334](https://arxiv.org/abs/2605.15334)，代码
[JiaruQian/IO2Code](https://github.com/JiaruQian/IO2Code)）的 IO2CodeBench：125
个小型 Python 函数，每题 8 个可见样例和 15 个隐藏测试，15 个全部通过才算解出。

benchmark 相关的内容全部在本目录；探索协议是 OpenCollab 内置的
[`reconstruct` workflow](../../docs/reconstruct.md)，OpenCollab 同时提供 agent
会话、工具、Docker 环境、预算和轨迹。

```text
main.py        把一道 IO2Code 任务绑定到 reconstruct workflow，用隐藏测试判分，写 result.json
tasks.py       从 IO2Code 检出读取任务（oracle 和隐藏测试留在本进程）
tools.py       query_oracle、check_candidate，候选程序在容器内执行
workflow.py    朴素的"探测、写代码、自检"workflow 和 benchmark 的固定模式（非协议模式）
tests/         离线单元测试（不调模型、不用 Docker、不需要检出）
```

## 前置条件

- Linux 或 macOS，装有 Docker。agent 在无网络的 `python:3.12-slim` 容器里工作，
  候选程序也在同一容器里执行，不在宿主机上运行。
- 本示例验证过的 IO2Code 版本：

  ```bash
  git clone https://github.com/JiaruQian/IO2Code.git
  git -C IO2Code checkout ef15827
  export IO2CODE_ROOT=$PWD/IO2Code
  ```

  检出只被本进程读取（oracle 函数、隐藏测试、输入生成器），不会挂载进容器。
- 在 `configs/.env` 里配置好模型（见 [configs/README.md](../../configs/README.md)）：
  `OPENCOLLAB_MODEL`、`OPENCOLLAB_API_KEY`、`OPENCOLLAB_BASE_URL`，只支持 Responses
  接口的模型加 `OPENCOLLAB_WIRE_PROTOCOL=responses`。推理模型请把
  `OPENCOLLAB_MAX_OUTPUT_TOKENS` 调高（下文结果用的是 65536）。会在终端事件里重新加密
  推理内容的模型（gpt-6-luna）需要在进程环境里设
  `OPENCOLLAB_TRUST_STREAMED_OUTPUT_ON_TERMINAL_MISMATCH=1`。

## 运行

在仓库根目录：

```bash
uv run python examples/io2code_oc/main.py --list
uv run python examples/io2code_oc/main.py Abs_Current Extra_PolishRPN \
  --random-share 0.5 --coverage-gate --docker python:3.12-slim
```

每个任务在 `examples/io2code_oc/runs/<run-name>/<task>/` 下拥有独立的
git 工作区和容器，产出 `result.json`（状态、隐藏测试得分、查询数、token、最终程序）、
`oracle_queries.json`（agent 拿到的每一对输入输出及其来源）和 OpenCollab 的
`artifacts/` 轨迹。崩溃或被跳过的题同样会写 `result.json`（状态为
`crashed`/`skipped`），`summary.json` 从运行目录里的全部 `result.json` 重建，
分母不会缩小。`--concurrency N` 并行跑多题，`--resume` 跳过已完成的题，
崩溃或被跳过的题会重试。

## 模式

| `--mode` | agent 拿到的证据 | 谁查询 oracle |
|---|---|---|
| `paper-active`（默认） | 开始 2 个样例；候选程序过了全部已知样例才发下一批（每批加 2）；连续 5 次晋级提前停止；最多 50 轮 | 内置的 `reconstruct` workflow 按论文 4.6 节协议查询，agent 负责提议输入 |
| `fixed` | benchmark 的 8 个可见样例 | 没有人 |
| `active` | 一开始没有 | agent 通过 `query_oracle` 自己查，上限 `--max-queries` |

`paper-active` 就是内置的 `reconstruct` workflow，对应论文仓库里的
`run_active_dio_agent.py`（批大小、晋级规则、早停、被拒提议的重试、选最终程序的阶段分）。
写程序的那一步是 OpenCollab 的 agent 会话，不是论文的进化式 DIO-Agent。`main.py` 只负责绑定：
oracle、benchmark 的输入生成器、容器内执行候选程序、工具，以及结束后的隐藏测试判分。

## 可选的验证纪律（reconstruct workflow 的参数）

| 开关 | 规则 |
|---|---|
| `--random-share 0.5` | 每批一半输入来自 benchmark 自带的生成器而不是 agent，让一部分证据独立于 agent 的假设 |
| `--coverage-gate` | 已知输入覆盖了 workflow 定义的结构性类别（空、单元素、边界值、重复……）后，晋级才计入早停；缺的类别会明确要求 agent 补 |
| `--memorisation-gate` | 把已知输入原样写进程序的候选不算通过验证 |
| `--complexity-gate` | 过于复杂的候选通过时需要更多证据才计入 |
| `--domain-check` | agent 提议的输入超出题目数值范围则拒绝 |
| `--rea` | 以上全部，且 `--random-share 0.5` |

## 结果

[RESULTS.md](RESULTS.md) 给出 115 道非多模态题在固定协议（90/115）、论文协议
（104/115）和论文协议加前两条纪律（113/115）下的结果，模型 `gpt-6-luna`，每种各跑一次。
10 道多模态题需要运行时的视觉模型，未覆盖。

## 信任边界

oracle 和隐藏测试不离开本进程。agent 的容器没有网络，只能看到自己的工作区。
候选程序在容器内执行，超时 30 秒，输出按 IO2Code 评测器的规则比较。任务名不会给
agent 看（很多任务名直接写出了规则）。
