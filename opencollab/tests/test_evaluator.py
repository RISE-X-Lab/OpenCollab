from __future__ import annotations

from evaluator_test_support import (
    Environment,
    EvalResult,
    EvalTask,
    FakeEnv,
    FakeLLMClient,
    LocalEnvironment,
    asyncio,
    container,
    evaluator,
    os,
    pytest,
    run,
    run_eval_batch,
    run_eval_task,
    subprocess,
    sys,
)


def test_run_eval_task_produces_patch(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(run_eval_task(
        EvalTask(task_id="t1", description="fix the bug"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert isinstance(result, EvalResult)
    assert result.task_id == "t1"
    assert result.patch_produced is True
    assert result.patch == env.diff
    assert result.error is None
    assert env.cleaned_up is True

def test_run_eval_task_empty_diff_not_produced(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    async def env_factory(task):
        return FakeEnv(diff="")

    result = run(run_eval_task(
        EvalTask(task_id="t2", description="noop"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert result.patch_produced is False
    assert result.patch == ""

@pytest.mark.parametrize("description", [None, 1, {}, []])
def test_invalid_task_description_is_rejected_before_side_effects(
    tmp_path,
    description,
):
    output_dir = tmp_path / "output"
    factory_called = False

    async def env_factory(task):
        nonlocal factory_called
        factory_called = True
        return FakeEnv()

    with pytest.raises(ValueError, match="description"):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-description", description=description),
                output_dir=str(output_dir),
                env_factory=env_factory,
            )
        )

    assert factory_called is False
    assert output_dir.exists() is False

@pytest.mark.parametrize("max_tokens", [True, 0, -1, 1.5, "2", float("nan")])
def test_invalid_task_max_tokens_is_rejected_before_side_effects(
    tmp_path,
    max_tokens,
):
    output_dir = tmp_path / "output"

    with pytest.raises(ValueError, match="max_tokens"):
        run(
            run_eval_task(
                EvalTask(
                    task_id="invalid-max-tokens",
                    description="fix",
                    max_tokens=max_tokens,
                ),
                output_dir=str(output_dir),
            )
        )

    assert output_dir.exists() is False

@pytest.mark.parametrize("max_steps", [True, 0, -1, 1.5, "2", float("nan")])
def test_invalid_max_steps_is_rejected_before_side_effects(tmp_path, max_steps):
    output_dir = tmp_path / "output"

    with pytest.raises(ValueError, match="max_steps"):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-max-steps", description="fix"),
                output_dir=str(output_dir),
                max_steps=max_steps,
            )
        )

    assert output_dir.exists() is False

def test_task_timeout_includes_environment_setup_before_workflow(tmp_path):
    workflow_ran = False
    setup_cancelled = False

    async def env_factory(task):
        nonlocal setup_cancelled
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            setup_cancelled = True
            raise
        return FakeEnv()

    async def workflow(ctx, args):
        nonlocal workflow_ran
        workflow_ran = True
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(task_id="setup-timeout", description="fix", timeout=0.01),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
        )
    )

    assert setup_cancelled is True
    assert workflow_ran is False
    assert result.error == "Task timed out after 0.01s"
    assert result.patch == ""
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False
    assert result.duration < 0.2

def test_asyncio_run_shutdown_finishes_third_cancel_late_environment_cleanup(
    tmp_path,
):
    marker = tmp_path / "late-environment-cleaned"
    output_dir = tmp_path / "output"
    package_root = os.path.dirname(os.path.dirname(__file__))
    script = r'''
import asyncio
import pathlib
import sys

from opencollab.adapters.env import Environment, ExecResult
from opencollab.harness.evaluator import EvalTask, run_eval_task


class LateEnvironment(Environment):
    async def exec_cmd(self, cmd, timeout=120.0):
        return ExecResult(0, "", "")

    async def cleanup(self):
        await asyncio.sleep(0.003)
        pathlib.Path(sys.argv[1]).write_text("cleaned", encoding="utf-8")


environment = LateEnvironment()
cancellations = 0


async def cancellation_insensitive_factory(_task):
    global cancellations
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancellations += 1
            if cancellations >= 3:
                return environment


async def main():
    result = await run_eval_task(
        EvalTask(task_id="loop-close-late-env", description="x", timeout=0.03),
        output_dir=sys.argv[2],
        tools_factory=list,
        env_factory=cancellation_insensitive_factory,
        cancellation_cleanup_timeout=0.01,
    )
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert cancellations == 3


asyncio.run(main(), debug=True)
assert cancellations == 3
assert pathlib.Path(sys.argv[1]).read_text(encoding="utf-8") == "cleaned"
'''
    process_env = dict(os.environ)
    process_env["PYTHONPATH"] = package_root
    completed = subprocess.run(
        [sys.executable, "-c", script, str(marker), str(output_dir)],
        capture_output=True,
        text=True,
        timeout=10,
        env=process_env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "cleaned"
    assert "Task was destroyed" not in completed.stderr
    assert "was never awaited" not in completed.stderr
    assert "pending" not in completed.stderr.lower()

@pytest.mark.parametrize("concurrency", [0, -1, 1.5, True, "2", float("nan")])
def test_run_eval_batch_rejects_invalid_concurrency(concurrency):
    with pytest.raises(ValueError, match="concurrency must be a positive integer"):
        run(run_eval_batch([], concurrency=concurrency))

def test_run_eval_batch_marks_unhandled_result_integrity_unknown(monkeypatch):
    async def fail_run_eval_task(task, **kwargs):
        raise RuntimeError("unexpected evaluator failure")

    monkeypatch.setattr(evaluator, "run_eval_task", fail_run_eval_task)

    result = run(
        run_eval_batch([EvalTask(task_id="broken", description="fix")])
    )[0]

    assert result.patch == ""
    assert result.patch_produced is False
    assert result.execution_quiesced is False
    assert result.patch_extraction_succeeded is False
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False

@pytest.mark.parametrize(
    "task_id",
    [
        "",
        ".",
        "..",
        "../escaped",
        "/tmp/escaped",
        "nested/task",
        "nested\\task",
        "C:\\escaped",
        "control\x1f",
        "x" * 241,
        "lone-surrogate-\ud800",
        "low-surrogate-\udcff",
    ],
)
def test_run_eval_task_rejects_unsafe_task_id_before_side_effects(
    task_id,
    tmp_path,
):
    output_dir = tmp_path / "output"
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    with pytest.raises(ValueError, match="path-safe"):
        run(
            run_eval_task(
                EvalTask(task_id=task_id, description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False

def test_run_eval_batch_rejects_duplicate_task_ids_before_start(monkeypatch):
    started = False

    async def fake_run_eval_task(task, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("duplicate batch must not start")

    monkeypatch.setattr(evaluator, "run_eval_task", fake_run_eval_task)
    tasks = [
        EvalTask(task_id="duplicate", description="first"),
        EvalTask(task_id="duplicate", description="second"),
    ]

    with pytest.raises(ValueError, match="must be unique"):
        run(run_eval_batch(tasks))

    assert started is False

@pytest.mark.parametrize(
    "task_ids",
    [
        ("Task", "task"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}", "cafe\N{COMBINING ACUTE ACCENT}"),
    ],
    ids=["case-fold", "unicode-normalization"],
)
def test_run_eval_batch_rejects_filesystem_equivalent_task_ids(task_ids):
    tasks = [EvalTask(task_id=task_id, description="fix") for task_id in task_ids]

    with pytest.raises(ValueError, match="must be unique"):
        run(run_eval_batch(tasks))

@pytest.mark.parametrize(
    "paths, message",
    [
        (
            tuple(
                f"artifact-{index}"
                for index in range(
                    evaluator.MAX_TASK_HARNESS_ARTIFACT_PATHS + 1
                )
            ),
            "path-count",
        ),
        (
            ("x" * (evaluator.MAX_TASK_HARNESS_ARTIFACT_PATH_BYTES + 1),),
            "aggregate-byte",
        ),
        (("bad\0path",), "filesystem-safe"),
        (("bad\udcffpath",), "filesystem-safe"),
    ],
    ids=["count", "bytes", "nul", "surrogate"],
)
def test_harness_artifact_inputs_are_bounded_before_side_effects(
    paths,
    message,
    tmp_path,
):
    output_dir = tmp_path / "output"
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    with pytest.raises(ValueError, match=message):
        run(
            run_eval_task(
                EvalTask(
                    task_id="bounded-artifacts",
                    description="fix",
                    harness_artifact_paths=paths,
                ),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False

@pytest.mark.parametrize("concurrency", [1, 2])
def test_default_batch_isolates_tasks_sharing_one_local_repo(
    concurrency,
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "base.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    ready = 0
    both_ready = asyncio.Event()

    async def workflow(ctx, args):
        nonlocal ready
        task_id = args["task_id"]
        await ctx.env.write_file(f"{task_id}.txt", f"{task_id}\n")
        if concurrency == 2:
            ready += 1
            if ready == 2:
                both_ready.set()
            await both_ready.wait()
        return {"status": "done"}

    tasks = [
        EvalTask(task_id="one", description="first", repo_path=str(repo)),
        EvalTask(task_id="two", description="second", repo_path=str(repo)),
    ]
    results = run(
        run_eval_batch(
            tasks,
            concurrency=concurrency,
            output_dir=str(tmp_path / "output"),
            tools_factory=list,
            workflow=workflow,
        )
    )
    by_id = {result.task_id: result for result in results}

    assert by_id["one"].submission_eligible is True
    assert "one.txt" in by_id["one"].patch
    assert "two.txt" not in by_id["one"].patch
    assert by_id["two"].submission_eligible is True
    assert "two.txt" in by_id["two"].patch
    assert "one.txt" not in by_id["two"].patch
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert status == ""

def test_default_docker_task_without_repo_path_does_not_create_host_backing(
    monkeypatch,
):
    observed: dict[str, object] = {}

    class FakeDockerEnvironment(Environment):
        def __init__(self, *, image, backing_environment=None):
            observed["image"] = image
            observed["backing"] = backing_environment

        async def setup(self, mount_dir=None):
            observed["mount_dir"] = mount_dir
            return "cid"

        async def cleanup(self) -> None:
            observed["cleaned"] = True

    class ForbiddenWorktree:
        def __init__(self, *args, **kwargs):
            raise AssertionError("repo-less Docker task must use its image workspace")

    monkeypatch.setattr(evaluator, "DockerEnvironment", FakeDockerEnvironment)
    monkeypatch.setattr(evaluator, "WorktreeEnvironment", ForbiddenWorktree)

    env = run(
        evaluator.default_env_factory(
            EvalTask(
                task_id="image-owned-repo",
                description="fix",
                docker_image="benchmark:latest",
            )
        )
    )

    assert isinstance(env, FakeDockerEnvironment)
    assert observed == {
        "image": "benchmark:latest",
        "backing": None,
        "mount_dir": None,
    }

def test_default_worktree_maps_source_repo_artifact_into_isolated_workspace(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    tasks_path = repo / "tasks.jsonl"
    tasks_path.write_text('{"task_id": "mapped"}\n', encoding="utf-8")

    async def workflow(ctx, args):
        await ctx.env.write_file("tasks.jsonl", '{"agent": "rewrote"}\n')
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(
                task_id="source-artifact-map",
                description="fix",
                repo_path=str(repo),
                harness_artifact_paths=(str(tasks_path),),
            ),
            output_dir=str(tmp_path / "output"),
            tools_factory=list,
            workflow=workflow,
        )
    )

    assert result.patch == ""
    assert result.patch_extraction_succeeded is True
    assert result.harness_artifact_exclusion_proven is True
    assert result.submission_eligible is True
    assert tasks_path.read_text(encoding="utf-8") == '{"task_id": "mapped"}\n'


def test_default_worktree_rejects_model_file_in_reserved_retirement_namespace(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )

    async def workflow(ctx, args):
        created = await ctx.env.exec_cmd(
            "printf %s hidden-model-change > .opencollab-retired-model-hidden.py"
        )
        assert created.returncode == 0
        return {"status": "done"}

    result = run(
        run_eval_task(
            EvalTask(
                task_id="reserved-retirement-prefix",
                description="fix",
                repo_path=str(repo),
            ),
            output_dir=str(tmp_path / "output"),
            tools_factory=list,
            workflow=workflow,
        )
    )

    assert result.patch == ""
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False
    assert "unregistered or modified .opencollab-retired-*" in (result.error or "")

def test_non_local_environment_never_maps_host_artifact_paths_into_container():
    class NonLocalEnv(Environment):
        workspace = "/testbed"
        local_filesystem = False

    assert evaluator._workspace_relative_artifact_paths(
        NonLocalEnv(),
        ["/testbed/eval_results", "/testbed/results.jsonl"],
    ) == []

def test_bind_mapped_environment_maps_host_artifacts_into_container_paths(tmp_path):
    class BindMappedEnv(Environment):
        workspace = "/workspace"
        local_filesystem = False

        def __init__(self, host_workspace):
            self.host_workspace = str(host_workspace)

    repo = tmp_path / "repo"
    artifacts = repo / "eval_results" / "trajectories"
    artifacts.mkdir(parents=True)

    assert evaluator._workspace_relative_artifact_paths(
        BindMappedEnv(repo),
        [artifacts, repo / "eval_results" / "results.jsonl"],
    ) == [
        "eval_results/trajectories",
        "eval_results/results.jsonl",
    ]

def test_host_artifact_mapping_follows_external_alias_back_into_workspace(tmp_path):
    repo = tmp_path / "repo"
    output = repo / "eval_results"
    output.mkdir(parents=True)
    alias = tmp_path / "output-alias"
    alias.symlink_to(output, target_is_directory=True)
    env = LocalEnvironment(str(repo))

    assert evaluator._workspace_relative_artifact_paths(
        env,
        [alias / "trajectories"],
    ) == ["eval_results/trajectories"]
