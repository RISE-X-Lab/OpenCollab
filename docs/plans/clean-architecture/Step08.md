# Step08 - CA-03 Close Legacy Tool Compatibility Boundary

Date: 2026-05-19
Branch: `refactor/step01-bootstrap`

## Current State

Step07 completes native `ToolRuntime` support for built-in tools:

- `BashTool`
- `FileReadTool`
- `FileWriteTool`
- `GrepTool`
- `AskUserTool`
- `MCPTool`

Each built-in tool now has a concrete `execute_with_runtime(params, runtime)`
implementation, while legacy `execute(params, env, interceptor, confirm_fn)`
wrappers remain for compatibility.

Current verified baseline from the Step07 review:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
# 42 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
# 36 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 104 passed
```

Current clean boundary:

```bash
rg -n "opencollab\\.core\\.session|opencollab\\.bootstrap|opencollab\\.tools\\.safety|SandboxInterceptor" \
  opencollab/opencollab/tools/base.py \
  opencollab/opencollab/tools/bash.py \
  opencollab/opencollab/tools/fs.py \
  opencollab/opencollab/tools/human.py \
  opencollab/opencollab/tools/mcp.py
# no matches
```

## Remaining Problem

CA-03 is behaviorally close, but compatibility logic is still mixed into the
main runtime path.

Current coupling points:

1. `ToolCallProcessor._execute_tool()` still checks for
   `execute_with_runtime`, then falls back to legacy `execute(...)`.

   This keeps duck-typed external tools working, but the session processor still
   knows about legacy runtime keyword expansion.

2. Base `Tool.execute_with_runtime()` still unwraps `ToolRuntime` into legacy
   `env`, `interceptor`, and `confirm_fn`.

   This is useful for third-party compatibility, but built-in tools no longer
   need it.

3. `tool_runtime_from_legacy()` still exposes the old `interceptor` parameter
   name.

   That name is now compatibility-only. The application-facing concept is
   `safety_policy`.

4. `ToolCallProcessor` still exposes `interceptor` as a constructor alias and
   instance attribute.

   This is probably still needed for tests/older call sites temporarily, but it
   should be explicitly marked as compatibility rather than treated as a core
   concept.

## Goal

Close CA-03 without breaking external tool compatibility:

- make `ToolCallProcessor` depend on a single runtime-aware dispatch helper;
- move legacy fallback out of the core session processor body;
- rename application-facing helper parameters to `safety_policy`;
- keep legacy `interceptor` support only at compatibility edges;
- keep all built-in tools runtime-native;
- keep tests and CLI/TUI behavior unchanged.

After this step, CA-03 should be clean enough to stop and move to CA-04.

## Implementation Plan

1. Add a tool dispatch adapter.

   Add a small application-owned helper, likely:

   - `opencollab/opencollab/application/tool_dispatch.py`

   Suggested shape:

   ```python
   async def execute_tool_with_runtime(tool, params, runtime):
       execute_with_runtime = getattr(tool, "execute_with_runtime", None)
       if execute_with_runtime is not None:
           return await execute_with_runtime(params, runtime)
       return await execute_legacy_tool(tool, params, runtime)

   async def execute_legacy_tool(tool, params, runtime):
       return await tool.execute(
           params,
           env=runtime.environment,
           interceptor=runtime.safety_policy,
           confirm_fn=runtime.confirm_fn(),
       )
   ```

   Keep this adapter dependency-light:

   ```text
   application.tool_dispatch -> application.tool_runtime only
   ```

   It must not import `core.session`, concrete tools, bootstrap, TUI, or
   `tools.safety`.

2. Update `ToolCallProcessor`.

   In `opencollab/opencollab/core/session/tools.py`:

   - import `execute_tool_with_runtime`;
   - replace inline runtime/legacy dispatch in `_execute_tool()` with:

     ```python
     result = await execute_tool_with_runtime(tool, args, runtime)
     ```

   `ToolCallProcessor` should still handle timing, exceptions, tracing, loop
   detection, and events. It should not know how legacy tool arguments are
   expanded.

3. Rename the legacy runtime helper parameter.

   In `opencollab/opencollab/application/tool_runtime.py`:

   - change `tool_runtime_from_legacy(..., interceptor=...)` to prefer:

     ```python
     tool_runtime_from_legacy(
         *,
         env,
         safety_policy,
         confirm_fn,
     )
     ```

   - optionally keep `interceptor` as a deprecated keyword-only alias for this
     patch if it materially reduces churn.

   Preferred approach for built-in tools:

   - update built-in wrappers to pass `safety_policy=interceptor`;
   - do not expose `interceptor` in new application-layer helpers except as a
     compatibility alias if needed.

4. Update built-in wrappers.

   In built-in tool `execute(...)` wrappers:

   - keep the legacy method signature unchanged;
   - call:

     ```python
     tool_runtime_from_legacy(
         env=env,
         safety_policy=interceptor,
         confirm_fn=confirm_fn,
     )
     ```

   This keeps public compatibility while moving naming pressure toward the
   application model.

5. Mark `ToolCallProcessor.interceptor` as compatibility-only.

   In `opencollab/opencollab/core/session/tools.py`:

   - keep constructor arg `interceptor` for now;
   - keep `self.interceptor` for compatibility tests;
   - add a short comment that new code should use `safety_policy`.

   Do not remove it yet unless `rg "interceptor="` proves there are no
   meaningful internal call sites and tests can be updated safely.

6. Strengthen dispatch tests.

   Extend `opencollab/tests/test_tool_call_processor_interceptor.py` or create
   a focused `test_tool_dispatch.py`.

   Required tests:

   - runtime-native tool dispatch uses `execute_with_runtime`;
   - legacy-only tool dispatch still receives `env`, `interceptor`, and
     `confirm_fn`;
   - `ToolCallProcessor._execute_tool()` delegates through the dispatch helper
     rather than owning fallback expansion directly.

   Prefer behavior tests over brittle source checks, but one small boundary
   source check is acceptable:

   ```python
   assert "confirm_fn=runtime.confirm_fn()" not in inspect.getsource(ToolCallProcessor._execute_tool)
   ```

7. Add application boundary checks.

   Add a test or command check that application dispatch/runtime modules do not
   import outward layers:

   ```bash
   rg -n "opencollab\\.core|opencollab\\.tools|opencollab\\.bootstrap|opencollab\\.tui" \
     opencollab/opencollab/application/tool_runtime.py \
     opencollab/opencollab/application/tool_dispatch.py
   # no matches
   ```

8. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary checks from repo root:

   ```bash
   rg -n "opencollab\\.core|opencollab\\.tools|opencollab\\.bootstrap|opencollab\\.tui" \
     opencollab/opencollab/application/tool_runtime.py \
     opencollab/opencollab/application/tool_dispatch.py

   rg -n "opencollab\\.core\\.session|opencollab\\.bootstrap|opencollab\\.tools\\.safety|SandboxInterceptor" \
     opencollab/opencollab/tools/base.py \
     opencollab/opencollab/tools/bash.py \
     opencollab/opencollab/tools/fs.py \
     opencollab/opencollab/tools/human.py \
     opencollab/opencollab/tools/mcp.py
   ```

   Both checks should return no matches.

## Acceptance Criteria

- `ToolCallProcessor._execute_tool()` delegates runtime dispatch to an
  application helper.
- Legacy `execute(...)` fallback still works for third-party/duck-typed tools.
- Built-in tools stay native-runtime.
- `tool_runtime_from_legacy()` uses `safety_policy` naming for new code.
- Public legacy tool signatures remain unchanged.
- Application runtime/dispatch modules do not import core, tools, bootstrap, or
  UI modules.
- Built-in tool modules do not import core session, bootstrap, or concrete
  sandbox modules.
- Tool schemas are unchanged.
- Full test suite remains green.

## Non-Goals

- Do not remove legacy `Tool.execute(...)` yet.
- Do not remove `ToolCallProcessor(interceptor=...)` yet unless it is proven
  safe and tiny.
- Do not change external or MCP tool compatibility.
- Do not change grep path-safety behavior.
- Do not change CLI/TUI behavior.
- Do not start CA-04 in this patch.

## Next After This

If Step08 is green, CA-03 is done enough.

Then start CA-04 with a domain extraction step:

- identify pure value/state objects currently in `core.session`;
- move `SessionPhase`, `ToolProcessingResult`, and small tool-call result
  value objects into a domain package;
- keep `SessionState` movement for a separate follow-up if the first extraction
  is too large.
