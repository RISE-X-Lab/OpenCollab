"""Shared fixtures and helpers for SWE v1 pro-lite runner tests."""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

runner = importlib.import_module("scripts.swe_v1_prolite_runner")
remote_runner = importlib.import_module("opencollab.harness.swe_v1_remote_runner")


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
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "model_name": "model",
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
    return namespace


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _test_only_patch() -> str:
    return (
        "diff --git a/tests/test_widget.py b/tests/test_widget.py\n"
        "--- a/tests/test_widget.py\n"
        "+++ b/tests/test_widget.py\n"
        "@@ -0,0 +1 @@\n"
        "+def test_widget(): pass\n"
    )


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
    "SimpleNamespace",
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
    "shlex",
    "signal",
    "subprocess",
    "sys",
    "threading",
]
