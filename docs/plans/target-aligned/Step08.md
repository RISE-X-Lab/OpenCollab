# Step08 - Consolidate the TUI under `adapters/tui/`

Date: 2026-05-20
Branch: `refactor/step08-adapters-tui` off the Step 07 branch.

## Goal

Collapse the two scattered terminal-UI modules into one adapter package:

- `opencollab/opencollab/cli/tui.py` (the Rich `TUI` renderer)
- `opencollab/opencollab/tui/session_adapter.py` (`TuiEventSink`,
  `TuiPermissionPolicy`, `SuspendableRender`)

into `opencollab/opencollab/adapters/tui/`. This fills the target
diagram's `adapters.tui` slot (`SessionEventView / TeamEventView /
PermissionPrompt`) and removes the orphan top-level `tui/` package plus
the misplaced `cli/tui.py` (UI rendering does not belong inside the CLI
command module).

Step 07 was the precondition: after `PermissionPolicy` became an alias
for `application.ports.PermissionPort`, both TUI files import only
inward (`application.event_bus`, `application.ports`, `domain.events`,
plus `rich`). Neither imports `core.session` anymore, so the relocation
is a pure move.

Like prior steps, this is **relocation only**: no class renames, no
signature changes, no behavior changes.

## Current Evidence

### The two files and their imports

`cli/tui.py` — `class TUI`:
```
import rich.{console,live,markdown,panel,spinner,text,table}
from opencollab.domain.events import SessionRuntimeEvent, TeamEvent
```

`tui/session_adapter.py` — `TuiEventSink`, `TuiPermissionPolicy`,
`SuspendableRender`:
```
from opencollab.application.event_bus import EventSink
from opencollab.application.ports import PermissionPort
from opencollab.domain.events import SessionRuntimeEvent, TeamEvent
```

Neither file imports the other. `TuiPermissionPolicy` accepts its render
target through the `SuspendableRender` Protocol (duck-typed), not via an
import of `TUI`. So the two modules are independent and can sit side by
side without a cross-import.

### Consumers

- `opencollab/opencollab/cli/main.py` — three deferred imports of
  `cli.tui.TUI` (lines 69, 196, 251) and two of
  `tui.session_adapter.{TuiEventSink, TuiPermissionPolicy}`
  (lines 197, 252). All are function-local imports.
- `opencollab/opencollab/tui/__init__.py` — re-exports
  `TuiEventSink`, `TuiPermissionPolicy`.
- `opencollab/tests/test_session_adapter.py:3` — imports
  `tui.session_adapter.TuiPermissionPolicy`.
- `opencollab/tests/test_tui_event_rendering.py:13` — imports
  `cli.tui.TUI`.

No other package imports either module.

## Target Shape For This Step

```text
opencollab/opencollab/adapters/tui/
  __init__.py          # re-exports TUI, TuiEventSink, TuiPermissionPolicy, SuspendableRender
  renderer.py          # the TUI class (moved from cli/tui.py)
  session_adapter.py   # TuiEventSink, TuiPermissionPolicy, SuspendableRender (moved from tui/session_adapter.py)
```

Removed:
- `opencollab/opencollab/cli/tui.py`
- `opencollab/opencollab/tui/` (whole package: `__init__.py`,
  `session_adapter.py`)

`adapters/tui/__init__.py`:
```python
"""Terminal UI adapter: renders runtime/team events, prompts for permission."""
from opencollab.adapters.tui.renderer import TUI
from opencollab.adapters.tui.session_adapter import (
    SuspendableRender,
    TuiEventSink,
    TuiPermissionPolicy,
)

__all__ = ["TUI", "TuiEventSink", "TuiPermissionPolicy", "SuspendableRender"]
```

Dependency direction after this step:
```text
adapters/tui/renderer.py        -> rich + domain.events
adapters/tui/session_adapter.py -> application.event_bus + application.ports + domain.events
cli/main.py                     -> adapters.tui (was cli.tui + tui.session_adapter)
```

## Implementation Plan

Single branch, one commit (the move is atomic; splitting it leaves a
half-wired import graph).

1. Create the package and move both files:
   ```bash
   mkdir -p opencollab/opencollab/adapters/tui
   git mv opencollab/opencollab/cli/tui.py \
          opencollab/opencollab/adapters/tui/renderer.py
   git mv opencollab/opencollab/tui/session_adapter.py \
          opencollab/opencollab/adapters/tui/session_adapter.py
   git rm opencollab/opencollab/tui/__init__.py
   ```
2. Write `opencollab/opencollab/adapters/tui/__init__.py` (content above).
3. Update the docstring cross-reference in
   `adapters/tui/session_adapter.py` that points at
   `cli.tui.TUI.event_handler` → `adapters.tui.renderer.TUI.event_handler`.
4. Rewrite consumer imports:
   - `cli/main.py`: `from opencollab.cli.tui import TUI` →
     `from opencollab.adapters.tui import TUI` (three sites); and
     `from opencollab.tui.session_adapter import TuiEventSink, TuiPermissionPolicy`
     → `from opencollab.adapters.tui import TuiEventSink, TuiPermissionPolicy`
     (two sites). These can collapse to a single
     `from opencollab.adapters.tui import TUI, TuiEventSink, TuiPermissionPolicy`
     per function.
   - `tests/test_session_adapter.py`:
     `from opencollab.tui.session_adapter import TuiPermissionPolicy` →
     `from opencollab.adapters.tui import TuiPermissionPolicy`.
   - `tests/test_tui_event_rendering.py`:
     `from opencollab.cli.tui import TUI` →
     `from opencollab.adapters.tui import TUI`.
5. Verify the now-empty `tui/` directory is fully removed (no
   `__pycache__` left tracked).

Verify:
```bash
rg "from opencollab\.cli\.tui|from opencollab\.tui\b|opencollab\.tui\." opencollab/  # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(adapters): consolidate TUI under adapters.tui`.

## Acceptance Criteria

- `opencollab/opencollab/adapters/tui/` contains `__init__.py`,
  `renderer.py`, `session_adapter.py`.
- `opencollab/opencollab/cli/tui.py` and the top-level
  `opencollab/opencollab/tui/` package no longer exist.
- `rg "opencollab\.cli\.tui|opencollab\.tui\."` returns zero matches
  outside `adapters/tui/`.
- `cli/main.py` and both test files import the TUI symbols from
  `opencollab.adapters.tui`.
- No new outward-pointing arrow: `adapters/tui/*` imports only
  `application.*`, `domain.*`, and third-party `rich`.
- Full test suite passes (expect 164, no test-count change).

## Non-Goals

- Do **not** rename `TUI`, `TuiEventSink`, `TuiPermissionPolicy`, or
  `SuspendableRender` to the target diagram's `SessionEventView /
  TeamEventView / PermissionPrompt` names. Class renames are a separate,
  optional cosmetic step.
- Do **not** split the single `TUI` class into separate session/team
  view classes. The diagram lists them as a target *concept*; the code
  already routes both event families through one `event_handler`.
- Do **not** touch `cli/main.py` logic beyond the import lines.
- Do **not** move `CallbackPermissionPolicy` (still in
  `core/session/tools.py`, pinned by characterization tests).

## Rollback Plan

Single commit. If the suite fails, `git revert` it; the move is atomic
so there is no partial state to untangle. The most likely failure is a
missed import site — `rg "opencollab\.tui\b|opencollab\.cli\.tui"` will
locate it before reverting.
