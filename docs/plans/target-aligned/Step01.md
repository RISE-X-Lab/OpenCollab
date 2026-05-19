# Step01 - Carve out the `adapters/` package

Date: 2026-05-19
Branch: `refactor/step01-bootstrap` (current) — open a child branch `refactor/step01-adapters` for this step.

## Goal

Create `opencollab/opencollab/adapters/` and relocate the concrete I/O modules
that currently masquerade as `core/` or live next to abstractions in `tools/`.
This step is **pure rehoming**: no behavior changes, no signature changes, no
port redesign. The application layer and bootstrap continue to depend on the
same classes — only the import paths change.

After this step, the target's `Interface Adapters` package in
`docs/repomap/repomap-target.puml` corresponds to a real Python package.

## Current Evidence

Concrete adapters that today live outside the target `adapters/` slot:

| Current path | Role | Target slot |
|---|---|---|
| `opencollab/opencollab/core/llm.py` | `LLMClient` (OpenAI/Anthropic SDK wrapper, implements `LLMPort`) | `adapters/llm.py` |
| `opencollab/opencollab/core/env.py` | `Environment`, `LocalEnvironment`, `DockerEnvironment`, `WorktreeEnvironment` (implement `EnvironmentPort` / `WorktreePoolPort`) | `adapters/env.py` |
| `opencollab/opencollab/core/tracer.py` | `Tracer` (JSONL trace sink, implements `TracePort`) | `adapters/trace.py` |
| `opencollab/opencollab/core/context.py` | `get_repo_map` (filesystem repo scan, behind `RepoMapPort`) | `adapters/repo_map.py` |
| `opencollab/opencollab/core/session/storage.py` | `SessionStore` (JSONL persistence, implements `SessionStorePort`) | `adapters/storage.py` |
| `opencollab/opencollab/tools/safety.py` | `SandboxInterceptor` (structurally implements `SafetyPolicyPort`) | `adapters/safety.py` |

Verified import surface (`rg "from opencollab\.(core\.(llm|env|tracer|context)|core\.session\.storage|tools\.safety)"`):

- Production callers: `bootstrap/container.py`, `bootstrap/runtime.py`, `bootstrap/safety.py`, `bootstrap/session_factory.py`, `core/__init__.py`, `core/session/__init__.py`, `core/session/session.py`, `core/session/compactor.py`, `harness/evaluator.py`, `team/orchestrator.py`, `team/teammate_factory.py`, `team/worktree_pool.py`.
- Tests: `test_bootstrap.py`, `test_session_characterization.py`, `test_session_construction.py`, `test_team_decomposition.py`, `test_tool_call_processor_interceptor.py`, `test_tool_runtime_contract.py`, `test_worktree_pool.py`.

Total: 13 production files, 7 test files. No domain or application file imports any of the six modules above — verified.

## Target Shape For This Step

```text
opencollab/opencollab/adapters/
  __init__.py
  llm.py        # was core/llm.py
  env.py        # was core/env.py
  trace.py      # was core/tracer.py
  repo_map.py   # was core/context.py
  storage.py    # was core/session/storage.py
  safety.py     # was tools/safety.py
```

Dependency direction after this step:

```text
adapters/*  -> application.ports + domain.*  + 3rd-party SDKs
bootstrap/* -> adapters/*  (concrete wiring)
core/       -> shrinks; only the session facade + agent + config + events remain
tools/      -> no longer ships safety; tool adapters keep importing the port
```

What does **not** change in this step:

- File contents (other than internal absolute-import updates needed because they cross-reference each other).
- Class names, public APIs, method signatures.
- `core/session/` (the runner / facade / event bus / autosave / compactor / state stays put — Step 2 territory).
- `core/agent.py`, `core/config.py`, `core/events.py` — these are not concrete adapters; they move in later steps.
- The `interceptor=` keyword in tool runtime contract — that is Clean-Architecture series CA-03.

## Implementation Plan

Execute moves one file at a time, run the full test suite between each, and
commit per file. This makes bisection trivial if a hidden import slips through.

### 1. Add the package

Create `opencollab/opencollab/adapters/__init__.py` (empty docstring + module
preamble). No re-exports — callers update their imports explicitly.

### 2. Move `core/llm.py` → `adapters/llm.py`

```bash
git mv opencollab/opencollab/core/llm.py opencollab/opencollab/adapters/llm.py
```

Rewrite imports. Expected callers (verify with `rg "from opencollab\.core\.llm"`):

- `opencollab/opencollab/bootstrap/container.py`
- `opencollab/opencollab/bootstrap/session_factory.py`
- `opencollab/opencollab/core/__init__.py` (drop the re-export, or change to alias)
- `opencollab/opencollab/core/session/session.py`
- `opencollab/opencollab/core/session/compactor.py`
- `opencollab/opencollab/team/teammate_factory.py`
- `opencollab/opencollab/harness/evaluator.py`
- relevant tests

Run tests.

### 3. Move `core/env.py` → `adapters/env.py`

```bash
git mv opencollab/opencollab/core/env.py opencollab/opencollab/adapters/env.py
```

Rewrite imports (verify with `rg "from opencollab\.core\.env"`). Notable
callers: `bootstrap/container.py`, `bootstrap/runtime.py`, `bootstrap/safety.py`, `team/orchestrator.py`, `team/teammate_factory.py`, `team/worktree_pool.py`, tests under `tests/`.

Run tests.

### 4. Move `core/tracer.py` → `adapters/trace.py`

```bash
git mv opencollab/opencollab/core/tracer.py opencollab/opencollab/adapters/trace.py
```

Note the filename change (`tracer` → `trace`) to match the target diagram's
`adapters.trace`. Class name `Tracer` stays.

Rewrite imports (verify with `rg "from opencollab\.core\.tracer"`).

Run tests.

### 5. Move `core/context.py` → `adapters/repo_map.py`

```bash
git mv opencollab/opencollab/core/context.py opencollab/opencollab/adapters/repo_map.py
```

Filename change (`context` → `repo_map`) to match the target's
`adapters.repo_map`. Function `get_repo_map` keeps its name.

Rewrite imports (verify with `rg "from opencollab\.core\.context"`).

Run tests.

### 6. Move `core/session/storage.py` → `adapters/storage.py`

```bash
git mv opencollab/opencollab/core/session/storage.py opencollab/opencollab/adapters/storage.py
```

Rewrite imports (verify with `rg "from opencollab\.core\.session\.storage"`).
Also update `opencollab/opencollab/core/session/__init__.py` if it re-exports
`SessionStore`.

Run tests.

### 7. Move `tools/safety.py` → `adapters/safety.py`

```bash
git mv opencollab/opencollab/tools/safety.py opencollab/opencollab/adapters/safety.py
```

Rewrite imports (verify with `rg "from opencollab\.tools\.safety"`). Notable
callers: `bootstrap/safety.py`, `team/orchestrator.py`, tool adapters under
`tools/` that reference `SandboxInterceptor` only under `TYPE_CHECKING`, and
tests `test_tool_call_processor_interceptor.py`, `test_tool_runtime_contract.py`.

Run tests.

### 8. Clean up `core/__init__.py`

Remove any re-exports of the six moved symbols. If anything used
`from opencollab.core import LLMClient` etc., update to
`from opencollab.adapters.llm import LLMClient`.

### 9. Final verification

From `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Static checks:

```bash
rg "from opencollab\.core\.llm\b"           opencollab/  # expect 0
rg "from opencollab\.core\.env\b"           opencollab/  # expect 0
rg "from opencollab\.core\.tracer\b"        opencollab/  # expect 0
rg "from opencollab\.core\.context\b"       opencollab/  # expect 0
rg "from opencollab\.core\.session\.storage" opencollab/  # expect 0
rg "from opencollab\.tools\.safety\b"       opencollab/  # expect 0
```

## Acceptance Criteria

- `opencollab/opencollab/adapters/` exists and contains exactly six modules:
  `llm.py`, `env.py`, `trace.py`, `repo_map.py`, `storage.py`, `safety.py`.
- None of the six old paths exist anymore (verified by `git ls-files`).
- The six grep checks above all return zero matches.
- Every production caller and every test imports through the new
  `opencollab.adapters.*` path.
- Full test suite passes with no behavior change.
- No file under `opencollab/opencollab/domain/` or
  `opencollab/opencollab/application/` gained any new import (these layers
  must remain inward-only).

## Non-Goals

- Do **not** rename classes, functions, or method signatures.
- Do **not** introduce new ports or change existing port surfaces.
- Do **not** touch `core/session/` (runner, session facade, autosave, compactor,
  event bus, state, tools) — Step 2.
- Do **not** move `core/agent.py`, `core/config.py`, or `core/events.py` —
  later steps decide whether they become `domain/`, `application/`, or stay.
- Do **not** relocate `team/orchestrator.py` into the application layer —
  Step 3.
- Do **not** add re-export shims at the old paths. A clean atomic rename is
  cheaper to reason about than a deprecation tail.

## Rollback Plan

Each move is a single `git mv` + a small batch of import rewrites + one commit.
If the test suite fails after a step:

1. `git revert` the failing commit (or `git reset --hard HEAD~1` if not yet
   pushed).
2. Investigate which import site was missed (`rg` for the old path globally,
   including `__init__.py` re-exports and `TYPE_CHECKING` blocks).
3. Re-apply the move with the fix.

Because the moves are independent, a failure on one (e.g. `safety.py`) does
not block the others; partial completion is a valid checkpoint.
