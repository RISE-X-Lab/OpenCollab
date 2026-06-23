from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.ports import (
    AskUserPort,
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SafetyPolicyPort,
    TracePort,
)
from opencollab.domain.session import SessionState
from opencollab.domain.tools import MAX_CALL_HASH_WINDOW, LoopDetection, ToolProcessingResult

# Loop detection (ref: opencode doom_loop detection — 3 identical calls)
MAX_SIMILAR_CALLS = 3

# Read-only, range-parameterized tools whose loop key is the FILE PATH alone, not
# the full args. A model thrashing on one file re-reads it with SHIFTING line
# ranges (sympy-11400 read ccode.py ~135 times), so each exact-arg hash is unique
# and the MAX_SIMILAR_CALLS counter never trips. Collapsing these tools to a
# path-only hash makes the re-reads collide so the loop is caught — at the more
# lenient MAX_SAME_FILE_READS, since a file is legitimately re-read a handful of
# times during distill-as-you-read but dozens of times is a stall.
_PATH_NORMALIZED_TOOLS = frozenset({"file_read"})
MAX_SAME_FILE_READS = 8


@dataclass(frozen=True)
class DeferredCall:
    """Returned by a deferrable tool instead of a result string.

    Signals that the tool handed work off (e.g. spawned a child agent) and
    this call's result arrives later; ``ref`` identifies the deferred work —
    for ``spawn_agent``, the child's aid — and is recorded on the pending row
    that suspends the session until the result is delivered.
    """

    ref: int


@dataclass(frozen=True)
class ToolRuntime:
    environment: EnvironmentPort | None
    safety_policy: SafetyPolicyPort | None
    permission_policy: PermissionPort | None
    ask_policy: AskUserPort | None = None
    aid: int = -1
    tool_call_id: str | None = None

    def confirm_fn(self):
        if self.permission_policy is None:
            return None
        return self.permission_policy.confirm

    def ask_fn(self):
        if self.ask_policy is None:
            return None
        return self.ask_policy.ask


class CallbackPermissionPolicy:
    def __init__(self, confirm_fn: Callable[[str], Awaitable[bool]]):
        self._confirm_fn = confirm_fn

    async def confirm(self, prompt: str) -> bool:
        return await self._confirm_fn(prompt)


class ToolExecutionUseCase:
    def __init__(
        self,
        *,
        agent: Any,
        environment: EnvironmentPort | None,
        state: SessionState,
        event_publisher: EventPublisherPort,
        event_factory: SessionEventFactory | None = None,
        tracer: TracePort | None = None,
        permission_policy: PermissionPort | None = None,
        ask_policy: AskUserPort | None = None,
        safety_policy: SafetyPolicyPort | None = None,
    ):
        self.agent = agent
        self.environment = environment
        self.state = state
        self.event_publisher = event_publisher
        self.event_factory = event_factory or default_session_event_factory(state.aid)
        self.tracer = tracer
        self.permission_policy = permission_policy
        self.ask_policy = ask_policy
        self.safety_policy = safety_policy

    async def process(self, tool_calls: list[dict]) -> ToolProcessingResult:
        """Execute a batch of tool calls and collect their result messages.

        Every call gets a ``role:"tool"`` answer — including failures (bad JSON
        args, unknown tool, loop detection), which answer with an error string
        instead of raising — so no ``tool_call_id`` is ever left unanswered.
        Repeated identical calls past ``MAX_SIMILAR_CALLS`` short-circuit with
        a loop warning rather than executing. Nothing is applied to session
        state here; the caller applies the returned ``ToolProcessingResult``.
        """
        result = ToolProcessingResult()
        recent_call_hashes = list(self.state.recent_call_hashes)

        for tc in tool_calls:
            func = tc["function"]
            tool_name = func["name"]
            tool_id = tc["id"]

            try:
                args = self.parse_tool_args(func)
            except json.JSONDecodeError:
                self._trace_short_circuit(
                    "tool_error",
                    tool_name,
                    {"error": "invalid_json_args", "args": func.get("arguments", "")[:200]},
                )
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
            # Read tools collide on the path alone, so they get a more lenient
            # threshold than the exact-arg loop limit (a few re-reads are normal).
            limit = (
                MAX_SAME_FILE_READS
                if tool_name in _PATH_NORMALIZED_TOOLS
                else MAX_SIMILAR_CALLS
            )
            if recent_same >= limit:
                detail = (
                    "on the same file (any line range)"
                    if tool_name in _PATH_NORMALIZED_TOOLS
                    else "with identical arguments"
                )
                warning = (
                    f"[Loop detected: tool '{tool_name}' called {recent_same} times {detail}. "
                    f"You are stuck in a loop. Try a completely different approach or ask for help.]"
                )
                self._trace_short_circuit(
                    "loop_blocked", tool_name, {"args": args, "count": recent_same}
                )
                result.messages_to_append.append({"role": "tool", "tool_call_id": tool_id, "content": warning})
                result.loop_detections.append(LoopDetection(tool=tool_name, count=recent_same))
                await self.event_publisher.emit(self.event_factory.loop_detected(tool_name, recent_same))
                continue

            tool = self.find_tool(tool_name)
            if not tool:
                self._trace_short_circuit(
                    "tool_error", tool_name, {"error": "unknown_tool", "args": args}
                )
                result.messages_to_append.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": f"Error: unknown tool '{tool_name}'. Available: {[t.name for t in self.agent.tools]}",
                })
                continue

            await self.event_publisher.emit(self.event_factory.tool_start(tool_name, args))

            tool_output, tool_latency = await self.execute_tool(tool, args)
            # The full result is persisted; a per-tool-result budget shaper caps
            # what the model sees at call time (see application.shaping).

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
        # Path-normalized read tools key on the file path alone so re-reads of one
        # file with different line ranges collide (see _PATH_NORMALIZED_TOOLS);
        # every other tool keys on its full args.
        key_args = (
            {"path": args.get("path")} if tool_name in _PATH_NORMALIZED_TOOLS else args
        )
        return hashlib.md5(
            json.dumps({"name": tool_name, "args": key_args}, sort_keys=True).encode()
        ).hexdigest()

    def count_recent_similar_calls(self, recent_call_hashes: list[str], call_hash: str) -> int:
        # Count identical calls across the WHOLE per-turn window (already capped at
        # MAX_CALL_HASH_WINDOW and reset each turn), not just the last few. A model
        # thrashing in a multi-call CYCLE — read A,B,C,…,A,B,C,… because cleared
        # tool outputs forced re-reads — repeats each hash only once per cycle
        # (10-17 calls apart in observed 100-step stalls), so the old last-6 slice
        # never saw three of them and the loop ran to the step cap. Scanning the
        # full window catches cyclic re-reads, not only back-to-back spam.
        # (ref: opencode doom_loop)
        return sum(1 for h in recent_call_hashes if h == call_hash)

    def find_tool(self, tool_name: str):
        return self.agent.find_tool(tool_name)

    async def execute_tool(self, tool, args: dict) -> tuple[str, float]:
        """Run one tool, mapping any exception to an error string.

        Returns ``(output, latency_seconds)`` — never raises, so one failing
        tool cannot abort the rest of the batch.
        """
        start = time.monotonic()
        runtime = self.tool_runtime()
        try:
            result = await tool.execute_with_runtime(args, runtime)
        except PermissionError as e:
            result = f"Permission denied: {e}"
        except Exception as e:
            result = f"Tool execution error: {type(e).__name__}: {e}"

        return result, time.monotonic() - start

    def tool_runtime(self, tool_call_id: str | None = None) -> ToolRuntime:
        return ToolRuntime(
            environment=self.environment,
            safety_policy=self.safety_policy,
            permission_policy=self.permission_policy,
            ask_policy=self.ask_policy,
            aid=self.state.aid,
            tool_call_id=tool_call_id,
        )

    async def execute_deferred(self, tc: dict) -> tuple[int | None, str | None]:
        """Drive a single deferrable tool (e.g. ``spawn_agent``).

        Returns ``(ref, None)`` when the tool deferred work and handed back a
        :class:`DeferredCall` (its ``ref`` — a child aid — is awaited), or
        ``(None, error_text)`` when it resolved synchronously (bad args,
        unknown tool, permission/topology rejection, or a plain string return)
        and its row should fill at once. The per-call ``tool_call_id`` is
        threaded into the runtime so the scheduler can route the eventual
        completion back to the right pending row.

        Deferred tools bypass ``process`` (and thus loop-detection hashing) by
        design — a spawn is never a doom-loop the way a repeated read is.
        """
        func = tc["function"]
        tool_name = func["name"]
        try:
            args = self.parse_tool_args(func)
        except json.JSONDecodeError:
            return None, f"Error: invalid JSON arguments: {func['arguments'][:200]}"

        tool = self.find_tool(tool_name)
        if not tool:
            return None, f"Error: unknown tool '{tool_name}'."

        await self.event_publisher.emit(self.event_factory.tool_start(tool_name, args))
        runtime = self.tool_runtime(tool_call_id=tc["id"])
        try:
            outcome = await tool.execute_with_runtime(args, runtime)
        except PermissionError as e:
            return None, f"Permission denied: {e}"
        except Exception as e:
            return None, f"Tool execution error: {type(e).__name__}: {e}"
        await self.event_publisher.emit(self.event_factory.tool_end(tool_name, 0.0))

        if isinstance(outcome, DeferredCall):
            return outcome.ref, None
        return None, str(outcome)

    def _trace_short_circuit(self, step_type: str, tool_name: str, detail: dict[str, Any]) -> None:
        """Record a tracer step for a pre-execution short-circuit.

        The three branches that answer a tool call without ever executing it
        (malformed JSON args, unknown tool, loop-detection block) previously left
        no trajectory trace, so a run could short-circuit silently. This logs a
        distinct ``step_type`` ("tool_error"/"loop_blocked") with the attempted
        tool and a small args snapshot. Observability only — no behavior change;
        a no-op when no tracer is wired.
        """
        if not self.tracer:
            return
        payload: dict[str, Any] = {"tool": tool_name}
        payload.update(detail)
        # Cap any args snapshot so a trace record can't balloon.
        if isinstance(payload.get("args"), dict):
            snapshot = json.dumps(payload["args"], default=str)[:500]
            payload["args"] = snapshot
        self.tracer.log_step(step_type=step_type, payload=payload)

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
