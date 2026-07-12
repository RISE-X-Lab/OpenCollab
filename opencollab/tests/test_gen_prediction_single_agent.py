from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opencollab.domain.session import SessionPhase
from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_PROVEN,
    latest_paired_rows,
    metric_submission_integrity,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

gp = pytest.importorskip("gen_prediction")


def test_single_agent_output_records_share_identity():
    patch = "diff --git a/pkg/a.py b/pkg/a.py\n+fixed\n"

    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id="record-1",
    )

    assert prediction["record_id"] == "record-1"
    assert metric["record_id"] == "record-1"
    assert prediction["patch_sha256"] == gp._patch_sha256(patch)
    assert metric["patch_sha256"] == prediction["patch_sha256"]
    assert metric["workflow_status"] == "done"
    assert metric["runner_returncode"] == 0
    assert prediction["workflow_metric"]["runner_returncode"] == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [("done", 0), ("done_with_timeout_patch", 124), ("error", 1)],
)
def test_output_records_write_runner_returncode_for_status(status, expected):
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch="+fixed",
        metrics={"workflow_status": status},
    )

    assert metric["runner_returncode"] == expected
    assert prediction["workflow_metric"]["runner_returncode"] == expected


def test_current_single_agent_integrity_fields_are_proven():
    patch = "+fixed"
    metrics = {
        "workflow_status": "done",
        "execution_quiesced": True,
        "submission_eligible": True,
    }

    gp.complete_single_agent_integrity(
        metrics,
        patch=patch,
        patch_extraction_succeeded=True,
    )
    metrics["patch_produced"] = True

    assert metric_submission_integrity(metrics) != SUBMISSION_INTEGRITY_PROVEN


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("done", "empty_patch_after_done"),
        ("done_with_timeout_patch", "done_with_timeout_patch"),
    ],
)
def test_zero_byte_trusted_extraction_preserves_timeout_failure(status, expected):
    metrics = {"workflow_status": status, "submission_eligible": True}

    gp.normalize_trusted_extraction_status(metrics, "")

    assert metrics["workflow_status"] == expected
    assert metrics["submission_eligible"] is False


@pytest.mark.parametrize(
    ("status", "returncode"),
    [("done", 1), ("done_with_timeout_patch", 1), ("done_with_timeout_patch", 0)],
)
def test_output_records_reject_conflicting_runner_returncode(status, returncode):
    with pytest.raises(ValueError, match="conflicts with workflow_status"):
        gp.build_output_records(
            instance_id="task-1",
            model_name="model",
            patch="+fixed",
            metrics={"workflow_status": status, "runner_returncode": returncode},
        )


@pytest.mark.parametrize("returncode", [True, False, "0", 0.0, None])
def test_output_records_reject_invalid_existing_runner_returncode(returncode):
    with pytest.raises(ValueError, match="non-boolean integer"):
        gp.build_output_records(
            instance_id="task-1",
            model_name="model",
            patch="+fixed",
            metrics={"workflow_status": "done", "runner_returncode": returncode},
        )


def test_single_agent_cli_accepts_metrics_argument():
    script = Path(__file__).resolve().parents[2] / "swebench" / "gen_prediction.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "--metrics" in result.stdout


def _patch_run_agent_dependencies(monkeypatch, session):
    class Dummy:
        def __init__(self, *args, **kwargs):
            self.name = kwargs.get("name", "dummy")

        def close(self):
            return None

    monkeypatch.setattr(gp, "DockerEnvironment", Dummy)
    monkeypatch.setattr(gp, "Agent", Dummy)
    monkeypatch.setattr(gp, "Tracer", Dummy)
    monkeypatch.setattr(gp, "make_run_dir", lambda root: Path("/tmp/run"))
    monkeypatch.setattr(gp, "agent_save_path", lambda *args: "/tmp/session.json")
    monkeypatch.setattr(gp, "build_session", lambda **kwargs: session)


def _agent_config():
    return {
        "model": "model",
        "provider": "provider",
        "api_key": "key",
        "base_url": "http://local",
    }


def test_single_agent_reports_real_terminal_phase(monkeypatch):
    class FakeSession:
        phase = SessionPhase.BUDGET_EXCEEDED
        step_count = 4
        used_tokens = 100

        async def add_user_message(self, content):
            return None

        async def run_loop(self):
            return ""

    _patch_run_agent_dependencies(monkeypatch, FakeSession())

    metrics = asyncio.run(gp.run_agent("task", "cid", _agent_config(), 4, 100, 1))

    assert metrics["workflow_status"] == "budget_exceeded"
    assert metrics["session_phase"] == "budget_exceeded"
    assert metrics["wall_clock_timeout"] is False


def test_single_agent_does_not_relabel_provider_timeout(monkeypatch):
    class FakeSession:
        phase = SessionPhase.CALLING_LLM
        step_count = 1
        used_tokens = 10

        async def add_user_message(self, content):
            return None

        async def run_loop(self):
            raise asyncio.TimeoutError("provider timeout")

    _patch_run_agent_dependencies(monkeypatch, FakeSession())

    metrics = asyncio.run(gp.run_agent("task", "cid", _agent_config(), 4, 100, 1))

    assert metrics["workflow_status"] == "error"
    assert metrics["wall_clock_timeout"] is False
    assert metrics["error_type"] == "TimeoutError"
    assert metrics["error"] == "provider timeout"


def test_single_agent_marks_only_caller_deadline_as_wall_timeout(monkeypatch):
    class FakeSession:
        phase = SessionPhase.CALLING_LLM
        step_count = 1
        used_tokens = 10

        async def add_user_message(self, content):
            return None

        async def run_loop(self):
            await asyncio.Event().wait()

    _patch_run_agent_dependencies(monkeypatch, FakeSession())

    metrics = asyncio.run(gp.run_agent("task", "cid", _agent_config(), 4, 100, 0.01))

    assert metrics["workflow_status"] == "done_with_timeout_patch"
    assert metrics["wall_clock_timeout"] is True
    assert metrics["execution_quiesced"] is True
    assert metrics["submission_eligible"] is True


def test_single_agent_non_quiescent_timeout_revokes_env_and_rejects_patch(monkeypatch):
    release_late_write = asyncio.Event()
    late_attempt_finished = asyncio.Event()
    state = {"late_write": False, "late_write_rejected": False, "cancels": 0}

    class RevocableEnv:
        def __init__(self):
            self.aborted = False

        async def abort(self):
            self.aborted = True

    env = RevocableEnv()

    class FakeSession:
        phase = SessionPhase.CALLING_LLM
        step_count = 1
        used_tokens = 10

        async def add_user_message(self, content):
            return None

        async def run_loop(self):
            while not release_late_write.is_set():
                try:
                    await release_late_write.wait()
                except asyncio.CancelledError:
                    state["cancels"] += 1
            if env.aborted:
                state["late_write_rejected"] = True
            else:
                state["late_write"] = True
            late_attempt_finished.set()

    class Dummy:
        def __init__(self, *args, **kwargs):
            self.name = kwargs.get("name", "dummy")

        def close(self):
            return None

    monkeypatch.setattr(gp, "DockerEnvironment", lambda **_kwargs: env)
    monkeypatch.setattr(gp, "Agent", Dummy)
    monkeypatch.setattr(gp, "Tracer", Dummy)
    monkeypatch.setattr(gp, "make_run_dir", lambda root: Path("/tmp/run"))
    monkeypatch.setattr(gp, "agent_save_path", lambda *args: "/tmp/session.json")
    monkeypatch.setattr(gp, "build_session", lambda **kwargs: FakeSession())
    monkeypatch.setattr(gp, "AGENT_CANCELLATION_GRACE_SECONDS", 0.01)

    async def scenario():
        metrics = await gp.run_agent("task", "cid", _agent_config(), 4, 100, 0.05)
        assert metrics["execution_quiesced"] is False
        assert metrics["submission_eligible"] is False
        assert metrics["workflow_status"] == "error"
        assert env.aborted is True
        release_late_write.set()
        await asyncio.wait_for(late_attempt_finished.wait(), timeout=1)
        return metrics

    metrics = asyncio.run(scenario())

    assert state["cancels"] >= 2
    assert state["late_write"] is False
    assert state["late_write_rejected"] is True
    assert gp.metrics_have_completed_identity(metrics, "+candidate") is False


def test_single_agent_always_closes_tracer(monkeypatch):
    class FakeSession:
        phase = SessionPhase.DONE
        step_count = 1
        used_tokens = 10

        async def add_user_message(self, content):
            return None

        async def run_loop(self):
            return ""

    closed = []

    class ClosingTracer:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            closed.append(True)

    _patch_run_agent_dependencies(monkeypatch, FakeSession())
    monkeypatch.setattr(gp, "Tracer", ClosingTracer)

    metrics = asyncio.run(gp.run_agent("task", "cid", _agent_config(), 4, 100, 1))

    assert metrics["workflow_status"] == "done"
    assert closed == [True]


def test_single_agent_records_tracer_close_failure(monkeypatch):
    class FakeSession:
        phase = SessionPhase.DONE
        step_count = 1
        used_tokens = 10

        async def add_user_message(self, content):
            return None

        async def run_loop(self):
            return ""

    class FailingTracer:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            raise OSError("trace disk failure")

    _patch_run_agent_dependencies(monkeypatch, FakeSession())
    monkeypatch.setattr(gp, "Tracer", FailingTracer)

    metrics = asyncio.run(gp.run_agent("task", "cid", _agent_config(), 4, 100, 1))

    assert metrics["tracer_close_error_type"] == "OSError"
    assert metrics["tracer_close_error"] == "trace disk failure"


def test_docker_timeout_accepts_positive_float(monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", "2.5")

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(gp.subprocess, "run", fake_run)

    gp._docker("ps")

    assert captured["timeout"] == 2.5


@pytest.mark.parametrize("value", ["invalid", "0", "-1", "nan", "inf"])
def test_docker_timeout_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("OPENCOLLAB_DOCKER_TIMEOUT", value)

    with pytest.raises(ValueError, match="must be a positive number"):
        gp._docker("ps")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_steps", 0),
        ("max_steps", -1),
        ("max_steps", 1.5),
        ("max_steps", True),
        ("budget", 0),
        ("budget", -1),
        ("budget", "10"),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", float("nan")),
        ("timeout", float("inf")),
        ("timeout", True),
    ],
)
def test_generation_limits_reject_invalid_values(field, value):
    values = {"max_steps": 10, "budget": 100, "timeout": 5.0}
    values[field] = value

    with pytest.raises(ValueError, match=rf"--{field.replace('_', '-')}"):
        gp.validate_generation_limits(**values)


def test_generation_limits_normalize_valid_values():
    assert gp.validate_generation_limits(max_steps=4, budget=100, timeout="2.5") == (
        4,
        100,
        2.5,
    )


def test_default_metrics_path_matches_status_layout(tmp_path):
    output = tmp_path / "predictions.jsonl"

    assert gp.default_metrics_path(output) == tmp_path / "metrics.jsonl"


def test_output_paths_reject_same_file_before_generation(tmp_path):
    output = tmp_path / "records.jsonl"

    with pytest.raises(ValueError, match="must use different files"):
        gp.output_paths(output, output)

    assert output.exists() is False


def test_output_paths_reject_hard_links_to_same_inode(tmp_path):
    predictions = tmp_path / "predictions.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    predictions.write_text("", encoding="utf-8")
    os.link(predictions, metrics)

    with pytest.raises(ValueError, match="must use different files"):
        gp.output_paths(predictions, metrics)

    with pytest.raises(ValueError, match="must use different files"):
        gp.append_output_records(
            predictions,
            metrics,
            {"instance_id": "task", "model_patch": "+x"},
            {"instance_id": "task", "workflow_status": "done"},
        )

    assert predictions.read_text(encoding="utf-8") == ""


def test_output_paths_reject_symlink_before_generation(tmp_path):
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    output = tmp_path / "predictions.jsonl"
    output.symlink_to(victim)

    with pytest.raises(ValueError, match="regular file or absent"):
        gp.output_paths(output, tmp_path / "metrics.jsonl")

    assert victim.read_text(encoding="utf-8") == "unchanged\n"


def test_output_paths_return_absolute_targets_for_relative_arguments(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)

    predictions, metrics = gp.output_paths(
        Path("results/predictions.jsonl"),
        Path("results/metrics.jsonl"),
    )

    assert predictions == tmp_path / "results" / "predictions.jsonl"
    assert metrics == tmp_path / "results" / "metrics.jsonl"
    assert predictions.is_absolute()
    assert metrics.is_absolute()


def test_pending_staging_rejects_symlink_target_before_owner_state_change(
    tmp_path,
):
    gp.write_container_marker(tmp_path, "cid", "name")
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    predictions = tmp_path / "predictions.jsonl"
    predictions.symlink_to(victim)
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch="diff --git a/a b/a\n+fixed\n",
        metrics={"workflow_status": "done"},
        record_id="record-1",
    )

    with pytest.raises(ValueError, match="regular file or absent"):
        gp.persist_pending_output(
            run_dir=tmp_path,
            predictions_path=predictions,
            metrics_path=tmp_path / "metrics.jsonl",
            prediction=prediction,
            metric=metric,
            cid="cid",
            name="name",
        )

    owner = gp._read_owner(gp.container_owner_path(tmp_path, "name"))
    assert owner is not None and owner["state"] == "active"
    assert victim.read_text(encoding="utf-8") == "unchanged\n"


def test_cli_runner_bounds_shutdown_of_task_that_refuses_cancellation():
    async def scenario():
        async def stubborn_task():
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

        asyncio.create_task(stubborn_task())
        await asyncio.sleep(0)
        return "completed"

    started = time.monotonic()
    result = gp.run_with_bounded_shutdown(
        scenario(),
        shutdown_timeout=0.01,
    )

    assert result == "completed"
    assert time.monotonic() - started < 0.5


def test_cli_runner_does_not_run_task_spawning_shutdown_cleanup():
    child_cancelled = []

    async def scenario():
        async def stubborn_child():
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    child_cancelled.append(True)

        async def parent():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                asyncio.create_task(stubborn_child())

        asyncio.create_task(parent())
        await asyncio.sleep(0)
        return "completed"

    result = gp.run_with_bounded_shutdown(
        scenario(),
        shutdown_timeout=0.01,
    )

    assert result == "completed"
    assert child_cancelled == []


def test_output_commit_recovers_when_metrics_projection_crashes(monkeypatch, tmp_path):
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch="diff --git a/a b/a\n+x\n",
        metrics={"workflow_status": "done"},
        record_id="r1",
    )
    prediction_path = tmp_path / "predictions.jsonl"
    metrics_path = tmp_path / "metrics.jsonl"
    original_append = gp._append_jsonl_durable
    calls = []

    def crash_before_metric_projection(path, row):
        calls.append(path.name)
        if path == metrics_path:
            raise RuntimeError("simulated process crash")
        original_append(path, row)

    monkeypatch.setattr(gp, "_append_jsonl_durable", crash_before_metric_projection)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        gp.append_output_records(prediction_path, metrics_path, prediction, metric)

    assert calls == ["predictions.jsonl", "metrics.jsonl"]
    assert not metrics_path.exists()
    predictions = [json.loads(line) for line in prediction_path.read_text().splitlines()]
    pair = latest_paired_rows(predictions, [], "task-1")

    assert pair.status == "embedded_metric"
    assert pair.prediction["record_id"] == "r1"
    assert pair.metric == metric


def test_durable_append_separates_preexisting_truncated_tail(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_bytes(b'{"instance_id":"truncated"')
    row = {"instance_id": "task-1", "model_patch": "+fixed"}

    gp._append_jsonl_durable(path, row)

    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"instance_id":"truncated"',
        json.dumps(row),
    ]
    valid_rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            valid_rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    assert valid_rows == [row]
