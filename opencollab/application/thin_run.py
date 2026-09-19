"""A thin single-agent run loop.

The whole control flow is ``run`` -> ``step`` -> ``query`` / ``observe`` /
``execute``. Every way the loop ends is an exception: ``Stop`` carries the
reason back to ``run``; ``Retry`` puts one message into the transcript and the
loop goes round again. There is no phase machine between the calls: the limits
are one comparison each in ``query``, the turn is over when the model answers
without a tool call (or a tool reports the turn submitted), and a failing tool
becomes the error string the model reads on its next call.

Tool execution is borrowed, not rewritten: ``ToolExecutionUseCase`` already
owns argument parsing and, in ``execute_tool``, the timeout, the cancellation
cleanup, and the exception-to-string contract. The ``Extension point`` comments
mark where the main loop layers something on; this loop keeps them empty.
"""

from __future__ import annotations

import time
from typing import Any

from opencollab.application._session_run_shared import _EMPTY_STOP_NUDGE, _EMPTY_STOP_PLACEHOLDER
from opencollab.application._session_run_usage import _normalize_completion_usage
from opencollab.application.ports import CompletionResponse, EnvironmentPort, LLMPort, TracePort
from opencollab.application.schema_validate import validate
from opencollab.application.tool_execution import ToolExecutionUseCase
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState

# Provider generation limits: the reply was cut off, so there is nothing to act on.
_TRUNCATED = frozenset({"length", "max_tokens", "model_context_window_exceeded"})
# Response fields a provider needs replayed on the next request (thinking blocks, Responses items).
_REPLAYED = (
    ("reasoning", "reasoning_content"),
    ("provider_state", "provider_state"),
    ("provider_items", "response_items"),
)


class Retry(Exception):
    """Append ``message`` to the transcript and take another step."""

    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        super().__init__(message.get("content", ""))


class Stop(Exception):
    """End the run. ``reason`` is returned; ``final_text`` overrides the last answer."""

    def __init__(self, reason: str, final_text: str | None = None) -> None:
        self.reason = reason
        self.final_text = final_text
        super().__init__(reason)


class _NoEvents:
    """The event sink ``ToolExecutionUseCase`` requires; this loop publishes nothing."""

    async def emit(self, event: Any) -> None:
        return None


class ThinRun:
    """Drive one agent from a task to a final answer with a flat loop."""

    def __init__(
        self,
        llm: LLMPort,
        agent: Agent,
        environment: EnvironmentPort | None,
        *,
        step_limit: int = 100,
        token_limit: int = 1_000_000,
        tracer: TracePort | None = None,
    ) -> None:
        self.llm = llm
        self.agent = agent
        # Only ``parse_tool_args`` and ``execute_tool`` are used; the ``SessionState``
        # is the constructor's required seat (it reads ``aid``), never a phase machine.
        self.tools = ToolExecutionUseCase(
            agent=agent, environment=environment, state=SessionState(messages=[]), event_publisher=_NoEvents()
        )
        self.step_limit = step_limit
        self.token_limit = token_limit
        self.tracer = tracer
        self.messages: list[dict[str, Any]] = []
        self.steps = 0
        self.tokens = 0

    async def run(self, task: str) -> tuple[str, str]:
        """Step until the loop stops. Returns ``(reason, final_text)``."""
        self.messages = [{"role": "system", "content": self.agent.system_prompt}, {"role": "user", "content": task}]
        while True:
            try:
                await self.step()
            except Retry as retry:
                self.messages.append(retry.message)
            except Stop as stop:
                answers = (
                    m["content"]
                    for m in reversed(self.messages)
                    if m["role"] == "assistant" and m.get("content") not in (None, "", _EMPTY_STOP_PLACEHOLDER)
                )
                return stop.reason, stop.final_text if stop.final_text is not None else next(answers, "")

    async def step(self) -> None:
        """Query the model, then act on what it said."""
        await self.observe(await self.query())

    async def query(self) -> CompletionResponse:
        """Check the limits, call the model, record the reply and its cost."""
        # Extension point: any stop condition evaluated before a model call goes here.
        if self.steps >= self.step_limit:
            raise Stop(f"step limit reached: {self.steps} steps")
        if self.tokens >= self.token_limit:
            raise Stop(f"token limit reached: {self.tokens} tokens used")
        self.steps += 1
        # Extension point: reshaping or compacting ``self.messages`` before the call goes here.
        started = time.monotonic()
        response = await self.llm.complete(
            self.messages,
            tools=self.agent.tool_schemas() or None,
            temperature=self.agent.temperature,
            thinking=self.agent.thinking,
            thinking_params=self.agent.thinking_params or None,
            reasoning_effort=self.agent.reasoning_effort,
            top_p=self.agent.top_p,
            max_output_tokens=self.agent.max_tokens_per_step,
        )
        latency = time.monotonic() - started
        _input_tokens, total_tokens = _normalize_completion_usage(response.usage)
        self.tokens += total_tokens
        if self.tokens >= self.token_limit:
            raise Stop(f"token limit reached: {self.tokens} tokens used")
        message: dict[str, Any] = {"role": "assistant"}
        if response.content:
            message["content"] = response.content
        if response.tool_calls:
            message["tool_calls"] = response.tool_calls
        for source, key in _REPLAYED:
            if value := getattr(response, source, None):
                message[key] = value
        if "content" not in message and "tool_calls" not in message:
            message["content"] = _EMPTY_STOP_PLACEHOLDER
        self.messages.append(message)
        if self.tracer is not None:
            payload = {"step": self.steps, "content": response.content, "tool_calls": response.tool_calls}
            self.tracer.log_step(step_type="llm_call", payload=payload, tokens=total_tokens, latency=latency)
        return response

    async def observe(self, response: CompletionResponse) -> None:
        """Finish on a plain answer; otherwise run every tool call in order."""
        # Extension point: any inspection of the reply before acting on it goes here.
        if response.finish_reason in _TRUNCATED:
            raise Stop("output truncated: provider reached its generation limit")
        if not response.tool_calls:
            if response.content and response.content.strip():
                raise Stop("completed", response.content)
            # An empty reply gets one nudge, as in the main loop; a second one ends the run.
            nudged = self.messages[-2].get("content") == _EMPTY_STOP_NUDGE
            if nudged or response.finish_reason not in (None, "stop"):
                raise Stop("completed", "")
            raise Retry({"role": "user", "content": _EMPTY_STOP_NUDGE})
        for call in response.tool_calls:
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": await self.execute(call)})
            if self.tools.environment_revoked:
                raise Stop("execution environment has been revoked")
            tool = self.agent.find_tool((call.get("function") or {}).get("name") or "")
            if getattr(tool, "turn_submitted", False):
                raise Stop("submitted", getattr(tool, "submitted_summary", None) or "")

    async def execute(self, tool_call: dict[str, Any]) -> str:
        """Run one tool call; every failure comes back as a string."""
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        tool = self.agent.find_tool(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {[t.name for t in self.agent.tools]}"
        try:
            args = self.tools.parse_tool_args(function)
        except ValueError as exc:
            return f"Error: invalid arguments for '{name}': {exc}"
        schema = getattr(tool, "parameters", None)
        if isinstance(schema, dict) and (errors := validate(args, schema)):
            return "Error: schema validation failed: " + "; ".join(errors)[:1_000]
        # Extension point: a check on the call before it runs (repeat detection, an allowlist) goes here.
        output, latency = await self.tools.execute_tool(tool, args, tool_id=tool_call.get("id"))
        if self.tracer is not None:
            shown = {k: (v if len(str(v)) <= 200 else str(v)[:200] + "...") for k, v in args.items()}
            payload = {"step": self.steps, "tool": name, "args": shown, "output_chars": len(output)}
            self.tracer.log_step(step_type="tool_exec", payload=payload, latency=latency)
        # Extension point: any observation of the result (counters, evidence) goes here.
        return output


__all__ = ["Retry", "Stop", "ThinRun"]
