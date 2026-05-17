# Step 01 — Extract `bootstrap/` composition layer

Part of the refactor sequence identified in `docs/repomap/module_map.puml`.
This is **step 1 of 7**: extract a composition layer out of `cli/main.py` so the
CLI only parses args and drives the REPL. Pure code-motion refactor — no
behavior changes, no public API removed.

---

## Goal

Move all wiring (config resolution, tracer, repo-map, interceptor, agent,
tool list, session, team) out of `cli/main.py`'s `_chat`, `_team`, `_eval`
functions into a new top-level `opencollab/bootstrap/` package.

After this PR, `cli/main.py` should:

- Parse Typer args.
- Build UI hooks (`TUI`, `TuiEventSink`, `TuiPermissionPolicy`).
- Call `bootstrap.*` to obtain a `Session` or `Team`.
- Run the REPL loop (read line → `add_user_message` → `run_loop` → handle
  `/save`, `/exit`).
- Print stats and goodbye.

Nothing else.

---

## Deliverable

```
opencollab/bootstrap/
  __init__.py          # re-exports public surface
  runtime.py           # RuntimeContext + build_runtime_context()
  tool_factory.py      # build_default_tools()
  session_factory.py   # build_chat_session() + build_team()
```

### Public surface (re-exported from `bootstrap/__init__.py`)

```python
from opencollab.bootstrap.runtime import RuntimeContext, build_runtime_context
from opencollab.bootstrap.tool_factory import build_default_tools
from opencollab.bootstrap.session_factory import build_chat_session, build_team
```

CLI imports nothing else from `bootstrap.*`.

### `runtime.py`

```python
@dataclass
class RuntimeContext:
    workspace: str                       # absolute path
    config: dict                         # resolved (model/provider/api_key/base_url/budget)
    tracer: Tracer | None
    repo_map: str | None
    interceptor: SandboxInterceptor
    event_sink: EventSink | None
    permission_policy: PermissionPolicy | None

def build_runtime_context(
    workspace: str,
    cli_overrides: dict,                 # output of cli._resolve_config
    *,
    trace: bool,
    yolo: bool,
    event_sink: EventSink | None = None,
    confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    run_id_prefix: str = "",
) -> RuntimeContext: ...
```

**Note (intentional):** `RuntimeContext` does **not** carry an `Environment`.
Env lifetime is per-Session (chat), per-delegation (team teammates), or
per-task (eval). `build_chat_session` constructs `LocalEnvironment(ctx.workspace)`
inline; `Team` and `run_eval_task` continue to own their env construction.

`yolo=True` ⇒ `permission_policy=None`. `yolo=False` + `confirm_fn` ⇒
`TuiPermissionPolicy(confirm_fn)`.

### `tool_factory.py`

```python
def build_default_tools(
    interceptor: SandboxInterceptor,
    *,
    include_ask_user: bool = False,
) -> list[Tool]:
    """Canonical tool bundle: bash, file_read, file_write, grep, [ask_user]."""
```

Isolating this means step 5 (interceptor unification) only changes this file.

### `session_factory.py`

```python
def build_chat_session(
    ctx: RuntimeContext,
    *,
    session_file: str | None = None,
    auto_save: bool = True,
) -> Session:
    """Build the single-agent chat Session. Constructs LocalEnvironment(ctx.workspace)
    internally. If session_file is provided and exists, uses Session.load(...); otherwise
    creates a fresh Session and immediately writes the auto-save JSONL."""

def build_team(
    ctx: RuntimeContext,
    *,
    use_worktrees: bool,
    interactive: bool,                   # if True, append AskUserTool to lead
) -> Team:
    """Build the multi-agent Team. interactive=True appends AskUserTool to
    team.lead_agent.tools post-construction (keeps headless eval clean —
    preserve the existing comment about the SWE-bench regression root cause)."""
```

---

## Concrete edits

1. **Create `opencollab/bootstrap/`** with the four files above. Move from
   `cli/main.py`:
   - `os.path.abspath(workspace)`
   - `SandboxInterceptor(workspace)` construction
   - `Tracer(run_id=...)` construction (chat: `uuid[:8]`; team: `"team-"+uuid[:8]`)
   - `get_repo_map(workspace)` call
   - auto-save path computation (`.opencollab/sessions/<id>.jsonl`)
   - `Agent(...)` construction with the default tool list
   - `Session(...)` / `Session.load(...)` selection
   - the post-construction `team.lead_agent.tools.append(AskUserTool())`

2. **Slim `cli/main.py`** (target: under ~250 lines, down from 451):
   - `_chat`: build `TUI` + `TuiEventSink` + `TuiPermissionPolicy` (UI stays in
     CLI), call `build_runtime_context`, call `build_chat_session`, run REPL.
   - `_team`: build UI hooks, `build_runtime_context`, `build_team(interactive=True)`,
     run REPL.
   - `_eval`: leave as-is in this PR (see "Out of scope" below).

3. **Add `Team.used_tokens` property** to replace the CLI's encapsulation breach
   at `cli/main.py:369`:
   ```python
   team_instance.lead_session.used_tokens + team_instance._used_tokens
   ```
   becomes
   ```python
   team_instance.used_tokens
   ```
   Property body: `return self.lead_session.used_tokens + self._used_tokens`.
   This is the only `team/orchestrator.py` edit in this PR.

4. **Do NOT touch** in this PR:
   - `Session.__init__` (step 2)
   - `Runner` / `_step` / `_advance` shims (step 3)
   - `TUI._active_instance` singleton (step 4)
   - `SandboxInterceptor` unification (step 5)
   - `Team` decomposition beyond the one property above (step 6)
   - `harness/evaluator.py` body (step 6/7)
   - Persistence via events (step 7)

---

## Import direction (must remain acyclic)

```
cli.main ─► bootstrap.session_factory ─► bootstrap.tool_factory
                    │                            │
                    └────► bootstrap.runtime ◄───┘
                                  │
                                  ▼
                       core/* , tools/* , team/*
```

`team.orchestrator` must **not** import `bootstrap.*`. Team keeps owning its
internal teammate construction. Bootstrap depends on Team, not the other way
around.

---

## Tests

Existing suite under `opencollab/tests/` already covers `Session`, `Team`, eval
behavior — all must still pass.

Add two unit tests in `opencollab/tests/test_bootstrap.py`:

- `test_build_chat_session_uses_repo_map_and_tools` — assert the Session's
  system message contains the repo-map header (`"Project Structure:"`) and the
  agent has exactly the expected tool names:
  `{"bash", "file_read", "file_write", "grep", "ask_user"}`.

- `test_build_runtime_context_resolves_workspace_and_tracer` — pass `trace=False`
  and assert `ctx.tracer is None`; pass `trace=True` and assert the JSONL
  trajectory file is created at the expected path; assert `ctx.workspace` is
  absolute.

---

## Acceptance checklist

- [ ] `opencollab/bootstrap/` exists with the four files; total ≤ ~200 lines.
- [ ] `python -m opencollab chat` enters the REPL and runs at least one
      tool-using turn end-to-end.
- [ ] `python -m opencollab team "<task>"` runs and exits cleanly; worktrees
      are cleaned up (`team_instance.cleanup()` still called).
- [ ] `python -m opencollab eval <fixture>` produces the same `results.jsonl`
      shape as before (eval path untouched).
- [ ] `cli/main.py` is under ~250 lines and no longer imports any of:
      `BashTool`, `FileReadTool`, `FileWriteTool`, `GrepTool`, `AskUserTool`,
      `SandboxInterceptor`, `LocalEnvironment`, `Tracer`, `get_repo_map`, `Agent`.
- [ ] `Team` has a public `used_tokens` property; CLI uses it.
- [ ] No edits to `core/session/*`, `harness/evaluator.py`. Only `team/orchestrator.py`
      edit is the new property.
- [ ] All existing tests pass; two new bootstrap tests added and passing.

---

## Risk & rollback

- Pure code-motion refactor. No public API removed; `Session`, `Team`, `Agent`,
  `Tool` signatures unchanged.
- Risk hotspots to eyeball during review:
  - Auto-save on first turn: chat currently writes a session JSONL
    immediately after construction (`cli/main.py:256`, `session.save(auto_save_path)`).
    Preserve this in `build_chat_session`.
  - The post-construction `AskUserTool` append on the Lead agent
    (`cli/main.py:340`) exists specifically to keep headless eval clean —
    keep the existing inline comment when moving it into `build_team`.
  - Bootstrap must not introduce a `core → team` import. Verify with
    `python -c "import opencollab.core"` succeeding without importing
    `team.orchestrator`.
- Rollback: single `git revert` of the PR.

---

## Out of scope (defer to later steps)

- Refactoring `_eval`'s wiring — `run_eval_task` builds its own env, agent,
  session, and tracer. Touching it doubles the diff and the eval path has its
  own concerns (Docker, timeouts, patch extraction). Defer to step 6.
- Anything in the "Do NOT touch" list above.

Human-readable summary for the PR description: *"Extract a `bootstrap/`
composition layer so `cli/main.py` only parses args and drives the REPL.
No behavior changes."*
