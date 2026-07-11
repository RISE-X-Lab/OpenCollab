from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from opencollab.harness.swe_eval_records import read_jsonl

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

gp = pytest.importorskip("gen_prediction")


def _stage_pending_output(tmp_path, *, record_id="record-1"):
    gp.write_container_marker(tmp_path, "cid", "name")
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id=record_id,
    )
    predictions_path = tmp_path / "predictions.jsonl"
    metrics_path = tmp_path / "metrics.jsonl"
    pending = gp.persist_pending_output(
        run_dir=tmp_path,
        predictions_path=predictions_path,
        metrics_path=metrics_path,
        prediction=prediction,
        metric=metric,
        cid="cid",
        name="name",
    )
    return pending, predictions_path, metrics_path, prediction, metric


def _make_owner_stale(tmp_path):
    path = gp.container_owner_path(tmp_path, "name")
    owner = json.loads(path.read_text(encoding="utf-8"))
    owner["owner_pid"] = 2**30
    owner["owner_start_identity"] = "proc:dead"
    gp._atomic_write_bytes(path, gp._encode_owner(owner))


def _jsonl_rows(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def test_relative_output_targets_remain_stable_during_pending_replay(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    predictions, metrics_path = gp.output_paths(
        "results/predictions.jsonl",
        "results/metrics.jsonl",
    )
    predictions.parent.mkdir()
    gp.write_container_marker(tmp_path, "cid", "name")
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id="record-relative",
    )
    pending = gp.persist_pending_output(
        run_dir=tmp_path,
        predictions_path=predictions,
        metrics_path=metrics_path,
        prediction=prediction,
        metric=metric,
        cid="cid",
        name="name",
    )
    _make_owner_stale(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )

    assert gp.recover_generation_state(tmp_path) is True
    assert _jsonl_rows(predictions) == [prediction]
    assert _jsonl_rows(metrics_path) == [metric]
    assert not pending.exists()


def test_cleanup_failure_retains_pending_candidate_and_owner(monkeypatch, tmp_path):
    pending, predictions, metrics_path, _prediction, _metric = _stage_pending_output(
        tmp_path
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: False
    )

    with pytest.raises(RuntimeError, match="technical container cleanup failed"):
        gp.finalize_container_ownership(
            run_dir=tmp_path,
            cid="cid",
            name="name",
            keep_container=False,
            completed=True,
            metrics={},
        )

    assert pending.exists()
    assert gp.container_owner_path(tmp_path, "name").exists()
    assert not predictions.exists()
    assert not metrics_path.exists()


def test_single_main_cleanup_failure_stages_candidate_before_publish(
    monkeypatch,
    tmp_path,
):
    instance_path = tmp_path / "instance.json"
    instance_path.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "repo": "acme/repo",
                "problem_statement": "fix it",
                "FAIL_TO_PASS": "[]",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "predictions.jsonl"
    monkeypatch.setattr(
        gp,
        "get_config",
        lambda root: {
            "model": "model",
            "provider": "provider",
            "api_key": "key",
            "base_url": "http://local",
        },
    )
    monkeypatch.setattr(
        gp, "start_container", lambda image, name, owner_token: "cid"
    )

    async def fake_run_agent(*args, **kwargs):
        return {
            "workflow_status": "done",
            "execution_quiesced": True,
            "submission_eligible": True,
        }

    monkeypatch.setattr(gp, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gp, "run_with_bounded_shutdown", lambda awaitable: asyncio.run(awaitable)
    )
    monkeypatch.setattr(
        gp,
        "extract_patch",
        lambda cid: "diff --git a/src/a.py b/src/a.py\n+fixed\n",
    )
    monkeypatch.setattr(
        gp, "remove_container_and_clear_marker", lambda run_dir, cid: False
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction.py",
            "--instance-file",
            str(instance_path),
            "--output",
            str(output),
        ],
    )

    with pytest.raises(RuntimeError, match="technical container cleanup failed"):
        gp.main()

    assert not output.exists()
    assert list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    assert list((tmp_path / ".opencollab" / "container_owners").glob("*.json"))


def test_single_main_output_symlink_race_cleans_active_container(
    monkeypatch,
    tmp_path,
):
    instance_path = tmp_path / "instance.json"
    instance_path.write_text(
        json.dumps(
            {
                "instance_id": "task-1",
                "repo": "acme/repo",
                "problem_statement": "fix it",
                "FAIL_TO_PASS": "[]",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "predictions.jsonl"
    victim = tmp_path / "victim.jsonl"
    victim.write_text("unchanged\n", encoding="utf-8")
    monkeypatch.setattr(
        gp,
        "get_config",
        lambda root: {
            "model": "model",
            "provider": "provider",
            "api_key": "key",
            "base_url": "http://local",
        },
    )
    monkeypatch.setattr(
        gp, "start_container", lambda image, name, owner_token: "cid"
    )

    async def fake_run_agent(*args, **kwargs):
        return {
            "workflow_status": "done",
            "execution_quiesced": True,
            "submission_eligible": True,
        }

    monkeypatch.setattr(gp, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gp,
        "run_with_bounded_shutdown",
        lambda awaitable: asyncio.run(awaitable),
    )

    def race_output_before_staging(_cid):
        output.symlink_to(victim)
        return "diff --git a/src/a.py b/src/a.py\n+fixed\n"

    monkeypatch.setattr(gp, "extract_patch", race_output_before_staging)
    removed = []

    def cleanup_owned_container(run_dir, cid):
        removed.append(cid)
        gp.clear_container_marker(run_dir, cid)
        return True

    monkeypatch.setattr(
        gp,
        "remove_container_and_clear_marker",
        cleanup_owned_container,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction.py",
            "--instance-file",
            str(instance_path),
            "--output",
            str(output),
        ],
    )

    with pytest.raises(ValueError, match="regular file or absent"):
        gp.main()

    assert removed == ["cid"]
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert not list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    assert not list((tmp_path / ".opencollab" / "container_owners").glob("*.json"))


def test_next_start_recovers_owner_and_publishes_pending_once(monkeypatch, tmp_path):
    pending, predictions, metrics_path, prediction, metric = _stage_pending_output(
        tmp_path
    )
    _make_owner_stale(tmp_path)
    removed = []
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    assert gp.recover_generation_state(tmp_path) is True
    assert gp.recover_generation_state(tmp_path) is True

    assert removed == ["cid"]
    assert _jsonl_rows(predictions) == [prediction]
    assert _jsonl_rows(metrics_path) == [metric]
    assert not pending.exists()
    assert not gp.container_owner_path(tmp_path, "name").exists()


def test_pending_publish_retains_candidate_when_output_path_is_replaced(
    monkeypatch,
    tmp_path,
):
    pending, predictions, metrics_path, _prediction, _metric = _stage_pending_output(
        tmp_path
    )
    detached = tmp_path / "detached-predictions.jsonl"
    original_fsync_directory = gp._fsync_directory
    replaced = False

    monkeypatch.setattr(gp, "_pending_owner_state", lambda *_args: "kept")

    def replace_prediction_after_sync(directory):
        nonlocal replaced
        original_fsync_directory(directory)
        if not replaced and predictions.exists():
            replaced = True
            predictions.rename(detached)
            predictions.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(gp, "_fsync_directory", replace_prediction_after_sync)

    with pytest.raises(OSError, match="output path changed"):
        gp.publish_pending_output(tmp_path, pending)

    assert pending.exists()
    assert predictions.read_text(encoding="utf-8") == "foreign\n"
    assert detached.exists()
    assert not metrics_path.exists()


def test_pending_replay_fills_metrics_after_prediction_only_crash(monkeypatch, tmp_path):
    pending, predictions, metrics_path, prediction, metric = _stage_pending_output(
        tmp_path
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    assert gp._append_jsonl_durable_once(predictions, prediction) is True

    assert gp.recover_generation_state(tmp_path) is True

    assert _jsonl_rows(predictions) == [prediction]
    assert _jsonl_rows(metrics_path) == [metric]
    assert not pending.exists()


def test_pending_replay_removes_marker_after_both_outputs_exist(monkeypatch, tmp_path):
    pending, predictions, metrics_path, prediction, metric = _stage_pending_output(
        tmp_path
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    assert gp._append_jsonl_durable_once(predictions, prediction) is True
    assert gp._append_jsonl_durable_once(metrics_path, metric) is True

    assert gp.recover_generation_state(tmp_path) is True

    assert _jsonl_rows(predictions) == [prediction]
    assert _jsonl_rows(metrics_path) == [metric]
    assert not pending.exists()


def test_pending_replay_separates_truncated_jsonl_tail(monkeypatch, tmp_path):
    pending, predictions, metrics_path, prediction, metric = _stage_pending_output(
        tmp_path
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    predictions.write_bytes(b'{"instance_id":"truncated"')

    assert gp.recover_generation_state(tmp_path) is True

    assert read_jsonl(predictions) == [prediction]
    assert read_jsonl(metrics_path) == [metric]
    assert not pending.exists()


def test_pending_replay_retains_candidate_for_malformed_complete_jsonl_record(
    monkeypatch,
    tmp_path,
):
    pending, predictions, metrics_path, _prediction, _metric = (
        _stage_pending_output(tmp_path)
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    malformed = b'{"instance_id":\n'
    predictions.write_bytes(malformed)

    assert gp.recover_generation_state(tmp_path) is False

    assert predictions.read_bytes() == malformed
    assert not metrics_path.exists()
    assert pending.exists()


def test_pending_survives_unknown_output_fsync(monkeypatch, tmp_path):
    pending, predictions, _metrics_path, _prediction, _metric = _stage_pending_output(
        tmp_path
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    original_fsync = gp.os.fsync

    def fail_prediction_fsync(fd):
        if predictions.exists() and os.fstat(fd).st_ino == predictions.stat().st_ino:
            raise OSError("fsync unknown")
        return original_fsync(fd)

    monkeypatch.setattr(gp.os, "fsync", fail_prediction_fsync)

    with pytest.raises(OSError, match="fsync unknown"):
        gp.publish_pending_output(tmp_path, pending)

    assert pending.exists()


def test_pending_survives_unknown_output_directory_fsync(monkeypatch, tmp_path):
    pending, _predictions, _metrics_path, _prediction, _metric = (
        _stage_pending_output(tmp_path)
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    original_fsync_directory = gp._fsync_directory

    def fail_output_directory_fsync(path):
        if path == tmp_path:
            raise OSError("directory fsync unknown")
        return original_fsync_directory(path)

    monkeypatch.setattr(gp, "_fsync_directory", fail_output_directory_fsync)

    with pytest.raises(OSError, match="directory fsync unknown"):
        gp.publish_pending_output(tmp_path, pending)

    assert pending.exists()


def test_staging_failure_marks_owner_for_manual_preservation(monkeypatch, tmp_path):
    gp.write_container_marker(tmp_path, "cid", "name")
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id="record-1",
    )
    monkeypatch.setattr(gp, "MAX_PENDING_OUTPUT_BYTES", 64)

    with pytest.raises(ValueError, match="byte limit"):
        gp.persist_pending_output(
            run_dir=tmp_path,
            predictions_path=tmp_path / "predictions.jsonl",
            metrics_path=tmp_path / "metrics.jsonl",
            prediction=prediction,
            metric=metric,
            cid="cid",
            name="name",
        )

    owner_path = gp.container_owner_path(tmp_path, "name")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    assert owner["state"] == "preservation_required"
    assert not list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))
    owner["owner_pid"] = 2**30
    owner["owner_start_identity"] = "proc:dead"
    gp._atomic_write_bytes(owner_path, gp._encode_owner(owner))
    removed = []
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    assert gp.recover_generation_state(tmp_path) is False
    assert removed == []
    assert owner_path.exists()


def test_recovery_promotes_durable_candidate_after_owner_upgrade_crash(
    monkeypatch,
    tmp_path,
):
    gp.write_container_marker(tmp_path, "cid", "name")
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction, metric = gp.build_output_records(
        instance_id="task-1",
        model_name="model",
        patch=patch,
        metrics={"workflow_status": "done"},
        record_id="record-1",
    )
    original_replace_owner = gp._replace_owner

    def crash_candidate_upgrade(path, previous, updated):
        if updated.get("state") == "candidate_staged":
            raise OSError("owner upgrade crashed")
        return original_replace_owner(path, previous, updated)

    monkeypatch.setattr(gp, "_replace_owner", crash_candidate_upgrade)
    with pytest.raises(OSError, match="owner upgrade crashed"):
        gp.persist_pending_output(
            run_dir=tmp_path,
            predictions_path=tmp_path / "predictions.jsonl",
            metrics_path=tmp_path / "metrics.jsonl",
            prediction=prediction,
            metric=metric,
            cid="cid",
            name="name",
        )
    monkeypatch.setattr(gp, "_replace_owner", original_replace_owner)
    owner_path = gp.container_owner_path(tmp_path, "name")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    assert owner["state"] == "preservation_required"
    owner["owner_pid"] = 2**30
    owner["owner_start_identity"] = "proc:dead"
    gp._atomic_write_bytes(owner_path, gp._encode_owner(owner))
    removed = []
    monkeypatch.setattr(
        gp,
        "_remove_labeled_container",
        lambda reference, owner_token, **kwargs: removed.append(reference) or True,
    )

    assert gp.recover_generation_state(tmp_path) is True
    assert gp.recover_generation_state(tmp_path) is True

    assert removed == ["cid"]
    assert _jsonl_rows(tmp_path / "predictions.jsonl") == [prediction]
    assert _jsonl_rows(tmp_path / "metrics.jsonl") == [metric]
    assert not owner_path.exists()
    assert not list((tmp_path / ".opencollab" / "pending_outputs").glob("*.json"))


def test_publish_pending_rejects_symlink_without_following_it(monkeypatch, tmp_path):
    pending, _predictions, _metrics_path, _prediction, _metric = (
        _stage_pending_output(tmp_path)
    )
    monkeypatch.setattr(
        gp, "_remove_labeled_container", lambda *args, **kwargs: True
    )
    assert gp.remove_container_and_clear_marker(tmp_path, "cid") is True
    target = tmp_path / "attacker.json"
    target.write_text("{}", encoding="utf-8")
    pending.unlink()
    pending.symlink_to(target)

    with pytest.raises(OSError):
        gp.publish_pending_output(tmp_path, pending)

    assert target.read_text(encoding="utf-8") == "{}"


def test_two_processes_recover_one_stale_pending_exactly_once(tmp_path):
    pending, predictions, metrics_path, prediction, metric = _stage_pending_output(
        tmp_path
    )
    _make_owner_stale(tmp_path)
    owner_path = gp.container_owner_path(tmp_path, "name")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["state"] = "preservation_required"
    gp._atomic_write_bytes(owner_path, gp._encode_owner(owner))
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = rm ]; then exit 0; fi\n"
        "if [ \"$1\" = inspect ]; then echo 'Error: No such object' >&2; exit 1; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    script = (
        "import pathlib,sys; "
        f"sys.path.insert(0, {str(_SWEBENCH_DIR)!r}); "
        "import gen_prediction as gp; "
        f"raise SystemExit(0 if gp.recover_generation_state(pathlib.Path({str(tmp_path)!r})) else 1)"
    )
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env.get('PATH', '')}"
    first = subprocess.Popen([sys.executable, "-c", script], env=env)
    second = subprocess.Popen([sys.executable, "-c", script], env=env)

    assert first.wait(timeout=10) == 0
    assert second.wait(timeout=10) == 0

    assert _jsonl_rows(predictions) == [prediction]
    assert _jsonl_rows(metrics_path) == [metric]
    assert not pending.exists()
    assert not owner_path.exists()
