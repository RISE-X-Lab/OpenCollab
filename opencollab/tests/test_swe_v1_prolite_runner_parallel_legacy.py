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


def load_parallel_retry_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_g11_parallel_runner.py"
    spec = importlib.util.spec_from_file_location("swe_g11_parallel_runner", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parallel_runner_does_not_reuse_technical_failure_reports():
    module = load_parallel_retry_module()
    config = SimpleNamespace(
        workflow="team-pro",
        model_name="teampro-label",
        llm_model="glm-5.2",
        context_window=400_000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32_768,
        budget=4_000_000,
        max_steps=60,
        max_task_starts=3,
        max_eval_attempts=2,
        workflow_env=(),
        remote_runtime_repo="/remote/runtime",
        remote_base="/remote/run",
    )
    summary = {
        "status": "done_with_technical_failures",
        "counts": {
            "tasks": 1,
            "generation_done": 0,
            "eval_done": 0,
            "technical_failed": 1,
        },
        "rows": [
            {
                "index": 7,
                "task": "task-7",
                "generation": {"status": "generation_failed"},
                "eval": {"status": "skipped_no_generation_patch"},
            }
        ],
    }

    assert module.report_is_reusable(summary, config, 7) is False


def test_parallel_runner_never_upgrades_a_legacy_empty_patch_report():
    module = load_parallel_retry_module()
    command = "openhands --headless --file {prompt_file}"
    config = SimpleNamespace(
        workflow="openhands-external",
        model_name="openhands-label",
        llm_model="anthropic/glm-5.2",
        context_window=400_000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32_768,
        budget=16_000_000,
        max_steps=60,
        max_task_starts=1,
        max_eval_attempts=2,
        workflow_env=(),
        remote_runtime_repo="/remote/runtime",
        remote_base="/remote/run",
        openhands_command=command,
        openhands_empty_patch_rejections=2,
        max_empty_patch_retries=1,
    )
    summary = {
        "status": "done_with_technical_failures",
        "workflow": config.workflow,
        "model_name": config.model_name,
        "llm_model": config.llm_model,
        "context_window": config.context_window,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_output_tokens": config.max_output_tokens,
        "budget": config.budget,
        "max_steps": config.max_steps,
        "openhands_empty_patch_rejections": config.openhands_empty_patch_rejections,
        "max_task_starts": config.max_task_starts,
        "max_empty_patch_retries": config.max_empty_patch_retries,
        "max_eval_attempts": config.max_eval_attempts,
        "workflow_env": {},
        "eval_only": False,
        "solver_attribution": "current_run",
        "remote_runtime_repo": config.remote_runtime_repo,
        "base_run_dir": "/remote/run/task_17",
        "openhands_command_sha256": module._openhands_command_sha256(command),
        "counts": {
            "tasks": 1,
            "generation_done": 0,
            "eval_done": 0,
            "technical_failed": 1,
        },
        "rows": [
            {
                "index": 17,
                "task": "task-empty",
                "generation": {
                    "status": "generation_failed",
                    "workflow_status": "empty_patch_after_done",
                    "patch_len": 0,
                },
                "eval": {"status": "skipped_no_generation_patch"},
            }
        ],
    }

    original = json.loads(json.dumps(summary))
    normalized, changed = module.normalize_legacy_empty_patch_summary(summary)

    assert changed is False
    assert normalized is summary
    assert normalized == original
    assert module.report_is_reusable(normalized, config, 17) is False


def test_parallel_runner_rejects_legacy_eval_reports_without_integrity_proof():
    module = load_parallel_retry_module()
    config = SimpleNamespace(
        workflow="team-pro",
        model_name="teampro-label",
        llm_model="glm-5.2",
        context_window=400_000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32_768,
        budget=4_000_000,
        max_steps=60,
        max_task_starts=3,
        max_eval_attempts=2,
        workflow_env=(),
        remote_runtime_repo="/remote/runtime",
        remote_base="/remote/run",
    )
    summary = {
        "status": "done",
        "workflow": "team-pro",
        "model_name": "teampro-label",
        "llm_model": "glm-5.2",
        "context_window": 400_000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
        "budget": 4_000_000,
        "max_steps": 60,
        "max_task_starts": 3,
        "max_empty_patch_retries": 1,
        "max_eval_attempts": 2,
        "workflow_env": {},
        "eval_only": False,
        "solver_attribution": "current_run",
        "remote_runtime_repo": "/remote/runtime",
        "base_run_dir": "/remote/run/task_7",
        "counts": {
            "tasks": 1,
            "generation_done": 1,
            "eval_done": 1,
            "technical_failed": 0,
        },
        "rows": [
            {
                "index": 7,
                "task": "task-7",
                "generation": {"status": "generation_done"},
                "eval": {"status": "eval_done"},
            }
        ],
    }

    assert module.report_is_reusable(summary, config, 7) is False

    summary["workflow"] = "validation-council-solve"
    assert module.report_is_reusable(summary, config, 7) is False


def test_parallel_runner_rejects_empty_or_wrong_task_rows():
    module = load_parallel_retry_module()
    config = SimpleNamespace(
        workflow="team-pro",
        model_name="teampro-label",
        llm_model="glm-5.2",
        context_window=400_000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32_768,
        budget=4_000_000,
        max_steps=60,
        max_task_starts=3,
        max_eval_attempts=2,
        workflow_env=(),
        remote_runtime_repo="/remote/runtime",
        remote_base="/remote/run",
    )
    identity = {
        "status": "done",
        "workflow": "team-pro",
        "workflow_env": {},
        "model_name": "teampro-label",
        "llm_model": "glm-5.2",
        "context_window": 400_000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
        "budget": 4_000_000,
        "max_steps": 60,
            "max_task_starts": 3,
            "max_empty_patch_retries": 1,
            "max_eval_attempts": 2,
        "eval_only": False,
        "solver_attribution": "current_run",
        "remote_runtime_repo": "/remote/runtime",
        "base_run_dir": "/remote/run/task_7",
        "counts": {
            "tasks": 1,
            "generation_done": 1,
            "eval_done": 1,
            "technical_failed": 0,
        },
    }

    assert module.report_is_reusable({**identity, "rows": []}, config, 7) is False
    wrong = {
        "index": 999,
        "task": "task-999",
        "generation": {"status": "generation_done"},
        "eval": {"status": "eval_done"},
    }
    assert module.report_is_reusable({**identity, "rows": [wrong]}, config, 7) is False


def test_parallel_token_compact_keeps_missing_cost_markers():
    module = load_parallel_retry_module()
    config = module.resolve_config(
        SimpleNamespace(
            start_index=1,
            end_index=1,
            indices="",
            max_workers=1,
            min_workers=1,
            adaptive_recovery_tasks=2,
            run_id="test-run",
            output_dir=Path("/tmp/test-run"),
            remote_base="/remote/test-run",
            remote_eval_work_root="/remote",
            remote_runtime_repo="",
            model_name="model",
            llm_model="glm-5.2",
            session_prefix="",
                host="host",
                ssh_command="ssh",
                remote_root="/remote-root",
                image_repository="registry.example/swe-images",
                workflow="workflow",
            remote_proxy_base_url="http://127.0.0.1:1",
            local_proxy_base_url="http://127.0.0.1:2",
            proxy_env_file=Path("/tmp/token.env"),
            budget=1,
            max_steps=1,
            swe_timeout=1,
            task_wall_timeout=1,
            eval_timeout=1,
            llm_timeout=1,
            checkpoint_interval=1,
            max_task_starts=1,
            max_eval_attempts=2,
            total_timeout=1,
            runner_attempts=1,
            retry_delay_seconds=0,
            usd_cny=None,
            no_sync_runtime=True,
            no_ensure_remote_proxy=True,
            skip_preflight=True,
            skip_health_checks=True,
            no_adaptive_concurrency=False,
            dry_run=False,
        )
    )
    compact = module._compact_token_summary(
        {
            "billable": {
                "source": "api_usage",
                "total_tokens": 10,
                "cost_usd": None,
                "partial_cost_usd": 0.0,
                "missing_cost_calls": 1,
            },
            "api_usage": {
                "calls": 1,
                "total_tokens": 10,
                "cost_usd": 0.0,
                "costed_calls": 0,
                "missing_cost_calls": 1,
                "cost_usd_complete": False,
            },
            "workflow": {"attempts": 1, "total_tokens": 10},
            "consistency": {"api_minus_workflow_tokens": 0},
        },
        config,
    )

    assert compact["billable"]["partial_cost_usd"] == 0.0
    assert compact["billable"]["missing_cost_calls"] == 1
    assert compact["api_usage"]["missing_cost_calls"] == 1
    assert compact["api_usage"]["cost_usd_complete"] is False
