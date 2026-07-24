"""Small stateful facade over OpenCollab's shared bootstrap runtimes."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opencollab.bootstrap.config import build_config
from opencollab.bootstrap.programmatic import (
    DEFAULT_AGENT_SYSTEM_PROMPT,
    ProgrammaticLifecycleError,
    ProgrammaticResult,
    run_agent,
    run_team,
    run_workflow,
)

from .result import RunError, RunResult


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_timeout(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def _non_empty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")
    return value


def _path(value: str | os.PathLike[str] | None, name: str) -> Path | None:
    if value is None:
        return None
    parsed = os.fspath(value)
    if not parsed or "\x00" in parsed:
        raise ValueError(f"{name} must be a valid filesystem path")
    return Path(parsed).resolve()


def _public_result(result: ProgrammaticResult) -> RunResult[Any]:
    return RunResult(
        output=result.output,
        status=result.status,
        reason=result.reason,
        tokens=result.tokens,
        artifacts=result.artifacts,
        error=result.error,
        metrics=dict(result.metrics),
    )


class OpenCollab:
    """Resolve configuration once, then run agents, teams, or workflows."""

    def __init__(
        self,
        workspace: str | os.PathLike[str] = ".",
        *,
        model: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        config: Mapping[str, Any] | None = None,
        environment: Any | None = None,
    ) -> None:
        raw_workspace = os.fspath(workspace)
        if not raw_workspace or "\x00" in raw_workspace:
            raise ValueError("workspace must be a valid filesystem path")
        resolved_workspace = Path(raw_workspace).resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {resolved_workspace}")
        if config is not None and not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        overrides = dict(config or {})
        overrides.update(
            {
                key: value
                for key, value in {
                    "model": model,
                    "provider": provider,
                    "api_key": api_key,
                    "base_url": base_url,
                }.items()
                if value is not None
            }
        )
        self._workspace = str(resolved_workspace)
        self._config = build_config(self._workspace, overrides=overrides).model_dump()
        self._environment = environment

    async def agent(
        self,
        prompt: str,
        *,
        tools: str | Sequence[Any] | None = "coding",
        budget: int | None = None,
        steps: int = 100,
        timeout: float | None = None,
        artifacts: str | os.PathLike[str] | None = None,
        trace: bool = True,
        name: str = "agent",
        system_prompt: str | None = None,
        llm: Any | None = None,
    ) -> RunResult[str]:
        """Run one directly configured agent."""
        _non_empty(prompt, "prompt")
        _non_empty(name, "name")
        if system_prompt is not None:
            _non_empty(system_prompt, "system_prompt")
        if not isinstance(trace, bool):
            raise ValueError("trace must be a boolean")
        try:
            result = await run_agent(
                prompt=prompt,
                config=self._config,
                workspace=self._workspace,
                tools=tools,
                max_tokens=_positive_int(
                    self._config["budget"] if budget is None else budget,
                    "budget",
                ),
                max_steps=_positive_int(steps, "steps"),
                timeout=_positive_timeout(timeout, "timeout"),
                artifacts=_path(artifacts, "artifacts"),
                trace=trace,
                environment=self._environment,
                name=name,
                system_prompt=system_prompt or DEFAULT_AGENT_SYSTEM_PROMPT,
                llm=llm,
            )
        except ProgrammaticLifecycleError as exc:
            raise RunError(str(exc)) from exc
        return _public_result(result)

    async def team(
        self,
        prompt: str,
        *,
        config: str | os.PathLike[str] | None = None,
        budget: int | None = None,
        timeout: float | None = None,
        artifacts: str | os.PathLike[str] | None = None,
        trace: bool = True,
        use_worktrees: bool = True,
    ) -> RunResult[str]:
        """Run one scheduler-controlled team turn."""
        _non_empty(prompt, "prompt")
        if self._environment is not None:
            raise ValueError("team runs do not accept a custom environment")
        if not isinstance(trace, bool) or not isinstance(use_worktrees, bool):
            raise ValueError("trace and use_worktrees must be booleans")
        team_path = _path(config, "config")
        try:
            result = await run_team(
                prompt=prompt,
                config=self._config,
                workspace=self._workspace,
                team_config_path=team_path,
                max_tokens=_positive_int(
                    self._config["budget"] if budget is None else budget,
                    "budget",
                ),
                timeout=_positive_timeout(timeout, "timeout"),
                artifacts=_path(artifacts, "artifacts"),
                trace=trace,
                use_worktrees=use_worktrees,
            )
        except ProgrammaticLifecycleError as exc:
            raise RunError(str(exc)) from exc
        return _public_result(result)

    async def workflow(
        self,
        flow: Any,
        inputs: Mapping[str, Any] | None = None,
        *,
        budget: int | None = None,
        concurrency: int = 4,
        timeout: float | None = None,
        artifacts: str | os.PathLike[str] | None = None,
        trace: bool = True,
    ) -> RunResult[Any]:
        """Run a decorated or plain async workflow function."""
        if not callable(flow) and not callable(getattr(flow, "fn", None)):
            raise TypeError("flow must be a workflow function or spec")
        if inputs is not None and not isinstance(inputs, Mapping):
            raise TypeError("inputs must be a mapping")
        normalized_inputs = dict(inputs or {})
        if any(not isinstance(key, str) for key in normalized_inputs):
            raise ValueError("workflow input keys must be strings")
        if not isinstance(trace, bool):
            raise ValueError("trace must be a boolean")
        try:
            result = await run_workflow(
                workflow=flow,
                inputs=normalized_inputs,
                config=self._config,
                workspace=self._workspace,
                max_tokens=(
                    self._config["budget"]
                    if budget is None
                    else _positive_int(budget, "budget")
                ),
                max_concurrency=_positive_int(concurrency, "concurrency"),
                timeout=_positive_timeout(timeout, "timeout"),
                artifacts=_path(artifacts, "artifacts"),
                trace=trace,
                environment=self._environment,
            )
        except ProgrammaticLifecycleError as exc:
            raise RunError(str(exc)) from exc
        return _public_result(result)


__all__ = ["OpenCollab"]
