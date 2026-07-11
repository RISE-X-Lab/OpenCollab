from __future__ import annotations

from evaluator_test_support import (
    FakeEnv,
    LocalEnvironment,
    eval_cli,
    evaluator,
    json,
    os,
    pytest,
    run,
    run_eval_task,
    save_results,
    subprocess,
)


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
        "".join(json.dumps({"task_id": f"task-{index}", "description": "fix"}) + "\n" for index in range(3)),
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
