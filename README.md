<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme-hero-dark.svg">
    <img src="assets/readme-hero-light.svg" alt="OpenCollab. Program how coding agents collaborate. Verify that they did." width="880">
  </picture>
</p>

<p align="center">
  A multi-agent coding framework with programmable collaboration and a controllable runtime.
</p>

<p align="center">
  <a href="https://arxiv.org/abs/2609.38345"><img src="https://img.shields.io/badge/arXiv-2609.38345-B31B1B.svg?style=flat-square&amp;labelColor=0F172A" alt="arXiv 2609.38345"></a>
  <a href="https://rise-x-lab.github.io/OpenCollab/"><img src="https://img.shields.io/badge/Project-Page-7C3AED.svg?style=flat-square&amp;labelColor=0F172A" alt="Project page"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/releases/latest"><img src="https://img.shields.io/github/v/release/RISE-X-Lab/OpenCollab?style=flat-square&amp;color=7C3AED&amp;labelColor=0F172A" alt="Latest release"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MulanPSL--2.0-blue.svg?style=flat-square&amp;color=5556EC&amp;labelColor=0F172A" alt="License: MulanPSL-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10--3.14-blue.svg?style=flat-square&amp;color=2563EB&amp;labelColor=0F172A" alt="Python 3.10 through 3.14">
  <a href="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/RISE-X-Lab/OpenCollab/ci.yml?branch=main&amp;style=flat-square&amp;label=CI&amp;labelColor=0F172A" alt="CI"></a>
  <br>
  <sub><b>English</b> · <a href="README.zh-CN.md">Chinese</a></sub>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-hero-dark.svg">
    <img src="assets/benchmark-hero-light.svg" alt="Pass@1 of six harnesses using GPT-5.6-Luna at max reasoning effort. OpenCollab (Duo) scores 68.91% on SWE-bench Pro, 83.15% on Terminal-Bench 2.1, and 65.49% on DeepSWE." width="980">
  </picture>
</p>
<p align="center">
  <sub>Duo produces two isolated solutions and adopts one. Base, a single agent, uses the fewest tokens across all three benchmarks.
  <a href="docs/results.md">Full results</a></sub>
</p>

## News

- 🎉 **2026-10-08** — [OpenCollab 0.9.3 (v1.0.0 PreRelease)](https://github.com/RISE-X-Lab/OpenCollab/releases/tag/v0.9.3)
  improves candidate preservation, team repair delivery, Git history handling,
  tool restrictions, and Anthropic input-budget estimates. See the
  [changelog](CHANGELOG.md#093---2026-10-08).

- 🎉 **2026-10-06** — [OpenCollab 0.9.2](https://github.com/RISE-X-Lab/OpenCollab/releases/tag/v0.9.2)
  includes the latest Duo V8 workflow and all the statistical features described
  in our paper to support research experiments. Following extensive testing and
  bug fixes, the framework is now nearly ready for 1.0.0. We will release version
  1.0.0 once our paper is accepted. See the [changelog](CHANGELOG.md#092---2026-10-06).
- **2026-09-29** — Our paper is on arXiv: [*OpenCollab: A Multi-Agent Coding Framework with
  Programmable Collaboration and Controllable Runtime*](https://arxiv.org/abs/2609.38345).
- **2026-09-27** — [OpenCollab 0.8](https://github.com/RISE-X-Lab/OpenCollab/releases/tag/v0.8.0)
  ships [Duo](docs/duo.md) built in and makes [Single2](docs/single2.md) the
  default Base agent.
- 🎉 OpenCollab has been selected for support by the **Seed Program of the
  Youth Open Source Special Fund**.

## Quick start

```bash
uv sync --locked
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
uv run opencollab --workspace .
```

`configs/.env` accepts any OpenAI-compatible or Anthropic endpoint. Never commit
real API keys. The command starts the built-in Self-Collaboration team. Its
Analyst entry plans and delegates to the Coder and Tester over a closed topology.
Type `/help` in the terminal for navigation, saving and exit controls.
To define additional roles, create and select an explicit team file as shown below.
To run Duo on a Git repository, use this command.

```bash
uv run opencollab workflow run duo --workspace /path/to/repository \
  --task 'Fix the public issue described here.' \
  --args '{"allow_unisolated_shell":true}'
```

Place run options such as `--workspace`, `--model` and `--budget` after
`workflow run NAME`. To list workflows in another workspace, use
`workflow list --workspace PATH`. Options placed before the `workflow`
subcommand are interpreted as belonging to the root interactive mode and
will produce a usage error.

## Program the collaboration

<p align="center">
  <picture>
    <source srcset="assets/oc-hero-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="assets/oc-hero-light.svg" media="(prefers-color-scheme: light)">
    <img src="assets/oc-hero-light.svg" alt="OpenCollab Team and Workflow modes" width="1200">
  </picture>
</p>

- **Team.** A YAML file declares each role's prompt, model, tools, and token
  allowance, the context policy, and who may message whom. Start from the
  built-in team and edit it:

  ```bash
  uv run opencollab team init team.yaml   # edit roles, prompts, tools, and topology
  uv run opencollab team show --team-config team.yaml
  uv run opencollab --team-config team.yaml --workspace .
  ```

- **Workflow.** A Python module issues every handoff. Duo is one;
  [write your own](opencollab/README.md#workflow-authoring) and run it with
  `opencollab workflow run NAME`.

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

If you find OpenCollab useful, please give it a ⭐ and cite our work. Our paper is
[*OpenCollab: A Multi-Agent Coding Framework with Programmable Collaboration and
Controllable Runtime*](https://arxiv.org/abs/2609.38345). OpenCollab builds on
[Self-Collaboration](https://arxiv.org/abs/2304.07590):

```bibtex
@misc{hsu2026opencollab,
  author        = {Hsu, Chun-Wah and Gong, Kai and Wu, Yu and Chen, Xianhe and
                   Li, Hanyu and Li, Jie and Liu, Mengyang and Liu, Zhixuan and
                   Tang, Naisheng and Chi, Jiaying and Fan, Ziheng and He, Xuning and
                   Yang, Xiaokang and Jiang, Xue and Dong, Yihong},
  title         = {OpenCollab: A Multi-Agent Coding Framework with Programmable Collaboration and Controllable Runtime},
  year          = {2026},
  eprint        = {2609.38345},
  archivePrefix = {arXiv},
  url           = {https://arxiv.org/abs/2609.38345}
}

@article{dong2023self,
  author        = {Dong, Yihong and Jiang, Xue and Jin, Zhi and Li, Ge},
  title         = {Self-Collaboration Code Generation via ChatGPT},
  journal       = {ACM Transactions on Software Engineering and Methodology},
  volume        = {33},
  number        = {7},
  pages         = {189:1--189:38},
  year          = {2024}
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
