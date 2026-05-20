# Step15 - Delete the `team/` compatibility shims

Date: 2026-05-20
Branch: `refactor/step15-drop-team-shims` off the Step 14 branch.

> First post-`core/` cleanup step. Step 14 made the target repomap's
> "no `core/` package" rule literally true. The next smallest mismatch is
> the top-level `team/` package: after Steps 09-11, real Team orchestration
> lives in `application/team.py`, delegate tools live in `tools/delegation.py`,
> and `team/` is now only two compatibility re-export files.
>
> This step deletes those shims. It does **not** move `tools/`, `cli/`, or the
> SWE-bench `harness/`; those are separate target-fidelity steps.

## Goal

1. Prove no production code imports `opencollab.team` anymore.
2. Remove the only test helper that still imports the legacy package:
   the concrete-tool package walk in `test_tool_call_processor_interceptor.py`.
3. Delete `opencollab/opencollab/team/` entirely.
4. Keep the application/domain boundary guards that forbid future
   `opencollab.team` imports.

End state: `opencollab/team/` does not exist; no production module or test
imports `opencollab.team`; Team orchestration remains available from
`opencollab.application.team.Team`.

## Current Evidence

`team/` after Step 14:

```text
team/__init__.py          # re-exports application.team.Team
team/orchestrator.py      # re-exports application.team.Team
```

Live import search from the Step 14 branch:

```text
opencollab/tests/test_tool_call_processor_interceptor.py:244
    for pkg_name in ("opencollab.tools", "opencollab.team"):

opencollab/opencollab/team/__init__.py
    from opencollab.application.team import Team

opencollab/opencollab/team/orchestrator.py
    from opencollab.application.team import Team
```

The remaining `opencollab.team` strings outside the package are boundary
guards or documentation strings:

```text
opencollab/tests/test_application_boundaries.py
opencollab/tests/test_domain_boundaries.py
opencollab/tests/test_context_compaction_use_case.py
```

Those should stay. They prevent the inner layers from depending on a package
that the target map no longer contains.

Current canonical Team importers already point at the application layer:

```text
bootstrap/session_factory.py        from opencollab.application.team import Team
tests/test_session_characterization.py
tests/test_team_event_emission.py
```

Delegate tool implementations are no longer in `team/`; they live in
`tools/delegation.py`, and the tests already import them from that location.

## Implementation Plan

Single branch, one small commit.

### 1. Retarget the concrete-tool legacy-execute guard

Edit `opencollab/tests/test_tool_call_processor_interceptor.py`:

- In `test_no_concrete_tool_defines_legacy_execute`, change the package walk
  from:

  ```python
  for pkg_name in ("opencollab.tools", "opencollab.team"):
  ```

  to:

  ```python
  for pkg_name in ("opencollab.tools",):
  ```

- Do not add an `opencollab.application.team` walk. `application.team` should
  not contain concrete `Tool` subclasses, and today it does not. The concrete
  delegation tools are covered by the `opencollab.tools` walk.
- Optional rename: keep the test name unchanged. It is already about concrete
  tools, not about the legacy package location.

Run:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
```

Expected: pass with no behavior changes.

### 2. Delete the legacy package

Remove:

```bash
git rm -r opencollab/opencollab/team
```

Do **not** remove `"opencollab.team"` from:

- `opencollab/tests/test_application_boundaries.py`
- `opencollab/tests/test_domain_boundaries.py`
- `opencollab/tests/test_context_compaction_use_case.py`

Those are string guards, not live imports.

### 3. Verify the package is gone

Run:

```bash
test ! -d opencollab/opencollab/team
rg "from opencollab\.team|import opencollab\.team" opencollab/opencollab opencollab/tests
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
git diff --check
```

Expected:

- `opencollab/opencollab/team/` is absent.
- No live import statements target `opencollab.team`.
- Full suite remains **162 passed** unless unrelated tests changed after the
  Step 14 baseline.
- `git diff --check` is clean.

Suggested commit:

```text
refactor(team): delete dissolved team compatibility package
```

## Acceptance Criteria

- `opencollab/opencollab/team/` does not exist.
- No `from opencollab.team` / `import opencollab.team` statement remains in
  `opencollab/opencollab` or `opencollab/tests`.
- `Team` is still imported from `opencollab.application.team` by bootstrap and
  tests.
- Delegate tools are still imported from `opencollab.tools.delegation` and
  still satisfy the runtime-native `execute_with_runtime` contract.
- Boundary tests still forbid application/domain imports of `opencollab.team`.
- Full suite passes.

## Non-Goals

- Do **not** move `tools/` to `adapters.tools` in this step. That move is
  broader: it requires import rewrites across concrete tools, bootstrap,
  harness tests, and boundary-test regex updates.
- Do **not** move `cli/` to `adapters.cli` in this step. It is mechanical, but
  unrelated to the retired Team shim package.
- Do **not** decide the fate of `harness/` here. It is SWE-bench/eval tooling
  and is not represented in the target map.
- Do **not** alter `TeamEvent` names, delegate tool schemas, lead-session
  wiring, worktree-pool behavior, or CLI/TUI behavior.

## Rollback Plan

This should be one commit. Reverting it restores the two re-export shims and
the old package-walk guard. Because no canonical importer should depend on the
shims, any failure after deletion is expected to be a missed legacy import that
can be retargeted directly to `opencollab.application.team`.

## Closing note - remaining target-map gaps

After Step 15, the remaining literal repomap mismatches are:

- `cli/` -> `adapters.cli`
- `tools/` -> `adapters.tools`
- `harness/` decision: relocate as out-of-scope evaluator tooling or leave it
  clearly outside the clean-architecture target

The inner dependency rules remain the important invariant: `domain/` and
`application/` should continue importing no outer packages.
