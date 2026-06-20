# workflows/

Deterministic multi-agent **workflows** — plain Python that orchestrates
one-shot agent sessions. Unlike a `team` (where an LLM *lead* decides who to
spawn via `spawn_agent`), a workflow's control flow is ordinary Python: loops,
round caps, never-identical retries, parallel fan-out, and stop conditions are
*guaranteed* by code, not prompted.

Each file here is one workflow. Drop a new `*.py` file in this directory and it
is discovered automatically — no registry edit, no `__init__.py`.

## Bundled workflows

| Name | Shape | Front half | Solve loop |
|------|-------|-----------|-----------|
| `self-collab` | sequential phases | analyze → parallel plan review (2 lenses, one revision) | per-phase coder/tester, **stops** on first failed phase |
| `split-solve` | independent subtasks | analyze → split into disjoint subtasks | per-subtask coder/tester (failures don't block others) → synthesize |
| `scout-solve` | parallel reconnaissance | analyze into dimensions → parallel read-only scouts → synthesize a brief | single coder/tester loop from the brief |

Pick the front half by how the *uncertainty* is shaped: one linear path
(`self-collab`), several disjoint fixes (`split-solve`), or one fix that first
needs broad understanding (`scout-solve`).

## How discovery works

`discover_workflows("workflows")`
(`opencollab/opencollab/bootstrap/workflow_runtime.py`) imports every top-level
`*.py` file in this directory and registers every module-level value carrying a
`__workflow_spec__` attribute (attached by the `@workflow` decorator).

- Files starting with `_` (and dunder names like `__init__.py`) are **skipped** —
  use a leading underscore for shared helper modules you don't want registered.
- The registry **rejects duplicate names**, so each `name=` must be unique.
- The scan directory is `workflows/` relative to the working directory, override
  with the `OPENCOLLAB_WORKFLOWS_DIR` env var.
- A missing directory yields an empty registry (no error).

## Writing a new workflow

A workflow is a single async function `async def fn(ctx, args) -> Any` tagged
with `@workflow`. Minimal skeleton:

```python
"""my-flow — one-line summary of the topology."""
from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, GrepTool
from opencollab.application.workflow_registry import workflow


@workflow(
    name="my-flow",                       # unique; this is the CLI name
    description="What it does, in one line.",
    phases=["analyze", "solve"],          # optional; labels for progress display
)
async def my_flow(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    # `goal` for CLI runs; `description` is what the eval harness passes.
    goal = str(args.get("goal") or args.get("description") or "").strip()
    if not goal:
        return {"status": "error", "error": 'missing "goal"'}

    await ctx.phase("analyze")
    findings = await ctx.agent(
        f"Investigate, read-only, then report:\n{goal}",
        label="analyst",
        tools=[BashTool(), FileReadTool(), GrepTool()],
    )

    await ctx.phase("solve")
    # ... drive a coder/tester loop, fan out, synthesize, etc.
    return {"status": "done", "findings": findings, "tokens_spent": ctx.budget.spent()}
```

That's the whole registration step — save the file, then `opencollab workflow
list` shows it. **No other file needs editing.**

### The `ctx` primitives (`application/workflow.py`)

| Primitive | Signature | Returns |
|-----------|-----------|---------|
| `ctx.agent` | `agent(prompt, *, schema=None, label=None, tools=None, isolation=False)` | the session's final text (`str`), or the validated `dict` when `schema=` is given, or `None` if the agent died |
| `ctx.parallel` | `parallel(thunks)` — `thunks` are zero-arg callables returning awaitables | `list` in input order; a thunk that raises → `None` in its slot |
| `ctx.pipeline` | `pipeline(items, *stages)` — each `stage(prev, item, idx)`; **no** inter-stage barrier | `list` in input order; a stage that raises drops that item to `None` |
| `ctx.phase` / `ctx.log` | `phase(title)` / `log(message)` | observability; printed by the CLI as `== title` / `-- message` (no-op when nothing is wired) |
| `ctx.budget` | `.total` (`int \| None`), `.spent()`, `.remaining()` | live token accounting across every session created so far |

**Concurrency** is bounded by a shared semaphore (CLI `--concurrency`, default 4);
`parallel`/`pipeline` may pass more items than that — the excess queues.

**Structured output:** pass `schema=<JSON Schema dict>`. The engine injects a
`structured_output` tool, instructs the agent to finish by calling it, validates
the payload, and returns the dict (one corrective retry on the same session,
then `None`). Always guard: `if not isinstance(result, dict): ...`.

**Parallel fan-out** — bind loop variables with default args so the late-binding
closure bug doesn't make every thunk see the last item:

```python
reports = await ctx.parallel(
    [
        (lambda d=d, i=i: ctx.agent(PROMPT.format(**d), label=f"scout:{i}", tools=_read_tools()))
        for i, d in enumerate(dimensions)
    ]
)
```

### Failure contract (important)

Every primitive localizes failure: a dead agent, a raising thunk, or a raising
pipeline stage yields `None` for *that unit of work only* and **never aborts the
fleet**. The single exception that escapes is `WorkflowBudgetExceeded`, raised by
`ctx.agent` only when the budget is *already* exhausted before a call starts. So:

- check `if result is None` / `if not isinstance(result, dict)` after every
  `ctx.agent` and substitute a sensible fallback;
- treat partial results from `parallel`/`pipeline` as normal (`.filter`/skip the
  `None`s, and `log` the ratio so a silent loss is visible).

### Tools (`opencollab/adapters/tools/`)

Provision each agent with exactly the tools its role needs (least privilege):

| Tool | Module | Role typically using it |
|------|--------|------------------------|
| `BashTool` | `tools.bash` | everyone (escape hatch) |
| `FileReadTool`, `GrepTool` | `tools.fs` | read-only: analyst / reviewer / scout / tester |
| `FileWriteTool` | `tools.fs` | coder / synthesizer |
| `ApplyPatchTool` | `tools.apply_patch` | coder (fallback edit) |
| `RunTestsTool` | `tools.run_tests` | coder / tester |

The bundled workflows factor these into `_read_tools()`, `_coder_tools()`,
`_tester_tools()` — copy that pattern.

### Conventions worth reusing

The three bundled workflows share idioms that earned their keep; lift them rather
than reinventing:

- **`SHARED_RULES`** — a tool-discipline + smallest-correct-change block handed to
  every role.
- **`VERDICT_SCHEMA`** with `PASS` / `FAIL` / `BLOCKED` — `BLOCKED` lets a tester
  flag an *environmental* dead end (missing dep, no network) so the loop stops
  instead of burning rounds.
- **Never re-issue an identical task** — carry the tester's `findings` into the
  next coder round (`FINDINGS_BLOCK`), and cap rounds (`MAX_*_ROUNDS`).
- **`goal` / `description` fallback** so the eval harness can run the workflow
  unchanged.
- **Return `tokens_spent: ctx.budget.spent()`** and a `status` of
  `done` / `incomplete` / `error` for uniform downstream handling.

## Running

```bash
opencollab workflow list
opencollab workflow run scout-solve --args '{"goal": "Fix the off-by-one in ..."}' -w /path/to/repo
```

From this repo's venv: `opencollab/.venv/bin/python -m opencollab workflow ...`.

Useful flags on `run`:

| Flag | Default | Meaning |
|------|---------|---------|
| `--args` | `{}` | JSON object of workflow arguments (must be an object) |
| `-w`, `--workspace` | `.` | working directory the agents operate in |
| `--budget` | `max(config, 500000)` | token ceiling (workflows fan out many sessions) |
| `-c`, `--concurrency` | `4` | max concurrent agent sessions |
| `-m` / `-p` / `--api-key` / `--base-url` | from config | LLM overrides |
| `--save` / `--no-save` | `--save` | persist each session transcript under `<workspace>/.opencollab/sessions/<run>/` |

The final result prints as a JSON block on stdout; phase/log lines stream above
it as `==` / `--` markers.

## Architecture boundary

Workflows import from `application` (the `@workflow` decorator and, transitively,
`WorkflowContext`) and from `adapters.tools`. The engine itself
(`application/workflow.py`, `application/workflow_registry.py`) stays in the
application layer — domain + stdlib only — while the runtime wiring (session
factory, discovery, persistence) lives in `bootstrap/workflow_runtime.py`. Keep
new workflows declarative: orchestration logic here, capabilities behind tools.
