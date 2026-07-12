from __future__ import annotations

from pathlib import Path
from typing import Any

from opencollab.adapters.env import ExecResult
from opencollab.bootstrap.workflow_runtime import _load_specs_from_file
from opencollab.harness.eval_adapter import (
    PatchCandidate,
    PreparedWorkspace,
    TaskSpec,
    docker_environment_for_workspace,
)
from opencollab.harness.solver_backend import (
    SolverBackend,
    SolverBudget,
    workflow_solver_spec,
)
from opencollab.harness.workflow_backend import WorkflowBackend


class FakeSolver:
    name = "fake"

    def solve(
        self,
        task: TaskSpec,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        assert workspace.repo_root == "/app"
        assert budget.max_attempts == 1
        return PatchCandidate(
            task_id=task.instance_id,
            solver_name=self.name,
            patch="diff --git a/file b/file\n+value\n",
        )


class EmptySolver:
    name = "empty"

    def solve(
        self,
        task: TaskSpec,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        return PatchCandidate(task_id=task.instance_id, solver_name=self.name, patch="")


def _task() -> TaskSpec:
    return TaskSpec(
        instance_id="task-1",
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix it.",
        docker_image="image:tag",
    )


def test_solver_backend_protocol_accepts_patch_and_empty_solver(tmp_path: Path) -> None:
    workspace = PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app")
    budget = SolverBudget(max_attempts=1)

    patch_solver = FakeSolver()
    empty_solver = EmptySolver()

    assert isinstance(patch_solver, SolverBackend)
    assert isinstance(empty_solver, SolverBackend)
    assert not patch_solver.solve(_task(), workspace, tmp_path, budget).is_empty
    assert empty_solver.solve(_task(), workspace, tmp_path, budget).is_empty


def test_default_solver_specs_include_g11_base_team_and_team_pro() -> None:
    assert workflow_solver_spec("g1.1").workflow_name == "validation-council-solve"
    assert workflow_solver_spec("g1.1").max_attempts == 3
    assert workflow_solver_spec("baseTeam").workflow_name == "base-team"
    team_pro = workflow_solver_spec("TeamPro")
    assert team_pro.workflow_name == "team-pro"
    assert team_pro.max_attempts == 3
    assert team_pro.default_budget_tokens == 4_000_000
    assert team_pro.default_model_name == "opencollab-glm52-teampro-prolite"
    assert team_pro.config_overrides == {
        "model": "glm-5.2",
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
    }
    assert workflow_solver_spec("openhands").workflow_name == "openhands-external"


def test_prepared_workspace_converts_to_docker_workspace_environment() -> None:
    workspace = PreparedWorkspace(container_id="abc123", repo_root="/app", workdir="/app")

    env = docker_environment_for_workspace(workspace)

    assert env.workspace == "/app"
    assert getattr(env, "_container_id") == "abc123"
    assert getattr(env, "_exec_workdir") == "/app"


def test_base_team_workflow_registers_without_reexporting_other_workflows() -> None:
    specs = _load_specs_from_file("workflows/base_team.py")
    names = {spec.name for spec in specs}

    assert names == {"base-team"}
    spec = specs[0]
    assert spec.phases == ("analyze", "code", "verify")


def test_team_pro_workflow_alias_preserves_dynamic_workflow_phases() -> None:
    specs = _load_specs_from_file("workflows/analyst_solve.py")
    by_name = {spec.name: spec for spec in specs}

    assert {"analyst-solve", "team-pro"} <= set(by_name)
    assert by_name["team-pro"].phases == ("scope", "recon", "plan", "implement", "verify")


def test_workflow_backend_returns_patch_candidate(monkeypatch: Any, tmp_path: Path) -> None:
    calls: dict[str, Any] = {}

    class FakeRegistry:
        def get(self, name: str) -> str:
            calls["workflow_name"] = name
            return "workflow-spec"

    class FakeEnv:
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            calls["diff_cmd"] = cmd
            calls["diff_timeout"] = timeout
            return ExecResult(0, "diff --git a/a b/a\n+value\n", "")

    async def fake_run_workflow(
        workflow_spec: Any,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls["workflow_spec"] = workflow_spec
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"status": "done", "tokens_spent": 17}

    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.discover_workflows",
        lambda _: FakeRegistry(),
    )
    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.docker_environment_for_workspace",
        lambda _: FakeEnv(),
    )
    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.run_workflow",
        fake_run_workflow,
    )

    backend = WorkflowBackend(
        spec=workflow_solver_spec("baseTeam"),
        cfg={"model": "m", "provider": "p"},
        workflows_dir=tmp_path,
    )
    candidate = backend.solve(
        _task(),
        PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app"),
        tmp_path / "run",
        SolverBudget(max_tokens=100),
    )

    assert calls["workflow_name"] == "base-team"
    assert calls["workflow_spec"] == "workflow-spec"
    assert calls["args"]["description"] == "Fix it."
    assert calls["kwargs"]["workspace"] == "/app"
    assert calls["kwargs"]["budget"] == 100
    assert calls["diff_cmd"] == "git --no-pager diff --binary"
    assert candidate.solver_name == "baseTeam"
    assert candidate.token_count == 17
    assert not candidate.is_empty


def test_workflow_backend_applies_team_pro_config_overrides(monkeypatch: Any, tmp_path: Path) -> None:
    calls: dict[str, Any] = {}

    class FakeRegistry:
        def get(self, name: str) -> str:
            return "workflow-spec"

    class FakeEnv:
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return ExecResult(0, "", "")

    async def fake_run_workflow(workflow_spec: Any, args: dict[str, Any], **kwargs: Any):
        calls["cfg"] = kwargs["cfg"]
        calls["budget"] = kwargs["budget"]
        return {"status": "done", "tokens_spent": 0}

    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.discover_workflows", lambda _: FakeRegistry()
    )
    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.docker_environment_for_workspace",
        lambda _: FakeEnv(),
    )
    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.run_workflow", fake_run_workflow
    )

    backend = WorkflowBackend(
        spec=workflow_solver_spec("TeamPro"),
        cfg={
            "model": "file-model",
            "provider": "anthropic",
            "temperature": 0.2,
            "top_p": None,
            "max_output_tokens": 8192,
        },
        workflows_dir=tmp_path,
    )
    backend.solve(
        _task(),
        PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app"),
        tmp_path / "run",
        SolverBudget(),
    )

    assert calls["cfg"]["model"] == "glm-5.2"
    assert calls["cfg"]["temperature"] == 1.0
    assert calls["cfg"]["top_p"] == 1.0
    assert calls["cfg"]["max_output_tokens"] == 32_768
    assert calls["budget"] == 4_000_000


def test_workflow_backend_uses_integer_default_budget(monkeypatch: Any, tmp_path: Path) -> None:
    calls: dict[str, Any] = {}

    class FakeRegistry:
        def get(self, name: str) -> str:
            return "workflow-spec"

    class FakeEnv:
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            return ExecResult(0, "", "")

    async def fake_run_workflow(
        workflow_spec: Any,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls["budget"] = kwargs["budget"]
        return {"status": "done", "tokens_spent": 0}

    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.discover_workflows",
        lambda _: FakeRegistry(),
    )
    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.docker_environment_for_workspace",
        lambda _: FakeEnv(),
    )
    monkeypatch.setattr(
        "opencollab.harness.workflow_backend.run_workflow",
        fake_run_workflow,
    )

    backend = WorkflowBackend(
        spec=workflow_solver_spec("baseTeam"),
        cfg={"model": "m", "provider": "p"},
        workflows_dir=tmp_path,
    )
    backend.solve(
        _task(),
        PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app"),
        tmp_path / "run",
        SolverBudget(),
    )

    assert calls["budget"] == 1_000_000
