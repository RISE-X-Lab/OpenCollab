<p align="center">
  <img src="assets/banner-dark.svg" alt="OpenCollab 标志与字标" width="600">
</p>

<p align="center">
  <a href="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml"><img src="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/releases/latest"><img src="https://img.shields.io/github/v/release/RISE-X-Lab/OpenCollab?color=7C3AED" alt="最新版本"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MulanPSL--2.0-blue.svg" alt="License: MulanPSL-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10--3.14-blue.svg" alt="Python 3.10 至 3.14">
  <a href="https://github.com/RISE-X-Lab/OpenCollab/blob/main/assets/README.md"><img src="https://img.shields.io/badge/brand-assets-7C3AED.svg" alt="品牌素材"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab"><img src="https://img.shields.io/badge/GitHub-View%20on%20GitHub-181717.svg?logo=github&logoColor=white" alt="在 GitHub 上查看"></a>
</p>

<h3 align="center">用代码编排 coding agent 的协作，用运行记录验证协作确实发生。</h3>

<p align="center">
  一个协作可编程、运行时可控的多智能体编程框架。<br>
  灵感来自 <a href="https://arxiv.org/abs/2304.07590" title="Self-collaboration Code Generation via ChatGPT — Dong, Jiang, Jin, Li (2023)"><b>Self-Collaboration</b></a>。
</p>

<p align="center">
  <a href="README.md">English</a> · <b>简体中文</b>
  <br>
  <a href="#评测结果">评测结果</a> ·
  <a href="#你的团队真的在协作吗">Adherence</a> ·
  <a href="#工作原理">工作原理</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/duo/README.zh-CN.md">Duo</a> ·
  <a href="https://github.com/RISE-X-Lab/OpenCollab-Eval">OpenCollab-Eval</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-hero-dark.svg">
    <img src="assets/benchmark-hero-light.svg" alt="五个 harness 都使用 GPT-5.6-Luna 时的 Pass@1。OpenCollab (Duo) 在三个 benchmark 上都最高：SWE-bench Pro 64.25%，Terminal-Bench 2.1 83.15%，DeepSWE 69.91%，高于 OpenCollab (Base)、Claude Code、Codex CLI 和 Mini-SWE-Agent。" width="980">
  </picture>
</p>

<table>
  <tr>
    <td width="50%" valign="top">
      <h4>🏆 同一模型下 Pass@1 最高</h4>
      所有 harness 都使用 GPT-5.6-Luna 时，OpenCollab (Duo) 在 SWE-bench Pro、
      Terminal-Bench 2.1 和 DeepSWE 上都超过 Claude Code、Codex CLI 和
      Mini-SWE-Agent。
    </td>
    <td width="50%" valign="top">
      <h4>💰 对比中最省钱的 harness</h4>
      单 agent 的 OpenCollab (Base) 在三个 benchmark 上的 token 用量和花费都是最低。
      Duo 在每个 benchmark 上都比 Claude Code 便宜。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h4>🧩 协作即代码</h4>
      在 YAML team 文件里声明角色、工具，以及谁可以给谁发消息；或者用 Python
      写出每一次交接。<a href="examples/mini-edict/">Mini Edict</a> 用 239 行复现了一个约
      24,000 行的多智能体系统的核心协议。
    </td>
    <td width="50%" valign="top">
      <h4>🔍 协作可检验</h4>
      每一次模型调用、工具调用、消息和拒绝，都记在带角色标记的事件流里。我们据此计算
      <b>Adherence</b>：声明的组织在多少比例的 run 里真的发生了。只改一个设置，它就从
      47.2% 变到 97.2%。
    </td>
  </tr>
</table>

## 动态

- **2026-09**：论文 *OpenCollab: A Multi-Agent Coding Framework with
  Programmable Collaboration and Controllable Runtime* 即将公开。
- **2026-09-27**：[OpenCollab 0.8](https://github.com/RISE-X-Lab/OpenCollab/releases/tag/v0.8.0)
  发布，内置双 coder workflow [Duo](docs/duo/README.zh-CN.md)，默认的 Base agent 改为
  [Single2](docs/single2.md)。
- 🎉 **祝贺！** OpenCollab 入选 **Seed Program of the Youth Open Source Special Fund** 资助。

## 评测结果

完整对比另外给出平均 token、估算的平均花费和缓存命中率。五个 harness 都使用
GPT-5.6-Luna，reasoning effort 设为 `max`，在 SWE-bench Pro（193 题）、
Terminal-Bench 2.1（89 题）和 DeepSWE（113 题）上评测。[OC (Base)](docs/single2.md)
是单 agent。[OC (Duo)](docs/duo/README.zh-CN.md) 生成两个相互隔离的解，比较各自的证据后采纳其中一个。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-results-dark.svg">
    <img src="assets/benchmark-results-light.svg" alt="跨 harness 评测结果。OC Duo 的 Pass@1 最高：SWE-bench Pro 64.25%，Terminal-Bench 2.1 83.15%，DeepSWE 69.91%。各列为 Pass@1、平均 token、估算的平均花费和缓存命中率。" width="980">
  </picture>
</p>

> [!TIP]
> **协作有保证时，团队胜过它自己的单 agent。** Duo 是一个 Workflow：每一次交接都由代码发出，
> 所以两个 coder 在每道题上都会工作。在 DeepSWE 上与 Base 按题配对，Duo 解出了 21 道
> Base 没解出的题，只丢了 5 道 Base 解出的题，高出 14.2 个点（精确符号检验，p = 0.0025）。
> 在 Terminal-Bench 2.1 上高出 3.4 个点。[^tb]

## 你的团队真的在协作吗？

给 agent 分配了 analyst、coder、tester 的角色之后，benchmark 分数仍然只报告最终结果，
说明不了这些 agent 有没有一起工作。在 OpenCollab 的轨迹里，它们常常没有：lead agent
给队友布置完任务后自己把活干了，或者还没交出任何工作就用光了自己的 token 预算。
声明的团队塌缩成了一个 agent。我们把这种现象称为 *illusion of collaboration*（协作的假象）。

<p align="center">
  <img src="assets/collaboration-traces.png" alt="两次运行的时间线，共用一条时间轴。只读工具的那次运行里，Adopter 给 Coder A 布置任务，Coder A 提交 commit，Adopter 采纳后通过测试。参照团队的那次运行里，Adopter 给两个 coder 都布置了任务，却一直自己干到预算耗尽，最后被评分的是它自己的代码，没有通过。" width="900">
</p>
<p align="center">
  <sub>两次运行，均由 OpenCollab 的事件流重建。(a) Adopter（即 lead agent）只有只读工具时，它把任务交给 coder，
  并采纳了 Coder A 的 commit。(b) 参照团队里，Adopter 给两个 coder 都布置了任务，却一直自己干到预算耗尽，
  最后被评分的是它自己的代码。</sub>
</p>

我们把这件事变成一个数。只有当轨迹显示每个声明的角色都参与了，并且委派、角色边界、
预算分配、信息流和 context 策略都符合声明时，一次 run 才算 adherent。**Adherence**
就是 adherent run 所占的比例。在 36 道 SWE-bench Pro 题上，对同一个团队只做一处改动，
它就会大幅变化：

| 对参照团队的改动 | Adherence |
| --- | ---: |
| 无（Qwen3.8-Flash、开放式 prompt、全部工具、星形拓扑） | 47.2% |
| prompt 规定必须交接 | 91.7% |
| lead agent 只拿只读工具 | 94.4% |
| 所有角色换成 GPT-5.6-Luna | 97.2% |

有了 Adherence，才能正确解读 Team 与 Single 之间的差距。用 Pass@1 的差值除以
Adherence，得到 complier average causal effect（CACE）；又因为每次 run 都记录了
Adherence，这个估计所依赖的假设可以用数据来审视，而不必直接假定。论文给出了六个审计维度的定义，
以及对模型、工具、预算、context 和拓扑的完整消融。

## 工作原理

### 协作即代码

<p align="center">
  <picture>
    <source srcset="assets/oc-hero-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="assets/oc-hero-light.svg" media="(prefers-color-scheme: light)">
    <img src="assets/oc-hero-light.svg" alt="OpenCollab 的 Team 与 Workflow 两种模式" width="1200">
  </picture>
</p>

OpenCollab 在同一个 agent runtime 上支持两种协作形式，另有单 agent 基线。

| 模式 | 命令 | 说明 |
| --- | --- | --- |
| **Team** | `opencollab [--team-config FILE] --workspace .` | lead 规划工作，并派生专职 agent 协作直到任务完成。分工由 agent 自己决定。 |
| **Workflow** | `opencollab workflow run NAME` | 由 Python 定义 fan-out、流水线、循环和验证，agent 完成其中每一步。 |

team 文件声明每个角色的 prompt、模型和工具，以及谁可以给谁发消息的拓扑。workflow 是一个
Python 模块，每一次交接都是显式的代码。

复杂的协作设计也能变成一份紧凑、可读的协议。[Edict](https://github.com/cft0808/edict)
把三省六部实现为一个约 24,000 行源码的独立系统；[Mini Edict](examples/mini-edict/) 在
OpenCollab runtime 上用 239 行 team 与 workflow 代码实现了 Edict 的核心审议与分派协议，
代码量约为原来的百分之一。研究者只需描述协作协议，运行它所需的基础设施由 OpenCollab 提供。
这个示例附有双语指南和测试。

### 同一个受控的 runtime

<p align="center">
  <img src="assets/oc-architecture.png" alt="OpenCollab 总览。任务、模型、角色 prompt、工具集、拓扑以及 token 与 context 策略构成的规格，驱动 Single、Team、Workflow 三种控制器之一。三者都跑在共享的 session runtime 上，使用隔离的 worktree 和同一个 session 循环。事件流进入审计，产出 Adherence、结果与成本。" width="900">
</p>

- **所有组织形式共用一个底座。** 单 agent、Team 和 Workflow 跑在同一个 session runtime 上，
  使用相同的模型客户端、context 处理和工具实现。换组织形式不会改动系统的其他部分。
  模型接入、context 处理、工具执行、编排和环境各有独立的扩展点，实验可以一次只改一个组件。
- **规则在调用时执行。** 沿未声明的边发送的消息会被拒绝，拒绝本身也会被记录。每次模型调用前
  都会检查该 agent 的 token 预算；到达上限的 agent 会带着明确的原因停止，所以被截断的 run
  不会与正常完成的 run 混淆。
- **用事件流取代文字日志。** `--trace` 为每一步写一条 JSONL 记录，带有 run、agent、角色、
  事件类型和 token 用量。脚本不必解析对话记录，就能还原谁做了什么。
- **隔离的工作区。** 队友可以在各自的 git worktree 里工作，再交回 diff。

## 快速开始

```bash
uv sync --locked
cp configs/.env.example configs/.env   # 然后设置 OPENCOLLAB_API_KEY
uv run opencollab --workspace .
```

把 `configs/.env` 指向一个 OpenAI 兼容端点或 Anthropic 端点。这条命令从内置的单个 `lead`
开始，它可以按需派生临时的专职 agent。不要提交真实的 API key。要使用声明好的角色和固定拓扑，
请显式指定 team 文件。

```bash
cp configs/team.example.yaml configs/team.yaml
uv run opencollab --team-config configs/team.yaml --workspace .
```

在本地 Git 仓库里运行内置的双 coder workflow [Duo](docs/duo/README.zh-CN.md)。它的两个
相互隔离的 coder 比较公开证据，再应用选中的 patch。是否允许在本地执行 shell 是 workflow 的
显式输入。

```bash
uv run opencollab workflow list --workspace /path/to/repository
uv run opencollab workflow run duo --workspace /path/to/repository \
  --args '{"goal":"Fix the public issue described here.","allow_unisolated_shell":true}'
```

[Duo 中文指南](docs/duo/README.zh-CN.md) 介绍 SDK 调用、Single2 profile、显式的完整文件证据，
以及面向任务的角色指令；[英文指南](docs/duo.md) 是正式版本。要把另一种协作协议写成 Python
模块，见 [Workflow authoring](https://github.com/RISE-X-Lab/OpenCollab/blob/main/opencollab/README.md#workflow-authoring)。

## 用 OpenCollab-Eval 评测

[OpenCollab-Eval](https://github.com/RISE-X-Lab/OpenCollab-Eval) 通过 OpenCollab 的公开 Python API
在软件工程 benchmark 上运行 agent。它为每道题创建隔离的工作区，记录 patch，运行官方测试，
并保留复查结果所需的命令和报告。目前支持 SWE-bench Pro-Lite，并为其他评测任务提供通用的
task runner。数据集、Docker 集成、benchmark 适配器和实验报告都在那个仓库；本仓库只包含协作框架。

默认的 [OC Base agent](docs/single2.md) 通过公开的 `agent(...)` 入口映射到 Single2。
完整的协作评测请按 [Duo with Single2 quick start](https://github.com/RISE-X-Lab/OpenCollab-Eval#duo-quick-start)
操作。它涵盖匹配的 OC/OCE 0.8 安装、benchmark 镜像、Responses 模型端点，以及用
`oc-eval g22 --config /path/to/g22.json --indices 1 --workers 1` 做单题官方评测。
同一份配置也能跑整批，并把每道题的 patch、轨迹和官方测试报告放在一起。

[评测指南](https://github.com/RISE-X-Lab/OpenCollab-Eval#supported-environment) 说明怎么运行，
[完整性指南](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/docs/evaluation-integrity.md)
说明结果怎么核查，[MIGRATION.md](https://github.com/RISE-X-Lab/OpenCollab-Eval/blob/main/MIGRATION.md)
记录两个仓库的边界。

## 文档

[包指南](https://github.com/RISE-X-Lab/OpenCollab/blob/main/opencollab/README.md) 介绍安装、CLI、
Python API、架构和运行时行为。[配置指南](https://github.com/RISE-X-Lab/OpenCollab/blob/main/configs/README.md)
介绍 provider、模型和 team。

[Mini Edict](https://github.com/RISE-X-Lab/OpenCollab/tree/main/examples/mini-edict) 展示一个九个角色的
制度化 workflow。[skills 指南](https://github.com/RISE-X-Lab/OpenCollab/blob/main/skills/README.md)
介绍按需加载的指令，[scripts 指南](https://github.com/RISE-X-Lab/OpenCollab/blob/main/scripts/README.md)
介绍启动脚本和 provider 诊断。

仓库开发流程见 [CONTRIBUTING.md](https://github.com/RISE-X-Lab/OpenCollab/blob/main/CONTRIBUTING.md)。
[测试指南](docs/testing.md) 介绍测试命令，[测试目录指南](tests/README.md) 把行为对应到测试主题。
维护者发布版本时可参照 [RELEASING.md](https://github.com/RISE-X-Lab/OpenCollab/blob/main/RELEASING.md)。
[文档索引](https://github.com/RISE-X-Lab/OpenCollab/blob/main/docs/README.md) 链接设计记录和研究笔记。
做 benchmark 的用户请从 [OpenCollab-Eval README](https://github.com/RISE-X-Lab/OpenCollab-Eval#readme) 开始。
以上文档均为英文。

## 引用

如果这个项目对你有帮助，欢迎点一个 ⭐ 并引用我们的工作。论文 *OpenCollab: A Multi-Agent Coding
Framework with Programmable Collaboration and Controllable Runtime* 即将公开。OpenCollab 建立在
Self-Collaboration 之上：

```bibtex
@article{dong2023self,
  author={Dong, Yihong and Jiang, Xue and Jin, Zhi and Li, Ge},
  title        = {Self-Collaboration Code Generation via ChatGPT},
  journal      = {ACM Transactions on Software Engineering and Methodology},
  volume       = {33},
  number       = {7},
  pages        = {189:1--189:38},
  year         = {2024}
}
```

## 许可证

OpenCollab 采用[木兰宽松许可证第 2 版](https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE)
（`MulanPSL-2.0`）。

## Star History

<p align="center">
  <a href="https://www.star-history.com/?repos=RISE-X-Lab%2FOpenCollab&amp;type=date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=RISE-X-Lab/OpenCollab&amp;type=Date&amp;theme=dark">
      <img src="https://api.star-history.com/svg?repos=RISE-X-Lab/OpenCollab&amp;type=Date" alt="OpenCollab star history 图" width="600">
    </picture>
  </a>
</p>

[^tb]: 在 Terminal-Bench 2.1 上，这一差距来自只有 Duo 解出的 6 道题和只有 Base 解出的 3 道题，
    统计上不显著（p = 0.51）。该 benchmark 上用到了泄漏参考材料的通过，已在评分前撤回，或用全新的运行替换。
