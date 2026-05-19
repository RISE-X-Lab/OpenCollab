# Step07 - Consolidate `PermissionPolicy` into `PermissionPort`

Date: 2026-05-20
Branch: open `refactor/step07-permission-port` off the merged Step 06 branch.

## Goal

Make `application.ports.PermissionPort` the single canonical Protocol for
"ask the human to confirm a sensitive action." Today the same interface
is declared twice:

- `opencollab/opencollab/application/ports.py:40` — `PermissionPort(Protocol)`
- `opencollab/opencollab/core/session/tools.py:20` — `PermissionPolicy(Protocol)`

Replace the `core/session/tools.py` definition with a one-line alias
`PermissionPolicy = PermissionPort`. Migrate every production caller
outside `core/session/*` to import `PermissionPort` from
`application.ports` directly. Drop the `Transitional:` comments that
document this very state in `team/orchestrator.py`,
`team/teammate_factory.py`, and `tui/session_adapter.py`.

Like Steps 01–06, this is a **pure consolidation**: the alias keeps the
legacy name resolvable, no behavior changes, no signatures change.

## Why this is the next step

Three production modules currently carry a `Transitional:` comment whose
text describes exactly this consolidation:

- `team/orchestrator.py:29-31` — "EventBus / EventSink / PermissionPolicy
  stay sourced from core.session as compatibility re-exports."
- `team/teammate_factory.py:22-24` — same.
- `tui/session_adapter.py:7-9` — "EventSink + PermissionPolicy stay
  sourced from core.session until REM-03/REM-04 narrow the ports further."

`EventSink` graduated in Step 04 (now at `application.event_bus`); the
remaining blocker on those comments is `PermissionPolicy`. After Step 07,
all three comments can be deleted and the imports can point at the
application layer directly.

This also clears the runway for two near-term steps:

- Consolidating `tui/session_adapter.py` + `cli/tui.py` under
  `adapters/tui/` — currently blocked because the TUI inherits the
  `PermissionPolicy` Protocol from `core.session`.
- Eventually retiring `core/session/tools.py:PermissionPolicy` entirely
  once the alias has no external readers (out of scope here).

## Current Evidence

### Identical Protocol bodies

```python
# application/ports.py:40
class PermissionPort(Protocol):
    async def confirm(self, prompt: str) -> bool: ...

# core/session/tools.py:20
class PermissionPolicy(Protocol):
    async def confirm(self, prompt: str) -> bool: ...
```

Both Protocols are static-typing-only (neither is `@runtime_checkable`).
Inheriting from either at runtime gives the same MRO behavior because
non-runtime-checkable Protocols inherit like ordinary abstract classes.

### `CallbackPermissionPolicy` is duck-typed

```python
# core/session/tools.py:25
class CallbackPermissionPolicy:
    def __init__(self, confirm_fn): ...
    async def confirm(self, prompt: str) -> bool: ...
```

No base class. Pure structural conformance. The consolidation does not
touch it.

### Caller surface (`rg "PermissionPolicy\b"`)

Production:

- `opencollab/opencollab/core/session/tools.py:20, 25` — definitions
  (`PermissionPolicy` Protocol + `CallbackPermissionPolicy` concrete)
- `opencollab/opencollab/core/session/__init__.py:9-10, 19, 31` —
  re-exports `CallbackPermissionPolicy` and `PermissionPolicy`
- `opencollab/opencollab/core/session/session.py:13, 52` — imports
  `PermissionPolicy` from `core.session.tools`, uses it as a parameter
  type on `Session.__init__`
- `opencollab/opencollab/bootstrap/runtime.py:15, 26, 35` — imports
  `PermissionPolicy` from `core.session`, uses it on the
  `RuntimeContext` dataclass + `build_runtime_context()` parameter
- `opencollab/opencollab/team/orchestrator.py:29-31, 166` — imports
  via the transitional comment, uses on `Team.__init__`
- `opencollab/opencollab/team/teammate_factory.py:22-24, 41, 141` —
  imports via transitional comment, uses on `TeammateConfig` and
  `build_teammate_session()`
- `opencollab/opencollab/tui/session_adapter.py:7-9, 36` — imports
  `PermissionPolicy`, inherits it on `TuiPermissionPolicy`

Tests:

- Several `FakePermissionPolicy` classes that are pure duck types
  (no import change needed).
- `tests/test_session_characterization.py:11, 424` —
  `CallbackPermissionPolicy` import via `core.session`; stays untouched.

### What stays put and why

- `core/session/__init__.py` continues to re-export `PermissionPolicy`
  (now an alias for `PermissionPort`) and `CallbackPermissionPolicy`.
  The characterization tests rely on `from opencollab.core.session
  import CallbackPermissionPolicy` and assume `PermissionPolicy` is
  importable from `core.session`.
- `CallbackPermissionPolicy` itself stays in
  `core/session/tools.py`. It is an adapter that could move to
  `adapters/permission.py` in a later step, but `test_session_characterization.py`
  pins the import path.

## Target Shape For This Step

`opencollab/opencollab/core/session/tools.py` — replace the Protocol
declaration with an alias:

```python
# was:
# class PermissionPolicy(Protocol):
#     async def confirm(self, prompt: str) -> bool: ...

# becomes:
from opencollab.application.ports import PermissionPort

PermissionPolicy = PermissionPort
"""Legacy alias for PermissionPort.

Production code should import ``PermissionPort`` from
``opencollab.application.ports``. This alias is kept so
``from opencollab.core.session import PermissionPolicy`` continues to
work for legacy callers and characterization tests.
"""
```

The five non-shim production sites switch their imports to:

```python
from opencollab.application.ports import PermissionPort
```

and replace their `PermissionPolicy` type annotations with `PermissionPort`.
`tui/session_adapter.py:36` additionally changes
`class TuiPermissionPolicy(PermissionPolicy):` to
`class TuiPermissionPolicy(PermissionPort):` — at runtime this is the
same class object after the alias, but the spelling matches the imported
name.

Dependency direction after this step:

```text
core/session/tools.py    -> application.ports (PermissionPort)
core/session/session.py  -> application.ports (PermissionPort)
bootstrap/runtime.py     -> application.ports (PermissionPort)
team/orchestrator.py     -> application.ports (PermissionPort)
team/teammate_factory.py -> application.ports (PermissionPort)
tui/session_adapter.py   -> application.ports (PermissionPort)
```

Every production module now talks to the application port directly. The
only remaining `PermissionPolicy` reader is `core/session/__init__.py`,
which re-exports the alias for legacy/test imports.

## Implementation Plan

Single branch, two commits.

### 1. Install the alias

Edit `opencollab/opencollab/core/session/tools.py`:

- Remove the `class PermissionPolicy(Protocol): ...` block (lines ~20–22).
- Add `from opencollab.application.ports import PermissionPort` near the
  other application imports already in the file.
- Add `PermissionPolicy = PermissionPort` with the docstring shown above.

Keep `class CallbackPermissionPolicy:` unchanged. Confirm
`opencollab/opencollab/core/session/__init__.py` still re-exports
`PermissionPolicy` — no edit required because the name still exists in
`core.session.tools`.

Add a one-line pin to `tests/test_session_construction.py` (or wherever
the existing port-structural tests live):

```python
def test_permission_policy_alias_resolves_to_permission_port():
    from opencollab.application.ports import PermissionPort
    from opencollab.core.session.tools import PermissionPolicy

    assert PermissionPolicy is PermissionPort
```

Verify:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(application): alias PermissionPolicy to PermissionPort`.

### 2. Migrate production readers

Five files. Each change is a one-line import swap plus a search-and-replace
on type annotations.

- `opencollab/opencollab/core/session/session.py`
  - Replace `from opencollab.core.session.tools import PermissionPolicy`
    with `from opencollab.application.ports import PermissionPort`.
  - Replace `permission_policy: PermissionPolicy | None` with
    `permission_policy: PermissionPort | None`.

- `opencollab/opencollab/bootstrap/runtime.py`
  - Replace `from opencollab.core.session import EventSink, PermissionPolicy`
    with two imports: keep `EventSink` from `application.event_bus`,
    add `from opencollab.application.ports import PermissionPort`.
  - Replace `PermissionPolicy` annotations with `PermissionPort`.

- `opencollab/opencollab/team/orchestrator.py`
  - Replace `from opencollab.core.session import EventBus, EventSink, PermissionPolicy`
    with `from opencollab.application.event_bus import EventBus, EventSink`
    plus `from opencollab.application.ports import PermissionPort`.
  - Replace the `Transitional:` comment block with a one-line note (or
    delete it).
  - Replace `PermissionPolicy` annotation with `PermissionPort`.

- `opencollab/opencollab/team/teammate_factory.py`
  - Replace `from opencollab.core.session import EventBus, PermissionPolicy, Session`
    with `from opencollab.application.event_bus import EventBus` +
    `from opencollab.application.ports import PermissionPort` and keep
    `from opencollab.core.session import Session` separately.
  - Replace the `Transitional:` comment block with a one-line note.
  - Replace `PermissionPolicy` annotations with `PermissionPort`.

- `opencollab/opencollab/tui/session_adapter.py`
  - Replace `from opencollab.core.session import EventSink, PermissionPolicy`
    with `from opencollab.application.event_bus import EventSink` +
    `from opencollab.application.ports import PermissionPort`.
  - Replace the `Transitional:` comment with a one-line note.
  - Change `class TuiPermissionPolicy(PermissionPolicy):` to
    `class TuiPermissionPolicy(PermissionPort):`.

Verify:

```bash
rg "from opencollab\.core\.session\b.*PermissionPolicy" opencollab/opencollab \
   | rg -v "core/session/tools\.py|core/session/__init__\.py"
# expect: empty — every production reader now imports from application.ports

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(application): point readers at PermissionPort`.

## Acceptance Criteria

- `core/session/tools.py` defines `PermissionPolicy = PermissionPort`
  as an alias and no longer carries a `class PermissionPolicy(Protocol)`
  block.
- `core/session/__init__.py` continues to expose `PermissionPolicy` and
  `CallbackPermissionPolicy`. No change to its `__all__`.
- The five production readers listed above import `PermissionPort` from
  `application.ports`. None of them import `PermissionPolicy` anymore.
- `tui/session_adapter.py:TuiPermissionPolicy` inherits from
  `PermissionPort`.
- The `Transitional:` comments referencing `PermissionPolicy` are gone
  or replaced with a one-line note explaining the alias.
- The new structural test passes.
- `rg "PermissionPolicy\b" opencollab/opencollab` returns matches only in
  `core/session/tools.py`, `core/session/__init__.py`, and the
  `CallbackPermissionPolicy` definition.
- Full test suite passes (163 → 164 with the new pin).

## Non-Goals

- Do **not** move `CallbackPermissionPolicy` to `adapters/`. It is pinned
  in `core/session/` by `test_session_characterization.py`. A future step
  can relocate it.
- Do **not** rename `TuiPermissionPolicy` to `TuiPermissionPort` or
  similar. Class name unchanged.
- Do **not** remove the `PermissionPolicy` alias. Legacy callers and
  characterization tests rely on it. Removal is a future step that also
  rewrites the `core/session/__init__.py` re-exports.
- Do **not** consolidate `tui/session_adapter.py` + `cli/tui.py` under
  `adapters/tui/`. This step is the precondition; the move itself is a
  separate plan.
- Do **not** unify `FakePermissionPolicy` test doubles into a single
  helper. They are deliberately local to each test file.

## Rollback Plan

Two commits, independently revertible.

- Reverting commit 2 puts the five production readers back on
  `PermissionPolicy`; commit 1's alias still satisfies them, so the
  revert is sufficient on its own.
- Reverting commit 1 restores the dedicated Protocol declaration in
  `core/session/tools.py`. Independent of commit 2.

If a Protocol inheritance edge case breaks (extremely unlikely given
both Protocols have identical bodies and neither is
`@runtime_checkable`), drop only the `TuiPermissionPolicy` base-class
change and keep the alias + the four other migrations.
