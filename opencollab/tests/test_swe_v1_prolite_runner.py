from __future__ import annotations

import hashlib
import http.server
import importlib.util
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
import scripts.swe_v1_prolite_runner as runner


def test_ensure_remote_proxy_falls_back_when_default_remote_port_is_busy():
    calls: list[list[str]] = []
    started_ports: set[int] = set()
    old_remote_http_ok = runner.remote_http_ok
    old_local_http_ok = runner.local_http_ok
    old_run_checked = runner.run_checked
    old_sleep = runner.time.sleep

    def fake_remote_http_ok(*, ssh_command, host, base_url, timeout=10):
        return base_url == "http://127.0.0.1:18789" and 18789 in started_ports

    def fake_run_checked(command, *, timeout=120, input_text=None):
        calls.append(command)
        forward = command[command.index("-R") + 1]
        if forward.startswith("127.0.0.1:18788:"):
            raise RuntimeError("Error: remote port forwarding failed for listen port 18788")
        if forward.startswith("127.0.0.1:18789:"):
            started_ports.add(18789)
            return None
        raise AssertionError(forward)

    try:
        runner.remote_http_ok = fake_remote_http_ok
        runner.local_http_ok = lambda base_url: True
        runner.run_checked = fake_run_checked
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
        runner.run_checked = old_run_checked
        runner.time.sleep = old_sleep

    assert summary["status"] == "started_fallback_port"
    assert summary["remote_proxy_base_url"] == "http://127.0.0.1:18789"
    assert summary["selected_remote_port"] == 18789
    assert calls[0][calls[0].index("-R") + 1] == "127.0.0.1:18788:127.0.0.1:8878"
    assert calls[-1][calls[-1].index("-R") + 1] == "127.0.0.1:18789:127.0.0.1:8878"


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


@pytest.mark.parametrize(
    ("runner_alive", "status", "expected"),
    [
        (False, "done", {"status": "done"}),
        (True, "done", None),
        (False, "running", None),
    ],
)
def test_probe_terminal_remote_summary_requires_dead_runner_and_terminal_status(
    monkeypatch, runner_alive, status, expected
):
    observed = {"runner_alive": runner_alive, "summary": {"status": status}}
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(observed),
            stderr="",
        ),
    )

    summary = runner.probe_terminal_remote_summary(
        ssh_command=["ssh"],
        host="example",
        base_run_dir="/remote/run",
    )

    assert summary == expected


def test_remote_summary_matches_payload_rejects_stale_runtime_identity():
    payload = {
        "start_index": 31,
        "limit": 1,
        "base_run_dir": "/remote/run/task_31",
        "remote_repo": "/remote/runtime",
        "invocation_id": "a" * 32,
        "workflow": "team-pro",
        "workflow_env": {},
        "model_name": "teampro-model",
        "llm_model": "glm-5.2",
        "context_window": 400000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32768,
        "budget": 4000000,
        "max_steps": 60,
        "max_task_starts": 3,
        "max_empty_patch_retries": 1,
        "max_eval_attempts": 2,
        "eval_only": False,
        "eval_dir_name": "official_eval",
    }
    summary = {
        "slice": "31",
        "base_run_dir": "/remote/run/task_31",
        "remote_runtime_repo": "/remote/runtime",
        "invocation_id": "a" * 32,
        "workflow": "team-pro",
        "workflow_env": {},
        "model_name": "teampro-model",
        "llm_model": "glm-5.2",
        "context_window": 400000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32768,
        "budget": 4000000,
        "max_steps": 60,
        "max_task_starts": 3,
        "max_empty_patch_retries": 1,
        "max_eval_attempts": 2,
        "eval_only": False,
        "eval_dir_name": "official_eval",
        "solver_attribution": "current_run",
    }

    assert runner.remote_summary_matches_payload(summary, payload) is True
    summary["invocation_id"] = "b" * 32
    assert runner.remote_summary_matches_payload(summary, payload) is False
    summary["invocation_id"] = "a" * 32
    summary["budget"] = 16000000
    assert runner.remote_summary_matches_payload(summary, payload) is False


def test_run_remote_recovers_terminal_summary_when_primary_ssh_hangs(monkeypatch):
    communicate_calls = []
    terminated = []

    class HangingProcess:
        pid = 4321
        returncode = None

        def communicate(self, input_text, timeout):
            communicate_calls.append((input_text, timeout))
            raise subprocess.TimeoutExpired(["ssh"], timeout)

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: HangingProcess())
    monkeypatch.setattr(
        runner,
        "probe_terminal_remote_summary",
        lambda **kwargs: {"status": "done", "counts": {"technical_failed": 0}},
    )
    monkeypatch.setattr(runner, "remote_summary_matches_payload", lambda summary, payload: True)
    monkeypatch.setattr(
        runner,
        "terminate_local_process_group",
        lambda proc: terminated.append(proc.pid),
    )
    args = SimpleNamespace(
        ssh_command="ssh",
        eval_only=True,
        no_ensure_remote_proxy=True,
        no_sync_runtime=True,
        host="example",
        local_proxy_base_url="http://127.0.0.1:8878",
        remote_proxy_base_url="http://127.0.0.1:18788",
        remote_runtime_repo="/remote/repo",
        proxy_env_file=Path("unused"),
        remote_root="/remote",
        base_run_dir="/remote/run",
        workflow="team-pro",
        workflow_env=[],
        openhands_command="",
        model_name="model",
        llm_model="glm-5.2",
        context_window=400000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32768,
        session_prefix="session",
        start_index=1,
        limit=1,
        budget=4000000,
        max_steps=60,
        swe_timeout=14400,
        task_wall_timeout=15300,
        eval_timeout=7200,
        llm_timeout=900,
        checkpoint_interval=300,
        max_task_starts=3,
        max_eval_attempts=2,
        eval_dir_name="official_eval",
        dry_run=False,
        total_timeout=240000,
    )

    summary = runner.run_remote(args)

    assert communicate_calls
    sent_payload = json.loads(communicate_calls[0][0])
    assert re.fullmatch(r"[0-9a-f]{32}", sent_payload["invocation_id"])
    assert terminated == [4321]
    assert summary["status"] == "done"
    assert summary["remote_transport"]["status"] == "recovered_terminal_summary"
    assert summary["remote_proxy"]["status"] == "skipped_eval_only"


def test_remote_runner_embedded_code_compiles():
    compile(runner.REMOTE_RUNNER, "<remote-runner>", "exec")


def test_runtime_sync_includes_team_pro_workflow():
    assert "workflows/analyst_solve.py" in runner.SYNC_FILES


def test_generation_shell_forwards_typed_llm_overrides():
    shell = (runner.REPO_ROOT / "scripts" / "run_swe_v2_one_from_fifo.sh").read_text(
        encoding="utf-8"
    )

    assert 'LLM_MODEL="${5:-}"' in shell
    assert 'llm_args+=(--model "$LLM_MODEL")' in shell
    assert 'llm_args+=(--temperature "$LLM_TEMPERATURE")' in shell
    assert 'llm_args+=(--top-p "$LLM_TOP_P")' in shell
    assert 'llm_args+=(--max-output-tokens "$LLM_MAX_OUTPUT_TOKENS")' in shell


def test_workflow_env_accepts_sampling_settings_and_rejects_secrets():
    assert runner.normalize_workflow_env(
        ["OPENCOLLAB_TEMPERATURE=1", "OPENCOLLAB_MAX_OUTPUT_TOKENS=32768"]
    ) == {
        "OPENCOLLAB_TEMPERATURE": "1",
        "OPENCOLLAB_MAX_OUTPUT_TOKENS": "32768",
    }
    with pytest.raises(ValueError, match="unsupported --workflow-env"):
        runner.normalize_workflow_env(["OPENCOLLAB_API_KEY=secret"])


def test_remote_runner_caps_eval_attempts_and_retries_environment_eval_failures():
    assert "max_eval_attempts = min(2, max(1," in runner.REMOTE_RUNNER
    assert 'retry_statuses = {"technical_eval_failed", "blocked_missing_eval_image"}' in runner.REMOTE_RUNNER
    assert "except subprocess.TimeoutExpired" in runner.REMOTE_RUNNER


def test_remote_runner_prepares_optional_redis_before_eval_tests():
    assert "start_optional_eval_services() {{" in runner.REMOTE_RUNNER
    assert "redis-server --bind 127.0.0.1 --port 6379" in runner.REMOTE_RUNNER
    assert '"service_setup_log_tail": read_text("service_setup.log")' in runner.REMOTE_RUNNER

    service_call = runner.REMOTE_RUNNER.rindex("start_optional_eval_services")
    f2p_call = runner.REMOTE_RUNNER.index("bash -c {shlex.quote(f2p_cmd)}")
    p2p_call = runner.REMOTE_RUNNER.index("bash -c {shlex.quote(p2p_cmd)}")

    assert service_call < f2p_call < p2p_call


def test_remote_runner_does_not_count_non_executed_eval_states():
    assert '"would_eval",' in runner.REMOTE_RUNNER
    assert '"skipped_no_generation_patch",' in runner.REMOTE_RUNNER
    assert '"blocked_missing_eval_image",' in runner.REMOTE_RUNNER
    assert 'final["attempt_count"] = eval_attempt_count(run_dir, prediction, task)' in runner.REMOTE_RUNNER


def test_remote_runner_classifies_completed_empty_patch_as_solver_result():
    assert '"status": "empty_patch"' in runner.REMOTE_RUNNER
    assert '"status": "skipped_empty_patch"' in runner.REMOTE_RUNNER
    assert 'generation_ok_statuses = {"generation_done", "empty_patch"}' in runner.REMOTE_RUNNER
    assert 'eval_ok_statuses = {"eval_done", "skipped_empty_patch"}' in runner.REMOTE_RUNNER
    assert 'log=str(existing_log) if existing_log.exists() else None' in runner.REMOTE_RUNNER
    assert "generation_identity_matches(prediction, metric)" in runner.REMOTE_RUNNER
    assert '"phase": "empty_patch_retry"' in runner.REMOTE_RUNNER
    assert 'final["max_empty_patch_retries"] = max_empty_patch_retries' in runner.REMOTE_RUNNER
    reuse_check = runner.REMOTE_RUNNER.index("reused_existing_artifact=True")
    start_limit = runner.REMOTE_RUNNER.index(
        "if start_count(run_dir) >= max_task_starts", reuse_check
    )
    assert reuse_check < start_limit


def test_remote_runner_eval_only_uses_existing_patch_without_starting_generation(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-eval-only"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        json.dumps({"instance_id": task, "dockerhub_tag": "fake.image", "test_cmd": "true"}) + "\n",
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
        "  output=\"\"\n"
        "  for arg in \"$@\"; do case \"$arg\" in *:/eval_output) output=\"${arg%:/eval_output}\" ;; esac; done\n"
        "  mkdir -p \"$output\"\n"
        "  for name in base_commit before_repo post_before_base model_patch test_patch f2p p2p; do "
        "echo 0 > \"$output/$name.exit\"; done\n"
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
        [sys.executable, "-c", runner.REMOTE_RUNNER],
        input=json.dumps(cfg),
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
        json.dumps({"instance_id": task, "dockerhub_tag": "fake.image", "test_cmd": "true"}) + "\n",
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
        "  for name in base_commit before_repo model_patch test_patch p2p; do echo 0 > \"$output/$name.exit\"; done\n"
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
        [sys.executable, "-c", runner.REMOTE_RUNNER],
        input=json.dumps(cfg),
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
        [sys.executable, "-c", runner.REMOTE_RUNNER],
        input=json.dumps(cfg),
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
        "if [ \"$1\" = \"run\" ]; then exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
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
            [sys.executable, "-c", runner.REMOTE_RUNNER],
            input=json.dumps(cfg),
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


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        return


def test_remote_runner_retries_blocked_eval_image_once_and_caps_configured_attempts(tmp_path):
    remote_root = tmp_path / "remote"
    remote_repo = tmp_path / "repo"
    base_run_dir = tmp_path / "run"
    task = "instance_owner__repo-1"
    dataset = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps({"instance_id": task, "dockerhub_tag": "missing.image"}) + "\n", encoding="utf-8")
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
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
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
            "dry_run": False,
        }
        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", runner.REMOTE_RUNNER],
            input=json.dumps(cfg),
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
    assert docker_calls.count("image inspect docker.1panel.live/jefzda/sweap-images:missing.image") == 2
    assert docker_calls.count("image inspect jefzda/sweap-images:missing.image") == 2


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
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
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
            [sys.executable, "-c", runner.REMOTE_RUNNER],
            input=json.dumps(cfg),
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
        "metric = {'instance_id': task, 'model_name': model, 'workflow': workflow, "
        "'workflow_status': 'empty_patch_after_done', 'record_id': record_id, "
        "'patch_sha256': empty_sha, 'llm_model': 'anthropic/glm-5.2', "
        "'context_window': 400000, 'temperature': 1.0, 'top_p': 1.0, "
        "'max_output_tokens': 32768, 'budget': 16000000, 'max_steps': 60, "
        "'empty_patch_rejections': 2, 'openhands_empty_patch_rejections': 2, "
        "'openhands_command_sha256': hashlib.sha256(command.encode()).hexdigest(), "
        "'solver_git_snapshot': snapshot}\n"
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
    remote_code = runner.REMOTE_RUNNER.replace(
        'else http_health(remote_proxy_base_url + "/healthz", timeout=45)',
        'else {"ok": True, "status": "test_bypass"}',
    )
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
        input=json.dumps(cfg),
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
        input=json.dumps(cfg),
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


def test_patch_fallback_rejects_reversed_patch(tmp_path):
    if shutil.which("patch") is None:
        return
    match = re.search(
        r"apply_patch_with_fallback\(\) \{\{.*?\n\}\}",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    function = match.group(0).replace("{{", "{").replace("}}", "}")
    (tmp_path / "file.txt").write_text("new\n", encoding="utf-8")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "--- a/file.txt",
                "+++ b/file.txt",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode != 0
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "new\n"
    assert "reversed" in log_file.read_text(encoding="utf-8", errors="replace").lower()


def test_patch_fallback_accepts_verified_already_applied_test_patch(tmp_path):
    if shutil.which("patch") is None:
        return
    match = re.search(
        r"apply_patch_with_fallback\(\) \{\{.*?\n\}\}",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    function = match.group(0).replace("{{", "{").replace("}}", "}")
    (tmp_path / "file.txt").write_text("new\n", encoding="utf-8")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "--- a/file.txt",
                "+++ b/file.txt",
                "@@ -1 +1 @@",
                "-old",
                "+new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file} "
        "ignore-space-change verify_already_applied\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode == 0
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "new\n"
    assert "verified test patch already applied" in log_file.read_text(
        encoding="utf-8", errors="replace"
    )


def test_eval_integrity_detects_missing_tests_and_proves_go_targets():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {
        "json": json,
        "re": re,
        "normalize_python_test_target": lambda target: (
            target.split("[", 1)[0]
            if "[" in target and not target.endswith("]")
            else target
        ),
    }
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)

    assert namespace["eval_log_has_infra_failure"](4, "collected 0 items") is True
    assert namespace["eval_log_has_infra_failure"](5, "no tests ran") is True
    assert namespace["eval_log_has_infra_failure"](
        1, "no required module provides package example.invalid/dependency"
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1, "request failed: getaddrinfo EAI_AGAIN nodejs.org"
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        4,
        "ERROR: not found: tests/test_feature.py::test_feature\n"
        "collected 0 items / 1 error\nno tests ran\n"
        "ImportError: cannot import name 'feature'",
    ) is False
    go_log = "\n".join(
        [
            json.dumps({"Action": "run", "Test": "TestA"}),
            json.dumps({"Action": "pass", "Test": "TestA"}),
            json.dumps({"Action": "pass", "Test": "TestB/sub"}),
        ]
    )
    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "go"},
        ["TestA", "TestB/sub"],
        0,
        go_log,
    )
    assert proof["ok"] is True
    missing = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "go"},
        ["TestA", "TestMissing"],
        0,
        go_log,
    )
    assert missing["ok"] is False
    assert missing["missing"] == ["TestMissing"]


def test_jest_command_uses_workspace_config_and_canonical_test_path():
    match = re.search(
        r"def js_runner_command\(.*?\n\ndef go_test_packages_from_patch",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"pathlib": pathlib, "shlex": shlex}
    source = match.group(0).rsplit("\n\ndef go_test_packages_from_patch", 1)[0]
    exec(source, namespace)

    files = namespace["canonical_js_test_files"](
        ["src/app/utils/replaceLocalURL.test.ts | should replace"],
        [
            "applications/drive/src/app/utils/replaceLocalURL.test.ts",
            "src/app/utils/replaceLocalURL.test.ts",
        ],
    )
    assert files == ["applications/drive/src/app/utils/replaceLocalURL.test.ts"]

    command = namespace["jest_test_command"](files)
    assert "--json" in command
    assert "--coverage=false" in command
    assert "--config applications/drive/jest.config.js" in command
    assert "--runTestsByPath applications/drive/src/app/utils/replaceLocalURL.test.ts" in command


def test_nodebb_mocha_command_forces_named_test_output():
    match = re.search(
        r"def js_runner_command\(.*?\n\nGENERATION_RETRY_STATUSES",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {
        "parse_literal_list": lambda value: value,
        "json": json,
        "pathlib": pathlib,
        "re": re,
        "shlex": shlex,
    }
    source = match.group(0).rsplit("\n\nGENERATION_RETRY_STATUSES", 1)[0]
    exec(source, namespace)

    command = namespace["prolite_test_command"](
        {
            "repo": "NodeBB/NodeBB",
            "repo_language": "javascript",
            "selected_test_files_to_run": ["test/topics.js"],
        },
        ["test/topics.js | Topic's order pinned topics should order pinned topics"],
    )

    assert "--reporter json-stream" in command
    assert "--grep" in command
    assert "undeclared failing test" not in command
    assert "test/topics.js" in command
    grep_line = next(line for line in command.splitlines() if "--grep" in line)
    tokens = shlex.split(grep_line.strip())
    selector = tokens[tokens.index("--grep") + 1]
    title = "Topic's order pinned topics should order pinned topics"
    assert re.fullmatch(selector, title)
    assert re.fullmatch(selector, title + " but not this suffix") is None


def _nodebb_runner_namespace():
    match = re.search(
        r"def js_runner_command\(.*?\n\nGENERATION_RETRY_STATUSES",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {
        "parse_literal_list": lambda value: value,
        "json": json,
        "pathlib": pathlib,
        "re": re,
        "shlex": shlex,
    }
    source = match.group(0).rsplit("\n\nGENERATION_RETRY_STATUSES", 1)[0]
    exec(source, namespace)
    return namespace


def _install_fake_mocha(tmp_path: Path) -> Path:
    binary = tmp_path / "node_modules" / ".bin" / "mocha"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

with pathlib.Path(os.environ["MOCHA_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
test_file = sys.argv[-1]
print('["end",{"tests":1,"passes":1,"failures":0}]')
raise SystemExit(3 if test_file == os.environ.get("MOCHA_FAIL_FILE") else 0)
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def test_nodebb_target_file_with_colons_stays_on_mocha_path(tmp_path: Path):
    namespace = _nodebb_runner_namespace()
    target_file = tmp_path / "targets.json"
    target_file.write_text(
        json.dumps(["test/topics.js | suite::case should pass"]),
        encoding="utf-8",
    )

    command = namespace["prolite_test_command"](
        {
            "repo": "NodeBB/NodeBB",
            "repo_language": "javascript",
            "selected_test_files_to_run": ["test/topics.js"],
        },
        ["test/topics.js | suite::case should pass"],
        str(target_file),
    )

    assert "python3 -m pytest" not in command
    assert str(target_file) in command
    assert "missing declared Mocha titles" in command


@pytest.mark.parametrize("title_count", [111, 271])
def test_nodebb_target_file_runs_one_mocha_process_per_file(
    tmp_path: Path,
    title_count: int,
):
    namespace = _nodebb_runner_namespace()
    _install_fake_mocha(tmp_path)
    calls = tmp_path / "mocha-calls.jsonl"
    target_file = tmp_path / "targets.json"
    titles = [f"test/topics.js | stateful case {index:03d}" for index in range(title_count)]
    target_file.write_text(json.dumps(titles), encoding="utf-8")
    command = namespace["mocha_test_command"](titles, ["test/topics.js"], str(target_file))

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={**os.environ, "MOCHA_CALLS": str(calls)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert len(invocations) == 1
    arguments = invocations[0]
    assert arguments[-1] == "test/topics.js"
    selector = arguments[arguments.index("--grep") + 1]
    assert all(re.fullmatch(selector, f"stateful case {index:03d}") for index in range(title_count))
    assert len(command) < 3000


def test_nodebb_target_file_continues_after_one_file_fails(tmp_path: Path):
    namespace = _nodebb_runner_namespace()
    _install_fake_mocha(tmp_path)
    calls = tmp_path / "mocha-calls.jsonl"
    target_file = tmp_path / "targets.json"
    titles = [
        "test/a.js | first case",
        "test/b.js | second case",
    ]
    target_file.write_text(json.dumps(titles), encoding="utf-8")
    command = namespace["mocha_test_command"](titles, ["test/a.js", "test/b.js"], str(target_file))

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "MOCHA_CALLS": str(calls),
            "MOCHA_FAIL_FILE": "test/a.js",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 3
    invocations = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert [arguments[-1] for arguments in invocations] == ["test/a.js", "test/b.js"]


def test_mocha_json_stream_output_proves_named_javascript_tests():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "test/topics.js | Topic's order pinned topics should error with unprivileged user",
        "test/topics.js | Topic's order pinned topics should order pinned topics",
    ]
    log = "\n".join(
        [
            '["start",{"total":188}]',
            '["pass",{"title":"should error with unprivileged user",'
            '"fullTitle":"Topic\'s order pinned topics should error with unprivileged user"}]',
            '["pass",{"title":"should order pinned topics",'
            '"fullTitle":"Topic\'s order pinned topics should order pinned topics"}]',
            '["end",{"suites":35,"tests":188,"passes":188,"failures":0}]',
        ]
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "javascript", "repo": "NodeBB/NodeBB"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected

    assert proof["missing"] == []


def test_jest_verbose_output_proves_uniquely_named_javascript_tests():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "src/usePhotosRecovery.test.ts | usePhotosRecovery should pass all state",
        "src/usePhotosRecovery.test.ts | usePhotosRecovery should report move errors",
    ]
    log = """PASS src/usePhotosRecovery.test.ts
  usePhotosRecovery
    ✓ should pass all state (70 ms)
    ✓ should report move errors (12 ms)

Test Suites: 1 passed, 1 total
Tests:       2 passed, 2 total
"""

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "typescript", "repo": "ProtonMail/WebClients"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []


def test_jest_json_maps_unique_contiguous_abbreviated_titles():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    test_file = "test/voice-broadcast/stores/VoiceBroadcastPreRecordingStore-test.ts"
    expected = [
        f"{test_file} | VoiceBroadcastPreRecordingStore | getCurrent",
        f"{test_file} | VoiceBroadcastPreRecordingStore | clearCurrent",
        f"{test_file} | when setting a current recording | getCurrent",
        f"{test_file} | and setting another pre-recording | getCurrent",
    ]
    assertions = [
        (["VoiceBroadcastPreRecordingStore"], "getCurrent() should return null"),
        (["VoiceBroadcastPreRecordingStore"], "clearCurrent() should work"),
        (
            ["VoiceBroadcastPreRecordingStore", "when setting a current recording"],
            "getCurrent() should return the recording",
        ),
        (
            [
                "VoiceBroadcastPreRecordingStore",
                "when setting a current recording",
                "and setting another pre-recording",
            ],
            "getCurrent() should return the new recording",
        ),
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/" + test_file,
                    "assertionResults": [
                        {
                            "ancestorTitles": ancestor_titles,
                            "title": title,
                            "fullName": " ".join([*ancestor_titles, title]),
                            "status": "passed",
                        }
                        for ancestor_titles, title in assertions
                    ],
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "typescript", "repo": "element-hq/element-web"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []


def test_jest_json_does_not_match_abbreviation_across_nested_title_level():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    test_file = "test/store.test.ts"
    expected = [f"{test_file} | Store | getCurrent"]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/" + test_file,
                    "assertionResults": [
                        {
                            "ancestorTitles": ["Store", "when populated"],
                            "title": "getCurrent() returns the value",
                            "fullName": "Store when populated getCurrent() returns the value",
                            "status": "passed",
                        }
                    ],
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "typescript", "repo": "element-hq/element-web"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is False
    assert proof["observed"] == []
    assert proof["missing"] == expected


def test_jest_command_groups_component_tests_under_component_config():
    match = re.search(
        r"def js_runner_command\(.*?\n\ndef go_test_packages_from_patch",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"pathlib": pathlib, "shlex": shlex}
    source = match.group(0).rsplit("\n\ndef go_test_packages_from_patch", 1)[0]
    exec(source, namespace)

    files = namespace["canonical_js_test_files"](
        ["containers/payments/RenewalNotice.test.tsx | should render"],
        [
            "packages/components/containers/payments/RenewalNotice.test.tsx",
            "containers/payments/RenewalNotice.test.tsx",
        ],
    )
    command = namespace["jest_test_command"](files)

    assert files == ["packages/components/containers/payments/RenewalNotice.test.tsx"]
    assert "--config packages/components/jest.config.js" in command
    assert "packages/components/containers/payments/RenewalNotice.test.tsx" in command


def test_jest_command_chains_multiple_workspaces_without_leading_and_operator():
    match = re.search(
        r"def js_runner_command\(.*?\n\ndef go_test_packages_from_patch",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"pathlib": pathlib, "shlex": shlex}
    source = match.group(0).rsplit("\n\ndef go_test_packages_from_patch", 1)[0]
    exec(source, namespace)

    command = namespace["jest_test_command"](
        [
            "applications/drive/src/drive.test.ts",
            "packages/components/src/component.test.ts",
        ]
    )

    assert "fi &&\nif" in command
    assert "\n&&\n" not in command


def test_jest_json_output_proves_named_javascript_tests():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "src/example.test.ts | Example should pass",
        "src/example.test.ts | Example should fail",
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "assertionResults": [
                        {"fullName": "Example should pass", "status": "passed"},
                        {"fullName": "Example should fail", "status": "failed"},
                    ]
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "ProtonMail/WebClients"},
        expected,
        1,
        log,
    )

    assert proof["observed"] == expected
    assert proof["passed"] == [expected[0]]
    assert proof["failed"] == [expected[1]]
    assert proof["missing"] == []


def test_jest_json_proof_reads_results_after_more_than_four_megabytes():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = ["src/large.test.ts | should finish"]
    event = json.dumps(
        {
            "testResults": [
                {
                    "assertionResults": [
                        {"fullName": "Large suite should finish", "status": "passed"}
                    ]
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "protonmail/webclients"},
        expected,
        0,
        "x" * 4_100_000 + "\n" + event,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert "def read_full_text(name, limit=64_000_000)" in runner.REMOTE_RUNNER


def test_jest_json_full_name_maps_unique_nested_titles_without_false_matches():
    proof_match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert proof_match is not None
    namespace = {"json": json, "re": re}
    source = proof_match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "src/example.test.ts | localhost should not replace local URLs",
        "src/example.test.ts | proton.me should not replace local URLs",
        'src/example.test.ts | should display the expected fields for the "new invitation" happy case',
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "fullName": "replaceLocalURL localhost should not replace local URLs",
                            "status": "passed",
                        },
                        {
                            "fullName": "replaceLocalURL proton.me should not replace local URLs",
                            "status": "passed",
                        },
                        {
                            "fullName": (
                                'ICS widget organizer mode should display the expected fields for the '
                                '"new invitation" happy case'
                            ),
                            "status": "failed",
                        },
                    ]
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "protonmail/webclients"},
        expected,
        1,
        log,
    )

    assert proof["observed"] == expected
    assert proof["passed"] == expected[:2]
    assert proof["failed"] == [expected[2]]
    assert proof["missing"] == []


def test_jest_json_uses_test_file_to_disambiguate_repeated_titles():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "test/a/example.test.ts | should render",
        "test/b/example.test.ts | should render",
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/a/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A should render", "status": "passed"}
                    ],
                },
                {
                    "name": "/app/test/b/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite B should render", "status": "passed"}
                    ],
                },
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"},
        expected,
        0,
        log,
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected

    only_a = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/a/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A should render", "status": "passed"}
                    ],
                }
            ]
        }
    )
    missing = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"}, expected, 0, only_a
    )
    assert missing["passed"] == [expected[0]]
    assert missing["missing"] == [expected[1]]

    wrong_file = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/b/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A unique title", "status": "passed"}
                    ],
                }
            ]
        }
    )
    unique_expected = ["test/a/example.test.ts | unique title"]
    wrong = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"},
        unique_expected,
        0,
        wrong_file,
    )
    assert wrong["observed"] == []
    assert wrong["missing"] == unique_expected

    a_pass_b_fail = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/a/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite A should render", "status": "passed"}
                    ],
                },
                {
                    "name": "/app/test/b/example.test.ts",
                    "assertionResults": [
                        {"fullName": "Suite B should render", "status": "failed"}
                    ],
                },
            ]
        }
    )
    mixed = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "element-hq/element-web"},
        expected,
        1,
        a_pass_b_fail,
    )
    assert mixed["passed"] == [expected[0]]
    assert mixed["failed"] == [expected[1]]


def test_jest_json_normalizes_multiple_declared_title_levels():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"json": json, "re": re}
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "test/recovery.test.ts | flow to set up recovery | should display the recovery key",
        "test/recovery.test.ts | flow to change recovery | should display the recovery key",
    ]
    log = json.dumps(
        {
            "testResults": [
                {
                    "name": "/app/test/recovery.test.ts",
                    "assertionResults": [
                        {
                            "fullName": "Recovery flow to set up recovery should display the recovery key",
                            "status": "passed",
                        },
                        {
                            "fullName": "Recovery flow to change recovery should display the recovery key",
                            "status": "passed",
                        },
                    ],
                }
            ]
        }
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "protonmail/webclients"}, expected, 0, log
    )

    assert proof["ok"] is True
    assert proof["passed"] == expected


def test_tutanota_uses_real_test_runner_and_proves_completed_suites():
    command_match = re.search(
        r"def js_runner_command\(.*?\n\nGENERATION_RETRY_STATUSES",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert command_match is not None
    command_namespace = {
        "parse_literal_list": lambda value: value,
        "json": json,
        "pathlib": pathlib,
        "re": re,
        "shlex": shlex,
    }
    command_source = command_match.group(0).rsplit("\n\nGENERATION_RETRY_STATUSES", 1)[0]
    exec(command_source, command_namespace)
    expected = [
        "test/tests/api/worker/rest/EntityRestClientTest.js | test suite",
        "test/tests/api/worker/rest/ServiceExecutorTest.js | test suite",
    ]

    command = command_namespace["prolite_test_command"](
        {"repo": "tutao/tutanota", "repo_language": "ts", "selected_test_files_to_run": []},
        expected,
    )

    assert "OPENCOLLAB_OSPEC_RESULTS" in command
    assert "EntityRestClient" in command
    assert "ServiceExecutor" in command
    assert "opencollabResults" in command
    assert command.endswith("&& npm_config_nodedir=/usr/local npm run test:app")
    assert command.index("const errCount = o.report(results, stats)") < command.index(
        "OPENCOLLAB_OSPEC_RESULTS"
    )
    assert command_namespace["prolite_test_command"](
        {"repo": "tutao/tutanota", "repo_language": "ts", "selected_test_files_to_run": []},
        [],
    ).endswith("exit 127")

    proof_match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert proof_match is not None
    proof_namespace = {"json": json, "re": re}
    proof_source = proof_match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(proof_source, proof_namespace)
    proof = proof_namespace["fail_to_pass_execution_proof"](
        {"repo_language": "ts", "repo": "tutao/tutanota"},
        expected,
        0,
        "OPENCOLLAB_OSPEC_RESULTS "
        + json.dumps(
            [
                {"task": "loads", "context": ["EntityRestClient", "Load"], "pass": True},
                {"task": "posts", "context": ["ServiceExecutor", "POST"], "pass": True},
            ]
        ),
    )

    assert proof["ok"] is True
    assert proof["observed"] == expected
    assert proof["passed"] == expected
    assert proof["missing"] == []


def test_go_test_command_discovers_each_named_test_package(tmp_path):
    match = re.search(
        r"def go_test_packages_from_patch\(.*?\n\ndef prolite_test_command",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {
        "json": json,
        "pathlib": pathlib,
        "re": re,
        "shlex": shlex,
        "python_test_command": lambda targets: "python3 -m pytest -vv " + " ".join(targets),
    }
    source = match.group(0).rsplit("\n\ndef prolite_test_command", 1)[0]
    exec(source, namespace)
    for package, test_name in (("pkg/a", "TestA"), ("pkg/b", "TestB")):
        path = tmp_path / package
        path.mkdir(parents=True)
        (path / "feature_test.go").write_text(
            f"package feature\nfunc {test_name}(t *testing.T) {{}}\n", encoding="utf-8"
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_go = bin_dir / "go"
    fake_go.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$GO_CALLS\"\n",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    calls = tmp_path / "go.calls"
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["GO_CALLS"] = str(calls)

    command = namespace["go_test_command"](["TestA", "TestB/subcase"])
    proc = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, text=True)

    assert proc.returncode == 0
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert any("-json ./pkg/a" in line and "TestA" in line for line in lines)
    assert any("-json ./pkg/b" in line and "TestB" in line for line in lines)


def test_ansible_test_command_forces_repository_import_root():
    match = re.search(
        r"def go_test_packages_from_patch\(.*?\n\ndef prolite_test_command",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {
        "json": json,
        "pathlib": pathlib,
        "re": re,
        "shlex": shlex,
        "python_test_command": lambda targets: "python3 -m pytest -vv " + " ".join(targets),
    }
    source = match.group(0).rsplit("\n\ndef prolite_test_command", 1)[0]
    exec(source, namespace)

    command = namespace["ansible_python_test_command"](
        ["test/units/galaxy/test_api.py::test_target"]
    )

    assert 'export PYTHONPATH="$PWD/lib${PYTHONPATH:+:$PYTHONPATH}"' in command
    assert "wrong ansible import root" in command
    assert "python3 -m pytest -vv test/units/galaxy/test_api.py::test_target" in command


def test_python_test_targets_are_batched_without_file_level_expansion():
    match = re.search(
        r"def normalize_python_test_target\(.*?\n\ndef js_runner_command",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"shlex": shlex}
    source = match.group(0).rsplit("\n\ndef js_runner_command", 1)[0]
    exec(source, namespace)
    targets = [f"tests/test_many.py::test_case[{index}]" for index in range(149)]

    compacted = namespace["compact_python_test_targets"](targets, [])
    command = namespace["python_test_command"](compacted)

    assert compacted == targets
    assert command.count("python3 -m pytest -vv") == 4
    assert "tests/test_many.py::test_case[0]" in command
    assert "tests/test_many.py::test_case[148]" in command
    malformed = [
        "tests/test_many.py::test_case[param-a",
        "tests/test_many.py::test_case[param-b",
    ]
    assert namespace["compact_python_test_targets"](malformed, []) == [
        "tests/test_many.py::test_case"
    ]


def test_python_batch_command_keeps_targets_out_of_bash_argv(tmp_path):
    match = re.search(
        r"def normalize_python_test_target\(.*?\n\ndef js_runner_command",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"shlex": shlex}
    source = match.group(0).rsplit("\n\ndef js_runner_command", 1)[0]
    exec(source, namespace)

    command = namespace["python_batch_test_command"](
        "/eval_input/p2p.targets.json", "qutebrowser/qutebrowser"
    )

    assert len(command) < 3000
    assert "/eval_input/p2p.targets.json" in command
    assert "xvfb-run" in command
    assert '"--no-xvfb"' in command
    assert '"no:xvfb"' not in command
    assert "targets[offset:offset + 40]" in command

    test_file = tmp_path / "test_param.py"
    test_file.write_text(
        "import pytest\n@pytest.mark.parametrize('value', ['alpha', 'beta'])\n"
        "def test_case(value):\n    assert value\n",
        encoding="utf-8",
    )
    targets_file = tmp_path / "targets.json"
    targets_file.write_text(json.dumps(["test_param.py::test_case[alpha"]), encoding="utf-8")
    executable = namespace["python_batch_test_command"](str(targets_file), "example/repo")
    proc = subprocess.run(
        ["bash", "-c", executable], cwd=tmp_path, text=True, capture_output=True
    )
    assert proc.returncode == 0
    assert "test_param.py::test_case[alpha] PASSED" in proc.stdout
    assert "test_param.py::test_case[beta] PASSED" in proc.stdout


def test_python_proof_preserves_passes_across_partial_batch_failure():
    match = re.search(
        r"EVAL_INFRA_FAILURE_PATTERNS = \(.*?\n\ndef eval_result_executed",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {
        "json": json,
        "re": re,
        "normalize_python_test_target": lambda target: (
            target.split("[", 1)[0]
            if "[" in target and not target.endswith("]")
            else target
        ),
    }
    source = match.group(0).rsplit("\n\ndef eval_result_executed", 1)[0]
    exec(source, namespace)
    expected = [
        "tests/test_feature.py::test_one",
        "tests/test_feature.py::test_two",
        "tests/test_feature.py::test_value[Hello World ☃]",
        "tests/test_feature.py::test_never_started",
    ]
    log = "\n".join(
        [
            "tests/test_feature.py::test_one PASSED [ 50%]",
            "XIO:  fatal IO error 11 (Resource temporarily unavailable)",
            "tests/test_feature.py::test_two PASSED [100%]",
            "tests/test_feature.py::test_value[Hello World ☃] PASSED [100%]",
            "tests/test_feature.py::test_value[Hello World ☃] FAILED [100%]",
        ]
    )

    proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "python", "repo": "qutebrowser/qutebrowser"},
        expected,
        1,
        log,
    )

    assert proof["passed"] == expected[:2]
    assert proof["failed"] == [expected[2]]
    assert proof["missing"] == [expected[3]]
    assert proof["ok"] is False
    assert namespace["eval_log_has_infra_failure"](1, log) is True

    malformed_expected = ["tests/test_feature.py::test_param[value with newline"]
    malformed_log = "\n".join(
        [
            "tests/test_feature.py::test_param[value one] PASSED [ 50%]",
            "tests/test_feature.py::test_param[value two] PASSED [100%]",
        ]
    )
    malformed_proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "python", "repo": "example/repo"},
        malformed_expected,
        0,
        malformed_log,
    )
    assert malformed_proof["passed"] == malformed_expected
    failing_family = malformed_log + "\ntests/test_feature.py::test_param[value three] FAILED [100%]"
    failing_proof = namespace["fail_to_pass_execution_proof"](
        {"repo_language": "python", "repo": "example/repo"},
        malformed_expected,
        1,
        failing_family,
    )
    assert failing_proof["passed"] == []
    assert failing_proof["failed"] == malformed_expected


def test_eval_only_identity_recomputes_full_patch_sha():
    match = re.search(
        r"def prediction_patch\(row\):.*?\n\ndef workflow_status",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    namespace = {"hashlib": hashlib, "re": re}
    source = match.group(0).rsplit("\n\ndef workflow_status", 1)[0]
    exec(source, namespace)
    task = "instance_owner__repo-1"
    patch = "diff --git a/a b/a\n"
    computed = hashlib.sha256(patch.encode()).hexdigest()
    prediction = {
        "instance_id": task,
        "record_id": "record",
        "model_patch": patch,
        "patch_sha256": computed,
    }
    metric = {
        "instance_id": task,
        "record_id": "record",
        "patch_sha256": computed,
    }

    assert namespace["row_patch_sha"](prediction) == computed
    assert namespace["completed_artifact_identity_matches"](
        prediction, metric, task
    ) is True
    prediction["patch_sha256"] = "a" * 64
    assert namespace["completed_artifact_identity_matches"](
        prediction, metric, task
    ) is False
    prediction["patch_sha256"] = computed[:12]
    assert namespace["completed_artifact_identity_matches"](
        prediction, metric, task
    ) is False


def test_patch_fallback_ignores_crlf_context_for_benchmark_test_patch(tmp_path):
    match = re.search(
        r"apply_patch_with_fallback\(\) \{\{.*?\n\}\}",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    function = match.group(0).replace("{{", "{").replace("}}", "}")
    (tmp_path / "file.txt").write_bytes(b"left\r\n")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/file.txt b/file.txt",
                "--- a/file.txt",
                "+++ b/file.txt",
                "@@ -1 +1,2 @@",
                " left",
                "+right",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file} ignore-space-change\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode == 0
    assert b"right" in (tmp_path / "file.txt").read_bytes()


def test_patch_fallback_dry_run_prevents_partial_application(tmp_path):
    match = re.search(
        r"apply_patch_with_fallback\(\) \{\{.*?\n\}\}",
        runner.REMOTE_RUNNER,
        re.S,
    )
    assert match is not None
    function = match.group(0).replace("{{", "{").replace("}}", "}")
    (tmp_path / "first.txt").write_text("first-old\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second-different\n", encoding="utf-8")
    patch_file = tmp_path / "change.patch"
    patch_file.write_text(
        "\n".join(
            [
                "diff --git a/first.txt b/first.txt",
                "--- a/first.txt",
                "+++ b/first.txt",
                "@@ -1 +1 @@",
                "-first-old",
                "+first-new",
                "diff --git a/second.txt b/second.txt",
                "--- a/second.txt",
                "+++ b/second.txt",
                "@@ -1 +1 @@",
                "-second-old",
                "+second-new",
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_file = tmp_path / "patch.log"
    script = tmp_path / "run.sh"
    script.write_text(
        f"{function}\napply_patch_with_fallback {patch_file} {log_file}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(["bash", str(script)], cwd=tmp_path, text=True)

    assert proc.returncode != 0
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first-old\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second-different\n"


def test_remote_runner_resets_the_workspace_to_the_dataset_base_commit():
    assert 'git reset --hard "$expected_base_commit"' in runner.REMOTE_RUNNER
    assert '"base_commit_status": base_commit_status' in runner.REMOTE_RUNNER
    assert 'actual_after_before="$(git rev-parse HEAD' in runner.REMOTE_RUNNER
    assert '"post_before_base_status": post_before_base_status' in runner.REMOTE_RUNNER


def test_local_eval_only_skips_generation_dependencies():
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert 'if args.eval_only:' in source
    assert '"token": "" if args.eval_only else get_proxy_token(args.proxy_env_file)' in source


def test_eval_only_reconciles_the_parent_final_report(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    task = "instance_owner__repo-82"
    patch = "a" * 64
    (parent / "parallel_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "rows": [
                            {
                                "index": 82,
                                "task": task,
                                "generation": {"status": "generation_done", "patch_sha256": patch},
                                "eval": {
                                    "status": "technical_eval_failed",
                                    "summary": {"technical_reasons": ["test_patch"]},
                                },
                            }
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
                    {
                        "index": 82,
                        "task": task,
                        "generation": {"status": "generation_done", "patch_sha256": patch},
                        "eval": {"status": "eval_done", "summary": {"resolved": False}},
                    }
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


def test_parallel_runner_normalizes_but_rejects_legacy_empty_patch_report():
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

    normalized, changed = module.normalize_legacy_empty_patch_summary(summary)

    assert changed is True
    assert normalized["counts"]["empty_patch"] == 1
    assert normalized["counts"]["technical_failed"] == 0
    assert normalized["rows"][0]["generation"]["status"] == "empty_patch"
    assert normalized["rows"][0]["eval"]["status"] == "skipped_empty_patch"
    assert module.report_is_reusable(normalized, config, 17) is False
    config.openhands_command = ""
    assert module.report_is_reusable(normalized, config, 17) is False


def test_parallel_runner_reuses_completed_eval_reports():
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

    assert module.report_is_reusable(summary, config, 7) is True

    summary["workflow"] = "validation-council-solve"
    assert module.report_is_reusable(summary, config, 7) is False


def test_remote_generation_identity_tracks_openhands_command_hash():
    assert (
        'identity["openhands_command_sha256"] = openhands_command_sha256'
        in runner.REMOTE_RUNNER
    )


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
            session_prefix="",
            host="host",
            ssh_command="ssh",
            remote_root="/remote-root",
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
