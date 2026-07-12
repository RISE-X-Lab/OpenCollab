from __future__ import annotations

# ruff: noqa: F401, F403, F405, I001

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


def test_remote_runner_does_not_reuse_patch_from_different_runtime(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-identity"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps({"instance_id": task, "dockerhub_tag": "fake.image"}) + "\n",
        encoding="utf-8",
    )
    remote_repo.mkdir()
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    patch = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    patch_sha = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    old_identity = {
        "instance_id": task,
        "record_id": "old-record",
        "patch_sha256": patch_sha,
        "model_name_or_path": "teampro-model",
        "workflow": "team-pro",
    }
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({**old_identity, "model_patch": patch}) + "\n", encoding="utf-8"
    )
    (run_dir / "metrics.jsonl").write_text(
        json.dumps(
            {
                **old_identity,
                "workflow_status": "done",
                "llm_model": "old-model",
                "context_window": 100_000,
                "temperature": 0.2,
                "top_p": None,
                "max_output_tokens": 8_192,
                "budget": 1_000_000,
                "max_steps": 60,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "generation.state.json").write_text(
        json.dumps(
            {
                "workflow": "team-pro",
                "model_name": "teampro-model",
                "runtime_identity": {
                    "llm_model": "old-model",
                    "context_window": 100_000,
                    "temperature": 0.2,
                    "top_p": None,
                    "max_output_tokens": 8_192,
                    "budget": 1_000_000,
                    "max_steps": 60,
                },
                "start_count": 3,
                "starts": [
                    {
                        "workflow": "team-pro",
                        "model_name": "teampro-model",
                        "runtime_identity": {
                            "llm_model": "old-model",
                            "context_window": 100_000,
                            "temperature": 0.2,
                            "top_p": None,
                            "max_output_tokens": 8_192,
                            "budget": 1_000_000,
                            "max_steps": 60,
                        },
                    }
                ]
                * 3,
            }
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"run\" ]; then echo /app; exit 0; fi\n"
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
            "workflow": "team-pro",
            "model_name": "teampro-model",
            "llm_model": "glm-5.2",
            "context_window": 400_000,
            "temperature": 1.0,
            "top_p": 1.0,
            "max_output_tokens": 32_768,
            "session_prefix": "test",
            "remote_proxy_base_url": f"http://127.0.0.1:{server.server_port}",
            "start_index": 1,
            "limit": 1,
            "budget": 4_000_000,
            "max_steps": 60,
            "swe_timeout": 1,
            "task_wall_timeout": 1,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 1,
            "max_task_starts": 9,
            "max_eval_attempts": 2,
            "dry_run": True,
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

    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["status"] == "dry_run"
    assert summary["max_task_starts"] == 3
    assert summary["rows"][0]["generation"]["status"] == "would_generate"


def test_remote_runner_retries_generation_failures_until_start_limit(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-2"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps({"instance_id": task, "dockerhub_tag": "ok.image"}) + "\n", encoding="utf-8")
    scripts_dir = remote_repo / "scripts"
    scripts_dir.mkdir(parents=True)
    fake_generator = scripts_dir / "run_swe_v2_one_from_fifo.sh"
    fake_generator.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "task=\"$1\"\n"
        "fifo=\"$3\"\n"
        "run_dir=\"$4\"\n"
        "cat \"$fifo\" >/dev/null\n"
        "mkdir -p \"$run_dir\"\n"
        "python3 - \"$task\" \"$run_dir\" <<'PY'\n"
        "import json, pathlib, sys\n"
        "task = sys.argv[1]\n"
        "run_dir = pathlib.Path(sys.argv[2])\n"
        "count_path = run_dir / 'fake_starts.txt'\n"
        "count = int(count_path.read_text()) if count_path.exists() else 0\n"
        "count += 1\n"
        "count_path.write_text(str(count), encoding='utf-8')\n"
        "record_id = f'r{count}'\n"
        "prediction = {'instance_id': task, 'model_patch': '', 'record_id': record_id, 'patch_sha256': ''}\n"
        "metric = {'instance_id': task, 'workflow_status': 'incomplete', "
        "'record_id': record_id, 'patch_sha256': '', 'steps': count}\n"
        "with (run_dir / 'predictions.jsonl').open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(prediction) + '\\n')\n"
        "with (run_dir / 'metrics.jsonl').open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(metric) + '\\n')\n"
        "PY\n",
        encoding="utf-8",
    )
    fake_generator.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 0; fi\n"
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
            "task_wall_timeout": 30,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 1,
            "max_task_starts": 2,
            "max_eval_attempts": 2,
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
    generation = summary["rows"][0]["generation"]
    assert generation["status"] == "generation_failed"
    assert generation["generation_attempt_count"] == 2
    assert generation["max_task_starts"] == 2
    assert len(generation["attempts"]) == 2
    assert generation["start_state"]["start_count"] == 2
    assert (base_run_dir / task / "fake_starts.txt").read_text(encoding="utf-8") == "2"


@pytest.mark.parametrize("second_mode", ["empty", "fail"])
def test_remote_runner_retries_empty_patch_once(tmp_path, second_mode):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-empty"
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True)
    (run_dir / "second_mode.txt").write_text(second_mode, encoding="utf-8")
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps({"instance_id": task, "dockerhub_tag": "ok.image"}) + "\n",
        encoding="utf-8",
    )
    scripts_dir = remote_repo / "scripts"
    scripts_dir.mkdir(parents=True)
    fake_generator = scripts_dir / "run_swe_v2_one_from_fifo.sh"
    fake_generator.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "task=\"$1\"\n"
        "fifo=\"$3\"\n"
        "run_dir=\"$4\"\n"
        "cat \"$fifo\" >/dev/null\n"
        "mkdir -p \"$run_dir\"\n"
        "python3 - \"$task\" \"$run_dir\" <<'PY'\n"
        "import hashlib, json, os, pathlib, sys\n"
        "task = sys.argv[1]\n"
        "run_dir = pathlib.Path(sys.argv[2])\n"
        "count_path = run_dir / 'fake_starts.txt'\n"
        "count = int(count_path.read_text()) if count_path.exists() else 0\n"
        "count += 1\n"
        "count_path.write_text(str(count), encoding='utf-8')\n"
        "mode = (run_dir / 'second_mode.txt').read_text().strip()\n"
        "if count == 2 and mode == 'fail':\n"
        "    raise SystemExit(0)\n"
        "record_id = f'empty-{count}'\n"
        "empty_sha = hashlib.sha256(b'').hexdigest()\n"
        "model = os.environ['OPENCOLLAB_SWE_MODEL_NAME']\n"
        "workflow = os.environ['OPENCOLLAB_SWE_WORKFLOW']\n"
        "command = os.environ['OPENCOLLAB_OPENHANDS_COMMAND']\n"
        "prediction = {'instance_id': task, 'model_name_or_path': model, "
        "'workflow': workflow, 'model_patch': '', 'record_id': record_id, "
        "'patch_sha256': empty_sha}\n"
        "snapshot = {'enabled': True, 'anonymous_head': 'a' * 40, "
        "'base_tree': 'b' * 40, 'commit_count': 1, 'remote_count': 0, "
        "'extra_git_metadata': 0, 'removed_git_metadata': 0}\n"
        "proof = {'schema': 'opencollab.trusted_patch_extraction.v1', "
        "'host_trusted': True, 'fixed_anonymous_base': 'a' * 40, "
        "'base_tree': 'b' * 40, 'archive_bounded': True, "
        "'baseline_archive_sha256': 'c' * 64, 'baseline_archive_bytes': 10, "
        "'baseline_archive_entries': 1, 'baseline_extracted_bytes': 1, "
        "'workspace_archive_sha256': 'd' * 64, 'workspace_archive_bytes': 10, "
        "'workspace_archive_entries': 1, 'workspace_extracted_bytes': 1, "
        "'archive_byte_limit': 4294967296, 'extracted_byte_limit': 8589934592, "
        "'file_byte_limit': 2147483648, 'entry_limit': 1000000, "
        "'patch_byte_limit': 8388608, 'container_quiesced_before': True, "
        "'container_quiesced_after': True, 'patch_sha256': empty_sha, "
        "'patch_bytes': 0}\n"
        "metric = {'instance_id': task, 'model_name': model, 'workflow': workflow, "
        "'workflow_status': 'empty_patch_after_done', 'record_id': record_id, "
        "'patch_sha256': empty_sha, 'llm_model': 'anthropic/glm-5.2', "
        "'context_window': 400000, 'temperature': 1.0, 'top_p': 1.0, "
        "'max_output_tokens': 32768, 'budget': 16000000, 'max_steps': 60, "
        "'llm_provider': 'anthropic', 'workflow_env': {}, "
        "'llm_base_url_sha256': hashlib.sha256(b'http://127.0.0.1:1').hexdigest(), "
        "'empty_patch_rejections': 2, 'openhands_empty_patch_rejections': 2, "
        "'openhands_command_sha256': hashlib.sha256(command.encode()).hexdigest(), "
        "'solver_git_snapshot': snapshot, 'trusted_patch_extraction': proof, "
        "'submission_eligible': False, 'execution_quiesced': True, "
        "'patch_extraction_succeeded': True, 'injected_path_cleanup_proven': True, "
        "'harness_artifact_exclusion_proven': True, "
        "'checkpoint_restore_integrity_proven': True, "
        "'task_stage_integrity_proven': True, 'test_patch_isolation_failed': False, "
        "'worktree_integrity_proven': True, 'patch_produced': False}\n"
        "with (run_dir / 'predictions.jsonl').open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(prediction) + '\\n')\n"
        "with (run_dir / 'metrics.jsonl').open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(metric) + '\\n')\n"
        "PY\n",
        encoding="utf-8",
    )
    fake_generator.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1 $2\" = \"image inspect\" ]; then exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    remote_code = REMOTE_TEST_RUNNER
    cfg = {
            "token": "dummy",
            "invocation_id": "b" * 32,
            "remote_root": str(remote_root),
            "remote_repo": str(remote_repo),
            "base_run_dir": str(base_run_dir),
            "workflow": "openhands-external",
            "workflow_env": {},
            "openhands_command": "openhands --file {prompt_file}",
            "openhands_empty_patch_rejections": 2,
            "max_empty_patch_retries": 1,
            "model_name": "openhands-model",
            "llm_model": "anthropic/glm-5.2",
            "context_window": 400000,
            "temperature": 1.0,
            "top_p": 1.0,
            "max_output_tokens": 32768,
            "session_prefix": "test",
            "remote_proxy_base_url": "http://127.0.0.1:1",
            "start_index": 1,
            "limit": 1,
            "budget": 16000000,
            "max_steps": 60,
            "swe_timeout": 1,
            "task_wall_timeout": 30,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 1,
            "max_task_starts": 3,
            "max_eval_attempts": 2,
            "dry_run": False,
    }
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", remote_code],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == (0 if second_mode == "empty" else 1), proc.stderr
    summary = json.loads(proc.stdout)
    generation = summary["rows"][0]["generation"]
    assert generation["status"] == ("empty_patch" if second_mode == "empty" else "generation_failed")
    assert generation["generation_attempt_count"] == 2
    assert generation["empty_patch_retry_count"] == 1
    assert generation["max_empty_patch_retries"] == 1
    assert (base_run_dir / task / "fake_starts.txt").read_text(encoding="utf-8") == "2"

    cfg["invocation_id"] = "c" * 32
    resumed = subprocess.run(
        [sys.executable, "-c", remote_code],
        input=json.dumps(_complete_remote_config(cfg)),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_summary = json.loads(resumed.stdout)
    resumed_generation = resumed_summary["rows"][0]["generation"]
    assert resumed_generation["status"] == "empty_patch"
    assert resumed_generation["empty_patch_retry_count"] == 1
    assert (base_run_dir / task / "fake_starts.txt").read_text(encoding="utf-8") == "2"


def test_remote_generation_identity_tracks_openhands_command_hash():
    assert (
        'identity["openhands_command_sha256"] = openhands_command_sha256'
        in REMOTE_IMPLEMENTATION_SOURCE
    )
