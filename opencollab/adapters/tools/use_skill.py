"""The ``use_skill`` dispatcher tool.

One generic dispatcher serves *all* skills: the model reads the catalog (injected
into the system prompt by ``ContextBuilder.build_plan``) and calls
``use_skill(name)`` to pull a skill's full instruction body into its context.

The store is a construction-time dependency, injected via the factory exactly
like the scheduler-bound tools take a ``SchedulerPort`` — a global, stateless
catalog is a construction dep, not a per-execution runtime capability, so
``ToolRuntime`` stays lean (mirrors ``adapters/tools/spawn.py``).

Delivery is *option A* (design §1): the body IS the tool result, tail-appended
at the natural tool-result position so the cached system prefix stays intact.
The store is the single size-cap site (design decision #5), so this tool trusts
the body it gets back and never re-caps.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.base import Tool
from opencollab.application.ports import SkillStorePort
from opencollab.application.tool_execution import ToolRuntime


class UseSkillTool(Tool):
    """Loads a catalog skill's full instructions into the agent's context."""

    name = "use_skill"
    description = (
        "Load the full instructions for a named skill into your context. Call "
        "this when a skill from the catalog matches the task at hand; the skill's "
        "complete instructions are returned to you as this tool call's result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact skill name from the catalog.",
            },
        },
        "required": ["name"],
    }

    def __init__(self, store: SkillStorePort) -> None:
        self._store = store

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        name = params.get("name", "")
        body = self._store.get_body(name)
        if body is None:
            available = ", ".join(m.name for m in self._store.list_manifests())
            if not available:
                return (
                    f"Unknown skill '{name}'. No skills are available in the catalog."
                )
            return f"Unknown skill '{name}'. Available skills: {available}"
        # Option A: the body IS the tool result — tail-appended, cache-friendly.
        return body


__all__ = ["UseSkillTool"]
