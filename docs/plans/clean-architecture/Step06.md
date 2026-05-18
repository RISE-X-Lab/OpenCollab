# Step06 - CA-03 Migrate BashTool To Native ToolRuntime

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Current Code Diff Review

The current Step05 code has made tool dispatch runtime-aware:

- `opencollab/opencollab/application/ports.py`
  - `ToolPort` now describes `execute_with_runtime(params, runtime)`.
  - The legacy `execute(params, env, interceptor, confirm_fn)` signature is no
    longer part of `ToolPort`.
- `opencollab/opencollab/application/tool_runtime.py`
  - `ToolRuntime.confirm_fn()` exposes the compatibility permission callback.
- `opencollab/opencollab/tools/base.py`
  - base `Tool.execute_with_runtime()` adapts `ToolRuntime` back to legacy
    keywords.
- `opencollab/opencollab/core/session/tools.py`
  - `ToolCallProcessor` prefers `execute_with_runtime()`.
  - legacy-only duck-typed tools still work through a fallback path.
- `opencollab/tests/test_tool_call_processor_interceptor.py`
  - verifies runtime-aware dispatch is preferred.
  - verifies legacy fallback still receives `env`, `interceptor`, and
    `confirm_fn`.
- `opencollab/tests/test_tool_runtime_contract.py`
  - verifies `ToolRuntime.confirm_fn()`.
  - verifies base `Tool.execute_with_runtime()` preserves legacy behavior.

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
# 23 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
# 36 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 85 passed
```

Boundary check from repo root:

```bash
rg -n "opencollab\\.tools\\.safety|SandboxInterceptor|opencollab\\.bootstrap\\.safety" \
  opencollab/opencollab/tools/base.py \
  opencollab/opencollab/tools/bash.py \
  opencollab/opencollab/tools/fs.py \
  opencollab/opencollab/tools/human.py \
  opencollab/opencollab/tools/mcp.py \
  opencollab/opencollab/core/session \
  opencollab/opencollab/team
# no matches
```

## Remaining Problem

The runtime-aware dispatch path exists, but all built-in tools still implement
their actual behavior in legacy `execute(...)` methods.

Current shape:

```text
ToolCallProcessor
  -> tool.execute_with_runtime(args, runtime)
      -> base Tool.execute_with_runtime(...)
          -> concrete_tool.execute(args, env=..., interceptor=..., confirm_fn=...)
```

This keeps compatibility, but built-in tools still consume leaked runtime
plumbing instead of the application-owned `ToolRuntime`.

## Goal

Migrate the first built-in tool to native `ToolRuntime` while preserving the
legacy wrapper.

Start with `BashTool` because it exercises all three runtime concerns:

- environment execution;
- safety policy command checks;
- permission callback for risky commands.

After this step:

- `BashTool.execute_with_runtime(params, runtime)` owns the real behavior.
- `BashTool.execute(...)` remains only as a compatibility wrapper.
- Bash tool behavior and schema are unchanged.
- Other tools stay on the base compatibility path for now.

## Implementation Plan

1. Strengthen the BashTool characterization tests.

   Extend `opencollab/tests/test_tool_runtime_contract.py` before editing
   behavior:

   - direct `BashTool.execute(...)` still works with legacy arguments;
   - direct `BashTool.execute_with_runtime(...)` works with `ToolRuntime`;
   - no-env runtime still returns
     `"Error: no execution environment available."`;
   - safety check happens before environment execution;
   - permission callback from `ToolRuntime.confirm_fn()` is passed into
     `check_cmd_interactive()`;
   - blocked commands still raise `PermissionError` before execution.

2. Move BashTool behavior into `execute_with_runtime()`.

   In `opencollab/opencollab/tools/bash.py`:

   - Import `ToolRuntime`.
   - Add a concrete `execute_with_runtime(self, params, runtime)` method.
   - Use:

     ```python
     env = runtime.environment
     safety_policy = runtime.safety_policy
     confirm_fn = runtime.confirm_fn()
     ```

   - Preserve the existing command, timeout, safety, execution, and output
     formatting behavior exactly.

3. Keep legacy `execute(...)` as a wrapper.

   In `BashTool.execute(...)`:

   - Construct a `ToolRuntime` from the legacy arguments.
   - Wrap `confirm_fn` in `CallbackPermissionPolicy` or a tiny local adapter if
     a permission policy object is required.

   Preferred small shape:

   ```python
   permission_policy = CallbackPermissionPolicy(confirm_fn) if confirm_fn else None
   runtime = ToolRuntime(
       environment=env,
       safety_policy=interceptor,
       permission_policy=permission_policy,
   )
   return await self.execute_with_runtime(params, runtime)
   ```

   If importing `CallbackPermissionPolicy` from `core.session.tools` would
   create an undesirable dependency from tools back to core/session, do not do
   that. Instead add a tiny application-level adapter or a `ToolRuntime`
   helper/factory that accepts a raw callback. The key rule is:

   ```text
   tools -> application is okay
   tools -> core.session is not okay
   ```

4. Prefer an application-owned callback adapter if needed.

   If a callback adapter is needed, add it outside `core.session`:

   - either `opencollab/opencollab/application/permissions.py`
   - or a very small `CallbackPermissionPort` in
     `opencollab/opencollab/application/tool_runtime.py`

   Keep the dependency direction clean:

   ```text
   tools.bash -> application.tool_runtime
   application.* -> no tools/core/session/bootstrap imports
   ```

5. Add a boundary assertion.

   Add or extend tests to assert `tools/bash.py` does not import
   `opencollab.core.session`.

   This prevents solving the compatibility wrapper by reaching back into
   `CallbackPermissionPolicy`.

6. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary checks from repo root:

   ```bash
   rg -n "opencollab\\.core\\.session" opencollab/opencollab/tools/bash.py
   rg -n "opencollab\\.tools\\.safety|SandboxInterceptor" \
     opencollab/opencollab/tools/base.py \
     opencollab/opencollab/tools/bash.py \
     opencollab/opencollab/core/session
   ```

   Both checks should return no matches.

## Acceptance Criteria

- `BashTool` has a concrete `execute_with_runtime(params, runtime)` method.
- Bash command behavior is unchanged.
- Bash command safety behavior is unchanged.
- Bash permission callback behavior is unchanged.
- Legacy `BashTool.execute(...)` still works.
- `tools/bash.py` does not import `core.session`, `bootstrap`, or
  `tools.safety`.
- `ToolCallProcessor` behavior remains unchanged.
- Full test suite remains green.

## Non-Goals

- Do not migrate `FileReadTool`, `FileWriteTool`, `GrepTool`, `AskUserTool`, or
  `MCPTool` in this step.
- Do not remove the base `Tool.execute_with_runtime()` compatibility adapter.
- Do not remove legacy `Tool.execute(...)`.
- Do not change risky command prompt text.
- Do not change grep path-safety behavior.
- Do not change CLI/TUI behavior.

## Next After This

After BashTool is native-runtime, continue with filesystem tools:

1. `FileReadTool.execute_with_runtime()`
2. `FileWriteTool.execute_with_runtime()`
3. `GrepTool.execute_with_runtime()`

Keep each migration small and behavior-characterized before changing code.
