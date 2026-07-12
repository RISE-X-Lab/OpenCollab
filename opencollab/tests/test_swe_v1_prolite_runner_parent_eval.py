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
from test_swe_eval_layer_report import _row


def test_eval_only_reconciles_the_parent_final_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    parent_row = _row(
        82,
        task,
        "/run/task-82.log",
        10,
        "technical_eval_failed",
    )
    child_row = _row(82, task, "/run/task-82.log", 10, "eval_done", False)
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "rows": [
                            parent_row
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    child = tmp_path / "task_82_report.json"
    child.write_text(
        json.dumps(
            {
                "rows": [
                    child_row
                ]
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(parent_output_dir=parent, json_output=child, usd_cny=None)

    result = runner.update_parent_fact_report(args)

    assert result["counts"]["unresolved"] == 1
    final = json.loads((parent / "final_eval_layer_report.json").read_text(encoding="utf-8"))
    assert final["counts"]["technical_failed_final"] == 0
    assert final["tasks"][0]["resolved"] is False


def test_eval_only_parent_budget_allows_only_the_remaining_attempt(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 1},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["effective_max_eval_attempts"] == 1
    assert args.max_eval_attempts == 1


def test_eval_only_parent_budget_rejects_an_extra_retry(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 2},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    try:
        runner.apply_parent_eval_budget(args)
    except RuntimeError as exc:
        assert "eval retry budget exhausted" in str(exc)
    else:
        raise AssertionError("an exhausted task must not launch another eval")


def test_eval_only_parent_budget_uses_the_updated_final_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "technical_eval_failed", "attempt_count": 1},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    (parent / "final_eval_layer_report.json").write_text(
        json.dumps({"tasks": [{"index": 82, "eval_attempt_count": 2}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    try:
        runner.apply_parent_eval_budget(args)
    except RuntimeError as exc:
        assert "eval retry budget exhausted" in str(exc)
    else:
        raise AssertionError("the updated parent report must block a third eval")


def test_eval_only_parent_budget_adds_split_parent_attempts(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    rows = [
        {
            "index": 82,
            "task": "instance_owner__repo-82",
            "eval": {"status": "technical_eval_failed", "attempt_count": 1},
        },
        {
            "index": 82,
            "task": "instance_owner__repo-82",
            "eval": {"status": "technical_eval_failed", "attempt_count": 1},
        },
    ]
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [rows[0]]}, {"rows": [rows[1]]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    try:
        runner.apply_parent_eval_budget(args)
    except RuntimeError as exc:
        assert "eval retry budget exhausted" in str(exc)
    else:
        raise AssertionError("split parent attempts must consume the full budget")


def test_eval_only_parent_budget_does_not_count_a_dry_run(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    row = {
        "index": 82,
        "task": "instance_owner__repo-82",
        "eval": {"status": "would_eval", "attempt_count": 1},
    }
    (parent / "parallel_summary.json").write_text(
        json.dumps({"results": [{"rows": [row]}]}),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        eval_only=True,
        parent_output_dir=parent,
        start_index=82,
        limit=1,
        max_eval_attempts=2,
    )

    budget = runner.apply_parent_eval_budget(args)

    assert budget["effective_max_eval_attempts"] == 2


def test_parent_eval_lock_excludes_a_second_process(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    lock_path = parent / ".eval_only.lock"
    with runner.ParentEvalLock(parent):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,sys; "
                    "handle=open(sys.argv[1], 'a+'); "
                    "\ntry:\n"
                    " fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n"
                    " raise SystemExit(0)\n"
                    "raise SystemExit(1)"
                ),
                str(lock_path),
            ],
            text=True,
            capture_output=True,
        )
    assert probe.returncode == 0, probe.stderr


def test_eval_only_cli_requires_a_parent_output_dir(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(Path(runner.__file__)), "--eval-only", "--dry-run"],
        cwd=Path(runner.__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "--eval-only requires --parent-output-dir" in proc.stderr
