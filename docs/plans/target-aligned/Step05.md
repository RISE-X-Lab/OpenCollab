# Step05 - Introduce `ToolSpec` and re-tighten `Agent.tools`

Date: 2026-05-20
Branch: open `refactor/step05-toolspec` off the merged Step 04 branch.

## Goal

Add `ToolSpec` as a domain-layer Protocol describing the schema surface every
tool exposes to an agent, and tighten `domain.agent.Agent.tools` from
`list[Any]` back to `list[ToolSpec]`.

This closes the known regression introduced in Step 02 (the `list[Any]`
widening was forced by the boundary test rejecting domain `TYPE_CHECKING`
imports of the concrete `Tool` adapter) and fills the target diagram's
`domain.agent → ToolSpec` slot named in `docs/repomap/repomap-target.puml`.

Like Steps 01–04, this is **purely additive**: one new Protocol, one
annotation change, one new structural test. No behavior, no signatures
on tools or use cases, no class renames.

## Current Evidence

### Why `Agent.tools` is currently `list[Any]`

In Step 02, `core/agent.py` moved to `domain/agent.py`. The original file
had a `TYPE_CHECKING` import of `opencollab.tools.base.Tool`. The repo's
boundary test (`tests/test_domain_boundaries.py:12-28`) rejects any
substring `"opencollab.tools"` (or any other outer-layer prefix) appearing
in any file under `domain/`, including under `TYPE_CHECKING`. So the
import was deleted and the annotation was widened to `Any`.

Today at `opencollab/opencollab/domain/agent.py`:

```python
tools: list[Any] = field(default_factory=list)

def find_tool(self, name: str) -> Any | None: ...
```

### What `Agent` actually needs from each tool

Reading `domain/agent.py`:

- `Agent.tool_schemas()` calls `t.to_openai_schema()`.
- `Agent.find_tool(name)` reads `t.name.lower()`.

That is the full surface: `name: str`, `to_openai_schema() -> dict`. Tools
also carry `description: str` and `parameters: dict[str, Any]` because
`to_openai_schema()` reads them — that part is internal to each tool.

### Why we cannot reuse `ToolPort`

`application/ports.py:52` already defines `ToolPort` with exactly the
right surface plus `execute_with_runtime`. But:

- `ToolPort` lives in `application/`. A domain module cannot import it.
- `ToolPort.execute_with_runtime` takes `ToolRuntime`, which is an
  application concern (`application/tool_runtime.py`). Agent never calls
  it. The execution surface belongs to the application, the schema
  surface belongs to the domain.

The clean split is: **two Protocols, one in each layer, sharing the
schema fields.** Domain owns `ToolSpec` (schema only). Application owns
`ToolPort` (schema + execute). The concrete `Tool` adapter satisfies
both structurally without changing.

### Boundary test surface

`tests/test_domain_boundaries.py:12-28` checks three files
(`session.py`, `tools.py`, `compaction.py`) for forbidden substrings.
`agent.py` and `events.py` are not in the list today; we should extend
the test to cover them so the new `ToolSpec` location stays clean.

## Target Shape For This Step

`opencollab/opencollab/domain/tools.py` gains:

```python
from typing import Any, Protocol


class ToolSpec(Protocol):
    """The schema surface every tool exposes to an agent.

    Domain-side Protocol describing only what an Agent needs at
    configuration time: name + description + parameters + openai schema
    rendering. Tool execution lives in application.ports.ToolPort.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_schema(self) -> dict[str, Any]: ...
```

`domain/agent.py` is updated:

```python
from opencollab.domain.tools import ToolSpec  # new

@dataclass
class Agent:
    ...
    tools: list[ToolSpec] = field(default_factory=list)  # was list[Any]

    def find_tool(self, name: str) -> ToolSpec | None:   # was Any | None
        ...
```

`domain/__init__.py` gains a `ToolSpec` re-export.

No other production file changes. `ToolPort` in `application/ports.py`
stays as-is — it is a runtime-side Protocol that happens to satisfy
`ToolSpec` structurally, and that relationship is captured by tests, not
by inheritance.

Dependency direction after this step:

```text
domain/agent.py -> domain/tools.py  (already in the same layer)
domain/tools.py -> stdlib only
application/*   -> unchanged
adapters/*      -> unchanged
tools/*         -> unchanged (Tool base class structurally satisfies ToolSpec)
```

## Implementation Plan

Single branch, two commits.

### 1. Add `ToolSpec` and tighten `Agent`

- Edit `opencollab/opencollab/domain/tools.py` to add the `ToolSpec`
  Protocol shown above. Append `"ToolSpec"` to its `__all__` if present.
- Edit `opencollab/opencollab/domain/agent.py`:
  - Add `from opencollab.domain.tools import ToolSpec`.
  - Change `tools: list[Any] = ...` → `tools: list[ToolSpec] = ...`.
  - Change `find_tool(...) -> Any | None` → `... -> ToolSpec | None`.
  - Keep the `Any` import only if other annotations still use it; remove
    if unused.
- Edit `opencollab/opencollab/domain/__init__.py` to re-export `ToolSpec`.

Verify:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Commit: `refactor(domain): introduce ToolSpec`.

### 2. Pin the structural relationships and extend the boundary test

Append to `tests/test_domain_boundaries.py`:

- Add `domain/agent.py` and `domain/events.py` to the `domain_files`
  list in `test_domain_modules_do_not_import_outer_layers` so the
  boundary rule covers them.

Append a new test (same file or `tests/test_tool_runtime_contract.py`,
whichever fits the existing structure):

```python
def test_tool_base_satisfies_tool_spec():
    from opencollab.domain.tools import ToolSpec
    from opencollab.tools.base import Tool

    instance: ToolSpec = Tool()  # structural check
    assert isinstance(instance.name, str)
    assert hasattr(instance, "to_openai_schema")
    assert callable(instance.to_openai_schema)


def test_tool_port_satisfies_tool_spec():
    """ToolPort carries strictly more surface than ToolSpec; any ToolPort is a ToolSpec."""
    from opencollab.application.ports import ToolPort
    from opencollab.domain.tools import ToolSpec

    # Both are runtime-checkable structurally; ensure the schema surface lines up.
    spec_attrs = {"name", "description", "parameters", "to_openai_schema"}
    assert spec_attrs.issubset(set(ToolPort.__dict__) | set(ToolSpec.__dict__))
```

Verify:

```bash
OPENAI_API_KEY=fake-test-key uv run pytest tests/ -q
```

Static checks:

```bash
rg "tools: list\[Any\]" opencollab/opencollab/domain/   # expect 0
rg "ToolSpec" opencollab/opencollab/domain/             # expect at least 2 (tools.py, agent.py)
```

Commit: `test(domain): pin ToolSpec boundary and adapter compatibility`.

## Acceptance Criteria

- `ToolSpec` is declared in `opencollab/opencollab/domain/tools.py` as a
  `Protocol`.
- `domain/agent.py:Agent.tools` is annotated `list[ToolSpec]`.
- `domain/agent.py:Agent.find_tool` returns `ToolSpec | None`.
- `tests/test_domain_boundaries.py` covers `agent.py` and `events.py` in
  the forbidden-import check.
- Two new tests pass: `Tool` satisfies `ToolSpec`; `ToolPort` carries the
  `ToolSpec` schema surface.
- No new arrow points outward from `domain/`.
- Full test suite passes (161 before + 2 new = 163 expected; one of the
  two could be folded into an existing file if preferred).

## Non-Goals

- Do **not** make `ToolPort` inherit from `ToolSpec` via Protocol
  inheritance. The two live in different layers; structural compatibility
  asserted by tests is enough and avoids cross-layer Protocol coupling.
- Do **not** rename `Agent` to `AgentProfile`. Separate concern.
- Do **not** rename `ToolPort` to `ToolAdapter` or similar.
- Do **not** alter `Tool` (`tools/base.py`). It already satisfies the new
  Protocol; no changes needed there.
- Do **not** start dissolving `ContextCompactor` / `ToolCallProcessor` /
  `SessionRunner`. Those are Step 06+ territory.
- Do **not** consolidate `tui/session_adapter.py` and `cli/tui.py` under
  `adapters/tui/`. Separate cleanup.

## Rollback Plan

Two commits, each independently revertible.

- Reverting commit 2 drops the structural pins; the runtime behavior is
  unchanged because the Protocol is purely declarative.
- Reverting commit 1 restores `tools: list[Any]` and removes `ToolSpec`.
  Nothing else depends on the new Protocol.

If only the boundary-test extension fails (because `agent.py` or
`events.py` contain an unexpected forbidden substring), drop that
sub-change rather than reverting the whole step — the substring would be
a real layering bug worth a separate fix.
