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
| **Team** | `opencollab --workspace .` | An autonomous multi-agent team: a lead plans the work and spawns specialists — coder, reviewer, tester — that collaborate until it's done. The LLM decides who does what; a single agent is just a team of one. |
| **Dynamic Workflow** | `opencollab workflow run NAME` | Deterministic orchestration: you script the control flow in Python — fan-out, loops, verification gates — and the LLM fills in each step. The structure is yours, not the model's to choose. |

## Quick start

```bash
cp configs/.env.example configs/.env   # then set OPENCOLLAB_API_KEY
scripts/start_opencollab.sh            # bootstraps the venv, then starts the agent
```

Point `configs/.env` at any OpenAI-compatible (or Anthropic) endpoint. To run as
a team, also `cp configs/team.example.yaml configs/team.yaml`. **Never commit
real API keys.**

## Install

OpenCollab supports Linux and macOS hosts. Its local file adapters require
descriptor-relative operations, no-follow `stat`, `O_NOFOLLOW`, fd-based
directory listing, callable `fcntl.flock`, and an atomic no-clobber rename:
`renameat2(RENAME_NOREPLACE)` on Linux or `renameatx_np(RENAME_EXCL)` on macOS
10.12 and newer. Missing primitives produce an explicit capability error.

For development, install the project and its test dependencies from the
repository root:

```bash
uv sync --extra dev
```

Or create a conventional virtual environment with `pip`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Commands

The repository launcher is the preferred entry point during local development:

```bash
scripts/start_opencollab.sh
```

It uses `.venv`, checks `configs/.env`, and starts the interactive agent (agent
0, the *lead*). There is no separate "team" subcommand: the same run becomes a
multi-agent team when `configs/team.yaml` is present.

Direct CLI commands are also available after installation. Activate the venv or
call the installed binary directly:

```bash
.venv/bin/opencollab --workspace .
OPENCOLLAB_WORKFLOWS_DIR=path/to/workflows \
  .venv/bin/opencollab workflow run NAME --args '{"goal": "..."}'
```

The workflow directory contains caller-authored Python modules tagged with
`@workflow`. The same `OPENCOLLAB_WORKFLOWS_DIR` setting applies to both
`opencollab workflow list` and `opencollab workflow run`.

Useful flags: `--trace` records a trajectory (every LLM call and tool exec);
`--no-worktrees` disables per-child git-worktree isolation; `--yolo`
auto-approves risky commands.

## External integrations

Evaluation runners and benchmark-specific workflows live in the companion
OpenCollab-Eval repository. Integrations depend on `opencollab.sdk`; OpenCollab
can evolve its internal layers without forcing evaluation code to track private
module paths.

## Python SDK

`opencollab.sdk` is the supported integration facade. Its `SDK_API_VERSION`
identifies the compatibility contract independently of the package release
version; SDK v2 covers single-agent runs, workflow runs, execution
environments, and curated coding tools.

```python
import asyncio
import os

from opencollab.sdk import (
    AgentRunBudget,
    AgentRunRequest,
    OpenCollabRuntime,
    RuntimeConfig,
    coding_toolset,
)


async def main() -> None:
    request = AgentRunRequest(
        prompt="Inspect this repository and report its release readiness.",
        config=RuntimeConfig(
            model=os.environ["OPENCOLLAB_MODEL"],
            provider=os.environ.get("OPENCOLLAB_PROVIDER", "openai"),
            api_key=os.environ.get("OPENCOLLAB_API_KEY"),
            base_url=os.environ.get("OPENCOLLAB_BASE_URL"),
        ),
        budget=AgentRunBudget(max_tokens=100_000, max_steps=20),
        tools=coding_toolset(),
        workspace=".",
    )
    result = await OpenCollabRuntime().run_agent(request)
    print(result.output)


asyncio.run(main())
```

Import public names from `opencollab.sdk` or its documented capability modules.
Treat other package paths as internal. An `artifact_dir`, when supplied, must be
new or empty because each SDK run claims it for executable evidence.

## Architecture

OpenCollab follows a strict clean architecture: dependencies point inward only,
`adapters → application → domain`.

- `opencollab/domain/` — pure value objects and the session FSM. Stdlib only,
  no I/O.
- `opencollab/application/` — use cases, the scheduler, messaging, and the port
  Protocols (`opencollab/application/ports.py`). Imports `domain` + stdlib only.
- `opencollab/adapters/` — concrete implementations: `cli/`, `tui/`, `llm/`,
  `tools/`, environments, tracing, and the session store.
- `opencollab/bootstrap/` — the composition root; the only layer that knows
  concrete types.
- `opencollab/sdk/` — the versioned integration boundary for external packages.
- `tests/` — characterization and regression tests, including the import-
  direction guards `test_domain_boundaries.py` and
  `test_application_boundaries.py`.

The dependency direction is enforced, not just documented: an inward-to-outward
import turns the boundary tests red. Every outward capability is a Protocol
*port* in `application/ports.py`; only `bootstrap/` knows concrete types, so
swapping an LLM, environment, or tool library is a new adapter plus one line of
wiring. The `domain`/`application` core imports no LLM and does no I/O, so its
suite runs without network access.

## How it works

Two core abstractions, then five swappable components behind ports.

**A single agent is a *validated state machine*** (`domain/session.py`). Edges
that are not in the transition table raise instead of silently passing, and
every way a turn can stop early is a *named terminal state* — `DONE`,
`BUDGET_EXCEEDED`, `STEP_LIMIT_EXCEEDED`, `CONTEXT_OVERFLOW`, `CANCELLED`,
`ERROR` — so an unhandled failure can never masquerade as a clean finish.

**A team is modeled on OS process concurrency.** The session table is the
process table, `spawn` is `fork`, budget is reserved *before* the first `await`
(so a batch of children cannot overspend the pool), and agents suspend and wake
on a cooperative scheduler. Each spawned child works in its own git worktree so
siblings cannot clobber each other's edits, and its diff is appended to its
result for the parent on completion.

The components you are most likely to swap, each behind a port:

| Component | What it does |
|-----------|--------------|
| **Context manager** | Context is an editable bundle of *sources* (identity / team / task / …), not one string. A deterministic shaping pipeline projects a bounded view at each model call — shed low-priority sources, clear stale tool output, snip old turns — while the persisted transcript stays lossless. |
| **Tool manager** | A name-keyed registry (`name → factory`) with a stateless executor and loop detection. Adding a tool is subclassing `Tool` plus one factory line; the executor is tool-agnostic. |
| **LLM provider** | A facade behind `LLMPort`, one module per provider, so each provider's quirks (for example, recovering markup-leaked tool calls or estimating missing usage) stay isolated. The OpenAI-compatible path is the default; a native Anthropic path (`provider=anthropic`) also exists. |
| **Workflow engine** | Point the CLI or SDK at a directory containing `@workflow`-tagged Python modules. `ctx.agent / parallel / pipeline` provide Python-guaranteed control flow over one-shot agent sessions. |
| **Skill store** | On-demand instruction sets the model loads by name through one generic `use_skill` tool (opt-in per role) — extend an agent's know-how without adding tools. Drop a `SKILL.md` under `skills/` and it is auto-catalogued. |

## Status

**Proven:** the core invariants (state-machine edges, budget reservation,
context shaping, import boundaries) pinned by the fast test suite.

**In progress / honest gaps:** the port-level *ablations* this architecture was
built to make cheap (hold the model fixed, swap one component, measure the
delta) have not been run yet; a workflow-vs-team A/B is open; prompt caching is
not wired; the session FSM does not yet persist budget/phase across restarts.
See [`docs/`](docs/) for design documents and project reviews.

## Making changes

1. Locate the module by listing or grepping the layer directories above — the
   layout is small and the names are descriptive.
2. Respect the dependency direction `adapters → application → domain`. Ports
   live in `opencollab/application/ports.py`; only `bootstrap/` knows concrete
   types.
3. Run `uv run ruff check .` and `uv run pytest -q` from the repository root.
4. If your change moves or renames modules, update the affected documentation
   in the same change.

An archived module-by-module map and dependency graph (a snapshot, not kept in
sync) lives under [`docs/archive/repomap/`](docs/archive/repomap/).

Contributing: see [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`CLAUDE.md`](CLAUDE.md). Use Conventional Commits; `refactor:` commits stay
behavior-preserving.

## License

OpenCollab is licensed under the [Mulan Permissive Software License v2](LICENSE)
(`MulanPSL-2.0`). The complete license text is included in source and wheel
distributions.
