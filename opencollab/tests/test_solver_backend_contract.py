from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
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
    SolverContractError,
    SolverTaskView,
    solve_with_public_task,
    solver_task_view,
    workflow_solver_spec,
)
from opencollab.harness.workflow_backend import WorkflowBackend

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeSolver:
    name = "fake"

    def __init__(self) -> None:
        self.seen_task_id = ""

    def solve(
        self,
        task: SolverTaskView,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        self.seen_task_id = task.task_id
        assert workspace.repo_root == "/app"
        assert budget.max_attempts == 1
        return PatchCandidate(
            task_id=task.task_id,
            solver_name=self.name,
            patch="diff --git a/file b/file\n+value\n",
        )


class EmptySolver:
    name = "empty"

    def solve(
        self,
        task: SolverTaskView,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        return PatchCandidate(task_id=task.task_id, solver_name=self.name, patch="")


class CandidateOverrideSolver:
    name = "candidate-override"

    def __init__(self, **overrides: Any) -> None:
        self._overrides = overrides

    def solve(
        self,
        task: SolverTaskView,
        workspace: PreparedWorkspace,
        run_dir: Path,
        budget: SolverBudget,
    ) -> PatchCandidate:
        values: dict[str, Any] = {
            "task_id": task.task_id,
            "solver_name": self.name,
            "patch": "",
        }
        values.update(self._overrides)
        return PatchCandidate(**values)


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
    patch = solve_with_public_task(patch_solver, _task(), workspace, tmp_path, budget)
    empty = solve_with_public_task(empty_solver, _task(), workspace, tmp_path, budget)

    assert patch_solver.seen_task_id.startswith("solver-")
    assert patch_solver.seen_task_id != "task-1"
    assert patch.task_id == "task-1"
    assert empty.task_id == "task-1"
    assert not patch.is_empty
    assert empty.is_empty


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"task_id": "solver-another-task"}, "task_id"),
        ({"solver_name": "another-solver"}, "solver_name"),
        ({"patch": None}, "patch"),
        ({"metadata": []}, "metadata"),
        ({"token_count": -1}, "token_count"),
        ({"cost_usd": float("nan")}, "cost_usd"),
    ],
)
def test_solver_bridge_rejects_unattributable_candidates(
    overrides: dict[str, Any],
    error: str,
    tmp_path: Path,
) -> None:
    backend = CandidateOverrideSolver(**overrides)
    workspace = PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app")

    with pytest.raises(SolverContractError, match=error):
        solve_with_public_task(
            backend,
            _task(),
            workspace,
            tmp_path,
            SolverBudget(),
        )


def test_default_solver_specs_include_g11_base_team_and_team_pro() -> None:
    assert workflow_solver_spec("g1.1").workflow_name == "validation-council-solve"
    assert workflow_solver_spec("g1.1").max_attempts == 3
    assert workflow_solver_spec("baseTeam").workflow_name == "base-team"
    team_pro = workflow_solver_spec("TeamPro")
    assert team_pro.workflow_name == "team-pro"
    assert team_pro.max_attempts == 3
    assert team_pro.default_budget_tokens == 4_000_000
    assert team_pro.required_runtime_options == (
        ("--model-name", "OPENCOLLAB_SWE_MODEL_NAME"),
        ("--llm-model", "OPENCOLLAB_SWE_LLM_MODEL"),
    )
    assert team_pro.config_overrides == {
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
    }
    assert workflow_solver_spec("openhands").workflow_name == "openhands-external"


def test_solver_task_view_excludes_answers_and_hidden_tests() -> None:
    task = TaskSpec(
        instance_id="owner__repo-target-commit",
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix the public bug report.",
        base_commit="target-commit",
        fail_to_pass=("tests/test_secret.py::test_fix",),
        pass_to_pass=("tests/test_regression.py",),
        selected_test_files=("tests/test_secret.py",),
        test_patch="secret test patch",
        reference_patch="gold answer",
        metadata={
            "FAIL_TO_PASS": ["tests/test_secret.py::test_fix"],
            "test_patch": "secret test patch",
            "patch": "gold answer",
            "solver_public_hints": ["The public API should remain compatible."],
            "solver_public_metadata": {"language": "Python"},
        },
    )

    view = solver_task_view(task)

    assert view.task_id.startswith("solver-")
    assert task.instance_id not in view.task_id
    assert view.repo == "owner/repo"
    assert view.problem_statement == "Fix the public bug report."
    assert view.hints == ("The public API should remain compatible.",)
    assert dict(view.metadata) == {"language": "Python"}
    serialized = repr(view)
    for hidden in (
        "gold answer",
        "secret test patch",
        "tests/test_secret.py",
        "target-commit",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    ):
        assert hidden not in serialized


def test_solver_task_view_rejects_hidden_fields_marked_as_public() -> None:
    task = TaskSpec(
        instance_id="task-1",
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix it.",
        metadata={"solver_public_metadata": {"reference_patch": "answer"}},
    )

    with pytest.raises(ValueError, match="hidden field"):
        solver_task_view(task)


def test_solver_task_view_rejects_non_json_public_metadata() -> None:
    task = TaskSpec(
        instance_id="task-1",
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix it.",
        metadata={"solver_public_metadata": {"unsafe": object()}},
    )

    with pytest.raises(ValueError, match="JSON-like"):
        solver_task_view(task)


def test_solver_task_view_ignores_public_id_override() -> None:
    task = TaskSpec(
        instance_id="owner__repo-target-commit",
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix it.",
        metadata={"solver_public_task_id": "owner__repo-target-commit"},
    )

    view = solver_task_view(task)

    assert re.fullmatch(r"solver-[0-9a-f]{32}", view.task_id)
    assert view.task_id != task.instance_id


@pytest.mark.parametrize(
    "metadata",
    [
        {"solver_public_hints": ["Apply secret reference patch here."]},
        {"solver_public_metadata": {"nested": {"value": "secret test patch"}}},
        {
            "innocent_name": "private evaluation oracle",
            "solver_public_metadata": {"description": "private evaluation oracle"},
        },
    ],
)
def test_solver_task_view_rejects_hidden_values_in_public_containers(
    metadata: dict[str, Any],
) -> None:
    task = TaskSpec(
        instance_id="task-1",
        dataset="swe-batch-pro-lite",
        repo="owner/repo",
        problem_statement="Fix it.",
        test_patch="secret test patch",
        reference_patch="secret reference patch",
        metadata=metadata,
    )

    with pytest.raises(ValueError, match="hidden task information"):
        solver_task_view(task)


def test_solver_budget_has_no_solver_visible_metadata_channel() -> None:
    with pytest.raises(TypeError, match="metadata"):
        SolverBudget(metadata={"reference_patch": "answer"})  # type: ignore[call-arg]


def test_prepared_workspace_converts_to_docker_workspace_environment() -> None:
    container_id = "a" * 64
    workspace = PreparedWorkspace(container_id=container_id, repo_root="/app", workdir="/app")

    env = docker_environment_for_workspace(workspace)

    assert env.workspace == "/app"
    assert getattr(env, "_container_id") == container_id
    assert getattr(env, "_exec_workdir") == "/app"


def test_base_team_workflow_registers_without_reexporting_other_workflows() -> None:
    specs = _load_specs_from_file(str(REPO_ROOT / "workflows" / "base_team.py"))
    names = {spec.name for spec in specs}

    assert names == {"base-team"}
    spec = specs[0]
    assert spec.phases == ("analyze", "code", "verify")


def test_team_pro_workflow_alias_preserves_dynamic_workflow_phases() -> None:
    specs = _load_specs_from_file(
        str(REPO_ROOT / "workflows" / "analyst_solve.py")
    )
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
    candidate = solve_with_public_task(
        backend,
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
    assert calls["args"]["instance_id"].startswith("solver-")
    assert calls["args"]["instance_id"] != "task-1"
    assert candidate.task_id == "task-1"
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
    solve_with_public_task(
        backend,
        _task(),
        PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app"),
        tmp_path / "run",
        SolverBudget(),
    )

    assert calls["cfg"]["model"] == "file-model"
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
    solve_with_public_task(
        backend,
        _task(),
        PreparedWorkspace(container_id="cid", repo_root="/app", workdir="/app"),
        tmp_path / "run",
        SolverBudget(),
    )

    assert calls["budget"] == 1_000_000
