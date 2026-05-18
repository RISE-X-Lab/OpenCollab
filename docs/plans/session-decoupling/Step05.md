# Step 05 — Unify `SandboxInterceptor` ownership inside `ToolCallProcessor`

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 5 of 7**: move sandbox/interceptor responsibility out of
individual `Tool` instances and out of `bootstrap` / `Team` / `harness`,
and into the runtime layer (`ToolCallProcessor`) that already owns `env`
and `permission_policy`.

---

## Goal

Today path/command policy is enforced in **two places**, with **four
construction sites**, and **four tool classes** each carry their own
`self._interceptor` field. The most visible symptom:

```python
# tools/fs.py — duplicate path-jail logic
def _resolve_env_scoped_path(path: str, env: Environment | None) -> str: ...

# tools/fs.py — every fs tool
if self._interceptor:
    path = self._interceptor.check_path(path)
elif env:
    path = _resolve_env_scoped_path(path, env)
```

Construction sites today:

1. `bootstrap/runtime.py:41` — `SandboxInterceptor(abs_workspace)` for the
   main workspace.
2. `team/orchestrator.py:177` — `SandboxInterceptor(workspace)` for Team's
   Lead.
3. `team/orchestrator.py:270` — `SandboxInterceptor(env.workspace)` for
   each teammate (against its worktree).
4. `harness/evaluator.py:96` — `SandboxInterceptor(env.workspace) if
   isinstance(env, LocalEnvironment) else None` for eval.

The interceptor is derivable from a single fact: `env.workspace`. Every
construction site re-derives it. Tools then receive it through their
constructor and use it (or fall back to a partial reimplementation in
`_resolve_env_scoped_path`).

After this PR:

- **`ToolCallProcessor` owns the interceptor**, builds it from
  `env.workspace`, and passes it to each `tool.execute(...)` call alongside
  `env` and `confirm_fn` (which it already injects).
- **Tools stop holding interceptors.** No `Tool.__init__(interceptor=...)`,
  no `self._interceptor` field, no `_resolve_env_scoped_path` fallback.
- **`bootstrap.RuntimeContext` drops the `interceptor` field**, and
  `build_default_tools` drops its `interceptor` parameter.
- **`Team` and `harness/evaluator` stop constructing interceptors entirely.**
  Per-teammate isolation still works because `ToolCallProcessor` derives
  its interceptor from the teammate session's `env.workspace`, which is
  the worktree root.

---

## Concrete edits

### 1. `opencollab/tools/base.py`

Extend `Tool.execute` signature to accept `interceptor`:

```python
async def execute(
    self,
    params: dict[str, Any],
    env: Environment | None = None,
    interceptor: SandboxInterceptor | None = None,   # NEW
    confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
) -> str:
    raise NotImplementedError(...)
```

Add `if TYPE_CHECKING: from opencollab.tools.safety import SandboxInterceptor`
to avoid an import cycle.

### 2. `opencollab/tools/bash.py`

- Remove `__init__(self, interceptor=None)` and `self._interceptor`.
  `BashTool` becomes a stateless tool (the default `Tool` no-arg `__init__`
  suffices).
- `execute(...)` now reads the `interceptor` parameter; replace
  `self._interceptor` references with the parameter.
- Drop `from opencollab.tools.safety import SandboxInterceptor` if it
  becomes unused after these changes (use the new parameter's annotation
  via `TYPE_CHECKING`).

### 3. `opencollab/tools/fs.py`

- Remove `__init__(self, interceptor=None)` and `self._interceptor` from
  `FileReadTool`, `FileWriteTool`, `GrepTool`.
- **Delete `_resolve_env_scoped_path`** entirely (lines 26–37). Its job is
  fully covered by `SandboxInterceptor.check_path`, which the
  ToolCallProcessor now always provides.
- Simplify the path-checking block in `FileReadTool.execute` and
  `FileWriteTool.execute` from:
  ```python
  if self._interceptor:
      path = self._interceptor.check_path(path)
  elif env:
      path = _resolve_env_scoped_path(path, env)
  ```
  to:
  ```python
  if interceptor:
      path = interceptor.check_path(path)
  ```
  (`GrepTool` doesn't currently call `check_path` — leave its body
  alone other than the signature change.)

### 4. `opencollab/tools/human.py` and `opencollab/tools/mcp.py`

Add `interceptor` to the `execute()` signature for protocol consistency.
Both bodies ignore the parameter — these tools don't touch filesystem
paths or shell commands. One-line signature edit each.

### 5. `opencollab/team/orchestrator.py`

`DelegateTaskTool.execute` and `DelegateWithReviewTool.execute` — add the
`interceptor` parameter for protocol consistency. Bodies ignore it.

In `Team`:

- Delete `self._interceptor = SandboxInterceptor(workspace)` from
  `__init__` (line 177).
- Delete the per-teammate interceptor construction in `delegate()` (line
  270): `teammate_interceptor = SandboxInterceptor(env.workspace)`.
- Simplify `_make_basic_tools` — it no longer needs an `interceptor`
  parameter. It becomes:
  ```python
  def _make_basic_tools(self) -> list[Tool]:
      return [BashTool(), FileReadTool(), FileWriteTool(), GrepTool()]
  ```
  Or — better — delete `_make_basic_tools` and call
  `build_default_tools(include_ask_user=False)` from
  `opencollab.bootstrap.tool_factory`. That makes the tool-set definition
  live in exactly one place. **Pick this option** unless there's a reason
  not to.
- Drop `from opencollab.tools.safety import SandboxInterceptor` from
  imports.

### 6. `opencollab/harness/evaluator.py`

- Delete line 96: `interceptor = SandboxInterceptor(env.workspace) if
  isinstance(env, LocalEnvironment) else None`.
- The tool construction below it becomes
  `[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()]` (or
  `build_default_tools(include_ask_user=False)`).
- Drop `from opencollab.tools.safety import SandboxInterceptor` from
  imports.

### 7. `opencollab/core/session/tools.py` — `ToolCallProcessor`

Add `interceptor` ownership:

```python
class ToolCallProcessor:
    def __init__(
        self,
        *,
        agent: Any,
        env: Any,
        state: SessionState,
        event_bus: EventBus,
        tracer: Any = None,
        permission_policy: PermissionPolicy | None = None,
        interceptor: SandboxInterceptor | None = None,   # NEW
    ):
        self.agent = agent
        self.env = env
        ...
        # Derive from env.workspace if not provided. Tests can inject a fake.
        self.interceptor = interceptor or (
            SandboxInterceptor(env.workspace) if env is not None else None
        )
```

In `_execute_tool`, pass `interceptor=self.interceptor`:

```python
result = await tool.execute(
    args,
    env=self.env,
    interceptor=self.interceptor,
    confirm_fn=self._tool_confirm_fn(),
)
```

Add `from opencollab.tools.safety import SandboxInterceptor` import.

### 8. `opencollab/core/session/session.py`

No `Session.__init__` signature change. The interceptor is derived inside
`ToolCallProcessor`. If a test needs to inject a custom interceptor, it can
construct `Session`, then replace `session.tool_processor.interceptor`
directly — a one-attribute write.

(Open question: should `Session.__init__` expose `interceptor` for
symmetry with `env`? **No** for this PR — keep the change surface small.
Add it only if a real caller needs it.)

### 9. `opencollab/bootstrap/runtime.py`

- Remove `interceptor: SandboxInterceptor` field from `RuntimeContext`.
- Remove the `interceptor = SandboxInterceptor(abs_workspace)` line from
  `build_runtime_context`.
- Remove the `interceptor` argument from the `RuntimeContext(...)` call.
- Drop `from opencollab.tools.safety import SandboxInterceptor` import.

### 10. `opencollab/bootstrap/tool_factory.py`

```python
# BEFORE
def build_default_tools(
    interceptor: SandboxInterceptor,
    *,
    include_ask_user: bool = False,
) -> list[Tool]:
    tools = [
        BashTool(interceptor),
        FileReadTool(interceptor),
        FileWriteTool(interceptor),
        GrepTool(interceptor),
    ]
    if include_ask_user:
        tools.append(AskUserTool())
    return tools

# AFTER
def build_default_tools(*, include_ask_user: bool = False) -> list[Tool]:
    tools: list[Tool] = [BashTool(), FileReadTool(), FileWriteTool(), GrepTool()]
    if include_ask_user:
        tools.append(AskUserTool())
    return tools
```

Drop the `SandboxInterceptor` import.

### 11. `opencollab/bootstrap/session_factory.py`

Update `build_chat_session` — `build_default_tools(ctx.interceptor,
include_ask_user=True)` becomes
`build_default_tools(include_ask_user=True)`. The `ctx.interceptor`
reference goes away (the field doesn't exist anymore).

---

## Verification

After the edits, run these greps:

```bash
grep -rn "_interceptor\b" opencollab/opencollab        # should be empty
grep -rn "_resolve_env_scoped_path" opencollab/opencollab  # should be empty
grep -rn "SandboxInterceptor(" opencollab/opencollab   # only in core/session/tools.py
grep -rn "ctx\.interceptor\|\.interceptor =" opencollab/opencollab
# the .interceptor = line in ToolCallProcessor is allowed; nothing else
```

Expected: every construction site of `SandboxInterceptor` collapses to
exactly one — inside `ToolCallProcessor.__init__`.

---

## Tests

No tests currently construct real tools (the existing suite uses
FakeAgent/FakeTool fixtures), so the test churn is minimal.

Existing tests that will need to keep working:

- `tests/test_bootstrap.py::test_build_chat_session_uses_repo_map_and_tools`
  asserts the tool name set `{bash, file_read, file_write, grep, ask_user}`.
  This continues to pass — tool names don't change.

Add **one** new test in a new file `tests/test_tool_call_processor_interceptor.py`:

```python
import asyncio
import pytest

from opencollab.core.env import LocalEnvironment
from opencollab.core.session.tools import ToolCallProcessor
from opencollab.core.session.events import EventBus
from opencollab.core.session.state import SessionState
from opencollab.tools.safety import SandboxInterceptor


class FakeAgent:
    tools: list = []
    def find_tool(self, name): return None


def test_tool_call_processor_derives_interceptor_from_env(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    env = LocalEnvironment(str(ws))
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=env,
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
    )
    assert isinstance(proc.interceptor, SandboxInterceptor)
    # Path inside workspace resolves; path outside raises.
    assert proc.interceptor.check_path("inside.txt").startswith(str(ws.resolve()))
    with pytest.raises(PermissionError):
        proc.interceptor.check_path("/etc/passwd")


def test_tool_call_processor_accepts_explicit_interceptor(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    env = LocalEnvironment(str(ws))
    custom = SandboxInterceptor(str(ws))
    proc = ToolCallProcessor(
        agent=FakeAgent(),
        env=env,
        state=SessionState(messages=[]),
        event_bus=EventBus(None),
        interceptor=custom,
    )
    assert proc.interceptor is custom
```

If `pytest-asyncio` isn't in scope, these are synchronous tests — fine
as written. Drop `import asyncio` if unused.

---

## What is NOT in this PR

- Moving `interceptor` onto `Session.__init__`'s public signature. The
  refactor doesn't need it. Defer until a real caller requires it.
- Refactoring `SandboxInterceptor` itself (cmd regex list, allowed_dirs,
  etc.). Out of scope.
- Splitting `Team` further — step 6.
- The `auto_save` event subscriber — step 7.

---

## Acceptance checklist

- [ ] `Tool.execute` base signature includes `interceptor:
      SandboxInterceptor | None = None`.
- [ ] `BashTool`, `FileReadTool`, `FileWriteTool`, `GrepTool`,
      `AskUserTool`, `MCPTool`, `DelegateTaskTool`,
      `DelegateWithReviewTool` all accept the `interceptor` parameter.
- [ ] No tool class has an `__init__(interceptor=...)` or
      `self._interceptor` attribute.
- [ ] `_resolve_env_scoped_path` is gone from `tools/fs.py`.
- [ ] `SandboxInterceptor` is constructed in exactly one place:
      `core/session/tools.py:ToolCallProcessor.__init__`.
- [ ] `bootstrap.RuntimeContext` no longer has an `interceptor` field;
      `build_runtime_context` doesn't construct one.
- [ ] `build_default_tools` no longer takes an `interceptor` parameter.
- [ ] `Team.__init__` doesn't construct a `SandboxInterceptor`;
      `Team._make_basic_tools` is either removed or trivially returns
      `build_default_tools()`.
- [ ] `harness/evaluator.py` doesn't construct a `SandboxInterceptor`.
- [ ] `ToolCallProcessor` passes `interceptor=self.interceptor` to every
      `tool.execute(...)` call.
- [ ] All existing tests pass (`OPENAI_API_KEY=fake-test-key uv run
      pytest tests/ -q` → `36 passed` + new file's 2 tests = `38 passed`).
- [ ] `python -m opencollab chat` smoke test: `bash` tool still
      successfully runs `ls` inside the workspace; a path-escape attempt
      (`file_read` on `/etc/passwd`) still returns a `PermissionError`-shaped
      error string.

---

## Risk & rollback

- **Breaking change to `Tool.execute` signature** (new `interceptor`
  parameter). Out-of-tree subclasses break. The codebase has 8 in-repo
  Tool subclasses; all updated in this PR.
- **Behavior change for Docker eval**: today the eval skips interceptor
  construction for `DockerEnvironment` (`harness/evaluator.py:96`). After
  this PR, `ToolCallProcessor` builds a `SandboxInterceptor` from
  `env.workspace` regardless of env type. For Docker that's the container
  path; the path-jail check still works (validates against
  `env.workspace`), and the `check_cmd` regex list is workspace-agnostic.
  This is a *tightening* of behavior, not a loosening — Docker tools now
  get the same destructive-command protection as local tools. If a Docker
  eval expects to run `rm -rf /` and have it succeed, this PR breaks
  that. Highly unlikely; flag in PR description.
- The per-teammate interceptor isolation **still works**: each teammate
  Session has its own `env=WorktreeEnvironment(...)` with the worktree as
  `env.workspace`, so the per-teammate `ToolCallProcessor` derives an
  interceptor rooted at that worktree. The mechanism is the same; only
  the construction site moves.
- Rollback: single `git revert`.

Human-readable summary for the PR description: *"Unify `SandboxInterceptor`
ownership inside `ToolCallProcessor`. Tools are now stateless;
interceptors are derived from `env.workspace` at the runtime layer.
Removes ~70 lines of duplicated wiring across `bootstrap`, `team`,
`harness`, and `tools/fs.py`."*
