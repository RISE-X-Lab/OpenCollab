<p align="center">
  <img src="assets/banner-dark.svg" alt="OpenCollab" width="600">
</p>

<p align="center">
  <a href="https://github.com/YihongDong/OpenCollab/actions/workflows/ci.yml"><img src="https://github.com/YihongDong/OpenCollab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MulanPSL--2.0-blue.svg" alt="License: MulanPSL-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg" alt="Python 3.10 | 3.11 | 3.12">
  <a href="assets/README.md"><img src="https://img.shields.io/badge/brand-assets-7C3AED.svg" alt="Brand assets"></a>
</p>

<p align="center">
  <b>An Operating Theory of Organized Intelligence.</b>
</p>

<p align="center">
  OpenCollab is an open research platform for organizing coding agents that
  read, edit, and verify real repositories.
</p>

<p align="center">
  <i>Inspired by <a href="https://arxiv.org/abs/2304.07590" title="Self-collaboration Code Generation via ChatGPT — Dong, Jiang, Jin, Li (2023)"><b>Self-Collaboration</b></a>.</i>
</p>

## What you can run

<p align="center">
  <picture>
    <source srcset="assets/oc-hero-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="assets/oc-hero-light.svg" media="(prefers-color-scheme: light)">
    <img src="assets/oc-hero-light.svg" alt="OpenCollab Team and Dynamic Workflow modes" width="1200">
  </picture>
</p>

The same agent runtime supports two explicit forms of collaboration:

| Mode | Command | What it is |
| --- | --- | --- |
| **Team** | `opencollab --workspace .` | A lead plans the work and spawns specialists that collaborate until the task is done. The agents decide the division of labor. |
| **Dynamic Workflow** | `opencollab workflow run NAME` | Python defines the control flow—fan-out, pipelines, loops, and verification gates—while agents complete each step. |

OpenCollab keeps the model, context, tools, orchestration, and execution
environment separable so experiments can change one component at a time.

## Quick start

```bash
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
scripts/start_opencollab.sh
```

Point `configs/.env` at an OpenAI-compatible or Anthropic endpoint. To run a
configured team, also copy `configs/team.example.yaml` to `configs/team.yaml`.
Never commit real API keys.

## Learn more

| You want… | Read |
| --- | --- |
| Installation, CLI, SDK, architecture, and runtime details | [`opencollab/README.md`](opencollab/README.md) |
| Model, provider, and team configuration | [`configs/README.md`](configs/README.md) |
| On-demand agent skills | [`skills/README.md`](skills/README.md) |
| Launchers and provider diagnostics | [`scripts/README.md`](scripts/README.md) |
| Contribution and development checks | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Design records and research notes | [`docs/`](docs/) |

## License

OpenCollab is licensed under the [Mulan Permissive Software License v2](LICENSE)
(`MulanPSL-2.0`).
