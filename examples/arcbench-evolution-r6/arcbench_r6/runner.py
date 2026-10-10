"""Serial independent Single2 instances, executable checks and bounded repair."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from contextlib import suppress

from arc_light.acceptance import AcceptanceTool
from arc_light.control import CheckpointTool, RunProgress
from arc_light.delivery import verify
from arc_light.evidence import improvement, repair_frontier, source_digest, summarize
from arc_light.planning import (
    affected_requirements,
    allocation,
    feature_groups,
    group_prompt,
    group_weight,
    plan_summary,
    source_inventory,
)
from arc_light.progress import monitor_tools
from arc_light.spec import report_summary

from opencollab.tools import builtin_tools, profile_tool_limits

from .prompts import SYSTEM_PROMPT, _repair_prompt, workspace_context
from .session_control import SessionPolicy


class EvolutionRunner:
    def __init__(self, ctx, workspace, index, product, settings, state, commit, emit, *, resume=False):
        self.ctx, self.workspace, self.index, self.product = ctx, workspace, index, product
        self.settings, self.state, self.commit, self.emit = settings, state, commit, emit
        previous = None
        progress_path = workspace / ".arc/checks/implementation-ledger.json"
        if resume and progress_path.exists():
            previous = json.loads(progress_path.read_text(encoding="utf-8"))
        self.progress = RunProgress(workspace, index, emit)
        if previous:
            for field, key in (
                ("rows", "requirements"),
                ("execution", "scheduled_execution"),
                ("group_runs", "group_runs"),
                ("acceptance", "coordinator_acceptance"),
            ):
                setattr(self.progress, field, previous.get(key, getattr(self.progress, field)))
            self.progress.changed_files = set(previous.get("changed_files", []))
        self.groups = feature_groups(index)
        self.progress.schedule = plan_summary(self.groups)
        self.progress.persist()

    def remaining(self):
        return self.settings.wall_seconds - self.state.elapsed

    def tools(self):
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
        return monitor_tools(self.workspace, coding, observer=self.progress) + [
            CheckpointTool(self.progress),
            AcceptanceTool(self.progress, self.remaining),
        ]

    async def _heartbeat(self, label):
        while True:
            self.state.persist()
            self.emit(
                {
                    "diag": "progress",
                    "phase": label,
                    "total_tokens": self.state.tokens,
                    "active_tools": list(self.progress.active_tools.values()),
                }
            )
            await asyncio.sleep(60)

    async def solve(self, prompt, *, soft, hard, steps, seconds, label):
        policy = SessionPolicy(self.progress, label, self.settings, self.state, soft, hard)
        heartbeat = asyncio.create_task(self._heartbeat(label))
        try:
            result = await self.ctx.agent_run(
                prompt,
                tools=self.tools(),
                budget=hard,
                run_control=policy.control(),
                max_steps=steps,
                timeout=seconds,
                cleanup_timeout=self.settings.cleanup_seconds,
                system_prompt=SYSTEM_PROMPT,
                label=label,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if result.session_id is not None:
            self.state.observe(label, result.session_id, result.tokens, result.steps)
        if not result.cleanup_complete or not result.workspace_ready:
            raise RuntimeError("Agent cleanup is incomplete; the shared application is retained")
        if result.observation_errors:
            raise RuntimeError("Run observations could not be persisted; candidate and session records are retained")
        return {
            "status": result.status,
            "reason": result.reason,
            "tokens": result.tokens,
            "steps": result.steps,
            "session_id": result.session_id,
            "final_summary": report_summary(result.output),
            "acceptance": "not_evaluated",
            "budget_cap_at_end": result.soft_budget_tokens,
            "compactions": policy.compactions,
            "hard_budget": result.hard_budget_tokens,
        }

    async def check(self):
        cancel = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                verify,
                self.workspace,
                timeout=max(1, min(600, self.remaining() - 60)),
                confirm_stability=True,
                cancel_event=cancel,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            await task
            raise

    def save_check(self, number, report):
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
        self.progress.record_acceptance(report, source_digest(self.workspace))

    async def repair(self):
        self.progress.verification_phase = "final"
        await self.ctx.phase("Full delivery verification")
        report = await self.check()
        rounds = self.state.data["repair_rounds"]
        self.save_check(len(rounds), report)
        stagnant = int(rounds[-1].get("stagnant_rounds", 0)) if rounds else 0
        stop_reason = "passed" if report["ok"] else "round_limit"
        if not report["ok"] and stagnant >= 2:
            return report, "two_rounds_without_observable_progress"
        while not report["ok"] and len(rounds) < 3:
            budget_left = self.settings.budget - self.state.tokens
            time_left = self.remaining() - 300
            if budget_left < 200_000 or time_left < 120:
                stop_reason = "budget_or_time_reserve"
                break
            targets = repair_frontier(report)
            if not targets:
                stop_reason = "environment_or_unverified"
                break
            number = len(rounds) + 1
            round_budget = min(2_000_000, budget_left if number == 3 else max(200_000, int(budget_left * 0.65)))
            row = {"round": number, "status": "running", "targets": [item["name"] for item in targets]}
            rounds.append(row)
            self.state.persist()
            await self.ctx.phase(f"Repair round {number}")
            info = await self.solve(
                workspace_context(self.workspace) + _repair_prompt(report),
                soft=round_budget,
                hard=round_budget,
                steps=60,
                seconds=min(600, max(60, time_left - 300)),
                label=f"repair-{number}",
            )
            before = report
            report = await self.check()
            advanced = improvement(before, report)
            stagnant = 0 if advanced else stagnant + 1
            row.update(info, observable_progress=advanced, stagnant_rounds=stagnant)
            self.save_check(number, report)
            self.state.persist()
            if report["ok"]:
                stop_reason = "passed"
            elif stagnant >= 2:
                stop_reason = "two_rounds_without_observable_progress"
                break
        return report, stop_reason

    async def run(self):
        settings = self.settings
        reserve = min(settings.repair_reserve, settings.budget // 4)
        hard = max(1, min(settings.main_hard_budget, settings.budget - reserve))
        soft = min(settings.main_budget, hard)
        main_started = self.state.data.setdefault("main_started_elapsed", self.state.elapsed)
        already_elapsed = max(0, self.state.elapsed - main_started)
        main_seconds = max(0, min(settings.agent_seconds - already_elapsed - 600, self.remaining() - 1500))
        deadline = time.monotonic() + main_seconds
        completed = []
        stop_reason = "all_groups_attempted"
        for number, original in enumerate(self.groups, 1):
            group = dict(original, number=number)
            saved = self.state.data["groups"].get(str(number))
            if saved and saved.get("status") == "checked":
                completed.append(dict(group, changed_files=saved["changed_files"]))
                continue
            weights = [group_weight(g) for g in self.groups[number - 1 :]]
            tokens, steps = self.state.phase_totals("group-")
            cap = allocation(max(0, hard - tokens), weights)
            budget = min(cap, max(allocation(max(0, soft - tokens), weights), 200_000))
            pass_steps = allocation(max(0, settings.max_steps - steps), weights, minimum=3)
            window = max(0, deadline - time.monotonic()) * weights[0] / sum(weights)
            group_deadline = time.monotonic() + window
            if not saved or saved.get("status") != "generated":
                if cap < 200_000 or pass_steps < 3 or window < 150:
                    stop_reason = "main_budget_steps_or_time_reserve"
                    break
                check_reserve = min(240, max(60, window * 0.25))
                self.progress.begin_group(
                    group, {"soft_tokens": budget, "hard_tokens": cap, "steps": pass_steps, "seconds": round(window)}
                )
                await self.ctx.phase(f"Requirement group {number}")
                before = source_inventory(self.workspace)
                info = await self.solve(
                    workspace_context(self.workspace)
                    + f"Current product: {self.product}.\n"
                    + group_prompt(group, self.progress),
                    soft=budget,
                    hard=cap,
                    steps=pass_steps,
                    seconds=max(60, window - check_reserve - settings.cleanup_seconds),
                    label=f"group-{number}",
                )
                after = source_inventory(self.workspace)
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                self.progress.finish_group(group, info, changed)
                self.commit(f"r6 implementation group {number}")
                saved = {"status": "generated", "changed_files": changed, "result": info}
                self.state.data["groups"][str(number)] = saved
                self.state.persist()
            ids = affected_requirements(group, completed, saved["changed_files"])
            tool = AcceptanceTool(
                self.progress,
                lambda: min(
                    self.remaining(),
                    max(0, group_deadline - time.monotonic()) + 90,
                    450,
                ),
            )
            verdict = await self.ctx.execute_verification(tool, {"scope": "requirements", "requirement_ids": ids})
            saved.update(status="checked", verification=verdict)
            self.state.persist()
            completed.append(dict(group, changed_files=saved["changed_files"]))
        report, repair_stop = await self.repair()
        return self.state.finish(
            status="completed",
            delivery_ok=bool(report["ok"]),
            main_stop_reason=stop_reason,
            repair_stop_reason=repair_stop,
            main_steps=self.state.phase_totals("group-")[1],
            unstarted_ids=[key for key, row in self.progress.execution.items() if row["status"] == "not_started"],
            scheduled_execution=self.progress.execution,
            coordinator_acceptance=self.progress.acceptance,
            verification=summarize(report),
        )
