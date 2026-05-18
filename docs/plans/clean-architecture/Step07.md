# Step07 - CA-03 Finish Native ToolRuntime For Built-In Tools

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Current Code Diff Review

Step06 has migrated `BashTool` to native `ToolRuntime`:

- `opencollab/opencollab/application/tool_runtime.py`
  - adds `CallbackPermissionPort`;
  - keeps `ToolRuntime.confirm_fn()` as the compatibility callback path.
- `opencollab/opencollab/tools/bash.py`
  - `BashTool.execute_with_runtime(params, runtime)` owns the real behavior;
  - legacy `BashTool.execute(...)` wraps legacy args into `ToolRuntime`.
- `opencollab/tests/test_tool_runtime_contract.py`
  - covers direct legacy `execute(...)`;
  - covers native `execute_with_runtime(...)`;
  - covers no-env behavior;
  - covers command safety and permission callback behavior;
  - covers blocked command behavior;
  - asserts `tools/bash.py` does not import inner layers.

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
# 27 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 89 passed
```

## Remaining Problem

Only `BashTool` is native-runtime. The remaining built-in tools still rely on
the base compatibility adapter:

```text
Tool.execute_with_runtime(...)
  -> concrete_tool.execute(params, env=..., interceptor=..., confirm_fn=...)
```

Remaining built-in tools:

- `FileReadTool`
- `FileWriteTool`
- `GrepTool`
- `AskUserTool`
- `MCPTool`

These tools now type against application ports, but their actual behavior still
lives in the legacy `execute(...)` method.

## Goal

Finish CA-03 for built-in tools in one coherent patch:

- move real behavior for remaining built-in tools into
  `execute_with_runtime(params, runtime)`;
- keep legacy `execute(...)` wrappers for compatibility;
- keep tool schemas unchanged;
- keep current user-visible behavior unchanged.

This is a bigger step than Step06, but it is still one architectural boundary:

```text
built-in tools consume ToolRuntime natively
```

## Implementation Plan

1. Add a reusable legacy-runtime helper.

   Avoid duplicating wrapper logic in every tool.

   In `opencollab/opencollab/application/tool_runtime.py`, add a helper such
   as:

   ```python
   def tool_runtime_from_legacy(
       *,
       env,
       interceptor,
       confirm_fn,
   ) -> ToolRuntime:
       return ToolRuntime(
           environment=env,
           safety_policy=interceptor,
           permission_policy=CallbackPermissionPort(confirm_fn) if confirm_fn else None,
       )
   ```

   Keep this application-owned and dependency-light.

   Then simplify `BashTool.execute(...)` to use the helper too.

2. Migrate `FileReadTool`.

   In `opencollab/opencollab/tools/fs.py`:

   - add `execute_with_runtime(params, runtime)`;
   - move current read behavior there;
   - use `runtime.environment`;
   - use `runtime.safety_policy.check_path(path)` when present;
   - keep fallback local file read behavior unchanged when no environment is
     provided;
   - keep legacy `execute(...)` as a wrapper using
     `tool_runtime_from_legacy(...)`.

   Tests:

   - direct legacy read still passes;
   - native runtime read still passes;
   - path jail still raises `PermissionError`;
   - file-not-found and permission-error string behavior remains unchanged.

3. Migrate `FileWriteTool`.

   In `opencollab/opencollab/tools/fs.py`:

   - add `execute_with_runtime(params, runtime)`;
   - move current create and `str_replace` behavior there;
   - keep file locking behavior unchanged;
   - keep safety policy path check unchanged;
   - keep local filesystem fallback unchanged when no environment is provided;
   - keep legacy `execute(...)` as a wrapper.

   Tests:

   - direct legacy create and str_replace still pass;
   - native runtime create and str_replace pass;
   - path jail behavior remains unchanged;
   - duplicate/missing `old_str` error messages remain unchanged.

4. Migrate `GrepTool`.

   In `opencollab/opencollab/tools/fs.py`:

   - add `execute_with_runtime(params, runtime)`;
   - move current grep behavior there;
   - keep current `env.exec_cmd(...)` path unchanged;
   - keep Python fallback unchanged when no environment is provided;
   - keep legacy `execute(...)` as a wrapper.

   Important compatibility rule:

   - Do **not** add path-safety checking to `GrepTool` in this step.
   - Existing tests characterize that `GrepTool` does not call
     `safety_policy.check_path()` for `path`.

   That may become a later security hardening decision, but not as part of this
   runtime-boundary migration.

5. Migrate `AskUserTool`.

   In `opencollab/opencollab/tools/human.py`:

   - add `execute_with_runtime(params, runtime)`;
   - use `runtime.confirm_fn()`;
   - keep non-interactive fallback unchanged when no permission callback exists;
   - keep `_prompt_user()` behavior unchanged;
   - keep legacy `execute(...)` as a wrapper.

   Tests:

   - native runtime without permission policy returns the exact current
     non-interactive fallback;
   - legacy `execute(..., confirm_fn=None)` still returns the same fallback;
   - if feasible, test that a runtime permission callback path reaches prompt
     code via monkeypatching `_prompt_user`, without requiring real input.

6. Migrate `MCPTool`.

   In `opencollab/opencollab/tools/mcp.py`:

   - add `execute_with_runtime(params, runtime)`;
   - ignore runtime for now, because MCP execution goes through its connection;
   - move current `call_tool` response handling into `execute_with_runtime`;
   - keep legacy `execute(...)` as a wrapper.

   Tests:

   - add a small fake MCP connection test if existing coverage is absent;
   - cover text content response;
   - cover error response;
   - cover exception response.

7. Update boundary tests.

   Extend `opencollab/tests/test_tool_runtime_contract.py`:

   - every built-in tool module should avoid imports from:
     - `opencollab.core.session`
     - `opencollab.bootstrap`
     - `opencollab.tools.safety`
   - built-in tool classes should implement a concrete
     `execute_with_runtime(...)` rather than relying on the base adapter.

   Suggested assertion:

   ```python
   assert FileReadTool.execute_with_runtime is not Tool.execute_with_runtime
   assert FileWriteTool.execute_with_runtime is not Tool.execute_with_runtime
   assert GrepTool.execute_with_runtime is not Tool.execute_with_runtime
   assert AskUserTool.execute_with_runtime is not Tool.execute_with_runtime
   assert MCPTool.execute_with_runtime is not Tool.execute_with_runtime
   ```

8. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary checks from repo root:

   ```bash
   rg -n "opencollab\\.core\\.session|opencollab\\.bootstrap|opencollab\\.tools\\.safety|SandboxInterceptor" \
     opencollab/opencollab/tools/base.py \
     opencollab/opencollab/tools/bash.py \
     opencollab/opencollab/tools/fs.py \
     opencollab/opencollab/tools/human.py \
     opencollab/opencollab/tools/mcp.py
   ```

   This should return no matches.

## Acceptance Criteria

- `BashTool`, `FileReadTool`, `FileWriteTool`, `GrepTool`, `AskUserTool`, and
  `MCPTool` all have concrete `execute_with_runtime(...)` implementations.
- Legacy `execute(...)` wrappers still work for all built-in tools.
- Tool schemas are unchanged.
- Bash behavior remains unchanged.
- Filesystem read/write behavior remains unchanged.
- Grep behavior remains unchanged, including current lack of path-safety check.
- Ask-user non-interactive fallback remains unchanged.
- MCP response formatting remains unchanged.
- Tool modules do not import `core.session`, `bootstrap`, or `tools.safety`.
- Full test suite remains green.

## Non-Goals

- Do not remove legacy `Tool.execute(...)`.
- Do not remove the base compatibility adapter yet.
- Do not change `GrepTool` path-safety behavior.
- Do not change permission prompt text or ask-user UI behavior.
- Do not change MCP protocol handling beyond dispatch shape.
- Do not move `ToolCallProcessor` out of `core.session` yet.
- Do not split events in this step.

## Next After This

After this step, CA-03 should be complete for built-in tools.

Next likely step:

- clean the remaining legacy fallback in `ToolCallProcessor`;
- decide how long third-party legacy `execute(...)` compatibility should stay;
- then start CA-04 by extracting pure session/tool value objects into a domain
  layer.
