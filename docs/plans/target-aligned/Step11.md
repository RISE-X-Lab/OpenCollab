# Step11 - Promote `Team` orchestration to `application/team.py`

Date: 2026-05-20
Branch: `refactor/step11-app-team` off the Step 10 branch.

> Part 3 (final) of the **team promotion arc** (Steps 09-11). After Steps 09
> (WorktreePool → adapters) and 10 (factory → bootstrap, split_budget →
> domain), the only things still pinning `Team` to outer layers are its
> inline construction of concrete tools and the lead environment, plus the
> delegation `Tool` subclasses. This step removes those couplings and moves
> the port-only orchestration into the application layer, filling the
> target's `app.team` slot (`RunTeam` / `DelegateTask` / `ReviewLoop`).
>
> This is the most invasive step in the arc (constructor reshaping + tool
> relocation + module move). It is gated by `test_team_decomposition.py`,
> `test_team_event_emission.py`, and `test_bootstrap.py`.

## Goal

1. Extract the delegation tools (`DelegateTaskTool`,
   `DelegateWithReviewTool`) — which subclass `tools.base.Tool` and call
   back into `Team` — into `tools/delegation.py`. They are adapters
   (concrete tools), so they stay in the tools layer; bootstrap wires them
   to the `Team` instance.
2. Lift concrete construction out of `Team.__init__`:
   - the lead's basic tools (`_make_basic_tools` builds `BashTool`,
     `FileReadTool`, `FileWriteTool`, `GrepTool`) → built by bootstrap and
     injected.
   - the lead `Environment` (`LocalEnvironment(workspace)` default) →
     injected.
3. Reconcile `WorktreePool` against `WorktreePoolPort` (rename `cleanup`
   → `release`, or have `Team` depend on the port surface) so `Team`
   depends on the abstract `WorktreePoolPort`, not the concrete
   `adapters.worktree_pool.WorktreePool`.
4. Move the now-port-only orchestration to `application/team.py` and the
   prompt constants to `application/team_prompts.py` (or `domain/team.py`
   if they are pure policy text).

End state: `application/team.py` imports only `domain.*`,
`application.ports`, `application.event_bus`, `application.tool_runtime`.
Concrete tools, envs, sessions, and worktree pools arrive by injection
from `bootstrap`.

## Current Evidence

`team/orchestrator.py` non-conforming dependencies (post-Step 10):

```python
from opencollab.adapters.env import Environment, LocalEnvironment, WorktreeEnvironment  # concrete
from opencollab.adapters.worktree_pool import WorktreePool                              # concrete
from opencollab.tools.base import Tool                                                  # adapter base
from opencollab.tools.bash import BashTool                                              # concrete
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool                   # concrete
from opencollab.team.prompts import LEAD_SYSTEM_PROMPT
```

Coupling sites:

- `DelegateTaskTool` / `DelegateWithReviewTool` (lines 49-138): subclass
  `Tool`, hold `self._team`, call `team.delegate(...)` /
  `team.delegate_with_review(...)`. Circular with `Team`.
- `Team._make_basic_tools()` (line 239): constructs concrete tools.
- `Team.__init__` (line 211): `WorktreePool(workspace, use_worktrees=...)`.
- `Team.__init__` (line ~218): `lead_runtime_env = lead_env or LocalEnvironment(workspace)`.
- `Team.delegate` (line 267): `await self._worktree_pool.acquire(role)`.
- `Team.cleanup` (line 386): `await self._worktree_pool.cleanup()`  ← the
  port calls this `release`; reconcile here.

`bootstrap/session_factory.py:build_team` is the composition site that will
construct tools/env/pool/factory and inject them.

### The delegation-tool circularity

`Team` builds `DelegateTaskTool(self)`, and that tool calls
`self._team.delegate(...)`. Once `Team` is in `application/` and the tool
is in `tools/`, the wiring must invert: bootstrap builds the `Team`, then
builds the delegation tools bound to it, then adds them to the lead agent.
The tools depend on a small port (e.g. `TeamDelegationPort` with
`delegate` / `delegate_with_review`) that `Team` satisfies — avoiding a
`tools → application` import. Define that port in `application/ports.py`.

## Target Shape For This Step

```text
opencollab/opencollab/application/team.py          # Team (RunTeam/DelegateTask/ReviewLoop)
opencollab/opencollab/application/ports.py         # + TeamDelegationPort
opencollab/opencollab/tools/delegation.py          # DelegateTaskTool, DelegateWithReviewTool
opencollab/opencollab/domain/team.py               # split_budget (+ maybe prompt text)
opencollab/opencollab/team/                         # package removed entirely
```

`Team.__init__` final signature (sketch): receives `lead_session`
(or an agent + injected tools + env), `worktree_pool: WorktreePoolPort`,
`session_factory: SessionFactoryPort`, `event_sink`, `permission_policy`,
budget params. No concrete construction inside.

Dependency direction after this step:

```text
application/team.py     -> domain.*, application.ports, application.event_bus, application.tool_runtime
tools/delegation.py     -> tools.base, application.ports (TeamDelegationPort), application.tool_runtime
bootstrap/session_factory -> application.team + tools.* + adapters.* (wires everything)
```

`opencollab/opencollab/team/` is deleted; `team/__init__.py`'s
`from opencollab.team.orchestrator import Team` export is replaced by
`from opencollab.application.team import Team` at call sites (or a thin
re-export if external callers depend on `opencollab.team.Team`).

## Implementation Plan

Single branch, suggested four commits. Run the suite after each.

### 1. Introduce `TeamDelegationPort` and extract delegation tools

- Add `TeamDelegationPort(Protocol)` to `application/ports.py` with
  `delegate(role, task, context) -> str` and
  `delegate_with_review(...) -> str` matching `Team`'s methods.
- Create `tools/delegation.py` with `DelegateTaskTool` /
  `DelegateWithReviewTool`, typed against `TeamDelegationPort` instead of
  the concrete `Team`.
- Commit: `refactor(tools): extract delegation tools`.

### 2. Lift tool + env construction into bootstrap

- Move `_make_basic_tools` logic into `bootstrap` (a helper next to
  `build_team`), or reuse `bootstrap/tool_factory.py`.
- Change `Team.__init__` to accept injected `lead_env` and lead tools;
  remove inline `BashTool`/`FileReadTool`/`LocalEnvironment` construction.
- `build_team` constructs basic tools, the lead env, the worktree pool, the
  session factory, then the `Team`, then the delegation tools bound to the
  team, then assembles the lead agent.
- Commit: `refactor(team): inject lead tools and environment`.

### 3. Depend on `WorktreePoolPort`, reconcile teardown

- Rename `WorktreePool.cleanup` → `release` (or add `release` and have
  `Team.cleanup` call it) so `WorktreePool` conforms to
  `WorktreePoolPort`. Update `adapters/worktree_pool.py` and
  `test_worktree_pool.py`.
- Type `Team`'s pool dependency as `WorktreePoolPort`.
- Commit: `refactor(adapters): conform WorktreePool to WorktreePoolPort`.

### 4. Relocate `Team` to the application layer

- `git mv opencollab/opencollab/team/orchestrator.py opencollab/opencollab/application/team.py`.
- Move `team/prompts.py` → `application/team_prompts.py` (or fold pure
  prompt text into `domain/team.py`).
- Update `team/__init__.py` consumers; delete the `team/` package.
- Update `bootstrap/session_factory.py`, `cli/main.py`, and tests
  (`test_team_decomposition.py`, `test_team_event_emission.py`,
  `test_bootstrap.py`) to import `Team` from `opencollab.application.team`.
- Verify boundary: `application/team.py` imports no `tools.*`,
  `adapters.*`, `core.*`, or `bootstrap.*`.
  ```bash
  rg "^\s*(from|import)\s+opencollab\.(tools|adapters|core|bootstrap|cli|team)\b" \
     opencollab/opencollab/application/team.py   # expect 0
  ```
- Commit: `refactor(application): promote Team orchestration`.

Final verify:
```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

## Acceptance Criteria

- `application/team.py` exists and imports only `domain.*` +
  `application.*`. The boundary `rg` above returns zero matches.
- `tools/delegation.py` holds the delegation tools, typed against
  `TeamDelegationPort`.
- `WorktreePool` conforms to `WorktreePoolPort` (`acquire` / `release`);
  `application/team.py` references the port type.
- The `team/` package no longer exists (or is a thin re-export shim if an
  external caller requires `opencollab.team.Team`).
- `build_team` is the sole composition site constructing tools, env, pool,
  factory, and delegation-tool wiring.
- `test_application_boundaries.py` passes with `application/team.py`
  present (it is scanned by the existing `_offenders("application")`).
- Team event emission and delegation behavior unchanged
  (`test_team_event_emission.py`, `test_team_decomposition.py` pass).
- Full test suite passes.

## Non-Goals

- Do **not** rename `Team` to `RunTeam` or split it into separate
  `RunTeam` / `DelegateTask` / `ReviewLoop` use-case classes. The target
  lists those as concepts; one cohesive `Team` use case is acceptable for
  this step. A later step may split it if the methods grow independently.
- Do **not** extract `TeamPlan` / `DelegationTask` / `ReviewVerdict` value
  types into `domain/team.py` unless the relocation actually needs them.
- Do **not** change delegation prompts, review-loop iteration count, or
  budget split behavior.
- Do **not** touch `core/session/` dissolution — that is the next arc.

## Rollback Plan

Four commits, revertible in reverse order. Commits 1-3 are independent
prep that leave the code working with `Team` still in `team/`; only commit
4 performs the relocation. If commit 4 fails on a missed import,
`rg "opencollab\.team\b"` locates it; if the failure is behavioral, revert
commit 4 alone — the prep commits are safe to keep and retry the move.

## Sequencing note

This closes the team arc. The remaining target gap after Step 11 is the
**`core/session/` dissolution** (the `SessionRunner` / `ContextCompactor`
/ `ToolCallProcessor` wrappers and the `Session` facade), which is gated by
~30 characterization assertions on `Session`'s public attributes and
warrants its own multi-step arc (Steps 12+).
