import asyncio
import gc
import json
import os
import subprocess
import sys

import pytest

from opencollab.adapters.cli import eval as eval_cli
from opencollab.adapters.env import Environment, ExecResult, LocalEnvironment
from opencollab.adapters.llm import LLMResponse, Usage
from opencollab.bootstrap import container
from opencollab.harness import evaluator
from opencollab.harness.evaluator import (
    EvalResult,
    EvalTask,
    run_eval_batch,
    run_eval_task,
    save_results,
)
from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
    metric_submission_integrity,
)


def run(coro):
    return asyncio.run(coro)


def is_worktree_diff_cmd(cmd: str) -> bool:
    return "git diff --cached --binary HEAD" in cmd


class FakeLLMClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(messages)
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


class FakeEnv(Environment):
    def __init__(self, diff="diff --git a/x b/x\n+new\n"):
        self.diff = diff
        self.cleaned_up = False
        self.cmds = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            return ExecResult(returncode=0, stdout=self.diff, stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


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


@pytest.mark.parametrize(
    "extras",
    [
        ["not", "a", "dict"],
        {"test_patch": 1},
    ],
    ids=["non-dict", "non-string-test-patch"],
)
def test_run_eval_task_rejects_invalid_extras_before_side_effects(
    extras,
    tmp_path,
):
    output_dir = tmp_path / "output"
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    with pytest.raises(ValueError, match="task extras"):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-extras", description="fix", extras=extras),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


def test_tracer_close_failure_still_cleans_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    real_tracer = evaluator.Tracer

    class FailingCloseTracer:
        def __init__(self, *args, **kwargs):
            self._inner = real_tracer(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            raise OSError("trace disk failure")

    monkeypatch.setattr(evaluator, "Tracer", FailingCloseTracer)
    env = FakeEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="trace-close", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env.cleaned_up is True
    assert result.patch_produced is True
    assert "tracer close failed: OSError: trace disk failure" in result.error


def test_tracer_destructor_never_emits_an_unraisable_close_failure(monkeypatch):
    unraisable = []

    class DestructorCloseFailure(evaluator.Tracer):
        def __init__(self):
            return None

        def close(self):
            raise KeyboardInterrupt("late close failed")

    tracer = DestructorCloseFailure()
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)

    del tracer
    gc.collect()

    assert unraisable == []


def test_patch_extraction_exception_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class ExtractionFailureEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                raise OSError("temporary index unavailable")
            return await super().exec_cmd(cmd, timeout)

    env = ExtractionFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="extract-exception", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env.cleaned_up is True
    assert result.patch_produced is False
    assert result.error == (
        "patch extraction failed: OSError: temporary index unavailable"
    )
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False


def test_patch_extraction_nonzero_exit_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class ExtractionFailureEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(returncode=128, stdout="", stderr="index locked")
            return await super().exec_cmd(cmd, timeout)

    env = ExtractionFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="extract-nonzero", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env.cleaned_up is True
    assert result.patch_produced is False
    assert result.error == (
        "patch extraction failed: RuntimeError: "
        "diff command exited 128: index locked"
    )
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False


def test_patch_extraction_rejects_truncated_diff(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class TruncatedDiffEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/x b/x\n+partial\n",
                    stderr="",
                    stdout_truncated=True,
                    stdout_dropped_bytes=8192,
                )
            return await super().exec_cmd(cmd, timeout)

    env = TruncatedDiffEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="extract-truncated", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch == ""
    assert result.patch_produced is False
    assert "patch extraction failed" in result.error
    assert "stdout dropped 8192 bytes" in result.error
    assert result.patch_extraction_succeeded is False
    assert result.submission_eligible is False


def test_environment_cleanup_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupFailureEnv(FakeEnv):
        async def cleanup(self) -> None:
            raise OSError("container removal failed")

    env = CleanupFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="cleanup-failure", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert result.patch_produced is False
    assert result.patch == ""
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert result.error == (
        "environment cleanup failed: OSError: container removal failed"
    )
    assert env._aborted is True


def test_environment_cleanup_exception_invokes_abort_hook(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupFailureEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.abort_calls = 0

        async def cleanup(self) -> None:
            raise OSError("container removal failed")

        async def abort(self) -> None:
            self.abort_calls += 1

    env = CleanupFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="cleanup-failure-abort", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env._aborted is True
    assert env.abort_calls == 1
    assert "environment cleanup failed: OSError: container removal failed" in result.error
    assert "environment abort" not in result.error


def test_environment_cleanup_and_abort_exceptions_are_both_reported(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupAndAbortFailureEnv(FakeEnv):
        async def cleanup(self) -> None:
            raise OSError("cleanup exploded")

        async def abort(self) -> None:
            raise RuntimeError("abort exploded")

    env = CleanupAndAbortFailureEnv()

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="cleanup-and-abort-failure", description="fix"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert env._aborted is True
    assert "environment cleanup failed: OSError: cleanup exploded" in result.error
    assert "environment abort failed: RuntimeError: abort exploded" in result.error


def test_environment_cleanup_exception_and_stubborn_abort_are_bounded(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class CleanupFailureBlockingAbortEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.release_abort = asyncio.Event()
            self.abort_finished = asyncio.Event()

        async def cleanup(self) -> None:
            raise OSError("cleanup exploded")

        async def abort(self) -> None:
            try:
                while not self.release_abort.is_set():
                    try:
                        await self.release_abort.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                self.abort_finished.set()

    env = CleanupFailureBlockingAbortEnv()

    async def env_factory(task):
        return env

    async def scenario():
        result = await asyncio.wait_for(
            run_eval_task(
                EvalTask(task_id="cleanup-failure-blocking-abort", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )
        env.release_abort.set()
        await asyncio.wait_for(env.abort_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert env._aborted is True
    assert "environment cleanup failed: OSError: cleanup exploded" in result.error
    assert "environment abort timed out" in result.error


def test_cancelled_environment_cleanup_is_reported_as_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    class BlockingCleanupEnv(FakeEnv):
        async def cleanup(self) -> None:
            await asyncio.Event().wait()

    env = BlockingCleanupEnv()

    async def env_factory(task):
        return env

    result = run(
        asyncio.wait_for(
            run_eval_task(
                EvalTask(task_id="blocked-cleanup", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )
    )

    assert result.patch_produced is False
    assert result.patch == ""
    assert result.execution_quiesced is False
    assert result.submission_eligible is False
    assert env._aborted is True
    assert "environment cleanup timed out" in result.error


def test_caller_cancellation_cleans_environment_before_propagating(
    monkeypatch, tmp_path
):
    started = asyncio.Event()

    async def wait_forever(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(evaluator, "_run_single_session", wait_forever)
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="cancelled", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())

    assert env.cleaned_up is True
    assert any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)


def test_single_session_timeout_waits_for_cleanup_and_keeps_metrics(
    monkeypatch, tmp_path
):
    cancel_seen = asyncio.Event()
    release_cancel = asyncio.Event()
    env = FakeEnv(diff="diff --git a/x b/x\n+early\n")

    class DelayedCancelSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0

        async def add_user_message(self, content):
            pass

        async def run_loop(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_seen.set()
                await release_cancel.wait()
                self.used_tokens = 17
                self.step_count = 3
                env.diff = "diff --git a/x b/x\n+late-cleanup-write\n"
                raise

    session = DelayedCancelSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="slow-single", description="fix", timeout=0.5),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
            )
        )
        try:
            await asyncio.wait_for(cancel_seen.wait(), timeout=2.0)
        except TimeoutError:
            await asyncio.wait({eval_task}, timeout=0.1)
            if eval_task.done():
                result = await eval_task
                raise AssertionError(
                    "evaluation finished without entering delayed cancellation: "
                    f"{result!r}"
                )
            frames = [
                f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}"
                for frame in eval_task.get_stack()
            ]
            raise AssertionError(
                "evaluation task did not reach cancellation: "
                f"task={eval_task!r}, frames={frames}, "
                f"awaiting={eval_task.get_coro().cr_await!r}"
            )
        await asyncio.sleep(0)
        assert eval_task.done() is False
        assert not any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
        release_cancel.set()
        return await eval_task

    result = run(scenario())

    assert result.error == "Task timed out after 0.5s"
    assert result.tokens_used == 17
    assert result.steps == 3
    assert "late-cleanup-write" in result.patch
    assert result.submission_eligible is True


def test_non_quiescent_timeout_is_bounded_and_revokes_environment(
    monkeypatch, tmp_path
):
    release_cleanup = asyncio.Event()
    cleanup_started = asyncio.Event()
    late_write_blocked = asyncio.Event()
    finished = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        writes: list[tuple[str, str]] = []

        async def write_file(self, path: str, content: str) -> None:
            self._ensure_active()
            self.writes.append((path, content))

    env = AbortTrackingEnv(diff="diff --git a/x b/x\n+untrusted\n")

    class NeverQuiescentSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0

        async def add_user_message(self, content):
            pass

        async def run_loop(self):
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    continue
            try:
                await env.write_file("late.py", "late")
            except RuntimeError:
                late_write_blocked.set()
            finally:
                finished.set()
            return "late"

    session = NeverQuiescentSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="never-clean", description="fix", timeout=0.01),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await cleanup_started.wait()
        result = await asyncio.wait_for(eval_task, timeout=0.5)
        release_cleanup.set()
        await asyncio.wait_for(finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert env._aborted is True
    assert env.cleaned_up is True
    assert late_write_blocked.is_set() is True
    assert env.writes == []
    assert result.patch == ""
    assert result.patch_produced is False
    assert "execution cleanup timed out" in result.error
    assert "patch extraction skipped" in result.error
    assert not any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
    assert result.execution_quiesced is False
    assert result.submission_eligible is False


def test_repeated_caller_cancel_cannot_interrupt_evaluator_teardown(
    monkeypatch, tmp_path
):
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_session = asyncio.Event()
    finished = asyncio.Event()
    late_write_blocked = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.writes = []

        async def write_file(self, path: str, content: str) -> None:
            self._ensure_active()
            self.writes.append((path, content))

    env = AbortTrackingEnv()

    class StubbornSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0

        async def add_user_message(self, content):
            pass

        async def run_loop(self):
            started.set()
            while not release_session.is_set():
                try:
                    await release_session.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            try:
                await env.write_file("late.py", "late")
            except RuntimeError:
                late_write_blocked.set()
            finally:
                finished.set()

    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: StubbornSession())

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="double-cancel", description="fix", timeout=60),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        eval_task.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.5)
        eval_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        release_session.set()
        await asyncio.wait_for(finished.wait(), timeout=0.5)

    run(scenario())

    assert env._aborted is True
    assert env.cleaned_up is True
    assert late_write_blocked.is_set() is True
    assert env.writes == []


def test_caller_cancel_owns_stubborn_initial_user_message(monkeypatch, tmp_path):
    add_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_add = asyncio.Event()
    add_finished = asyncio.Event()
    late_write_blocked = asyncio.Event()

    class AbortTrackingEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.writes = []

        async def write_file(self, path: str, content: str) -> None:
            self._ensure_active()
            self.writes.append((path, content))

    env = AbortTrackingEnv()

    class StubbornAddSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0
        run_loop_called = False

        async def add_user_message(self, content):
            add_started.set()
            while not release_add.is_set():
                try:
                    await release_add.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            try:
                await env.write_file("late-message.py", content)
            except RuntimeError:
                late_write_blocked.set()
            finally:
                add_finished.set()

        async def run_loop(self):
            self.run_loop_called = True

    session = StubbornAddSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stubborn-initial-add", description="fix", timeout=60),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(add_started.wait(), timeout=0.5)
        eval_task.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), timeout=0.5)
        eval_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        release_add.set()
        await asyncio.wait_for(add_finished.wait(), timeout=0.5)

    run(scenario())

    assert env._aborted is True
    assert env.cleaned_up is True
    assert env.writes == []
    assert late_write_blocked.is_set() is True
    assert session.run_loop_called is False


def test_task_deadline_bounds_stubborn_initial_user_message(monkeypatch, tmp_path):
    add_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_add = asyncio.Event()
    add_finished = asyncio.Event()

    class StubbornAddSession:
        used_tokens = 0
        step_count = 0
        markup_recovered = 0
        run_loop_called = False

        async def add_user_message(self, content):
            add_started.set()
            while not release_add.is_set():
                try:
                    await release_add.wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
            add_finished.set()

        async def run_loop(self):
            self.run_loop_called = True

    session = StubbornAddSession()
    monkeypatch.setattr(evaluator, "build_session", lambda **kwargs: session)
    env = FakeEnv()

    async def env_factory(task):
        return env

    async def scenario():
        result = await asyncio.wait_for(
            run_eval_task(
                EvalTask(
                    task_id="stubborn-add-deadline",
                    description="fix",
                    timeout=0.01,
                ),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            ),
            timeout=0.5,
        )
        release_add.set()
        await asyncio.wait_for(add_finished.wait(), timeout=0.5)
        return result

    result = run(scenario())

    assert add_started.is_set() is True
    assert cancellation_seen.is_set() is True
    assert session.run_loop_called is False
    assert env._aborted is True
    assert env.cleaned_up is True
    assert result.patch == ""
    assert result.error and result.error.startswith("Task timed out after 0.01s")


def test_caller_cancel_bounds_stubborn_environment_cleanup(monkeypatch, tmp_path):
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class StubbornCleanupEnv(FakeEnv):
        async def cleanup(self) -> None:
            cleanup_started.set()
            while not release_cleanup.is_set():
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_cancelled.set()
            self.cleaned_up = True
            cleanup_finished.set()

    env = StubbornCleanupEnv()
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)

    async def env_factory(task):
        return env

    async def scenario():
        eval_task = asyncio.create_task(
            run_eval_task(
                EvalTask(task_id="stubborn-env-cleanup", description="fix"),
                output_dir=str(tmp_path),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=0.01,
            )
        )
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        eval_task.cancel()
        eval_task.cancel()
        await asyncio.wait_for(cleanup_cancelled.wait(), timeout=0.5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(eval_task, timeout=0.5)
        assert env._aborted is True
        release_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.5)

    run(scenario())


@pytest.mark.parametrize(
    "cleanup_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_cleanup_timeout_without_side_effects(
    tmp_path, cleanup_timeout
):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="cancellation_cleanup_timeout must be a finite positive number",
    ):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-cleanup-timeout", description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
                cancellation_cleanup_timeout=cleanup_timeout,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


@pytest.mark.parametrize(
    "task_timeout",
    [float("nan"), float("inf"), float("-inf"), 0, -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_task_timeout_without_side_effects(
    tmp_path, task_timeout
):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="task timeout must be a finite positive number",
    ):
        run(
            run_eval_task(
                EvalTask(
                    task_id="invalid-task-timeout",
                    description="fix",
                    timeout=task_timeout,
                ),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


@pytest.mark.parametrize(
    "checkpoint_interval",
    [float("nan"), float("inf"), float("-inf"), -1, True, "invalid"],
)
def test_run_eval_task_rejects_invalid_checkpoint_interval_without_side_effects(
    tmp_path, checkpoint_interval
):
    env_factory_called = False

    async def env_factory(task):
        nonlocal env_factory_called
        env_factory_called = True
        return FakeEnv()

    output_dir = tmp_path / "results"
    with pytest.raises(
        ValueError,
        match="checkpoint_interval_seconds must be finite and non-negative",
    ):
        run(
            run_eval_task(
                EvalTask(task_id="invalid-checkpoint-interval", description="fix"),
                output_dir=str(output_dir),
                tools_factory=list,
                env_factory=env_factory,
                checkpoint_interval_seconds=checkpoint_interval,
            )
        )

    assert env_factory_called is False
    assert output_dir.exists() is False


def test_run_eval_task_staged_extraction_includes_new_files(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    env = FakeEnv(
        diff=(
            "diff --git a/new_module.py b/new_module.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new_module.py\n"
            "@@ -0,0 +1 @@\n"
            "+value = 1\n"
        )
    )

    async def env_factory(task):
        return env

    result = run(
        run_eval_task(
            EvalTask(task_id="new-file", description="add file"),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
        )
    )

    assert any(is_worktree_diff_cmd(cmd) for cmd in env.cmds)
    assert "new file mode" in result.patch
    assert result.patch_produced is True


def test_run_eval_task_honors_injected_params(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "LLMClient", FakeLLMClient)
    captured = {}
    sentinel_tool = object()

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["prompt"] = agent.system_prompt
        captured["tools"] = list(agent.tools)
        captured["max_steps"] = max_steps
        captured["temperature"] = agent.temperature
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="t3", description="task"),
        output_dir=str(tmp_path),
        prompt="CUSTOM PROMPT",
        tools_factory=lambda: [sentinel_tool],
        env_factory=env_factory,
        max_steps=7,
        temperature=0.55,
    ))

    assert captured["prompt"] == "CUSTOM PROMPT"
    assert captured["tools"] == [sentinel_tool]
    assert captured["max_steps"] == 7
    assert captured["temperature"] == 0.55


class CapturingLLMClient:
    """Fake LLM client that records every kwarg passed to ``complete``.

    Accepts ``**kwargs`` so a forwarded ``top_p`` (or ``thinking`` etc.) does
    not raise — lets a test assert the sampling knob reaches the provider call.
    """

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        CapturingLLMClient.last_kwargs = {"temperature": temperature, **kwargs}
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage=Usage(input_tokens=3, output_tokens=2),
            finish_reason="stop",
        )


def test_run_eval_task_forwards_top_p_to_agent_and_provider(monkeypatch, tmp_path):
    # The eval path must put top_p on the Agent AND carry it into the provider
    # ``complete`` call, mirroring temperature. This is the latent eval-gap fix:
    # a configured top_p (like OPENCOLLAB_TOP_P) actually takes effect.
    CapturingLLMClient.last_kwargs = {}
    monkeypatch.setattr(container, "LLMClient", CapturingLLMClient)
    captured = {}

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["temperature"] = agent.temperature
        captured["top_p"] = agent.top_p
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="tp", description="task"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
        temperature=0.3,
        top_p=0.85,
    ))

    assert captured["temperature"] == 0.3
    assert captured["top_p"] == 0.85
    # And it actually reached the provider call (not just stored on the Agent).
    assert CapturingLLMClient.last_kwargs.get("top_p") == 0.85


def test_run_eval_task_top_p_unset_omits_it_from_provider_call(monkeypatch, tmp_path):
    # Default top_p (None) leaves the Agent default None and is NOT forwarded to
    # the provider call — so the request is byte-identical to today's behavior.
    CapturingLLMClient.last_kwargs = {}
    monkeypatch.setattr(container, "LLMClient", CapturingLLMClient)
    captured = {}

    real_build_session = evaluator.build_session

    def spy_build_session(*, agent, max_steps, **kwargs):
        captured["top_p"] = agent.top_p
        return real_build_session(agent=agent, max_steps=max_steps, **kwargs)

    monkeypatch.setattr(evaluator, "build_session", spy_build_session)

    async def env_factory(task):
        return FakeEnv()

    run(run_eval_task(
        EvalTask(task_id="tp0", description="task"),
        output_dir=str(tmp_path),
        tools_factory=list,
        env_factory=env_factory,
    ))

    assert captured["top_p"] is None
    assert "top_p" not in CapturingLLMClient.last_kwargs


def test_default_tools_match_curated_team_surface():
    # The headless eval agent must exercise the same curated toolset as team
    # roles — in particular run_tests/git_diff/apply_patch, which the bash
    # description deflects to. Guards against the two paths drifting apart.
    names = [t.name for t in evaluator.default_tools()]
    assert names == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "git_diff",
        "grep",
    ]
    by_name = {tool.name: tool for tool in evaluator.default_tools()}
    assert by_name["bash"].require_process_isolation is True
    assert by_name["run_tests"].require_process_isolation is True
    assert by_name["run_tests"].allow_runner_override is False
    assert by_name["run_tests"].allow_extra_args is False


# --------------------------------------------------------------------------- #
# inject-f2p: extras passthrough, test injection, diff-exclusion
# --------------------------------------------------------------------------- #


def test_eval_task_round_trips_extras():
    # extras defaults to None and carries an arbitrary benchmark dict unchanged.
    assert EvalTask(task_id="t", description="d").extras is None
    extras = {"test_patch": "diff...", "fail_to_pass": ["pkg::test_a"]}
    task = EvalTask(task_id="t", description="d", extras=extras)
    assert task.extras == extras


class InjectFakeEnv(Environment):
    """Env that faithfully models git's per-path revert of injected test files.

    Models the real driver's contamination surface: the submitted patch is
    extracted with ``git add -A && git diff --cached`` (``staged_diff`` here), so
    any injected test edit still in the tree at extraction time LEAKS. Each
    injected path can be a tracked modification (revertible with
    ``git checkout --``) or a brand-new untracked file (which ``git checkout``
    canNOT remove — it errors rc=1 — and only ``git clean -fq`` deletes). A path
    is excluded from the extracted diff only once it has been BOTH checked out and
    cleaned per-path, matching the production exclusion. This exposes the new-file
    leak the old always-succeeds fake hid.
    """

    def __init__(self, src_path="src/app.py", mod_path=None, new_path=None):
        self.src_path = src_path
        self.mod_path = mod_path  # injected tracked-file modification (or None)
        self.new_path = new_path  # injected brand-new untracked file (or None)
        # A path is "present in the working tree" (and thus leaks into the staged
        # diff) until reverted. Tracked mods clear on checkout; new files clear
        # only on clean (checkout errors on them).
        self.checked_out: set[str] = set()
        self.cleaned: set[str] = set()
        self.cmds: list[str] = []
        self.cleaned_up = False

    async def write_file(self, path: str, content: str) -> None:
        pass

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        path = f"/tmp/{prefix}{id(self):x}{suffix}"
        await self.write_file(path, content)
        return path

    async def remove_file(self, path: str) -> None:
        return None

    def _leaks(self, path: str | None, *, untracked: bool) -> bool:
        if not path:
            return False
        if untracked:
            return path not in self.cleaned  # only `git clean` removes it
        return path not in self.checked_out  # `git checkout` reverts it

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if cmd.startswith("git apply"):
            return ExecResult(returncode=0, stdout="", stderr="")
        checkout_prefix = "git --literal-pathspecs checkout -- "
        clean_prefix = "git --literal-pathspecs clean -fq -- "
        status_prefix = "git --literal-pathspecs status --porcelain=v1 -z -- "
        if cmd.startswith(checkout_prefix):
            path = cmd[len(checkout_prefix):].strip().strip("'\"")
            # git checkout errors (rc=1) on an untracked/new path and reverts
            # nothing; it restores a tracked modification.
            if path == self.new_path:
                return ExecResult(
                    returncode=1,
                    stdout="",
                    stderr=f"error: pathspec '{path}' did not match any file(s) known to git",
                )
            self.checked_out.add(path)
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith(clean_prefix):
            path = cmd[len(clean_prefix):].strip().strip("'\"")
            self.cleaned.add(path)
            return ExecResult(returncode=0, stdout="", stderr="")
        if cmd.startswith(status_prefix):
            path = cmd[len(status_prefix):].strip().strip("'\"")
            dirty = self._leaks(path, untracked=path == self.new_path)
            return ExecResult(
                returncode=0,
                stdout=f"?? {path}\n" if dirty else "",
                stderr="",
            )
        if is_worktree_diff_cmd(cmd) or cmd.startswith("git diff"):
            parts = [f"diff --git a/{self.src_path} b/{self.src_path}\n+fix\n"]
            if self._leaks(self.mod_path, untracked=False):
                parts.append(f"diff --git a/{self.mod_path} b/{self.mod_path}\n+assert thing\n")
            if self._leaks(self.new_path, untracked=True):
                parts.append(
                    f"diff --git a/{self.new_path} b/{self.new_path}\n"
                    f"new file mode 100644\n+brand new test\n"
                )
            return ExecResult(returncode=0, stdout="".join(parts), stderr="")
        return ExecResult(returncode=0, stdout="", stderr="")

    async def cleanup(self) -> None:
        self.cleaned_up = True


def test_diff_exclusion_omits_injected_test_paths(tmp_path):
    # Injected test_patch that MODIFIES an existing tracked test file.
    env = InjectFakeEnv(mod_path="tests/test_app.py")

    async def env_factory(task):
        return env

    seen = {}

    async def wf(ctx, args):
        seen["args"] = args
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
                        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+assert thing\n"
                    ),
                    "fail_to_pass": ["tests/test_app.py::test_thing"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # The injected test path was checked out before extraction.
    assert "tests/test_app.py" in env.checked_out
    checkout_cmds = [
        c for c in env.cmds if c.startswith("git --literal-pathspecs checkout --")
    ]
    assert checkout_cmds and "tests/test_app.py" in checkout_cmds[0]
    # The submitted patch contains the source edit but NOT the injected test.
    assert "src/app.py" in result.patch
    assert "tests/test_app.py" not in result.patch
    # The workflow saw fail_to_pass and the injected paths in its args dict.
    assert seen["args"]["fail_to_pass"] == ["tests/test_app.py::test_thing"]
    assert seen["args"]["injected_test_paths"] == ["tests/test_app.py"]


def test_diff_exclusion_omits_injected_new_test_file(tmp_path):
    # Regression for the new-file leak: SWE-bench test_patches commonly ADD a new
    # test file. `git checkout --` cannot remove an untracked file (errors rc=1);
    # the production exclusion must also `git clean -fq` it. The submitted patch is
    # extracted with `git add -A && git diff --cached`, so a surviving new file
    # would otherwise be staged and leak -> double-apply at grading time (D1).
    env = InjectFakeEnv(new_path="tests/test_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj-new",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n"
                        "@@ -0,0 +1 @@\n+brand new test\n"
                    ),
                    "fail_to_pass": ["tests/test_new.py::test_new"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # The injected new file was cleaned (checkout alone cannot remove it).
    assert "tests/test_new.py" in env.cleaned
    # The submitted patch contains the source edit but NOT the injected new test.
    assert "src/app.py" in result.patch
    assert "tests/test_new.py" not in result.patch


def test_diff_exclusion_one_new_file_does_not_strand_other_injected_edits(tmp_path):
    # A mixed test_patch (new file + modified existing file) must not let the new
    # file abort reverting the rest. The old single-command `git checkout -- p1 p2`
    # aborted entirely (rc=1) on the untracked path, stranding the tracked edit in
    # the submitted patch. Per-path revert keeps each independent.
    env = InjectFakeEnv(mod_path="tests/test_exist.py", new_path="tests/test_brand_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="t-inj-mixed",
                description="fix it",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_exist.py b/tests/test_exist.py\n"
                        "--- a/tests/test_exist.py\n+++ b/tests/test_exist.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+assert thing\n"
                        "diff --git a/tests/test_brand_new.py b/tests/test_brand_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_brand_new.py\n"
                        "@@ -0,0 +1 @@\n+brand new test\n"
                    ),
                    "fail_to_pass": ["tests/test_brand_new.py::test_new"],
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    # Both injected paths reverted: the tracked mod checked out, the new file cleaned.
    assert "tests/test_exist.py" in env.checked_out
    assert "tests/test_brand_new.py" in env.cleaned
    # Neither injected test leaks; only the source edit remains.
    assert "src/app.py" in result.patch
    assert "tests/test_exist.py" not in result.patch
    assert "tests/test_brand_new.py" not in result.patch


def test_failed_injected_path_cleanup_is_reported(tmp_path):
    class CleanupFailureEnv(InjectFakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git --literal-pathspecs clean -fq -- "):
                self.cmds.append(cmd)
                return ExecResult(returncode=1, stdout="", stderr="clean failed")
            return await super().exec_cmd(cmd, timeout)

    env = CleanupFailureEnv(new_path="tests/test_new.py")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="cleanup-injected",
                description="fix",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "new file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n"
                        "@@ -0,0 +1 @@\n+test\n"
                    )
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert result.error == (
        "test patch cleanup failed: RuntimeError: "
        "injected path still dirty: tests/test_new.py: ?? tests/test_new.py"
    )
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False


def test_injected_path_cleanup_aggregate_deadline_invalidates_submission(tmp_path):
    class SlowCleanupEnv(InjectFakeEnv):
        def __init__(self):
            super().__init__(mod_path="tests/test_slow.py")
            self.cleanup_timeouts: list[float] = []

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git --literal-pathspecs checkout -- "):
                self.cleanup_timeouts.append(timeout)
                await asyncio.sleep(0.01)
            return await super().exec_cmd(cmd, timeout)

    env = SlowCleanupEnv()

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="cleanup-deadline",
                description="fix",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_slow.py b/tests/test_slow.py\n"
                        "--- a/tests/test_slow.py\n"
                        "+++ b/tests/test_slow.py\n"
                        "@@ -1 +1,2 @@\n x=1\n+test\n"
                    )
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            cancellation_cleanup_timeout=0.005,
        )
    )

    assert env.cleanup_timeouts
    assert 0 < env.cleanup_timeouts[0] <= 0.005
    assert result.patch_produced is True
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False
    assert "aggregate injected-path cleanup deadline expired" in result.error


def test_failed_partial_test_patch_rollback_stops_agent_and_invalidates_output(tmp_path):
    class IsolationFailureEnv(Environment):
        def __init__(self):
            self.cmds: list[str] = []
            self.cleaned_up = False

        async def write_file(self, path: str, content: str) -> None:
            return None

        async def write_temp_file(
            self,
            content: str,
            *,
            prefix: str,
            suffix: str = ".tmp",
        ) -> str:
            path = f"/tmp/{prefix}isolation{suffix}"
            await self.write_file(path, content)
            return path

        async def remove_file(self, path: str) -> None:
            return None

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            self.cmds.append(cmd)
            if cmd.startswith("git apply --numstat"):
                return ExecResult(
                    returncode=0,
                    stdout="1\t1\ttests/test_new.py\0",
                    stderr="",
                )
            if cmd.startswith("git apply --check"):
                return ExecResult(returncode=0, stdout="", stderr="")
            if cmd.startswith("git apply -v"):
                raise OSError("git apply transport failed after partial mutation")
            if cmd.startswith("patch --dry-run"):
                return ExecResult(returncode=0, stdout="", stderr="")
            if cmd.startswith("patch -p1"):
                return ExecResult(returncode=1, stdout="", stderr="partial apply")
            if cmd.startswith("git --literal-pathspecs"):
                return ExecResult(returncode=1, stdout="", stderr="rollback failed")
            if is_worktree_diff_cmd(cmd):
                return ExecResult(
                    returncode=0,
                    stdout="diff --git a/tests/test_new.py b/tests/test_new.py\n+leak\n",
                    stderr="",
                )
            return ExecResult(returncode=0, stdout="", stderr="")

        async def cleanup(self) -> None:
            self.cleaned_up = True

    env = IsolationFailureEnv()
    workflow_ran = False
    checkpoint_dir = tmp_path / "trajectories" / "partial-injection-rollback"
    checkpoint_dir.mkdir(parents=True)
    old_checkpoint = "diff --git a/src/old.py b/src/old.py\n+preserved\n"
    checkpoint_path = checkpoint_dir / "checkpoint.worktree.patch"
    checkpoint_path.write_text(old_checkpoint, encoding="utf-8")

    async def env_factory(task):
        return env

    async def wf(ctx, args):
        nonlocal workflow_ran
        workflow_ran = True
        return "done"

    result = run(
        run_eval_task(
            EvalTask(
                task_id="partial-injection-rollback",
                description="fix",
                extras={
                    "test_patch": (
                        "diff --git a/tests/test_new.py b/tests/test_new.py\n"
                        "--- a/tests/test_new.py\n"
                        "+++ b/tests/test_new.py\n"
                        "@@ -1 +1 @@\n-old\n+injected\n"
                    )
                },
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
            checkpoint_interval_seconds=300,
        )
    )

    assert workflow_ran is False
    assert env.cleaned_up is True
    assert result.patch == ""
    assert result.patch_produced is False
    assert result.test_patch_isolation_failed is True
    assert result.injected_path_cleanup_proven is False
    assert result.submission_eligible is False
    assert "TestPatchIsolationError" in result.error
    diff_commands = [cmd for cmd in env.cmds if is_worktree_diff_cmd(cmd)]
    assert len(diff_commands) == 1
    assert result.checkpoint_result["final"]["status"] == (
        "skipped_test_patch_isolation_failure"
    )
    assert checkpoint_path.read_text(encoding="utf-8") == old_checkpoint
    diff_command = diff_commands[0]
    assert (
        "git --literal-pathspecs reset -q HEAD -- tests/test_new.py"
        in diff_command
    )
    assert "tests/test_new.py.orig" not in diff_command
    assert "tests/test_new.py.rej" not in diff_command


def test_workflow_args_preserve_fail_to_pass_when_not_injected(tmp_path):
    # A missing test patch must not erase the declared verification targets.
    # The workflow may prove that the targets already exist and pass; otherwise
    # its FAIL_TO_PASS gate must keep the result red.
    env = FakeEnv()

    async def env_factory(task):
        return env

    seen = {}

    async def wf(ctx, args):
        seen["args"] = args
        return "done"

    run(
        run_eval_task(
            EvalTask(
                task_id="t-f2p",
                description="x",
                extras={"fail_to_pass": ["pkg::test_a", "pkg::test_b"]},
            ),
            output_dir=str(tmp_path),
            tools_factory=list,
            env_factory=env_factory,
            workflow=wf,
        )
    )

    assert seen["args"]["fail_to_pass"] == ["pkg::test_a", "pkg::test_b"]
    assert "injected_test_paths" not in seen["args"]


def test_save_results_writes_patch_produced_key(tmp_path):
    results = [
        EvalResult(
            task_id="t1",
            patch="diff --git\n+x\n",
            patch_produced=True,
            tokens_used=5,
            steps=1,
            duration=0.123,
            checkpoint_result={
                "restore": {
                    "status": "restored",
                    "worktree_integrity_proven": True,
                    "submission_eligible": True,
                }
            },
        ),
    ]
    out = tmp_path / "results.jsonl"
    save_results(results, str(out))

    record = json.loads(out.read_text().strip())
    assert record["patch_produced"] is True
    assert "success" not in record
    assert record["task_id"] == "t1"
    assert record["patch"] == "diff --git\n+x\n"
    assert record["patch_lines"] == 2
    assert record["test_patch_isolation_failed"] is False
    assert record["execution_quiesced"] is True
    assert record["patch_extraction_succeeded"] is True
    assert record["injected_path_cleanup_proven"] is True
    assert record["harness_artifact_exclusion_proven"] is True
    assert record["checkpoint_restore_integrity_proven"] is True
    assert record["task_stage_integrity_proven"] is True
    assert record["submission_eligible"] is True
    assert record["checkpoint_result"]["restore"]["worktree_integrity_proven"] is True
    assert metric_submission_integrity(record) == SUBMISSION_INTEGRITY_PROVEN


def test_save_results_supports_name_max_destination(tmp_path):
    output = tmp_path / ("r" * 255)

    save_results([], str(output))

    assert output.read_bytes() == b""


def test_save_results_rejects_oversized_record_before_replace(monkeypatch, tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text('{"old": true}\n', encoding="utf-8")
    result = EvalResult(
        task_id="large",
        patch="x" * 200,
        patch_produced=True,
        tokens_used=0,
        steps=0,
        duration=0.0,
    )
    monkeypatch.setattr(evaluator, "MAX_RESULT_RECORD_BYTES", 64)

    with pytest.raises(ValueError, match="record exceeds"):
        save_results([result], str(output))

    assert output.read_text(encoding="utf-8") == '{"old": true}\n'
    temp_directory = tmp_path / evaluator.RESULT_TEMP_DIRECTORY
    assert list(temp_directory.iterdir()) == []


@pytest.mark.parametrize("nested", [False, True], ids=["root", "intermediate"])
def test_run_eval_task_rejects_symlink_output_directory_without_external_writes(
    tmp_path,
    nested,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if nested:
        safe_parent = tmp_path / "safe"
        safe_parent.mkdir()
        link = safe_parent / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output_dir = link / "nested"
    else:
        link = tmp_path / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output_dir = link

    with pytest.raises(OSError, match="not a real directory"):
        run(
            run_eval_task(
                EvalTask(task_id="symlink-output", description="x"),
                output_dir=str(output_dir),
                tools_factory=list,
            )
        )

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("nested", [False, True], ids=["root", "intermediate"])
def test_save_results_rejects_symlink_parent_without_external_writes(
    tmp_path,
    nested,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if nested:
        safe_parent = tmp_path / "safe"
        safe_parent.mkdir()
        link = safe_parent / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output = link / "nested" / "results.jsonl"
    else:
        link = tmp_path / "linked"
        link.symlink_to(outside, target_is_directory=True)
        output = link / "results.jsonl"

    with pytest.raises(OSError, match="not a real directory"):
        save_results([], str(output))

    assert list(outside.iterdir()) == []


def test_save_results_rejects_symlink_target_without_overwriting_destination(
    tmp_path,
):
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"outside": true}\n', encoding="utf-8")
    output = tmp_path / "results.jsonl"
    output.symlink_to(outside)

    with pytest.raises(OSError, match="not a regular file"):
        save_results([], str(output))

    assert outside.read_text(encoding="utf-8") == '{"outside": true}\n'


def test_save_results_detects_parent_swap_after_dirfd_replace(
    monkeypatch,
    tmp_path,
):
    parent = tmp_path / "output"
    parent.mkdir()
    moved_parent = tmp_path / "moved-output"
    output = parent / "results.jsonl"
    real_replace = evaluator.os.replace

    def replace_then_swap(source, target, **kwargs):
        real_replace(source, target, **kwargs)
        parent.rename(moved_parent)
        parent.mkdir()

    monkeypatch.setattr(evaluator.os, "replace", replace_then_swap)

    with pytest.raises(OSError, match="parent changed after atomic replace"):
        save_results([], str(output))

    assert output.exists() is False
    assert (moved_parent / "results.jsonl").is_file()


def test_save_results_detects_target_swap_after_dirfd_replace(
    monkeypatch,
    tmp_path,
):
    output = tmp_path / "results.jsonl"
    real_replace = evaluator.os.replace

    def replace_then_swap_target(source, target, **kwargs):
        real_replace(source, target, **kwargs)
        parent_fd = kwargs["dst_dir_fd"]
        os.unlink(target, dir_fd=parent_fd)
        replacement_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        os.close(replacement_fd)

    monkeypatch.setattr(evaluator.os, "replace", replace_then_swap_target)

    with pytest.raises(OSError, match="target changed after atomic replace"):
        save_results([], str(output))


def test_ineligible_safe_patch_remains_observable_but_is_not_counted(tmp_path):
    result = EvalResult(
        task_id="cleanup-unproven",
        patch="diff --git a/src/app.py b/src/app.py\n+safe internal diff\n",
        patch_produced=True,
        tokens_used=5,
        steps=1,
        duration=0.123,
        injected_path_cleanup_proven=False,
        submission_eligible=False,
    )
    out = tmp_path / "results.jsonl"

    save_results([result], str(out))

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["patch_produced"] is True
    assert record["patch"].startswith("diff --git")
    assert record["injected_path_cleanup_proven"] is False
    assert record["submission_eligible"] is False
    assert eval_cli._result_counts([result]) == (0, 1)


def test_save_results_failure_preserves_previous_file(monkeypatch, tmp_path):
    out = tmp_path / "nested" / "results.jsonl"
    out.parent.mkdir()
    out.write_text('{"old": true}\n', encoding="utf-8")
    result = EvalResult(
        task_id="replacement",
        patch="diff --git a/x b/x\n+new\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=0.1,
    )

    def fail_replace(source, target, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(evaluator.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_results([result], str(out))

    assert out.read_text(encoding="utf-8") == '{"old": true}\n'
    temp_directory = out.parent / evaluator.RESULT_TEMP_DIRECTORY
    assert temp_directory.is_dir()
    assert list(temp_directory.iterdir()) == []


@pytest.mark.parametrize(
    "failing_call, failure_message",
    [
        (2, "source directory fsync failed"),
        (3, "destination directory fsync failed"),
    ],
    ids=["source-directory", "destination-directory"],
)
def test_save_results_reports_directory_fsync_failure_after_replace(
    monkeypatch,
    tmp_path,
    failing_call,
    failure_message,
):
    out = tmp_path / "results.jsonl"
    real_fsync = evaluator.os.fsync
    fsync_calls = 0

    def fail_directory_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == failing_call:
            raise OSError(failure_message)
        return real_fsync(fd)

    monkeypatch.setattr(evaluator.os, "fsync", fail_directory_fsync)
    result = EvalResult(
        task_id="durability",
        patch="",
        patch_produced=False,
        tokens_used=0,
        steps=0,
        duration=0.1,
    )

    with pytest.raises(OSError, match=failure_message):
        save_results([result], str(out))

    assert fsync_calls == 3
    assert json.loads(out.read_text(encoding="utf-8"))["task_id"] == "durability"
    temp_directory = tmp_path / evaluator.RESULT_TEMP_DIRECTORY
    assert temp_directory.is_dir()
    assert list(temp_directory.iterdir()) == []


def test_cli_eval_preserves_task_extras(monkeypatch, tmp_path):
    tasks_path = tmp_path / "tasks.jsonl"
    extras = {
        "test_patch": (
            "diff --git a/tests/x.py b/tests/x.py\n"
            "--- a/tests/x.py\n"
            "+++ b/tests/x.py\n"
            "@@ -1 +1,2 @@\n x = 1\n+assert x\n"
        ),
        "fail_to_pass": ["tests/x.py::test_x"],
        "task_id": "spoofed-task",
        "description": "spoofed description",
        "injected_test_paths": ["spoofed.py"],
    }
    tasks_path.write_text(
        json.dumps(
            {
                "task_id": "task-with-extras",
                "description": "fix",
                "extras": extras,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    captured = {}

    async def fake_run_eval_batch(tasks, **kwargs):
        captured["tasks"] = tasks

        class InjectionEnv(FakeEnv):
            async def write_file(self, path: str, content: str) -> None:
                return None

            async def write_temp_file(
                self,
                content: str,
                *,
                prefix: str,
                suffix: str = ".tmp",
            ) -> str:
                path = f"/tmp/{prefix}owned{suffix}"
                await self.write_file(path, content)
                return path

            async def remove_file(self, path: str) -> None:
                return None

        async def env_factory(task):
            return InjectionEnv()

        async def workflow(ctx, args):
            captured["workflow_args"] = args
            return "done"

        result = await run_eval_task(
            tasks[0],
            output_dir=str(tmp_path / "inner-output"),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
        )
        return [result]

    monkeypatch.setattr(evaluator, "run_eval_batch", fake_run_eval_batch)
    monkeypatch.setattr(evaluator, "save_results", lambda results, output: None)

    run(
        eval_cli._eval(
            tasks_file=str(tasks_path),
            model="m",
            provider="openai",
            api_key="k",
            base_url=None,
            output_dir=str(tmp_path / "output"),
            concurrency=1,
            max_tokens=100,
            timeout=10,
            temperature=0.0,
        )
    )

    assert captured["tasks"][0].extras == extras
    assert captured["workflow_args"]["task_id"] == "task-with-extras"
    assert captured["workflow_args"]["description"] == "fix"
    assert captured["workflow_args"]["fail_to_pass"] == ["tests/x.py::test_x"]
    assert captured["workflow_args"]["injected_test_paths"] == ["tests/x.py"]


def test_cli_tasks_file_inside_repo_is_excluded_from_local_patch(
    monkeypatch,
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
    tasks_path.write_text(
        json.dumps(
            {
                "task_id": "cli-artifact",
                "description": "fix",
                "repo_path": str(repo),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    captured = {}

    async def fake_run_eval_batch(tasks, **kwargs):
        captured["task"] = tasks[0]

        async def env_factory(task):
            return LocalEnvironment(str(repo))

        async def workflow(ctx, args):
            return {"status": "done"}

        result = await run_eval_task(
            tasks[0],
            output_dir=str(tmp_path / "outside-output"),
            tools_factory=list,
            env_factory=env_factory,
            workflow=workflow,
        )
        captured["result"] = result
        return [result]

    monkeypatch.setattr(evaluator, "run_eval_batch", fake_run_eval_batch)
    monkeypatch.setattr(evaluator, "save_results", lambda results, output: None)

    run(
        eval_cli._eval(
            tasks_file=str(tasks_path),
            model="m",
            provider="openai",
            api_key="k",
            base_url=None,
            output_dir=str(tmp_path / "output"),
            concurrency=1,
            max_tokens=100,
            timeout=10,
            temperature=0.0,
        )
    )

    task = captured["task"]
    result = captured["result"]
    assert task.harness_artifact_paths == (str(tasks_path.resolve()),)
    assert result.patch == ""
    assert result.patch_extraction_succeeded is True
    assert result.harness_artifact_exclusion_proven is True
    assert result.submission_eligible is True


def test_cli_eval_rejects_non_object_extras_before_batch(monkeypatch, tmp_path):
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(
        json.dumps(
            {
                "task_id": "bad-extras",
                "description": "fix",
                "extras": ["not", "an", "object"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    batch_called = False

    async def fake_run_eval_batch(tasks, **kwargs):
        nonlocal batch_called
        batch_called = True
        return []

    monkeypatch.setattr(evaluator, "run_eval_batch", fake_run_eval_batch)

    with pytest.raises(ValueError, match="extras must be a JSON object"):
        run(
            eval_cli._eval(
                tasks_file=str(tasks_path),
                model="m",
                provider="openai",
                api_key="k",
                base_url=None,
                output_dir=str(tmp_path / "output"),
                concurrency=1,
                max_tokens=100,
                timeout=10,
                temperature=0.0,
            )
        )

    assert batch_called is False


def test_cli_eval_rejects_non_string_test_patch_before_batch(monkeypatch, tmp_path):
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(
        json.dumps(
            {
                "task_id": "bad-test-patch",
                "description": "fix",
                "extras": {"test_patch": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    batch_called = False

    async def fake_run_eval_batch(tasks, **kwargs):
        nonlocal batch_called
        batch_called = True
        return []

    monkeypatch.setattr(evaluator, "run_eval_batch", fake_run_eval_batch)

    with pytest.raises(ValueError, match="test_patch must be a string"):
        run(
            eval_cli._eval(
                tasks_file=str(tasks_path),
                model="m",
                provider="openai",
                api_key="k",
                base_url=None,
                output_dir=str(tmp_path / "output"),
                concurrency=1,
                max_tokens=100,
                timeout=10,
                temperature=0.0,
            )
        )

    assert batch_called is False


def test_cli_eval_rejects_oversized_task_line(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_cli, "MAX_EVAL_TASK_LINE_BYTES", 80)
    monkeypatch.setattr(eval_cli, "MAX_EVAL_TASK_FILE_BYTES", 4096)
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(
        json.dumps(
            {
                "task_id": "oversized-line",
                "description": "x" * 200,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="line 1 exceeds 80-byte limit"):
        eval_cli._read_task_payloads(str(tasks_path))


def test_cli_eval_rejects_oversized_tasks_file(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_cli, "MAX_EVAL_TASK_LINE_BYTES", 4096)
    monkeypatch.setattr(eval_cli, "MAX_EVAL_TASK_FILE_BYTES", 100)
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(
        json.dumps(
            {
                "task_id": "oversized-file",
                "description": "x" * 200,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="file exceeds 100-byte limit"):
        eval_cli._read_task_payloads(str(tasks_path))


def test_cli_eval_rejects_excess_task_count(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_cli, "MAX_EVAL_TASKS", 2)
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(
        "".join(
            json.dumps({"task_id": f"task-{index}", "description": "fix"})
            + "\n"
            for index in range(3)
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exceeds 2-task limit"):
        eval_cli._read_task_payloads(str(tasks_path))


def test_cli_eval_rejects_symlinked_tasks_file(tmp_path):
    target = tmp_path / "actual-tasks.jsonl"
    target.write_text(
        json.dumps({"task_id": "task-1", "description": "fix"}) + "\n",
        encoding="utf-8",
    )
    link = tmp_path / "tasks.jsonl"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="readable regular file"):
        eval_cli._read_task_payloads(str(link))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_cli_eval_rejects_fifo_tasks_file_without_blocking(tmp_path):
    tasks_path = tmp_path / "tasks.jsonl"
    os.mkfifo(tasks_path)

    with pytest.raises(ValueError, match="readable regular file"):
        eval_cli._read_task_payloads(str(tasks_path))


def test_save_results_creates_parent_for_empty_batch(tmp_path):
    out = tmp_path / "new" / "results.jsonl"

    save_results([], str(out))

    assert out.read_text(encoding="utf-8") == ""
