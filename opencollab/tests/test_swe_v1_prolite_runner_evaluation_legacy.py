from __future__ import annotations

# ruff: noqa: E501, F401, F403, F405, I001

import hashlib
import http.server
import importlib.util
import inspect
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe_v1_prolite_runner_test_support import *


def test_remote_runner_eval_only_uses_existing_patch_without_starting_generation(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-eval-only"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps(
            {
                "instance_id": task,
                "dockerhub_tag": "fake.image",
                "repo_language": "python",
                "fail_to_pass": ["tests/test_feature.py::test_feature"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    (run_dir / "predictions.jsonl").write_text(
        json.dumps(
            {"instance_id": task, "model_patch": patch, "record_id": "existing", "patch_sha256": patch_sha}
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.jsonl").write_text(
        json.dumps(
            {"instance_id": task, "workflow_status": "done", "record_id": "existing", "patch_sha256": patch_sha}
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"run\" ]; then\n"
        "  output=\"\"; input=\"\"\n"
        "  for arg in \"$@\"; do case \"$arg\" in *:/eval_output) output=\"${arg%:/eval_output}\" ;; *:/eval_input:ro) input=\"${arg%:/eval_input:ro}\" ;; esac; done\n"
        "  mkdir -p \"$output\"\n"
        "  for name in base_commit before_repo post_before_base service_bootstrap model_patch test_patch f2p p2p; do "
        "echo 0 > \"$output/$name.exit\"; done\n"
        "  echo 0 > \"$output/f2p.batch_001.exit\"\n"
        "  echo 'pytest -p opencollab_pytest_proof -q -rA -o addopts= tests/test_feature.py::test_feature' > \"$output/f2p.batch_001.command\"\n"
        "  : > \"$output/f2p.batch_001.log\"\n"
        "  nonce=$(cat \"$input/proof.nonce\")\n"
        "  proof=\"$output/f2p.batch_001.proof.$nonce.jsonl\"\n"
        "  printf '%s\\n' '{\"event\":\"session_start\"}' '{\"event\":\"collection_finish\",\"nodeids\":[\"tests/test_feature.py::test_feature\"]}' '{\"event\":\"runtest_logreport\",\"nodeid\":\"tests/test_feature.py::test_feature\",\"when\":\"setup\",\"outcome\":\"passed\"}' '{\"event\":\"runtest_logreport\",\"nodeid\":\"tests/test_feature.py::test_feature\",\"when\":\"call\",\"outcome\":\"passed\"}' '{\"event\":\"runtest_logreport\",\"nodeid\":\"tests/test_feature.py::test_feature\",\"when\":\"teardown\",\"outcome\":\"passed\"}' '{\"event\":\"session_finish\",\"exitstatus\":0}' > \"$proof\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    cfg = {
        "token": "dummy",
        "invocation_id": "a" * 32,
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "model_name": "model",
        "session_prefix": "test",
        "remote_proxy_base_url": "http://127.0.0.1:1",
        "start_index": 1,
        "limit": 1,
        "budget": 1,
        "max_steps": 1,
        "swe_timeout": 1,
        "task_wall_timeout": 1,
        "eval_timeout": 1,
        "llm_timeout": 1,
        "checkpoint_interval": 1,
        "max_task_starts": 1,
        "max_eval_attempts": 1,
        "eval_only": True,
        "eval_dir_name": "official_eval_fresh",
        "dry_run": False,
    }
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["eval_only"] is True
    assert summary["eval_dir_name"] == "official_eval_fresh"
    assert summary["preflight"]["proxy_health"]["status"] == "skipped_eval_only"
    assert summary["preflight"]["remote_repo_exists"] is False
    assert summary["preflight"]["remote_runtime_required"] is False
    assert summary["counts"]["resolved"] == 1
    assert summary["solver_attribution"] == "historical_artifact"
    assert summary["rows"][0]["generation"]["eval_only"] is True
    assert summary["rows"][0]["generation"]["artifact_identity_status"] == "legacy_unknown"
    assert (run_dir / "official_eval_fresh" / "summary.json").exists()


def test_remote_runner_persists_eval_attempt_cap_across_resume(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-eval-resume"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps(
            {
                "instance_id": task,
                "dockerhub_tag": "fake.image",
                "repo_language": "python",
                "fail_to_pass": ["tests/test_feature.py::test_feature"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    (run_dir / "predictions.jsonl").write_text(
        json.dumps(
            {"instance_id": task, "model_patch": patch, "record_id": "existing", "patch_sha256": patch_sha}
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.jsonl").write_text(
        json.dumps(
            {"instance_id": task, "workflow_status": "done", "record_id": "existing", "patch_sha256": patch_sha}
        )
        + "\n",
        encoding="utf-8",
    )
    docker_runs = tmp_path / "docker_runs.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"run\" ]; then\n"
        f"  count_file={shlex.quote(str(docker_runs))}\n"
        "  count=0; [ ! -f \"$count_file\" ] || count=$(cat \"$count_file\")\n"
        "  echo $((count + 1)) > \"$count_file\"\n"
        "  output=\"\"\n"
        "  for arg in \"$@\"; do case \"$arg\" in *:/eval_output) output=\"${arg%:/eval_output}\" ;; esac; done\n"
        "  mkdir -p \"$output\"\n"
        "  for name in base_commit before_repo post_before_base service_bootstrap model_patch test_patch p2p; do echo 0 > \"$output/$name.exit\"; done\n"
        "  echo 1 > \"$output/f2p.exit\"\n"
        "  echo 'ECONNREFUSED 127.0.0.1:6379' > \"$output/f2p.log\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    cfg = {
        "token": "dummy",
        "invocation_id": "d" * 32,
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "model_name": "model",
        "session_prefix": "test",
        "remote_proxy_base_url": "http://127.0.0.1:1",
        "start_index": 1,
        "limit": 1,
        "budget": 1,
        "max_steps": 1,
        "swe_timeout": 1,
        "task_wall_timeout": 1,
        "eval_timeout": 1,
        "llm_timeout": 1,
        "checkpoint_interval": 1,
        "max_task_starts": 1,
        "max_eval_attempts": 2,
        "eval_only": True,
        "eval_dir_name": "official_eval_resume",
        "dry_run": False,
    }
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    first = subprocess.run(
        [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert first.returncode == 1, first.stderr
    assert docker_runs.read_text(encoding="utf-8").strip() == "2"
    assert json.loads(first.stdout)["rows"][0]["eval"]["attempt_count"] == 2

    cfg["invocation_id"] = "e" * 32
    resumed = subprocess.run(
        [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert resumed.returncode == 1, resumed.stderr
    resumed_eval = json.loads(resumed.stdout)["rows"][0]["eval"]
    assert resumed_eval["attempt_count"] == 2
    assert resumed_eval["retry_budget_exhausted"] is True
    assert docker_runs.read_text(encoding="utf-8").strip() == "2"


def test_remote_runner_retries_blocked_eval_image_once_and_caps_configured_attempts(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-1"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps(
            {
                "instance_id": task,
                "dockerhub_tag": "missing.image",
                "repo_language": "python",
                "fail_to_pass": ["tests/test_feature.py::test_feature"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    remote_repo.mkdir()
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({
            "instance_id": task,
            "model_patch": patch,
            "record_id": "r1",
            "patch_sha256": patch_sha,
            "model_name_or_path": "model",
            "workflow": "validation-council-solve",
            "budget": 1,
            "max_steps": 1,
        }) + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.jsonl").write_text(
        json.dumps({
            "instance_id": task,
            "workflow_status": "done",
            "record_id": "r1",
            "patch_sha256": patch_sha,
            "model_name_or_path": "model",
            "workflow": "validation-council-solve",
            "budget": 1,
            "max_steps": 1,
        }) + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker_calls.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$@\" >> " + str(docker_log) + "\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 1; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    server = _NoopHealthServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = {
            "token": "dummy",
            "invocation_id": "a" * 32,
            "remote_root": str(remote_root),
            "remote_repo": str(remote_repo),
            "base_run_dir": str(base_run_dir),
            "workflow": "validation-council-solve",
            "model_name": "model",
            "session_prefix": "test",
            "remote_proxy_base_url": f"http://127.0.0.1:{server.server_port}",
            "start_index": 1,
            "limit": 1,
            "budget": 1,
            "max_steps": 1,
            "swe_timeout": 1,
            "task_wall_timeout": 1,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 1,
            "max_task_starts": 1,
            "max_eval_attempts": 5,
            "eval_only": True,
            "dry_run": False,
        }
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", REMOTE_TEST_RUNNER],
        input=json.dumps(_complete_remote_config(cfg)),
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
    finally:
        server.shutdown()

    assert proc.returncode == 1
    summary = json.loads(proc.stdout)
    assert summary["max_eval_attempts"] == 2
    assert summary["counts"]["eval_attempts"] == 0
    assert summary["counts"]["eval_retry_tasks"] == 0
    evaluation = summary["rows"][0]["eval"]
    assert evaluation["status"] == "blocked_missing_eval_image"
    assert evaluation["attempt_count"] == 0
    assert evaluation["max_eval_attempts"] == 2
    assert len(evaluation["attempts"]) == 2
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert docker_calls.count("image inspect registry.example/swebench:missing.image") == 2
    assert docker_calls.count("pull registry.example/swebench:missing.image") == 2




def test_remote_runner_resets_the_workspace_to_the_dataset_base_commit():
    assert 'git reset --hard "$expected_base_commit"' in REMOTE_IMPLEMENTATION_SOURCE
    assert '"base_commit_status": base_commit_status' in REMOTE_IMPLEMENTATION_SOURCE
    assert 'actual_after_before="$(git rev-parse HEAD' in REMOTE_IMPLEMENTATION_SOURCE
    assert '"post_before_base_status": post_before_base_status' in REMOTE_IMPLEMENTATION_SOURCE


def test_local_eval_only_skips_generation_dependencies():
    main_source = inspect.getsource(runner.main)
    controller_source = inspect.getsource(runner.run_remote)
    assert "if args.eval_only:" in main_source
    assert (
        '"token": "" if eval_only else get_proxy_token(args.proxy_env_file)'
        in controller_source
    )
