# Step14 - REM-07 Retire Legacy Tool Execution Compatibility

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current Code Review

Step13 moved runtime construction into `bootstrap/container.py` and slimmed
`Session` to a facade. Test suite: 161 passed.

The remaining named legacy surface is the tool execution bridge.

Today every concrete tool carries two parallel entry points:

- `Tool.execute(params, env=, interceptor=, confirm_fn=)` — the historical
  shape, kept as a compatibility shim;
- `Tool.execute_with_runtime(params, runtime: ToolRuntime)` — the
  runtime-native shape that the application layer dispatches on.

Concrete tools override **both** in a redundant way:

- `opencollab/opencollab/tools/bash.py:50-62` — `execute` builds a
  `ToolRuntime` via `tool_runtime_from_legacy` and forwards to
  `execute_with_runtime`.
- `opencollab/opencollab/tools/fs.py:44-58`, `:138-152`, `:235-249` —
  same shape repeated for `FileReadTool`, `FileWriteTool`, `GrepTool`.
- `opencollab/opencollab/tools/human.py:43-57` — same shape for
  `AskUserTool`.
- `opencollab/opencollab/tools/mcp.py:179-193` — same shape for `MCPTool`.

The base class mirrors the bridge in the opposite direction:

- `opencollab/opencollab/tools/base.py:44-52` — `Tool.execute` raises
  `NotImplementedError`;
- `opencollab/opencollab/tools/base.py:54-64` — `Tool.execute_with_runtime`
  default falls back to `self.execute(params, env=, interceptor=,
  confirm_fn=)`.

The application layer keeps the legacy fallback:

- `opencollab/opencollab/application/tool_runtime.py:29-40` —
  `tool_runtime_from_legacy(env=, safety_policy=, confirm_fn=, interceptor=)`;
- `opencollab/opencollab/application/tool_dispatch.py:13-29` —
  `execute_tool_with_runtime` prefers `execute_with_runtime` but falls
  back to `execute_legacy_tool`, which calls
  `tool.execute(params, env=, interceptor=, confirm_fn=)`.

Tests still pin the bridge:

- `opencollab/tests/test_tool_runtime_contract.py:8,77,102,127,143,149,
  161,164,181,215` — directly imports `tool_runtime_from_legacy` and
  calls `BashTool().execute(...)`, `FileReadTool().execute(...)` with
  the legacy keyword shape.
- `opencollab/tests/test_tool_call_processor_interceptor.py:48` —
  defines a fake tool with the legacy `execute(args, env=, interceptor=,
  confirm_fn=)` signature.
- `opencollab/tests/test_session_characterization.py:98` — same fake
  tool shape.
- `opencollab/tests/test_tool_execution_use_case.py:81` — same.
- `opencollab/tests/test_tool_dispatch.py:38` — same.

Verification baseline from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 161 passed
```

## Remaining Problem

The bridge is now pure dead weight:

- Every concrete built-in tool has a `ToolPort` runtime-native
  implementation (`execute_with_runtime`).
- Dispatch in `application/tool_dispatch.py` already prefers
  `execute_with_runtime`; the legacy branch only runs if a tool lacks the
  runtime-native method, which no built-in tool does
  (`test_tool_runtime_contract.py:505-510` already asserts this).
- The two-way bridge in `tools/base.py` keeps both shapes alive purely so
  the legacy keyword call sites in tests and `tool_runtime_from_legacy`
  keep working.

This forces two costs:

1. Anyone adding a new tool must decide which method to override, and
   gets no compile-time signal which one is canonical.
2. The `EnvironmentPort` / `SafetyPolicyPort` / permission contract is
   threaded through tools in two shapes: a `ToolRuntime` bundle on one
   side, three separate keyword arguments on the other. The keyword
   shape leaks safety/env coupling into every tool surface.

## Goal

After this step:

- `Tool.execute_with_runtime(params, runtime)` is the **only** entry
  point. It is required on every concrete tool.
- `Tool.execute(params, env=, interceptor=, confirm_fn=)` is gone.
- `application.tool_runtime.tool_runtime_from_legacy` is gone.
- `application.tool_dispatch.execute_legacy_tool` is gone;
  `execute_tool_with_runtime` calls `execute_with_runtime` directly.
- No concrete tool in `opencollab.tools` references
  `tool_runtime_from_legacy`.
- Tests use `ToolRuntime` directly when they want to call a tool, or
  build a fake tool that implements `execute_with_runtime`.
- `ToolPort` in `application/ports.py` remains the structural protocol;
  tool subclasses already satisfy it.

This is one boundary change:

```text
tool runtime contract narrows to ToolPort.execute_with_runtime
```

Tool **schemas** (`name`, `description`, `parameters`,
`to_openai_schema()`) are unchanged.

## Implementation Plan

### 1. Inventory external callers

Before deleting, confirm there is no out-of-tree consumer of the legacy
shape:

```bash
rg -n "Tool\\.execute\\(|tool_runtime_from_legacy|execute_legacy_tool" \
  opencollab/opencollab opencollab/tests
```

Expected matches: only the call sites listed in this plan. If anything
else surfaces (notebooks, scripts under `opencollab/scripts`, evaluator
adapters), surface them in the PR description and migrate them in the
same patch.

There is **no** public extension point claim today: `Tool` is documented
as an internal base class. We treat legacy as internal and delete it.

### 2. Make `execute_with_runtime` required on the base class

In `opencollab/opencollab/tools/base.py`:

- delete the `execute(...)` method;
- replace `execute_with_runtime` default body with
  `raise NotImplementedError(f"Tool '{self.name}' must implement execute_with_runtime()")`;
- remove the now-unused imports of
  `EnvironmentPort`, `SafetyPolicyPort`, `Callable`, `Awaitable`;
- update the module docstring line `LLM calls tools via function calling;
  the framework routes to the right Tool.execute().` to read
  `... routes to the right Tool.execute_with_runtime().`.

Result: `Tool` reduces to schema fields plus the runtime contract method.

### 3. Drop `execute(...)` overrides in built-in tools

Remove the `execute(...)` shim from each concrete tool and drop the
`tool_runtime_from_legacy` import:

- `opencollab/opencollab/tools/bash.py`
  - delete the `execute(...)` method (`bash.py:50-62`);
  - drop `from opencollab.application.tool_runtime import ToolRuntime, tool_runtime_from_legacy` and re-add only `ToolRuntime`;
  - drop unused `Callable, Awaitable` imports if any.
- `opencollab/opencollab/tools/fs.py`
  - delete `execute(...)` for `FileReadTool` (`fs.py:44-58`),
    `FileWriteTool` (`fs.py:138-152`), `GrepTool` (`fs.py:235-249`);
  - same import cleanup.
- `opencollab/opencollab/tools/human.py`
  - delete `execute(...)` for `AskUserTool` (`human.py:43-57`);
  - same import cleanup.
- `opencollab/opencollab/tools/mcp.py`
  - delete `execute(...)` for `MCPTool` (`mcp.py:179-193`);
  - same import cleanup.

Each tool retains its `execute_with_runtime` body unchanged.

### 4. Remove the application-layer bridge

In `opencollab/opencollab/application/tool_runtime.py`:

- delete `tool_runtime_from_legacy(...)` (`tool_runtime.py:29-40`);
- delete `CallbackPermissionPort` if no caller remains after Step 5
  (it was introduced solely for the legacy bridge); if a caller exists,
  keep it but document the reason.

In `opencollab/opencollab/application/tool_dispatch.py`:

- replace `execute_tool_with_runtime` body with:

  ```python
  async def execute_tool_with_runtime(
      tool: Any,
      params: dict[str, Any],
      runtime: ToolRuntime,
  ) -> str:
      return await tool.execute_with_runtime(params, runtime)
  ```

- delete `execute_legacy_tool`.

The application layer now talks to tools through one method.

### 5. Migrate tests to the runtime-native shape

Tests that build fake tools using the legacy signature need to switch
to `execute_with_runtime`.

- `opencollab/tests/test_tool_runtime_contract.py`
  - remove the `from opencollab.application.tool_runtime import
    ... tool_runtime_from_legacy` import; add only `ToolRuntime`;
  - rewrite the two `test_tool_runtime_from_legacy_*` tests as
    `test_tool_runtime_constructs_directly` covering the same wiring
    properties (env, safety_policy, permission_policy `confirm` callable);
  - rewrite `BashTool().execute({"command": ...}, env=..., interceptor=...)`
    call sites (`:102`, `:127`, `:181`, `:215`) as:

    ```python
    runtime = ToolRuntime(
        environment=env,
        safety_policy=safety,
        permission_policy=None,
    )
    result = run(BashTool().execute_with_runtime({...}, runtime))
    ```

  - keep the runtime-native override checks at `:505-510` unchanged.
- `opencollab/tests/test_tool_call_processor_interceptor.py:48` — change
  the fake tool to define `execute_with_runtime(self, params, runtime)`
  and read env/safety from `runtime`.
- `opencollab/tests/test_session_characterization.py:98` — same change
  for the fake tool.
- `opencollab/tests/test_tool_execution_use_case.py:81` — same.
- `opencollab/tests/test_tool_dispatch.py:38` — same; this test
  previously exercised the legacy fallback path, so it now becomes a
  redundant duplicate of the runtime-native dispatch test. Either:
  - keep it as a smoke test that asserts dispatch calls
    `execute_with_runtime`; or
  - delete it if `test_tool_runtime_contract.py` covers the same
    behavior.

  Preferred: keep as a smoke test renamed accordingly.

### 6. Update boundary checks and docs

- Re-run the existing application boundary tests
  (`test_application_boundaries.py`) — no change expected; this step
  removes code rather than adding cross-layer imports.
- Add a guard test that asserts no concrete tool keeps an `execute`
  method:

  ```python
  def test_no_concrete_tool_defines_legacy_execute():
      import inspect
      import opencollab.tools as tools_pkg
      from opencollab.tools.base import Tool
      for name in ("bash", "fs", "human", "mcp"):
          mod = __import__(f"opencollab.tools.{name}", fromlist=["_"])
          for cls in vars(mod).values():
              if isinstance(cls, type) and issubclass(cls, Tool) and cls is not Tool:
                  assert "execute" not in cls.__dict__, (
                      f"{cls.__name__} still overrides legacy execute"
                  )
  ```

  Place under `opencollab/tests/test_tool_runtime_contract.py` so it
  lives next to the runtime override assertions already there.
- Update `opencollab/opencollab/tools/base.py` module docstring to drop
  the reference to the legacy `execute()` method.

### 7. Verify

From `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_tool_runtime_contract.py \
  tests/test_tool_dispatch.py \
  tests/test_tool_call_processor_interceptor.py \
  tests/test_tool_execution_use_case.py -q

OPENAI_API_KEY=fake-test-key uv run pytest \
  tests/test_session_characterization.py \
  tests/test_session_construction.py \
  tests/test_application_boundaries.py -q

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Search confirmations from repo root:

```bash
rg -n "tool_runtime_from_legacy|execute_legacy_tool" \
  opencollab/opencollab opencollab/tests
# no matches

rg -n "def execute\\(" opencollab/opencollab/tools
# no matches in concrete tools (base.py also clean)

rg -n "Tool\\.execute\\(" opencollab/opencollab opencollab/tests
# no matches outside docstrings
```

Manual smoke test (golden path):

```bash
OPENAI_API_KEY=$REAL_KEY uv run opencollab chat
# verify bash, file read, file write, grep, ask_user behavior end-to-end.
```

## Acceptance Criteria

- `Tool.execute` no longer exists on the base class or any concrete tool
  in `opencollab.tools`.
- `Tool.execute_with_runtime` is the only required override; the base
  default raises `NotImplementedError`.
- `application.tool_runtime.tool_runtime_from_legacy` is deleted.
- `application.tool_dispatch.execute_legacy_tool` is deleted;
  `execute_tool_with_runtime` is a one-liner that calls
  `tool.execute_with_runtime(...)`.
- No concrete tool imports `tool_runtime_from_legacy`.
- All migrated tests pass without referencing the legacy keyword shape.
- New `test_no_concrete_tool_defines_legacy_execute` guard prevents
  regressions.
- `test_application_boundaries.py` continues to pass.
- Full test suite (`pytest tests/ -q`) is green.
- Tool schemas (`name`, `description`, `parameters`,
  `to_openai_schema()`) are byte-for-byte unchanged.

## Non-Goals

- Do not rename `execute_with_runtime` or change its signature in this
  step. (A later cleanup may rename to `execute`, but only after the
  legacy method is gone for at least one release-equivalent window.)
- Do not change `ToolRuntime` fields or `EnvironmentPort` /
  `SafetyPolicyPort` / `PermissionPort` protocols.
- Do not move tool modules between layers.
- Do not introduce a `ToolRegistry`.
- Do not change MCP tool discovery or schema generation.
- Do not change CLI behavior or tool output formatting.
- Do not extract `SessionRunUseCase`. That is Step15 (REM-02).

## Risks And Mitigations

- **External tool consumers we missed**. Mitigation: inventory step
  before deletion; if any out-of-tree tool inherits `Tool` and still
  implements `execute(...)`, the new base default will raise
  `NotImplementedError` loudly on first invocation rather than
  silently misbehaving.
- **Test fakes that subclass `Tool`**. Mitigation: the migration step
  enumerates each fake (`test_tool_call_processor_interceptor.py:48`,
  `test_session_characterization.py:98`,
  `test_tool_execution_use_case.py:81`, `test_tool_dispatch.py:38`)
  and converts them.
- **MCP tool overload**. `MCPTool` currently bridges to MCP server
  invocation through `execute_with_runtime`; the legacy shim is also
  present. Mitigation: read `mcp.py:193+` carefully and confirm the
  runtime-native path already handles the production MCP call, then
  delete only the shim.
- **CallbackPermissionPort orphan**. After removing the legacy bridge,
  `CallbackPermissionPort` may have no callers. Mitigation: delete it
  if so; bootstrap and TUI already implement `PermissionPort` directly.

## Next After This

Step15 (REM-02) — extract `SessionRunUseCase`:

- move the run loop, cancellation handling, budget checks, step tracing,
  compaction trigger, and tool execution coordination from
  `core.session.runner.SessionRunner` into
  `opencollab.application.session_run.SessionRunUseCase`;
- depend on `LLMPort`, `EventPublisherPort`, `TracePort`,
  `TokenEstimatorPort`, plus `ToolExecutionUseCase` and
  `ContextCompactionUseCase`;
- keep `SessionRunner` as a facade so `bootstrap.container` continues to
  export the same surface.

Step16 — refresh `docs/repomap/repomap-v2.puml` and re-render PDFs to
match the post-Step15 dependency graph; mark REM-08 done.

After Step15 lands, `core.session` should hold only:

- `events.py` (compatibility alias for `SessionRuntimeEvent`);
- `storage.py` (`SessionStore`);
- `autosave.py` (`AutoSaveSubscriber`);
- `session.py` (`Session` facade);
- `runner.py` (`SessionRunner` facade);
- `compactor.py` (`ContextCompactor` facade);
- `tools.py` (`ToolCallProcessor` facade).

At that point Clean Architecture dependency-rule compliance should be
≥90%, and the next phase becomes optional polish (facade removal,
package renames, full repomap refresh).
