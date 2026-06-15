# OpenCollab

OpenCollab is a minimal multi-agent software development framework with chat,
team, and headless evaluation modes.

OpenCollab follows a strict clean architecture. For the layer map and
contribution guidance, see `CLAUDE.md` and `opencollab/README.md`.

## Repository Guide

| Path | What it is | Read next |
|------|------------|-----------|
| `opencollab/` | The installable Python package and `opencollab` CLI. | `opencollab/README.md` |
| `configs/` | Runtime configuration templates and config loading notes. | `configs/README.md` |
| `scripts/` | Repository-level launcher and benchmark helper scripts. | `scripts/README.md` |
| `swebench/` | Docker-based SWE-bench runner. | `swebench/README.md` |
| `docs/archive/` | Historical record: completed refactor plans, architecture surveys, and the old code map. | files within |

Untracked local state (gitignored, safe to ignore when reading the code):
`evals/` (prediction outputs), `logs/`, `swe_workdir/`.

## Start Here

1. Create local runtime configuration from `configs/.env.example`.
2. Start OpenCollab from the repository root with `scripts/start_opencollab.sh`.
3. Use the nested READMEs above for setup details, CLI modes, evaluation, and
   benchmark tooling.

Do not commit real API keys or local runtime state.

## Context layering

An agent's context is not one concatenated string but an editable bundle of
*sources*. Each `ContextSource`
(`opencollab/opencollab/domain/context.py:55`) is tagged with three axes: which
`ContextLayer` it belongs to, when it loads (`LoadTiming`), and where it lands
structurally (`ContextPosition` — the system prompt vs. a user-context message).

The layers (`domain/context.py:27`) are:

| Layer | Holds |
|-------|-------|
| `IDENTITY` | Who the agent is — its role prompt. |
| `TEAM` | The topology-aware "your team" section (who this role may spawn or message). |
| `PROJECT` | Repo/project conventions (reserved; registered, loaded later). |
| `MEMORY` | Recalled cross-session memory (reserved; registered, loaded later). |
| `TASK` | The concrete assignment — the rendered `DelegationTask` / first turn. |
| `TOOL_META` | Tool schemas / usage notes. |

`ContextBuilder.build_plan` (`opencollab/opencollab/bootstrap/context_builder.py:70`)
is the editorial step: it emits an ordered `ContextPlan` of these sources. The
plan's assembly is generic over `ContextPosition` and never special-cases a
layer, so adding a new kind of context is just registering a new source.
`ContextPlan.system_prompt()` (`domain/context.py:94`) folds every
`STARTUP`+`SYSTEM` source — identity and team — into the single system message,
which `build_agent` hands to `Agent.system_prompt`. The task, project, and
memory layers carry `USER_CONTEXT` position and are seeded as their own user
messages instead.

`TOOL_META` is registered but deferred: tool schemas already reach the model via
function-calling, so that layer is *not* injected as prose
(`context_builder.py:137-147`). Likewise the reserved `PROJECT`/`MEMORY` sources
are registered now with a `loader_key` for a future lazy-loading pass but
contribute no content at startup.

The layers are **load-bearing under pressure**, not just labels. Each layer has
a keep/shed priority (`LAYER_PRIORITY` in `domain/context.py`): identity/team/task
rank high, project/memory low. When a `USER_CONTEXT` source is seeded it is
stamped with an internal `_ctx` tag (layer + resolved priority) that rides along
on the message — the same convention as `tool_call_id`, so providers ignore it.
Sources at or above `PIN_FLOOR` (`application/shaping/pipeline.py`) are **pinned**:
the compaction chain will never clear, snip, or summarize them, so an agent's own
task can no longer be folded into a summary.

At runtime, history is trimmed by a reactive compaction chain
(`opencollab/opencollab/application/shaping/reactive.py`): it no-ops until the
estimated context crosses a trigger, then degrades progressively — shed the
lowest-priority context sources first (`LowPriorityContextShedShaper`; dormant
until project/memory carry content), then clear old tool output, snip whole old
tool-exchange turns, and (default-off) auto-compact the remaining non-pinned span
to a model-generated summary. Every step is a read-time projection over a copy,
leaving the persisted transcript intact.
