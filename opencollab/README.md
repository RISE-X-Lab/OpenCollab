# OpenCollab Package

This directory contains the installable Python package and the `opencollab`
CLI.

## Install

From the repository root with `uv`:

```bash
uv venv opencollab/.venv
uv pip install --python opencollab/.venv/bin/python -e opencollab
```

Or with `pip`:

```bash
python3 -m venv opencollab/.venv
opencollab/.venv/bin/pip install -e opencollab
```

From inside this package directory:

```bash
pip install -e .
```

## Commands

The repository launcher is the preferred entrypoint during local development:

```bash
scripts/start_opencollab.sh
```

It uses `opencollab/.venv`, checks `configs/.env`, and starts the interactive
agent (agent 0, the *lead*). There is no separate "team" subcommand: the same
run becomes a multi-agent team when a `configs/team.yaml` is present.

Direct CLI commands are also available after installation. Activate the venv or
call the installed binary directly:

```bash
opencollab/.venv/bin/opencollab --workspace .                       # interactive agent / team
opencollab/.venv/bin/opencollab workflow run scout-solve --args '{"goal": "..."}'
opencollab/.venv/bin/opencollab eval tasks.jsonl --output eval_results --concurrency 1
```

Useful flags: `--trace` records a trajectory (every LLM call and tool exec);
`--no-worktrees` disables per-child git-worktree isolation; `--yolo`
auto-approves risky commands.

## Headless Eval

The eval command reads JSONL tasks. Each line describes one task:

```json
{"task_id":"example","description":"Fix the bug described here.","repo_path":"/path/to/repo","timeout":600,"max_tokens":1000000}
```

The harness writes `results.jsonl` and trajectory logs under the output
directory.

Use the Docker-based SWE-bench runner in `swebench/` for benchmark
container orchestration.

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
- `opencollab/harness/` — the headless evaluation runner.
- `tests/` — characterization and regression tests, including the import-
  direction guards `test_domain_boundaries.py` and
  `test_application_boundaries.py`.

The dependency direction is enforced, not documented: an inward→outward import
turns the boundary tests red. Every outward capability is a Protocol *port* in
`application/ports.py`; only `bootstrap/` knows concrete types, so swapping an
LLM, environment, or tool library is a new adapter plus one line of wiring. The
`domain`/`application` core imports no LLM and does no I/O, so the suite
(~866 tests) runs in seconds with zero network.

## How it works

Two core abstractions, then five swappable components behind ports.

**A single agent is a *validated state machine*** (`domain/session.py`). Edges
that aren't in the transition table raise instead of silently passing, and every
way a turn can stop early is a *named terminal state* — `DONE`,
`BUDGET_EXCEEDED`, `STEP_LIMIT_EXCEEDED`, `CONTEXT_OVERFLOW`, `CANCELLED`,
`ERROR` — so an unhandled failure can never masquerade as a clean finish.

**A team is modeled on OS process concurrency.** The session table is the
process table, `spawn` is `fork`, budget is reserved *before* the first `await`
(so a batch of children can't overspend the pool), and agents suspend and wake
on a cooperative scheduler. Each spawned child works in its own git worktree (so
siblings can't clobber each other's edits) and its diff is appended to its result
for the parent on completion.

The components you'd most likely swap, each behind a port:

| Component | What it does |
|-----------|--------------|
| **Context manager** | Context is an editable bundle of *sources* (identity / team / task / …), not one string. A deterministic shaping pipeline projects a bounded view at each model call — shed low-priority sources, clear stale tool output, snip old turns — while the persisted transcript stays lossless. |
| **Tool manager** | A name-keyed registry (`name → factory`) with a stateless executor and loop detection. Adding a tool is subclassing `Tool` plus one factory line; the executor is tool-agnostic. |
| **LLM provider** | A façade behind `LLMPort`, one module per provider, so each provider's quirks (e.g. recovering markup-leaked tool calls, estimating missing usage) stay isolated. The OpenAI-compatible path is the default; a native Anthropic path (`provider=anthropic`) also exists. |
| **Workflow engine** | Drop a `@workflow`-tagged `*.py` in `../workflows/` and it's auto-discovered. `ctx.agent / parallel / pipeline` give Python-guaranteed control flow over one-shot agent sessions. |
| **Skill store** | On-demand instruction sets the model loads by name through one generic `use_skill` tool (opt-in per role) — extend an agent's know-how without adding any tools. Drop a `SKILL.md` under `../skills/` and it's auto-catalogued. |

## Status

**Proven:** the core invariants (state-machine edges, budget reservation,
context shaping, import boundaries) pinned by the fast test suite.

**In progress / honest gaps:** the port-level *ablations* this architecture was
built to make cheap (hold the model fixed, swap one component, measure the delta)
have not been run yet; a workflow-vs-team A/B is open; prompt caching isn't
wired; the session FSM doesn't yet persist budget/phase across restarts. See
`../docs/` for the design docs and a candid three-day review.

## Making Changes

1. Locate the module by listing or grepping the layer directories above — the
   layout is small and the names are descriptive.
2. Respect the dependency direction `adapters → application → domain`. Ports
   live in `opencollab/application/ports.py`; only `bootstrap/` knows concrete
   types.
3. Tests live in `tests/`. Run them from this package directory.
4. If your change moves or renames modules, update the affected README in the
   same commit.

An archived module-by-module map and dependency graph (a snapshot, not kept in
sync) lives under `../docs/archive/repomap/`.
