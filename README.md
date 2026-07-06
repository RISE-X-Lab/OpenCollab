<p align="center">
  <img src="assets/banner-dark.svg" alt="OpenCollab" width="600">
</p>

<p align="center">
  <a href="https://github.com/YihongDong/OpenCollab/actions/workflows/ci.yml"><img src="https://github.com/YihongDong/OpenCollab/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg" alt="Python 3.10 | 3.11 | 3.12">
  <a href="assets/README.md"><img src="https://img.shields.io/badge/brand-assets-7C3AED.svg" alt="Brand assets"></a>
</p>

<p align="center">
  <b>An Operating Theory of Organized Intelligence.</b>
</p>

<!-- <p align="center">
  Turn LLMs into a coordinated software-engineering team — one that reads,
  edits, and tests a real repository.
</p> -->

<p align="center">
  <i>OpenCollab is Inspired by <a href="https://arxiv.org/abs/2304.07590" title="Self-collaboration Code Generation via ChatGPT — Dong, Jiang, Jin, Li (2023)"><b>Self-Collaboration</b></a> </i>
</p>

<!-- Logo & brand assets live in assets/ — see assets/README.md for the brand guide. -->
## What you can run

<p align="center">
  <picture>
    <source srcset="assets/oc-hero-dark.svg" media="(prefers-color-scheme: dark)">
    <source srcset="assets/oc-hero-light.svg" media="(prefers-color-scheme: light)">
    <img src="assets/oc-hero-light.svg" alt="OpenCollab" width="1200">
  </picture>
</p>

<!-- OpenCollab turns an LLM into a software engineer that reads, edits, and tests a
real repository. It's built to separate what the *model* contributes from what
the *scaffolding* (context, tools, orchestration) contributes — so everything
but the model sits behind swappable ports. -->


Two ways to run the same agents:

| Mode | Command | What it is |
|------|---------|------------|
| **Team** | _(coming soon)_ | An autonomous multi-agent team: a lead plans the work and spawns specialists — coder, reviewer, tester — that collaborate until it's done. The LLM decides who does what; a single agent is just a team of one. |
| **Dynamic Workflow** | _(coming soon)_ | Deterministic orchestration: you script the control flow in Python — fan-out, loops, verification gates — and the LLM fills in each step. The structure is yours, not the model's to choose. |

> Commands land with the CLI — for now, start from **Quick start** below.

## Quick start

```bash
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
scripts/start_opencollab.sh            # bootstraps the venv, then starts the agent
```

Point `configs/.env` at any OpenAI-compatible (or Anthropic) endpoint. To run as
a team, also `cp configs/team.example.yaml configs/team.yaml`. **Never commit
real API keys.**

<!-- ## Learn more

| You want… | Read |
|-----------|------|
| The architecture & design principles (start here) | [`CLAUDE.md`](CLAUDE.md) |
| Install, CLI, and how it works in depth | [`opencollab/README.md`](opencollab/README.md) |
| Configuration (model, team, sampling) | [`configs/README.md`](configs/README.md) |
| Deterministic workflows | [`workflows/README.md`](workflows/README.md) |
| Skills (on-demand instruction sets) | [`skills/README.md`](skills/README.md) |
| SWE-bench eval | [`scripts/README.md`](scripts/README.md) · [`swebench/README.md`](swebench/README.md) |
| Design docs & reviews | [`docs/`](docs/) | -->

Contributing: see [`CLAUDE.md`](CLAUDE.md). Conventional commits; `refactor:`
commits stay behavior-preserving.
