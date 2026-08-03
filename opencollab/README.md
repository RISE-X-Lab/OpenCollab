# OpenCollab Python Package

OpenCollab is installed as a Python package and exposes a small public API over
the framework runtime. The [project homepage](../README.md) gives the quick
start. Run repository commands from the repository root unless a command says
otherwise.

## Install

OpenCollab supports Linux and macOS hosts. Its local file adapters require
descriptor-relative operations, no-follow `stat`, `O_NOFOLLOW`, fd-based
directory listing, callable `fcntl.flock`, and an atomic no-clobber rename. The
rename primitive is `renameat2(RENAME_NOREPLACE)` on Linux and
`renameatx_np(RENAME_EXCL)` on macOS 10.12 and newer. OpenCollab reports a
capability error when the host lacks a required primitive.

Install the project and development dependencies with `uv`.

```bash
uv sync --extra dev
```

Or create a conventional virtual environment with `pip`.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Commands

During development, run the registered CLI from the project environment.

```bash
uv run opencollab --workspace .
```

It runs the current checkout in the project environment, resolves
`configs/.env`, and starts agent 0 with the built-in lead-only configuration.
Pass `--team-config PATH` to select a declared multi-agent team.
`scripts/start_opencollab.sh` remains available for environments that need its
physical-path handling.

After installation, invoke the CLI directly from the active environment.

```bash
opencollab --workspace .
opencollab --team-config configs/team.yaml --workspace .
OPENCOLLAB_WORKFLOWS_DIR=path/to/workflows \
  opencollab workflow run NAME --args '{"goal": "..."}'
```

A workflow directory contains caller-authored Python modules tagged with
`@workflow`. `OPENCOLLAB_WORKFLOWS_DIR` applies to both `workflow list` and
`workflow run`.

The following flags control tracing, isolation, team selection, and approvals.

- `--trace` records every LLM call and tool execution.
- `--no-worktrees` disables per-child git-worktree isolation.
- `--team-config PATH` selects a team YAML file.
- `--yolo` auto-approves risky commands.

## Python SDK

The package root exposes `OpenCollab`, `RunResult`, `RunError`, and the
`workflow` decorator. Compatibility follows package SemVer.

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

The same client exposes `agent(...)`, `team(...)`, and `workflow(...)`. All
three return `RunResult`. Import optional authoring contracts from
`opencollab.tools`, `opencollab.environments`, and `opencollab.workflows`.
Treat other package paths as internal. An `artifacts` directory, when supplied,
must be new or empty because each run claims it for executable evidence.
`team(...)` uses the built-in lead-only configuration unless its `config=`
argument names a team YAML file.

`OpenCollab.configuration` is a read-only snapshot of effective model,
provider, budget, timeout, sampling, output-token, and thinking settings.
`thinking_params` is deep-copied, so callers receive an independent snapshot. API keys
and base URLs are excluded. `base_url_sha256` provides a stable endpoint
fingerprint without exposing credentials embedded in a URL. Agent runs accept
`max_steps` and `cleanup_timeout`. The older `steps` spelling remains a supported
alias.
Workflow runs accept `max_steps`, `system_prompt`, and `cleanup_timeout`.
Completed, stopped, and failed workflow results report aggregate session,
step, token, and markup-recovery metrics. Sanitized child-provider failures are
available through `RunResult.agent_failures`.

Use `builtin_tools` to compose tools through the public package.

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

Environment composition uses the same narrow public module.

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

These factories return caller-owned environments whose setup has not started.
Every public environment supports `await environment.setup()`. Docker
environments also accept `mount_dir`. Host and worktree environments raise an
argument error for `mount_dir`. The caller controls setup, mount, and cleanup
timing. Concrete adapter classes live in `adapters`, and Bootstrap wires them.

Run metrics separate OpenCollab-owned session quiescence from environment
quiescence. `session_quiesced` proves that session and persistence work has
finished. For a caller-owned environment, `environment_quiesced`,
`cleanup_quiesced`, and `execution_quiesced` remain `None` until the caller
performs and verifies its own environment cleanup.

### Workflow authoring

A workflow is a plain async Python function tagged with `@workflow`. Create
`workflows/implement_and_review.py` as shown below.

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
the decorated function through the CLI.

```bash
uv run opencollab workflow run implement-and-review \
  --args '{"task": "Add a regression test for the target bug."}'
```

Set `OPENCOLLAB_WORKFLOWS_DIR` to use another directory. The same decorated
function can be passed directly to `await OpenCollab(".").workflow(...)` when
embedding OpenCollab in Python.

Evidence-preserving workflows can call `ctx.draft_findings(...)` to capture a
structured cite-or-abstain draft before exploration. OpenCollab owns this
generic capture primitive. OpenCollab-Eval owns benchmark prompts, policies,
datasets, and runners.

For a visual architecture walkthrough, open the
[SDK 0.4 research architecture](../docs/sdk-0.4-explainer.html).

OpenCollab-Eval contains evaluation runners and benchmark workflows. Topology
research uses `team(...)` and `workflow(...)`. External harnesses define
ablations, and Bootstrap binds each treatment through the Clean Architecture
ports.

## Architecture

OpenCollab follows strict clean architecture. Dependencies point inward along
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
the application becomes a port. Its concrete implementation is wired in
`bootstrap/`. This keeps the domain and application testable without network or
filesystem access.

## How it works

### Validated agent sessions

A single agent is a validated state machine in `domain/session.py`. Invalid
edges raise an error. Every early stop has a named terminal state. `DONE`
contains a final answer, `ERROR` records an unhandled fault, and `STOPPED`
records a graceful halt. Its `reason` identifies a budget, step, loop, or
context overflow.

### Cooperative teams

A team stores active agents in a session table and schedules them cooperatively.
The `spawn` operation creates child sessions. Budget is reserved before the
first `await`, so a concurrent child batch cannot overspend the shared pool.
Each child can work in an isolated git worktree and return its diff with the
result.

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

Port-level ablations and workflow-versus-team comparisons have not been
completed. Prompt caching is unavailable. Budget and phase reset when a session
restarts. See [`../docs/`](../docs/) for design records and research notes.

## Development

Respect the dependency direction and keep public names re-exported when modules
move. Run the checks from the repository root.

```bash
uv run ruff check .
uv run pytest -q
```

See [`../CONTRIBUTING.md`](../CONTRIBUTING.md) for contributor guidance and
[`../CLAUDE.md`](../CLAUDE.md) for repository-specific architecture notes. The
archived module map under [`../docs/archive/repomap/`](../docs/archive/repomap/)
records an earlier revision. Use the current source and tests for the current
module layout.

## License

OpenCollab is licensed under the
[Mulan Permissive Software License v2](../LICENSE) (`MulanPSL-2.0`).
