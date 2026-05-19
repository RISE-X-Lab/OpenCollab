# Step06 - Promote `AutoSaveSubscriber` to the application layer

Date: 2026-05-20
Branch: open `refactor/step06-autosave` off the merged Step 05 branch.

## Goal

Move `opencollab/opencollab/core/session/autosave.py` to
`opencollab/opencollab/application/autosave.py`. The class is an
event-driven persistence *policy* — it depends only on application
abstractions (`EventSink`, `SessionEvent`) and a caller-supplied
`save_fn` — so it belongs next to the other application use cases.

While we're touching `core/session/__init__.py`, delete the dead
`SessionMachine = SessionRunner` alias: it is defined, re-exported in
`__all__`, and imported by nobody (`rg "SessionMachine"` returns only
the definition site).

Like Steps 01–05, this is **pure relocation + dead-code removal**: no
behavior change, no signature change, no class rename.

## Current Evidence

### `AutoSaveSubscriber` is application-shaped

`opencollab/opencollab/core/session/autosave.py` (38 lines):

```python
from opencollab.application.event_bus import EventSink
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent

SAVE_TRIGGERS = frozenset({
    "user_message_appended",
    "compaction_applied",
    "step_end",
})

class AutoSaveSubscriber(EventSink):
    def __init__(self, save_fn: Callable[[], None]):
        self._save = save_fn

    async def emit(self, event: SessionEvent) -> None:
        if event.type not in SAVE_TRIGGERS: return
        try:
            self._save()
        except Exception as exc:
            logging.getLogger(__name__).debug("auto-save failed: %s", exc)
```

Observations:

- Imports are already inward (`application.event_bus`, `domain.events`).
  Moving up to `application/` does not introduce any new outer arrow.
- The class encapsulates a *policy* (which event types trigger a save)
  expressed in domain vocabulary. That is application work.
- It is dependency-inverted: it takes a `save_fn`, never a `SessionStore`.
  Composition happens in `bootstrap/container.py:96`.

### Caller surface

`rg "AutoSaveSubscriber\b"` excluding the definition file:

Production (2):
- `opencollab/opencollab/bootstrap/container.py:30, 96`
- `opencollab/opencollab/core/session/session.py:11, 34 (comment), 176-180`

Tests (2):
- `opencollab/tests/test_autosave_subscriber.py:10`
- `opencollab/tests/test_session_construction.py:14, 91, 97, 130`

No external code or downstream package imports it. No re-export through
`core/session/__init__.py` (verified: `AutoSaveSubscriber` is not in the
`__init__`'s `__all__`).

### Dead alias

`opencollab/opencollab/core/session/__init__.py`:

```python
SessionRunner  # imported above
...
SessionMachine = SessionRunner   # line 17
...
__all__ = [
    ...
    "SessionMachine",     # line 36
    ...
]
```

`rg "SessionMachine"` returns exactly these two lines and nothing else.
Unused since it was introduced. Safe to delete.

## Target Shape For This Step

```text
opencollab/opencollab/application/
  __init__.py
  autosave.py          # new — moved from core/session/autosave.py
  compaction.py
  event_bus.py
  ports.py
  session_run.py
  tool_dispatch.py
  tool_execution.py
  tool_runtime.py

opencollab/opencollab/core/session/
  __init__.py          # SessionMachine alias deleted
  events.py            # untouched
  state.py             # untouched
  runner.py            # untouched
  session.py           # imports updated
  compactor.py         # untouched
  tools.py             # untouched
  # autosave.py removed
```

Dependency direction after this step (unchanged in shape, just
relocated):

```text
application/autosave.py -> application.event_bus + domain.events
bootstrap/container.py  -> application.autosave (was core.session.autosave)
core/session/session.py -> application.autosave (was core.session.autosave)
```

## Implementation Plan

Single branch, two commits.

### 1. Move `AutoSaveSubscriber` to the application layer

```bash
git mv opencollab/opencollab/core/session/autosave.py \
       opencollab/opencollab/application/autosave.py
```

The file body needs no edits — its imports already point at
`application.event_bus` and `domain.events`.

Rewrite the four callers:

- `opencollab/opencollab/bootstrap/container.py:30`
- `opencollab/opencollab/core/session/session.py:11`
- `opencollab/tests/test_autosave_subscriber.py:10`
- `opencollab/tests/test_session_construction.py:14`

Each is a single-line change:
`from opencollab.core.session.autosave import ...` →
`from opencollab.application.autosave import ...`.

Update the docstring comment in
`opencollab/opencollab/core/session/session.py:34` if it still mentions
the old path (it currently lists `AutoSaveSubscriber` among classes
"living in this module" — adjust to reflect the new home).

Optionally export from `opencollab/opencollab/application/__init__.py`
to follow the file's existing re-export pattern (the file already
re-exports `event_bus` symbols and other application surface — verify
and add `AutoSaveSubscriber` + `SAVE_TRIGGERS` if the pattern fits).

Verify:

```bash
rg "from opencollab\.core\.session\.autosave" opencollab/  # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(application): hoist autosave subscriber`.

### 2. Delete the dead `SessionMachine` alias

Edit `opencollab/opencollab/core/session/__init__.py`:

- Remove `SessionMachine = SessionRunner` (line 17).
- Remove `"SessionMachine"` from `__all__` (line 36).

No callers exist; this is pure dead-code removal.

Verify:

```bash
rg "SessionMachine" opencollab/   # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `chore(core): drop unused SessionMachine alias`.

## Acceptance Criteria

- `opencollab/opencollab/application/autosave.py` exists.
- `opencollab/opencollab/core/session/autosave.py` does not exist.
- All four callers import from `opencollab.application.autosave`.
- `rg "from opencollab\.core\.session\.autosave"` returns zero matches.
- `rg "SessionMachine"` returns zero matches.
- No file under `opencollab/opencollab/domain/` gains a new import.
- No new outward-pointing arrow is introduced anywhere.
- Full test suite passes (163 before → 163 expected, no test count
  change).

## Non-Goals

- Do **not** consolidate `tui/session_adapter.py` and `cli/tui.py` under
  `adapters/tui/`. That move is blocked on the
  `PermissionPolicy` / `PermissionPort` consolidation and rates its own
  step.
- Do **not** dissolve `ContextCompactor` / `ToolCallProcessor` /
  `SessionRunner`. Characterization tests pin their public attribute
  surface on `Session` — substantive refactor.
- Do **not** retire `core/session/state.py`, `core/session/events.py`,
  or the `core/session/compactor.py` / `core/session/tools.py`
  re-exports. They are pinned by
  `test_legacy_core_session_modules_reexport_domain_value_objects`.
- Do **not** rename `AutoSaveSubscriber` or `SAVE_TRIGGERS`.
- Do **not** change `SAVE_TRIGGERS` membership. The triggers are tied
  to the event type Literal at `domain/events.py:SessionEventType`;
  altering them is a behavior change.

## Rollback Plan

Two commits, independently revertible.

- Reverting commit 2 restores the `SessionMachine` alias. Nothing depends
  on it, so this is safe.
- Reverting commit 1 puts `AutoSaveSubscriber` back under `core/session/`
  and restores the four import sites. Independent of commit 2.

If only the optional `application/__init__.py` re-export causes a
problem (unlikely but possible if re-export order matters), drop that
single line rather than reverting the move.
