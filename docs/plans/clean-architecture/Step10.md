# Step10 - CA-05 Extract Tool Execution Use Case

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

Step09 has started CA-04 by extracting pure domain value objects:

- `opencollab/opencollab/domain/session.py`
  - `SessionPhase`
  - `SessionState`
- `opencollab/opencollab/domain/tools.py`
  - `MAX_CALL_HASH_WINDOW`
  - `ToolProcessingResult`
- `opencollab/opencollab/domain/compaction.py`
  - `CompactResult`
- Compatibility modules still work:
  - `opencollab.core.session.state`
  - `opencollab.core.session.tools`
  - `opencollab.core.session.compactor`
  - `opencollab.core.session`

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_domain_boundaries.py tests/test_session_characterization.py -q
# 39 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_dispatch.py tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
# 48 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 113 passed
```

Domain boundary check:

```bash
rg -n "opencollab\\.(core|application|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/domain
# no matches
```

Current review judgment:

- CA-02 is complete.
- CA-03 is complete enough for built-in tools and dispatch.
- CA-04 has begun with value-object extraction.
- The next high-leverage move is CA-05 for tool execution.

## Remaining Problem

`opencollab.core.session.tools.ToolCallProcessor` still mixes multiple
application concerns:

- parsing tool-call arguments;
- hashing and loop detection;
- unknown-tool error creation;
- event emission for `tool_start`, `tool_end`, and `loop_detected`;
- runtime construction;
- dispatch through `application.tool_dispatch`;
- tracing;
- output truncation;
- domain result construction.

This means the session core package still owns a use case that should belong in
the application layer:

```text
execute tool calls for a session turn
```

`SessionRunner` currently depends on `ToolCallProcessor`, so moving this use
case behind an application service is the next clean boundary.

## Goal

Extract the tool-call processing use case into `opencollab.application` while
preserving public compatibility.

After this step:

- application layer owns the tool execution use case;
- `core.session.tools.ToolCallProcessor` remains as a thin compatibility facade
  or wrapper;
- `SessionRunner` can continue to receive a `tool_processor` object with
  `.process(...)`;
- event names and payloads remain unchanged;
- tool schemas and behavior remain unchanged;
- characterization tests remain green.

This is a bigger step than value-object extraction, but still one architectural
boundary:

```text
tool execution use case moves from core.session to application
```

## Implementation Plan

1. Create application tool execution module.

   Add:

   - `opencollab/opencollab/application/tool_execution.py`

   Move or introduce:

   - `ToolExecutionUseCase`
   - tool-call parsing helpers;
   - tool-call hashing;
   - loop detection;
   - output truncation;
   - tracing payload creation;
   - `ToolRuntime` construction or runtime provider injection.

   Keep concrete behavior identical to current `ToolCallProcessor`.

2. Define the application-facing dependencies.

   Start pragmatic. Do not introduce a large hierarchy of ports unless needed.

   `ToolExecutionUseCase` can accept:

   ```python
   agent: Any
   environment: EnvironmentPort | None
   state: SessionState
   event_publisher: EventPublisherPort or current EventBus-compatible object
   tracer: Any | None
   permission_policy: PermissionPort | None
   safety_policy: SafetyPolicyPort | None
   ```

   If adding `EventPublisherPort` is small, add it to
   `application/ports.py`:

   ```python
   class EventPublisherPort(Protocol):
       async def emit(self, event: Any) -> None:
           ...
   ```

   Keep event object type as `Any` for this step to avoid splitting
   `SessionEvent` yet. Event-contract splitting is CA-06.

3. Move constants carefully.

   Current constants:

   - `MAX_SIMILAR_CALLS`
   - `MAX_TOOL_OUTPUT_CHARS`
   - `MAX_CALL_HASH_WINDOW` now lives in domain tools.

   Preferred shape:

   - keep `MAX_SIMILAR_CALLS` and `MAX_TOOL_OUTPUT_CHARS` exported from
     `core.session.tools` for compatibility;
   - define canonical values in `application.tool_execution` or
     `domain.tools` depending on whether they are policy or pure value.

   Recommended for this step:

   - put `MAX_SIMILAR_CALLS` and `MAX_TOOL_OUTPUT_CHARS` in
     `application.tool_execution`;
   - re-export them from `core.session.tools`.

4. Preserve `SessionEvent` for now.

   `ToolExecutionUseCase` will still emit the same `SessionEvent` instances:

   - `loop_detected`
   - `tool_start`
   - `tool_end`

   This technically means application imports `core.session.events` unless we
   add a thin event factory/port.

   Preferred compromise for this step:

   - add an application-level `ToolExecutionEvents` callback/factory object, or
   - pass event factory callables from `ToolCallProcessor`.

   Simpler safe shape:

   ```python
   @dataclass(frozen=True)
   class ToolExecutionEventFactory:
       loop_detected: Callable[[str, int], Any]
       tool_start: Callable[[str, dict], Any]
       tool_end: Callable[[str, float], Any]
   ```

   Then `core.session.tools.ToolCallProcessor` supplies factories that create
   `SessionEvent`.

   This keeps:

   ```text
   application.tool_execution -> no core.session.events import
   core.session.tools -> application.tool_execution + SessionEvent
   ```

5. Keep `ToolCallProcessor` as compatibility facade.

   In `opencollab/opencollab/core/session/tools.py`:

   - keep public constructor signature;
   - keep `interceptor` alias;
   - build a `ToolExecutionUseCase` internally;
   - make `process(tool_calls)` delegate to the use case.

   The compatibility facade should still expose:

   - `.interceptor`
   - `.safety_policy`
   - `.permission_policy`
   - `_tool_runtime()` if tests still use it
   - `_execute_tool()` if tests still use it

   But mark private helpers as compatibility/test helpers where possible.

6. Update tests in the right order.

   Before moving code, add/adjust tests to make the boundary explicit:

   - `tests/test_tool_execution_use_case.py`
     - invalid JSON arguments;
     - unknown tool error;
     - loop detection;
     - runtime-native tool execution;
     - legacy fallback still works through `execute_tool_with_runtime`;
     - event factory emits the same event types/data;
     - trace payload still capped.

   Then update existing tests:

   - `test_tool_call_processor_interceptor.py`
     - compatibility facade still delegates;
     - direct private helper expectations updated only if necessary.

   Keep `test_session_characterization.py` unchanged if possible.

7. Add application boundary tests.

   Extend or add:

   - `tests/test_application_boundaries.py`

   Required checks:

   ```bash
   rg -n "opencollab\\.core\\.session|opencollab\\.tools|opencollab\\.bootstrap|opencollab\\.tui" \
     opencollab/opencollab/application/tool_execution.py
   # no matches
   ```

   Allowed imports:

   - `opencollab.application.*`
   - `opencollab.domain.*`
   - standard library

   If `EventBus` shape requires `Any`, keep it structural.

8. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_execution_use_case.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_domain_boundaries.py tests/test_tool_dispatch.py tests/test_tool_runtime_contract.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary checks from repo root:

   ```bash
   rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
     opencollab/opencollab/domain

   rg -n "opencollab\\.core\\.session|opencollab\\.tools|opencollab\\.bootstrap|opencollab\\.tui" \
     opencollab/opencollab/application/tool_execution.py \
     opencollab/opencollab/application/tool_dispatch.py \
     opencollab/opencollab/application/tool_runtime.py
   ```

## Acceptance Criteria

- `ToolExecutionUseCase` exists in `opencollab.application`.
- `core.session.tools.ToolCallProcessor` is a thin compatibility facade.
- `SessionRunner` behavior is unchanged.
- Event names and payloads are unchanged.
- Tool loop detection behavior is unchanged.
- Tool output truncation behavior is unchanged.
- Tool trace payload behavior is unchanged.
- Application tool execution module does not import `core.session`, concrete
  tools, bootstrap, TUI, CLI, or team modules.
- Existing public imports from `opencollab.core.session` still work.
- Full test suite remains green.

## Non-Goals

- Do not split `SessionEvent` yet.
- Do not move `SessionRunner` yet.
- Do not move `ContextCompactor` yet.
- Do not remove `ToolCallProcessor` yet.
- Do not remove legacy `interceptor` compatibility yet.
- Do not change CLI/TUI behavior.
- Do not change tool schemas.

## Next After This

After tool execution is an application use case, the next bigger step should be
one of:

1. Move context compaction into an application use case.
2. Start splitting event contracts so team orchestration stops overloading
   `SessionEvent`.
3. Make `Session` construction thinner by injecting/factoring runtime
   collaborators from bootstrap.

Recommended order:

1. Context compaction use case.
2. Event contract split.
3. Session construction cleanup.
