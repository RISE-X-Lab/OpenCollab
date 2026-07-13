"""Apply OpenCollab runtime limits to the OpenHands CLI SDK objects."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from .container_quiescence import (
        guarded_terminal_invocation as _guarded_terminal_invocation,
    )
    from .container_quiescence import (
        stop_guarded_terminal_session as _stop_guarded_terminal_session,
    )
else:
    from container_quiescence import (
        guarded_terminal_invocation as _guarded_terminal_invocation,
    )
    from container_quiescence import (
        stop_guarded_terminal_session as _stop_guarded_terminal_session,
    )


@dataclass(frozen=True)
class RuntimeSettings:
    context_window: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    token_budget: int | None = None
    max_steps: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeSettings":
        values = os.environ if env is None else env

        def optional_int(name: str) -> int | None:
            raw = values.get(name)
            return int(raw) if raw not in (None, "") else None

        def optional_float(name: str) -> float | None:
            raw = values.get(name)
            return float(raw) if raw not in (None, "") else None

        return cls(
            context_window=optional_int("OPENHANDS_CONTEXT_WINDOW"),
            temperature=optional_float("OPENHANDS_TEMPERATURE"),
            top_p=optional_float("OPENHANDS_TOP_P"),
            max_output_tokens=optional_int("OPENHANDS_MAX_OUTPUT_TOKENS"),
            token_budget=optional_int("OPENHANDS_TOKEN_BUDGET"),
            max_steps=optional_int("OPENHANDS_MAX_STEPS"),
        )

    def llm_updates(self) -> dict[str, int | float]:
        candidates = {
            "max_input_tokens": self.context_window,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_output_tokens": self.max_output_tokens,
        }
        return {key: value for key, value in candidates.items() if value is not None}


def apply_agent_settings(agent: Any, settings: RuntimeSettings) -> Any:
    updates = settings.llm_updates()
    if not updates:
        return agent
    llm = agent.llm.model_copy(update=updates)
    condenser = getattr(agent, "condenser", None)
    if condenser is not None and getattr(condenser, "llm", None) is not None:
        condenser = condenser.model_copy(
            update={"llm": condenser.llm.model_copy(update=updates)}
        )
    return agent.model_copy(update={"llm": llm, "condenser": condenser})


def _usage_total(llm: Any) -> int:
    metrics = getattr(llm, "metrics", None)
    usage = getattr(metrics, "accumulated_token_usage", None)
    if usage is None:
        return 0
    return int(getattr(usage, "prompt_tokens", 0) or 0) + int(
        getattr(usage, "completion_tokens", 0) or 0
    )


class TokenBudgetGuard:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0
        self.reserved = 0
        self._seen_by_llm: dict[int, int] = {}
        self._lock = threading.Lock()

    def reserve(self, request_upper_bound: int) -> int:
        if request_upper_bound <= 0:
            raise ValueError("request_upper_bound must be positive")
        with self._lock:
            projected = self.spent + self.reserved + request_upper_bound
            if projected > self.limit:
                raise RuntimeError(
                    "OpenHands token budget cannot cover the next request "
                    f"({self.spent}+{self.reserved}+{request_upper_bound}>{self.limit})"
                )
            self.reserved += request_upper_bound
        return request_upper_bound

    def record(
        self, llm: Any, *, reservation: int, enforce: bool = True
    ) -> None:
        current = _usage_total(llm)
        identity = id(llm)
        with self._lock:
            previous = self._seen_by_llm.get(identity, 0)
            self.spent += max(current - previous, 0)
            self._seen_by_llm[identity] = max(previous, current)
            self.reserved = max(self.reserved - reservation, 0)
            exceeded = self.spent > self.limit
        if enforce and exceeded:
            raise RuntimeError(
                f"OpenHands token budget exceeded ({self.spent}/{self.limit})"
            )


_INSTALLED = False


def _isolated_agent_tools(agent: Any, terminal_name: str) -> list[Any]:
    safe_tools = [
        tool
        for tool in getattr(agent, "tools", [])
        if getattr(tool, "name", "") == terminal_name
    ]
    if not safe_tools:
        raise RuntimeError("OpenHands isolated agent is missing TerminalTool")
    return safe_tools


def _install_isolated_terminal(container_id: str) -> None:
    """Bind every model-visible shell command to one pre-created container."""
    from openhands.sdk.tool import ToolExecutor
    from openhands.tools.terminal import impl as terminal_impl
    from openhands.tools.terminal.definition import TerminalObservation

    class ContainerTerminalExecutor(ToolExecutor):
        is_pooled = False

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            self.full_output_save_dir = kwargs.get("full_output_save_dir") or os.environ.get(
                "OPENHANDS_OUTPUT_DIR"
            )

        def __call__(self, action: Any, conversation: Any = None) -> Any:
            del conversation
            if action.reset and not action.command.strip():
                return TerminalObservation.from_text(
                    text="Container terminal state reset.",
                    command="[RESET]",
                    exit_code=0,
                )
            if action.is_input:
                return TerminalObservation.from_text(
                    text="Interactive host terminal input is disabled.",
                    is_error=True,
                    command=action.command or "[INPUT]",
                    exit_code=2,
                )
            command = action.command.strip()
            if not command:
                return TerminalObservation.from_text(
                    text="",
                    command="",
                    exit_code=0,
                )
            timeout = float(action.timeout or 30.0)
            invocation = _guarded_terminal_invocation(container_id, command)
            try:
                result = subprocess.run(
                    invocation.argv,
                    input=invocation.source,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                output = result.stdout
                if result.stderr:
                    output += ("\n" if output else "") + result.stderr
                return TerminalObservation.from_text(
                    text=output,
                    is_error=result.returncode != 0,
                    command=command,
                    exit_code=result.returncode,
                )
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                try:
                    cleanup = _stop_guarded_terminal_session(container_id, invocation)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    cleanup_returncode = 125
                    cleanup_detail = f"{type(cleanup_error).__name__}: {cleanup_error}"
                else:
                    cleanup_returncode = cleanup.returncode
                    cleanup_detail = cleanup.stderr.strip()
                if cleanup_returncode != 0:
                    output += (
                        "\nContainer command cleanup could not be proven"
                        f" (exit {cleanup_returncode}): {cleanup_detail}"
                    )
                return TerminalObservation.from_text(
                    text=output + f"\nCommand timed out after {timeout:g}s.",
                    is_error=True,
                    command=command,
                    exit_code=-1 if cleanup_returncode == 0 else 125,
                )

        def close(self) -> None:
            return None

    terminal_impl.TerminalExecutor = ContainerTerminalExecutor


def install_runtime_overrides(settings: RuntimeSettings | None = None) -> RuntimeSettings:
    global _INSTALLED
    settings = settings or RuntimeSettings.from_env()
    if _INSTALLED:
        return settings

    from openhands.sdk import LLM
    from openhands.tools.terminal import TerminalTool
    from openhands_cli import setup as setup_module
    from openhands_cli.stores.agent_store import AgentStore

    container_id = os.environ.get("OPENHANDS_CONTAINER_ID", "").strip()
    if not container_id:
        raise RuntimeError("OpenHands container isolation requires OPENHANDS_CONTAINER_ID")
    _install_isolated_terminal(container_id)

    original_load_or_create = AgentStore.load_or_create

    @functools.wraps(original_load_or_create)
    def load_or_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        agent = original_load_or_create(self, *args, **kwargs)
        if agent is None:
            return None
        agent = apply_agent_settings(agent, settings)
        safe_tools = _isolated_agent_tools(agent, TerminalTool.name)
        return agent.model_copy(update={"tools": safe_tools})

    AgentStore.load_or_create = load_or_create

    if settings.max_steps is not None:
        original_conversation = setup_module.Conversation

        @functools.wraps(original_conversation)
        def conversation(*args: Any, **kwargs: Any) -> Any:
            kwargs["max_iteration_per_run"] = settings.max_steps
            return original_conversation(*args, **kwargs)

        setup_module.Conversation = conversation

    if settings.token_budget is not None:
        guard = TokenBudgetGuard(settings.token_budget)
        for method_name in ("completion", "responses"):
            original_method = getattr(LLM, method_name)

            @functools.wraps(original_method)
            def guarded_call(
                self: Any,
                *args: Any,
                __original: Any = original_method,
                **kwargs: Any,
            ) -> Any:
                max_input = int(getattr(self, "max_input_tokens", 0) or 0)
                max_output = int(getattr(self, "max_output_tokens", 0) or 0)
                request_upper_bound = max_input + max_output
                if request_upper_bound <= 0:
                    request_upper_bound = settings.token_budget or 1
                reservation = guard.reserve(request_upper_bound)
                try:
                    result = __original(self, *args, **kwargs)
                except BaseException:
                    guard.record(self, reservation=reservation, enforce=False)
                    raise
                guard.record(self, reservation=reservation)
                return result

            setattr(LLM, method_name, guarded_call)

    output_dir = os.environ.get("OPENHANDS_OUTPUT_DIR")
    if output_dir:
        path = Path(output_dir) / "runtime_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _INSTALLED = True
    return settings
