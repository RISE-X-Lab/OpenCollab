# Step15 - REM-02 Extract Session Run Use Case (+ Step14 hotfix)

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

Step14 retired the legacy tool execution bridge. `Tool.execute(...)` is
gone from the base class; concrete tools only override
`execute_with_runtime(params, runtime)`. Test suite: 152 passed.

The remaining big runtime concern is `SessionRunner` itself.

`opencollab/opencollab/core/session/runner.py` (217 lines) owns:

- the loop body (`run_loop`, `_advance`);
- cancellation handling and budget check (`_precheck`);
- compaction trigger (`_run_compaction`);
- LLM call + tracing (`_run_llm_call`, `_call_llm`,
  `_record_llm_trace`);
- response handling (`_handle_pending_response`);
- tool execution (`_execute_pending_tools`);
- step lifecycle (`_autosave_pending_step`, `_finish_step`);
- direct imports of `opencollab.core.llm.LLMResponse`,
  `opencollab.core.session.events.{EventBus, SessionEvent}`,
  `opencollab.core.session.compactor.ContextCompactor`,
  `opencollab.core.session.tools.ToolCallProcessor`.

Two of those imports are facades over application use cases:

- `ContextCompactor` (`core/session/compactor.py`) delegates to
  `application.compaction.ContextCompactionUseCase`;
- `ToolCallProcessor` (`core/session/tools.py`) delegates to
  `application.tool_execution.ToolExecutionUseCase`.

So the run loop reaches application logic through two facades and emits
events typed as `SessionEvent` (which is already a compatibility alias
for `application.events.SessionRuntimeEvent` after Step12).

### Step14 hotfix: delegate tools still use the legacy signature

`opencollab/opencollab/team/orchestrator.py` defines two `Tool`
subclasses that **only** implement the deleted legacy `execute(...)`
signature:

- `DelegateTaskTool.execute(...)` at `orchestrator.py:81-91`;
- `DelegateWithReviewTool.execute(...)` at `orchestrator.py:128-138`.

After Step14 the base class `execute_with_runtime` raises
`NotImplementedError`. The current test suite never drives the Lead's
LLM to emit a `delegate_task` or `delegate_with_review` tool call
(every team test invokes `Team.delegate(...)` directly), so the
regression is invisible — but the first real Lead -> delegate call
will crash.

Verification baseline from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 152 passed (the latent delegate-tool break is not covered)
```

## Goal

After this step:

- `opencollab.application.session_run.SessionRunUseCase` owns the loop
  body, phase transitions, cancellation, budget check, LLM call,
  tracing, compaction trigger, tool execution coordination, and step
  lifecycle.
- The use case depends only on application/domain modules + standard
  library. Concrete `LLMResponse`, `EventBus`, and event class imports
  from `core.session` are gone.
- `opencollab.core.session.runner.SessionRunner` remains as a thin
  compatibility facade that constructs the use case from its existing
  kwargs and delegates `run_loop(...)`.
- Bootstrap (`bootstrap.container.build_session_runtime`) is unchanged
  externally; internally it can either keep wiring through the facade
  or call the use case directly.
- `DelegateTaskTool` and `DelegateWithReviewTool` implement
  `execute_with_runtime(params, runtime)`. The Step14 guard test
  (`test_no_concrete_tool_defines_legacy_execute`) is added and prevents
  recurrence.
- Tool schemas, event names, payload keys, and CLI/TUI behavior are
  unchanged.

This step is one architectural boundary plus one Step14 hotfix:

```text
session run loop moves from core.session to application
delegate tools migrate to execute_with_runtime
```

## Implementation Plan

### 0. Step14 hotfix (must land first, ideally as its own commit)

Fix the delegate tools and add the missing guard:

- `opencollab/opencollab/team/orchestrator.py`
  - replace `DelegateTaskTool.execute(...)` with:

    ```python
    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        role = params["role"]
        task = params["task"]
        context = params.get("context", "")
        return await self._team.delegate(role, task, context)
    ```

  - same shape for `DelegateWithReviewTool.execute_with_runtime`;
  - drop the now-unused
    `Environment`, `SafetyPolicyPort`, `Callable`, `Awaitable` imports
    on these classes;
  - add `from opencollab.application.tool_runtime import ToolRuntime`.
- `opencollab/tests/test_tool_call_processor_interceptor.py`
  - add the guard the Step14 plan called for:

    ```python
    def test_no_concrete_tool_defines_legacy_execute():
        import pkgutil, importlib, inspect
        from opencollab.tools.base import Tool
        offenders: list[str] = []
        # Walk opencollab.tools and opencollab.team for Tool subclasses.
        for pkg_name in ("opencollab.tools", "opencollab.team"):
            pkg = importlib.import_module(pkg_name)
            for _, mod_name, _ in pkgutil.walk_packages(
                pkg.__path__, prefix=f"{pkg_name}."
            ):
                mod = importlib.import_module(mod_name)
                for cls in vars(mod).values():
                    if (
                        isinstance(cls, type)
                        and issubclass(cls, Tool)
                        and cls is not Tool
                        and "execute" in cls.__dict__
                    ):
                        offenders.append(f"{mod_name}.{cls.__name__}")
        assert offenders == [], offenders
    ```

- `opencollab/tests/test_team_decomposition.py` (extend)
  - add a test that builds `DelegateTaskTool(team)` and
    `DelegateWithReviewTool(team)` and asserts they expose
    `execute_with_runtime`; drive each through a fake
    `Team.delegate` / `Team.delegate_with_review` and assert the
    string return path works.

Run `pytest tests/ -q` after this commit and confirm: 152 → ~155
passing (depending on how many new assertions land).

### 1. Add characterization tests for the run loop

Before moving code, lock observable behavior of `SessionRunner.run_loop`
that isn't already covered by `test_session_characterization.py`:

- `opencollab/tests/test_session_run_loop.py` (new)
  - exit conditions: budget exceeded emits
    `SessionEvent("error", {"reason": "budget_exceeded"})` and sets
    phase to `BUDGET_EXCEEDED`;
  - cancellation: a set `cancel_event` injects the interrupt system
    message, emits `error` with `reason=cancelled`, and sets phase to
    `CANCELLED`;
  - compaction trigger: when the (injected) compactor's `should_compact`
    returns True during `_precheck`, the next phase is `COMPACTING` and
    `compaction_applied` is emitted with `tokens_after` after a
    `did_compact=True` result;
  - step lifecycle: each LLM step emits `step_start` then `step_end`
    with the same `step` integer and the latency the runner recorded;
  - LLM call: the runner forwards `temperature=agent.temperature` and
    the agent's tool schemas;
  - response handling: when `response.tool_calls` is empty, the runner
    marks done, emits `step_end`, and sets phase to `DONE`; when
    populated, the next phase is `EXECUTING_TOOLS`;
  - trace: `tracer.log_step(step_type="llm_call", payload={...},
    tokens=..., latency=...)` is called with the exact payload shape
    the current `_record_llm_trace` builds.

The fake LLM, fake compactor, fake tool processor, and fake tracer
should be the minimal duck types the runner uses today. Use these
fakes both before and after the move so the test suite never depends on
the concrete `core.session.*` types during the run-loop migration.

Run against current code first. All new tests must pass before any
move.

### 2. Extract `SessionRunUseCase` into the application layer

Add `opencollab/opencollab/application/session_run.py`:

```python
@dataclass(frozen=True)
class SessionRunEventFactory:
    step_start: Callable[[int], Any]
    step_end: Callable[[int, float], Any]
    text_delta: Callable[[str], Any]
    error: Callable[[str], Any]
    compaction_applied: Callable[[int], Any]


class SessionRunUseCase:
    def __init__(
        self,
        *,
        agent: Any,
        state: SessionState,
        llm: LLMPort,
        event_publisher: EventPublisherPort,
        event_factory: SessionRunEventFactory,
        tool_execution: ToolExecutionUseCase,
        compaction: ContextCompactionUseCase,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 200_000,
        max_steps: int = 100,
    ): ...

    async def run_loop(
        self, cancel_event: asyncio.Event | None = None
    ) -> str: ...
```

Key choices:

- **Depend on application use cases, not core facades**. Replace the
  `tool_processor: ToolCallProcessor` and `compactor: ContextCompactor`
  attributes with `ToolExecutionUseCase` and `ContextCompactionUseCase`
  directly. The facades in `core.session` continue to wrap those same
  use cases for callers that build a `Session` the old way.
- **Drop the `core.llm.LLMResponse` import**. Treat the LLM response as
  the duck-typed shape the use case actually reads
  (`response.content: str | None`, `response.tool_calls: list[dict]`,
  `response.finish_reason: str`, `response.usage.total_tokens: int`).
  Type it as `Any` for now; tighten to a `LLMResponsePort` only if a
  second implementation needs it.
- **Use `SessionRuntimeEvent`**. Construct events via the injected
  `event_factory` so the application file never imports
  `core.session.events`. Bootstrap supplies a factory that returns
  `SessionRuntimeEvent` instances (which `EventBus` already handles).
- **Phase transitions stay identical**. Preserve the exact `match` on
  `SessionPhase` from `runner.py:67-85`. Same enum, same order.
- **Trace payload byte-equivalent**. Move `_record_llm_trace` verbatim,
  only changing `self.tracer` typing.

Test boundary check after this file is added:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application/session_run.py
# no matches
```

### 3. Reshape `core.session.runner.SessionRunner` into a facade

Rewrite `opencollab/opencollab/core/session/runner.py`:

- keep the public constructor signature exactly as today (tests and
  bootstrap rely on it);
- inside `__init__`, build a `SessionRunEventFactory` that constructs
  `SessionEvent` instances (which are `SessionRuntimeEvent` aliases) and
  build a `SessionRunUseCase` from the kwargs;
- expose `state`, `event_bus`, `llm`, `tracer`, `tool_processor`,
  `compactor`, `max_budget_tokens`, `max_steps` as attributes (some
  tests read them);
- delegate `run_loop(...)` to the use case;
- remove every private `_advance`, `_precheck`, `_run_compaction`,
  `_run_llm_call`, `_handle_pending_response`, `_execute_pending_tools`,
  `_autosave_pending_step`, `_finish_step`, `_clear_pending_step`,
  `_build_tool_schemas`, `_call_llm`, `_record_llm_trace`,
  `_append_assistant_message` from this file. They now live in the use
  case.

Result: the facade is ~30 lines plus the event-factory builder.

If a private method survives because a test reads it directly
(e.g., `test_session_characterization.py` patches `_call_llm`), keep
the same method name on the facade as a thin forwarder to a use-case
hook. Prefer rewriting the test to inject a `FakeLLM` instead — the
characterization tests added in Step 1 should make that easy.

### 4. Bootstrap wiring update

`opencollab/opencollab/bootstrap/container.py` constructs `SessionRunner`
today. Two options:

- **Option A (recommended)**: leave bootstrap calling
  `SessionRunner(...)`. The facade now builds the use case internally.
  Zero diff in `container.py`.
- **Option B**: bootstrap builds the `SessionRunUseCase` directly and
  attaches it as `runtime.runner` if/when `runner` becomes optional.

Pick Option A unless removing the facade in this patch is trivial. The
goal is small, reviewable diffs; facade removal is a separate later
step.

### 5. Application surface updates

`opencollab/opencollab/application/__init__.py` — re-export
`SessionRunUseCase`, `SessionRunEventFactory`:

```python
from opencollab.application.session_run import (
    SessionRunEventFactory,
    SessionRunUseCase,
)
```

Update `__all__` accordingly.

### 6. Boundary checks

After the move, the dependency rule must still hold:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application \
  opencollab/opencollab/domain
# no matches

rg -n "opencollab\\.core\\.session|opencollab\\.core\\.llm" \
  opencollab/opencollab/application/session_run.py
# no matches

rg -n "from opencollab\\.core\\.llm import LLMResponse" \
  opencollab/opencollab/application
# no matches
```

`test_application_boundaries.py` already enforces the first two
implicitly; rerun to confirm.

### 7. Verify

From `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_session_run_loop.py \
  tests/test_session_characterization.py \
  tests/test_application_boundaries.py -q

OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_tool_execution_use_case.py \
  tests/test_context_compaction_use_case.py \
  tests/test_session_construction.py \
  tests/test_team_decomposition.py \
  tests/test_tool_call_processor_interceptor.py -q

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Manual smoke test (golden path):

```bash
OPENAI_API_KEY=$REAL_KEY uv run opencollab chat
# verify multi-step chat, at least one tool call, compaction trigger
# under a low compaction threshold, cancel via Ctrl+C, and budget cap.
```

Targeted manual check for the Step14 hotfix:

```bash
OPENAI_API_KEY=$REAL_KEY uv run opencollab team --use-worktrees
# ask the Lead something that should trigger a delegate_task call;
# verify the teammate runs and the Lead receives the summary.
```

## Acceptance Criteria

- `opencollab.application.session_run.SessionRunUseCase` exists and
  owns the run loop, cancellation, budget check, LLM call, tracing,
  compaction trigger, tool execution coordination, and step lifecycle.
- `opencollab.application.session_run` does not import from `core`,
  `tools`, `bootstrap`, `cli`, `tui`, or `team`.
- `opencollab.core.session.runner.SessionRunner` is a compatibility
  facade with the same public constructor and `run_loop(...)`.
- All current event names and payload keys are preserved:
  `step_start{step}`, `step_end{step,latency}`, `text_delta{content}`,
  `error{reason}`, `compaction_applied{tokens_after}`.
- `DelegateTaskTool` and `DelegateWithReviewTool` implement
  `execute_with_runtime(params, runtime)`; neither defines `execute`.
- `test_no_concrete_tool_defines_legacy_execute` exists and passes.
- Boundary tests (`test_application_boundaries.py`) pass.
- New run-loop characterization tests
  (`test_session_run_loop.py`) pass.
- `test_session_characterization.py` continues to pass without
  monkey-patching private runner internals (rewrite if needed to inject
  fakes through the constructor instead).
- Full test suite (`pytest tests/ -q`) is green.
- CLI/TUI behavior on the golden path is unchanged.

## Non-Goals

- Do not remove the `SessionRunner` facade in this patch.
- Do not remove the `ToolCallProcessor` or `ContextCompactor` facades.
- Do not move `LLMClient`, `Tracer`, `SessionStore`, `EventBus`, or
  `AutoSaveSubscriber` between layers.
- Do not change the `SessionPhase` enum order or names.
- Do not change the trace payload schema.
- Do not change autosave semantics (autosave still listens for
  `step_end` and `user_message_appended` on the bus).
- Do not change message persistence format.
- Do not change CLI argument behavior.
- Do not introduce `LLMResponsePort` unless a second producer needs it.

## Risks And Mitigations

- **Test fixtures patch private runner methods**.
  `test_session_characterization.py` historically monkey-patches deep
  internals such as `_call_llm` or `_record_llm_trace`. Mitigation:
  Step 1's characterization tests cover the same observable behavior
  through public surfaces (FakeLLM, fake tracer); rewrite the
  characterization tests to use those fakes instead of monkey-patching
  the facade's internals.
- **`LLMResponse` shape mismatch**. The run loop currently reads
  `response.content`, `response.tool_calls`, `response.finish_reason`,
  and `response.usage.total_tokens`. Mitigation: document this contract
  as a docstring at the top of `session_run.py`; the structural shape
  matches `core.llm.LLMResponse` exactly today.
- **Event ordering regression**. Phase ordering is the contract
  consumers rely on (autosave fires on `step_end`, TUI updates on
  `text_delta` / `tool_start` / `tool_end`). Mitigation: characterization
  tests in Step 1 assert exact event sequences.
- **Hidden delegate-tool bug already in main**. The hotfix in Step 0
  exists precisely because Step14's deletion of `Tool.execute` left
  `DelegateTaskTool` broken. Mitigation: do not start Step 1 until
  Step 0 is committed and the guard test is green.

## Next After This

Step16 (REM-08) — refresh architecture artifacts:

- regenerate `docs/repomap/repomap-v2.puml` to reflect the
  post-Step15 dependency graph;
- render PDF/SVG via the local PlantUML server;
- compare against `repomap-target.puml` and update the target only if
  the goal itself has shifted.

After Step16, `core.session` should hold only thin facades and the
event compatibility alias. Optional follow-ups (not in scope):

- consider removing facades once external callers are confirmed to
  import directly from `application` or through `Session`;
- consider renaming `core.session` to `runtime` to reflect that it now
  holds only runtime adapters;
- consider promoting `LLMResponse` to a `domain.llm` value type if a
  second LLM provider needs it.

These are optional polish — the Clean Architecture phase ends at
Step16.
