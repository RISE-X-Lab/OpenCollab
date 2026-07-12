"""Shared fixtures and helpers for SWE v1 pro-lite runner tests."""

from __future__ import annotations

import fcntl
import importlib
import inspect
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from generation_proof_test_support import trusted_patch_proof_fields

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

runner = importlib.import_module("scripts.swe_v1_prolite_runner")
remote_runner = importlib.import_module("opencollab.harness.swe_v1_remote_runner")
remote_commands = importlib.import_module("opencollab.harness.swe_v1_remote_commands")
remote_eval_script = importlib.import_module(
    "opencollab.harness.swe_v1_remote_eval_script"
)
remote_evaluation = importlib.import_module(
    "opencollab.harness.swe_v1_remote_evaluation"
)
remote_generation = importlib.import_module(
    "opencollab.harness.swe_v1_remote_generation"
)
remote_records = importlib.import_module("opencollab.harness.swe_v1_remote_records")
remote_state = importlib.import_module("opencollab.harness.swe_v1_remote_state")
remote_target_proof = importlib.import_module(
    "opencollab.harness.swe_v1_remote_target_proof"
)
swe_eval_records = importlib.import_module("opencollab.harness.swe_eval_records")
_REMOTE_NAMESPACES_BY_BASE: dict[str, dict] = {}

REMOTE_IMPLEMENTATION_SOURCE = "\n".join(
    inspect.getsource(module)
    for module in (
        remote_commands,
        remote_generation,
        remote_evaluation,
        remote_eval_script,
        remote_records,
        remote_target_proof,
    )
)

REMOTE_TEST_RUNNER = """import json
import pathlib
import sys
from opencollab.harness.swe_v1_remote_runner import install_into
namespace = {}
install_into(namespace, json.loads(sys.stdin.read()))
namespace["process_start_identity"] = lambda pid: f"test:{pid}"
namespace["http_health"] = lambda *args, **kwargs: {"ok": True, "status": "test_bypass"}
namespace["bind_eval_container_marker"] = lambda *args, **kwargs: {"ok": True, "status": "test_bound"}
def test_container_cleanup(cidfile, marker_path, container_name):
    pathlib.Path(cidfile).unlink(missing_ok=True)
    pathlib.Path(marker_path).unlink(missing_ok=True)
    return {"ok": True, "status": "test_cleaned"}
namespace["cleanup_eval_container"] = test_container_cleanup
namespace["clear_pending_eval_marker"] = test_container_cleanup
namespace["cleanup_preflight_container"] = lambda *args, **kwargs: {
    "ok": True,
    "status": "test_cleaned",
}
namespace["initialize_runner_ownership"]()
raise SystemExit(namespace["main"]())
"""


class _NoopHealthServer:
    server_port = 1

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        return None


def _proof_namespace() -> dict[str, object]:
    return {
        "eval_log_has_infra_failure": remote_generation.eval_log_has_infra_failure,
        "fail_to_pass_execution_proof": remote_target_proof.fail_to_pass_execution_proof,
    }


def _command_namespace() -> dict[str, object]:
    namespace = dict(vars(remote_target_proof))
    namespace.update(vars(remote_commands))
    return namespace


def _patch_fallback_function() -> str:
    match = re.search(
        r"apply_patch_with_fallback\(\) \{.*?\n\}",
        remote_eval_script.DIRECT_EVAL_SCRIPT,
        re.S,
    )
    assert match is not None
    return match.group(0)


def _complete_remote_config(config: dict) -> dict:
    completed = dict(config)
    completed.setdefault("owner_nonce", "d" * 32)
    completed.setdefault("invocation_id", "e" * 32)
    completed.setdefault("workflow_env", {})
    completed.setdefault("openhands_command", "")
    completed.setdefault("openhands_empty_patch_rejections", 2)
    completed.setdefault("max_empty_patch_retries", 1)
    completed.setdefault("llm_model", "")
    completed.setdefault("llm_provider", "anthropic")
    completed.setdefault("context_window", None)
    completed.setdefault("temperature", None)
    completed.setdefault("top_p", None)
    completed.setdefault("max_output_tokens", None)
    completed.setdefault("image_repository", "registry.example/swebench")
    completed.setdefault("max_eval_attempts", 2)
    completed.setdefault("eval_only", False)
    completed.setdefault("eval_dir_name", "official_eval")
    return completed


def _remote_config(tmp_path, **overrides):
    remote_root = tmp_path / "remote"
    remote_repo = remote_root / "repo"
    remote_repo.mkdir(parents=True, exist_ok=True)
    package_link = remote_repo / "opencollab"
    if not package_link.exists():
        package_link.symlink_to(
            _REPO_ROOT / "opencollab",
            target_is_directory=True,
        )
    base_run_dir = tmp_path / "run"
    cfg = {
        "token": "tok",
        "owner_nonce": "a" * 32,
        "invocation_id": "b" * 32,
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "workflow_env": {},
        "openhands_command": "",
        "openhands_empty_patch_rejections": 2,
        "max_empty_patch_retries": 1,
        "model_name": "model",
        "llm_model": "",
        "llm_provider": "anthropic",
        "context_window": None,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "session_prefix": "test",
        "image_repository": "registry.example/swebench",
        "remote_proxy_base_url": "http://127.0.0.1:18788",
        "start_index": 1,
        "limit": 1,
        "budget": 1000,
        "max_steps": 3,
        "swe_timeout": 10,
        "task_wall_timeout": 10,
        "eval_timeout": 10,
        "llm_timeout": 10,
        "checkpoint_interval": 300,
        "max_task_starts": 1,
        "max_eval_attempts": 2,
        "eval_only": False,
        "eval_dir_name": "official_eval",
        "dry_run": False,
    }
    cfg.update(overrides)
    return cfg


def _remote_namespace(tmp_path, **overrides):
    cfg = _remote_config(tmp_path, **overrides)
    namespace = {"__name__": "swe_v1_remote_runner_test"}
    remote_runner.install_into(namespace, cfg)
    namespace["RUNNER_LOCK_FD"] = -1
    namespace["RUNNER_OWNER_RECORD"] = {
        "owner_nonce": cfg["owner_nonce"],
        "pid": os.getpid(),
    }
    _REMOTE_NAMESPACES_BY_BASE[str(namespace["base_run_dir"])] = namespace
    return namespace


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    namespace = next(
        (
            value
            for base, value in _REMOTE_NAMESPACES_BY_BASE.items()
            if path == Path(base) or Path(base) in path.parents
        ),
        None,
    )
    normalized_rows = []
    for value in rows:
        row = dict(value)
        if namespace is not None and path.name == "predictions.jsonl":
            row.setdefault("model_name_or_path", namespace["model_name"])
            row.setdefault("workflow", namespace["workflow"])
            embedded = row.get("workflow_metric")
            if isinstance(embedded, dict):
                embedded = dict(embedded)
                embedded.setdefault("model_name", namespace["model_name"])
                embedded.setdefault("workflow", namespace["workflow"])
                for key, item in namespace["generation_runtime_identity"]().items():
                    embedded.setdefault(key, item)
                row["workflow_metric"] = embedded
        if namespace is not None and path.name == "metrics.jsonl":
            row.setdefault("model_name", namespace["model_name"])
            row.setdefault("workflow", namespace["workflow"])
            for key, item in namespace["generation_runtime_identity"]().items():
                row.setdefault(key, item)
        normalized_rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in normalized_rows) + "\n",
        encoding="utf-8",
    )


def _test_only_patch() -> str:
    return (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n"
        "+++ b/tests/test_widget.py\n"
        "@@ -0,0 +1 @@\n"
        "+def test_widget(): pass\n"
    )


def _proven_submission_integrity(
    patch: str = "diff --git a/src/a.py b/src/a.py\n+current\n",
) -> dict:
    return {
        "submission_eligible": True,
        "execution_quiesced": True,
        "patch_extraction_succeeded": True,
        "injected_path_cleanup_proven": True,
        "harness_artifact_exclusion_proven": True,
        "checkpoint_restore_integrity_proven": True,
        "task_stage_integrity_proven": True,
        "test_patch_isolation_failed": False,
        "worktree_integrity_proven": True,
        "patch_produced": True,
        **trusted_patch_proof_fields(patch),
    }


def _seed_remote_completed_generation(namespace, task: str = "task-1") -> None:
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
                "model_name_or_path": namespace["model_name"],
                "workflow": namespace["workflow"],
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
                **_proven_submission_integrity(),
                "model_name": namespace["model_name"],
                "workflow": namespace["workflow"],
                **namespace["generation_runtime_identity"](),
            }
        ],
    )


def _spawn_term_ignoring_descendant(tmp_path):
    ready = tmp_path / "descendant.ready"
    child_code = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    leader_code = (
        "import signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    deadline = runner.time.monotonic() + 2
    while not ready.exists() and runner.time.monotonic() < deadline:
        runner.time.sleep(0.01)
    if not ready.exists():
        runner.os.killpg(process.pid, runner.signal.SIGKILL)
        process.wait(timeout=1)
        raise AssertionError("descendant did not become ready")
    return process


def _spawn_normal_exit_with_term_ignoring_descendant(tmp_path):
    ready = tmp_path / "normal-exit-descendant.ready"
    child_code = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    leader_code = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"p=pathlib.Path({str(ready)!r}); "
        "[(time.sleep(0.01)) for _ in range(200) if not p.exists()]"
    )
    return subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


__all__ = [
    "Path",
    "REMOTE_IMPLEMENTATION_SOURCE",
    "REMOTE_TEST_RUNNER",
    "SimpleNamespace",
    "_NoopHealthServer",
    "_command_namespace",
    "_complete_remote_config",
    "_patch_fallback_function",
    "_proof_namespace",
    "_proven_submission_integrity",
    "_remote_config",
    "_remote_namespace",
    "_seed_remote_completed_generation",
    "_spawn_normal_exit_with_term_ignoring_descendant",
    "_spawn_term_ignoring_descendant",
    "_test_only_patch",
    "_write_jsonl",
    "contextmanager",
    "fcntl",
    "json",
    "os",
    "pytest",
    "runner",
    "remote_commands",
    "remote_eval_script",
    "remote_evaluation",
    "remote_generation",
    "remote_records",
    "remote_state",
    "remote_target_proof",
    "shlex",
    "signal",
    "subprocess",
    "sys",
    "threading",
    "swe_eval_records",
]
