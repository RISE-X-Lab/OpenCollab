# Step12 - REM-01 + REM-05 Split Event Contracts and Clean Team Boundary

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

After Step11, the application layer owns tool execution and context
compaction, but session runtime events, team orchestration events, and TUI
view rendering still share a single `SessionEvent` shape:

- `opencollab/opencollab/core/session/events.py`
  - defines `SessionEvent(type: str, data: dict)`, `EventSink`, `EventBus`;
  - is the only event vocabulary in the project.
- `opencollab/opencollab/team/orchestrator.py`
  - imports `EventBus`, `EventSink`, `SessionEvent`, `Session`,
    `PermissionPolicy` from `opencollab.core.session`;
  - emits `SessionEvent(type="tool_start", data={"tool": "delegate", ...})`
    and `SessionEvent(type="tool_end", ...)` for delegation start/finish
    (`orchestrator.py:244`, `orchestrator.py:279`);
  - emits `SessionEvent(type="tool_start", data={"tool": "review_loop", ...})`
    for the self-collaboration loop (`orchestrator.py:313`).
- `opencollab/opencollab/team/teammate_factory.py`
  - imports `EventBus`, `PermissionPolicy`, `Session` from
    `opencollab.core.session`;
  - constructs a teammate `Session` directly.
- `opencollab/opencollab/tui/session_adapter.py`
  - imports `EventSink` and `SessionEvent` from `opencollab.core.session`.
- `opencollab/opencollab/cli/tui.py`
  - branches on raw `event.type` strings (`text_delta`, `tool_start`,
    `tool_end`, `step_start`, `compaction`, `loop_detected`,
    `budget_warning`, `error`) and inspects `event.data` dictionaries.

Verification baseline from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 129 passed
```

Boundary state before Step12:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application \
  opencollab/opencollab/domain
# no matches

rg -n "SessionEvent|EventBus|Session\\(" \
  opencollab/opencollab/team \
  opencollab/opencollab/tui
# multiple matches (this step narrows them)
```

## Remaining Problem

The `Team` layer is forced to speak the session runtime's vocabulary:

- Delegation progress is encoded as `tool_start` / `tool_end` events with a
  synthetic tool name (`delegate`, `review_loop`). The TUI then conditionally
  re-interprets those tool names as teammate lifecycle messages.
- The team layer constructs a runtime `Session` directly to drive a teammate,
  binding it to `core.session` internals.
- The TUI consumes `SessionEvent` directly, so any rename or split of the
  internal event shape ripples into rendering code.

This violates two repo rules:

```text
team orchestration events should not overload SessionEvent semantics
TUI consumes adapter output, not internal session/team event semantics
```

The target for this step is to introduce explicit event contracts for the
session runtime and for team orchestration, route team delegation through a
small session-factory port, and translate both event families into TUI view
actions through an adapter. Behavior must remain identical.

## Goal

After this step:

- Application layer owns the event contracts:
  - `SessionRuntimeEvent` covers session lifecycle (text deltas, step
    start/end, tool start/end emitted by the session runtime, compaction,
    loop detection, budget warnings, errors);
  - `TeamEvent` covers delegation/review lifecycle.
- `core.session.events.SessionEvent` becomes a compatibility alias /
  adapter for `SessionRuntimeEvent` so existing call sites keep working.
- `EventBus` continues to fan out, but accepts both event families through
  a single `EventEnvelope`-style abstraction or a shared base class. No
  bus-shape change is required if both event types remain duck-compatible
  with the existing `(type, data)` contract during migration.
- `Team` emits `TeamEvent` for:
  - `delegation_started`
  - `teammate_run_started`
  - `teammate_run_completed`
  - `review_started`
  - `review_completed`
- `Team` builds teammate sessions through a `SessionFactoryPort` injected
  by bootstrap, instead of importing `Session` directly.
- `tui/session_adapter.py` translates both event families into display
  actions; `cli/tui.py` keeps its current visible output verbatim.
- `docs/repomap/repomap-v2.puml` is refreshed to reflect the new
  application event boundary and the team -> application port edge.

This is two architectural boundaries landed together because they touch
the same files in `team/` and `tui/`:

```text
event contracts move from core.session to application
team orchestration depends on application/domain contracts, not core internals
```

## Implementation Plan

### 1. Add characterization tests first

Before moving any code, lock the current observable behavior:

- `opencollab/tests/test_team_event_emission.py` (new)
  - assert exact event order during `Team.delegate("coder", "task")`:
    `tool_start{tool="delegate"}` → teammate session events →
    `tool_end{tool="delegate"}`;
  - assert exact event order during `Team.delegate_with_review(task)`:
    `tool_start{tool="review_loop", iteration=1}` → coder delegation →
    reviewer delegation → optional further iterations;
  - assert that latency, role, and task fields appear with their current
    keys and trimming (e.g., `task[:100]`).
- `opencollab/tests/test_tui_event_rendering.py` (new or extension)
  - drive `event_handler` with synthetic session and team events and snapshot
    the resulting `_active_tools`, `_status_lines`, and `_step` mutations;
  - this is the safety net that lets us re-route TUI through an adapter
    without changing visible output.

Run the new tests against the current code first. They must pass.

### 2. Add application event types

Add:

- `opencollab/opencollab/application/events.py`

Define narrow, frozen dataclasses:

```python
from dataclasses import dataclass, field
from typing import Any, Literal

SessionEventType = Literal[
    "text_delta", "tool_start", "tool_end",
    "step_start", "step_end",
    "compaction", "compaction_applied",
    "loop_detected", "budget_warning", "error",
]

@dataclass(frozen=True)
class SessionRuntimeEvent:
    type: SessionEventType
    data: dict[str, Any] = field(default_factory=dict)

TeamEventType = Literal[
    "delegation_started", "delegation_completed",
    "teammate_run_started", "teammate_run_completed",
    "review_started", "review_completed",
]

@dataclass(frozen=True)
class TeamEvent:
    type: TeamEventType
    data: dict[str, Any] = field(default_factory=dict)
```

Keep payload shape close to today's `event.data` so the TUI adapter can map
1:1 in the migration patch. New keys may be added (e.g.,
`TeamEvent(type="delegation_started", data={"role": ..., "task": ...})`),
but do not remove keys the TUI currently reads.

Do not import from `core`, `tools`, `bootstrap`, `cli`, `tui`, or `team`.

### 3. Make `SessionEvent` a compatibility alias

In `opencollab/opencollab/core/session/events.py`:

- re-export `SessionRuntimeEvent` from `opencollab.application.events`;
- keep `SessionEvent` as an alias for `SessionRuntimeEvent`;
- keep `EventSink`, `EventCallback`, `EventBus` unchanged in signature;
- accept either `SessionRuntimeEvent` or `TeamEvent` in `EventBus.emit`
  (typed as a union or `Any`-with-runtime-protocol).

Existing imports such as
`from opencollab.core.session import SessionEvent, EventBus, EventSink`
keep working.

### 4. Add `SessionFactoryPort`

In `opencollab/opencollab/application/ports.py`:

```python
class SessionFactoryPort(Protocol):
    def build_teammate_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
    ) -> Any: ...
```

Adapt the current `build_teammate_session` function as the default adapter
behind this port. The adapter lives in `bootstrap/`, not in `team/`.

### 5. Route team orchestration through the new contracts

In `opencollab/opencollab/team/orchestrator.py`:

- replace the three `SessionEvent(type="tool_start"/"tool_end", ...)`
  emissions with `TeamEvent` emissions:
  - `delegate(...)` emits
    `TeamEvent("delegation_started", {"role", "task"})` and
    `TeamEvent("delegation_completed", {"role", "latency", "result_len"})`;
  - `delegate_with_review(...)` emits
    `TeamEvent("review_started", {"iteration", "max"})` and
    `TeamEvent("review_completed", {"iteration", "verdict"})`;
- drop the direct `from opencollab.core.session import ... Session ...`
  import in favor of the `SessionFactoryPort`;
- the constructor accepts a session factory via dependency injection;
- `event_bus: EventBus` stays as the publication channel for now (no bus
  shape change in this patch).

In `opencollab/opencollab/team/teammate_factory.py`:

- the module remains as the default factory implementation;
- bootstrap binds it to `SessionFactoryPort`;
- `team.orchestrator` no longer imports it directly.

### 6. Update TUI to consume both event families

In `opencollab/opencollab/tui/session_adapter.py`:

- accept both `SessionRuntimeEvent` and `TeamEvent` in
  `TuiEventSink.emit`;
- translate each into a small `TuiViewAction` (status update, activity
  log line, step counter update) before handing to `tui.event_handler`,
  OR keep the dispatch in `cli/tui.py` but split it into
  `_handle_session_event` and `_handle_team_event` methods;
- pick whichever approach keeps `cli/tui.py` byte-for-byte equivalent on
  golden-path output. The adapter-with-view-action shape is the long-run
  target, but a smaller `_handle_team_event` split is acceptable here if
  it keeps the patch reviewable.

In `opencollab/opencollab/cli/tui.py`:

- the team-shaped branches (`tool == "delegate"`,
  `tool == "delegate_task"`, `tool == "review_loop"`) move out of the
  generic `tool_start` / `tool_end` handler into the team-event handler;
- visible output strings remain unchanged.

### 7. Wire bootstrap

In `opencollab/opencollab/bootstrap/` (existing files):

- create a `SessionFactoryPort` binding that wraps
  `team.teammate_factory.build_teammate_session`;
- pass the factory into `Team` construction;
- no other bootstrap surface changes.

### 8. Refresh the architecture map

- update `docs/repomap/repomap-v2.puml` to:
  - show `application.events` as the owner of `SessionRuntimeEvent`
    and `TeamEvent`;
  - show `team` -> `application.ports.SessionFactoryPort`;
  - show `tui` -> `application.events`, not `core.session.events`;
- render PDF/SVG via the local PlantUML server.

Do not touch `repomap-target.puml` unless the target itself changes.

### 9. Verify

From `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_team_event_emission.py \
  tests/test_tui_event_rendering.py -q

OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_session_characterization.py \
  tests/test_autosave_subscriber.py \
  tests/test_session_adapter.py \
  tests/test_team_decomposition.py -q

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Boundary checks from repo root:

```bash
rg -n "opencollab\\.(core|tools|bootstrap|cli|tui|team)" \
  opencollab/opencollab/application \
  opencollab/opencollab/domain
# no matches

rg -n "opencollab\\.core\\.session" \
  opencollab/opencollab/team/orchestrator.py \
  opencollab/opencollab/team/teammate_factory.py
# only allowed: EventBus / SessionEvent compatibility alias path,
# documented as transitional

rg -n "SessionEvent" \
  opencollab/opencollab/tui/session_adapter.py \
  opencollab/opencollab/cli/tui.py
# imports resolve to application.events; no direct core.session.events imports
```

Manual smoke test (golden path):

```bash
OPENAI_API_KEY=$REAL_KEY uv run opencollab chat
# verify text streaming, tool start/end lines, teammate delegate lines,
# review_loop iteration lines, and compaction status all render identically.
```

## Acceptance Criteria

- `opencollab.application.events.SessionRuntimeEvent` and
  `opencollab.application.events.TeamEvent` exist as frozen value types.
- `opencollab.core.session.events.SessionEvent` is a compatibility alias for
  `SessionRuntimeEvent`; existing imports keep working.
- `opencollab.team.orchestrator` no longer emits `SessionEvent(type="tool_start"/"tool_end")` for delegation or review_loop progress.
- `opencollab.team.orchestrator` emits `TeamEvent` for delegation and review
  lifecycle.
- `opencollab.team.orchestrator` and `opencollab.team.teammate_factory` do not
  construct `Session` directly; they go through `SessionFactoryPort`.
- TUI rendering of session deltas, tool lines, delegate lines, review_loop
  lines, compaction status, and error status is byte-for-byte equivalent on
  the golden path.
- `opencollab.tui.session_adapter` imports event types from
  `opencollab.application.events`.
- `docs/repomap/repomap-v2.puml` reflects the new edges and a fresh
  rendered diagram is committed under `docs/repomap/`.
- Application/domain boundary checks have no new matches.
- Full test suite (`pytest tests/ -q`) is green.

## Non-Goals

- Do not extract `SessionRunUseCase` in this patch
  (REMAIN.md explicitly forbids combining event splitting with session-runner
  extraction without expanded characterization coverage; this step adds
  characterization for team/TUI only, not for the run loop).
- Do not move `SessionRunner` out of `core.session`.
- Do not remove `EventBus`; bus shape stays the same.
- Do not change the autosave subscriber contract.
- Do not change message persistence or JSONL semantics.
- Do not introduce `LLMPort`, `TracePort`, or `TokenEstimatorPort` in this
  step (deferred to Bundle B / REM-03).
- Do not change CLI argument behavior or visible CLI output.
- Do not rename existing event keys consumed by the TUI; only add team-event
  keys.

## Risks And Mitigations

- **TUI regression risk**. The TUI currently special-cases tool names
  (`delegate`, `delegate_task`, `review_loop`) inside the generic
  `tool_start` branch. The new code splits this into a team-event handler.
  Mitigation: characterization test in Step 1 snapshots the current
  `_active_tools` / `_status_lines` mutations and re-runs them against the
  new dispatch.
- **Event-fan-out double-emit risk**. If we leave the old `tool_start` /
  `tool_end` emissions for delegation in place while also adding `TeamEvent`,
  TUI will show every delegation twice. Mitigation: this step *replaces*,
  not duplicates, the delegation emissions; the characterization test
  asserts exact event sequence.
- **Import cycle risk**. `application.events` must not import from `core`.
  Mitigation: boundary check in Step 9 fails the patch if it does.

## Next After This

After Step12, the natural follow-ups are:

1. Bundle B (REM-03 + REM-04 + REM-06 + REM-07):
   - finish narrowing application ports (`LLMPort`, `SessionStorePort`,
     `TracePort`, `TokenEstimatorPort`, `ToolPort`, `RepoMapPort`,
     `WorktreePoolPort`);
   - move `Session`'s concrete construction (`EventBus`,
     `AutoSaveSubscriber`, `SessionStore`, `ToolCallProcessor`,
     `ContextCompactor`, `SessionRunner`) into `bootstrap/container.py`;
   - retire `tool_runtime_from_legacy` and `Tool.execute(env=,
     interceptor=, confirm_fn=)`.
2. Bundle C (REM-02):
   - extract `SessionRunUseCase` into `opencollab.application.session_run`;
   - keep `core.session.runner.SessionRunner` as a facade.

Bundle C is intentionally last because it touches the run loop and is
incompatible with concurrent event-shape changes.
