"""Sequential independent agents with weighted opportunities and executable checks."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any

from opencollab.sdk import BudgetDecision, BudgetSnapshot, RunControl, RunEvent
from opencollab.tools import Tool
from opencollab.workflows import WorkflowContext, workflow

from ._evolution_execution import _coding_tools, _verify_commands


def _positive(value: Any, name: str, *, integer: bool = False, zero: bool = False) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)) \
            or not math.isfinite(value) or value < (0 if zero else 1 if integer else 0) \
            or (not integer and not zero and value == 0):
        raise ValueError(f"{name} must be {'a nonnegative' if zero else 'a positive'} finite number")


@dataclass
class EvolutionGroup:
    id: str
    prompt: str
    weight: float = 1
    dependencies: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "prompt"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"group {name} must be nonempty text")
        if self.weight is None:
            raise ValueError("group weight must be a positive finite number")
        _positive(self.weight, "group weight")
        for name in ("dependencies", "resources", "targets"):
            values = getattr(self, name)
            if isinstance(values, str) or not isinstance(values, Sequence) \
                    or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"group {name} must be a sequence of nonempty strings")
            setattr(self, name, tuple(dict.fromkeys(values)))
        if not self.targets:
            self.targets = (self.id,)


@dataclass
class EvolutionCheck:
    ok: bool
    executed: bool
    report: dict[str, Any] = field(default_factory=dict)
    repair_targets: tuple[str, ...] = ()
    progress_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool) or not isinstance(self.executed, bool):
            raise ValueError("check ok and executed must be booleans")
        self.report = dict(self.report)
        self.repair_targets = tuple(self.repair_targets)
        self.progress_markers = tuple(self.progress_markers)

    @property
    def passed(self) -> bool:
        return self.ok and self.executed


@dataclass
class EvolutionConfig:
    """Optional resource limits and scheduling ratios for a concrete invocation."""

    budget: int | None = None
    main_budget: int | None = None
    main_hard_budget: int | None = None
    repair_reserve: int = 0
    max_steps: int | None = None
    wall_seconds: float | None = None
    agent_seconds: float | None = None
    minimum_group_tokens: int = 1
    minimum_group_steps: int = 1
    minimum_group_seconds: float = 0
    main_agent_time_reserve: float = 0
    main_wall_time_reserve: float = 0
    group_check_fraction: float = 0.25
    group_check_min_seconds: float = 0
    group_check_max_seconds: float | None = None
    group_check_grace_seconds: float = 0
    group_check_timeout: float | None = None
    minimum_session_seconds: float = 0
    cleanup_seconds: float = 2
    tool_cleanup_seconds: float | None = None
    max_output_tokens: int = 1
    history_trigger_tokens: int | None = None
    extension_tokens: int = 1
    extension_margin_tokens: int | None = None
    extension_margin_fraction: float = 0.1
    budget_final_prompt: str | None = None
    max_repair_rounds: int = 3
    stagnant_round_limit: int = 2
    repair_budget: int | None = None
    repair_budget_fraction: float = 0.65
    repair_time_reserve: float = 0
    minimum_repair_seconds: float = 0
    repair_agent_time_reserve: float = 0
    repair_timeout: float | None = None
    repair_steps: int | None = None
    final_check_timeout: float | None = None
    final_check_time_reserve: float = 0
    system_prompt: str | None = None
    heartbeat_seconds: float = 60

    def __post_init__(self) -> None:
        integers = {
            "budget", "main_budget", "main_hard_budget", "max_steps", "minimum_group_tokens",
            "minimum_group_steps", "max_output_tokens", "history_trigger_tokens", "extension_tokens",
            "extension_margin_tokens", "stagnant_round_limit", "repair_budget", "repair_steps",
        }
        zeroes = {
            "repair_reserve", "max_repair_rounds", "minimum_group_seconds", "main_agent_time_reserve",
            "main_wall_time_reserve", "group_check_min_seconds", "group_check_grace_seconds",
            "minimum_session_seconds", "repair_time_reserve", "minimum_repair_seconds",
            "repair_agent_time_reserve", "final_check_time_reserve",
        }
        for name in asdict(self):
            if name in {"system_prompt", "budget_final_prompt"}:
                value = getattr(self, name)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(f"{name} must be nonempty text or None")
                continue
            if getattr(self, name) is None and self.__dataclass_fields__[name].default is not None:
                raise ValueError(f"{name} requires a numeric value")
            _positive(getattr(self, name), name, integer=name in integers or name in {
                "repair_reserve", "max_repair_rounds",
            }, zero=name in zeroes)
        for name in ("group_check_fraction", "extension_margin_fraction", "repair_budget_fraction"):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be at most one")
        if self.history_trigger_tokens is not None and self.history_trigger_tokens < 2:
            raise ValueError("history_trigger_tokens must be at least two")


class EvolutionState:
    """Ordinary serializable progress with cumulative per-session accounting."""

    def __init__(self, data: Mapping[str, Any] | None = None, *, run_id: str | None = None) -> None:
        self.data = copy.deepcopy(dict(data or {}))
        existing = self.data.get("run_id")
        if existing is not None and run_id is not None and existing != run_id:
            raise ValueError("continuation requires the original run_id")
        self.data.setdefault("run_id", run_id or "evolution-" + uuid.uuid4().hex)
        for name, value in (("sessions", {}), ("groups", {}), ("repair_rounds", [])):
            self.data.setdefault(name, value)
        self._elapsed_before = float(self.data.get("elapsed_seconds", 0))
        _positive(self._elapsed_before, "elapsed_seconds", zero=True)
        self._started = time.monotonic()

    @property
    def tokens(self) -> int:
        return sum(row["tokens"] for row in self.data["sessions"].values())

    @property
    def elapsed(self) -> float:
        return self._elapsed_before + time.monotonic() - self._started

    def phase_totals(self, prefix: str) -> tuple[int, int]:
        rows = [row for row in self.data["sessions"].values() if row["phase"].startswith(prefix)]
        return sum(row["tokens"] for row in rows), sum(row["steps"] for row in rows)

    def observe(self, phase: str, session_id: str, tokens: int, steps: int) -> None:
        _positive(tokens, "session tokens", integer=True, zero=True)
        _positive(steps, "session steps", integer=True, zero=True)
        row = self.data["sessions"].setdefault(str(session_id), {"phase": phase, "tokens": 0, "steps": 0})
        if row["phase"] != phase:
            raise ValueError("a session id cannot belong to two phases")
        row["tokens"] = max(row["tokens"], int(tokens))
        row["steps"] = max(row["steps"], int(steps))

    def snapshot(self) -> dict[str, Any]:
        self.data["elapsed_seconds"] = self.elapsed
        return copy.deepcopy(self.data)


class EvolutionAdapter:
    """Domain prompts, native tools, executed checks and optional state storage."""

    def __init__(self, *, check_commands: Sequence[Any] = (), tools: Sequence[Tool] | str | None = None) -> None:
        if isinstance(check_commands, str) or not isinstance(check_commands, Sequence):
            raise ValueError("check_commands must be a sequence")
        self.check_commands = tuple(check_commands)
        self._tools = tools
        self._progress_version = 0
        self._last_progress = -math.inf
        self._source_versions: dict[str, int] = {}

    def _observed(self, path: str | None) -> None:
        self._progress_version += 1
        self._last_progress = time.monotonic()
        if path is not None:
            self._source_versions[path] = self._progress_version

    def group_prompt(self, group: EvolutionGroup, state: EvolutionState) -> str:
        return group.prompt + "\n\nPrior executed groups and their checks\n" + json.dumps(
            state.data["groups"], ensure_ascii=False,
        )

    def repair_prompt(self, check: EvolutionCheck, state: EvolutionState) -> str:
        return "Repair the failures shown by these executed checks and verify the changed behavior.\n" + json.dumps(
            check.report, ensure_ascii=False,
        )

    def tools(self, phase: str) -> Sequence[Tool] | str | None:
        return self._tools if self._tools is not None else _coding_tools(self._observed)

    def source_snapshot(self) -> Mapping[str, object]:
        return dict(self._source_versions)

    def affects_prior_groups(self, changed_files: Sequence[str]) -> bool:
        return False

    def progress(self) -> tuple[int, bool]:
        return self._progress_version, time.monotonic() - self._last_progress <= 60

    async def verify(self, ctx: WorkflowContext, targets: tuple[str, ...] | None,
                     seconds: float | None) -> EvolutionCheck:
        report = await _verify_commands(ctx, self.check_commands, seconds)
        rows = report["checks"]
        return EvolutionCheck(
            report["ok"], report["executed"], report,
            tuple(row["command"] for row in rows if row["executed"] and not row["ok"] and (
                row.get("exit_code") not in {0, 5} or row.get("expected_output") is not None
            )),
            tuple(row["command"] for row in rows if row["ok"]),
        )

    def improved(self, before: EvolutionCheck, after: EvolutionCheck) -> bool:
        return after.passed or bool(set(after.progress_markers) - set(before.progress_markers))

    def save_state(self, data: dict[str, Any]) -> None:
        pass

    def on_update(self, kind: str, state: EvolutionState, data: dict[str, Any]) -> None:
        pass

    def on_event(self, phase: str, event: RunEvent) -> None:
        pass


def plan_evolution_groups(groups: Sequence[EvolutionGroup]) -> list[EvolutionGroup]:
    """Order groups by dependencies, combining a dependency cycle into one session.

    Dependencies naming group ids become target ids. The planned descriptors
    can consequently be passed through this function again after cycle merging.
    Input order resolves independent ready groups and the order within a cycle.
    """
    if not groups or any(not isinstance(group, EvolutionGroup) for group in groups):
        raise ValueError("groups must contain at least one EvolutionGroup")
    owners: dict[str, int] = {}
    for number, group in enumerate(groups):
        for key in (group.id, *group.targets):
            if key in owners and owners[key] != number:
                raise ValueError(f"duplicate group or target id {key!r}")
            owners[key] = number
    edges = {}
    for number, group in enumerate(groups):
        unknown = set(group.dependencies) - owners.keys()
        if unknown:
            raise ValueError(f"unknown group dependencies {sorted(unknown)}")
        edges[number] = {owners[key] for key in group.dependencies} - {number}
    indices, lows, stack, active, components = {}, {}, [], set(), []

    def visit(node: int) -> None:
        indices[node] = lows[node] = len(indices)
        stack.append(node)
        active.add(node)
        for dependency in sorted(edges[node]):
            if dependency not in indices:
                visit(dependency)
                lows[node] = min(lows[node], lows[dependency])
            elif dependency in active:
                lows[node] = min(lows[node], indices[dependency])
        if lows[node] == indices[node]:
            members = []
            while True:
                member = stack.pop()
                active.remove(member)
                members.append(member)
                if member == node:
                    break
            components.append(sorted(members))

    for number in range(len(groups)):
        if number not in indices:
            visit(number)
    component_owner = {member: number for number, members in enumerate(components) for member in members}
    pending, ordered, done = set(range(len(components))), [], set()
    while pending:
        ready = [number for number in pending if {
            component_owner[dep] for member in components[number] for dep in edges[member]
        } - {number} <= done]
        number = min(ready, key=lambda item: min(components[item]))
        pending.remove(number)
        done.add(number)
        members = [groups[item] for item in components[number]]
        local = {key for group in members for key in (group.id, *group.targets)}
        targets = {key for group in groups for key in group.targets}
        dependencies = tuple(dict.fromkeys(
            key if key in targets else groups[owners[key]].targets[0]
            for group in members for key in group.dependencies if key not in local
        ))
        if len(members) == 1 and dependencies == members[0].dependencies:
            ordered.append(members[0])
            continue
        ordered.append(EvolutionGroup(
            id=members[0].id, prompt="\n\n".join(group.prompt for group in members),
            weight=sum(group.weight for group in members),
            dependencies=dependencies,
            resources=tuple(dict.fromkeys(key for group in members for key in group.resources)),
            targets=tuple(key for group in members for key in group.targets),
        ))
    return ordered


def _allocation(remaining: int | None, weights: Sequence[float], minimum: int) -> int | None:
    if remaining is None:
        return None
    if not weights or remaining < minimum:
        return 0
    floor = min(minimum, remaining // len(weights))
    share = int(remaining * weights[0] / sum(weights))
    return min(remaining - floor * (len(weights) - 1), max(minimum, share))


def _minimum(*values: float | int | None) -> float:
    return min((value for value in values if value is not None), default=math.inf)


def _optional(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _affected(group: EvolutionGroup, completed: Sequence[tuple[EvolutionGroup, Sequence[str]]],
              changed: Sequence[str], *, broad: bool = False,
              dependency_owners: Mapping[str, str] | None = None) -> tuple[str, ...]:
    affected = set(group.targets)
    affected_groups = {group.id}
    owners = dependency_owners or {
        key: item.id for item in (group, *(prior for prior, _paths in completed)) for key in (item.id, *item.targets)
    }
    group_dependencies = {owners[key] for key in group.dependencies if key in owners}
    while True:
        previous = set(affected_groups)
        for prior, paths in completed:
            prior_dependencies = {owners[key] for key in prior.dependencies if key in owners}
            if broad or set(changed).intersection(paths) or set(group.resources).intersection(prior.resources) \
                    or prior_dependencies.intersection(affected_groups) or prior.id in group_dependencies:
                affected.update(prior.targets)
                affected_groups.add(prior.id)
        if previous == affected_groups:
            return tuple(sorted(affected))


class _EvolutionRun:
    def __init__(self, ctx: WorkflowContext, groups: Sequence[EvolutionGroup], config: EvolutionConfig,
                 adapter: EvolutionAdapter, state: EvolutionState) -> None:
        self.ctx, self.config, self.adapter, self.state = ctx, config, adapter, state
        self.groups = plan_evolution_groups(groups)
        target_owners = {key: group.id for group in self.groups for key in group.targets}
        self.dependency_owners = {key: target_owners[group.targets[0]]
                                  for group in groups for key in (group.id, *group.targets)}
        plan = [asdict(group) for group in self.groups]
        plan = json.loads(json.dumps(plan))
        if "plan" in state.data and state.data["plan"] != plan:
            raise ValueError("continuation requires the same groups")
        state.data["plan"] = plan
        context_run_id = getattr(ctx, "run_id", None)
        if context_run_id is not None and state.data["run_id"] != context_run_id:
            raise ValueError("continuation requires the original context run_id")
        available = self._live_tokens()
        if "budget_total" not in state.data:
            total = _minimum(config.budget, None if available is None else available + state.tokens)
            state.data["budget_total"] = None if not math.isfinite(total) else int(total)
        self.total = state.data["budget_total"]
        if config.budget is not None:
            self.total = min(self.total, config.budget) if self.total is not None else config.budget
            state.data["budget_total"] = self.total
        self.state.data["status"] = "running"

    def _live_tokens(self) -> int | None:
        value = self.ctx.tokens_remaining()
        return None if value is None or value == math.inf else max(0, int(value))

    def tokens_left(self) -> int | None:
        value = _minimum(self._live_tokens(), None if self.total is None else max(0, self.total - self.state.tokens))
        return None if not math.isfinite(value) else int(value)

    def seconds_left(self) -> float:
        return max(0, _minimum(self.ctx.seconds_left(), None if self.config.wall_seconds is None
                               else self.config.wall_seconds - self.state.elapsed))

    def persist(self, kind: str | None = None, **data: Any) -> None:
        if kind is not None:
            self.adapter.on_update(kind, self.state, data)
        self.adapter.save_state(self.state.snapshot())

    async def heartbeat(self, label: str) -> None:
        while True:
            self.persist("heartbeat", phase=label, total_tokens=self.state.tokens)
            await asyncio.sleep(self.config.heartbeat_seconds)

    async def solve(self, prompt: str, *, soft: int | None, hard: int | None, steps: int | None,
                    seconds: float | None, label: str) -> dict[str, Any]:
        config, state, adapter = self.config, self.state, self.adapter
        last_extended, last_notice, compactions = 0, None, 0

        def event(observation: RunEvent) -> None:
            nonlocal compactions
            if observation.session_id:
                state.observe(label, observation.session_id, observation.used_tokens, observation.steps)
            if observation.type == "context_shaping":
                reports = observation.data.get("reports", [observation.data])
                compactions += sum(row.get("rung") in {"tool_output_clear", "old_history_snip", "auto_compact"}
                                   for row in reports)
            adapter.on_event(label, observation)
            self.persist()

        def decide(snapshot: BudgetSnapshot) -> BudgetDecision:
            nonlocal last_extended, last_notice
            cap = snapshot.soft_budget_tokens
            if cap is None:
                return BudgetDecision(None)
            headroom = max(snapshot.minimum_output_tokens, config.max_output_tokens)
            margin = max(1, int(cap * config.extension_margin_fraction))
            if config.extension_margin_tokens is not None:
                margin = min(config.extension_margin_tokens, margin)
            if cap - snapshot.used_tokens >= snapshot.reserved_input_tokens + headroom + margin:
                return BudgetDecision(cap)
            version, recent = adapter.progress()
            ceiling = _minimum(hard, snapshot.hard_budget_tokens)
            if recent and version > last_extended and cap < ceiling:
                proposed = min(ceiling, max(cap + config.extension_tokens,
                                          snapshot.used_tokens + snapshot.reserved_input_tokens + headroom))
                last_extended = version
                self.persist("budget_extended", phase=label, old_cap=cap, new_cap=int(proposed),
                             hard_cap=_optional(ceiling), tokens=snapshot.used_tokens, same_session=True)
                return BudgetDecision(int(proposed))
            prompt = None
            if last_notice != cap:
                last_notice = cap
                prompt = config.budget_final_prompt or (
                    "Finish the current operation and focused checks, then record the remaining work. "
                    "Recent source changes or executed check evidence can release available additional allowance."
                )
            return BudgetDecision(cap, final_prompt=prompt)

        heartbeat = asyncio.create_task(self.heartbeat(label))
        try:
            result = await self.ctx.agent_run(
                prompt, tools=adapter.tools(label), budget=hard, timeout=seconds, max_steps=steps,
                system_prompt=config.system_prompt, label=label, cleanup_timeout=config.cleanup_seconds,
                run_control=RunControl(initial_soft_budget_tokens=soft, decide_budget=decide, on_event=event,
                                       history_trigger_tokens=config.history_trigger_tokens,
                                       tool_cancellation_cleanup_timeout=config.tool_cleanup_seconds),
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if result.session_id:
            state.observe(label, result.session_id, result.tokens, result.steps)
        if not result.cleanup_complete or not result.workspace_ready:
            raise RuntimeError("agent cleanup is incomplete")
        if result.observation_errors:
            raise RuntimeError("run observations failed")
        return {"status": result.status, "reason": result.reason, "tokens": result.tokens,
                "steps": result.steps, "session_id": result.session_id, "output": result.output,
                "soft_budget_tokens": result.soft_budget_tokens, "hard_budget_tokens": result.hard_budget_tokens,
                "compactions": compactions}

    async def check(self, targets: tuple[str, ...] | None, seconds: float | None) -> EvolutionCheck:
        check = await self.adapter.verify(self.ctx, targets, seconds)
        if not isinstance(check, EvolutionCheck):
            raise TypeError("verify must return an EvolutionCheck")
        return check

    async def full_check(self) -> EvolutionCheck:
        seconds = max(0, min(self.seconds_left() - self.config.final_check_time_reserve,
                             _minimum(self.config.final_check_timeout)))
        check = await self.check(None, _optional(seconds))
        self.state.data["verification"] = asdict(check)
        self.persist("check_finished", number=len(self.state.data["repair_rounds"]), check=check)
        return check

    async def repair(self) -> tuple[EvolutionCheck, str]:
        await self.ctx.phase("Full delivery verification")
        report = await self.full_check()
        rounds, config = self.state.data["repair_rounds"], self.config
        stagnant = int(rounds[-1].get("stagnant_rounds", 0)) if rounds else 0
        stop = "passed" if report.passed else "round_limit"
        if not report.passed and stagnant >= config.stagnant_round_limit:
            return report, "rounds_without_observable_progress"
        while not report.passed and len(rounds) < config.max_repair_rounds:
            available, seconds = self.tokens_left(), self.seconds_left() - config.repair_time_reserve
            if (available is not None and available < config.minimum_group_tokens) \
                    or seconds < config.minimum_repair_seconds or seconds <= 0:
                stop = "budget_or_time_reserve"
                break
            if not report.repair_targets:
                stop = "environment_or_unverified"
                break
            number = len(rounds) + 1
            allowance = available if number == config.max_repair_rounds or available is None else max(
                config.minimum_group_tokens, int(available * config.repair_budget_fraction),
            )
            allowance_value = _minimum(config.repair_budget, allowance)
            budget = None if not math.isfinite(allowance_value) else int(allowance_value)
            row = {"round": number, "status": "running", "targets": list(report.repair_targets)}
            rounds.append(row)
            self.persist("repair_started", number=number, check=report, allocation={"hard_tokens": budget})
            await self.ctx.phase(f"Repair round {number}")
            timeout = min(_minimum(config.repair_timeout), max(config.minimum_session_seconds,
                                                               seconds - config.repair_agent_time_reserve))
            info = await self.solve(self.adapter.repair_prompt(report, self.state), soft=budget, hard=budget,
                                    steps=config.repair_steps, seconds=_optional(timeout), label=f"repair-{number}")
            before, report = report, await self.full_check()
            advanced = self.adapter.improved(before, report)
            stagnant = 0 if advanced else stagnant + 1
            row.update(info, observable_progress=advanced, stagnant_rounds=stagnant)
            self.persist("repair_finished", number=number, result=row, check=report)
            if report.passed:
                stop = "passed"
            elif stagnant >= config.stagnant_round_limit:
                stop = "rounds_without_observable_progress"
                break
        return report, stop

    async def run(self) -> dict[str, Any]:
        config, state = self.config, self.state
        reserve = config.repair_reserve if self.total is None else min(config.repair_reserve, self.total // 4)
        hard_value = _minimum(config.main_hard_budget, None if self.total is None else max(0, self.total - reserve))
        hard = None if not math.isfinite(hard_value) else int(hard_value)
        soft_value = _minimum(config.main_budget, hard)
        soft = None if not math.isfinite(soft_value) else int(soft_value)
        started = state.data.setdefault("main_started_elapsed", state.elapsed)
        elapsed = max(0, state.elapsed - started)
        window = max(0, min(_minimum(config.agent_seconds) - elapsed - config.main_agent_time_reserve,
                            self.seconds_left() - config.main_wall_time_reserve))
        deadline, completed, stop = time.monotonic() + window, [], "all_groups_attempted"
        self.persist()
        for number, group in enumerate(self.groups, 1):
            saved = state.data["groups"].get(str(number))
            if saved and saved.get("status") == "checked":
                completed.append((group, saved["changed_files"]))
                continue
            weights = [item.weight for item in self.groups[number - 1:]]
            tokens, steps = state.phase_totals("group-")
            available = self.tokens_left()
            remaining_hard = _minimum(available, None if hard is None else max(0, hard - tokens))
            cap = _allocation(None if not math.isfinite(remaining_hard) else int(remaining_hard), weights,
                              config.minimum_group_tokens)
            remaining_soft = _minimum(available, None if soft is None else max(0, soft - tokens))
            allocation = _allocation(None if not math.isfinite(remaining_soft) else int(remaining_soft), weights,
                                     config.minimum_group_tokens)
            budget = None if cap is None and allocation is None else min(
                cap if cap is not None else math.inf,
                max(config.minimum_group_tokens, allocation if allocation is not None else cap),
            )
            pass_steps = _allocation(None if config.max_steps is None else max(0, config.max_steps - steps),
                                     weights, config.minimum_group_steps)
            window = max(0, deadline - time.monotonic()) * weights[0] / sum(weights)
            group_deadline = time.monotonic() + window
            if not saved or saved.get("status") != "generated":
                if (cap is not None and cap < config.minimum_group_tokens) \
                        or (pass_steps is not None and pass_steps < config.minimum_group_steps) \
                        or window < config.minimum_group_seconds or window <= 0:
                    stop = "main_budget_steps_or_time_reserve"
                    break
                check_reserve = min(_minimum(config.group_check_max_seconds),
                                    max(config.group_check_min_seconds, window * config.group_check_fraction))
                if not math.isfinite(check_reserve):
                    check_reserve = config.group_check_min_seconds
                allocation_info = {"soft_tokens": budget, "hard_tokens": cap, "steps": pass_steps,
                                   "seconds": _optional(window)}
                self.persist("group_started", group=group, number=number, allocation=allocation_info)
                await self.ctx.phase(f"Task group {number}")
                before = dict(self.adapter.source_snapshot())
                timeout = max(config.minimum_session_seconds, window - check_reserve - config.cleanup_seconds)
                if timeout <= 0:
                    stop = "main_budget_steps_or_time_reserve"
                    break
                info = await self.solve(self.adapter.group_prompt(group, state), soft=budget, hard=cap,
                                        steps=pass_steps, seconds=_optional(timeout), label=f"group-{number}")
                after = dict(self.adapter.source_snapshot())
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                saved = {"status": "generated", "changed_files": changed, "result": info}
                state.data["groups"][str(number)] = saved
                self.persist("group_generated", group=group, number=number, result=info, changed_files=changed)
            targets = _affected(group, completed, saved["changed_files"],
                                broad=self.adapter.affects_prior_groups(saved["changed_files"]),
                                dependency_owners=self.dependency_owners)
            seconds = min(self.seconds_left(), max(0, group_deadline - time.monotonic())
                          + config.group_check_grace_seconds, _minimum(config.group_check_timeout))
            check = await self.check(targets, _optional(seconds))
            saved.update(status="checked", verification=asdict(check))
            self.persist("group_checked", group=group, number=number, targets=targets, check=check,
                         changed_files=saved["changed_files"], result=saved["result"])
            completed.append((group, saved["changed_files"]))
        report, repair_stop = await self.repair()
        status = "completed" if report.passed else "failed" if report.executed else "unverified"
        state.data.update(status=status, delivery_ok=report.passed)
        result = {"run_id": state.data["run_id"], "status": status, "delivery_ok": report.passed,
                  "total_tokens": state.tokens, "elapsed_seconds": state.elapsed,
                  "main_stop_reason": stop, "repair_stop_reason": repair_stop,
                  "main_steps": state.phase_totals("group-")[1],
                  "unstarted_ids": [key for number, group in enumerate(self.groups, 1)
                                    if str(number) not in state.data["groups"] for key in group.targets],
                  "groups": copy.deepcopy(state.data["groups"]), "verification": asdict(report),
                  "repair_rounds": copy.deepcopy(state.data["repair_rounds"])}
        self.persist("finished", result=result, check=report)
        result["state"] = state.snapshot()
        return result


async def run_evolution(ctx: WorkflowContext, groups: Sequence[EvolutionGroup], *,
                        config: EvolutionConfig | None = None, adapter: EvolutionAdapter | None = None,
                        state: EvolutionState | None = None) -> dict[str, Any]:
    """Run supplied groups on the context's shared workspace using fresh managed sessions."""
    config = config or EvolutionConfig()
    adapter = adapter or EvolutionAdapter()
    state = state or EvolutionState(run_id=getattr(ctx, "run_id", None))
    runner = _EvolutionRun(ctx, groups, config, adapter, state)
    try:
        return await runner.run()
    except BaseException as error:
        state.data.update(status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                          delivery_ok=False, error_type=type(error).__name__)
        runner.persist("finished", error_type=type(error).__name__, status=state.data["status"])
        raise


@workflow(
    name="evolution",
    description="Independent agents execute weighted task groups with real checks and bounded repair",
    phases=["implement", "verify", "repair", "deliver"],
)
async def evolution(ctx: WorkflowContext, args: dict[str, Any]) -> dict[str, Any]:
    """Execute JSON groups and check commands through the native workflow runtime.

    Each group supplies an id and prompt, with optional weight, dependencies,
    resources and targets. Config fields are optional. Test commands retain
    parser-backed passing evidence; command probes supply expected_output.
    """
    values = args.get("groups")
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError("groups must be a sequence of JSON objects")
    groups = [EvolutionGroup(**value) for value in values]
    config = EvolutionConfig(**args.get("config", {}))
    adapter = EvolutionAdapter(check_commands=args.get("check_commands", ()))
    saved = args.get("state")
    state = EvolutionState(saved, run_id=getattr(ctx, "run_id", None))
    return await run_evolution(ctx, groups, config=config, adapter=adapter, state=state)


__all__ = ["EvolutionAdapter", "EvolutionCheck", "EvolutionConfig", "EvolutionGroup", "EvolutionState",
           "evolution", "plan_evolution_groups", "run_evolution"]
