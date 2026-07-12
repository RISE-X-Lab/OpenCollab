from __future__ import annotations

import inspect
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from opencollab.harness import swe_v1_remote_commands as remote_commands
from opencollab.harness import swe_v1_remote_eval_script as remote_eval_script
from opencollab.harness import swe_v1_remote_evaluation as remote_evaluation
from opencollab.harness import swe_v1_remote_generation as remote_generation
from opencollab.harness import swe_v1_remote_records as remote_records
from opencollab.harness import swe_v1_remote_state as remote_state
from opencollab.harness import swe_v1_remote_target_proof as remote_target_proof

import scripts.swe_v1_prolite_runner as runner

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
            return SimpleNamespace(pid=1234), ""
        raise AssertionError(forward)

    try:
        runner.remote_http_ok = fake_remote_http_ok
        runner.local_http_ok = lambda base_url: True
        runner.start_remote_proxy_tunnel = fake_start_remote_proxy_tunnel
        runner.time.sleep = lambda _seconds: None

        summary = runner.ensure_remote_proxy(
            ssh_command=["ssh"],
            host="eval-host",
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
            host="eval-host",
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
            host="eval-host",
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
        "llm_provider": "anthropic",
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
        "llm_provider": "anthropic",
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
        lambda proc: not terminated.append(proc.pid),
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
        image_repository="registry.example/swebench",
        proxy_env_file=Path("unused"),
        remote_root="/remote",
        base_run_dir="/remote/run",
        workflow="team-pro",
        workflow_env=[],
        openhands_command="",
        openhands_empty_patch_rejections=2,
        max_empty_patch_retries=1,
        model_name="model",
        llm_model="glm-5.2",
        llm_provider="anthropic",
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
    assert "scripts/container_process_guard.py" in runner.SYNC_FILES
    assert "swebench/container_quiescence.py" in runner.SYNC_FILES
    assert "swebench/gen_prediction_patch.py" in runner.SYNC_FILES
    assert "swebench/gen_prediction_snapshot_config.py" in runner.SYNC_FILES
    assert "swebench/openhands_process_supervisor.py" in runner.SYNC_FILES


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
    config = _complete_remote_config(
        {
            "token": "x",
            "remote_root": "/tmp/remote",
            "remote_repo": "/tmp/repo",
            "base_run_dir": "/tmp/run",
            "workflow": "validation-council-solve",
            "model_name": "model",
            "session_prefix": "session",
            "remote_proxy_base_url": "http://127.0.0.1:1",
            "start_index": 1,
            "limit": 1,
            "budget": 1,
            "max_steps": 1,
            "swe_timeout": 1,
            "task_wall_timeout": 1,
            "eval_timeout": 1,
            "llm_timeout": 1,
            "checkpoint_interval": 0,
            "max_task_starts": 1,
            "max_eval_attempts": 99,
            "dry_run": True,
        }
    )
    remote_state.configure(config)
    assert remote_state.max_eval_attempts == 2
    source = inspect.getsource(remote_evaluation.eval_for_task)
    assert 'retry_statuses = {"technical_eval_failed", "blocked_missing_eval_image"}' in source


def test_remote_runner_prepares_optional_redis_before_eval_tests():
    script = remote_commands.prolite_service_bootstrap(
        {"repo": "NodeBB/NodeBB"}
    )
    assert "redis-server --daemonize yes --bind 127.0.0.1 --port 6379" in script
    assert "redis ready on 127.0.0.1:6379" in script


def test_remote_runner_does_not_count_non_executed_eval_states():
    source = inspect.getsource(remote_evaluation.eval_for_task)
    assert 'final["attempt_count"] = eval_attempt_count(run_dir, prediction, task)' in source
    once_source = inspect.getsource(remote_evaluation.eval_for_task_once)
    assert '"executed": False' in once_source


def test_remote_runner_classifies_completed_empty_patch_as_solver_result():
    result = remote_records.empty_patch_result(
        "task",
        {"instance_id": "task", "record_id": "r1", "model_patch": ""},
        {"workflow_status": "empty_patch_after_done"},
        "record_id",
    )
    assert result["status"] == "empty_patch"
    source = inspect.getsource(remote_generation.generation_for_task)
    assert '"phase": "empty_patch_retry"' in source
    assert "generation_identity_matches" in inspect.getsource(
        remote_generation.generation_for_task_once
    )
