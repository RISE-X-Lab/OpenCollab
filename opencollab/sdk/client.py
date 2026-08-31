"""Small stateful facade over OpenCollab's shared bootstrap runtimes."""

from __future__ import annotations

import copy
import hashlib
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from opencollab.bootstrap.config import build_config
from opencollab.bootstrap.programmatic import (
    DEFAULT_AGENT_SYSTEM_PROMPT,
    DEFAULT_TEAM_CLEANUP_TIMEOUT_SECONDS,
    ProgrammaticLifecycleError,
    ProgrammaticResult,
    run_agent,
    run_team,
    run_workflow,
)
from opencollab.bootstrap.session_factory import SESSION_MAX_STEPS

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


def _required_positive_timeout(value: object, name: str) -> float:
    parsed = _positive_timeout(value, name)
    if parsed is None:
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
    agent_failures = tuple(
        {
            "label": item.get("label"),
            "exception_type": item.get("exception_type"),
            "status_code": item.get("status_code"),
            "provider_error_type": item.get("provider_error_type"),
        }
        for item in result.agent_failures
    )
    return RunResult(
        output=result.output,
        status=result.status,
        reason=result.reason,
        tokens=result.tokens,
        artifacts=result.artifacts,
        error=result.error,
        metrics=dict(result.metrics),
        agent_failures=agent_failures,
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

    @property
    def configuration(self) -> Mapping[str, Any]:
        """Return read-only non-secret metadata with isolated nested values."""
        public_keys = (
            "model",
            "provider",
            "wire_protocol",
            "budget",
            "llm_timeout",
            "temperature",
            "top_p",
            "max_output_tokens",
            "thinking",
            "thinking_params",
            "reasoning_effort",
            "llm_connect_timeout",
            "llm_first_event_timeout",
            "llm_stream_idle_timeout",
            "llm_stream_chat",
        )
        public = {
            key: copy.deepcopy(self._config[key])
            for key in public_keys
            if key in self._config
        }
        base_url = self._config.get("base_url")
        public["base_url_sha256"] = (
            hashlib.sha256(base_url.encode("utf-8")).hexdigest()
            if isinstance(base_url, str) and base_url
            else None
        )
        return MappingProxyType(public)

    async def agent(
        self,
        prompt: str,
        *,
        tools: str | Sequence[Any] | None = "coding",
        budget: int | None = None,
        max_steps: int | None = None,
        steps: int | None = None,
        timeout: float | None = None,
        cleanup_timeout: float = 2.0,
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
        if max_steps is not None and steps is not None:
            raise ValueError("max_steps and steps cannot both be set")
        resolved_max_steps = (
            100
            if max_steps is None and steps is None
            else _positive_int(
                max_steps if max_steps is not None else steps,
                "max_steps" if max_steps is not None else "steps",
            )
        )
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
                max_steps=resolved_max_steps,
                timeout=_positive_timeout(timeout, "timeout"),
                cleanup_timeout=_required_positive_timeout(
                    cleanup_timeout,
                    "cleanup_timeout",
                ),
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
        cleanup_timeout: float = DEFAULT_TEAM_CLEANUP_TIMEOUT_SECONDS,
        artifacts: str | os.PathLike[str] | None = None,
        trace: bool = True,
        use_worktrees: bool = True,
        prebuild_team: bool = False,
        allow_unisolated_shell: bool | None = None,
        max_steps: int = SESSION_MAX_STEPS,
        serialize_turns: bool = False,
        record_delivery_tree: bool = False,
    ) -> RunResult[str]:
        """Run one scheduler-controlled team turn.

        ``prebuild_team`` seats every role the team config declares before the
        first model call and refuses ``spawn_agent`` thereafter, so the roster
        is an input to the run instead of something the model decides mid-run.

        ``allow_unisolated_shell`` says whether an agent seated at the start may
        execute commands the OS does not sandbox. It is *not* the same question
        as "is a human present": an SDK run never has one, so no agent here is
        ever given ``ask_user``, but an unattended experiment may still need its
        agents to run ``git`` in their worktrees. ``None`` leaves the shell
        answer where it has always been for an SDK run — off.

        ``max_steps`` is the step ceiling every seat gets, entry agent and
        teammates alike. A comparison between a team and a solo agent can hold
        their tokens equal or their steps equal but not both, and this run holds
        tokens; the ceiling is only here to stop a runaway, so set it above what
        the token budget can pay for and read the realized step counts out of
        the trajectory.

        An environment given to the client is where this team works: agent 0
        runs in it, and with ``use_worktrees`` each teammate gets an isolated
        view of the same place. That is how a team is run against a repository
        that exists only inside a container -- the case the evaluation harness
        creates, where the repository cannot be exposed to the host at all.

        ``serialize_turns`` holds the team to one turn at a time: a teammate a
        message wakes waits for the running turn to finish instead of running
        beside it. It changes only *when* an agent runs — every declared edge
        stays open and ``message_agent`` stays voluntary, so whether the agents
        hand work to each other is still theirs to decide. Off by default. The
        run records which way it was set, under
        ``assigned.topology_nodes.turns_serialized``.

        ``record_delivery_tree`` returns ``metrics["tree_snapshots"]``: the diff
        of the tree this run is graded on — agent 0's — taken before every turn
        and at every teammate message that was queued. Two consecutive rows
        bracket one seat's working period, so a line in the delivered patch can
        be attributed to the seat that was working when it arrived, and the row
        at the first message answers what had already been done before anyone
        was asked. Off by default: it costs a ``git diff`` per boundary, and off
        the key is absent rather than empty.
        """
        _non_empty(prompt, "prompt")
        if not isinstance(trace, bool) or not isinstance(use_worktrees, bool):
            raise ValueError("trace and use_worktrees must be booleans")
        if not isinstance(prebuild_team, bool):
            raise ValueError("prebuild_team must be a boolean")
        if not isinstance(serialize_turns, bool):
            raise ValueError("serialize_turns must be a boolean")
        if not isinstance(record_delivery_tree, bool):
            raise ValueError("record_delivery_tree must be a boolean")
        if allow_unisolated_shell is not None and not isinstance(allow_unisolated_shell, bool):
            raise ValueError("allow_unisolated_shell must be a boolean or None")
        resolved_team_max_steps = _positive_int(max_steps, "max_steps")
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
                cleanup_timeout=_required_positive_timeout(
                    cleanup_timeout,
                    "cleanup_timeout",
                ),
                artifacts=_path(artifacts, "artifacts"),
                trace=trace,
                use_worktrees=use_worktrees,
                prebuild_team=prebuild_team,
                allow_unisolated_shell=allow_unisolated_shell,
                max_steps=resolved_team_max_steps,
                serialize_turns=serialize_turns,
                environment=self._environment,
                record_delivery_tree=record_delivery_tree,
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
        task_concurrency: int | None = None,
        timeout: float | None = None,
        max_steps: int = 100,
        system_prompt: str | None = None,
        cleanup_timeout: float = 2.0,
        artifacts: str | os.PathLike[str] | None = None,
        trace: bool = True,
    ) -> RunResult[Any]:
        """Run a decorated or plain async workflow function.

        ``concurrency`` limits agent sessions. ``task_concurrency`` separately
        limits active parallel/pipeline units across the workflow and defaults
        to ``concurrency``. Mixed agent and task work may therefore peak at the
        sum of both limits.
        """
        if not callable(flow) and not callable(getattr(flow, "fn", None)):
            raise TypeError("flow must be a workflow function or spec")
        if inputs is not None and not isinstance(inputs, Mapping):
            raise TypeError("inputs must be a mapping")
        normalized_inputs = dict(inputs or {})
        if any(not isinstance(key, str) for key in normalized_inputs):
            raise ValueError("workflow input keys must be strings")
        if system_prompt is not None:
            _non_empty(system_prompt, "system_prompt")
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
                task_concurrency=(
                    None
                    if task_concurrency is None
                    else _positive_int(task_concurrency, "task_concurrency")
                ),
                timeout=_positive_timeout(timeout, "timeout"),
                max_steps=_positive_int(max_steps, "max_steps"),
                system_prompt=system_prompt,
                cleanup_timeout=_required_positive_timeout(
                    cleanup_timeout,
                    "cleanup_timeout",
                ),
                artifacts=_path(artifacts, "artifacts"),
                trace=trace,
                environment=self._environment,
            )
        except ProgrammaticLifecycleError as exc:
            raise RunError(str(exc)) from exc
        return _public_result(result)


__all__ = ["OpenCollab"]
