from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from opencollab.application.ports import (
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SafetyPolicyPort,
    TracePort,
)
from opencollab.application.tool_dispatch import execute_tool_with_runtime
from opencollab.application.tool_runtime import ToolRuntime
from opencollab.domain.session import SessionState
from opencollab.domain.tools import LoopDetection, MAX_CALL_HASH_WINDOW, ToolProcessingResult

# Loop detection (ref: opencode doom_loop detection — 3 identical calls)
MAX_SIMILAR_CALLS = 3
# Output truncation for tool results (ref: openclaw truncateOversizedToolResults)
MAX_TOOL_OUTPUT_CHARS = 16_000


@dataclass(frozen=True)
class ToolExecutionEventFactory:
    loop_detected: Callable[[str, int], Any]
    tool_start: Callable[[str, dict[str, Any]], Any]
    tool_end: Callable[[str, float], Any]


class ToolExecutionUseCase:
    def __init__(
        self,
        *,
        agent: Any,
        environment: EnvironmentPort | None,
        state: SessionState,
        event_publisher: EventPublisherPort,
        event_factory: ToolExecutionEventFactory,
        tracer: TracePort | None = None,
        permission_policy: PermissionPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
        dispatch_tool: Callable[[Any, dict[str, Any], ToolRuntime], Awaitable[str]] = execute_tool_with_runtime,
    ):
        self.agent = agent
        self.environment = environment
        self.state = state
        self.event_publisher = event_publisher
        self.event_factory = event_factory
        self.tracer = tracer
        self.permission_policy = permission_policy
        self.safety_policy = safety_policy
        self.dispatch_tool = dispatch_tool

    async def process(self, tool_calls: list[dict]) -> ToolProcessingResult:
        result = ToolProcessingResult()
        recent_call_hashes = list(self.state.recent_call_hashes)

        for tc in tool_calls:
            func = tc["function"]
            tool_name = func["name"]
            tool_id = tc["id"]

            try:
                args = self.parse_tool_args(func)
            except json.JSONDecodeError:
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: invalid JSON arguments: {func['arguments'][:200]}",
                })
                continue

            call_hash = self.tool_call_hash(tool_name, args)
            result.recent_hash_updates.append(call_hash)
            recent_call_hashes.append(call_hash)
            if len(recent_call_hashes) > MAX_CALL_HASH_WINDOW:
                recent_call_hashes = recent_call_hashes[-MAX_CALL_HASH_WINDOW:]

            recent_same = self.count_recent_similar_calls(recent_call_hashes, call_hash)
            if recent_same >= MAX_SIMILAR_CALLS:
                warning = (
                    f"[Loop detected: tool '{tool_name}' called {recent_same} times with identical arguments. "
                    f"You are stuck in a loop. Try a completely different approach or ask for help.]"
                )
                result.messages_to_append.append({"role": "tool", "tool_call_id": tool_id, "content": warning})
                result.loop_detections.append(LoopDetection(tool=tool_name, count=recent_same))
                await self.event_publisher.emit(self.event_factory.loop_detected(tool_name, recent_same))
                continue

            tool = self.find_tool(tool_name)
            if not tool:
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: unknown tool '{tool_name}'. Available: {[t.name for t in self.agent.tools]}",
                })
                continue

            await self.event_publisher.emit(self.event_factory.tool_start(tool_name, args))

            tool_output, tool_latency = await self.execute_tool(tool, args)
            tool_output = self.truncate_tool_result(tool_output)

            if self.tracer:
                self.tracer.log_step(
                    step_type="tool_exec",
                    payload=self.trace_payload(tool_name, args, tool_output),
                    tokens=0,
                    latency=tool_latency,
                )

            result.messages_to_append.append(self.tool_result_message(tool_id, tool_output))
            await self.event_publisher.emit(self.event_factory.tool_end(tool_name, tool_latency))

        return result

    def parse_tool_args(self, func: dict) -> dict:
        args_str = func["arguments"]
        return json.loads(args_str) if args_str else {}

    def tool_call_hash(self, tool_name: str, args: dict) -> str:
        return hashlib.md5(json.dumps({"name": tool_name, "args": args}, sort_keys=True).encode()).hexdigest()

    def count_recent_similar_calls(self, recent_call_hashes: list[str], call_hash: str) -> int:
        # Check for repeated identical calls (ref: opencode doom_loop)
        return sum(1 for h in recent_call_hashes[-MAX_SIMILAR_CALLS * 2 :] if h == call_hash)

    def find_tool(self, tool_name: str):
        return self.agent.find_tool(tool_name)

    async def execute_tool(self, tool, args: dict) -> tuple[str, float]:
        start = time.monotonic()
        runtime = self.tool_runtime()
        try:
            result = await self.dispatch_tool(tool, args, runtime)
        except PermissionError as e:
            result = f"Permission denied: {e}"
        except Exception as e:
            result = f"Tool execution error: {type(e).__name__}: {e}"

        return result, time.monotonic() - start

    def tool_runtime(self) -> ToolRuntime:
        return ToolRuntime(
            environment=self.environment,
            safety_policy=self.safety_policy,
            permission_policy=self.permission_policy,
        )

    def truncate_tool_result(self, result: str) -> str:
        if len(result) > MAX_TOOL_OUTPUT_CHARS:
            return (
                result[: MAX_TOOL_OUTPUT_CHARS // 2]
                + f"\n\n... [{len(result) - MAX_TOOL_OUTPUT_CHARS} chars truncated] ...\n\n"
                + result[-MAX_TOOL_OUTPUT_CHARS // 2 :]
            )
        return result

    def trace_payload(self, tool_name: str, args: dict, tool_output: str) -> dict[str, Any]:
        # Cap result in trace to 4k to keep trajectory files manageable.
        trace_result = (
            tool_output
            if len(tool_output) <= 4096
            else tool_output[:2048] + "\n...[truncated]...\n" + tool_output[-2048:]
        )
        return {
            "tool": tool_name,
            "args": args,
            "result_len": len(tool_output),
            "result": trace_result,
        }

    def tool_result_message(self, tool_id: str, result: str) -> dict[str, str]:
        return {"role": "tool", "tool_call_id": tool_id, "content": result}
