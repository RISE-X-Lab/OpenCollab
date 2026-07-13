"""Workflow-backed solver implementation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from opencollab.harness.eval_adapter import (
    PatchCandidate,
    PreparedWorkspace,
    docker_environment_for_workspace,
)
from opencollab.harness.solver_backend import (
    SolverBudget,
    SolverContractError,
    SolverTaskView,
    WorkflowSolverSpec,
)
from opencollab.sdk import (
    OpenCollabRuntime,
    RuntimeConfig,
    WorkflowRunRequest,
    discover_workflows,
)
from opencollab.sdk import (
    RunBudget as SDKRunBudget,
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
        runtime: OpenCollabRuntime | None = None,
    ) -> None:
        self.spec = spec
        self.name = spec.name
        self._cfg = {**cfg, **spec.config_overrides}
        self._workflows_dir = Path(workflows_dir)
        self._max_concurrency = max(1, max_concurrency)
        self._runtime = runtime or OpenCollabRuntime()

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
        result = await self._runtime.run_workflow(
            WorkflowRunRequest(
                workflow=workflow_spec,
                inputs=args,
                config=RuntimeConfig.from_mapping(self._cfg),
                budget=SDKRunBudget(
                    max_tokens=self._effective_budget(budget),
                    timeout_seconds=budget.timeout_seconds,
                    max_concurrency=self._max_concurrency,
                ),
                environment=env,
                workspace=workspace.repo_root,
                artifact_dir=run_dir,
            )
        )
        diff = await _tracked_diff(env)
        return PatchCandidate(
            task_id=task.task_id,
            solver_name=self.name,
            patch=diff,
            log_path=str(run_dir),
            token_count=result.tokens_spent or 0,
            metadata={
                "sdk_api_version": result.sdk_api_version,
                "workflow_name": result.workflow_name,
                "workflow_output": result.output,
            },
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
    if result.returncode != 0 or result.stdout_truncated or result.stderr_truncated:
        raise SolverContractError("workflow patch extraction command failed")
    return result.stdout


__all__ = ["WorkflowBackend"]
