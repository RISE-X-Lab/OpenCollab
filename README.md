# OpenCollab

**Run LLM coding agents three ways — as a single interactive agent, an
autonomous team, or a deterministic workflow.**

OpenCollab turns an LLM into a software engineer that reads, edits, and tests a
real repository. It's built to separate what the *model* contributes from what
the *scaffolding* (context, tools, orchestration) contributes — so everything
but the model sits behind swappable ports.

> SWE-bench Lite (n=300), team + `kimi-k2.6`, graded by the official harness:
> **185 / 300 = 61.7% resolved**.

## What you can run

| Mode | Command | What it is |
|------|---------|------------|
| **Interactive** | `opencollab` | A lead agent you chat with in the terminal; it can spawn helpers. Add a `configs/team.yaml` to run a full multi-agent team. |
| **Workflow** | `opencollab workflow run <name>` | Deterministic multi-agent orchestration — control flow is Python, not the LLM's choice. |
| **Eval** | `opencollab eval <tasks.jsonl>` | Headless benchmark runner (SWE-bench, etc.). |

## Quick start

```bash
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
scripts/start_opencollab.sh            # bootstraps the venv, then starts the agent
```

Point `configs/.env` at any OpenAI-compatible (or Anthropic) endpoint. To run as
a team, also `cp configs/team.example.yaml configs/team.yaml`. **Never commit
real API keys.**

## Learn more

| You want… | Read |
|-----------|------|
| Install, CLI, how it works, architecture | [`opencollab/README.md`](opencollab/README.md) |
| Configuration (model, team, sampling) | [`configs/README.md`](configs/README.md) |
| Deterministic workflows | [`workflows/README.md`](workflows/README.md) |
| Skills (on-demand instruction sets) | [`skills/README.md`](skills/README.md) |
| SWE-bench eval | [`scripts/README.md`](scripts/README.md) · [`swebench/README.md`](swebench/README.md) |
| Design docs & reviews | [`docs/`](docs/) |

Contributing: see [`CLAUDE.md`](CLAUDE.md). Conventional commits; `refactor:`
commits stay behavior-preserving.
