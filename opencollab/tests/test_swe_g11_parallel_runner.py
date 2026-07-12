from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "swe_g11_parallel_runner.py"
    spec = importlib.util.spec_from_file_location("swe_g11_parallel_runner", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    values = {
        "start_index": 51,
        "end_index": 75,
        "indices": "",
        "max_workers": 5,
        "min_workers": 1,
        "adaptive_recovery_tasks": 2,
        "run_id": "swe_g11_prolite51_75_test",
        "output_dir": Path("/tmp/swe_g11_prolite51_75_test"),
        "remote_base": "",
        "remote_eval_work_root": "/remote/eval_work",
        "remote_runtime_repo": "",
        "model_name": "model",
        "llm_model": "glm-5.2",
        "context_window": 400_000,
        "temperature": 1.0,
        "top_p": 1.0,
        "max_output_tokens": 32_768,
        "session_prefix": "",
        "host": "host",
        "ssh_command": "ssh",
        "remote_root": "/remote/root",
        "workflow": "validation-council-solve",
        "workflow_env": [],
        "openhands_command": "",
        "max_empty_patch_retries": 1,
        "remote_proxy_base_url": "http://127.0.0.1:18788",
        "local_proxy_base_url": "http://127.0.0.1:8878",
        "proxy_env_file": Path("/tmp/glm52.env"),
        "budget": 16,
        "max_steps": 60,
        "swe_timeout": 14400,
        "task_wall_timeout": 15300,
        "eval_timeout": 7200,
        "llm_timeout": 900,
        "checkpoint_interval": 300,
        "max_task_starts": 1,
        "max_eval_attempts": 9,
        "total_timeout": 240000,
        "runner_attempts": 3,
        "retry_delay_seconds": 60,
        "usd_cny": 6.76,
        "no_sync_runtime": False,
        "no_ensure_remote_proxy": False,
        "skip_preflight": False,
        "skip_health_checks": False,
        "no_adaptive_concurrency": False,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parallel_config_uses_requested_range_and_worker_count():
    module = _load_module()

    config = module.resolve_config(_args())

    assert config.indices == tuple(range(51, 76))
    assert config.max_workers == 5
    assert config.min_workers == 1
    assert config.adaptive_concurrency is True
    assert config.adaptive_recovery_tasks == 2
    assert config.run_id == "swe_g11_prolite51_75_test"
    assert config.remote_base == "/remote/eval_work/swe_g11_prolite51_75_test"
    assert config.remote_runtime_repo == "/remote/eval_work/swe_g11_prolite51_75_test/_runtime/repo"
    assert config.output_dir == Path("/tmp/swe_g11_prolite51_75_test")
    assert config.max_eval_attempts == 2
    assert config.max_empty_patch_retries == 1
    assert config.workflow_env == ()
    assert config.llm_model == "glm-5.2"
    assert config.context_window == 400_000


def test_parser_defaults_to_g11_three_task_starts():
    module = _load_module()

    args = module.build_parser().parse_args(["--start-index", "51", "--end-index", "75"])
    config = module.resolve_config(args)

    assert config.indices == tuple(range(51, 76))
    assert config.max_workers == 5
    assert config.max_task_starts == 3
    command = module.task_command(config, 51)
    assert command[command.index("--max-task-starts") + 1] == "3"


def test_task_starts_are_clamped_to_one_through_three():
    module = _load_module()

    assert module.resolve_config(_args(max_task_starts=9)).max_task_starts == 3
    assert module.resolve_config(_args(max_task_starts=0)).max_task_starts == 1


def test_empty_patch_retries_are_clamped_to_zero_or_one():
    module = _load_module()

    assert module.resolve_config(_args(max_empty_patch_retries=-1)).max_empty_patch_retries == 0
    assert module.resolve_config(_args(max_empty_patch_retries=9)).max_empty_patch_retries == 1


def test_parser_accepts_compact_sparse_ranges():
    module = _load_module()

    config = module.resolve_config(_args(indices="1-3,7,10-12", start_index=None, end_index=None))

    assert config.indices == (1, 2, 3, 7, 10, 11, 12)
    assert module.range_label(config.indices) == "1-3,7,10-12"


def test_workflow_env_is_validated_and_forwarded():
    module = _load_module()
    config = module.resolve_config(
        _args(workflow_env=["OPENCOLLAB_TEMPERATURE=1", "OPENCOLLAB_TOP_P=1"])
    )

    command = module.task_command(config, 51)

    assert config.workflow_env == ("OPENCOLLAB_TEMPERATURE=1", "OPENCOLLAB_TOP_P=1")
    assert command.count("--workflow-env") == 2
    assert "OPENCOLLAB_TEMPERATURE=1" in command
    assert "OPENCOLLAB_TOP_P=1" in command


def test_task_command_forwards_typed_llm_settings():
    module = _load_module()
    config = module.resolve_config(_args())

    command = module.task_command(config, 51)

    assert command[command.index("--llm-model") + 1] == "glm-5.2"
    assert command[command.index("--context-window") + 1] == "400000"
    assert command[command.index("--temperature") + 1] == "1.0"
    assert command[command.index("--top-p") + 1] == "1.0"
    assert command[command.index("--max-output-tokens") + 1] == "32768"


def test_workflow_env_rejects_secret_or_arbitrary_keys():
    module = _load_module()

    with pytest.raises(ValueError, match="unsupported --workflow-env"):
        module.resolve_config(_args(workflow_env=["OPENCOLLAB_API_KEY=secret"]))


def test_preflight_forwards_budget_and_step_limit(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            output_dir=tmp_path,
            budget=4_000_000,
            max_steps=60,
            workflow_env=["OPENCOLLAB_MAX_OUTPUT_TOKENS=32768"],
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        report = {
            "status": "dry_run",
            "workflow": config.workflow,
            "workflow_env": {"OPENCOLLAB_MAX_OUTPUT_TOKENS": "32768"},
            "budget": 4_000_000,
            "max_steps": 60,
        }
        (tmp_path / "shared_runtime_preflight.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        return module.subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    original = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        module.prepare_runtime(config)
    finally:
        module.subprocess.run = original

    command = captured["command"]
    assert command[command.index("--budget") + 1] == "4000000"
    assert command[command.index("--max-steps") + 1] == "60"
    assert "OPENCOLLAB_MAX_OUTPUT_TOKENS=32768" in command


def test_task_command_is_built_from_requested_index():
    module = _load_module()
    config = module.resolve_config(_args(no_sync_runtime=True, no_ensure_remote_proxy=True))

    command = module.task_command(config, 75)
    joined = " ".join(command)

    assert "--start-index" in command
    assert command[command.index("--start-index") + 1] == "75"
    assert command[command.index("--limit") + 1] == "1"
    assert command[command.index("--base-run-dir") + 1] == "/remote/eval_work/swe_g11_prolite51_75_test/task_75"
    assert command[command.index("--json-output") + 1] == "/tmp/swe_g11_prolite51_75_test/task_75_report.json"
    assert command[command.index("--max-task-starts") + 1] == "1"
    assert "--no-sync-runtime" in command
    assert "--no-ensure-remote-proxy" in command
    assert "39_50" not in joined
    assert "36_50" not in joined


def test_task_command_forwards_openhands_command():
    module = _load_module()
    config = module.resolve_config(
        _args(
            workflow="openhands-external",
            openhands_command="openhands --prompt-file {prompt_file}",
        )
    )

    command = module.task_command(config, 51)

    assert "--openhands-command" in command
    assert command[command.index("--openhands-command") + 1] == "openhands --prompt-file {prompt_file}"
    assert command[command.index("--openhands-empty-patch-rejections") + 1] == "2"


def test_openhands_command_is_not_read_directly_from_environment(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("OPENCOLLAB_OPENHANDS_COMMAND", "hidden-command")

    args = module.build_parser().parse_args(["--indices", "1"])

    assert args.openhands_command == ""


def test_openhands_completed_report_reuse_requires_same_command():
    module = _load_module()
    command = "openhands --headless --file {prompt_file} --override-with-envs"
    config = module.resolve_config(
        _args(
            start_index=1,
            end_index=1,
            workflow="openhands-external",
            openhands_command=command,
            remote_base="/remote/openhands",
        )
    )
    summary = {
        "status": "done",
        "workflow": "openhands-external",
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
        "base_run_dir": "/remote/openhands/task_1",
        "openhands_command_sha256": module._openhands_command_sha256(command),
        "counts": {
            "tasks": 1,
            "generation_done": 1,
            "eval_done": 1,
            "technical_failed": 0,
        },
        "rows": [
            {
                "index": 1,
                "task": "task-1",
                "generation": {
                    "status": "generation_done",
                    "solver_git_snapshot": {
                        "enabled": True,
                        "anonymous_head": "a" * 40,
                        "base_tree": "b" * 40,
                        "commit_count": 1,
                        "remote_count": 0,
                        "extra_git_metadata": 0,
                        "removed_git_metadata": 0,
                    },
                },
                "eval": {"status": "eval_done"},
            }
        ],
    }

    assert module.report_is_reusable(summary, config, 1) is True
    without_snapshot = json.loads(json.dumps(summary))
    del without_snapshot["rows"][0]["generation"]["solver_git_snapshot"]
    assert module.report_is_reusable(without_snapshot, config, 1) is False
    changed = module.resolve_config(
        _args(
            start_index=1,
            end_index=1,
            workflow="openhands-external",
            openhands_command="openhands --version",
            remote_base="/remote/openhands",
        )
    )
    assert module.report_is_reusable(summary, changed, 1) is False
    missing = module.resolve_config(
        _args(
            start_index=1,
            end_index=1,
            workflow="openhands-external",
            openhands_command="",
            remote_base="/remote/openhands",
        )
    )
    assert module.report_is_reusable(summary, missing, 1) is False


def test_aggregate_uses_configured_indices_for_done_status():
    module = _load_module()
    config = module.resolve_config(_args(indices="51,53", start_index=None, end_index=None))
    results = [
        {"index": 51, "completed": True, "returncode": 0, "tasks": 1, "generation_done": 1, "eval_done": 1},
        {"index": 53, "completed": True, "returncode": 1, "tasks": 1, "generation_done": 1, "eval_done": 1},
    ]

    summary = module.aggregate(config, results)

    assert summary["status"] == "done"
    assert summary["range"] == "51,53"
    assert summary["indices"] == [51, 53]
    assert summary["counts"]["tasks"] == 2
    assert summary["workflow"] == "validation-council-solve"
    assert summary["workflow_env"] == []
    assert summary["llm_model"] == "glm-5.2"
    assert summary["context_window"] == 400_000
    assert summary["temperature"] == 1.0
    assert summary["top_p"] == 1.0
    assert summary["max_output_tokens"] == 32_768
    assert summary["budget"] == 16
    assert summary["max_steps"] == 60


def test_aggregate_marks_completed_technical_failures_explicitly():
    module = _load_module()
    config = module.resolve_config(_args(indices="51", start_index=None, end_index=None))
    results = [
        {
            "index": 51,
            "completed": True,
            "returncode": 1,
            "tasks": 0,
            "generation_done": 0,
            "eval_done": 0,
            "technical_failed": 1,
            "runner_status": "preflight_failed",
        }
    ]

    summary = module.aggregate(config, results)

    assert summary["status"] == "done_with_technical_failures"
    assert summary["counts"]["technical_failed"] == 1


def test_run_one_retries_transient_preflight_report(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="51",
            start_index=None,
            end_index=None,
            output_dir=tmp_path,
            runner_attempts=3,
            retry_delay_seconds=0,
        )
    )
    report_path = module.task_paths(config, 51)["json_report"]
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = {
                "status": "preflight_failed",
                "counts": {"tasks": 0, "technical_failed": 1},
                "rows": [],
            }
        else:
            payload = {
                "status": "done",
                "counts": {
                    "tasks": 1,
                    "generation_done": 1,
                    "eval_done": 1,
                    "resolved": 1,
                    "unresolved": 0,
                    "technical_failed": 0,
                },
                "rows": [{"index": 51}],
            }
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.run_one(config, 51)
    finally:
        module.subprocess.run = old_run

    assert calls == 2
    assert result["runner_status"] == "done"
    assert result["completed"] is True
    assert result["attempts"] == 2
    assert result["technical_failed"] == 0


def test_run_one_returns_last_preflight_failure_after_runner_attempts(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="51",
            start_index=None,
            end_index=None,
            output_dir=tmp_path,
            runner_attempts=3,
            retry_delay_seconds=0,
        )
    )
    report_path = module.task_paths(config, 51)["json_report"]
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        report_path.write_text(
            json.dumps(
                {
                    "status": "preflight_failed",
                    "counts": {"tasks": 0, "technical_failed": 1},
                    "rows": [],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.run_one(config, 51)
    finally:
        module.subprocess.run = old_run

    assert calls == 3
    assert result["runner_status"] == "preflight_failed"
    assert result["completed"] is True
    assert result["attempts"] == 3
    assert result["technical_failed"] == 1


def test_run_one_reuses_completed_report_without_subprocess(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            indices="51",
            start_index=None,
            end_index=None,
            output_dir=tmp_path,
        )
    )
    report_path = module.task_paths(config, 51)["json_report"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "status": "done",
                "workflow": config.workflow,
                "model_name": config.model_name,
                "llm_model": config.llm_model,
                "context_window": config.context_window,
                "temperature": config.temperature,
                "top_p": config.top_p,
                "max_output_tokens": config.max_output_tokens,
                "budget": config.budget,
                "max_steps": config.max_steps,
                "max_task_starts": config.max_task_starts,
                "max_empty_patch_retries": config.max_empty_patch_retries,
                "max_eval_attempts": config.max_eval_attempts,
                "workflow_env": {},
                "eval_only": False,
                "solver_attribution": "current_run",
                "remote_runtime_repo": config.remote_runtime_repo,
                "base_run_dir": f"{config.remote_base}/task_51",
                "counts": {
                    "tasks": 1,
                    "generation_done": 1,
                    "eval_done": 1,
                    "technical_failed": 0,
                },
                "rows": [
                    {
                        "index": 51,
                        "task": "task-51",
                        "generation": {"status": "generation_done"},
                        "eval": {"status": "eval_done"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    old_run = module.subprocess.run
    try:
        module.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("completed report must be reused")
        )
        result = module.run_one(config, 51)
    finally:
        module.subprocess.run = old_run

    assert result["runner_status"] == "done"
    assert result["reused_existing_report"] is True
    assert result["elapsed_seconds"] == 0.0


def test_scheduler_decreases_on_resource_failures_and_recovers_after_clean_tasks():
    module = _load_module()
    config = module.resolve_config(_args())
    state = module.SchedulerState(current_workers=5)

    module.update_scheduler_state(
        config,
        state,
        {"index": 51, "runner_status": "missing_report", "completed": False, "returncode": 2},
    )

    assert state.current_workers == 4
    assert state.clean_streak == 0
    assert state.events[-1]["action"] == "decrease"

    clean = {"index": 52, "runner_status": "done", "completed": True, "returncode": 0}
    module.update_scheduler_state(config, state, clean)
    assert state.current_workers == 4
    assert state.clean_streak == 1
    module.update_scheduler_state(config, state, {**clean, "index": 53})

    assert state.current_workers == 5
    assert state.clean_streak == 0
    assert state.events[-1]["action"] == "increase"


def test_scheduler_ignores_semantic_eval_failures():
    module = _load_module()
    config = module.resolve_config(_args())
    state = module.SchedulerState(current_workers=5)

    result = {
        "index": 54,
        "runner_status": "done",
        "completed": True,
        "returncode": 1,
        "technical_failed": 0,
        "rows": [
            {
                "generation": {"status": "generation_done"},
                "eval": {
                    "status": "eval_done",
                    "summary": {
                        "resolved": False,
                        "technical_reasons": [],
                        "command_log": "/nfsEDS/dongyh/data/kaka/docker/opencollab/eval_work/task/command.log",
                        "tests_status": {
                            "f2p_log_tail": "assertion failed: ssh timeout banner should stay visible",
                            "p2p_log_tail": "expected docker label text in rendered output",
                        },
                    },
                },
            }
        ],
    }

    assert module.result_resource_reasons(result) == []
    module.update_scheduler_state(config, state, result)
    assert state.current_workers == 5
    assert state.clean_streak == 1


def test_remote_health_check_builds_parameterized_ssh_probe(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(output_dir=tmp_path))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return module.subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        result = module.run_remote_health_checks(config)
    finally:
        module.subprocess.run = old_run

    assert result["status"] == "ok"
    assert calls
    joined = " ".join(calls[0])
    assert "swe_g11_prolite51_75_test" in joined
    assert "docker info" in joined
    assert "test -d" in joined


def test_remote_health_check_skips_without_ssh_for_dry_run_or_explicit_skip(tmp_path):
    module = _load_module()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise AssertionError("ssh should not be called")

    old_run = module.subprocess.run
    try:
        module.subprocess.run = fake_run
        dry_config = module.resolve_config(_args(output_dir=tmp_path / "dry", dry_run=True))
        dry_config.output_dir.mkdir(parents=True)
        dry_result = module.run_remote_health_checks(dry_config)
        skip_config = module.resolve_config(_args(output_dir=tmp_path / "skip", skip_health_checks=True))
        skip_config.output_dir.mkdir(parents=True)
        skip_result = module.run_remote_health_checks(skip_config)
    finally:
        module.subprocess.run = old_run

    assert calls == []
    assert dry_result == {"status": "skipped", "reason": "dry_run"}
    assert skip_result == {"status": "skipped", "reason": "disabled"}


def test_run_parallel_stops_before_generation_when_health_check_fails(tmp_path):
    module = _load_module()
    config = module.resolve_config(_args(start_index=51, end_index=52, output_dir=tmp_path))
    called = {"run_one": 0}

    def fake_run_one(*args, **kwargs):
        called["run_one"] += 1
        return {}

    old_prepare = module.prepare_runtime
    old_health = module.run_remote_health_checks
    old_run_one = module.run_one
    try:
        module.prepare_runtime = lambda cfg: None
        module.run_remote_health_checks = lambda cfg: (_ for _ in ()).throw(RuntimeError("remote health check failed"))
        module.run_one = fake_run_one
        with pytest.raises(RuntimeError, match="remote health check failed"):
            module.run_parallel(config)
    finally:
        module.prepare_runtime = old_prepare
        module.run_remote_health_checks = old_health
        module.run_one = old_run_one

    assert called["run_one"] == 0


def test_run_parallel_submits_only_the_current_worker_window(tmp_path):
    module = _load_module()
    config = module.resolve_config(
        _args(
            start_index=51,
            end_index=55,
            max_workers=3,
            output_dir=tmp_path,
            no_sync_runtime=True,
            no_ensure_remote_proxy=True,
            skip_preflight=True,
            skip_health_checks=True,
            retry_delay_seconds=0,
        )
    )
    started: list[int] = []
    active = 0
    max_active = 0
    lock = threading.Lock()
    first_window_started = threading.Event()
    release = threading.Event()

    def fake_run_one(cfg, index):
        nonlocal active, max_active
        with lock:
            started.append(index)
            active += 1
            max_active = max(max_active, active)
            if len(started) == 3:
                first_window_started.set()
        release.wait(timeout=5)
        with lock:
            active -= 1
        return {
            "index": index,
            "returncode": 0,
            "runner_status": "done",
            "tasks": 1,
            "generation_done": 1,
            "eval_done": 1,
            "completed": True,
        }

    final: dict[str, object] = {}
    errors: list[BaseException] = []
    old_prepare = module.prepare_runtime
    old_health = module.run_remote_health_checks
    old_run_one = module.run_one
    old_token = module.build_token_summary
    old_fact = module.build_eval_fact_report
    try:
        module.prepare_runtime = lambda cfg: None
        module.run_remote_health_checks = lambda cfg: {"status": "skipped"}
        module.run_one = fake_run_one
        module.build_token_summary = lambda cfg: {"status": "done"}
        module.build_eval_fact_report = lambda cfg: {"status": "done", "counts": {}}

        def target():
            try:
                final.update(module.run_parallel(config))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=target)
        thread.start()
        assert first_window_started.wait(timeout=2)
        time.sleep(0.05)
        with lock:
            assert started == [51, 52, 53]
            assert max_active == 3
        release.set()
        thread.join(timeout=5)
    finally:
        release.set()
        module.prepare_runtime = old_prepare
        module.run_remote_health_checks = old_health
        module.run_one = old_run_one
        module.build_token_summary = old_token
        module.build_eval_fact_report = old_fact

    assert not errors
    assert final["status"] == "done"
    assert sorted(started) == [51, 52, 53, 54, 55]
