# Step02 - Finish CA-02 Safety Boundary Wiring

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Current Code Diff Review

The current CA-02 patch has made the main cut:

- `opencollab/opencollab/application/ports.py` introduces
  `SafetyPolicyPort`.
- `opencollab/opencollab/bootstrap/safety.py` builds a workspace-rooted
  `SandboxInterceptor` behind that port.
- `opencollab/opencollab/core/session/tools.py` no longer imports
  `SandboxInterceptor`; it accepts `safety_policy` and keeps `interceptor` as a
  temporary compatibility alias.
- `opencollab/opencollab/core/session/session.py` accepts `safety_policy` and
  passes it to `ToolCallProcessor`.
- `opencollab/tests/test_tool_call_processor_interceptor.py` now covers
  explicit `safety_policy`, explicit legacy `interceptor`, and absence of a
  concrete sandbox symbol in `core.session.tools`.

Verification already run from `opencollab/`:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
# 4 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 59 passed
```

Architecture check already run:

```bash
rg -n "from opencollab\\.tools\\.safety import SandboxInterceptor|SandboxInterceptor" opencollab/opencollab/core/session
# no matches
```

## Remaining Problem

The main `ToolCallProcessor -> SandboxInterceptor` dependency is gone, but the
current patch introduced a new boundary smell:

- `Session._build_runtime()` imports
  `opencollab.bootstrap.safety.build_workspace_safety_policy`.

That keeps behavior green, but it points dependency direction outward from
`core.session` to `bootstrap`. For Clean Architecture, bootstrap should depend
on core/application contracts, not the reverse.

This step should finish CA-02 before starting CA-03.

## Goal

Move safety-policy creation out of `Session._build_runtime()` and into the
existing composition paths, while preserving current CLI/TUI/team behavior.

After this step:

- `core.session.session` imports `SafetyPolicyPort`, but not `bootstrap`.
- `core.session.tools` imports `SafetyPolicyPort`, but not `tools.safety`.
- Concrete `SandboxInterceptor` creation lives in composition/wiring code.
- Existing production session builders pass a safety policy explicitly.

## Implementation Plan

1. Characterize the current construction behavior.

   Add or adjust tests before changing wiring:

   - `build_chat_session(ctx)` gives its `Session.tool_processor` a
     workspace-rooted safety policy.
   - `Team(...).lead_session.tool_processor` gets workspace-rooted safety.
   - `build_teammate_session(...)` gets a safety policy rooted to the teammate
     environment/worktree.
   - Direct `ToolCallProcessor(..., safety_policy=...)` and legacy
     `interceptor=...` behavior stays compatible.

   Keep concrete `SandboxInterceptor` assertions in tests only; do not leak the
   type into `core.session`.

2. Remove the `Session -> bootstrap.safety` import.

   In `opencollab/opencollab/core/session/session.py`:

   - Keep constructor parameter `safety_policy: SafetyPolicyPort | None = None`.
   - Remove the local import of `build_workspace_safety_policy`.
   - Do not derive concrete safety inside `_build_runtime()`.
   - Pass `self._safety_policy` directly to `ToolCallProcessor`.

   This makes `Session` depend on the application port only.

3. Push safety policy creation into chat composition.

   In `opencollab/opencollab/bootstrap/session_factory.py`:

   - Import `build_workspace_safety_policy`.
   - Build `safety_policy = build_workspace_safety_policy(env)`.
   - Add `safety_policy=safety_policy` to `common_kwargs`.

   This preserves CLI chat behavior because chat sessions still get a
   workspace-rooted sandbox.

4. Push safety policy creation into team lead composition.

   In `opencollab/opencollab/team/orchestrator.py`:

   - Derive `lead_env` once before building `lead_session_kwargs`.
   - Build a safety policy from that environment.
   - Pass `safety_policy=...` into the lead `Session`.

   Keep this change small. Do not refactor team events or worktree logic in
   this step.

5. Push safety policy creation into teammate composition.

   In `opencollab/opencollab/team/teammate_factory.py`:

   - Build a safety policy from the provided teammate `env`.
   - Pass `safety_policy=...` into the returned `Session`.

   This preserves the existing worktree-rooted safety behavior for delegated
   agents.

6. Clean docs/comments that still describe old ownership.

   Update narrow comments only where they are now misleading:

   - `opencollab/opencollab/bootstrap/runtime.py`
   - `opencollab/opencollab/bootstrap/tool_factory.py`
   - `opencollab/opencollab/team/orchestrator.py`
   - `opencollab/opencollab/team/teammate_factory.py`

   Do not rewrite broader architecture docs in this step.

7. Remove generated cache files before staging.

   The current working tree contains generated Python cache files under the new
   untracked `opencollab/opencollab/application/` package. Remove
   `__pycache__` before staging so only source and docs are committed.

8. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_bootstrap.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary checks from repo root:

   ```bash
   rg -n "opencollab\\.bootstrap\\.safety|SandboxInterceptor" opencollab/opencollab/core/session
   rg -n "from opencollab\\.tools\\.safety import SandboxInterceptor" opencollab/opencollab/core/session
   ```

   Both boundary checks should return no matches.

## Acceptance Criteria

- `core.session.tools` has no dependency on `opencollab.tools.safety`.
- `core.session.session` has no dependency on `opencollab.bootstrap.safety`.
- `Session` remains constructible with an explicit `safety_policy`.
- CLI chat sessions still get workspace-rooted path and command safety.
- Team lead and teammate sessions still get workspace/worktree-rooted safety.
- Existing `interceptor=` injection remains as a temporary compatibility path
  for tests and older call sites.
- Full test suite remains green.

## Non-Goals

- Do not rename the `Tool.execute(..., interceptor=...)` keyword yet.
- Do not introduce `EnvironmentPort`, `PermissionPort`, or `ToolPort` yet.
- Do not change blocked/risky command behavior.
- Do not change event semantics.
- Do not change CLI/TUI output behavior.

## Next After This

Once this boundary is clean and committed, start CA-03:

- characterize bash/fs/human tool runtime behavior;
- introduce a cleaner tool execution context or runtime ports;
- migrate tools away from the public `interceptor` plumbing gradually.
