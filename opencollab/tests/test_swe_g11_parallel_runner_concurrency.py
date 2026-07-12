from __future__ import annotations

import threading
import time

from test_swe_g11_parallel_runner import _args, _load_module


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
                "empty_patch": 0,
                "eval_done": 1,
                "eval_attempts": 1,
                "eval_retry_tasks": 0,
                "resolved": 1,
                "unresolved": 0,
                "technical_failed": 0,
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
        module.build_eval_fact_report = lambda cfg: {
            "status": "done",
            "counts": {
                "tasks": 5,
                "eval_attempts": 5,
                "eval_retry_tasks": 0,
                "eval_success": 5,
                "empty_patch": 0,
                "resolved": 5,
                "unresolved": 0,
                "technical_failed_final": 0,
            },
        }

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
