# Clean Architecture Remaining Work

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

This note records the remaining implementation work after the current
Clean Architecture phase through Step11.

## Current Code Review

Implemented boundaries:

- CA-02 is complete: session tool processing no longer depends directly on
  `tools.safety`.
- CA-03 is complete enough for the current built-in tools: tool dispatch now
  runs through `ToolRuntime`, with legacy compatibility isolated in
  application-level dispatch helpers.
- CA-04 has started: pure session/tool/compaction value objects now live in
  `opencollab.domain`.
- CA-05 has started:
  - `ToolExecutionUseCase` lives in `opencollab.application.tool_execution`.
  - `ContextCompactionUseCase` lives in `opencollab.application.compaction`.
  - `core.session.tools.ToolCallProcessor` and
    `core.session.compactor.ContextCompactor` are now compatibility facades.

Verification from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_context_compaction_use_case.py \
  tests/test_session_characterization.py \
  tests/test_autosave_subscriber.py -q
# 54 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 129 passed
```

Boundary check:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application \
  opencollab/opencollab/domain
# no matches
```

## Current Distance From Target

Pragmatic status after Step11:

- Maintainability decoupling: roughly 80-85%.
- Clean Architecture dependency rule: roughly 65-70%.
- Distance to the target diagram: roughly 35-40% remaining.

The largest remaining gap is not the small use cases already extracted. It is
the runtime shell around them:

- `Session` still constructs concrete collaborators.
- `SessionRunner` still owns the main run-loop orchestration inside
  `core.session`.
- `SessionEvent` is still used as a shared event type for session runtime,
  team delegation, autosave, and TUI display.
- Team orchestration still depends on `core.session` concrete types.
- Application ports are still incomplete.

## Remaining Implementation Backlog

### REM-01 - Split Event Contracts

Goal:

Separate session runtime events, team orchestration events, and UI view events.

Current coupling:

- `opencollab.team.orchestrator` imports `SessionEvent`, `EventBus`, and
  `Session` from `opencollab.core.session`.
- Team delegation progress still emits `tool_start` / `tool_end` style
  `SessionEvent` objects.
- `opencollab.tui.session_adapter` consumes `SessionEvent` directly.

Implementation plan:

1. Add event value types under `opencollab.application.events` or
   `opencollab.domain.events`:
   - `SessionRuntimeEvent`
   - `TeamEvent`
   - optionally a small `EventEnvelope` if one bus must carry multiple event
     families during compatibility.
2. Keep `core.session.events.SessionEvent` as a compatibility alias or adapter
   during migration.
3. Update `Team` to emit team-specific events for delegation lifecycle:
   - delegation started;
   - teammate run started;
   - teammate run completed;
   - review completed.
4. Update the TUI adapter to translate runtime/team events into display
   actions.
5. Keep user-visible CLI/TUI behavior unchanged.

Acceptance:

- Team no longer overloads session tool events for delegation progress.
- TUI depends on adapter output, not internal session/team event semantics.
- Existing autosave events still fire for message mutations.
- Full tests pass.

### REM-02 - Extract Session Run Use Case

Goal:

Move the main session loop out of `core.session.runner` into the application
layer.

Current coupling:

- `SessionRunner` still owns cancellation handling, budget checks, LLM calls,
  event emission, compaction checks, tool execution calls, and step tracing.
- It imports concrete session event and facade types from `core.session`.

Implementation plan:

1. Add `opencollab.application.session_run`.
2. Move the run-loop behavior into `SessionRunUseCase`.
3. Inject dependencies structurally:
   - `SessionState`
   - LLM port or structural LLM client
   - event publisher
   - tool execution use case
   - compaction use case
   - tracer
   - token/budget policy
4. Keep `core.session.runner.SessionRunner` as a thin compatibility facade.
5. Preserve all event names and payloads until REM-01 is complete, or pass
   event factories into the use case as Step10 and Step11 did.

Acceptance:

- `application.session_run` imports only application/domain/standard-library
  modules.
- `core.session.runner` becomes a facade.
- Session lifecycle state is still mutated only through `SessionState` or
  `Session`.
- Session characterization tests remain green.

### REM-03 - Complete Application Ports

Goal:

Make application dependencies explicit before finishing the composition root.

Ports still needed or worth tightening:

- `LLMPort`
- `SessionStorePort`
- `TracePort`
- `TokenEstimatorPort`
- `ToolPort`
- `RepoMapPort`
- `WorktreePoolPort`
- `SessionFactoryPort`
- event publisher / event sink contracts after REM-01

Implementation plan:

1. Extend `opencollab.application.ports` with small structural protocols.
2. Keep protocols narrow. Do not encode whole concrete classes as ports.
3. Update extracted use cases to depend on those ports where it removes an
   `Any` or a concrete import.
4. Add boundary tests that prevent application modules from importing
   `core`, `tools`, `bootstrap`, `cli`, `tui`, or `team`.

Acceptance:

- Application dependencies are explicit and narrow.
- No behavior change.
- Boundary tests document the dependency rule.

### REM-04 - Complete Composition Root

Goal:

Move concrete runtime construction out of `Session`.

Current coupling:

- `core.session.session.Session` still constructs:
  - `EventBus`
  - `AutoSaveSubscriber`
  - `SessionStore`
  - `ToolCallProcessor`
  - `ContextCompactor`
  - `SessionRunner`

Implementation plan:

1. Introduce a session assembly object in `bootstrap/session_factory.py` or a
   new `bootstrap/container.py`.
2. Build runtime collaborators in bootstrap.
3. Change `Session` to accept already-built collaborators or a small
   `SessionRuntime` bundle.
4. Keep the public `Session(...)` constructor compatible by routing old
   arguments through a default factory.
5. Gradually move bootstrap-only imports out of `core.session`.

Acceptance:

- `Session` becomes a public facade over state and application use cases.
- Concrete SDK/storage/UI/tool wiring lives in bootstrap/adapters.
- Existing CLI construction behavior remains unchanged.

### REM-05 - Clean Team Boundary

Goal:

Make team orchestration depend on application/domain contracts instead of
`core.session` concrete internals.

Current coupling:

- `team.orchestrator` imports `Session`, `SessionEvent`, `EventBus`,
  `EventSink`, and `PermissionPolicy`.
- `team.teammate_factory` imports `Session`, `EventBus`, and
  `PermissionPolicy`.

Implementation plan:

1. Define a small session-runner/session-factory port for team use.
2. Move teammate session construction behind bootstrap or an application
   factory.
3. Convert team delegation progress to `TeamEvent`.
4. Keep existing teammate behavior and worktree isolation unchanged.

Acceptance:

- `opencollab.team` no longer constructs or depends on concrete session
  internals except through compatibility adapters.
- Team tests cover delegation event behavior.
- No CLI team behavior change.

### REM-06 - Finish CLI And Evaluator Wiring

Goal:

Keep CLI as a driver and move policy-adjacent construction into bootstrap.

Implementation plan:

1. Review `opencollab.cli.main` for remaining config resolution, evaluator,
   and runtime construction policy.
2. Move construction into bootstrap functions where it is not already isolated.
3. Keep CLI argument behavior and output unchanged.

Acceptance:

- CLI remains a thin driver.
- Public command behavior is unchanged.

### REM-07 - Retire Legacy Tool Compatibility

Goal:

Remove old tool execution compatibility after the new runtime contract is fully
adopted.

Candidates:

- `Tool.execute(..., env=..., interceptor=..., confirm_fn=...)`
- `ToolCallProcessor.interceptor`
- `tool_runtime_from_legacy(interceptor=...)`
- legacy fallback paths in `application.tool_dispatch`

Implementation plan:

1. First inventory external or test-only callers.
2. Decide whether these are public extension points.
3. If they are not public, remove them with focused tests.
4. If they are public, keep adapters but move them to an explicit
   compatibility module.

Acceptance:

- Built-in tools use only `ToolRuntime`.
- Any remaining legacy path is deliberately named as compatibility.
- Tool schemas remain unchanged.

### REM-08 - Refresh Architecture Artifacts

Goal:

Keep the diagrams honest after the next structural changes.

Implementation plan:

1. Regenerate or update:
   - `docs/repomap/repomap-v2.puml`
   - `docs/repomap/repomap-target.puml` only if the target changes.
2. Render PDFs under `docs/repomap/`.
3. Compare actual dependencies against the target diagram.

Acceptance:

- Current map reflects code after REM-01 through REM-05.
- Target map remains aspirational, not a disguised current-state diagram.

## Recommended Next Step

Do REM-01 next.

Reason:

- It directly addresses one of the repo rules:
  "Team orchestration events should not overload SessionEvent semantics."
- It reduces coupling before extracting the session run-loop.
- It makes the later TUI and team boundary cleanup less risky.
- It is large enough for the stronger multi-step planning style, but still has
  one clear architectural theme.

Suggested next plan file:

```text
docs/plans/clean-architecture/Step12.md
```

Suggested Step12 scope:

```text
Split session runtime events, team events, and TUI view adaptation while
preserving current CLI/TUI behavior.
```

## Non-Goals For The Remaining Phase

- Do not rewrite the whole framework.
- Do not change public CLI behavior.
- Do not change provider API behavior.
- Do not change tool schemas unless a specific compatibility decision is made.
- Do not migrate session storage format unless separately planned.
- Do not combine event splitting with session-runner extraction in the same
  patch unless characterization coverage is expanded first.

## Verification Checklist

Run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Boundary checks from repo root:

```bash
rg -n "opencollab\\.(core|application|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/domain

rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application

rg -n "SessionEvent|EventBus|Session\\(" \
  opencollab/opencollab/team \
  opencollab/opencollab/tui
```

The first two checks should have no matches, except for intentionally documented
compatibility modules if a step explicitly creates one.
