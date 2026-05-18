# Step 06 — Decompose `Team` into orchestrator + teammate factory + worktree pool

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 6 of 7**: split the 399-line `team/orchestrator.py` into three
focused modules — `orchestrator.py` (slimmer `Team` class), `teammate_factory.py`
(teammate Agent + Session construction), and `worktree_pool.py` (lifecycle of
per-teammate `WorktreeEnvironment`s).

---

## Goal

Today `Team` holds **seven** entangled responsibilities:

1. Delegation tool definitions (`DelegateTaskTool`, `DelegateWithReviewTool`).
2. Lead `Agent` + `Session` construction.
3. Per-teammate `Agent` construction.
4. Per-teammate `Session` construction.
5. Token-budget arithmetic (`remaining_budget`, `reserve_for_lead`,
   `teammate_budget`).
6. Worktree lifecycle (create, track, cleanup).
7. Orchestration: `run()`, `delegate()`, `delegate_with_review()`, diff-append.

Reading `delegate()` (lines 234–328) means reading worktree setup, agent build,
budget math, session build, message dispatch, tracer logging, event emission,
diff capture, *and* the control flow — all in one ~95-line function. Worse,
the budget arithmetic is the kind of subtle code that needs its own tests, but
today it can only be exercised by spinning up the whole `Team`.

After this PR:

- **`team/worktree_pool.py`** owns the `WorktreeEnvironment` lifecycle. Single
  responsibility: lend out worktree envs, track them, clean them up. ~40 lines.
- **`team/teammate_factory.py`** owns teammate `Agent` + `Session`
  construction, including the budget arithmetic. ~70 lines.
- **`team/orchestrator.py`** owns the `Team` class (lead session + `run()` +
  `delegate()` + `delegate_with_review()`) and the two delegation tools.
  Slimmer — `delegate()` becomes a ~25-line control-flow function.

No behavior change. The public API (`Team(workspace=..., model=...).run(msg)`
and `team.cleanup()`) is unchanged.

---

## Why these modules live under `team/`, not `bootstrap/`

A reasonable alternative is to put `teammate_factory.py` under
`opencollab/bootstrap/` — since `bootstrap/` already holds
`build_chat_session` and `build_team`, why not `build_teammate_session` too?

We're choosing `team/` for three reasons, in order of weight:

1. **Lifetime.** `bootstrap/` was extracted in step 1 to hold objects whose
   lifetime equals the CLI invocation (`RuntimeContext`, chat `Session`,
   `Team`). Each is built once per `python -m opencollab …` run. A teammate
   `Session` is the opposite: spawned per-delegation, dozens of times during
   a single team run, in response to a Lead tool call. Putting it in
   `bootstrap/` re-mixes the two lifetimes step 1 separated.

2. **Layering direction.** Today no in-repo module under `team/` or
   `harness/` imports from `bootstrap/`. Bootstrap is the consumer of the
   libraries, not a provider to them. Step 5 rejected the same `team →
   bootstrap` edge when we declined to centralize tool lists into
   `build_default_tools()`. Same edge, same answer.

3. **Cohesion.** `WorktreePool` is unambiguously a team-internal concern.
   If `teammate_factory` moves to `bootstrap/` but `worktree_pool` stays in
   `team/`, the two halves of "build one teammate" split across packages —
   they should travel together.

A useful litmus test: *"If I'm reading `cli/main.py` and follow imports to
understand what gets built, where do I land?"* Before: `bootstrap/`. After:
still `bootstrap/`. The Lead session goes there. The teammate session,
which the CLI never sees directly, does not.

---

## Concrete edits

### 1. New file: `opencollab/team/worktree_pool.py`

```python
"""Lifecycle for per-teammate WorktreeEnvironments.

A teammate running in parallel needs its own physical workspace so it cannot
corrupt a sibling's edits. WorktreePool encapsulates: create a worktree env
for a given role, remember it for cleanup, tear them all down at the end.
"""

from __future__ import annotations

import uuid

from opencollab.core.env import Environment, LocalEnvironment, WorktreeEnvironment


class WorktreePool:
    """Lends out worktree-isolated environments and tracks them for cleanup.

    When use_worktrees is False, hands out LocalEnvironment(workspace) instead
    — caller code does not need to branch on the mode.
    """

    def __init__(self, workspace: str, *, use_worktrees: bool):
        self._workspace = workspace
        self._use_worktrees = use_worktrees
        self._envs: list[WorktreeEnvironment] = []

    async def acquire(self, role: str) -> Environment:
        """Create (and remember) an isolated env for a teammate of this role."""
        if not self._use_worktrees:
            return LocalEnvironment(self._workspace)

        branch = f"opencollab-{role}-{uuid.uuid4().hex[:8]}"
        env = WorktreeEnvironment(self._workspace, branch_name=branch)
        await env.setup()
        self._envs.append(env)
        return env

    async def cleanup(self) -> None:
        """Tear down every worktree this pool has handed out."""
        for env in self._envs:
            try:
                await env.cleanup()
            except Exception:
                pass
        self._envs.clear()
```

**Why a class, not free functions:** the pool holds state (the list of envs
for cleanup). A pair of `acquire(workspace, role, use_worktrees)` /
`cleanup(envs)` functions would push that list-management responsibility back
onto `Team`, defeating the point of the split.

**Why no `release()` per env:** today there is no mid-task cleanup — all
worktrees survive until `Team.cleanup()`. If we ever want eager cleanup, add
`release(env)` later. YAGNI for this PR.

### 2. New file: `opencollab/team/teammate_factory.py`

```python
"""Construct a teammate Session given a role, environment, and shared config.

Centralizes the per-teammate Agent + Session wiring and the budget split
(reserve some headroom for the Lead's follow-up turns).
"""

from __future__ import annotations

from dataclasses import dataclass

from opencollab.core.agent import Agent
from opencollab.core.env import Environment
from opencollab.core.session import EventBus, PermissionPolicy, Session
from opencollab.core.tracer import Tracer
from opencollab.team.prompts import get_role_prompt
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool


@dataclass
class TeammateConfig:
    """Shared LLM/runtime config every teammate inherits from the Team."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPolicy | None
    repo_map: str | None


def split_budget(total: int, used: int) -> int:
    """How many tokens this teammate gets, reserving headroom for the Lead.

    Same arithmetic as the previous inline version — extracted so it can be
    unit-tested directly without spinning up a Team.

    - Always leaves the teammate at least 10_000 tokens (a floor for any real
      reasoning).
    - Reserves min(25% of original total, remaining - 10_000) for the Lead.
    """
    remaining = max(10_000, total - used)
    reserve_for_lead = min(
        max(10_000, total // 4),
        max(0, remaining - 10_000),
    )
    return max(10_000, remaining - reserve_for_lead)


def build_teammate_session(
    *,
    role: str,
    env: Environment,
    cfg: TeammateConfig,
    budget: int,
    max_steps: int = 50,
) -> Session:
    """Build the teammate Agent + Session bundle.

    Tools are stateless; ToolCallProcessor derives a worktree-rooted
    SandboxInterceptor from env.workspace (step 5).
    """
    agent = Agent(
        name=role,
        system_prompt=get_role_prompt(role),
        tools=[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()],
        model=cfg.model,
        provider=cfg.provider,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
    )
    return Session(
        agent=agent,
        env=env,
        tracer=cfg.tracer,
        max_budget_tokens=budget,
        max_steps=max_steps,
        event_sink=cfg.event_bus,
        permission_policy=cfg.permission_policy,
        repo_map=cfg.repo_map,
    )
```

**Why a `TeammateConfig` dataclass:** seven shared fields would otherwise
become a seven-arg signature on `build_teammate_session`. The dataclass also
gives us a single point to add new shared knobs (timeouts, retry policy)
without touching every call site.

**Why `split_budget` is a free function, not a method:** it has no state,
takes two ints, returns one. A free function is testable in isolation; a
method on `Team` would require constructing `Team`.

**Tool list duplication:** the same `[BashTool(), FileReadTool(),
FileWriteTool(), GrepTool()]` appears in `harness/evaluator.py` and was
removed from `Team._make_basic_tools`. We're now reintroducing it here.
That's still acceptable: each call site is one line and locally legible.
Centralizing into `bootstrap.build_default_tools()` would force a `team →
bootstrap` import direction which we explicitly rejected in step 5.

### 3. `opencollab/team/orchestrator.py` — slim down

**Imports change:**

```python
# REMOVE
from opencollab.core.env import Environment, LocalEnvironment, WorktreeEnvironment
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.team.prompts import get_role_prompt, LEAD_SYSTEM_PROMPT

# REPLACE WITH
from opencollab.core.env import Environment, LocalEnvironment, WorktreeEnvironment
from opencollab.team.prompts import LEAD_SYSTEM_PROMPT
from opencollab.team.teammate_factory import (
    TeammateConfig,
    build_teammate_session,
    split_budget,
)
from opencollab.team.worktree_pool import WorktreePool
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
```

(`Environment`, `WorktreeEnvironment`, and the basic tools are still needed in
this file — `Environment` and `WorktreeEnvironment` for type annotations and
`isinstance(env, WorktreeEnvironment)`, the basic tools for the Lead's own
toolbox.)

**`Team.__init__` body — replace the worktree-tracking list with a pool:**

```python
# BEFORE
self.use_worktrees = use_worktrees
...
self._teammate_envs: list[WorktreeEnvironment] = []

# AFTER
self._worktree_pool = WorktreePool(workspace, use_worktrees=use_worktrees)
```

`self.use_worktrees` is no longer needed as a public attribute — it lives
inside the pool. Grep for callers; in-repo there are none.

**`Team._make_basic_tools` — leave it.** It still encapsulates the Lead's
tool set; teammate tools live in `teammate_factory.py`. One-line method,
fine. (Alternative: inline it at the single call site in `__init__`. Worker
may inline if they prefer — both fine.)

**`Team.delegate()` — collapse from ~95 lines to ~30:**

```python
async def delegate(self, role: str, task: str, context: str = "") -> str:
    """Spawn an isolated teammate to execute a task. Return its summary."""
    start = time.monotonic()
    await self.event_bus.emit(SessionEvent(
        type="tool_start",
        data={"tool": "delegate", "role": role, "task": task[:100]},
    ))

    env = await self._worktree_pool.acquire(role)
    teammate_cfg = TeammateConfig(
        model=self.model,
        provider=self.provider,
        api_key=self.api_key,
        base_url=self.base_url,
        tracer=self.tracer,
        event_bus=self.event_bus,
        permission_policy=self.permission_policy,
        repo_map=self.repo_map,
    )
    budget = split_budget(self._total_budget, self._used_tokens)
    teammate_session = build_teammate_session(
        role=role, env=env, cfg=teammate_cfg, budget=budget,
    )

    task_message = f"Context:\n{context}\n\nTask:\n{task}" if context else task
    await teammate_session.add_user_message(task_message)
    result = await teammate_session.run_loop()

    self._used_tokens += teammate_session.used_tokens
    latency = time.monotonic() - start
    if self.tracer:
        self.tracer.log_step(
            step_type="delegate",
            payload={"role": role, "task": task[:200], "result_len": len(result)},
            tokens=teammate_session.used_tokens,
            latency=latency,
        )
    await self.event_bus.emit(SessionEvent(
        type="tool_end",
        data={"tool": "delegate", "role": role, "latency": latency},
    ))

    return await self._append_worktree_diff(env, result)
```

**Extract diff-append to a helper:**

```python
async def _append_worktree_diff(self, env: Environment, result: str) -> str:
    """If env is a worktree, append its diff (truncated) to the result."""
    if not isinstance(env, WorktreeEnvironment):
        return result
    diff = await env.get_diff()
    if not diff:
        return result
    if len(diff) > 12_000:
        diff = (
            diff[:6_000]
            + f"\n\n... [{len(diff) - 12_000} chars truncated] ...\n\n"
            + diff[-6_000:]
        )
    return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"
```

**`Team.cleanup()`:**

```python
async def cleanup(self) -> None:
    await self._worktree_pool.cleanup()
```

**`delegate_with_review()`:** unchanged. It composes `delegate()` and is
already a self-contained control-flow function.

**Dead-code removal:** the bottom-of-file helper `_emit_maybe` (lines
395–398) has zero callers in this file or elsewhere. Delete it.

### 4. `opencollab/team/__init__.py` — unchanged

Still exports `Team`. The new modules are internal.

```python
from opencollab.team.orchestrator import Team

__all__ = ["Team"]
```

### 5. `opencollab/bootstrap/session_factory.py` — unchanged

Still imports `Team` from `opencollab.team.orchestrator`. No signature change.

---

## New tests

Add `opencollab/tests/test_team_decomposition.py`:

```python
"""Unit tests for the extracted Team submodules."""

from __future__ import annotations

import pytest

from opencollab.team.teammate_factory import split_budget


# split_budget arithmetic — used to be inline in Team.delegate, hard to test.

def test_split_budget_fresh_team_reserves_quarter_for_lead():
    # Total 400_000, nothing used → lead reserve = max(10_000, 100_000) = 100_000
    # teammate = max(10_000, 400_000 - 100_000) = 300_000
    assert split_budget(total=400_000, used=0) == 300_000


def test_split_budget_with_prior_usage_subtracts():
    # Total 400_000, used 150_000 → remaining 250_000
    # reserve = min(100_000, 240_000) = 100_000
    # teammate = max(10_000, 250_000 - 100_000) = 150_000
    assert split_budget(total=400_000, used=150_000) == 150_000


def test_split_budget_floors_teammate_at_10k():
    # Total 400_000, used 395_000 → remaining max(10_000, 5_000) = 10_000
    # reserve = min(100_000, 0) = 0
    # teammate = max(10_000, 10_000 - 0) = 10_000
    assert split_budget(total=400_000, used=395_000) == 10_000


def test_split_budget_small_total_still_floors_at_10k():
    # Total 30_000, used 0 → remaining 30_000
    # reserve = min(max(10_000, 7_500), max(0, 20_000)) = 10_000
    # teammate = max(10_000, 20_000) = 20_000
    assert split_budget(total=30_000, used=0) == 20_000
```

Add `opencollab/tests/test_worktree_pool.py`:

```python
"""WorktreePool delegates to LocalEnvironment when worktrees are disabled,
and tracks WorktreeEnvironments for cleanup when enabled."""

from __future__ import annotations

import asyncio

import pytest

from opencollab.core.env import LocalEnvironment
from opencollab.team.worktree_pool import WorktreePool


def test_pool_returns_local_env_when_worktrees_disabled(tmp_path):
    pool = WorktreePool(str(tmp_path), use_worktrees=False)

    env1 = asyncio.run(pool.acquire("coder"))
    env2 = asyncio.run(pool.acquire("reviewer"))

    assert isinstance(env1, LocalEnvironment)
    assert isinstance(env2, LocalEnvironment)
    # No tracking — LocalEnvironments don't need cleanup
    assert pool._envs == []

    # cleanup() is a no-op; should not raise
    asyncio.run(pool.cleanup())
```

(A test that actually creates a real `WorktreeEnvironment` requires a git
repo and is heavier than belongs in `tests/`. The behavior of
`WorktreeEnvironment.setup()` and `.cleanup()` is the env's responsibility,
not the pool's; the pool just calls them. We trust those.)

---

## Verification

After the edits, verify with:

```bash
# WorktreeEnvironment is only constructed inside the pool now.
grep -rn "WorktreeEnvironment(" opencollab/opencollab
# Expected: only opencollab/team/worktree_pool.py (and the class definition
# in opencollab/core/env.py).

# split_budget is the only place this arithmetic lives.
grep -rn "_total_budget - .*_used_tokens\|reserve_for_lead" opencollab/opencollab
# Expected: zero matches in orchestrator.py; only inside teammate_factory.py.

# Team.__init__ no longer holds the env list.
grep -n "_teammate_envs" opencollab/opencollab/team/orchestrator.py
# Expected: zero matches.

# Dead helper gone.
grep -n "_emit_maybe" opencollab/opencollab/team/orchestrator.py
# Expected: zero matches.

# Public API still imports cleanly.
python -c "from opencollab.team import Team; print(Team.__name__)"
# Expected: prints "Team".
```

---

## Acceptance checklist

- [ ] `opencollab/team/worktree_pool.py` exists with `WorktreePool` class.
- [ ] `opencollab/team/teammate_factory.py` exists with `TeammateConfig`,
      `split_budget`, and `build_teammate_session`.
- [ ] `WorktreeEnvironment(` is constructed in exactly one place outside
      its definition: `worktree_pool.py`.
- [ ] `Team._teammate_envs` is gone; `Team._worktree_pool` exists instead.
- [ ] `Team.delegate()` is under 40 lines (was ~95).
- [ ] Budget arithmetic lives only in `teammate_factory.split_budget`.
- [ ] `_emit_maybe` helper deleted from `orchestrator.py`.
- [ ] `from opencollab.team import Team` still works (no `__init__.py`
      changes).
- [ ] `build_team(ctx, use_worktrees=..., interactive=...)` in
      `bootstrap/session_factory.py` still works without changes.
- [ ] All existing tests pass (`cd opencollab && OPENAI_API_KEY=fake-test-key
      uv run pytest tests/ -q` → 41 passing + 4 new = 45 passing).
- [ ] `tests/test_team_decomposition.py` and `tests/test_worktree_pool.py`
      added.
- [ ] `python -m opencollab team "<task>"` smoke test still works (Lead
      delegates to coder, gets diff back, cleanup tears down worktree).

---

## What is NOT in this PR

- Splitting `delegation_tools.py` out into its own file. `DelegateTaskTool`
  and `DelegateWithReviewTool` stay in `orchestrator.py` because they're
  thin proxies over `Team.delegate()` / `Team.delegate_with_review()` and
  moving them would just create a circular-import workaround.
- Changing the public `Team(...)` constructor signature.
- Adding worktree reuse / pooling beyond lifecycle tracking.
- Touching the Self-Collaboration verdict-parsing logic in
  `delegate_with_review` — its design is fine as-is.
- `auto_save` event subscriber — step 7.

---

## Risk & rollback

- **Pure refactor.** No behavior change. The arithmetic in `split_budget`
  is preserved exactly (the tests above pin the existing numbers).
- **Worktree behavior identical.** The pool just wraps the same
  `WorktreeEnvironment.setup()` / `.cleanup()` calls in the same order.
- **Subtle risk: argument-order drift in `build_teammate_session`.** The
  function uses keyword-only arguments (`*`) and a `TeammateConfig`
  dataclass to avoid this. Positional calls won't compile.
- **Subtle risk: `WorktreePool` accidentally holding a reference to a long-
  cleaned-up env.** `cleanup()` clears `self._envs` explicitly; same as the
  current code path.
- **Test coverage gap: actual worktree creation is not unit-tested.** It
  wasn't before either; that's covered by `team` smoke-tests and (loosely)
  by the SWE-bench eval harness.
- Rollback: single `git revert`.

Human-readable summary for the PR description: *"Decompose `Team` (399 lines)
into `orchestrator.py` (slimmer `Team`), `teammate_factory.py` (Agent +
Session builder + budget arithmetic), and `worktree_pool.py` (per-teammate
worktree lifecycle). No behavior change; the budget split is now unit-tested
directly."*

---

## Notes for the worker

A few things to verify or weigh as you implement, none of them blockers:

- **Confirm `Team.use_worktrees` has no external callers before deleting.**
  Run `grep -rn "\.use_worktrees\|use_worktrees=" opencollab/opencollab opencollab/tests`.
  Expected: only the `bootstrap.build_team(...)` arg passing it in and the
  `__init__` storing it. If anything else reads `team.use_worktrees` as a
  public attribute, replace those reads with whatever they really need (or
  expose a small read-only property on `Team`).

- **`Team._make_basic_tools` is optional to keep.** The plan leaves it as
  a one-line method for the Lead's toolbox. Inlining it at the single call
  site in `__init__` is equally fine. Don't bikeshed.

- **Tool-list duplication is intentional.** After this PR,
  `[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()]` appears in
  three places: `teammate_factory.build_teammate_session`,
  `team/orchestrator.py:Team._make_basic_tools` (for the Lead),
  and `harness/evaluator.py:run_eval_task`. All three are one-liners.
  Centralizing them would force a `team/harness → bootstrap` import
  direction we explicitly rejected in step 5. Three one-liners > one
  inverted layer. If a future tool gets added, update all three.

- **`WorktreePool` test only covers the `use_worktrees=False` path.** Real
  worktree creation requires a git repo and `WorktreeEnvironment.setup()`
  side-effects that belong in `core/env` tests, not in `team/` tests. The
  pool itself is a thin lifetime tracker; the included test is sufficient
  to catch regressions in the dispatch and cleanup paths.

- **The `split_budget` tests pin the *current* arithmetic exactly.** If a
  future PR wants to tune the reservation strategy (e.g., raise the floor,
  change the 25% reserve), expect those four tests to fail and need
  updating — that's the contract working as designed. Read the test as
  "spec" not "regression net."
