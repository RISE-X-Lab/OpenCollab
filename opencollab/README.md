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

The package root is the complete everyday API: one stateful client, one result
type, one error, and the workflow decorator. Compatibility follows package
SemVer.

```python
import asyncio

from opencollab import OpenCollab


async def main() -> None:
    oc = OpenCollab(".")  # resolves configs/.env and environment variables once
    result = await oc.agent(
        "Inspect this repository and report its release readiness.",
        budget=100_000,
        artifacts="artifacts/release-readiness",
    )
    print(result.raise_for_status().output)


asyncio.run(main())
```

The same client exposes `agent(...)`, `team(...)`, and `workflow(...)`; all
return `RunResult`. Import optional authoring contracts from
`opencollab.tools`, `opencollab.environments`, and `opencollab.workflows`.
Treat other package paths as internal. An `artifacts` directory, when supplied,
must be new or empty because each run claims it for executable evidence.

`OpenCollab.configuration` is a read-only snapshot of effective model,
provider, budget, timeout, sampling, output-token, and thinking settings.
`thinking_params` is deep-copied so callers cannot mutate client state. API keys
and base URLs are excluded. `base_url_sha256` provides a stable endpoint
fingerprint without exposing credentials embedded in a URL. Agent runs accept
`max_steps` and `cleanup_timeout`; the older `steps` spelling remains a supported
alias.
Workflow runs accept `max_steps`, `system_prompt`, and `cleanup_timeout`.
Completed, stopped, and failed workflow results report aggregate session,
step, token, and markup-recovery metrics. Sanitized child-provider failures are
available through `RunResult.agent_failures`.

Built-in tools can be composed without importing concrete adapters:

```python
from opencollab.tools import builtin_tools

tools = builtin_tools(
    "file_read",
    "file_write",
    "run_tests",
    allow_file_creation=False,
)
```

The helper returns fresh tools in caller order. Headless shell and test tools
require process-isolated environments, test-runner overrides stay disabled,
and limits for unselected tools are rejected. The `run_tests` instance satisfies
the public `VerificationTool` protocol. Its read-only `verified_targets`
property contains exact requested targets whose latest parser-backed verdict
was green.

Environment composition uses the same narrow public module:

```python
from opencollab.environments import (
    docker_environment,
    local_environment,
    worktree_environment,
)

host = local_environment(".")
isolated_source = worktree_environment(".")
container = docker_environment("python:3.11-slim", isolated_source)
```

These factories construct caller-owned environments without eagerly setting
them up. The caller retains setup, mount, and cleanup timing. Concrete adapter
classes stay behind the bootstrap boundary.

### Workflow authoring

A workflow is a plain async Python function tagged with `@workflow`. Create
`workflows/implement_and_review.py`:

```python
from typing import Any

from opencollab import workflow
from opencollab.workflows import WorkflowContext


@workflow(name="implement-and-review")
async def implement_and_review(
    ctx: WorkflowContext,
    inputs: dict[str, Any],
) -> str | dict[str, Any] | None:
    draft = await ctx.agent(
        f"Implement and verify: {inputs['task']}",
        tools=inputs.get("tools"),
        timeout=900,
    )
    await ctx.log(f"Tokens spent: {ctx.tokens_spent()}")
    diff = await ctx.diff()
    return await ctx.agent(
        f"Review the implementation and fix gaps:\n{draft}\n\nDiff:\n{diff or ''}"
    )
```

OpenCollab discovers top-level `*.py` modules in `workflows/` by default. Run
the decorated function through the CLI:

```bash
uv run opencollab workflow run implement-and-review \
  --args '{"task": "Add a regression test for the target bug."}'
```

Set `OPENCOLLAB_WORKFLOWS_DIR` to use another directory. The same decorated
function can be passed directly to `await OpenCollab(".").workflow(...)` when
embedding OpenCollab in Python.

For a visual architecture walkthrough, open the
[SDK 0.4 research architecture](../docs/sdk-v3-explainer.html).

Evaluation runners and benchmark-specific workflows intentionally live outside
the framework package. Topology research uses `team(...)` and `workflow(...)`;
external harnesses define ablations while Bootstrap binds each treatment
through the Clean Architecture ports.

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
