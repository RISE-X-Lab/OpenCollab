<p align="center">
  <img src="assets/banner-dark.svg" alt="OpenCollab mark and wordmark" width="600">
</p>

<p align="center">
  <a href="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml"><img src="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/releases/latest"><img src="https://img.shields.io/github/v/release/RISE-X-Lab/OpenCollab?color=7C3AED" alt="Latest release"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MulanPSL--2.0-blue.svg" alt="License: MulanPSL-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10--3.14-blue.svg" alt="Python 3.10 through 3.14">
</p>

<h3 align="center">Program how coding agents collaborate. Verify that they did.</h3>

<p align="center">
  OpenCollab is a multi-agent coding framework. It runs every collaboration on one
  controlled runtime and records whether the collaboration actually happened.
  <br>
  <b>English</b> · <a href="README.zh-CN.md">Chinese</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-hero-dark.svg">
    <img src="assets/benchmark-hero-light.svg" alt="Pass@1 of five harnesses that all use GPT-5.6-Luna. OpenCollab (Duo) is highest on all three benchmarks: 64.25% on SWE-bench Pro, 83.15% on Terminal-Bench 2.1, and 69.91% on DeepSWE, ahead of OpenCollab (Base), Claude Code, Codex CLI, and Mini-SWE-Agent." width="980">
  </picture>
</p>
<p align="center">
  <sub>Duo produces two isolated solutions and adopts one. Base, a single agent, uses the fewest tokens of the five.
  <a href="docs/results.md">Full results</a></sub>
</p>

## News

- **2026-09** — Our paper, *OpenCollab: A Multi-Agent Coding Framework with
  Programmable Collaboration and Controllable Runtime*, is coming soon.
- **2026-09-27** — [OpenCollab 0.8](https://github.com/RISE-X-Lab/OpenCollab/releases/tag/v0.8.0)
  ships [Duo](docs/duo.md) built in and makes [Single2](docs/single2.md) the
  default Base agent.
- 🎉 **Congratulations!** OpenCollab has been selected for support by the
  **Seed Program of the Youth Open Source Special Fund**.

## Quick start

```bash
uv sync --locked
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
uv run opencollab --workspace .
```

`configs/.env` accepts any OpenAI-compatible or Anthropic endpoint; never commit
real API keys. The command starts the built-in `lead` agent, which may spawn
specialists as needed. To run Duo on a Git repository:

```bash
uv run opencollab workflow run duo --workspace /path/to/repository \
  --args '{"goal":"Fix the public issue described here.","allow_unisolated_shell":true}'
```

## Program the collaboration

<p align="center">
  <picture>
    <source srcset="assets/oc-hero-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="assets/oc-hero-light.svg" media="(prefers-color-scheme: light)">
    <img src="assets/oc-hero-light.svg" alt="OpenCollab Team and Workflow modes" width="1200">
  </picture>
</p>

- **Team.** Copy [`configs/team.example.yaml`](configs/team.example.yaml) to
  `configs/team.yaml`, declare each role's prompt, model, and tools and who may
  message whom, then run
  `uv run opencollab --team-config configs/team.yaml --workspace .`
- **Workflow.** Write the handoffs as a Python module
  ([workflow authoring](opencollab/README.md#workflow-authoring)) and run it
  with `uv run opencollab workflow run NAME`. Duo is a built-in workflow.

[Mini Edict](examples/mini-edict/) reimplements the core protocol of
[Edict](https://github.com/cft0808/edict), a multi-agent system of roughly
24,000 lines, in 239 lines of team and workflow code.

## Verify it happened

In a Team, the agents decide the handoffs, and a declared team does not
guarantee that they happen. In our runs, the lead agent often briefed its
teammates and then did the work itself. OpenCollab enforces the declaration and
records what each agent did:

- a message along an undeclared edge is refused, and the refusal is recorded;
- each agent's token budget is checked before every model call;
- `--trace` writes one JSONL record per step, stamped with the run, agent, role,
  event type, and token usage.

From these records we measure **Adherence**, the share of runs in which the
declared organization actually happened. Changing one setting of the same team
raised it from 47.2% to as much as 97.2%.
See [how Adherence is measured](docs/adherence.md).

## Learn more

| Guide | What it covers |
| --- | --- |
| [Package guide](opencollab/README.md) | CLI, Python API, architecture, and runtime behavior |
| [Configuration guide](configs/README.md) | Providers, models, and team files |
| [Duo guide](docs/duo.md) · [Chinese](docs/duo/README.zh-CN.md) | Running the dual-coder workflow from the CLI and SDK |
| [Benchmark results](docs/results.md) | Tokens, cost, the paired Duo–Base test, and how to run an evaluation |
| [Adherence](docs/adherence.md) | What the runtime records, and how we measure Adherence |
| [OpenCollab-Eval README](https://github.com/RISE-X-Lab/OpenCollab-Eval#readme) | Running agents on software-engineering benchmarks |
| [Documentation index](docs/README.md) | Skills, migration guides, and design records |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development checks, [testing](docs/testing.md), and [releases](RELEASING.md) |

## Citation

If you find OpenCollab useful, please give it a ⭐ and cite our work. Our paper,
*OpenCollab: A Multi-Agent Coding Framework with Programmable Collaboration and
Controllable Runtime*, is coming soon. OpenCollab builds on
[Self-Collaboration](https://arxiv.org/abs/2304.07590):

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

## License

OpenCollab is licensed under the [Mulan Permissive Software License v2](https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE)
(`MulanPSL-2.0`).

## Star History

<p align="center">
  <a href="https://www.star-history.com/?repos=RISE-X-Lab%2FOpenCollab&amp;type=date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=RISE-X-Lab/OpenCollab&amp;type=Date&amp;theme=dark">
      <img src="https://api.star-history.com/svg?repos=RISE-X-Lab/OpenCollab&amp;type=Date" alt="OpenCollab star history chart" width="600">
    </picture>
  </a>
</p>
