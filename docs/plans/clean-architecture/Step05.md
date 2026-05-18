# Step05 - CA-03 Runtime-Aware Tool Dispatch

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Current Code Diff Review

The current Step04 code has introduced the first application-owned tool runtime
contracts:

- `opencollab/opencollab/application/ports.py`
  - `EnvironmentPort`
  - `SafetyPolicyPort`
  - `SafetyPolicyFactory`
  - `PermissionPort`
  - `ToolPort`
- `opencollab/opencollab/application/tool_runtime.py`
  - `ToolRuntime`
- `opencollab/opencollab/core/session/tools.py`
  - builds `ToolRuntime` from `env`, `safety_policy`, and `permission_policy`.
- `opencollab/opencollab/tools/base.py`, `bash.py`, `fs.py`, `human.py`,
  and `mcp.py`
  - no longer type against `opencollab.tools.safety.SandboxInterceptor`.
  - use `EnvironmentPort` and `SafetyPolicyPort` for type hints.
- `opencollab/tests/test_tool_runtime_contract.py`
  - characterizes current bash/fs/grep/ask-user runtime behavior before the
    public tool execution contract changes.

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
# 18 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_bootstrap.py tests/test_team_decomposition.py tests/test_session_characterization.py -q
# 47 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 80 passed
```

Boundary check from repo root:

```bash
rg -n "opencollab\\.tools\\.safety|SandboxInterceptor" \
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

`ToolRuntime` exists, but `ToolCallProcessor` still unwraps it back into the
legacy public tool signature:

```python
await tool.execute(
    args,
    env=runtime.environment,
    interceptor=runtime.safety_policy,
    confirm_fn=runtime.permission_policy.confirm if runtime.permission_policy else None,
)
```

So the application runtime boundary is only partially useful. The old contract
is still owned by the session processor, and `ToolPort` still exposes the legacy
`env/interceptor/confirm_fn` shape.

## Goal

Introduce a runtime-aware tool dispatch path while preserving compatibility for
existing tools.

After this step:

- `ToolCallProcessor` dispatches tools through `ToolRuntime`.
- The compatibility shim from `ToolRuntime` to legacy keywords lives in the
  tool abstraction or a small adapter, not inline in `ToolCallProcessor`.
- `ToolPort` describes the runtime-aware contract, not the legacy keyword
  contract.
- Existing built-in tools and tests keep working.
- Third-party/MCP-style tools that only implement `execute(...)` keep working
  through a fallback shim.

## Implementation Plan

1. Add a convenience method to `ToolRuntime`.

   In `opencollab/opencollab/application/tool_runtime.py`, add:

   ```python
   def confirm_fn(self):
       if self.permission_policy is None:
           return None
       return self.permission_policy.confirm
   ```

   Keep `ToolRuntime` adapter-free. It should still import only application
   ports and standard-library modules.

2. Change `ToolPort` to describe runtime-aware execution.

   In `opencollab/opencollab/application/ports.py`:

   - Use a `TYPE_CHECKING` import or a string annotation for `ToolRuntime` to
     avoid import cycles.
   - Prefer this shape:

     ```python
     class ToolPort(Protocol):
         name: str
         description: str
         parameters: dict[str, Any]

         def to_openai_schema(self) -> dict[str, Any]:
             ...

         async def execute_with_runtime(
             self,
             params: dict[str, Any],
             runtime: "ToolRuntime",
         ) -> str:
             ...
     ```

   - Keep the legacy `execute(...)` method off `ToolPort`.

   Concrete tools may still have `execute(...)`; it is now a compatibility
   detail, not the application port contract.

3. Add runtime dispatch to the base `Tool`.

   In `opencollab/opencollab/tools/base.py`:

   - Import `ToolRuntime`.
   - Add:

     ```python
     async def execute_with_runtime(self, params, runtime):
         return await self.execute(
             params,
             env=runtime.environment,
             interceptor=runtime.safety_policy,
             confirm_fn=runtime.confirm_fn(),
         )
     ```

   - Keep the existing `execute(...)` method unchanged for compatibility.

   This makes every built-in subclass runtime-dispatchable without changing
   all tool implementations in one patch.

4. Update `ToolCallProcessor` to use runtime dispatch.

   In `opencollab/opencollab/core/session/tools.py`:

   - Build `runtime = self._tool_runtime()`.
   - Prefer `tool.execute_with_runtime(args, runtime)`.
   - Keep a compatibility fallback for non-`Tool` duck-typed tools:

     ```python
     execute_with_runtime = getattr(tool, "execute_with_runtime", None)
     if execute_with_runtime is not None:
         result = await execute_with_runtime(args, runtime)
     else:
         result = await tool.execute(
             args,
             env=runtime.environment,
             interceptor=runtime.safety_policy,
             confirm_fn=runtime.confirm_fn(),
         )
     ```

   The fallback is temporary. It should be small and isolated.

5. Add focused tests for dispatch behavior.

   Extend `opencollab/tests/test_tool_call_processor_interceptor.py` or add a
   new focused test file.

   Test cases:

   - A tool implementing `execute_with_runtime()` receives the same
     `ToolRuntime` built by `ToolCallProcessor`.
   - A legacy tool implementing only `execute(...)` still receives:
     - `env`
     - `interceptor` / safety policy
     - `confirm_fn`
   - Existing `FakeTool` behavior in `test_session_characterization.py` still
     passes.

6. Keep tool behavior tests green.

   `opencollab/tests/test_tool_runtime_contract.py` should still pass without
   rewriting the built-in tools yet. It is guarding the legacy behavior while
   dispatch changes underneath.

7. Add a boundary assertion for the processor.

   Add a test or source check that makes this direction explicit:

   - `ToolCallProcessor` has a runtime dispatch path.
   - The inline legacy keyword expansion is only in the fallback path, not the
     default path.

   Do not overfit this to exact formatting. Prefer behavior tests over fragile
   source-string checks where possible.

8. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

## Acceptance Criteria

- `ToolRuntime` exposes a single compatibility path for `confirm_fn`.
- `ToolPort` no longer advertises the legacy `execute(params, env,
  interceptor, confirm_fn)` signature.
- `Tool` has `execute_with_runtime(params, runtime)`.
- `ToolCallProcessor` dispatches through `execute_with_runtime()` when present.
- Legacy-only tool objects still work through an isolated fallback.
- Built-in tool schemas are unchanged.
- Tool runtime characterization tests remain green.
- Full test suite remains green.

## Non-Goals

- Do not rewrite all built-in tools to use `ToolRuntime` internally yet.
- Do not remove legacy `Tool.execute(...)` yet.
- Do not rename the legacy `interceptor` keyword yet.
- Do not change grep path-safety behavior yet.
- Do not change CLI/TUI behavior.
- Do not split team/session event contracts in this step.

## Next After This

After Step05, migrate built-in tools one by one to native `ToolRuntime`
implementations:

1. `BashTool`
2. `FileReadTool`
3. `FileWriteTool`
4. `GrepTool`
5. `AskUserTool`
6. `MCPTool`

Each migration should keep the legacy `execute(...)` wrapper until external
tool compatibility is explicitly retired.
