# Step 04 — Replace `TUI._active_instance` singleton with explicit injection

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 4 of 7**: kill the `TUI._active_instance` class-var singleton,
move suspend/resume choreography into `TuiPermissionPolicy`, and make
`bootstrap` UI-agnostic.

---

## Goal

Today the "ask the user a yes/no question" flow is split across three
modules with an implicit contract:

1. `cli/tui.py:41` stashes `TUI._active_instance = self` in the constructor.
2. `cli/main.py:_confirm_prompt` retrieves it via `TUI.get_active()`,
   suspends Live through duck-typed `_safe_suspend_live`/`_safe_resume_live`
   helpers, reads a line, and parses y/N.
3. `tui/session_adapter.py:TuiPermissionPolicy` wraps `_confirm_prompt` —
   but has no awareness of the render at all; it's a thin adapter for a
   callable.

Problems:

- **Global singleton.** Constructing a second `TUI` (e.g., in
  `_print_missing_key_hint` at `cli/main.py:84`) silently replaces the
  active instance for the whole process. Tests can't run two TUIs.
- **Hidden dependency.** `_confirm_prompt` reaches across module boundaries
  via a class variable. Static analysis sees no edge.
- **Duck typing as a workaround.** `_safe_suspend_live` /
  `_safe_resume_live` exist because the type of `TUI.get_active()` was
  effectively `Any`. The protocol between core, adapter, and UI is
  implicit.
- **Bootstrap depends on UI.** `bootstrap/runtime.py:19` imports
  `TuiPermissionPolicy` to wrap a `confirm_fn`. Bootstrap should be
  UI-agnostic — the import direction `bootstrap → tui` is wrong.

After this PR:

- `TuiPermissionPolicy` takes the render *explicitly* and owns the whole
  suspend → read → parse → resume choreography.
- The `TUI._active_instance` class-var, `TUI.get_active()` classmethod,
  `_safe_suspend_live`, `_safe_resume_live`, and `_confirm_prompt` are
  deleted.
- `build_runtime_context` accepts a pre-built `permission_policy` and
  knows nothing about `--yolo` or `confirm_fn`. The yolo decision moves
  to the CLI.

---

## Concrete edits

### 1. `opencollab/tui/session_adapter.py`

**Add a `SuspendableRender` Protocol** and rewrite `TuiPermissionPolicy`
to take the render directly and own the choreography:

```python
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol

from opencollab.core.session import EventSink, PermissionPolicy, SessionEvent


class SuspendableRender(Protocol):
    """A render target whose live output can be paused for user input."""

    def suspend_live(self) -> bool: ...
    def resume_live(self, was_suspended: bool) -> None: ...


class TuiEventSink(EventSink):
    def __init__(self, tui):
        self.tui = tui

    async def emit(self, event: SessionEvent) -> None:
        result = self.tui.event_handler(event)
        if asyncio.iscoroutine(result):
            await result


class TuiPermissionPolicy(PermissionPolicy):
    """Permission policy that pauses a live render around the y/N prompt."""

    def __init__(
        self,
        render: SuspendableRender,
        read_line: Callable[[str], Awaitable[str]],
    ):
        self._render = render
        self._read_line = read_line

    async def confirm(self, prompt: str) -> bool:
        was_suspended = self._render.suspend_live()
        try:
            answer = await self._read_line(f"{prompt} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            return False
        finally:
            self._render.resume_live(was_suspended)
        return answer.strip().lower() in ("y", "yes")
```

The structural-typing here means `TUI` satisfies `SuspendableRender`
automatically (it already has both methods with the right signatures).
No `TUI`-specific import is needed.

### 2. `opencollab/cli/tui.py`

**Delete the singleton plumbing:**

- Line 41: `TUI._active_instance = self`  → remove.
- Line 43: `_active_instance: "TUI | None" = None` (class var) → remove.
- Lines 45–48: `@classmethod get_active(cls)` → remove.

Keep `suspend_live` and `resume_live` exactly as they are — they already
satisfy the `SuspendableRender` Protocol.

### 3. `opencollab/cli/main.py`

**Delete:**

- `_safe_suspend_live` (lines 50–54).
- `_safe_resume_live` (lines 57–60).
- `_confirm_prompt` (lines 343–356).

**`_read_command` (lines 179–187)** — replace duck-typed helpers with
direct calls on `tui`:

```python
async def _read_command(tui: TUI) -> str | None:
    """Prompt for a user line, returning None on EOF/interrupt."""
    was_suspended = tui.suspend_live()
    try:
        return await _read_line("> ")
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        tui.resume_live(was_suspended)
```

(Live is normally not active here — `turn()` calls `start_live`/`stop_live`
internally — so this is defensive. Keep it.)

**`_chat` and `_team`** — build the permission policy locally and pass it
to bootstrap:

```python
async def _chat(workspace, cfg, session_file, trace, yolo):
    from opencollab.bootstrap import build_chat_session, build_runtime_context
    from opencollab.cli.tui import TUI
    from opencollab.tui.session_adapter import TuiEventSink, TuiPermissionPolicy

    tui = TUI(console)
    tui.print_welcome()

    permission_policy = None
    if not yolo:
        permission_policy = TuiPermissionPolicy(render=tui, read_line=_read_line)

    ctx = build_runtime_context(
        workspace, cfg, trace=trace,
        event_sink=TuiEventSink(tui),
        permission_policy=permission_policy,
    )
    ...
```

Same pattern in `_team` (with `run_id_prefix="team-"`).

### 4. `opencollab/bootstrap/runtime.py`

**Simplify `build_runtime_context` signature:**

```python
# BEFORE
def build_runtime_context(
    workspace: str,
    cli_overrides: dict,
    *,
    trace: bool,
    yolo: bool,
    event_sink: EventSink | None = None,
    confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    run_id_prefix: str = "",
) -> RuntimeContext: ...

# AFTER
def build_runtime_context(
    workspace: str,
    cli_overrides: dict,
    *,
    trace: bool,
    event_sink: EventSink | None = None,
    permission_policy: PermissionPolicy | None = None,
    run_id_prefix: str = "",
) -> RuntimeContext: ...
```

**Remove from the body:**

- The `yolo` branch.
- The `TuiPermissionPolicy(confirm_fn)` construction.
- The `from opencollab.tui.session_adapter import TuiPermissionPolicy` import.

**Body becomes:**

```python
abs_workspace = os.path.abspath(workspace)
interceptor = SandboxInterceptor(abs_workspace)
tracer = Tracer(run_id=f"{run_id_prefix}{uuid.uuid4().hex[:8]}") if trace else None
repo_map = get_repo_map(abs_workspace)

return RuntimeContext(
    workspace=abs_workspace,
    config=dict(cli_overrides),
    tracer=tracer,
    repo_map=repo_map,
    interceptor=interceptor,
    event_sink=event_sink,
    permission_policy=permission_policy,
)
```

After this change `opencollab.bootstrap` no longer imports anything from
`opencollab.tui`. Verify with:

```bash
grep -rn "opencollab.tui\|opencollab.cli" opencollab/opencollab/bootstrap/
```

Expected: zero matches.

### 5. `opencollab/tests/test_bootstrap.py`

The two existing tests call `build_runtime_context(..., yolo=True)`. After
the signature change, drop the `yolo=True` argument (defaults give the same
behavior — no permission policy).

```python
# BEFORE
ctx = build_runtime_context(str(workspace), _cfg(), trace=False, yolo=True)
# AFTER
ctx = build_runtime_context(str(workspace), _cfg(), trace=False)
```

Both test functions need this update. No other test edits expected.

---

## New test

Add `opencollab/tests/test_session_adapter.py` with one test for the new
`TuiPermissionPolicy.confirm` choreography:

```python
import pytest

from opencollab.tui.session_adapter import TuiPermissionPolicy


class FakeRender:
    def __init__(self):
        self.events = []

    def suspend_live(self) -> bool:
        self.events.append("suspend")
        return True

    def resume_live(self, was_suspended: bool) -> None:
        self.events.append(("resume", was_suspended))


@pytest.mark.asyncio
async def test_tui_permission_policy_suspends_resumes_and_parses_yes():
    render = FakeRender()
    answers = iter(["yes\n"])

    async def fake_read(prompt: str) -> str:
        assert prompt.endswith("[y/N] ")
        return next(answers)

    policy = TuiPermissionPolicy(render=render, read_line=fake_read)
    assert await policy.confirm("Allow?") is True
    assert render.events == ["suspend", ("resume", True)]


@pytest.mark.asyncio
async def test_tui_permission_policy_parses_no_and_eof():
    render = FakeRender()

    async def fake_read_n(prompt: str) -> str:
        return "n"

    async def fake_read_eof(prompt: str) -> str:
        raise EOFError

    policy_n = TuiPermissionPolicy(render=render, read_line=fake_read_n)
    assert await policy_n.confirm("Allow?") is False

    render2 = FakeRender()
    policy_eof = TuiPermissionPolicy(render=render2, read_line=fake_read_eof)
    assert await policy_eof.confirm("Allow?") is False
    # Resume must still fire on EOF
    assert any(e == ("resume", True) for e in render2.events)
```

If `pytest-asyncio` isn't already a test dep, drop `@pytest.mark.asyncio`
and run via `asyncio.run(...)` in a sync test instead — match the existing
pattern used in `test_session_characterization.py` (which uses a synchronous
`run(...)` helper).

---

## What is NOT in this PR

- `SandboxInterceptor` unification — step 5.
- `Team` decomposition — step 6.
- `auto_save` event subscriber — step 7.
- Moving `_read_line` into the TUI as a `tui.read_line(prompt)` method.
  Tempting, but adds API without removing duplication; defer until something
  forces it.
- Removing the defensive `suspend_live`/`resume_live` around `_read_command`.
  Live is normally already stopped at REPL idle, so the calls are no-ops in
  practice, but they're cheap defense against future regressions.

---

## Verification

Before finishing, run these greps — all must return zero matches:

```bash
grep -rn "_active_instance\|get_active" opencollab/opencollab
grep -rn "_safe_suspend_live\|_safe_resume_live\|_confirm_prompt" opencollab/opencollab
grep -rn "TuiPermissionPolicy" opencollab/opencollab/bootstrap
grep -rn "opencollab.tui\|opencollab.cli" opencollab/opencollab/bootstrap
```

---

## Acceptance checklist

- [ ] `TUI._active_instance` class var and `TUI.get_active()` classmethod
      are gone from `cli/tui.py`.
- [ ] `cli/main.py` no longer defines `_safe_suspend_live`,
      `_safe_resume_live`, or `_confirm_prompt`.
- [ ] `TuiPermissionPolicy.__init__` takes `(render, read_line)`; its
      `.confirm` calls `suspend_live` / `resume_live` and parses y/N.
- [ ] `cli/main.py` constructs `TuiPermissionPolicy(render=tui,
      read_line=_read_line)` in `_chat` and `_team`; passes it as
      `permission_policy=` to `build_runtime_context`.
- [ ] `build_runtime_context` no longer accepts `yolo` or `confirm_fn`;
      accepts `permission_policy` instead.
- [ ] `bootstrap/` imports nothing from `opencollab.tui` or
      `opencollab.cli`.
- [ ] `tests/test_bootstrap.py` updated (drop `yolo=True`).
- [ ] `tests/test_session_adapter.py` added with at least one test for the
      suspend/resume + y/N parsing behavior.
- [ ] Full test suite passes (`pytest tests/ -q` → all green) with
      `OPENAI_API_KEY=fake-test-key`.
- [ ] `python -m opencollab chat` and `python -m opencollab team "<task>"`
      smoke-test still work end-to-end. The y/N prompt for a risky command
      (e.g., `rm -rf ./scratch`) still pauses the spinner cleanly.

---

## Risk & rollback

- **Breaking change to `build_runtime_context`** (removes `yolo` and
  `confirm_fn` parameters, adds `permission_policy`). Only callers are
  `cli/main.py` and `tests/test_bootstrap.py`, both updated in the same PR.
- **Singleton removal** is observable to any code that called
  `TUI.get_active()`. In-repo grep finds only `_confirm_prompt` as a
  caller, which is also removed.
- **Subtle risk**: if a future test or external caller constructs `TUI`
  expecting the singleton side-effect, it now silently no-ops. This is the
  intended behavior — if someone wants the active TUI, they should be
  passed it.
- Rollback: single `git revert`.

Human-readable summary for the PR description: *"Replace the
`TUI._active_instance` singleton with explicit `SuspendableRender`
injection. `TuiPermissionPolicy` now owns the suspend/resume/y-or-N
choreography; bootstrap is UI-agnostic."*
