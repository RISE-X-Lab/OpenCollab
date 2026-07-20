# OpenCollab Python Package

This guide documents the installed package and the framework internals behind
the concise [project homepage](../README.md). Run repository commands from the
repository root unless noted otherwise.

## Install

OpenCollab supports Linux and macOS hosts. Its local file adapters require
descriptor-relative operations, no-follow `stat`, `O_NOFOLLOW`, fd-based
directory listing, callable `fcntl.flock`, and an atomic no-clobber rename:
`renameat2(RENAME_NOREPLACE)` on Linux or `renameatx_np(RENAME_EXCL)` on macOS
10.12 and newer. Missing primitives produce an explicit capability error.

Install the project and development dependencies with `uv`:

```bash
uv sync --extra dev
```

Or create a conventional virtual environment with `pip`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Commands

The repository launcher is the preferred entry point during development:

```bash
scripts/start_opencollab.sh
```

It uses `.venv`, checks `configs/.env`, and starts agent 0, the lead. There is
no separate `team` subcommand: the same run becomes a configured multi-agent
team when `configs/team.yaml` is present.

After installation, invoke the CLI directly:

```bash
.venv/bin/opencollab --workspace .
OPENCOLLAB_WORKFLOWS_DIR=path/to/workflows \
  .venv/bin/opencollab workflow run NAME --args '{"goal": "..."}'
```

A workflow directory contains caller-authored Python modules tagged with
`@workflow`. `OPENCOLLAB_WORKFLOWS_DIR` applies to both `workflow list` and
`workflow run`.

Useful flags:

- `--trace` records every LLM call and tool execution.
- `--no-worktrees` disables per-child git-worktree isolation.
- `--yolo` auto-approves risky commands.

## Python SDK

`opencollab.sdk` is the versioned integration boundary. Its
`SDK_API_VERSION` is independent of the package release version. SDK v2 covers
single-agent runs, workflow runs, execution environments, and curated coding
tools.

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
Treat other package paths as internal. An `artifact_dir`, when supplied, must
be new or empty because each SDK run claims it for executable evidence.

Evaluation runners and benchmark-specific workflows live in the companion
OpenCollab-Eval repository and consume this SDK boundary.

## Architecture

OpenCollab follows strict clean architecture: dependencies point inward only,
`adapters → application → domain`.

- `domain/` contains pure value objects and the session state machine. It uses
  the standard library and performs no I/O.
- `application/` contains use cases, scheduling, messaging, and the Protocol
  ports in `application/ports.py`.
- `adapters/` contains the CLI, TUI, LLM providers, tools, environments,
  tracing, and persistence implementations.
- `bootstrap/` is the composition root and the only layer that wires concrete
  types together.
- `sdk/` is the versioned integration surface for external packages.

Boundary tests enforce the dependency direction. An outer capability needed by
the application becomes a port; its concrete implementation is wired in
`bootstrap/`. This keeps the domain and application testable without network or
filesystem access.

## How it works

### Validated agent sessions

A single agent is a validated state machine in `domain/session.py`. Invalid
edges raise instead of silently passing. Every early stop is one of three
named terminal states: `DONE` (a final answer), `ERROR` (an unhandled fault),
and `STOPPED`-a single graceful-stop terminal whose `reason` records whether
the halt was a budget, step, loop, or context overflow.

### Cooperative teams

A team follows an OS-process-inspired model: the session table is the process
table, `spawn` resembles `fork`, and agents suspend and wake through a
cooperative scheduler. Budget is reserved before the first `await`, preventing
a concurrent child batch from overspending the shared pool. Each child can work
in an isolated git worktree, and its diff returns to the parent with its result.

### Swappable components

| Component | Responsibility |
| --- | --- |
| **Context manager** | Builds a bounded view from prioritized context sources while keeping the persisted transcript lossless. |
| **Tool manager** | Provides a name-keyed registry, stateless execution, safety checks, and loop detection. |
| **LLM provider** | Isolates OpenAI-compatible and native Anthropic behavior behind `LLMPort`. |
| **Workflow engine** | Gives Python explicit control over agent fan-out, pipelines, phases, and verification. |
| **Skill store** | Loads named instruction sets on demand through the generic `use_skill` tool. |

## Status

The test suite pins core invariants such as session transitions, budget
reservation, context shaping, and import boundaries.

Open research gaps remain: port-level ablations have not yet been run,
workflow-versus-team comparisons are incomplete, prompt caching is not wired,
and the session state machine does not yet persist budget and phase across
restarts. See [`../docs/`](../docs/) for design records and research notes.

## Development

Respect the dependency direction and keep public names re-exported when modules
move. From the repository root, run:

```bash
uv run ruff check .
uv run pytest -q
```

See [`../CONTRIBUTING.md`](../CONTRIBUTING.md) for contributor guidance and
[`../CLAUDE.md`](../CLAUDE.md) for repository-specific architecture notes. The
archived module map under [`../docs/archive/repomap/`](../docs/archive/repomap/)
is a historical snapshot rather than a maintained source of truth.

## License

OpenCollab is licensed under the
[Mulan Permissive Software License v2](../LICENSE) (`MulanPSL-2.0`).
