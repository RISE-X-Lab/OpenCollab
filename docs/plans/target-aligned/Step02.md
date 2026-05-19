# Step02 - Hoist pure entities into `domain/`

Date: 2026-05-19
Branch: open `refactor/step02-domain-entities` off the merged Step 01 branch.

## Goal

Move two pure-data modules that currently live one layer too far out into
the `domain/` layer, where the target architecture places them:

1. `core/agent.py` (the `Agent` dataclass) → `domain/agent.py`.
2. `application/events.py` (`SessionRuntimeEvent`, `TeamEvent`, the
   `*EventType` Literal aliases) → `domain/events.py`.

Add a minimal `DomainEvent` structural protocol so the target's
`domain.events` slot also exposes the common shape both event dataclasses
already satisfy.

Like Step 01, this step is **pure rehoming with one tiny additive type**:
no class renames, no signature changes, no behavior changes, no port
redesign.

## Current Evidence

### `core/agent.py`

A 50-line dataclass: name, system_prompt, tools, model/provider config,
plus `tool_schemas()` and `find_tool()`. It originally had one
`TYPE_CHECKING` import of `opencollab.tools.base.Tool`; the implementation
must remove that type-only import because the repo's boundary test forbids
domain imports from outer layers even under `TYPE_CHECKING`.

Caller surface (`rg "from opencollab\.core\.agent"`):

- `opencollab/opencollab/bootstrap/container.py:26`
- `opencollab/opencollab/bootstrap/session_factory.py:11`
- `opencollab/opencollab/core/__init__.py:1`
- `opencollab/opencollab/core/session/session.py:8`
- `opencollab/opencollab/harness/evaluator.py:21`
- `opencollab/opencollab/team/orchestrator.py:28`
- `opencollab/opencollab/team/teammate_factory.py:19`

7 production files. No test imports it directly (tests reach it through
`opencollab.core`).

### `application/events.py`

Defines two `(type, data)` dataclasses (`SessionRuntimeEvent`, `TeamEvent`)
plus `SessionEventType` and `TeamEventType` Literal aliases. Module docstring
already promises no imports from core / tools / bootstrap / cli / tui / team
— it is structurally domain code, just filed under `application/`.

Caller surface (`rg "from opencollab\.application\.events"`):

- `opencollab/opencollab/cli/tui.py:22`
- `opencollab/opencollab/core/session/events.py:19, 20`
- `opencollab/opencollab/team/orchestrator.py:22`
- `opencollab/opencollab/tui/session_adapter.py:6`
- `opencollab/tests/test_team_event_emission.py:13`
- `opencollab/tests/test_tui_event_rendering.py:12`

4 production files, 2 tests.

### What stays put

- `core/session/events.py` keeps `EventBus`, `EventSink`, `EventCallback`,
  and the backward-compat aliases `SessionEvent = SessionRuntimeEvent`.
  These are runtime infrastructure, not domain data. They graduate to the
  application layer (or to a `MessageBus` adapter) in a later step.
- `core/events.py` (the existing top-level shim) keeps re-exporting the
  same names through `core.session.events`.

## Target Shape For This Step

```text
opencollab/opencollab/domain/
  __init__.py
  session.py
  tools.py
  compaction.py
  agent.py       # new — moved from core/agent.py
  events.py      # new — moved from application/events.py + DomainEvent
```

`domain/agent.py`: behavior stays identical to today's `core/agent.py`.
Remove the type-only `opencollab.tools.base.Tool` import and use stdlib-only
annotations for the tool list / lookup return. Rewriting the agent's `tools`
field to `list[ToolSpec]` is explicitly deferred (target `domain.agent` slot
lists `ToolSpec`, but introducing it is a separate step).

`domain/events.py`: identical to today's `application/events.py` plus one
new declaration:

```python
from typing import Any, Protocol

class DomainEvent(Protocol):
    """Structural shape every domain event carries: a string type and a data dict."""
    type: str
    data: dict[str, Any]
```

`SessionRuntimeEvent` and `TeamEvent` already satisfy this protocol
structurally — no inheritance change.

`application/events.py`: becomes a thin re-export module so existing
callers (including tests) keep working without touching their imports:

```python
from opencollab.domain.events import (
    DomainEvent,
    SessionEventType,
    SessionRuntimeEvent,
    TeamEvent,
    TeamEventType,
)

__all__ = [...]
```

(The re-export shim is **only** introduced here to keep this step a pure
move. It will be removed in a later step that audits all callers.)

`core/agent.py`: deleted. No shim; the caller surface is small enough to
update atomically. `opencollab.core.Agent` keeps working because
`core/__init__.py` is updated to import from `domain.agent`.

Dependency direction after this step:

```text
domain/*              -> stdlib only
application/events.py -> domain.events (re-export shim)
core/__init__.py      -> domain.agent + core.session
core/session/events.py -> domain.events (was application.events)
```

## Implementation Plan

Execute as two independent sub-moves, each ending in a green test run and
its own commit.

### 1. Move `Agent` into `domain/`

```bash
git mv opencollab/opencollab/core/agent.py opencollab/opencollab/domain/agent.py
```

Rewrite imports in the 7 production sites listed above (`rg
"from opencollab.core.agent"` → `from opencollab.domain.agent`).

Update `opencollab/opencollab/core/__init__.py`: change
`from opencollab.core.agent import Agent` to
`from opencollab.domain.agent import Agent`. Keep the `Agent` re-export so
external callers and tests using `from opencollab.core import Agent`
remain unaffected.

Update `opencollab/opencollab/domain/__init__.py` to export `Agent`:

```python
from opencollab.domain.agent import Agent
```

Verify:

```bash
rg "from opencollab\.core\.agent\b" opencollab/   # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(domain): hoist Agent dataclass`.

### 2. Move event contracts into `domain/`

```bash
git mv opencollab/opencollab/application/events.py opencollab/opencollab/domain/events.py
```

Add the `DomainEvent` Protocol at the top of the moved file. Preserve
existing class definitions verbatim. Append `"DomainEvent"` to `__all__`.

Recreate `opencollab/opencollab/application/events.py` as a re-export
shim:

```python
"""Compatibility shim — event contracts now live in opencollab.domain.events."""
from opencollab.domain.events import (
    DomainEvent,
    SessionEventType,
    SessionRuntimeEvent,
    TeamEvent,
    TeamEventType,
)

__all__ = [
    "DomainEvent",
    "SessionEventType",
    "SessionRuntimeEvent",
    "TeamEvent",
    "TeamEventType",
]
```

Update `opencollab/opencollab/domain/__init__.py` to also export the new
names:

```python
from opencollab.domain.events import DomainEvent, SessionRuntimeEvent, TeamEvent
```

Update **internal-only** callers to point straight at `domain.events`
(skip the shim entirely):

- `opencollab/opencollab/core/session/events.py:19,20` →
  `from opencollab.domain.events import ...`

Leave `cli/tui.py`, `tui/session_adapter.py`, `team/orchestrator.py`, and
the two test files importing through `application.events` for now — they
will be migrated in the step that retires the shim. This intentionally
keeps the diff small.

Verify:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Static checks:

```bash
rg "from opencollab\.core\.agent\b"     opencollab/   # expect 0
rg "^from opencollab\.application\.events" opencollab/opencollab  # only the shim itself
```

Commit: `refactor(domain): hoist event contracts`.

## Acceptance Criteria

- `opencollab/opencollab/domain/` contains `agent.py` and `events.py`.
- `opencollab/opencollab/core/agent.py` no longer exists; no caller
  references it.
- `opencollab/opencollab/application/events.py` exists but contains only a
  re-export shim of the names now defined in `domain/events.py`.
- `DomainEvent` Protocol is declared in `domain/events.py` and exported.
- `from opencollab.core import Agent` and
  `from opencollab.application.events import SessionRuntimeEvent` both
  continue to work (the former through `core/__init__.py`, the latter
  through the shim).
- `core/session/events.py` imports event values from `domain.events`, not
  from `application.events`.
- Full test suite passes.
- No domain module gains an import from `application`, `adapters`,
  `bootstrap`, `core`, `tools`, `cli`, `tui`, `team`, or `harness`.

## Non-Goals

- Do **not** rename `Agent` to `AgentProfile`. The target diagram uses the
  latter name but renaming is a separate, broader refactor.
- Do **not** introduce `ToolSpec` or rewrite `Agent.tools` to use it. Use a
  stdlib-only annotation instead of importing the concrete `Tool` adapter.
- Do **not** retire the `application/events.py` shim. That happens in a
  later step that updates the four remaining production callers + two
  tests.
- Do **not** touch `core/session/events.py`'s `EventBus`, `EventSink`,
  `EventCallback` — they are runtime infrastructure, not domain data, and
  they move (or don't) in a later step.
- Do **not** introduce a `DomainEvent` base class; the Protocol is enough
  to express the structural shape without changing inheritance.
- Do **not** move `core/config.py` — config loading touches env vars and
  filesystem; it belongs in `bootstrap/` or `adapters/`, not `domain/`.
  That is its own step.

## Rollback Plan

Each sub-move is one `git mv` + a small import sweep + one commit. If the
test suite fails after sub-move 1, `git revert` it; sub-move 2 is
independent and can still proceed. If sub-move 2 fails, revert it
without touching sub-move 1.

The only non-`git mv` content is:

- two lines in `domain/__init__.py` (re-exports)
- one Protocol declaration in `domain/events.py`
- five lines in `application/events.py` (the shim)

All four are trivial to back out.
