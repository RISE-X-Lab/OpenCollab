# Step04 - Start CA-03 Tool Runtime Boundary

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Current Code Diff Review

The current Step03 code has implemented the safety factory boundary:

- `opencollab/opencollab/application/ports.py` now exports
  `SafetyPolicyFactory`.
- `opencollab/opencollab/bootstrap/session_factory.py` passes
  `build_workspace_safety_policy` into `Team`.
- `opencollab/opencollab/team/orchestrator.py` accepts
  `safety_policy_factory` and no longer imports `bootstrap.safety`.
- `opencollab/opencollab/team/teammate_factory.py` carries
  `safety_policy_factory` in `TeammateConfig` and uses it for delegated
  sessions.
- Direct `Session(...)`, direct `Team(...)`, and direct
  `build_teammate_session(...)` no longer create concrete safety policy unless
  one is explicitly supplied by composition.

Verification run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py tests/test_bootstrap.py tests/test_team_decomposition.py -q
# 18 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
# 36 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 69 passed
```

Boundary check from repo root:

```bash
rg -n "opencollab\\.bootstrap\\.safety|from opencollab\\.tools\\.safety import SandboxInterceptor|SandboxInterceptor" opencollab/opencollab/core/session opencollab/opencollab/team
# no matches
```

Current judgment:

- CA-02 is now functionally green and architecturally clean enough to commit.
- The next real coupling is CA-03: the public tool runtime contract still leaks
  framework plumbing into every tool call.

## Remaining Coupling

`ToolCallProcessor` still calls tools as:

```python
await tool.execute(
    args,
    env=self.env,
    interceptor=self.safety_policy,
    confirm_fn=self._tool_confirm_fn(),
)
```

Tool implementations still expose this shape:

```python
async def execute(
    self,
    params,
    env=None,
    interceptor=None,
    confirm_fn=None,
) -> str:
```

Specific dependency issues:

- `opencollab.tools.base`, `bash`, `fs`, `human`, and `mcp` type against
  `opencollab.core.env.Environment`.
- Those modules still mention `opencollab.tools.safety.SandboxInterceptor`
  under `TYPE_CHECKING`.
- Safety is now passed as `SafetyPolicyPort`, but the runtime keyword is still
  named `interceptor`.
- Human interaction is still represented as `confirm_fn` rather than an
  application-owned permission/human-input port.
- There is no application-owned `ToolPort`, `EnvironmentPort`, or
  `ToolExecutionContext` yet.

## Goal

Start CA-03 with a narrow, low-risk patch:

1. Characterize current tool runtime behavior.
2. Introduce application-owned runtime contracts.
3. Keep existing tool schemas and external behavior unchanged.
4. Avoid migrating all tool implementations in the same patch.

This step should prepare the codebase for a later migration from:

```text
Tool.execute(params, env, interceptor, confirm_fn)
```

to:

```text
Tool.execute(params, runtime)
```

or an equivalent application-owned `ToolPort` contract.

## Implementation Plan

1. Add characterization tests for current runtime behavior.

   Create or extend focused tests for the actual tools:

   - `BashTool`
     - returns `"Error: no execution environment available."` when `env` is
       absent;
     - calls `safety_policy.check_cmd_interactive(cmd, confirm_fn)` before
       executing;
     - preserves blocked/risky command behavior through `SandboxInterceptor`.

   - `FileReadTool`
     - uses `safety_policy.check_path(path)` before reading;
     - preserves workspace path-jail behavior.

   - `FileWriteTool`
     - uses `safety_policy.check_path(path)` before writing;
     - preserves create and `str_replace` behavior.

   - `GrepTool`
     - preserves current `env.exec_cmd(...)` path for search.
     - Note: it currently does not call `check_path()` on `path`; characterize
       before deciding whether that is a bug or intentional compatibility.

   - `AskUserTool`
     - preserves non-interactive fallback when `confirm_fn is None`.
     - Characterize current prompt path enough to avoid changing TUI/CLI
       behavior accidentally.

   Suggested target file:

   - `opencollab/tests/test_tool_runtime_contract.py`

2. Add application runtime ports without migrating tools yet.

   Extend `opencollab/opencollab/application/ports.py` with small protocols:

   - `EnvironmentPort`
     - `exec_cmd(cmd: str, timeout: float = ...)`
     - `read_file(path: str)`
     - `write_file(path: str, content: str)`

   - `PermissionPort`
     - `confirm(prompt: str) -> Awaitable[bool]`

   - `ToolPort`
     - name/description/parameters attributes;
     - `to_openai_schema()`;
     - keep the execution method deliberately conservative for now.

   The first patch should not force every concrete tool to inherit these
   protocols. Structural typing is enough.

3. Add a runtime context value object.

   Add an application-owned runtime context, likely in
   `opencollab/opencollab/application/tool_runtime.py` or
   `opencollab/opencollab/application/ports.py` if it stays very small:

   ```python
   @dataclass(frozen=True)
   class ToolRuntime:
       environment: EnvironmentPort | None
       safety_policy: SafetyPolicyPort | None
       permission_policy: PermissionPort | None
   ```

   Keep it adapter-free. It should not import `core.env`, `tools.safety`,
   bootstrap, TUI, or concrete terminal classes.

4. Add a compatibility adapter in `ToolCallProcessor`.

   In `opencollab/opencollab/core/session/tools.py`:

   - Build a `ToolRuntime` from existing `env`, `safety_policy`, and
     `permission_policy`.
   - Keep calling current tools with legacy keywords for now:

     ```python
     await tool.execute(
         args,
         env=runtime.environment,
         interceptor=runtime.safety_policy,
         confirm_fn=runtime.permission_policy.confirm if runtime.permission_policy else None,
     )
     ```

   This introduces the application runtime object without changing tool
   behavior in the same patch.

5. Remove concrete sandbox type mentions from tool type hints.

   In `opencollab/opencollab/tools/base.py`, `bash.py`, `fs.py`, `human.py`,
   and `mcp.py`:

   - Replace `TYPE_CHECKING` imports of `SandboxInterceptor` with
     `SafetyPolicyPort`.
   - Prefer `EnvironmentPort` over `core.env.Environment` where it can be done
     without runtime behavior changes.

   Do not rename the public `interceptor` keyword in this step.

6. Add boundary tests.

   Add checks that:

   ```bash
   rg -n "opencollab\\.tools\\.safety|SandboxInterceptor" opencollab/opencollab/tools opencollab/opencollab/core/session opencollab/opencollab/team
   ```

   has no matches outside `opencollab/opencollab/tools/safety.py` and tests.

   If this is too broad for one patch, at minimum require no matches in:

   - `opencollab/opencollab/tools/base.py`
   - `opencollab/opencollab/tools/bash.py`
   - `opencollab/opencollab/tools/fs.py`
   - `opencollab/opencollab/tools/human.py`
   - `opencollab/opencollab/tools/mcp.py`

7. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_runtime_contract.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py tests/test_bootstrap.py tests/test_team_decomposition.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

## Acceptance Criteria

- Current CA-02 boundary remains clean:
  - no `tools.safety` import from `core.session`;
  - no `bootstrap.safety` import from `core.session` or `team`.
- Application layer defines the first runtime ports needed for CA-03.
- `ToolRuntime` exists and is adapter-free.
- `ToolCallProcessor` constructs and uses `ToolRuntime` internally while still
  preserving the legacy tool call keyword contract.
- Concrete tools no longer type against `SandboxInterceptor`.
- Tool schemas are unchanged.
- CLI/TUI-visible behavior is unchanged.
- Full test suite remains green.

## Non-Goals

- Do not rename `interceptor` to `safety_policy` in the public
  `Tool.execute(...)` signature yet.
- Do not require all tools to implement a new `ToolPort` execution method yet.
- Do not change `GrepTool` path-safety behavior until it is characterized and
  explicitly scoped.
- Do not change risky command prompts, blocked command patterns, or yolo-mode
  behavior.
- Do not split event contracts in this step.

## Next After This

After Step04, continue CA-03 with the real migration:

- add a new runtime-aware execution path;
- migrate built-in tools one by one from legacy keywords to `ToolRuntime`;
- keep a compatibility shim for third-party or MCP tools until the public tool
  contract can be changed safely.
