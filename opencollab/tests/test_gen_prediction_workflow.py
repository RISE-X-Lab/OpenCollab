"""Tests for swebench/gen_prediction_workflow prompt and extras construction.

``build_task`` renders either the baseline parity prompt or a blind validation
prompt. ``build_extras`` is the matching EvalTask extras contract. We import the
module from the repo-root ``swebench/`` dir, the same way the script bootstraps.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab.harness.evaluator import EvalResult, EvalTask

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

gpw = pytest.importorskip("gen_prediction_workflow")


FIXTURE = {
    "instance_id": "acme__widget-42",
    "repo": "acme/widget",
    "problem_statement": "Widget explodes on empty input.",
    "hints_text": "look at parse()",
    "FAIL_TO_PASS": '["tests/test_widget.py::test_empty", "tests/test_widget.py::test_none"]',
    "test_patch": (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n+++ b/tests/test_widget.py\n"
        "@@ -1 +1,2 @@\n x=1\n+assert widget('') == ''\n"
    ),
}


def test_fail_to_pass_ids_parses_json_string():
    assert gpw._fail_to_pass_ids(FIXTURE) == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_fail_to_pass_ids_accepts_list_and_missing():
    assert gpw._fail_to_pass_ids({"FAIL_TO_PASS": ["a", "b"]}) == ["a", "b"]
    assert gpw._fail_to_pass_ids({}) == []


def test_build_task_lists_target_tests_without_literal_values():
    prompt = gpw.build_task(FIXTURE)
    assert "tests/test_widget.py::test_empty" in prompt
    assert "Widget explodes on empty input." in prompt


def test_build_task_can_omit_hidden_grading_ids_for_blind_validation():
    prompt = gpw.build_task(FIXTURE, include_fail_to_pass=False)

    assert "Blind validation mode" in prompt
    assert "Widget explodes on empty input." in prompt
    assert "tests/test_widget.py::test_empty" not in prompt
    assert "Tests that must pass after your fix" not in prompt


def test_build_extras_populates_hidden_data_by_default():
    extras = gpw.build_extras(FIXTURE)

    assert extras["test_patch"] == FIXTURE["test_patch"]
    assert extras["fail_to_pass"] == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_build_extras_omits_hidden_data_for_blind_validation():
    extras = gpw.build_extras(FIXTURE, include_hidden_tests=False)

    assert extras == {"blind_validation": True}
    assert "test_patch" not in extras
    assert "fail_to_pass" not in extras


def test_validation_council_defaults_to_blind_validation():
    assert gpw._blind_validation_default("validation-council-solve", None) is True
    assert gpw._blind_validation_default("generate_review_fix", None) is False
    assert gpw._blind_validation_default("validation-council-solve", False) is False
    assert gpw._blind_validation_default("generate_review_fix", True) is True


def test_generate_path_resolves_validation_council_blind_default_from_spec():
    class Spec:
        name = "validation-council-solve"

    def workflow_fn():
        return None

    workflow_fn.__workflow_spec__ = Spec()

    assert gpw._resolve_blind_validation(workflow_fn, None) is True
    assert gpw._resolve_blind_validation(workflow_fn, False) is False


def test_validation_artifact_paths_flags_temp_and_test_files():
    patch = "\n".join(
        [
            "diff --git a/widget.py b/widget.py",
            "diff --git a/tests/tmp_probe.py b/tests/tmp_probe.py",
            "diff --git a/pkg/tests/test_widget.py b/pkg/tests/test_widget.py",
            "diff --git a/.opencollab-validation/probe.py b/.opencollab-validation/probe.py",
        ]
    )

    assert gpw._validation_artifact_paths(patch) == [
        "tests/tmp_probe.py",
        "pkg/tests/test_widget.py",
        ".opencollab-validation/probe.py",
    ]


def test_validation_artifact_paths_does_not_flag_production_test_module():
    patch = "\n".join(
        [
            "diff --git a/django/test/testcases.py b/django/test/testcases.py",
            "diff --git a/sklearn/utils/_testing.py b/sklearn/utils/_testing.py",
        ]
    )

    assert gpw._validation_artifact_paths(patch) == []


def test_workflow_result_extracts_allowed_and_disallowed_paths():
    result = {
        "allowed_patch_paths": ["b/pkg/widget.py", " /src/core.py "],
        "disallowed_patch_paths": ["tests/test_widget.py"],
    }

    assert gpw._workflow_allowed_patch_paths(result) == {"pkg/widget.py", "src/core.py"}
    assert gpw._workflow_disallowed_patch_paths(result) == {"tests/test_widget.py"}


def test_patch_paths_to_remove_respects_workflow_allowlist():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/pkg/widget.py",
            "diff --git a/pkg/notes.txt b/pkg/notes.txt",
            "diff --git a/tests/test_widget.py b/tests/test_widget.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py"},
        disallowed_paths=set(),
    ) == ["pkg/notes.txt", "tests/test_widget.py"]


def test_patch_paths_to_remove_honors_disallowed_paths():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/pkg/widget.py",
            "diff --git a/tmp/check.py b/tmp/check.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py", "tmp/check.py"},
        disallowed_paths={"tmp/check.py"},
    ) == ["tmp/check.py"]


def test_empty_allowlist_removes_every_patch_path_for_guarded_workflow():
    patch = "diff --git a/pkg/widget.py b/pkg/widget.py"

    assert gpw._patch_paths_to_remove(patch, allowed_paths=set()) == ["pkg/widget.py"]


def test_extract_patch_guarded_reextracts_after_cleanup(monkeypatch):
    patches = [
        "\n".join(
            [
                "diff --git a/pkg/widget.py b/pkg/widget.py",
                "diff --git a/tmp/check.py b/tmp/check.py",
            ]
        ),
        "diff --git a/pkg/widget.py b/pkg/widget.py",
    ]
    removed = []

    def fake_extract_patch(cid):
        assert cid == "cid"
        return patches.pop(0)

    def fake_remove_patch_paths(cid, paths):
        assert cid == "cid"
        removed.extend(paths)

    monkeypatch.setattr(gpw.gp, "extract_patch", fake_extract_patch)
    monkeypatch.setattr(gpw, "_remove_patch_paths", fake_remove_patch_paths)

    patch, cleanup = gpw.extract_patch_guarded(
        "cid",
        guard_validation_artifacts=True,
        allowed_paths={"pkg/widget.py", "tmp/check.py"},
        disallowed_paths={"tmp/check.py"},
    )

    assert cleanup == ["tmp/check.py"]
    assert removed == ["tmp/check.py"]
    assert patch == "diff --git a/pkg/widget.py b/pkg/widget.py"


def test_cleanup_patch_paths_command_has_legacy_git_fallbacks():
    cmd = gpw._cleanup_patch_paths_command(["yarn.lock", "tmp/check file.py"])

    assert "git restore --staged --worktree --" in cmd
    assert "git reset -q HEAD --" in cmd
    assert "git checkout --" in cmd
    assert "git clean -fdq --" in cmd
    assert "'tmp/check file.py'" in cmd


def test_cleanup_patch_paths_command_removes_tracked_noise_without_touching_allowed(tmp_path):
    real_git = shutil.which("git")
    if not real_git:
        pytest.skip("git unavailable")

    repo = tmp_path

    def run(args):
        return subprocess.run(args, cwd=repo, text=True, capture_output=True, check=True)

    run(["git", "init"])
    (repo / "pkg.py").write_text("old\n", encoding="utf-8")
    (repo / "yarn.lock").write_text("lock\n", encoding="utf-8")
    run(["git", "add", "pkg.py", "yarn.lock"])
    run(["git", "-c", "user.name=OpenCollab", "-c", "user.email=test@example.com", "commit", "-m", "init"])

    (repo / "pkg.py").write_text("new\n", encoding="utf-8")
    (repo / "yarn.lock").write_text("rewritten\n", encoding="utf-8")
    run(["git", "add", "pkg.py", "yarn.lock"])

    subprocess.run(
        ["bash", "-lc", gpw._cleanup_patch_paths_command(["yarn.lock"])],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )

    staged = run(["git", "diff", "--cached", "--name-only"]).stdout.splitlines()
    assert staged == ["pkg.py"]
    assert (repo / "pkg.py").read_text(encoding="utf-8") == "new\n"
    assert (repo / "yarn.lock").read_text(encoding="utf-8") == "lock\n"


def test_cleanup_patch_paths_command_falls_back_when_git_restore_is_missing(tmp_path):
    real_git = shutil.which("git")
    if not real_git:
        pytest.skip("git unavailable")

    repo = tmp_path / "repo"
    repo.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "if [ \"$1\" = \"restore\" ]; then",
                "  echo 'git restore unavailable' >&2",
                "  exit 1",
                "fi",
                f"exec {shlex.quote(real_git)} \"$@\"",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    fake_env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}

    def run(args, *, env=None):
        return subprocess.run(
            args,
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    run([real_git, "init"])
    (repo / "pkg.py").write_text("old\n", encoding="utf-8")
    (repo / "yarn.lock").write_text("lock\n", encoding="utf-8")
    run([real_git, "add", "pkg.py", "yarn.lock"])
    run([real_git, "-c", "user.name=OpenCollab", "-c", "user.email=test@example.com", "commit", "-m", "init"])

    (repo / "pkg.py").write_text("new\n", encoding="utf-8")
    (repo / "yarn.lock").write_text("rewritten\n", encoding="utf-8")
    run([real_git, "add", "pkg.py", "yarn.lock"])

    subprocess.run(
        ["bash", "-lc", gpw._cleanup_patch_paths_command(["yarn.lock"])],
        cwd=repo,
        env=fake_env,
        text=True,
        capture_output=True,
        check=True,
    )

    staged = run([real_git, "diff", "--cached", "--name-only"]).stdout.splitlines()
    assert staged == ["pkg.py"]
    assert (repo / "pkg.py").read_text(encoding="utf-8") == "new\n"
    assert (repo / "yarn.lock").read_text(encoding="utf-8") == "lock\n"


def test_json_safe_degrades_unknown_objects():
    class Thing:
        def __str__(self):
            return "thing"

    assert gpw._json_safe({"x": [Thing()]}) == {"x": ["thing"]}


def test_result_metrics_json_safes_workflow_result_without_patch():
    class Thing:
        def __str__(self):
            return "thing"

    result = EvalResult(
        task_id="i",
        patch="diff --git a/a b/a",
        patch_produced=True,
        tokens_used=1,
        steps=2,
        duration=3.0,
        workflow_result={"x": Thing()},
    )

    metrics = gpw._result_metrics(result)

    assert "patch" not in metrics
    assert metrics["workflow_result"] == {"x": "thing"}


def test_evaltask_contract_matches_non_blind_extras():
    # Mirror how generate() builds the EvalTask without needing docker.
    task = EvalTask(
        task_id=FIXTURE["instance_id"],
        description=gpw.build_task(FIXTURE),
        extras=gpw.build_extras(FIXTURE),
    )
    assert task.extras["test_patch"] == FIXTURE["test_patch"]
    assert task.extras["fail_to_pass"] == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_generate_forwards_checkpoint_options(monkeypatch, tmp_path):
    captured = {}

    async def fake_run_eval_task(task, **kwargs):
        captured["task"] = task
        captured["kwargs"] = kwargs
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={"allowed_patch_paths": ["pkg/a.py"]},
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(gpw.gp, "start_container", lambda image, name: "cid")
    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: True)
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: ("diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n", []),
    )
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=False,
        checkpoint_interval_seconds=300,
        resume=True,
        output=str(tmp_path / "predictions.jsonl"),
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    patch, metrics = asyncio.run(
        gpw.generate(FIXTURE, "image", cfg, args, gpw.generate_review_fix, "generate_review_fix")
    )

    assert patch.strip()
    assert metrics["checkpoint_result"] is None
    assert captured["kwargs"]["checkpoint_interval_seconds"] == 300
    assert captured["kwargs"]["resume_from_checkpoint"] is True


def test_generate_marks_non_error_patch_as_done(monkeypatch):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
            patch_produced=True,
            tokens_used=1,
            steps=1,
            duration=1.0,
            workflow_result={},
        )

    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(gpw.gp, "start_container", lambda image, name: "cid")
    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: True)
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: ("diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n", []),
    )
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=False,
        checkpoint_interval_seconds=0,
        resume=False,
        output="predictions.jsonl",
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    _patch, metrics = asyncio.run(
        gpw.generate(FIXTURE, "image", cfg, args, gpw.generate_review_fix, "generate_review_fix")
    )

    assert metrics["workflow_status"] == "done"


def test_container_marker_survives_failed_remove(monkeypatch, tmp_path):
    gpw.gp.write_container_marker(tmp_path, "cid123", "name123")
    monkeypatch.setattr(gpw.gp, "remove_container", lambda cid: False)

    removed = gpw.gp.remove_container_and_clear_marker(tmp_path, "cid123")

    assert removed is False
    assert (tmp_path / "container.id").read_text(encoding="utf-8") == "cid123\n"
    assert (tmp_path / "container.name").read_text(encoding="utf-8") == "name123\n"


def test_output_records_share_record_id_and_patch_sha():
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"

    prediction, metrics = gpw.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id="record-1",
    )

    assert prediction["record_id"] == "record-1"
    assert metrics["record_id"] == "record-1"
    assert prediction["patch_sha256"] == gpw._patch_sha256(patch)
    assert metrics["patch_sha256"] == prediction["patch_sha256"]
    assert metrics["instance_id"] == prediction["instance_id"]
