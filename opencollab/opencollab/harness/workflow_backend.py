"""Workflow-backed solver implementation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from opencollab.bootstrap.workflow_runtime import discover_workflows, run_workflow
from opencollab.harness.eval_adapter import (
    PatchCandidate,
    PreparedWorkspace,
    docker_environment_for_workspace,
)
from opencollab.harness.solver_backend import (
    SolverBudget,
    SolverTaskView,
    WorkflowSolverSpec,
)


class WorkflowBackend:
    """Run one OpenCollab workflow as a solver backend."""

    def __init__(
        self,
        *,
        spec: WorkflowSolverSpec,
        cfg: dict[str, Any],
        workflows_dir: Path | str = "workflows",
        max_concurrency: int = 4,
    ) -> None:
        self.spec = spec
        self.name = spec.name
        self._cfg = {**cfg, **spec.config_overrides}
        self._workflows_dir = Path(workflows_dir)
        self._max_concurrency = max(1, max_concurrency)

    def solve(
        self,
        task: SolverTaskView,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        return asyncio.run(self._solve_async(task, workspace, run_dir, budget))

    async def _solve_async(
        self,
        task: SolverTaskView,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        run_dir.mkdir(parents=True, exist_ok=True)
        registry = discover_workflows(str(self._workflows_dir))
        workflow_spec = registry.get(self.spec.workflow_name)
        env = docker_environment_for_workspace(workspace)
        args = {
            "description": task.problem_statement,
            "goal": task.problem_statement,
            "instance_id": task.task_id,
            "repo": task.repo,
            "hints": list(task.hints),
            "public_metadata": dict(task.metadata),
            **self.spec.args,
        }
        result = await run_workflow(
            workflow_spec,
            args,
            cfg=self._cfg,
            workspace=workspace.repo_root,
            budget=self._effective_budget(budget),
            max_concurrency=self._max_concurrency,
            save_dir=str(run_dir),
            env=env,
        )
        diff = await _tracked_diff(env)
        token_count = _result_tokens(result)
        return PatchCandidate(
            task_id=task.task_id,
            solver_name=self.name,
            patch=diff,
            log_path=str(run_dir),
            token_count=token_count,
            metadata={"workflow_result": result},
        )

    def _effective_budget(self, budget: SolverBudget) -> int:
        for value in (
            budget.max_tokens,
            self.spec.default_budget_tokens,
            self._cfg.get("budget"),
        ):
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 1_000_000


async def _tracked_diff(env: Any) -> str:
    result = await env.exec_cmd("git --no-pager diff --binary", timeout=120)
    if result.returncode != 0:
        return ""
    return result.stdout


def _result_tokens(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    try:
        return int(result.get("tokens_spent") or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["WorkflowBackend"]
