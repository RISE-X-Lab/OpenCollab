"""Competition operations and report translation for the native evolution workflow."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading

from arc_light.acceptance import AcceptanceTool
from arc_light.control import CheckpointTool, RunProgress
from arc_light.delivery import verify
from arc_light.evidence import improvement, repair_frontier, source_digest, summarize
from arc_light.planning import (
    affects_prior_groups,
    feature_groups,
    group_prompt,
    group_weight,
    plan_summary,
    source_inventory,
)
from arc_light.progress import monitor_tools
from arc_light.reports import write_json
from arc_light.spec import report_summary
from opencollab.builtin_workflows.evolution import (
    EvolutionAdapter,
    EvolutionCheck,
    EvolutionGroup,
    EvolutionState,
    run_evolution,
)

from opencollab.tools import builtin_tools, profile_tool_limits

from .prompts import _repair_prompt, workspace_context
from .session_control import SessionMetrics


class EvolutionRunner(EvolutionAdapter):
    def __init__(self, ctx, workspace, index, product, settings, state, commit, emit, *, resume=False):
        super().__init__()
        self.ctx, self.workspace, self.index, self.product = ctx, workspace, index, product
        self.settings, self.state, self.commit, self.emit = settings, state, commit, emit
        previous = None
        progress_path = workspace / ".arc/checks/implementation-ledger.json"
        if resume and progress_path.exists():
            previous = json.loads(progress_path.read_text(encoding="utf-8"))
        self.ledger = RunProgress(workspace, index, emit)
        if previous:
            for field, key in (
                ("rows", "requirements"),
                ("execution", "scheduled_execution"),
                ("group_runs", "group_runs"),
                ("acceptance", "coordinator_acceptance"),
            ):
                setattr(self.ledger, field, previous.get(key, getattr(self.ledger, field)))
            self.ledger.changed_files = set(previous.get("changed_files", []))
        self.groups = feature_groups(index)
        self.ledger.schedule = plan_summary(self.groups)
        self.ledger.persist()
        self.metrics = SessionMetrics(workspace)
        self.verdicts = {}
        self.state.persist()
        self.core_state = EvolutionState(self.state.data, run_id=self.state.run_id)
        self.state.bind_evolution(self.core_state)

    def remaining(self):
        return self.settings.wall_seconds - self.core_state.elapsed

    def _domain_group(self, group, number):
        selected = set(group.targets)
        rows = [row for row in self.index if row["id"] in selected]
        return {
            "number": number,
            "ids": [row["id"] for row in rows],
            "rows": rows,
            "resources": list(group.resources),
            "dependencies": list(group.dependencies),
        }

    def group_prompt(self, group, state):
        return (
            workspace_context(self.workspace)
            + f"Current product: {self.product}.\n"
            + group_prompt(self._domain_group(group, 0), self.ledger)
        )

    def repair_prompt(self, check, state):
        return workspace_context(self.workspace) + _repair_prompt(check.report)

    def tools(self, phase):
        coding = builtin_tools(
            "bash",
            "file_read",
            "file_write",
            "apply_patch",
            "grep",
            "git_diff",
            headless=False,
            limits=profile_tool_limits("single2"),
        )
        return monitor_tools(self.workspace, coding, observer=self.ledger) + [
            CheckpointTool(self.ledger),
            AcceptanceTool(self.ledger, self.remaining),
        ]

    def source_snapshot(self):
        return source_inventory(self.workspace)

    def affects_prior_groups(self, changed_files):
        return affects_prior_groups(changed_files)

    def progress(self):
        return self.ledger.progress_version, self.ledger.recent_progress()

    @staticmethod
    def _check(report, *, executed=True):
        return EvolutionCheck(
            ok=bool(report.get("ok")),
            executed=executed,
            report=report,
            repair_targets=tuple(item["name"] for item in repair_frontier(report)),
        )

    async def verify(self, ctx, targets, seconds):
        if targets is not None:
            tool = AcceptanceTool(self.ledger, lambda: seconds if seconds is not None else self.remaining())
            verdict = await ctx.execute_verification(tool, {"scope": "requirements", "requirement_ids": list(targets)})
            self.verdicts[tuple(targets)] = verdict
            try:
                result = json.loads(verdict)
            except (TypeError, ValueError):
                return self._check({"ok": False, "scope": "requirements", "checks": []}, executed=False)
            report_path = result.get("report_path")
            if not report_path:
                return self._check(result, executed=False)
            report = json.loads(self.workspace.joinpath(report_path).read_text(encoding="utf-8"))
            if result.get("stale"):
                report.update(ok=False, stale=True)
            return self._check(report)
        self.ledger.verification_phase = "final"
        cancel = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                verify,
                self.workspace,
                timeout=max(1, seconds if seconds is not None else min(600, self.remaining() - 60)),
                confirm_stability=True,
                cancel_event=cancel,
            )
        )
        try:
            report = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            await task
            raise
        return self._check(report)

    def improved(self, before, after):
        return improvement(before.report, after.report)

    def save_state(self, data):
        self.state.data = data
        write_json(self.state.path, data)

    def on_event(self, phase, event):
        self.metrics.event(phase, event)

    def _session_report(self, result):
        info = dict(result)
        info["final_summary"] = report_summary(info.pop("output", info.get("final_summary", "")))
        info["acceptance"] = "not_evaluated"
        info["budget_cap_at_end"] = info.pop("soft_budget_tokens", info.get("budget_cap_at_end"))
        info["hard_budget"] = info.pop("hard_budget_tokens", info.get("hard_budget"))
        return info

    def on_update(self, kind, state, data):
        if kind in {"group_started", "group_generated"}:
            group = self._domain_group(data["group"], data["number"])
            if kind == "group_started":
                allocation = dict(data["allocation"])
                if allocation["seconds"] is not None:
                    allocation["seconds"] = round(allocation["seconds"])
                self.ledger.begin_group(group, allocation)
            else:
                info = self._session_report(data["result"])
                self.ledger.finish_group(group, info, data["changed_files"])
                state.data["groups"][str(data["number"])]["result"] = info
                self.commit(f"r6 implementation group {data['number']}")
        elif kind == "group_checked":
            verdict = self.verdicts.get(tuple(data["targets"]))
            if verdict is not None:
                state.data["groups"][str(data["number"])]["verification"] = verdict
        elif kind == "check_finished":
            self._save_check(data["number"], data["check"].report)
        elif kind == "repair_finished":
            row = state.data["repair_rounds"][-1]
            info = self._session_report(data["result"])
            row.clear()
            row.update(info)
        elif kind == "budget_extended":
            self.emit({"diag": "budget_extended", **data})
        elif kind == "heartbeat":
            self.emit(
                {
                    "diag": "progress",
                    "phase": data["phase"],
                    "total_tokens": state.tokens,
                    "active_tools": list(self.ledger.active_tools.values()),
                }
            )

    def _save_check(self, number, report):
        destination = self.workspace / ".arc/checks/repair-history" / str(number)
        destination.mkdir(parents=True, exist_ok=True)
        for name in (
            "verification.json",
            "browser-report.json",
            "scenario-ledger.json",
            "restart-diff.json",
            "stability-report.json",
            "stability-history.json",
            "backend.log",
        ):
            path = self.workspace / ".arc/checks" / name
            if path.is_file():
                shutil.copy2(path, destination / name)
        self.ledger.record_acceptance(report, source_digest(self.workspace))

    async def run(self):
        targets = {key for group in self.groups for key in group["ids"]}
        groups = []
        for number, group in enumerate(self.groups, 1):
            identifier = f"arc-group-{number}"
            while identifier in targets:
                identifier = "_" + identifier
            groups.append(EvolutionGroup(
                id=identifier,
                prompt="Implement the current requirement group.",
                weight=group_weight(group),
                dependencies=tuple(group["dependencies"]),
                resources=tuple(group["resources"]),
                targets=tuple(group["ids"]),
            ))
        result = await run_evolution(
            self.ctx, groups, config=self.settings.evolution_config(), adapter=self, state=self.core_state
        )
        return self.state.finish(
            status="completed",
            delivery_ok=result["delivery_ok"],
            main_stop_reason=result["main_stop_reason"],
            repair_stop_reason=(
                "two_rounds_without_observable_progress"
                if result["repair_stop_reason"] == "rounds_without_observable_progress"
                else result["repair_stop_reason"]
            ),
            main_steps=self.core_state.phase_totals("group-")[1],
            unstarted_ids=[key for key, row in self.ledger.execution.items() if row["status"] == "not_started"],
            scheduled_execution=self.ledger.execution,
            coordinator_acceptance=self.ledger.acceptance,
            verification=summarize(result["verification"]["report"]),
        )
