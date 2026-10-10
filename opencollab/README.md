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
`configs/.env`, and starts agent 0 with the built-in Self-Collaboration team.
Team runs use the root command with `--team-config PATH`. To work with team files,
use `team init` to create an editable team file and `team show` to inspect its
roles and topology. `scripts/start_opencollab.sh` remains available for
environments that need its physical-path handling.

After installation, invoke the CLI directly from the active environment.

```bash
opencollab --workspace .
opencollab team init team.yaml
opencollab team show --team-config team.yaml
opencollab --team-config team.yaml --workspace .
opencollab workflow list --workspace .
OPENCOLLAB_WORKFLOWS_DIR=path/to/workflows \
  opencollab workflow run NAME --args '{"goal": "..."}'
```

A workflow directory contains caller-authored Python modules tagged with
`@workflow`. Both `workflow list` and `workflow run` combine them with installed
workflows, including [Duo](../docs/duo.md). `OPENCOLLAB_WORKFLOWS_DIR` selects the
caller directory. Relative paths resolve from the workspace, and duplicate
names raise the registry's existing error.

In an interactive session, `/help` shows the local controls. Tab and Shift+Tab
select an agent, `/save` saves the lead session, and `/exit` leaves the terminal.
A reply to a waiting agent question is delivered before local commands.

For workflows that accept a `goal`, `--task TEXT` and `--task-file PATH` provide
that argument directly. Other arguments still use `--args`; an explicit `goal`
in that JSON conflicts with either shortcut.

For the root team command, `--trace` enables trajectory recording,
`--no-worktrees` disables per-child git-worktree isolation, and
`--team-config PATH` selects a team YAML file. `--yolo` auto-approves risky
commands. `--prompt TEXT` or `--prompt-file PATH` runs one turn and exits.
Add `--hold` to inspect agents at the prompt after that turn.

Workflow options follow `workflow run NAME` or `workflow list`. For example,
use `opencollab workflow run NAME --workspace . --no-trace`. Workflow runs
save sessions and record orchestration by default. `--no-trace` keeps saved
sessions and the workflow manifest, while `--no-save` disables run artifacts.

## Python SDK

The package root exposes `OpenCollab`, `RunResult`, `RunError`, the
`workflow` decorator, and `RunControl`, `BudgetSnapshot`, `BudgetDecision`,
and `RunEvent` for controlled agent runs. Compatibility follows package SemVer.

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
Installed collaboration protocols are public through
`opencollab.builtin_workflows`, and Git patch parsing is public through
`opencollab.patches`. Named agent profile resolution is public through
`opencollab.profiles`.
Treat other package paths as internal. An `artifacts` directory, when supplied,
must be new or empty because each run claims it for executable evidence.
`team(...)` uses the built-in Self-Collaboration team unless its `config=`
argument names a team YAML file. Its `cleanup_timeout` bounds scheduler
shutdown and must be a finite positive number.

`agent(...)` uses Base, which currently maps to the
[Single2 profile](../docs/single2.md). `agent(..., profile="base")` follows the
same mapping. `agent(..., profile="single2")` and the `agent2(...)` convenience
method select Single2 explicitly. The `default` and `single` spellings are
compatibility aliases for Base. Run metrics record the concrete profile name.
`opencollab.profiles.resolve_profile_name(...)` exposes this resolution to
integrations. Team configuration and workflow role configuration keep their
own selection paths.

`agent(...)` and `agent2(...)` accept an optional `run_control`. The effective
`budget` authorizes a hard token ceiling. `initial_soft_budget_tokens` starts a
smaller allowance, clipped to that actual authorization. A synchronous or async
`decide_budget(snapshot)` callback may keep or increase the soft allowance by
returning `BudgetDecision(soft_budget_tokens, final_prompt=None)`. The runner
calls the callback at each PRECHECK after cancellation and loop checks, before
budget-driven wind-down, and after input estimation for every provider attempt.
The host policy chooses when to extend or issue a closing prompt while there
is still enough allowance for the current request. Each snapshot includes the run and session IDs, token and step
counters, both allowances, input reservation, minimum output requirement, and
trigger reason. Suggestions that decrease the allowance, increase it to a value below actual
spend, or exceed the hard authorization produce a failed result carrying the
policy error. Keeping the current allowance after usage crosses it produces
the ordinary soft-budget stop. Exhausting the hard authorization produces the ordinary budget
stop. A `final_prompt` enters the current request and its input reservation is
recomputed before the provider call.

```python
from opencollab import BudgetDecision, RunControl


def decide(snapshot):
    allowance = snapshot.soft_budget_tokens
    if snapshot.used_tokens > 0 and progress_evidence_is_available():
        allowance = min(snapshot.hard_budget_tokens, allowance + 50_000)
    return BudgetDecision(allowance)


result = await oc.agent2(
    "Fix the failing regression.",
    budget=200_000,
    run_control=RunControl(initial_soft_budget_tokens=100_000, decide_budget=decide),
)
```

An `on_event(event)` receiver runs synchronously and independently of `trace`.
Its compact `RunEvent` carries `type`, `run_id`, `session_id`, `aid`,
`used_tokens`, `steps`, and a detached `data` mapping. `usage` events report
normalized input, output, and total tokens at each successful or failed
provider attempt's accounting point, including summary and late responses.
The `purpose` and `late` fields identify those paths. Cache-read,
cache-creation, reasoning, and estimated-usage fields preserve provider
metadata. Cached input is already included in input and total token counters. `context_shaping` events
report normal and emergency compaction. `budget_decision`, `error`, and
`session_stopped` events expose decisions and lifecycle state. The stopped
event records outstanding provider requests. The receiver stays attached to its
original run through late accounting and cleanup, then receives
`cleanup_completed`. Receiver exceptions remain readable in
`result.metrics["observation_errors"]` after accounting and cleanup finish.

`RunControl.history_trigger_tokens` optionally caps the history-compaction
trigger for this run. It must be an integer of at least two. For a known model
window, the effective trigger is the smaller of the cap and the model-derived
trigger, and the target remains 75 percent of that trigger. Unknown windows
retain the fixed default thresholds. Omission uses the ordinary model-derived
thresholds. Enabled trajectories record the effective threshold and its source.
`RunControl.tool_cancellation_cleanup_timeout` sets this session's existing
tool cancellation cleanup wait. It accepts a finite positive number of seconds.
Omission retains the configured runtime default.

`RunResult.status` can be `completed`, `stopped`, or `failed`. A completed result
has `ok=True`, while stopped and failed results have `ok=False`. Stopped results
include a `reason` field (such as a token limit or run timeout). The
`raise_for_status()` method returns completed results unchanged and raises
`RunError` for stopped or failed results, with the original result accessible
via `exception.result`. Invalid arguments raise their validation error before
the run starts. Lifecycle and persistence failures can raise `RunError` directly.

All three methods accept a caller-chosen `run_id`. When omitted, OpenCollab
generates an ID for that run. `result.metrics["run_id"]` matches the saved
agent snapshot, team manifest, or workflow manifest and any enabled trajectory.
This identity is available with `trace=False` as well.

`OpenCollab.configuration` is a read-only snapshot of effective model,
provider, budget, timeout, sampling, output-token, and thinking settings.
`thinking_params` is deep-copied, so callers receive an independent snapshot. API keys
and base URLs are excluded. `base_url_sha256` provides a stable endpoint
fingerprint without exposing credentials embedded in a URL. Agent runs accept
`max_steps` and `cleanup_timeout`. The older `steps` spelling remains a supported
alias.
`timeout` bounds the whole agent, team, or workflow run. The resolved
`llm_timeout` and connection and stream timeouts control model transport.
`cleanup_timeout` separately bounds owned shutdown. See
[configured evaluation runtime](../docs/evaluation-runtime.md) for cancellation,
token accounting, and unbounded limits, and [configuration](../configs/README.md)
for protocol selection and transport settings.
Workflow runs accept `max_steps`, `system_prompt`, and `cleanup_timeout`.
Their first argument accepts an installed workflow name such as `"duo"`, a
caller-authored name, or a workflow function or spec. Names use the same
workspace directory resolution as the CLI. Functions and specs run directly.
Their `concurrency` option limits agent sessions only. `task_concurrency`
separately limits active `parallel` and `pipeline` units across the workflow,
including candidate child workflows;
omitting it inherits `concurrency`. The two limits are independent, so mixed
agent and task work may peak at their sum.
Completed, stopped, and failed workflow results report aggregate session,
step, token, and markup-recovery metrics. Sanitized child-provider failures are
available through `RunResult.agent_failures`. Nested failures and operating-system
errors also include a bounded `exception_chain` with exception types, modules,
integer `errno` values, and HTTP status codes when available. Messages, request
bodies, and filesystem paths are excluded from this structured evidence.

Use `builtin_tools` to compose tools through the public package.

```python
from opencollab.tools import builtin_tools

tools = builtin_tools(
    "file_read",
    "file_write",
    "bash",
    allow_file_creation=False,
)
```

The helper returns fresh tools in caller order. Headless shell tools require
process-isolated environments, and limits for unselected tools are rejected.
Use `bash` to run the repository's native test command and inspect its exit code
and output. The `VerificationTool` protocol remains available for tools that
provide parser-backed test evidence. `evidence_tools` composes native tools
with a Bash observer for workflows such as Duo. See
[native test evidence](../docs/test-evidence.md) for record collection and
supported runners.
See [test-runner migration](../docs/migrations/remove-run-tests.md) for existing
team files, saved runs, and verifier integrations.

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
`workflows/implement_and_review.py` with this implementation.

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

OpenCollab discovers top-level `*.py` modules in `workflows/` by default and
combines them with installed workflows in the CLI and SDK name lookup. Run
the decorated function through the CLI.

```bash
uv run opencollab workflow run implement-and-review \
  --args '{"task": "Add a regression test for the target bug."}'
```

Set `OPENCOLLAB_WORKFLOWS_DIR` to use another directory. The same decorated
function can be passed directly to `await OpenCollab(".").workflow(...)` when
embedding OpenCollab in Python. The [Duo guide](../docs/duo.md) shows built-in
workflow calls and the [Chinese guide](../docs/duo/README.zh-CN.md) gives the same
usage examples.

Evidence-preserving workflows can call `ctx.draft_findings(...)` to capture a
structured cite-or-abstain draft before exploration. OpenCollab owns this
generic capture primitive. OpenCollab-Eval owns benchmark prompts, policies,
datasets, and runners.

For a visual architecture walkthrough, open the
[SDK 0.4 research architecture](../docs/sdk-0.4-explainer.html).

OpenCollab owns built-in collaboration protocols such as Duo. OpenCollab-Eval
contains evaluation runners and benchmark-specific workflows. Topology
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
edges raise an error. The state machine has three terminal states. `DONE`
contains a final answer, `ERROR` records an unhandled fault, and `STOPPED`
records a graceful halt. Its `reason` identifies a budget, step, loop, or
context overflow.

### Cooperative teams

A team stores active agents in a session table and schedules them cooperatively.
The `spawn` operation creates child sessions. Budget is reserved before the
first `await`, so a concurrent child batch cannot overspend the shared pool.
A team file may instead give each role its own allowance; each agent is then
held to that allowance alone, and the team's total is their sum.
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

The runtime ports support component replacement and controlled comparisons.
See [benchmark results](../docs/results.md) and
[collaboration adherence](../docs/adherence.md) for experiment settings and
reported observations.

Responses requests carry a `prompt_cache_key` scoped to the model
client and response session. Provider-reported cache reads and cache creation
are retained in usage records. Cache availability and reuse follow the endpoint.
Session snapshots restore token usage and recoverable terminal or awaiting-event
phases. In-flight provider and tool
phases resume from `IDLE` because their process-local coroutines do not survive
a restart. See [`../docs/`](../docs/) for design records and research notes.

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
