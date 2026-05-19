# Step04 - Hoist `EventBus` into the application layer

Date: 2026-05-20
Branch: open `refactor/step04-event-bus` off the merged Step 03 branch.

## Goal

Move the runtime event fan-out (`EventBus` class + `EventSink` Protocol +
`EventCallback` alias) out of `opencollab/opencollab/core/session/events.py`
and into a new `opencollab/opencollab/application/event_bus.py`. Leave
`core/session/events.py` as a thin compat shim so the
characterization-pinned `core/events.py` keeps working.

After this step, the runtime fan-out lives next to its port
(`EventPublisherPort` at `application/ports.py:46`) instead of straddling
the `core/` boundary. It also unblocks dissolution of `core/session/` —
every module under that package currently imports `EventBus` from itself.

Like Steps 01–03, this is a **pure relocation** with one tiny additive
artifact (a structural test) and no behavior, signature, or class-name
changes.

## Current Evidence

### What `core/session/events.py` contains today

```python
# domain re-exports for backward compatibility
from opencollab.domain.events import SessionRuntimeEvent as _SessionRuntimeEvent
from opencollab.domain.events import TeamEvent as _TeamEvent
SessionEvent = _SessionRuntimeEvent
SessionRuntimeEvent = _SessionRuntimeEvent
TeamEvent = _TeamEvent

# runtime infrastructure (the part being hoisted in this step)
EventCallback = Callable[[Any], Awaitable[None] | None]

class EventSink(Protocol):
    async def emit(self, event: Any) -> None: ...

class EventBus:
    """Fan-out broadcaster. Multiple subscribers; failures isolated per-sink."""
    def __init__(self, target: EventSink | EventCallback | None = None): ...
    def subscribe(self, target: EventSink | EventCallback) -> None: ...
    @property
    def sink(self) -> EventSink | EventCallback | None: ...
    async def emit(self, event: Any) -> None: ...
```

`EventBus` is pure-Python asyncio fan-out with no I/O dependencies. It
structurally implements `EventPublisherPort` (Protocol with one
`async emit(event) -> None`). `EventSink` is the same Protocol, just
spelled differently.

### Caller surface (`rg "from opencollab\.core\.session\.events"`)

13 import sites:

Production (within `opencollab/opencollab/`):
- `bootstrap/container.py`
- `core/session/__init__.py`
- `core/session/autosave.py`
- `core/session/compactor.py`
- `core/session/runner.py`
- `core/session/session.py`
- `core/session/tools.py`
- `core/events.py` (top-level compat shim, pinned by characterization test)

Tests:
- `tests/test_autosave_subscriber.py`
- `tests/test_session_construction.py`
- `tests/test_session_run_loop.py`
- `tests/test_team_decomposition.py`
- `tests/test_tool_call_processor_interceptor.py`

### What stays put and why

- `core/events.py` (top-level shim) is asserted identical to
  `core/session/events.py` exports by
  `tests/test_session_characterization.py:139–140`. Do not touch it.
- `SessionEvent`, `SessionRuntimeEvent`, `TeamEvent` re-exports in
  `core/session/events.py` stay — they are domain value types and the
  shim file is a convenient single re-export point for legacy callers.
- `core/session/state.py` (2-line `SessionState` shim) is unrelated and
  not in this step's scope.

## Target Shape For This Step

```text
opencollab/opencollab/application/
  __init__.py
  compaction.py
  event_bus.py        # new — EventBus + EventSink + EventCallback
  ports.py
  session_run.py
  tool_dispatch.py
  tool_execution.py
  tool_runtime.py

opencollab/opencollab/core/session/
  events.py           # becomes a thin shim:
                      #   re-exports EventBus / EventSink / EventCallback
                      #   from opencollab.application.event_bus
                      #   re-exports SessionEvent / SessionRuntimeEvent / TeamEvent
                      #   from opencollab.domain.events
```

`application/event_bus.py` contents (verbatim move from
`core/session/events.py`, with the module docstring updated):

```python
"""Runtime event fan-out — implementation of EventPublisherPort.

The bus accepts any event object that carries `type` and `data` attributes
(structurally a DomainEvent). Subscribers may be either async callables or
objects with an async `emit` method. Per-subscriber failures are isolated
so one bad sink cannot break siblings or the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol


EventCallback = Callable[[Any], Awaitable[None] | None]


class EventSink(Protocol):
    async def emit(self, event: Any) -> None: ...


class EventBus:
    def __init__(self, target: EventSink | EventCallback | None = None): ...
    def subscribe(self, target: EventSink | EventCallback) -> None: ...
    @property
    def sink(self) -> EventSink | EventCallback | None: ...
    async def emit(self, event: Any) -> None: ...


__all__ = ["EventBus", "EventCallback", "EventSink"]
```

`core/session/events.py` becomes:

```python
"""Compatibility shim — runtime fan-out moved to application.event_bus.

This module remains because:
  - core/events.py re-exports from here, and the characterization test pins
    `from opencollab.core.events import EventBus, SessionEvent` to specific
    object identities.
  - Existing test files import EventBus / SessionEvent from this location.

New production code should import EventBus from opencollab.application.event_bus
and event value types from opencollab.domain.events.
"""

from opencollab.application.event_bus import EventBus, EventCallback, EventSink
from opencollab.domain.events import SessionRuntimeEvent, TeamEvent

# Legacy alias preserved for the v3 event split migration.
SessionEvent = SessionRuntimeEvent

__all__ = [
    "EventBus",
    "EventCallback",
    "EventSink",
    "SessionEvent",
    "SessionRuntimeEvent",
    "TeamEvent",
]
```

Dependency direction after this step:

```text
application/event_bus.py   -> stdlib only
application/ports.py       -> stdlib only (unchanged)
core/session/events.py     -> application.event_bus + domain.events
core/events.py             -> core.session.events (unchanged shim)
core/session/*             -> application.event_bus (direct, no shim)
bootstrap/container.py     -> application.event_bus (direct)
```

No new arrow points outward.

## Implementation Plan

Single branch, two commits.

### 1. Create `application/event_bus.py` and update the shim

- Create `opencollab/opencollab/application/event_bus.py` with the
  exact `EventBus`, `EventSink`, `EventCallback` definitions currently
  in `core/session/events.py`. Update the module docstring to describe
  the new home.
- Rewrite `opencollab/opencollab/core/session/events.py` to the
  re-export shim shown above. Keep all five exported names
  (`EventBus`, `EventCallback`, `EventSink`, `SessionEvent`,
  `SessionRuntimeEvent`, `TeamEvent`) so existing callers continue to
  work.
- Update `opencollab/opencollab/application/__init__.py` to re-export
  the new names if it currently re-exports other application surface
  (it does — verify and follow the existing pattern; otherwise leave it
  alone).

Verify:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(application): hoist event bus`.

### 2. Migrate production callers to import directly

Update these production files to import `EventBus` / `EventSink` /
`EventCallback` from `opencollab.application.event_bus`:

- `opencollab/opencollab/bootstrap/container.py`
- `opencollab/opencollab/core/session/__init__.py`
- `opencollab/opencollab/core/session/autosave.py`
- `opencollab/opencollab/core/session/compactor.py`
- `opencollab/opencollab/core/session/runner.py`
- `opencollab/opencollab/core/session/session.py`
- `opencollab/opencollab/core/session/tools.py`

For each: import `SessionEvent` / `SessionRuntimeEvent` / `TeamEvent`
value types from `domain.events` as needed, preserving the local public
name (for example `SessionRuntimeEvent as SessionEvent`).

Leave the five test files importing through `core.session.events` for
now — migrating them is mechanically identical but they belong in a
later cleanup pass that retires the shim entirely.

Leave `core/events.py` untouched.

Verify:

```bash
rg "from opencollab\.core\.session\.events import" opencollab/opencollab \
  | rg -v "core/session/events\.py|core/events\.py"
# expect: empty — every production module now imports from application.event_bus
# for the bus, and from domain.events for value types.

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(application): rewire bus consumers`.

### 3. Add a structural assertion (in the same second commit)

Append to `tests/test_session_construction.py` (or wherever the
application-port tests live; pick the file that already imports
`EventPublisherPort`):

```python
def test_event_bus_satisfies_event_publisher_port():
    from opencollab.application.event_bus import EventBus
    from opencollab.application.ports import EventPublisherPort

    bus: EventPublisherPort = EventBus()  # mypy/pyright-level structural check
    assert hasattr(bus, "emit")
    assert callable(getattr(bus, "emit"))
```

This is a one-line behavioral pin that the bus continues to satisfy the
port. Cheap, durable.

## Acceptance Criteria

- `opencollab/opencollab/application/event_bus.py` exists and contains
  `EventBus`, `EventSink`, `EventCallback`.
- `opencollab/opencollab/core/session/events.py` is a re-export shim;
  it no longer defines `class EventBus`, `class EventSink`, or
  `EventCallback`.
- All seven production callers listed above import `EventBus` /
  `EventSink` / `EventCallback` from `opencollab.application.event_bus`.
- `tests/test_session_characterization.py:139–140` still passes
  (`CompatEventBus is EventBus` is preserved through the shim chain
  `core.events → core.session.events → application.event_bus`).
- New structural test passes.
- Full test suite passes (expect 161 tests = 160 + the new one).
- No module under `opencollab/opencollab/domain/` gains a new import.

## Non-Goals

- Do **not** retire `core/session/events.py` — characterization tests
  rely on the import chain.
- Do **not** migrate the five test files still going through
  `core.session.events`; that is its own cleanup step.
- Do **not** touch `core/events.py`.
- Do **not** promote `SessionRunner` to `application/`; that module is
  bootstrap-glue masquerading as a state machine and should be deleted
  or demoted in a later step.
- Do **not** rename `EventSink` to `EventPublisherPort`. They are
  structurally identical Protocols but renames are a separate concern.
- Do **not** add new methods to `EventBus`. The hoist preserves the
  exact public surface.

## Rollback Plan

Two commits, each independently revertible.

- Reverting commit 2 returns production callers to the shim path; the
  shim still works because commit 1 made it transitive.
- Reverting commit 1 (after reverting 2) restores `EventBus` to
  `core/session/events.py` and deletes `application/event_bus.py`.

If only the structural test fails (very unlikely), drop the test rather
than reverting the move.
