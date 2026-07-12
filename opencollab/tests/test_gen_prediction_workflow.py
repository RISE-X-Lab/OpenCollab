"""Tests for swebench/gen_prediction_workflow prompt and extras construction.

``build_task`` renders either the baseline parity prompt or a blind validation
prompt. ``build_extras`` is the matching EvalTask extras contract. We import the
module from the repo-root ``swebench/`` dir, the same way the script bootstraps.
"""

from __future__ import annotations

import asyncio
import json
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
    "base_commit": "a" * 40,
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


@pytest.fixture(autouse=True)
def _isolated_solver_snapshot(monkeypatch):
    evidence = SimpleNamespace(
        as_dict=lambda: {
            "enabled": True,
            "anonymous_head": "b" * 40,
            "base_tree": "c" * 40,
            "commit_count": 1,
            "remote_count": 0,
            "extra_git_metadata": 0,
            "removed_git_metadata": 0,
        }
    )
    monkeypatch.setattr(
        gpw.gp,
        "prepare_solver_git_snapshot",
        lambda container_id, expected_base_commit: evidence,
    )
    monkeypatch.setattr(gpw.gp, "anonymous_solver_task_id", lambda: "solver-opaque-test-id")


def test_fail_to_pass_ids_parses_json_string():
    assert gpw._fail_to_pass_ids(FIXTURE) == [
        "tests/test_widget.py::test_empty",
        "tests/test_widget.py::test_none",
    ]


def test_fail_to_pass_ids_accepts_list_and_missing():
    assert gpw._fail_to_pass_ids({"FAIL_TO_PASS": ["a", "b"]}) == ["a", "b"]
    assert gpw._fail_to_pass_ids({}) == []


@pytest.mark.parametrize(
    ("max_steps", "budget", "timeout"),
    [
        (0, 100, 5.0),
        (5, 0, 5.0),
        (5, 100, 0),
        (5, 100, float("nan")),
        (5, 100, float("inf")),
    ],
)
def test_workflow_generation_limits_share_single_agent_validation(
    max_steps,
    budget,
    timeout,
):
    with pytest.raises(ValueError):
        gpw.gp.validate_generation_limits(
            max_steps=max_steps,
            budget=budget,
            timeout=timeout,
        )


@pytest.mark.parametrize(
    "checkpoint_interval",
    [-1, float("nan"), float("inf"), True, "bad"],
)
def test_workflow_generation_rejects_invalid_checkpoint_interval(
    checkpoint_interval,
):
    with pytest.raises(ValueError, match="checkpoint-interval"):
        gpw.validate_workflow_limits(
            max_steps=5,
            budget=100,
            timeout=5,
            checkpoint_interval=checkpoint_interval,
        )


def test_workflow_generation_accepts_disabled_checkpointing():
    assert gpw.validate_workflow_limits(
        max_steps=5,
        budget=100,
        timeout=5,
        checkpoint_interval=0,
    ) == (5, 100, 5.0, 0.0)


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

    assert gpw._workflow_allowed_patch_paths(result) == {
        "b/pkg/widget.py",
        "src/core.py",
    }
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


def test_agent_allowlist_cannot_override_test_artifact_blacklist():
    patch = "diff --git a/src/test_parser.py b/src/test_parser.py"

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"src/test_parser.py"},
    ) == ["src/test_parser.py"]


@pytest.mark.parametrize(
    "path",
    ["tests/test_bug.py", ".opencollab-validation/probe.py"],
)
def test_agent_allowlist_cannot_admit_blind_validation_artifact(path):
    patch = f"diff --git a/{path} b/{path}"

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={path},
        disallowed_paths=set(),
    ) == [path]


def test_patch_paths_decode_default_git_c_quoted_unicode(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source = repo / "src"
    source.mkdir()
    target = source / "café.py"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
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
    target.write_text("new\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    assert "\\303\\251" in patch
    assert gpw._patch_paths(patch) == ["src/café.py"]
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"src/café.py"},
    ) == []


def test_patch_path_normalization_preserves_repo_relative_b_directory():
    patch = "diff --git a/b/foo.py b/b/foo.py"

    assert gpw._normalize_patch_path("b/foo.py") == "b/foo.py"
    assert gpw._patch_entries(patch) == [("b/foo.py", "b/foo.py")]
    assert gpw._patch_paths(patch) == ["b/foo.py"]
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"b/foo.py"},
    ) == []
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"foo.py"},
    ) == ["b/foo.py"]


def test_test_to_allowed_rename_removes_both_endpoints():
    patch = "\n".join(
        [
            "diff --git a/tests/test_widget.py b/pkg/widget.py",
            "similarity index 100%",
            "rename from tests/test_widget.py",
            "rename to pkg/widget.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py"},
    ) == ["tests/test_widget.py", "pkg/widget.py"]


def test_copy_with_unallowed_endpoint_removes_both_endpoints():
    patch = "\n".join(
        [
            "diff --git a/pkg/widget.py b/tmp/validation_copy.py",
            "similarity index 100%",
            "copy from pkg/widget.py",
            "copy to tmp/validation_copy.py",
        ]
    )

    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"pkg/widget.py"},
    ) == ["pkg/widget.py", "tmp/validation_copy.py"]


def test_git_c_quoted_unicode_rename_removes_both_endpoints(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source_dir = repo / "tests"
    source_dir.mkdir()
    source = source_dir / "naïve.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
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
    target_dir = repo / "src"
    target_dir.mkdir()
    target = target_dir / "café.py"
    source.rename(target)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    patch = subprocess.run(
        ["git", "diff", "--cached", "--find-renames"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    assert "rename from" in patch
    assert "\\303\\257" in patch
    assert "\\303\\251" in patch
    assert gpw._patch_entries(patch) == [("tests/naïve.py", "src/café.py")]
    assert gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"src/café.py"},
    ) == ["tests/naïve.py", "src/café.py"]


def test_extract_patch_guarded_reextracts_after_cleanup(monkeypatch):
    patches = [
        "\n".join(
            [
                "diff --git a/pkg/widget.py b/pkg/widget.py",
                "diff --git a/tmp/check.py b/pkg/from_check.py",
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
        allowed_paths={"pkg/widget.py", "tmp/check.py", "pkg/from_check.py"},
        disallowed_paths={"tmp/check.py"},
    )

    assert cleanup == ["tmp/check.py", "pkg/from_check.py"]
    assert removed == ["tmp/check.py", "pkg/from_check.py"]
    assert patch == "diff --git a/pkg/widget.py b/pkg/widget.py"


def test_cleanup_patch_paths_command_has_literal_git_fallbacks():
    cmd = gpw._cleanup_patch_paths_command(["yarn.lock", "tmp/check file.py"])

    assert "git --literal-pathspecs restore --staged --worktree --" in cmd
    assert "git --literal-pathspecs reset -q HEAD --" in cmd
    assert "git --literal-pathspecs checkout --" in cmd
    assert "git --literal-pathspecs clean -fdq --" in cmd
    assert cmd.count("git --literal-pathspecs") == 4
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


def test_cleanup_patch_paths_command_restores_both_rename_endpoints(tmp_path):
    repo = tmp_path

    def run(args):
        return subprocess.run(
            args,
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )

    run(["git", "init", "-q"])
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_path = tests_dir / "test_widget.py"
    test_path.write_text("test data\n", encoding="utf-8")
    allowed_path = repo / "widget.py"
    allowed_path.write_text("old\n", encoding="utf-8")
    run(["git", "add", "."])
    run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ]
    )

    renamed_path = repo / "widget_from_test.py"
    test_path.rename(renamed_path)
    allowed_path.write_text("new\n", encoding="utf-8")
    run(["git", "add", "-A"])
    patch = run(["git", "diff", "--cached", "--find-renames"]).stdout
    violations = gpw._patch_paths_to_remove(
        patch,
        allowed_paths={"widget.py", "widget_from_test.py"},
    )

    assert violations == ["tests/test_widget.py", "widget_from_test.py"]
    subprocess.run(
        ["bash", "-lc", gpw._cleanup_patch_paths_command(violations)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )

    assert run(["git", "diff", "--cached", "--name-only"]).stdout.splitlines() == [
        "widget.py"
    ]
    assert test_path.read_text(encoding="utf-8") == "test data\n"
    assert not renamed_path.exists()


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
                "if [ \"$1\" = \"--literal-pathspecs\" ] "
                "&& [ \"$2\" = \"restore\" ]; then",
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
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
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
    assert metrics["solver_git_snapshot"]["commit_count"] == 1
    assert captured["task"].task_id == "solver-opaque-test-id"
    assert FIXTURE["instance_id"] not in captured["task"].task_id
    assert captured["kwargs"]["checkpoint_interval_seconds"] == 300
    assert captured["kwargs"]["resume_from_checkpoint"] is True


@pytest.mark.parametrize(
    "result_flags",
    [
        {
            "test_patch_isolation_failed": True,
            "submission_eligible": False,
        },
        {
            "execution_quiesced": False,
            "submission_eligible": False,
        },
        {
            "patch_extraction_succeeded": False,
            "submission_eligible": False,
        },
        {
            "injected_path_cleanup_proven": False,
            "submission_eligible": False,
        },
    ],
    ids=[
        "test-patch-isolation",
        "execution-not-quiesced",
        "internal-extraction-failed",
        "injected-cleanup-unproven",
    ],
)
def test_generate_skips_outer_extraction_for_ineligible_eval_result(
    monkeypatch,
    tmp_path,
    result_flags,
):
    async def fake_run_eval_task(task, **kwargs):
        return EvalResult(
            task_id=task.task_id,
            patch="",
            patch_produced=False,
            tokens_used=0,
            steps=0,
            duration=1.0,
            error="harness integrity failure",
            **result_flags,
        )

    finalized = []
    monkeypatch.setattr(gpw, "run_eval_task", fake_run_eval_task)
    monkeypatch.setattr(
        gpw.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "cid",
    )
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: pytest.fail("outer patch extraction must be skipped"),
    )
    monkeypatch.setattr(
        gpw.gp,
        "finalize_container_ownership",
        lambda **kwargs: finalized.append(kwargs),
    )
    output = tmp_path / "predictions.jsonl"
    metrics_output = tmp_path / "metrics.jsonl"
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=False,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(output),
        metrics=str(metrics_output),
        model_name="model",
        _persist_output_after_cleanup=True,
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
        gpw.generate(
            FIXTURE,
            "image",
            cfg,
            args,
            gpw.generate_review_fix,
            "generate_review_fix",
        )
    )

    assert patch == ""
    assert metrics["submission_eligible"] is False
    assert metrics["patch_produced"] is False
    assert len(finalized) == 1
    assert finalized[0]["cid"] == "cid"
    assert json.loads(output.read_text(encoding="utf-8"))["model_patch"] == ""


def test_generate_persists_completed_patch_only_after_container_cleanup(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    output = tmp_path / "predictions.jsonl"
    metrics_output = tmp_path / "metrics.jsonl"
    cleanup_observations = []

    def fake_cleanup(run_dir, cid):
        cleanup_observations.append(output.exists())
        gpw.gp.clear_container_marker(run_dir, cid)
        return True

    monkeypatch.setattr(gpw.gp, "remove_container_and_clear_marker", fake_cleanup)
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
        output=str(output),
        metrics=str(metrics_output),
        model_name="model",
        _persist_output_after_cleanup=True,
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
    assert cleanup_observations == [False]
    assert json.loads(output.read_text(encoding="utf-8"))["model_patch"].strip()
    assert json.loads(metrics_output.read_text(encoding="utf-8"))["workflow_status"] == "done"


def test_generate_output_symlink_race_cleans_active_container(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    output = tmp_path / "predictions.jsonl"
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")

    def race_output_before_staging(*args, **kwargs):
        output.symlink_to(victim)
        return "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n", []

    monkeypatch.setattr(gpw, "extract_patch_guarded", race_output_before_staging)
    removed = []

    def cleanup_owned_container(run_dir, cid):
        removed.append(cid)
        gpw.gp.clear_container_marker(run_dir, cid)
        return True

    monkeypatch.setattr(
        gpw.gp,
        "remove_container_and_clear_marker",
        cleanup_owned_container,
    )
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=True,
        blind_validation=False,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(output),
        metrics=str(tmp_path / "metrics.jsonl"),
        model_name="model",
        _persist_output_after_cleanup=True,
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    with pytest.raises(ValueError, match="regular file or absent"):
        asyncio.run(
            gpw.generate(
                FIXTURE,
                "image",
                cfg,
                args,
                gpw.generate_review_fix,
                "generate_review_fix",
            )
        )

    assert removed == ["cid"]
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert not list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    assert not list((tmp_path / ".opencollab" / "container_owners").glob("*.json"))


def test_generate_cleanup_failure_does_not_publish_done(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        gpw.gp, "start_container", lambda image, name, owner_token: "cid"
    )
    monkeypatch.setattr(
        gpw.gp, "remove_container_and_clear_marker", lambda run_dir, cid: False
    )
    monkeypatch.setattr(
        gpw,
        "extract_patch_guarded",
        lambda *args, **kwargs: ("diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n", []),
    )
    output = tmp_path / "predictions.jsonl"
    args = SimpleNamespace(
        timeout=10,
        budget=1000,
        max_steps=3,
        keep_container=False,
        blind_validation=False,
        checkpoint_interval_seconds=0,
        resume=False,
        output=str(output),
        metrics=str(tmp_path / "metrics.jsonl"),
        model_name="model",
        _persist_output_after_cleanup=True,
    )
    cfg = {
        "model": "m",
        "provider": "openai",
        "api_key": "k",
        "base_url": "http://local",
        "temperature": 0.0,
        "thinking": False,
    }

    with pytest.raises(RuntimeError, match="technical container cleanup failed"):
        asyncio.run(
            gpw.generate(
                FIXTURE,
                "image",
                cfg,
                args,
                gpw.generate_review_fix,
                "generate_review_fix",
            )
        )

    assert not output.exists()
    assert list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    assert list((tmp_path / ".opencollab" / "container_owners").glob("*.json"))


def test_workflow_status_does_not_relabel_provider_failure_as_timeout_patch():
    result = EvalResult(
        task_id="task-1",
        patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=1.0,
        error="TimeoutError: provider request timed out",
    )

    status = gpw._workflow_status_for_result(result, result.patch)

    assert status == "error"


def test_workflow_status_preserves_structured_advisory_gap():
    result = EvalResult(
        task_id="task-1",
        patch="diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n",
        patch_produced=True,
        tokens_used=1,
        steps=1,
        duration=1.0,
        workflow_result={"status": "advisory_gap", "done_with_advisory_gap": True},
    )

    status = gpw._workflow_status_for_result(result, result.patch)

    assert status == "advisory_gap"


def test_container_marker_survives_failed_remove(monkeypatch, tmp_path):
    gpw.gp.write_container_marker(tmp_path, "cid123", "name123")
    monkeypatch.setattr(gpw.gp, "_remove_owned_container", lambda record: False)

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
    assert metrics["runner_returncode"] == 0
    assert prediction["workflow_metric"]["runner_returncode"] == 0
