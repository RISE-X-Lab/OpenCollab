from __future__ import annotations

from typing import Any, Awaitable, Callable

from opencollab.application.event_bus import EventBus
from opencollab.application.ports import PermissionPort, SafetyPolicyPort
from opencollab.application.tool_dispatch import execute_tool_with_runtime
from opencollab.application.tool_execution import (
    MAX_SIMILAR_CALLS,
    MAX_TOOL_OUTPUT_CHARS,
    ToolExecutionEventFactory,
    ToolExecutionUseCase,
)
from opencollab.application.tool_runtime import ToolRuntime
from opencollab.domain.events import SessionRuntimeEvent as SessionEvent
from opencollab.domain.session import SessionState
from opencollab.domain.tools import MAX_CALL_HASH_WINDOW, ToolProcessingResult


PermissionPolicy = PermissionPort
"""Legacy alias for PermissionPort.

Production code should import ``PermissionPort`` from
``opencollab.application.ports``. This alias is kept so
``from opencollab.core.session import PermissionPolicy`` continues to work for
legacy callers and characterization tests.
"""


class CallbackPermissionPolicy:
    def __init__(self, confirm_fn: Callable[[str], Awaitable[bool]]):
        self._confirm_fn = confirm_fn

    async def confirm(self, prompt: str) -> bool:
        return await self._confirm_fn(prompt)


class ToolCallProcessor:
    def __init__(
        self,
        *,
        agent: Any,
        env: Any,
        state: SessionState,
        event_bus: EventBus,
        tracer: Any = None,
        permission_policy: PermissionPolicy | None = None,
        safety_policy: SafetyPolicyPort | None = None,
        interceptor: SafetyPolicyPort | None = None,
    ):
        self.agent = agent
        self.env = env
        self.state = state
        self.event_bus = event_bus
        self.tracer = tracer
        self.permission_policy = permission_policy
        self.safety_policy = safety_policy if safety_policy is not None else interceptor
        # Compatibility alias for older tests/call sites; new code should use safety_policy.
        self.interceptor = self.safety_policy
        self._tool_execution = ToolExecutionUseCase(
            agent=self.agent,
            environment=self.env,
            state=self.state,
            event_publisher=self.event_bus,
            event_factory=self._event_factory(),
            tracer=self.tracer,
            permission_policy=self.permission_policy,
            safety_policy=self.safety_policy,
            dispatch_tool=execute_tool_with_runtime,
        )

    async def process(self, tool_calls: list[dict]) -> ToolProcessingResult:
        return await self._tool_execution.process(tool_calls)

    def _parse_tool_args(self, func: dict) -> dict:
        return self._tool_execution.parse_tool_args(func)

    def _tool_call_hash(self, tool_name: str, args: dict) -> str:
        return self._tool_execution.tool_call_hash(tool_name, args)

    def _count_recent_similar_calls(self, recent_call_hashes: list[str], call_hash: str) -> int:
        return self._tool_execution.count_recent_similar_calls(recent_call_hashes, call_hash)

    def _find_tool(self, tool_name: str):
        return self._tool_execution.find_tool(tool_name)

    async def _execute_tool(self, tool, args: dict) -> tuple[str, float]:
        return await self._tool_execution.execute_tool(tool, args)

    def _tool_runtime(self) -> ToolRuntime:
        return self._tool_execution.tool_runtime()

    def _truncate_tool_result(self, result: str) -> str:
        return self._tool_execution.truncate_tool_result(result)

    def _tool_result_message(self, tool_id: str, result: str) -> dict[str, str]:
        return self._tool_execution.tool_result_message(tool_id, result)

    def _event_factory(self) -> ToolExecutionEventFactory:
        return ToolExecutionEventFactory(
            loop_detected=lambda tool, count: SessionEvent(
                type="loop_detected",
                data={"tool": tool, "count": count},
            ),
            tool_start=lambda tool, args: SessionEvent(
                type="tool_start",
                data={"tool": tool, "args": args},
            ),
            tool_end=lambda tool, latency: SessionEvent(
                type="tool_end",
                data={"tool": tool, "latency": latency},
            ),
        )
