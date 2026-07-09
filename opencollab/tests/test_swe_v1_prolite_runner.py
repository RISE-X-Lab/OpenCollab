from __future__ import annotations

import io
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

runner = importlib.import_module("scripts.swe_v1_prolite_runner")


def _remote_namespace(tmp_path, **overrides):
    remote_root = tmp_path / "remote"
    remote_repo = remote_root / "repo"
    base_run_dir = tmp_path / "run"
    cfg = {
        "token": "tok",
        "remote_root": str(remote_root),
        "remote_repo": str(remote_repo),
        "base_run_dir": str(base_run_dir),
        "workflow": "validation-council-solve",
        "model_name": "model",
        "session_prefix": "test",
        "remote_proxy_base_url": "http://127.0.0.1:18788",
        "start_index": 1,
        "limit": 1,
        "budget": 1000,
        "max_steps": 3,
        "swe_timeout": 10,
        "task_wall_timeout": 10,
        "eval_timeout": 10,
        "checkpoint_interval": 300,
        "max_task_starts": 1,
        "dry_run": False,
    }
    cfg.update(overrides)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(cfg))
    namespace = {"__name__": "swe_v1_remote_runner_test"}
    remote_code = runner.REMOTE_RUNNER.rsplit("raise SystemExit(main())", 1)[0]
    try:
        exec(remote_code, namespace)
    finally:
        sys.stdin = old_stdin
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


def test_remote_runner_rejects_invalid_slice_config(tmp_path):
    namespace = _remote_namespace(tmp_path, start_index=0, limit=0, max_task_starts=0)

    errors = namespace["validate_runner_config"]()

    assert "start_index must be >= 1" in errors
    assert "limit must be > 0" in errors
    assert "max_task_starts must be >= 1" in errors


def test_remote_runner_rejects_test_only_patch_before_eval(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    done, _prediction, _metric, _pairing = namespace["generation_done"](run_dir, task)
    result = namespace["eval_for_task"]({"instance_id": task})

    assert done is False
    assert result["status"] == "empty_eval_patch_invalid"
    assert result["summary"]["eval_model_patch_chars"] == 0
    assert result["summary"]["technical_reasons"] == ["empty_eval_patch_after_filter"]


def test_filter_model_patch_handles_diff_paths_with_spaces(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/src/app code.py b/src/app code.py\n"
        "--- a/src/app code.py\n"
        "+++ b/src/app code.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/tests/test app.py b/tests/test app.py\n"
        "--- a/tests/test app.py\n"
        "+++ b/tests/test app.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "src/app code.py" in filtered
    assert "tests/test app.py" not in filtered


def test_prolite_go_command_uses_package_targets(tmp_path):
    namespace = _remote_namespace(tmp_path)

    command = namespace["prolite_test_command"](
        {"repo_language": "go"},
        ["internal/api/widget_test.go", "pkg/server/router_test.go"],
    )

    assert command == "go test ./internal/api ./pkg/server"


def test_ensure_image_pulls_missing_image(tmp_path):
    namespace = _remote_namespace(tmp_path)
    existing: set[str] = set()
    calls: list[list[str]] = []

    def fake_image_exists(image):
        return image in existing

    def fake_run(command, timeout=60):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            existing.add(command[2])
            return {"returncode": 0, "stdout": "", "stderr": ""}
        return {"returncode": 1, "stdout": "", "stderr": "unexpected"}

    namespace["image_exists"] = fake_image_exists
    namespace["run"] = fake_run

    result = namespace["ensure_image"]("example/image:tag")

    assert result["ok"] is True
    assert result["pulled"] is True
    assert calls == [["docker", "pull", "example/image:tag"]]


def test_remote_runner_does_not_reuse_stale_done_for_test_only_patch(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = _test_only_patch()
    patch_sha = namespace["patch_sha"](patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
    }
    stale_summary = {
        "status": "done",
        "task": task,
        "patch_sha256": patch_sha,
        "record_id": "r1",
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](stale_summary, prediction, task) is False


def test_ensure_remote_proxy_falls_back_when_default_remote_port_is_busy():
    calls: list[list[str]] = []
    started_ports: set[int] = set()
    old_remote_http_ok = runner.remote_http_ok
    old_local_http_ok = runner.local_http_ok
    old_start_remote_proxy_tunnel = runner.start_remote_proxy_tunnel
    old_sleep = runner.time.sleep

    def fake_remote_http_ok(*, ssh_command, host, base_url, timeout=10):
        return base_url == "http://127.0.0.1:18789" and 18789 in started_ports

    def fake_start_remote_proxy_tunnel(command):
        calls.append(command)
        forward = command[command.index("-R") + 1]
        if forward.startswith("127.0.0.1:18788:"):
            return None, "Error: remote port forwarding failed for listen port 18788"
        if forward.startswith("127.0.0.1:18789:"):
            started_ports.add(18789)
            return SimpleNamespace(poll=lambda: None), ""
        raise AssertionError(forward)

    try:
        runner.remote_http_ok = fake_remote_http_ok
        runner.local_http_ok = lambda base_url: True
        runner.start_remote_proxy_tunnel = fake_start_remote_proxy_tunnel
        runner.time.sleep = lambda _seconds: None

        summary = runner.ensure_remote_proxy(
            ssh_command=["ssh"],
            host="jinan-aws",
            local_proxy_base_url="http://127.0.0.1:8878",
            remote_proxy_base_url="http://127.0.0.1:18788",
            enabled=True,
        )
    finally:
        runner.remote_http_ok = old_remote_http_ok
        runner.local_http_ok = old_local_http_ok
        runner.start_remote_proxy_tunnel = old_start_remote_proxy_tunnel
        runner.time.sleep = old_sleep

    assert summary["status"] == "started_fallback_port"
    assert summary["remote_proxy_base_url"] == "http://127.0.0.1:18789"
    assert summary["selected_remote_port"] == 18789
    assert "-N" in calls[1]
    assert "-fN" not in calls[1]
    assert calls[0][calls[0].index("-R") + 1] == "127.0.0.1:18788:127.0.0.1:8878"
    assert calls[1][calls[1].index("-R") + 1] == "127.0.0.1:18789:127.0.0.1:8878"


def test_remote_http_ok_keeps_ssh_outer_timeout_above_short_http_probe():
    calls: list[dict] = []
    old_run = runner.subprocess.run

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        runner.subprocess.run = fake_run
        ok = runner.remote_http_ok(
            ssh_command=["ssh"],
            host="jinan-aws",
            base_url="http://127.0.0.1:18792",
            timeout=2,
        )
    finally:
        runner.subprocess.run = old_run

    assert ok is True
    assert calls[0]["timeout"] == runner.REMOTE_HEALTH_SSH_TIMEOUT_FLOOR
    assert "http://127.0.0.1:18792/healthz" in calls[0]["command"][-1]


def test_remote_http_ok_returns_false_on_outer_timeout():
    old_run = runner.subprocess.run

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    try:
        runner.subprocess.run = fake_run
        ok = runner.remote_http_ok(
            ssh_command=["ssh"],
            host="jinan-aws",
            base_url="http://127.0.0.1:18792",
            timeout=2,
        )
    finally:
        runner.subprocess.run = old_run

    assert ok is False
