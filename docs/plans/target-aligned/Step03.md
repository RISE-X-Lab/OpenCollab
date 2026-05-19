# Step03 - Consolidate small leftovers

Date: 2026-05-19
Branch: open `refactor/step03-config-and-events-shim` off the merged Step 02 branch.

## Goal

Bundle two independent, pure-mechanical moves that have been sitting as
loose ends since Steps 01–02:

1. **`core/config.py` → `bootstrap/config.py`** — fills the target diagram's
   `bootstrap.config` slot. The config module is pydantic-backed env loading;
   it is composition-root work, not domain or application policy.
2. **Retire `application/events.py` shim** — update the five remaining
   callers to import event contracts straight from `opencollab.domain.events`
   and delete the shim file. The shim was introduced by Step 02 to keep that
   step a pure move; Step 03 closes it.

Neither move changes behavior, signatures, or class names. Both are
verified by `rg` going to zero matches plus a green test run.

## Why these two together

- They touch disjoint files. Failure on one does not affect the other.
- Each is small (1 caller for config; 5 callers for events).
- Bundling avoids opening two micro-branches for ~10 minutes of work each.

## Why **not** these other candidates

- `core/events.py` (top-level event shim) is held in place by
  `tests/test_session_characterization.py:139–140`, which asserts
  `CompatEventBus is EventBus` and `CompatSessionEvent is SessionEvent`.
  Removing it would require removing or rewriting the characterization
  test, which is out of scope for a tidy-up.
- `core/session/runner.py` looks like a candidate for promotion to
  `application/session_state_machine.py` (target's `app.session_runner`),
  but it still imports `EventBus`, `ContextCompactor`, and
  `ToolCallProcessor` from `core/session/*` as runtime types. Moving it
  before `EventBus` graduates would create an application→core arrow.
  Defer to Step 04 (which will need to migrate `EventBus` first).
- Introducing `domain.tool.ToolSpec` to tighten `Agent.tools: list[Any]`
  is appealing but doesn't unblock anything; defer until the team / tool
  reshape settles, when call sites will tell us what fields ToolSpec
  needs.

## Current Evidence

### `core/config.py`

Pydantic `BaseModel` (`OpenCollabConfig`) plus env-file parsing. Imports
`pydantic` (external SDK) and `pathlib`. Single in-repo caller:

```
opencollab/opencollab/cli/main.py:    from opencollab.core.config import get_config
```

The import lives inside a function (deferred import), so the change is
local.

### `application/events.py` (the shim)

Five remaining callers (`rg "from opencollab\.application\.events"`):

- `opencollab/opencollab/cli/tui.py`
- `opencollab/opencollab/tui/session_adapter.py`
- `opencollab/opencollab/team/orchestrator.py`
- `opencollab/tests/test_team_event_emission.py`
- `opencollab/tests/test_tui_event_rendering.py`

The shim file itself is the only thing in
`opencollab/opencollab/application/events.py`:

```python
"""Compatibility shim; event contracts now live in opencollab.domain.events."""
from opencollab.domain.events import (DomainEvent, SessionEventType,
    SessionRuntimeEvent, TeamEvent, TeamEventType)
__all__ = [...]
```

`application/__init__.py` does not re-export anything from
`application.events` — verified with `rg "from opencollab\.application\.events" opencollab/opencollab/application/__init__.py` (no match).

## Target Shape For This Step

After Step 03:

```text
opencollab/opencollab/bootstrap/
  __init__.py
  config.py        # new — moved from core/config.py
  container.py
  runtime.py
  safety.py
  session_factory.py
  tool_factory.py

opencollab/opencollab/core/
  __init__.py
  events.py        # untouched; characterization-pinned compat shim
  session/         # untouched in this step

opencollab/opencollab/application/
  __init__.py
  compaction.py
  ports.py
  session_run.py
  tool_dispatch.py
  tool_execution.py
  tool_runtime.py
  # events.py deleted
```

Dependency direction after this step:

```text
bootstrap/config.py -> pydantic (external)
cli/main.py         -> bootstrap.config (was core.config)
cli/tui.py          -> domain.events (was application.events)
tui/session_adapter -> domain.events (was application.events)
team/orchestrator   -> domain.events (was application.events)
tests/*             -> domain.events (was application.events)
```

## Implementation Plan

Two independent sub-moves. Run the test suite and commit between them.

### 1. Move `core/config.py` into `bootstrap/`

```bash
git mv opencollab/opencollab/core/config.py opencollab/opencollab/bootstrap/config.py
```

Rewrite the one in-repo caller:

- `opencollab/opencollab/cli/main.py`: change
  `from opencollab.core.config import get_config` →
  `from opencollab.bootstrap.config import get_config`.

Update `opencollab/opencollab/core/__init__.py` only if it re-exports
config symbols (it currently does not — verified: it only exports `Agent`
and `Session`).

Update `opencollab/opencollab/bootstrap/__init__.py` if it should expose
`get_config` / `OpenCollabConfig` for callers that import via
`from opencollab.bootstrap import get_config`. Optional — keep parity
with whatever `bootstrap/__init__.py` exposes today.

Verify:

```bash
rg "from opencollab\.core\.config\b" opencollab/   # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(bootstrap): hoist config loader`.

### 2. Retire the `application/events.py` shim

Rewrite imports in the five callers:

- `opencollab/opencollab/cli/tui.py`
- `opencollab/opencollab/tui/session_adapter.py`
- `opencollab/opencollab/team/orchestrator.py`
- `opencollab/tests/test_team_event_emission.py`
- `opencollab/tests/test_tui_event_rendering.py`

Change every `from opencollab.application.events import ...` to
`from opencollab.domain.events import ...`. The imported names
(`SessionRuntimeEvent`, `TeamEvent`, `DomainEvent`, `SessionEventType`,
`TeamEventType`) are identical objects in both locations after Step 02.

Delete the shim file:

```bash
git rm opencollab/opencollab/application/events.py
```

Verify:

```bash
rg "from opencollab\.application\.events" opencollab/    # expect 0
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(application): retire events shim`.

## Acceptance Criteria

- `opencollab/opencollab/bootstrap/config.py` exists; `opencollab/opencollab/core/config.py` does not.
- `opencollab/opencollab/application/events.py` does not exist.
- Both `rg` checks above return zero matches.
- Full test suite passes (160 tests after Step 02; expect 160 here too).
- No module under `opencollab/opencollab/domain/` or `opencollab/opencollab/application/` gains a new import.
- No new shims, no class renames, no signature changes.

## Non-Goals

- Do **not** touch `core/events.py` — it is pinned by
  `test_session_characterization.py`.
- Do **not** promote `core/session/runner.py` to `application/` — Step 04.
- Do **not** introduce `ToolSpec` or re-tighten `Agent.tools` typing.
- Do **not** move `core/session/*` modules.
- Do **not** retire `bootstrap/__init__.py` re-exports if any exist —
  they belong to the composition-root contract and should change with
  a deliberate plan.

## Rollback Plan

Each sub-move is one or two file operations + a small import sweep + one
commit. If sub-move 1 fails, revert it and proceed to sub-move 2
independently. If sub-move 2 fails, revert it; sub-move 1 stands on its
own.
