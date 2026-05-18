# Step03 - Remove Team Dependency On Bootstrap Safety

Date: 2026-05-18
Branch: `refactor/step01-bootstrap`

## Current Code Diff Review

The current CA-02/Step02 code is behaviorally green:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py tests/test_bootstrap.py tests/test_team_decomposition.py -q
# 14 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
# 35 passed

OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
# 64 passed
```

Boundary check:

```bash
rg -n "opencollab\\.bootstrap\\.safety|from opencollab\\.tools\\.safety import SandboxInterceptor|SandboxInterceptor" opencollab/opencollab/core/session
# no matches
```

What is now clean:

- `core.session.tools` no longer imports `opencollab.tools.safety`.
- `core.session.session` no longer imports `opencollab.bootstrap.safety`.
- `ToolCallProcessor` depends on `SafetyPolicyPort`.
- `Session` accepts an explicit `safety_policy` and passes it into
  `ToolCallProcessor`.
- Chat composition passes a workspace-rooted safety policy into `Session`.
- Team lead and teammate sessions still get workspace/worktree-rooted safety.

Remaining architectural smell:

- `opencollab/opencollab/team/orchestrator.py` imports
  `opencollab.bootstrap.safety.build_workspace_safety_policy`.
- `opencollab/opencollab/team/teammate_factory.py` imports
  `opencollab.bootstrap.safety.build_workspace_safety_policy`.

That preserves behavior, but dependency direction is still wrong:

```text
team -> bootstrap -> tools.safety
```

Team runtime code should not depend on bootstrap. Bootstrap should wire team
runtime dependencies.

## Goal

Finish the CA-02 wiring boundary by removing `team -> bootstrap.safety`.

After this step:

- `core.session` depends on application ports only.
- `team` depends on application ports only for safety policy.
- `bootstrap` remains the place that knows concrete `SandboxInterceptor`
  construction.
- Chat, team lead, and teammate sessions still receive workspace/worktree-rooted
  safety in production composition.

## Key Design Decision

Use an injected safety-policy factory.

Add a small application-level callable type or protocol:

```python
SafetyPolicyFactory = Callable[[Any], SafetyPolicyPort | None]
```

Then:

- `bootstrap.session_factory.build_team()` passes
  `build_workspace_safety_policy` into `Team`.
- `Team` stores the injected factory and uses it for the lead session.
- `TeammateConfig` carries the same factory to `build_teammate_session()`.
- `build_teammate_session()` calls the injected factory for its environment.

This avoids importing bootstrap from team code while keeping construction local
and reviewable.

## Implementation Plan

1. Add the factory contract.

   In `opencollab/opencollab/application/ports.py`:

   - Add `SafetyPolicyFactory` as a type alias.
   - Keep it dependency-light. If possible, use only standard-library typing
     plus `SafetyPolicyPort`.

   Suggested shape:

   ```python
   from typing import Any, Callable

   SafetyPolicyFactory = Callable[[Any], SafetyPolicyPort | None]
   ```

2. Update `Team` to accept the factory.

   In `opencollab/opencollab/team/orchestrator.py`:

   - Import `SafetyPolicyFactory` from `opencollab.application.ports`.
   - Add constructor parameter
     `safety_policy_factory: SafetyPolicyFactory | None = None`.
   - Store it on `self`.
   - Remove the local import of `build_workspace_safety_policy`.
   - For the lead session, derive:

     ```python
     lead_safety_policy = (
         safety_policy_factory(lead_runtime_env)
         if safety_policy_factory is not None
         else None
     )
     ```

   - Pass `lead_safety_policy` into `Session`.

   This makes direct `Team(...)` construction safe but explicit: no factory
   means no automatically derived sandbox policy.

3. Update teammate config and factory.

   In `opencollab/opencollab/team/teammate_factory.py`:

   - Add `safety_policy_factory: SafetyPolicyFactory | None` to
     `TeammateConfig`.
   - Remove the local import of `build_workspace_safety_policy`.
   - In `build_teammate_session()`, derive:

     ```python
     safety_policy = (
         cfg.safety_policy_factory(env)
         if cfg.safety_policy_factory is not None
         else None
     )
     ```

   - Pass `safety_policy=safety_policy` into `Session`.

4. Wire the factory from bootstrap.

   In `opencollab/opencollab/bootstrap/session_factory.py`:

   - Import `build_workspace_safety_policy` if not already imported.
   - When building `Team`, pass:

     ```python
     safety_policy_factory=build_workspace_safety_policy
     ```

   Chat session construction can keep directly calling
   `build_workspace_safety_policy(env)`.

5. Update tests.

   Add or adjust tests to make the new contract explicit:

   - `build_team(ctx, ...)` wires workspace safety policy for the lead session.
   - `Team(..., safety_policy_factory=...)` wires lead safety without importing
     bootstrap.
   - `build_teammate_session(..., cfg=TeammateConfig(...,
     safety_policy_factory=...))` wires teammate safety.
   - Boundary test:

     ```bash
     rg -n "opencollab\\.bootstrap\\.safety" opencollab/opencollab/team
     # no matches
     ```

   Keep concrete `SandboxInterceptor` assertions in tests only.

6. Decide and pin direct public facade behavior.

   Direct construction behavior changed during Step02:

   ```python
   Session(agent, env=LocalEnvironment(...))
   ```

   no longer automatically derives a sandbox policy unless one is supplied.

   For this phase, prefer the explicit contract:

   - direct `Session` does not construct concrete safety adapters;
   - production composition must pass `safety_policy`;
   - tests that need safety should pass it explicitly or use bootstrap builders.

   Add a focused characterization test if needed so this is intentional.

7. Verify.

   From `opencollab/`:

   ```bash
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_tool_call_processor_interceptor.py tests/test_bootstrap.py tests/test_team_decomposition.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/test_session_characterization.py -q
   OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
   ```

   Boundary checks from repo root:

   ```bash
   rg -n "opencollab\\.bootstrap\\.safety" opencollab/opencollab/core/session opencollab/opencollab/team
   rg -n "from opencollab\\.tools\\.safety import SandboxInterceptor" opencollab/opencollab/core/session opencollab/opencollab/team
   ```

   Both checks should return no matches.

## Acceptance Criteria

- `core.session` has no dependency on `opencollab.tools.safety`.
- `core.session` has no dependency on `opencollab.bootstrap.safety`.
- `team` has no dependency on `opencollab.bootstrap.safety`.
- `Team` receives safety wiring through an injected factory.
- `TeammateConfig` carries the safety factory to delegated sessions.
- `bootstrap` remains the only layer in this path that imports
  `SandboxInterceptor`.
- Chat, team lead, and teammate sessions still get workspace/worktree-rooted
  safety when built through bootstrap composition.
- Full test suite remains green.

## Non-Goals

- Do not rename `Tool.execute(..., interceptor=...)` yet.
- Do not migrate tool implementations to `ToolPort` yet.
- Do not introduce the full application use-case layer yet.
- Do not change blocked/risky command patterns or confirmation prompts.
- Do not change team event semantics.
- Do not change CLI/TUI behavior.

## Next After This

After this patch, CA-02 should be architecturally complete enough to commit.

Then start CA-03:

- characterize bash/fs/human tool runtime behavior;
- introduce a cleaner tool execution context or runtime ports;
- gradually remove public `interceptor` plumbing from tool execution.
