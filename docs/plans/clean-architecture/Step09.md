# Step09 - CA-04 Extract Domain Value Objects

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

Step08 has closed the CA-03 tool-runtime boundary enough to move on:

- `opencollab/opencollab/application/tool_dispatch.py`
  - owns runtime-aware dispatch;
  - owns legacy-only fallback expansion;
  - imports only application runtime.
- `opencollab/opencollab/core/session/tools.py`
  - delegates tool execution to `execute_tool_with_runtime(...)`;
  - no longer expands legacy `env/interceptor/confirm_fn` in the processor.
- `opencollab/opencollab/application/tool_runtime.py`
  - `tool_runtime_from_legacy(...)` now prefers `safety_policy`;
  - `interceptor` remains as a compatibility alias.
- Built-in tools call `tool_runtime_from_legacy(..., safety_policy=interceptor, ...)`
  from legacy wrappers.

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_dispatch.py tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
# 48 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
# 36 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 110 passed
```

Boundary checks already clean:

```bash
rg -n "opencollab\\.core|opencollab\\.tools|opencollab\\.bootstrap|opencollab\\.tui" \
  opencollab/opencollab/application/tool_runtime.py \
  opencollab/opencollab/application/tool_dispatch.py
# no matches

rg -n "opencollab\\.core\\.session|opencollab\\.bootstrap|opencollab\\.tools\\.safety|SandboxInterceptor" \
  opencollab/opencollab/tools/base.py \
  opencollab/opencollab/tools/bash.py \
  opencollab/opencollab/tools/fs.py \
  opencollab/opencollab/tools/human.py \
  opencollab/opencollab/tools/mcp.py
# no matches
```

Current review judgment:

- CA-02 is done.
- CA-03 is done enough for built-in tools and processor dispatch.
- The next major architectural gap is CA-04: domain state/value objects are
  still inside `core.session`.

## Remaining Problem

`core.session` still contains pure value/state types mixed with runtime
orchestration:

- `SessionPhase` in `core/session/state.py`
- `SessionState` in `core/session/state.py`
- `ToolProcessingResult` in `core/session/tools.py`
- `CompactResult` in `core/session/compactor.py`
- small tool-call loop-detection result shapes represented as raw dicts

This keeps the "domain" concept hidden inside the session runtime package.

Target Clean Architecture direction:

```text
drivers/frameworks -> adapters -> application/use cases -> domain
```

The domain layer should be import-light and should not depend on:

- SDKs / providers;
- filesystem;
- shell / Docker / git;
- TUI / CLI;
- bootstrap / composition;
- concrete tools.

## Goal

Start CA-04 with a larger but bounded extraction:

1. Create a domain package.
2. Move pure session/tool/compaction value objects into it.
3. Preserve all existing public imports through compatibility re-exports.
4. Keep behavior unchanged.

This step should be larger than the earlier CA-02/CA-03 steps, but still one
architectural boundary:

```text
pure value objects move from core.session into domain
```

## Implementation Plan

1. Create the domain package.

   Add:

   - `opencollab/opencollab/domain/__init__.py`
   - `opencollab/opencollab/domain/session.py`
   - `opencollab/opencollab/domain/tools.py`
   - `opencollab/opencollab/domain/compaction.py`

   Keep these files dependency-light:

   - standard library only where possible;
   - no imports from `core`, `application`, `tools`, `bootstrap`, `cli`, `tui`,
     or `team`.

2. Move `SessionPhase`.

   Move `SessionPhase` from `core/session/state.py` to
   `domain/session.py`.

   Then update `core/session/state.py` to import and re-export it:

   ```python
   from opencollab.domain.session import SessionPhase
   ```

   Keep `from opencollab.core.session import SessionPhase` working through
   `core/session/__init__.py`.

3. Move `SessionState`.

   Move `SessionState` to `domain/session.py` if the dependency check confirms
   it has no outward imports.

   Current shape is pure enough:

   - messages list;
   - token count;
   - step count;
   - done flag;
   - recent tool hashes;
   - phase;
   - mutation methods.

   Then make `core/session/state.py` a compatibility module:

   ```python
   from opencollab.domain.session import SessionPhase, SessionState

   __all__ = ["SessionPhase", "SessionState"]
   ```

   This keeps internal and external imports stable for now.

4. Move `ToolProcessingResult`.

   Move the dataclass from `core/session/tools.py` to
   `domain/tools.py`.

   Be careful with `apply_to(state)`:

   - current method imports/uses `SessionState` behavior and the
     `MAX_CALL_HASH_WINDOW` constant from `core.session.tools`;
   - domain should not import `core.session.tools`.

   Preferred options:

   - move the max-window constant to `domain/tools.py`; or
   - make `apply_to(state, max_window=...)` accept the window explicitly.

   Better for this step:

   ```python
   MAX_CALL_HASH_WINDOW = 200

   @dataclass
   class ToolProcessingResult:
       ...
       def apply_to(self, state: SessionState, max_window: int = MAX_CALL_HASH_WINDOW) -> None:
           ...
   ```

   Then `core/session/tools.py` imports and re-exports
   `ToolProcessingResult` plus the constants needed for compatibility.

5. Move `CompactResult`.

   Move `CompactResult` from `core/session/compactor.py` to
   `domain/compaction.py`.

   Keep `core/session/compactor.py` importing it:

   ```python
   from opencollab.domain.compaction import CompactResult
   ```

   Keep `from opencollab.core.session import CompactResult` working.

6. Optionally introduce typed loop-detection value object.

   Current `ToolProcessingResult.loop_detections` is
   `list[dict[str, Any]]`.

   If the patch stays readable, add:

   ```python
   @dataclass(frozen=True)
   class ToolLoopDetection:
       tool: str
       count: int
   ```

   But do not force this if it causes broad test churn. Compatibility with the
   existing list-of-dicts shape is more important for this step.

7. Update imports.

   Update production imports in:

   - `core/session/session.py`
   - `core/session/runner.py`
   - `core/session/tools.py`
   - `core/session/compactor.py`
   - `core/session/__init__.py`

   Prefer direct domain imports for implementation modules where sensible:

   ```python
   from opencollab.domain.session import SessionPhase, SessionState
   ```

   But preserve compatibility modules so older imports remain valid.

8. Add domain boundary tests.

   Add a focused test file:

   - `opencollab/tests/test_domain_boundaries.py`

   Test:

   ```python
   domain_files = [
       "opencollab/domain/session.py",
       "opencollab/domain/tools.py",
       "opencollab/domain/compaction.py",
   ]
   assert no "opencollab.core" imports
   assert no "opencollab.application" imports
   assert no "opencollab.tools" imports
   assert no "opencollab.bootstrap" imports
   assert no "opencollab.cli" imports
   assert no "opencollab.tui" imports
   ```

   Also add compatibility assertions:

   - `opencollab.core.session.SessionPhase is opencollab.domain.session.SessionPhase`
   - `opencollab.core.session.SessionState is opencollab.domain.session.SessionState`
   - `opencollab.core.session.ToolProcessingResult is opencollab.domain.tools.ToolProcessingResult`
   - `opencollab.core.session.CompactResult is opencollab.domain.compaction.CompactResult`

9. Preserve characterization behavior.

   Existing characterization tests that assert:

   - `session.phase`;
   - `_recent_call_hashes`;
   - `ToolProcessingResult`;
   - `CompactResult`;
   - save/load messages-only behavior;

   must remain unchanged.

10. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_domain_boundaries.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_dispatch.py tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary check from repo root:

   ```bash
   rg -n "opencollab\\.(core|application|tools|bootstrap|cli|tui|team)" \
     opencollab/opencollab/domain
   # no matches
   ```

## Acceptance Criteria

- `opencollab.domain` package exists.
- `SessionPhase` lives in `domain/session.py`.
- `SessionState` lives in `domain/session.py`.
- `ToolProcessingResult` lives in `domain/tools.py`.
- `CompactResult` lives in `domain/compaction.py`.
- Existing `opencollab.core.session` imports still work.
- Domain modules do not import outward layers.
- No CLI/TUI behavior changes.
- No save/load behavior changes.
- Full test suite remains green.

## Non-Goals

- Do not move `Session`, `SessionRunner`, `ContextCompactor`, or
  `ToolCallProcessor` yet.
- Do not split events in this step.
- Do not change `SessionEvent` semantics.
- Do not change storage format.
- Do not remove compatibility re-exports.
- Do not start use-case extraction in this patch.

## Next After This

After domain value objects are extracted, continue CA-04/CA-05 with one of two
paths:

1. Extract tool execution use case from `ToolCallProcessor` into
   `application`.
2. Extract session run-loop use cases from `SessionRunner` into `application`.

Prefer the tool execution use case first because CA-03 already isolated the
tool runtime boundary and the blast radius should be smaller.
