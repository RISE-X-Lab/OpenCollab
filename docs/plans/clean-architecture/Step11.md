# Step11 - CA-05 Extract Context Compaction Use Case

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

Step10 has extracted tool execution into the application layer:

- `opencollab/opencollab/application/tool_execution.py`
  - owns `ToolExecutionUseCase`;
  - owns tool-call parsing, hashing, loop detection, output truncation, trace
    payload construction, runtime construction, and dispatch;
  - imports only application/domain/standard-library modules.
- `opencollab/opencollab/core/session/tools.py`
  - keeps `ToolCallProcessor` as a compatibility facade;
  - delegates `.process(...)` to `ToolExecutionUseCase`;
  - still creates `SessionEvent` through event factories.
- `opencollab/opencollab/application/ports.py`
  - includes `EventPublisherPort`.
- `opencollab/tests/test_tool_execution_use_case.py`
  - covers invalid JSON, unknown tools, loop detection, runtime-native tools,
    legacy fallback, trace capping, and output truncation.

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_execution_use_case.py tests/test_tool_call_processor_interceptor.py -q
# 22 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py tests/test_domain_boundaries.py -q
# 39 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_dispatch.py tests/test_tool_runtime_contract.py -q
# 35 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 122 passed
```

Boundary checks:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/domain
# no matches

rg -n "opencollab\\.core\\.session|opencollab\\.tools|opencollab\\.bootstrap|opencollab\\.tui" \
  opencollab/opencollab/application/tool_execution.py \
  opencollab/opencollab/application/tool_dispatch.py \
  opencollab/opencollab/application/tool_runtime.py
# no matches
```

Current review judgment:

- CA-02 is complete.
- CA-03 is complete enough.
- CA-04 has extracted the first domain value objects.
- CA-05 has started with tool execution.
- The next high-value CA-05 extraction is context compaction.

## Remaining Problem

`opencollab.core.session.compactor.ContextCompactor` still owns the compaction
use case inside the session runtime package:

- context-overflow event emission;
- message splitting;
- compaction prompt construction;
- LLM summary call;
- fallback summary behavior when LLM fails;
- compacted message reconstruction;
- trace emission;
- optional state application;
- `compaction_applied` event emission.

This should become an application use case:

```text
compact session context
```

The current `ContextCompactor` also imports concrete-ish core modules:

- `opencollab.core.llm.estimate_messages_tokens`
- `opencollab.core.session.events.EventBus`
- `opencollab.core.session.events.SessionEvent`

The target for this step is not to split events yet, but to move compaction
logic behind application-level ports/factories the same way Step10 handled tool
execution events.

## Goal

Extract context compaction into `opencollab.application` while preserving public
compatibility.

After this step:

- application layer owns the compaction use case;
- `core.session.compactor.ContextCompactor` remains as a thin compatibility
  facade;
- event names and payloads remain unchanged:
  - `compaction`
  - `compaction_applied`
- direct `session.compactor.compact(apply=True)` still autosaves compacted
  messages;
- `SessionRunner._run_compaction()` behavior is unchanged;
- CLI/TUI behavior is unchanged.

This is one architectural boundary:

```text
context compaction use case moves from core.session to application
```

## Implementation Plan

1. Add application compaction module.

   Add:

   - `opencollab/opencollab/application/compaction.py`

   Move or introduce:

   - `ContextCompactionUseCase`
   - `DEFAULT_COMPACTION_THRESHOLD`
   - `COMPACTION_KEEP_RECENT`
   - message splitting;
   - compaction prompt construction;
   - summary LLM call;
   - fallback raw-text behavior;
   - compacted message reconstruction.

   Keep behavior identical to current `ContextCompactor`.

2. Add small application ports/factories.

   Avoid importing `core.session.events` from application.

   Add an event factory similar to Step10:

   ```python
   @dataclass(frozen=True)
   class CompactionEventFactory:
       compaction: Callable[[], Any]
       compaction_applied: Callable[[int], Any]
   ```

   `ContextCompactor` will provide factories that construct `SessionEvent`.

   Use existing `EventPublisherPort` for the event bus-compatible object.

3. Handle token estimation without importing core.

   `ContextCompactionUseCase.should_compact()` needs token estimation.

   Options:

   - inject `estimate_tokens: Callable[[list[dict]], int]`;
   - or add a small `TokenEstimatorPort` to `application/ports.py`.

   Preferred for this patch:

   ```python
   estimate_tokens: Callable[[list[dict[str, Any]]], int]
   ```

   `ContextCompactor` passes `opencollab.core.llm.estimate_messages_tokens`.

   This keeps:

   ```text
   application.compaction -> no core.llm import
   core.session.compactor -> application.compaction + core.llm
   ```

4. Keep LLM dependency structural.

   `ContextCompactionUseCase` can accept `llm: Any` for now and call:

   ```python
   await llm.complete(summary_request, temperature=0.0)
   ```

   Do not introduce `LLMPort` yet unless the patch stays simple.

   `LLMPort` can be part of the later session-runner/use-case extraction.

5. Keep tracer structural.

   Keep `tracer: Any | None` and call `tracer.log_step(...)` as today.

   Do not move tracing adapters in this patch.

6. Preserve `ContextCompactor` facade.

   In `opencollab/opencollab/core/session/compactor.py`:

   - keep public constructor signature;
   - construct a `ContextCompactionUseCase`;
   - delegate:

     ```python
     def should_compact(self) -> bool:
         return self._use_case.should_compact()

     async def compact(self, apply: bool = True) -> CompactResult:
         return await self._use_case.compact(apply=apply)
     ```

   Keep compatibility attributes that existing tests rely on:

   - `state`
   - `llm`
   - `event_bus`
   - `tracer`
   - `compaction_threshold`

7. Preserve private helper compatibility if useful.

   Existing tests currently call public behavior, not private compactor helpers.
   If no internal call sites depend on them, private helper methods can move
   fully into the use case.

   If needed, keep thin forwarding methods temporarily:

   - `_split_messages_for_compaction`
   - `_build_compaction_prompt`
   - `_call_compaction_llm`
   - `_build_compacted_messages`

   Prefer removing these from the facade if tests stay green.

8. Add focused application tests.

   Add:

   - `opencollab/tests/test_context_compaction_use_case.py`

   Cover:

   - `should_compact()` uses injected token estimator;
   - insufficient messages emits only `compaction` and returns empty
     `CompactResult`;
   - successful compaction builds the exact current summary message;
   - LLM failure falls back to raw older text;
   - `compact(apply=False)` returns result without mutating state;
   - `compact(apply=True)` mutates state and emits `compaction_applied`;
   - tracer payload remains unchanged.

   Keep `test_session_characterization.py` unchanged if possible.

9. Add application boundary tests.

   Extend or add:

   - `opencollab/tests/test_application_boundaries.py`

   Required check:

   ```bash
   rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
     opencollab/opencollab/application/compaction.py
   # no matches
   ```

   Allowed imports:

   - `opencollab.application.*`
   - `opencollab.domain.*`
   - standard library

10. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_context_compaction_use_case.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py tests/test_autosave_subscriber.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_execution_use_case.py tests/test_tool_call_processor_interceptor.py -q
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
     opencollab/opencollab/application/tool_runtime.py \
     opencollab/opencollab/application/compaction.py
   ```

## Acceptance Criteria

- `ContextCompactionUseCase` exists in `opencollab.application`.
- `ContextCompactor` is a thin compatibility facade.
- `CompactResult` remains a domain value object.
- `compaction` event behavior is unchanged.
- `compaction_applied` event behavior is unchanged.
- Direct `compact(apply=True)` still autosaves through the event bus.
- `SessionRunner._run_compaction()` behavior is unchanged.
- Application compaction module does not import core/session, tools, bootstrap,
  CLI, TUI, or team modules.
- Existing public imports from `opencollab.core.session` still work.
- Full test suite remains green.

## Non-Goals

- Do not split `SessionEvent` yet.
- Do not move `SessionRunner` yet.
- Do not move LLM client construction.
- Do not introduce full `LLMPort` yet unless needed trivially.
- Do not change compaction prompt text.
- Do not change fallback summary behavior.
- Do not change storage/autosave semantics.
- Do not change CLI/TUI behavior.

## Next After This

After compaction is an application use case, the next bigger step should be
event-contract cleanup:

1. introduce application/domain event value contracts;
2. stop team orchestration from overloading `SessionEvent` tool events;
3. keep TUI consuming adapter-derived view events.

If event splitting feels too broad, first thin `Session` construction by moving
`ToolCallProcessor` and `ContextCompactor` factory wiring into bootstrap.
