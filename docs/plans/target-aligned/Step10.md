# Step10 - Move teammate session composition to `bootstrap/`, seed `domain/team.py`

Date: 2026-05-20
Branch: `refactor/step10-teammate-factory` off the Step 09 branch.

> Part 2 of the **team promotion arc** (Steps 09-11). This step removes the
> last `core.session` import from the team package and reshapes `Team` so the
> session factory is injected, not self-constructed. Unlike Steps 01-09 this
> is a **behavior-adjacent refactor** (constructor reshaping + bootstrap
> rewiring), not a pure relocation — it is gated by `test_team_decomposition.py`
> and `test_team_event_emission.py`.

## Goal

1. Move the session-composition pieces out of `team/teammate_factory.py`
   into `bootstrap/teammate_factory.py`:
   - `TeammateConfig` (config bundle)
   - `build_teammate_session(...)` (constructs a teammate `Session`)
   - `DefaultSessionFactory` (implements `SessionFactoryPort`, imports
     `core.session.Session`)
2. Move the pure budget-policy function `split_budget` into a new
   `domain/team.py`, seeding the target's `domain.team` slot.
3. Reshape `team/orchestrator.py:Team.__init__` so `session_factory` is a
   **required injected dependency** (no inline `DefaultSessionFactory`
   default). `bootstrap.session_factory.build_team` constructs the config +
   factory and passes it in.

After this step the `team/` package imports neither `core.session` nor
`bootstrap` — it depends only on ports, domain, adapters, and concrete
tools (the tool/env coupling is removed in Step 11).

## Why injection, not import

`test_team_decomposition.py:152` (`test_team_modules_do_not_import_bootstrap_safety`)
locks the rule that **team modules must not import `bootstrap`**. So the
factory cannot move to bootstrap and be imported back by the orchestrator.
The orchestrator already accepts `session_factory: SessionFactoryPort | None`;
this step makes that the only path and deletes the self-construction
default that pulls `DefaultSessionFactory` (and thus `core.session`) into
the team package.

## Current Evidence

`team/teammate_factory.py` structure:

- `TeammateConfig` (dataclass) — line 29
- `split_budget(total, used) -> int` — line 44 (pure arithmetic policy)
- `build_teammate_session(...)` — line 62 (imports + builds `Session`)
- `DefaultSessionFactory` — line 103 (implements `SessionFactoryPort`)

`team/teammate_factory.py` imports `from opencollab.core.session import Session`
(line 22) — the **only** remaining `core.session` reader in `team/`
(`team/orchestrator.py` no longer imports it after Step 07).

`team/orchestrator.py:204-213` constructs the default:
```python
self._session_factory: SessionFactoryPort = (
    session_factory if session_factory is not None
    else DefaultSessionFactory(self._teammate_cfg)
)
```
and builds `TeammateConfig` inline at `:194-204`.

`bootstrap/session_factory.py:76` already has `build_team(...)` — the
natural place to construct and inject the factory.

### Pinned by tests (must update import paths, not behavior)

`test_team_decomposition.py:13`:
```python
from opencollab.team.teammate_factory import TeammateConfig, build_teammate_session, split_budget
```
- `split_budget` arithmetic: lines 38-62 (4 cases) — keep behavior identical.
- `build_teammate_session` safety wiring: lines 65-113 — keep behavior
  identical.

These imports move to their new homes (`bootstrap.teammate_factory` /
`domain.team`).

## Target Shape For This Step

```text
opencollab/opencollab/domain/team.py            # new — split_budget (+ docstring)
opencollab/opencollab/bootstrap/teammate_factory.py  # new — TeammateConfig, build_teammate_session, DefaultSessionFactory
opencollab/opencollab/team/
  __init__.py
  orchestrator.py     # session_factory now required-injected
  prompts.py
  # teammate_factory.py removed
```

Dependency direction after this step:

```text
domain/team.py              -> stdlib only
bootstrap/teammate_factory  -> core.session.Session + adapters + ports (composition; allowed)
bootstrap/session_factory   -> bootstrap.teammate_factory (build_team injects the factory)
team/orchestrator.py        -> ports + domain.team + adapters.env + tools.* (no core.session, no bootstrap)
```

## Implementation Plan

Single branch, suggested three commits.

### 1. Seed `domain/team.py` with `split_budget`

- Create `opencollab/opencollab/domain/team.py` containing `split_budget`
  verbatim, with a module docstring noting it is pure team-budget policy.
- Re-export from `domain/__init__.py`.
- Update `team/orchestrator.py` and `test_team_decomposition.py` to import
  `split_budget` from `opencollab.domain.team`.
- Run tests. Commit: `refactor(domain): seed domain.team with split_budget`.

### 2. Move composition to `bootstrap/teammate_factory.py`

- `git mv opencollab/opencollab/team/teammate_factory.py opencollab/opencollab/bootstrap/teammate_factory.py`.
- Delete the now-moved `split_budget` from it (it lives in `domain.team`
  now); import it from `domain.team` if `build_teammate_session` uses it
  (check — `split_budget` is called by `Team.delegate`, likely not by the
  factory; if unused in the factory, no import needed).
- Update `bootstrap/session_factory.py:build_team` to construct
  `TeammateConfig` + `DefaultSessionFactory` and pass `session_factory=`
  into `Team(...)`.
- Update `test_team_decomposition.py` imports of `TeammateConfig` /
  `build_teammate_session` to `opencollab.bootstrap.teammate_factory`.
- Run tests. Commit: `refactor(bootstrap): hoist teammate session factory`.

### 3. Make `session_factory` required in `Team`

- In `team/orchestrator.py`:
  - Remove the `DefaultSessionFactory` import and the `TeammateConfig`
    inline construction.
  - Change the default-construction branch to require an injected
    `session_factory` (raise a clear `TypeError`/`ValueError` if `None`, or
    make the parameter mandatory). `build_team` always supplies it.
  - The `TeammateConfig` was only built to feed `DefaultSessionFactory`;
    since the factory is now injected pre-configured, drop the inline
    `TeammateConfig` from `Team.__init__`.
- Verify `team/` imports neither `core.session` nor `bootstrap`:
  ```bash
  rg "from opencollab\.(core\.session|bootstrap)" opencollab/opencollab/team/   # expect 0
  ```
- Run tests. Commit: `refactor(team): require injected session factory`.

Final verify:
```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

## Acceptance Criteria

- `domain/team.py` defines `split_budget`; `domain/__init__.py` exports it.
- `bootstrap/teammate_factory.py` contains `TeammateConfig`,
  `build_teammate_session`, `DefaultSessionFactory`.
- `team/teammate_factory.py` no longer exists.
- `team/orchestrator.py` imports neither `opencollab.core.session` nor
  `opencollab.bootstrap.*`.
- `build_team` constructs and injects the session factory.
- `split_budget` behavior is byte-identical (4 arithmetic tests pass).
- `build_teammate_session` safety wiring is unchanged (2 tests pass).
- `test_team_modules_do_not_import_bootstrap_safety` still passes.
- Full test suite passes (164, no count change).

## Non-Goals

- Do **not** move the delegation `Tool` subclasses or `_make_basic_tools`
  out of `Team` yet — Step 11.
- Do **not** relocate `Team` to `application/` yet — Step 11.
- Do **not** add value types to `domain/team.py` beyond `split_budget`
  (the target's `TeamPlan` / `DelegationTask` / `ReviewVerdict` are not yet
  extracted; defer to a step that actually needs them).
- Do **not** reconcile `WorktreePool.cleanup` vs `WorktreePoolPort.release`
  — Step 11.
- Do **not** rename `DefaultSessionFactory` or `TeammateConfig`.

## Rollback Plan

Three commits, each independently revertible in reverse order. The
riskiest is commit 3 (constructor reshape); if a hidden caller relied on
the `DefaultSessionFactory` default, the failure surfaces immediately in
`test_team_decomposition.py` / `test_bootstrap.py`. Revert commit 3 alone
to restore the default while keeping the relocations from commits 1-2.
