# Step17 - Move `tools/` under `adapters.tools`

Date: 2026-05-20
Branch: `refactor/step17-adapters-tools` off the Step 16 branch.

> Final target-alignment package relocation. Steps 14-16 removed the stale
> top-level `core/`, `team/`, and `cli/` packages. The only remaining target
> mismatch that should be fixed is top-level `tools/`: the concrete tools are
> adapters, and the target map slots them under `adapters.tools`.
>
> Leave `harness/` alone as a package. It is SWE-bench/eval tooling and is not
> part of this final relocation. If needed, retarget only its concrete tool
> imports so eval mode still imports after `opencollab.tools` is deleted.

## Goal

1. Move concrete tool implementations from `opencollab.tools.*` to
   `opencollab.adapters.tools.*`.
2. Retarget bootstrap factories, tests, and any direct concrete-tool importers
   to the new adapter path.
3. Delete the top-level `opencollab/opencollab/tools/` package entirely.
4. Keep the application/domain boundary guards that forbid future
   `opencollab.tools` imports.
5. Leave `opencollab/opencollab/harness/` in place.

End state: `opencollab/tools/` does not exist; concrete built-in tools live in
`opencollab/adapters/tools/`; application/domain still depend only on ports
and domain types, not concrete tools.

## Current Evidence

`tools/` after Step 16:

```text
tools/__init__.py
tools/base.py
tools/bash.py
tools/delegation.py
tools/fs.py
tools/human.py
tools/mcp.py
```

Production importers of `opencollab.tools` from the Step 16 branch:

```text
bootstrap/tool_factory.py
    from opencollab.tools.{base,bash,fs,human}

bootstrap/session_factory.py
    from opencollab.tools.delegation import DelegateTaskTool, DelegateWithReviewTool
    from opencollab.tools.human import AskUserTool

bootstrap/teammate_factory.py
    from opencollab.tools.bash import BashTool
    from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool

harness/evaluator.py
    from opencollab.tools.bash import BashTool
    from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
```

Test importers / path pins:

```text
tests/test_tool_runtime_contract.py
tests/test_tool_call_processor_interceptor.py
tests/test_team_decomposition.py
tests/test_domain_boundaries.py
```

The remaining `opencollab.tools` strings in application/domain boundary tests
and source-guard assertions should stay. They protect the inner layers from
reaching out to concrete tool adapters.

Important boundary fact: moving tools under `adapters.tools` does **not** mean
application/domain can import them. `test_application_boundaries.py` already
forbids `opencollab.adapters`, and `test_domain_boundaries.py` already forbids
both `opencollab.adapters` and `opencollab.tools`.

## Implementation Plan

Single branch, suggested three commits.

### 1. Move the package and fix internal tool imports

Create the adapter package and move files:

```bash
mkdir -p opencollab/opencollab/adapters/tools
git mv opencollab/opencollab/tools/__init__.py opencollab/opencollab/adapters/tools/__init__.py
git mv opencollab/opencollab/tools/base.py opencollab/opencollab/adapters/tools/base.py
git mv opencollab/opencollab/tools/bash.py opencollab/opencollab/adapters/tools/bash.py
git mv opencollab/opencollab/tools/delegation.py opencollab/opencollab/adapters/tools/delegation.py
git mv opencollab/opencollab/tools/fs.py opencollab/opencollab/adapters/tools/fs.py
git mv opencollab/opencollab/tools/human.py opencollab/opencollab/adapters/tools/human.py
git mv opencollab/opencollab/tools/mcp.py opencollab/opencollab/adapters/tools/mcp.py
```

Retarget imports inside the moved package:

- `adapters/tools/__init__.py`
- `adapters/tools/bash.py`
- `adapters/tools/delegation.py`
- `adapters/tools/fs.py`
- `adapters/tools/human.py`
- `adapters/tools/mcp.py`

Use either relative imports (`from .base import Tool`) or the canonical
absolute adapter path (`from opencollab.adapters.tools.base import Tool`), but
leave no `opencollab.tools` import inside the moved package.

Do not leave a compatibility shim at `opencollab.tools`. The point of this
step is to make the target map literally true.

Suggested commit:

```text
refactor(tools): move concrete tools under adapters
```

### 2. Retarget bootstrap and tests

Retarget production imports:

- `bootstrap/tool_factory.py`
  - `opencollab.tools.base` -> `opencollab.adapters.tools.base`
  - `opencollab.tools.bash` -> `opencollab.adapters.tools.bash`
  - `opencollab.tools.fs` -> `opencollab.adapters.tools.fs`
  - `opencollab.tools.human` -> `opencollab.adapters.tools.human`
- `bootstrap/session_factory.py`
  - `opencollab.tools.delegation` -> `opencollab.adapters.tools.delegation`
  - `opencollab.tools.human` -> `opencollab.adapters.tools.human`
- `bootstrap/teammate_factory.py`
  - `opencollab.tools.bash` -> `opencollab.adapters.tools.bash`
  - `opencollab.tools.fs` -> `opencollab.adapters.tools.fs`

Harness rule for this step:

- Do **not** move or redesign `opencollab/harness`.
- If `harness/evaluator.py` imports concrete built-in tools, retarget those
  import lines to `opencollab.adapters.tools.*` so eval mode still imports.
  This is a mechanical dependency update, not a harness architecture step.

Retarget tests:

- `tests/test_tool_runtime_contract.py`
  - imports -> `opencollab.adapters.tools.*`
  - package walk -> `opencollab.adapters.tools`
  - path-list guards -> `opencollab/adapters/tools/*.py`
- `tests/test_tool_call_processor_interceptor.py`
  - `Tool` import -> `opencollab.adapters.tools.base`
  - package walk -> `opencollab.adapters.tools`
- `tests/test_team_decomposition.py`
  - delegation imports -> `opencollab.adapters.tools.delegation`
- `tests/test_domain_boundaries.py`
  - `Tool` import in `test_tool_base_satisfies_tool_spec` ->
    `opencollab.adapters.tools.base`
  - keep `"opencollab.tools"` in the forbidden list.

Do not weaken these existing guard assertions:

```text
tests/test_context_compaction_use_case.py   assert "opencollab.tools" not in source
tests/test_tool_dispatch.py                 assert "opencollab.tools" not in source
tests/test_tool_execution_use_case.py       assert "opencollab.tools" not in source
tests/test_application_boundaries.py        forbids opencollab.tools and opencollab.adapters
tests/test_domain_boundaries.py             forbids opencollab.tools and opencollab.adapters
```

Suggested commit:

```text
test(tools): retarget concrete tool imports to adapters
```

### 3. Delete stale references and verify

Run the strict old-path search:

```bash
rg "from opencollab\.tools|import opencollab\.tools|opencollab\.tools\." \
  opencollab/opencollab opencollab/tests opencollab/pyproject.toml
```

Expected: no matches. Plain string guards like `"opencollab.tools"` may remain
only where they are intentionally checking inner-layer source text.

Run the package checks:

```bash
test ! -d opencollab/opencollab/tools
test -d opencollab/opencollab/adapters/tools
```

Run focused tests first:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_tool_runtime_contract.py \
  tests/test_tool_call_processor_interceptor.py \
  tests/test_team_decomposition.py \
  tests/test_domain_boundaries.py \
  -q
```

Run a harness import smoke check without moving harness:

```bash
OPENAI_API_KEY=fake-test-key uv run python -c "import opencollab.harness.evaluator"
```

Run the full verification:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
git diff --check
```

Expected: full suite remains **162 passed** unless unrelated tests changed
after the Step 16 baseline.

Suggested commit if kept separate:

```text
refactor(tools): remove top-level tools package
```

## Acceptance Criteria

- `opencollab/opencollab/tools/` does not exist.
- `opencollab/opencollab/adapters/tools/` contains:
  - `__init__.py`
  - `base.py`
  - `bash.py`
  - `delegation.py`
  - `fs.py`
  - `human.py`
  - `mcp.py`
- No live `from opencollab.tools` / `import opencollab.tools` /
  `opencollab.tools.*` reference remains in production code, tests, or
  packaging metadata.
- Bootstrap factories import concrete tools from `opencollab.adapters.tools`.
- Delegate tools still satisfy the runtime-native `execute_with_runtime`
  contract.
- `harness/` remains in place; eval imports still work.
- Boundary tests still forbid application/domain imports of concrete adapters.
- Full suite passes.

## Non-Goals

- Do **not** move `harness/`, rename it, split it, or decide a new target-map
  slot for it. Leave it as out-of-scope eval tooling.
- Do **not** change tool schemas, names, descriptions, parameters, or output
  formatting.
- Do **not** change `ToolRuntime`, `ToolPort`, `ToolSpec`, tool execution
  semantics, safety policy semantics, or permission behavior.
- Do **not** change CLI behavior. The eval command may keep lazily importing
  `opencollab.harness.evaluator`.
- Do **not** weaken application/domain boundary tests. `application/` and
  `domain/` still must not import concrete tool adapters.
- Do **not** leave an `opencollab.tools` compatibility shim.

## Rollback Plan

This should be a mechanical package-move series. Reverting it restores the
old `opencollab.tools.*` import path. Any failure after deletion should name a
missed import directly; retarget it to `opencollab.adapters.tools.*` rather
than reintroducing a top-level shim.

## Closing note - target alignment after this step

After Step 17, the target-aligned package set is effectively:

```text
domain/
application/
adapters/
bootstrap/
harness/       # explicitly out-of-scope eval tooling, left alone
```

No further package-relocation step is planned for `harness/` in this arc. If
the project later wants to formalize eval tooling, handle that as a separate
product/tooling decision rather than part of the clean-architecture target
alignment.
