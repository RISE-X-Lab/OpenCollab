from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from opencollab.harness.token_cost import build_summary, collect_workflow_usage, to_markdown


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _load_cli_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_token_cost_summary.py"
    spec = importlib.util.spec_from_file_location("swe_token_cost_summary", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_token_cost_summary_uses_api_usage_as_billable_source(tmp_path):
    run_dir = tmp_path / "run"
    _write_jsonl(
        run_dir / "task_1/_runtime/repo/.opencollab/logs/api_usage.jsonl",
        [
            {
                "schema": "opencollab.api_usage.v1",
                "pid": 11,
                "status": "success",
                "model": "glm-5.2",
                "usage": {
                    "input_tokens": 1000,
                    "uncached_input_tokens": 200,
                    "cached_input_tokens": 800,
                    "output_tokens": 100,
                    "total_tokens": 1100,
                    "cost_usd": 0.001,
                },
            },
            {
                "schema": "opencollab.api_usage.v1",
                "pid": 22,
                "status": "success",
                "model": "glm-5.2",
                "usage": {
                    "input_tokens": 2000,
                    "uncached_input_tokens": 1500,
                    "cached_input_tokens": 500,
                    "output_tokens": 200,
                    "total_tokens": 2200,
                    "cost_usd": 0.004,
                },
            },
        ],
    )
    log = run_dir / "task_1/instance/generation_logs/instance.outer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("workflow: tokens=3200 steps=2 duration=30s error=None\n", encoding="utf-8")

    summary = build_summary([run_dir], usd_cny=7.0)

    assert summary["api_usage"]["calls"] == 2
    assert summary["api_usage"]["total_tokens"] == 3300
    assert summary["workflow"]["total_tokens"] == 3200
    assert summary["billable"]["source"] == "api_usage"
    assert summary["billable"]["total_tokens"] == 3300
    assert summary["billable"]["cost_usd"] == 0.005
    assert summary["billable"]["cost_cny"] == 0.035
    assert summary["consistency"]["api_minus_workflow_tokens"] == 100
    assert [group["total_tokens"] for group in summary["api_usage"]["groups"]] == [1100, 2200]
    assert [group["cost_usd"] for group in summary["api_usage"]["groups"]] == [0.001, 0.004]


def test_workflow_summary_falls_back_without_api_usage(tmp_path):
    run_dir = tmp_path / "run"
    log = run_dir / "task/generation_logs/task.outer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "workflow: tokens=10 steps=1 duration=2s error=None\n"
        "workflow: tokens=20 steps=3 duration=4s error=None\n",
        encoding="utf-8",
    )

    summary = build_summary([run_dir])

    assert summary["api_usage"]["calls"] == 0
    assert summary["workflow"]["attempts"] == 2
    assert summary["workflow"]["total_tokens"] == 30
    assert summary["billable"]["source"] == "workflow_log"
    assert summary["billable"]["total_tokens"] == 30
    assert summary["billable"]["cost_usd"] is None


def test_api_usage_without_cost_is_not_reported_as_zero_dollars(tmp_path):
    run_dir = tmp_path / "run"
    _write_jsonl(
        run_dir / ".opencollab/logs/api_usage.jsonl",
        [
            {
                "schema": "opencollab.api_usage.v1",
                "status": "success",
                "model": "glm-5.2",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            }
        ],
    )

    summary = build_summary([run_dir], usd_cny=7.0)

    assert summary["api_usage"]["missing_cost_calls"] == 1
    assert summary["api_usage"]["cost_usd_complete"] is False
    assert summary["billable"]["cost_usd"] is None
    assert summary["billable"]["partial_cost_usd"] == 0.0
    assert "cost_cny" not in summary["billable"]


def test_collect_workflow_usage_deduplicates_overlapping_roots(tmp_path):
    run_dir = tmp_path / "run"
    log = run_dir / "task/generation_logs/task.outer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("workflow: tokens=10 steps=1 duration=2s error=None\n", encoding="utf-8")

    summary = collect_workflow_usage([run_dir, log])

    assert summary["attempts"] == 1
    assert summary["total_tokens"] == 10


def test_token_cost_markdown_includes_billable_totals(tmp_path):
    run_dir = tmp_path / "run"
    log = run_dir / "task/generation_logs/task.outer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("workflow: tokens=10 steps=1 duration=2s error=None\n", encoding="utf-8")

    text = to_markdown(build_summary([run_dir]))

    assert "billable_total_tokens" in text
    assert "`10`" in text


def test_remote_summary_json_loader_tolerates_stdout_noise():
    module = _load_cli_module()
    payload = {
        "schema": "opencollab.swe_token_cost_summary.v1",
        "billable": {"total_tokens": 10},
    }
    stdout = "warning before\n" + json.dumps({"other": True}) + "\n" + json.dumps(payload) + "\nwarning after\n"

    assert module._loads_summary_json(stdout) == payload


def test_remote_summary_timeout_error_includes_context(monkeypatch):
    module = _load_cli_module()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["ssh"],
            timeout=5,
            output="stdout details",
            stderr="stderr details",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_remote_source", lambda: "print('x')")
    args = SimpleNamespace(
        ssh_command="ssh",
        remote_host="host",
        run_dir=["/remote/run"],
        model="glm-5.2",
        usd_cny=None,
        timeout=5,
    )

    try:
        module._build_remote(args)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected timeout")

    assert "timed out after 5s" in message
    assert "stdout details" in message
    assert "stderr details" in message
