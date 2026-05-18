from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from opencollab.core.session.events import EventBus, SessionEvent
from opencollab.core.session.state import SessionState
from opencollab.tools.safety import SandboxInterceptor

# Loop detection (ref: opencode doom_loop detection — 3 identical calls)
MAX_SIMILAR_CALLS = 3
MAX_CALL_HASH_WINDOW = 200

# Output truncation for tool results (ref: openclaw truncateOversizedToolResults)
MAX_TOOL_OUTPUT_CHARS = 16_000


class PermissionPolicy(Protocol):
    async def confirm(self, prompt: str) -> bool:
        ...


class CallbackPermissionPolicy:
    def __init__(self, confirm_fn: Callable[[str], Awaitable[bool]]):
        self._confirm_fn = confirm_fn

    async def confirm(self, prompt: str) -> bool:
        return await self._confirm_fn(prompt)


@dataclass
class ToolProcessingResult:
    messages_to_append: list[dict[str, Any]] = field(default_factory=list)
    recent_hash_updates: list[str] = field(default_factory=list)
    loop_detections: list[dict[str, Any]] = field(default_factory=list)

    def apply_to(self, state: SessionState) -> None:
        for message in self.messages_to_append:
            state.append_message(message)
        for call_hash in self.recent_hash_updates:
            state.remember_tool_call_hash(call_hash, max_window=MAX_CALL_HASH_WINDOW)


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
        interceptor: SandboxInterceptor | None = None,
    ):
        self.agent = agent
        self.env = env
        self.state = state
        self.event_bus = event_bus
        self.tracer = tracer
        self.permission_policy = permission_policy
        # Derive from env.workspace if not provided. Tests can inject a fake.
        if interceptor is None and env is not None and getattr(env, "workspace", None):
            interceptor = SandboxInterceptor(env.workspace)
        self.interceptor = interceptor

    async def process(self, tool_calls: list[dict]) -> ToolProcessingResult:
        result = ToolProcessingResult()
        recent_call_hashes = list(self.state.recent_call_hashes)

        for tc in tool_calls:
            func = tc["function"]
            tool_name = func["name"]
            tool_id = tc["id"]

            try:
                args = self._parse_tool_args(func)
            except json.JSONDecodeError:
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: invalid JSON arguments: {func['arguments'][:200]}",
                })
                continue

            call_hash = self._tool_call_hash(tool_name, args)
            result.recent_hash_updates.append(call_hash)
            recent_call_hashes.append(call_hash)
            if len(recent_call_hashes) > MAX_CALL_HASH_WINDOW:
                recent_call_hashes = recent_call_hashes[-MAX_CALL_HASH_WINDOW:]

            recent_same = self._count_recent_similar_calls(recent_call_hashes, call_hash)
            if recent_same >= MAX_SIMILAR_CALLS:
                warning = (
                    f"[Loop detected: tool '{tool_name}' called {recent_same} times with identical arguments. "
                    f"You are stuck in a loop. Try a completely different approach or ask for help.]"
                )
                result.messages_to_append.append({"role": "tool", "tool_call_id": tool_id, "content": warning})
                result.loop_detections.append({"tool": tool_name, "count": recent_same})
                await self.event_bus.emit(
                    SessionEvent(type="loop_detected", data={"tool": tool_name, "count": recent_same})
                )
                continue

            tool = self._find_tool(tool_name)
            if not tool:
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: unknown tool '{tool_name}'. Available: {[t.name for t in self.agent.tools]}",
                })
                continue

            await self.event_bus.emit(SessionEvent(type="tool_start", data={"tool": tool_name, "args": args}))

            tool_output, tool_latency = await self._execute_tool(tool, args)
            tool_output = self._truncate_tool_result(tool_output)

            if self.tracer:
                # Cap result in trace to 4k to keep trajectory files manageable
                trace_result = (
                    tool_output
                    if len(tool_output) <= 4096
                    else tool_output[:2048] + "\n...[truncated]...\n" + tool_output[-2048:]
                )
                self.tracer.log_step(
                    step_type="tool_exec",
                    payload={
                        "tool": tool_name,
                        "args": args,
                        "result_len": len(tool_output),
                        "result": trace_result,
                    },
                    tokens=0,
                    latency=tool_latency,
                )

            result.messages_to_append.append(self._tool_result_message(tool_id, tool_output))
            await self.event_bus.emit(SessionEvent(type="tool_end", data={"tool": tool_name, "latency": tool_latency}))

        return result

    def _parse_tool_args(self, func: dict) -> dict:
        args_str = func["arguments"]
        return json.loads(args_str) if args_str else {}

    def _tool_call_hash(self, tool_name: str, args: dict) -> str:
        return hashlib.md5(json.dumps({"name": tool_name, "args": args}, sort_keys=True).encode()).hexdigest()

    def _count_recent_similar_calls(self, recent_call_hashes: list[str], call_hash: str) -> int:
        # Check for repeated identical calls (ref: opencode doom_loop)
        return sum(1 for h in recent_call_hashes[-MAX_SIMILAR_CALLS * 2 :] if h == call_hash)

    def _find_tool(self, tool_name: str):
        return self.agent.find_tool(tool_name)

    async def _execute_tool(self, tool, args: dict) -> tuple[str, float]:
        start = time.monotonic()
        try:
            result = await tool.execute(
                args,
                env=self.env,
                interceptor=self.interceptor,
                confirm_fn=self._tool_confirm_fn(),
            )
        except PermissionError as e:
            result = f"Permission denied: {e}"
        except Exception as e:
            result = f"Tool execution error: {type(e).__name__}: {e}"

        return result, time.monotonic() - start

    def _tool_confirm_fn(self):
        if self.permission_policy is None:
            return None
        return self.permission_policy.confirm

    def _truncate_tool_result(self, result: str) -> str:
        if len(result) > MAX_TOOL_OUTPUT_CHARS:
            return result[:MAX_TOOL_OUTPUT_CHARS // 2] + \
                f"\n\n... [{len(result) - MAX_TOOL_OUTPUT_CHARS} chars truncated] ...\n\n" + \
                result[-MAX_TOOL_OUTPUT_CHARS // 2:]
        return result

    def _tool_result_message(self, tool_id: str, result: str) -> dict[str, str]:
        return {"role": "tool", "tool_call_id": tool_id, "content": result}
