# Step01 - CA-02 Cut `core.session.tools -> tools.safety`

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Goal

Remove the direct dependency from `opencollab.core.session.tools` to
`opencollab.tools.safety.SandboxInterceptor` while preserving current tool
safety behavior.

This step is intentionally narrow. It does not redesign all tools yet, and it
does not change tool schemas, CLI behavior, TUI behavior, or provider behavior.

## Current Evidence

Current violation:

- `opencollab/opencollab/core/session/tools.py` imports
  `opencollab.tools.safety.SandboxInterceptor`.
- `ToolCallProcessor.__init__()` derives a concrete `SandboxInterceptor` from
  `env.workspace`.
- `ToolCallProcessor._execute_tool()` passes the concrete interceptor into
  `Tool.execute(..., interceptor=...)`.

Existing characterization coverage:

- `opencollab/tests/test_tool_call_processor_interceptor.py`
  verifies that `ToolCallProcessor` derives a workspace-rooted interceptor from
  `LocalEnvironment`.
- The same test verifies that an explicit interceptor object is preserved.

Related but out of scope for this step:

- Tool implementations under `opencollab/opencollab/tools/` still type against
  `SandboxInterceptor` under `TYPE_CHECKING`.
- Team code still mentions `SandboxInterceptor` in type hints and docs.
- `Tool.execute(params, env, interceptor, confirm_fn)` is still a leaky runtime
  contract. That is CA-03.

## Target Shape For This Step

Add a small application-owned safety protocol and make `ToolCallProcessor`
depend on that protocol instead of the concrete sandbox class.

Planned shape:

```text
opencollab.application.ports
  SafetyPolicyPort
    check_path(target_path: str) -> str
    check_cmd(cmd: str) -> None
    is_risky(cmd: str) -> bool
    check_cmd_interactive(cmd: str, confirm_fn: ...) -> None

opencollab.tools.safety
  SandboxInterceptor implements SafetyPolicyPort structurally

opencollab.core.session.tools
  imports SafetyPolicyPort only
  accepts safety_policy: SafetyPolicyPort | None
  keeps interceptor compatibility as a temporary alias if needed
```

Dependency direction after this step:

```text
core.session.tools -> application.ports
tools.safety       -> application.ports or structural compatibility only
bootstrap/session  -> may still choose the concrete sandbox adapter
```

The important acceptance condition is that `core.session.tools` no longer
imports `opencollab.tools.safety`.

## Implementation Plan

1. Add the port module.

   Create `opencollab/opencollab/application/__init__.py` and
   `opencollab/opencollab/application/ports.py`.

   Add `SafetyPolicyPort` as a `Protocol` with the current surface actually
   used by bash/fs tools:

   - `check_path(target_path: str) -> str`
   - `check_cmd(cmd: str) -> None`
   - `is_risky(cmd: str) -> bool`
   - `check_cmd_interactive(cmd, confirm_fn=None) -> Awaitable[None]`

   Keep this file dependency-light: standard-library typing only.

2. Add a narrow factory outside `core.session.tools`.

   Preferred small patch:

   - Add `opencollab/opencollab/bootstrap/safety.py`.
   - Implement `build_workspace_safety_policy(env) -> SafetyPolicyPort | None`.
   - The factory imports `SandboxInterceptor` and derives it from
     `env.workspace`.

   This moves concrete sandbox creation out of `ToolCallProcessor` without
   forcing a broad composition-root rewrite.

3. Update `ToolCallProcessor`.

   In `opencollab/opencollab/core/session/tools.py`:

   - Replace the `SandboxInterceptor` import with `SafetyPolicyPort`.
   - Add a constructor argument named `safety_policy:
     SafetyPolicyPort | None = None`.
   - Keep `interceptor: SafetyPolicyPort | None = None` temporarily for
     compatibility with existing tests and call sites.
   - Normalize both names internally to one attribute.

   Compatibility detail:

   - Keep `self.interceptor` for now because existing tools still receive an
     argument named `interceptor`, and existing tests assert on
     `proc.interceptor`.
   - Add `self.safety_policy = ...` as the cleaner internal name.
   - Pass `interceptor=self.safety_policy` to existing tools until CA-03
     changes the tool runtime contract.

4. Update `Session` construction.

   In `opencollab/opencollab/core/session/session.py`:

   - Use the new bootstrap factory to derive `safety_policy` from `env`.
   - Pass `safety_policy=...` into `ToolCallProcessor`.

   This keeps the old behavior:

   - If `env.workspace` exists, tools get workspace-rooted safety.
   - If no workspace exists, no safety policy is attached.
   - Explicit injected interceptor/policy still wins for tests or future
     call sites.

5. Update tests.

   Edit `opencollab/tests/test_tool_call_processor_interceptor.py` to keep the
   behavior checks but assert the architecture boundary too:

   - Existing derived workspace behavior still passes.
   - Existing explicit interceptor behavior still passes.
   - Add a test or import check proving `opencollab.core.session.tools` does
     not expose/import `SandboxInterceptor`.

   If needed, add a small fake `SafetyPolicyPort` implementation to verify that
   `ToolCallProcessor` accepts policy-shaped objects without depending on the
   concrete sandbox class.

6. Run verification.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

## Acceptance Criteria

- `rg "from opencollab.tools.safety import SandboxInterceptor" opencollab/opencollab/core/session`
  returns no matches.
- `ToolCallProcessor` depends on `SafetyPolicyPort`, not `SandboxInterceptor`.
- Workspace-derived path safety behavior remains unchanged.
- Explicit safety/interceptor injection remains compatible.
- Existing tools still receive the runtime object they expect through the
  current `interceptor=` parameter.
- Full test suite remains green.

## Non-Goals

- Do not rename the public tool execution keyword from `interceptor` yet.
- Do not move bash/fs/human/mcp tool implementations.
- Do not split all application/domain packages in this step.
- Do not change safety rules, risky command prompts, blocked command patterns,
  or workspace path behavior.
- Do not change CLI/TUI user-visible behavior.

## Rollback Plan

This should be a small patch. If behavior changes unexpectedly:

1. Keep the new `SafetyPolicyPort`.
2. Revert only the construction move.
3. Restore deriving `SandboxInterceptor` at the previous call site.
4. Re-run the characterization test to identify the behavior difference before
   attempting the extraction again.
