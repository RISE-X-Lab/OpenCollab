<p align="center">
  <img src="https://raw.githubusercontent.com/RISE-X-Lab/OpenCollab/main/assets/banner-dark.svg" alt="OpenCollab mark and wordmark" width="600">
</p>

<h1 align="center">OpenCollab</h1>

<p align="center">
  <a href="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml"><img src="https://github.com/RISE-X-Lab/OpenCollab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MulanPSL--2.0-blue.svg" alt="License: MulanPSL-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10--3.14-blue.svg" alt="Python 3.10 through 3.14">
  <a href="https://github.com/RISE-X-Lab/OpenCollab/blob/main/assets/README.md"><img src="https://img.shields.io/badge/brand-assets-7C3AED.svg" alt="Brand assets"></a>
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
    <source srcset="https://raw.githubusercontent.com/RISE-X-Lab/OpenCollab/main/assets/oc-hero-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="https://raw.githubusercontent.com/RISE-X-Lab/OpenCollab/main/assets/oc-hero-light.svg" media="(prefers-color-scheme: light)">
    <img src="https://raw.githubusercontent.com/RISE-X-Lab/OpenCollab/main/assets/oc-hero-light.svg" alt="OpenCollab Team and Dynamic Workflow modes" width="1200">
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
uv sync --locked
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
cp configs/team.example.yaml configs/team.yaml
uv run opencollab --workspace .
```

Point `configs/.env` at an OpenAI-compatible or Anthropic endpoint. The command
starts Team mode with the checked-in example topology. Never commit real API
keys.

For repeatable pipelines, author a Python workflow and run it by name:

```bash
uv run opencollab workflow run NAME --args '{"task": "..."}'
```

See [Workflow authoring](https://github.com/RISE-X-Lab/OpenCollab/blob/main/opencollab/README.md#workflow-authoring)
for a complete module.

## Learn more

| You want… | Read |
| --- | --- |
| Installation, CLI, SDK, architecture, and runtime details | [Package guide](https://github.com/RISE-X-Lab/OpenCollab/blob/main/opencollab/README.md) |
| Model, provider, and team configuration | [Configuration guide](https://github.com/RISE-X-Lab/OpenCollab/blob/main/configs/README.md) |
| On-demand agent skills | [Skills guide](https://github.com/RISE-X-Lab/OpenCollab/blob/main/skills/README.md) |
| Launchers and provider diagnostics | [Scripts guide](https://github.com/RISE-X-Lab/OpenCollab/blob/main/scripts/README.md) |
| Contribution and development checks | [Contributing](https://github.com/RISE-X-Lab/OpenCollab/blob/main/CONTRIBUTING.md) |
| Design records and research notes | [Documentation index](https://github.com/RISE-X-Lab/OpenCollab/blob/main/docs/README.md) |

## License

OpenCollab is licensed under the [Mulan Permissive Software License v2](https://github.com/RISE-X-Lab/OpenCollab/blob/main/LICENSE)
(`MulanPSL-2.0`).
