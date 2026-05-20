# Step09 - Relocate `WorktreePool` to `adapters/`

Date: 2026-05-20
Branch: `refactor/step09-worktree-pool` off the Step 08 branch.

> Part 1 of the **team promotion arc** (Steps 09-11), which moves
> `team/orchestrator.py:Team` to `application/team.py`. This step peels off
> the cleanest leaf first; Steps 10-11 handle the harder couplings.

## Goal

Move `opencollab/opencollab/team/worktree_pool.py` to
`opencollab/opencollab/adapters/worktree_pool.py`. `WorktreePool` is an
environment-lifecycle adapter — it hands out `LocalEnvironment` /
`WorktreeEnvironment` instances and tears them down — so it belongs in the
adapters layer next to `adapters/env.py`, not in the team package.

Pure relocation. No method renames, no signature changes, no behavior
changes.

## Current Evidence

`team/worktree_pool.py` (entire import surface):

```python
from opencollab.adapters.env import Environment, LocalEnvironment, WorktreeEnvironment
```

Class surface:

```python
class WorktreePool:
    def __init__(self, workspace: str, *, use_worktrees: bool): ...
    async def acquire(self, role: str) -> Environment: ...
    async def cleanup(self) -> None: ...
```

It imports only `adapters.env` — already a clean leaf with no team-internal
or core dependencies.

Callers (`rg "WorktreePool\b"`):

- `opencollab/opencollab/team/orchestrator.py:42` (import),
  `:213` (constructs `WorktreePool(workspace, use_worktrees=...)`),
  `:267` (`await self._worktree_pool.acquire(role)`),
  `:386` (`await self._worktree_pool.cleanup()`).
- `opencollab/tests/test_worktree_pool.py` (imports + exercises it).

### Note on `WorktreePoolPort`

`application/ports.py:152` declares `WorktreePoolPort` with `acquire` /
`release`. `WorktreePool` exposes `acquire` / `cleanup` — the teardown
method name differs (`cleanup` vs `release`). This step does **not**
reconcile that mismatch; reconciling it changes the call site in
`orchestrator.py:386` and is a behavior-adjacent change better left to the
step that promotes `Team` (Step 11), where the orchestration is being
reshaped anyway.

## Target Shape For This Step

```text
opencollab/opencollab/adapters/
  env.py
  worktree_pool.py     # new — moved from team/worktree_pool.py
  ...
```

Dependency direction after this step:

```text
adapters/worktree_pool.py -> adapters.env (sibling adapter)
team/orchestrator.py      -> adapters.worktree_pool (was team.worktree_pool)
```

## Implementation Plan

Single branch, one commit.

1. Move the file:
   ```bash
   git mv opencollab/opencollab/team/worktree_pool.py \
          opencollab/opencollab/adapters/worktree_pool.py
   ```
2. Rewrite imports:
   - `opencollab/opencollab/team/orchestrator.py:42`:
     `from opencollab.team.worktree_pool import WorktreePool` →
     `from opencollab.adapters.worktree_pool import WorktreePool`.
   - `opencollab/tests/test_worktree_pool.py`: same swap.
3. Check `team/__init__.py` for a `WorktreePool` re-export; update or drop
   if present.

Verify:
```bash
rg "from opencollab\.team\.worktree_pool" opencollab/   # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(adapters): relocate WorktreePool`.

## Acceptance Criteria

- `opencollab/opencollab/adapters/worktree_pool.py` exists;
  `opencollab/opencollab/team/worktree_pool.py` does not.
- `rg "from opencollab\.team\.worktree_pool"` returns zero matches.
- `adapters/worktree_pool.py` imports only `adapters.env`.
- Full test suite passes (164, no count change).

## Non-Goals

- Do **not** rename `WorktreePool.cleanup` to `release` or otherwise make
  it conform to `WorktreePoolPort`. Deferred to Step 11.
- Do **not** touch `Team` construction logic beyond the import line.
- Do **not** move `teammate_factory.py` or `prompts.py` — Steps 10-11.

## Rollback Plan

Single commit; `git revert` if the suite fails. The likely failure is a
missed `team/__init__.py` re-export — `rg "team\.worktree_pool"` locates
it.
