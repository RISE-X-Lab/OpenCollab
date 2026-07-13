"""Versioned request and result values for the public OpenCollab SDK."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from opencollab.application.workflow_registry import WorkflowFn, WorkflowSpec

from .environment import ExecutionEnvironment
from .errors import InvalidSDKRequestError

if TYPE_CHECKING:
    from opencollab.application.ports import LLMPort

SDK_API_VERSION = 2


def _non_empty_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InvalidSDKRequestError(f"{field_name} must be a non-empty, trimmed string")
    if "\x00" in value:
        raise InvalidSDKRequestError(f"{field_name} must not contain NUL bytes")
    return value


def _positive_integer(value: object, *, field_name: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidSDKRequestError(f"{field_name} must be a positive integer")
    return value


def _positive_number(value: object, *, field_name: str, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSDKRequestError(f"{field_name} must be a positive finite number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise InvalidSDKRequestError(f"{field_name} must be a positive finite number")
    return parsed


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """LLM configuration accepted by the stable workflow runtime boundary."""

    model: str
    provider: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    llm_timeout_seconds: float = 600.0
    temperature: float = 0.2
    top_p: float | None = None
    max_output_tokens: int = 8_192
    thinking: bool = False
    thinking_params: Mapping[str, Any] = field(default_factory=lambda: {"enable_thinking": True})

    def __post_init__(self) -> None:
        _non_empty_text(self.model, field_name="model")
        _non_empty_text(self.provider, field_name="provider")
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise InvalidSDKRequestError("api_key must be a string or None")
        if self.base_url is not None:
            _non_empty_text(self.base_url, field_name="base_url")
        _positive_number(self.llm_timeout_seconds, field_name="llm_timeout_seconds")
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise InvalidSDKRequestError("temperature must be a finite number from 0 to 2")
        temperature = float(self.temperature)
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise InvalidSDKRequestError("temperature must be a finite number from 0 to 2")
        if self.top_p is not None:
            if isinstance(self.top_p, bool) or not isinstance(self.top_p, (int, float)):
                raise InvalidSDKRequestError("top_p must be None or a finite number from 0 to 1")
            top_p = float(self.top_p)
            if not math.isfinite(top_p) or not 0 <= top_p <= 1:
                raise InvalidSDKRequestError("top_p must be None or a finite number from 0 to 1")
        _positive_integer(self.max_output_tokens, field_name="max_output_tokens")
        if not isinstance(self.thinking, bool):
            raise InvalidSDKRequestError("thinking must be a boolean")
        if not isinstance(self.thinking_params, Mapping):
            raise InvalidSDKRequestError("thinking_params must be a mapping")
        object.__setattr__(self, "thinking_params", deepcopy(dict(self.thinking_params)))

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> RuntimeConfig:
        """Build a validated SDK config from the existing OpenCollab config shape."""
        if not isinstance(config, Mapping):
            raise InvalidSDKRequestError("config must be a mapping")
        return cls(
            model=config.get("model"),
            provider=config.get("provider"),
            api_key=config.get("api_key"),
            base_url=config.get("base_url"),
            llm_timeout_seconds=config.get("llm_timeout", 600.0),
            temperature=config.get("temperature", 0.2),
            top_p=config.get("top_p"),
            max_output_tokens=config.get("max_output_tokens", 8_192),
            thinking=config.get("thinking", False),
            thinking_params=config.get("thinking_params") or {"enable_thinking": True},
        )

    def as_runtime_dict(self) -> dict[str, Any]:
        """Return a fresh configuration mapping for the internal runtime."""
        return {
            "model": self.model,
            "provider": self.provider,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "llm_timeout": float(self.llm_timeout_seconds),
            "temperature": float(self.temperature),
            "top_p": None if self.top_p is None else float(self.top_p),
            "max_output_tokens": self.max_output_tokens,
            "thinking": self.thinking,
            "thinking_params": deepcopy(dict(self.thinking_params)),
        }


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Resource and shutdown bounds for one workflow execution."""

    max_tokens: int | None = None
    timeout_seconds: float | None = None
    max_concurrency: int = 4
    cleanup_timeout_seconds: float = 2.0
    deadline_margin_seconds: float = 120.0

    def __post_init__(self) -> None:
        _positive_integer(self.max_tokens, field_name="max_tokens", optional=True)
        _positive_number(self.timeout_seconds, field_name="timeout_seconds", optional=True)
        _positive_integer(self.max_concurrency, field_name="max_concurrency")
        _positive_number(self.cleanup_timeout_seconds, field_name="cleanup_timeout_seconds")
        _positive_number(self.deadline_margin_seconds, field_name="deadline_margin_seconds")


@dataclass(frozen=True, slots=True)
class AgentRunBudget:
    """Token, step, wall-clock, and shutdown bounds for one agent run."""

    max_tokens: int = 1_000_000
    max_steps: int = 100
    timeout_seconds: float | None = None
    cleanup_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        _positive_integer(self.max_tokens, field_name="max_tokens")
        _positive_integer(self.max_steps, field_name="max_steps")
        _positive_number(self.timeout_seconds, field_name="timeout_seconds", optional=True)
        _positive_number(self.cleanup_timeout_seconds, field_name="cleanup_timeout_seconds")


@dataclass(frozen=True, slots=True)
class WorkflowRunRequest:
    """All inputs required to run one OpenCollab workflow.

    A supplied environment remains caller-owned after successful completion.
    The runtime revokes it when execution times out or is cancelled.
    """

    workflow: WorkflowSpec | WorkflowFn
    config: RuntimeConfig
    inputs: Mapping[str, Any] = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)
    environment: ExecutionEnvironment | None = None
    environment_workdir: str | None = None
    source_root: str | None = None
    # Compatibility alias used by SDK v1. When the two explicit paths are
    # absent, ``workspace`` supplies both values.
    workspace: str | None = None
    artifact_dir: Path | None = None
    trace: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, WorkflowSpec) and not callable(self.workflow):
            raise InvalidSDKRequestError("workflow must be a WorkflowSpec or callable workflow")
        if not isinstance(self.config, RuntimeConfig):
            raise InvalidSDKRequestError("config must be a RuntimeConfig")
        if not isinstance(self.inputs, Mapping):
            raise InvalidSDKRequestError("inputs must be a mapping")
        if any(not isinstance(key, str) for key in self.inputs):
            raise InvalidSDKRequestError("inputs keys must be strings")
        object.__setattr__(self, "inputs", deepcopy(dict(self.inputs)))
        if not isinstance(self.budget, RunBudget):
            raise InvalidSDKRequestError("budget must be a RunBudget")
        if self.environment is not None and not isinstance(self.environment, ExecutionEnvironment):
            raise InvalidSDKRequestError("environment must implement the OpenCollab Environment contract")
        for field_name in ("environment_workdir", "source_root", "workspace"):
            value = getattr(self, field_name)
            if value is not None:
                _non_empty_text(value, field_name=field_name)
        if self.artifact_dir is not None:
            artifact_dir = os.fspath(self.artifact_dir)
            if not artifact_dir or "\x00" in artifact_dir:
                raise InvalidSDKRequestError("artifact_dir must be a valid filesystem path")
            object.__setattr__(self, "artifact_dir", Path(artifact_dir))
        if not isinstance(self.trace, bool):
            raise InvalidSDKRequestError("trace must be a boolean")


@dataclass(frozen=True, slots=True)
class WorkflowRunResult:
    """Stable workflow output returned after the hardened lifecycle completes."""

    output: Any
    workflow_name: str
    tokens_spent: int | None
    session_count: int | None
    artifact_dir: Path | None
    manifest_path: Path | None
    sdk_api_version: int = SDK_API_VERSION


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Validated inputs for one stable single-agent execution.

    A supplied environment remains caller-owned after successful completion.
    The runtime revokes it when execution times out or is cancelled.
    ``failure_mode='return'`` converts quiescent timeouts and execution errors
    into evidence-bearing results. Lifecycle and evidence failures still raise.
    """

    prompt: str
    config: RuntimeConfig
    budget: AgentRunBudget = field(default_factory=AgentRunBudget)
    name: str = "sdk-agent"
    system_prompt: str = (
        "You are an autonomous software-engineering agent. Complete the user task, "
        "use the available tools when needed, and report the verified result."
    )
    tools: tuple[Any, ...] = ()
    environment: ExecutionEnvironment | None = None
    environment_workdir: str | None = None
    source_root: str | None = None
    workspace: str | None = None
    artifact_dir: Path | None = None
    trace: bool = True
    failure_mode: Literal["raise", "return"] = "raise"
    llm: LLMPort | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _non_empty_text(self.prompt, field_name="prompt")
        _non_empty_text(self.name, field_name="name")
        _non_empty_text(self.system_prompt, field_name="system_prompt")
        if not isinstance(self.config, RuntimeConfig):
            raise InvalidSDKRequestError("config must be a RuntimeConfig")
        if not isinstance(self.budget, AgentRunBudget):
            raise InvalidSDKRequestError("budget must be an AgentRunBudget")
        if not isinstance(self.tools, (list, tuple)):
            raise InvalidSDKRequestError("tools must be a sequence")
        object.__setattr__(self, "tools", tuple(self.tools))
        if self.environment is not None and not isinstance(self.environment, ExecutionEnvironment):
            raise InvalidSDKRequestError("environment must implement the OpenCollab Environment contract")
        for field_name in ("environment_workdir", "source_root", "workspace"):
            value = getattr(self, field_name)
            if value is not None:
                _non_empty_text(value, field_name=field_name)
        if self.artifact_dir is not None:
            artifact_dir = os.fspath(self.artifact_dir)
            if not artifact_dir or "\x00" in artifact_dir:
                raise InvalidSDKRequestError("artifact_dir must be a valid filesystem path")
            object.__setattr__(self, "artifact_dir", Path(artifact_dir))
        if not isinstance(self.trace, bool):
            raise InvalidSDKRequestError("trace must be a boolean")
        if not isinstance(self.failure_mode, str) or self.failure_mode not in {
            "raise",
            "return",
        }:
            raise InvalidSDKRequestError("failure_mode must be 'raise' or 'return'")
        if self.llm is not None and not callable(getattr(self.llm, "complete", None)):
            raise InvalidSDKRequestError("llm must implement the OpenCollab LLM contract")


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Stable outcome returned after agent-owned work and evidence are quiescent."""

    output: str | None
    outcome: Literal["completed", "timed_out", "failed"]
    phase: str
    terminal_reason: str | None
    error_type: str | None
    error_message: str | None
    tokens_spent: int
    step_count: int
    artifact_dir: Path | None
    transcript_path: Path | None
    trace_path: Path | None
    cleanup_quiesced: bool
    sdk_api_version: int = SDK_API_VERSION


__all__ = [
    "AgentRunBudget",
    "AgentRunRequest",
    "AgentRunResult",
    "RunBudget",
    "RuntimeConfig",
    "SDK_API_VERSION",
    "WorkflowRunRequest",
    "WorkflowRunResult",
]
