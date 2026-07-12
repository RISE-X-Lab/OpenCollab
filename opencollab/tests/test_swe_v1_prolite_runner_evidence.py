from __future__ import annotations

from swe_v1_prolite_runner_test_support import (
    Path,
    _proven_submission_integrity,
    _remote_namespace,
    _seed_remote_completed_generation,
    _test_only_patch,
    _write_jsonl,
    json,
    pytest,
)


def test_remote_runner_rejects_invalid_slice_config(tmp_path):
    namespace = _remote_namespace(tmp_path, start_index=0, limit=0, max_task_starts=-1)

    errors = namespace["validate_runner_config"]()

    assert "start_index must be >= 1" in errors
    assert "limit must be > 0" in errors
    assert "max_task_starts must be >= 0" in errors


def test_remote_runner_rejects_excessive_slice_limit(tmp_path):
    namespace = _remote_namespace(tmp_path, limit=1001)

    assert "limit must be <= 1000" in namespace["validate_runner_config"]()


def test_remote_runner_allows_eval_only_mode_with_existing_generation(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=0, eval_only=True)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    _write_jsonl(
        run_dir / "predictions.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "model_patch": patch,
            }
        ],
    )
    _write_jsonl(
        run_dir / "metrics.jsonl",
        [
            {
                "instance_id": task,
                "record_id": "r1",
                "patch_sha256": patch_sha,
                "workflow_status": "done",
                "runner_returncode": 0,
            }
        ],
    )

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"


def test_remote_runner_eval_only_mode_does_not_start_missing_generation(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=0)

    result = namespace["generation_for_task"]({"instance_id": "task-1"})

    assert result["status"] == "generation_start_limit_reached"


def test_remote_runner_skips_eval_after_generation_cleanup_failure(tmp_path):
    namespace = _remote_namespace(tmp_path)
    namespace["remote_root"].mkdir(parents=True, exist_ok=True)
    namespace["remote_repo"].mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].parent.mkdir(parents=True, exist_ok=True)
    namespace["dataset_path"].write_text("[]\n", encoding="utf-8")
    namespace["http_health"] = lambda *args, **kwargs: {"ok": True}
    namespace["load_dataset"] = lambda *_args: [{"instance_id": "task-1"}]
    namespace["generation_for_task"] = lambda row: {
        "status": "technical_generation_cleanup_failed",
        "task": row["instance_id"],
    }
    eval_calls = []
    namespace["eval_for_task"] = lambda row: eval_calls.append(row) or {"status": "eval_done"}

    returncode = namespace["main"]()
    summary = json.loads((namespace["base_run_dir"] / "summary.json").read_text(encoding="utf-8"))

    assert returncode == 1
    assert eval_calls == []
    assert summary["rows"][0]["eval"] == {
        "status": "skipped_generation_not_ready",
        "task": "task-1",
        "generation_status": "technical_generation_cleanup_failed",
        "reason": "generation_not_ready",
    }


def test_remote_runner_recovers_committed_prediction_without_metrics_projection(tmp_path):
    namespace = _remote_namespace(tmp_path, max_task_starts=1)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        **_proven_submission_integrity(patch),
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    namespace["write_json"](
        namespace["generation_state_path"](run_dir),
        {"start_count": 1},
    )

    result = namespace["generation_for_task"]({"instance_id": task})

    assert result["status"] == "generation_done"
    assert result["pairing"] == "embedded_metric"


@pytest.mark.parametrize(
    ("status", "returncode", "expected"),
    [
        ("done", 0, True),
        ("done", 1, False),
        ("done_with_timeout_patch", 124, True),
        ("done_with_timeout_patch", 1, False),
        ("done_with_timeout_patch", 0, False),
        ("done", True, False),
        ("done", None, False),
    ],
)
def test_remote_generation_done_requires_strict_status_returncode_identity(
    tmp_path,
    status,
    returncode,
    expected,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": status,
        **_proven_submission_integrity(patch),
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    if returncode is not None:
        metric["runner_returncode"] = returncode
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    done, _prediction, _metric, pairing = namespace["generation_done"](run_dir, task)

    assert done is expected
    assert pairing == "embedded_metric"


@pytest.mark.parametrize(
    ("integrity_fields", "expected"),
    [
        ({}, False),
        (
            _proven_submission_integrity(
                "diff --git a/src/a.py b/src/a.py\n+fixed\n"
            ),
            True,
        ),
        ({"submission_eligible": False}, False),
        ({"execution_quiesced": False}, False),
        ({"test_patch_isolation_failed": True}, False),
    ],
)
def test_remote_generation_done_requires_modern_submission_integrity(
    tmp_path,
    integrity_fields,
    expected,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        **integrity_fields,
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    done, _prediction, _metric, pairing = namespace["generation_done"](
        run_dir,
        task,
    )

    assert done is expected
    assert pairing == "embedded_metric"


def test_eval_only_can_observe_a_legacy_artifact_without_promoting_it_to_current_run(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    patch_sha = namespace["patch_sha"](patch)
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
        "model_name": namespace["model_name"],
        "workflow": namespace["workflow"],
        **namespace["generation_runtime_identity"](),
    }
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": patch_sha,
        "model_patch": patch,
        "workflow_metric": metric,
        "model_name_or_path": namespace["model_name"],
        "workflow": namespace["workflow"],
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])

    current, *_ = namespace["generation_done"](run_dir, task)
    historical, _prediction, _metric, _pairing = namespace["generation_done"](
        run_dir,
        task,
        require_identity=False,
    )

    assert current is False
    assert historical is True
    assert namespace["historical_generation_identity_status"](
        prediction, metric, task
    ) == "legacy_verified"


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
        "runner_returncode": 0,
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


def test_filter_model_patch_decodes_quoted_octal_git_paths(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        'diff --git "a/src/\\346\\250\\241\\345\\235\\227.py" '
        '"b/src/\\346\\250\\241\\345\\235\\227.py"\n'
        '--- "a/src/\\346\\250\\241\\345\\235\\227.py"\n'
        '+++ "b/src/\\346\\250\\241\\345\\235\\227.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        'diff --git "a/test_\\344\\270\\255.py" "b/test_\\344\\270\\255.py"\n'
        '--- "a/test_\\344\\270\\255.py"\n'
        '+++ "b/test_\\344\\270\\255.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        'diff --git "a/src\\\\tests\\\\module.py" "b/src\\\\tests\\\\module.py"\n'
        '--- "a/src\\\\tests\\\\module.py"\n'
        '+++ "b/src\\\\tests\\\\module.py"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "src/\\346\\250\\241\\345\\235\\227.py" in filtered
    assert "test_\\344\\270\\255.py" not in filtered
    assert "src\\\\tests\\\\module.py" in filtered


def test_prolite_prediction_sha_comes_from_patch_text(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    run_dir = namespace["base_run_dir"] / task
    stale_patch = "diff --git a/src/a.py b/src/a.py\n+stale\n"
    current_patch = "diff --git a/src/a.py b/src/a.py\n+current\n"
    stale_sha = namespace["patch_sha"](stale_patch)
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "model_patch": current_patch,
    }
    metric = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": stale_sha,
        "workflow_status": "done",
        "runner_returncode": 0,
    }
    _write_jsonl(run_dir / "predictions.jsonl", [prediction])
    _write_jsonl(run_dir / "metrics.jsonl", [metric])

    done, _prediction, _metric, pairing = namespace["generation_done"](run_dir, task)

    assert namespace["row_patch_sha"](prediction) == namespace["patch_sha"](current_patch)
    assert done is False
    assert pairing == "record_id_patch_sha_mismatch"


def test_remote_patch_sha_match_requires_exact_hex_digest(tmp_path):
    namespace = _remote_namespace(tmp_path)
    digest = "a1" * 32

    assert namespace["patch_sha_matches"](digest, digest) is True
    assert namespace["patch_sha_matches"](digest[:12], digest) is False
    assert namespace["patch_sha_matches"]("g" * 64, "g" * 64) is False
    assert namespace["patch_sha_matches"](digest.upper(), digest) is False


def test_prolite_python_plan_batches_81_exact_node_ids_without_file_fallback(tmp_path):
    namespace = _remote_namespace(tmp_path)
    targets = [f"tests/test_many.py::test_case[{index}]" for index in range(81)]

    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, targets)

    assert plan["coverage_verified"] is True
    assert plan["coverage"] == "exact_targets"
    assert [target for batch in plan["target_batches"] for target in batch] == targets
    assert [len(batch) for batch in plan["target_batches"]] == [80, 1]
    command_targets = []
    for command in plan["commands"]:
        argv = namespace["shlex"].split(command)
        assert argv[:7] == [
            "pytest",
            "-p",
            "opencollab_pytest_proof",
            "-q",
            "-rA",
            "-o",
            "addopts=",
        ]
        command_targets.extend(argv[7:])
    assert command_targets == targets
    assert plan["commands"][1] != (
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/test_many.py"
    )


@pytest.mark.parametrize(
    "evidence_mode",
    ["matching", "tampered", "missing_log", "unsafe_late_log", "go_package_pass_only"],
)
def test_prolite_eval_requires_matching_batch_and_target_evidence(
    monkeypatch,
    tmp_path,
    evidence_mode,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-81"
    container_id = "d" * 64
    is_go = evidence_mode == "go_package_pass_only"
    targets = (
        ["internal/api/widget_test.go::TestWidget"]
        if is_go
        else [f"tests/test_many.py::test_case[{index}]" for index in range(81)]
    )
    _seed_remote_completed_generation(namespace, task)

    class FinishedProcess:
        pid = 424280

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, *args, **kwargs):
        input_mount = next(item for item in command if str(item).endswith(":/eval_input:ro"))
        output_mount = next(item for item in command if str(item).endswith(":/eval_output"))
        input_dir = Path(str(input_mount).removesuffix(":/eval_input:ro"))
        output_dir = Path(str(output_mount).removesuffix(":/eval_output"))
        cidfile = Path(command[command.index("--cidfile") + 1])
        cidfile.write_text(container_id, encoding="ascii")
        proof_nonce = (input_dir / "proof.nonce").read_text(encoding="ascii").strip()
        for name in (
            "base_commit.exit",
            "service_bootstrap.exit",
            "before_repo.exit",
            "post_before_base.exit",
            "model_patch.exit",
            "test_patch.exit",
            "f2p.exit",
            "p2p.exit",
        ):
            (output_dir / name).write_text("0\n", encoding="ascii")
        for name in ("service_bootstrap.log", "model_patch.log", "test_patch.log"):
            (output_dir / name).write_text("", encoding="utf-8")
        if evidence_mode == "unsafe_late_log":
            unsafe = output_dir / "service_bootstrap.log"
            unsafe.unlink()
            unsafe.symlink_to(output_dir / "attacker.log")
        for prefix in ("f2p", "p2p"):
            (output_dir / f"{prefix}.command").write_bytes((input_dir / f"{prefix}.command").read_bytes())
            (output_dir / f"{prefix}.log").write_text("", encoding="utf-8")
            plan = json.loads((input_dir / f"{prefix}.plan.json").read_text(encoding="utf-8"))
            for index, batch_command in enumerate(plan["commands"], 1):
                stem = output_dir / f"{prefix}.batch_{index:03d}"
                observed_command = (
                    "echo ok"
                    if evidence_mode == "tampered" and prefix == "f2p" and index == len(plan["commands"])
                    else batch_command
                )
                Path(f"{stem}.command").write_text(observed_command + "\n", encoding="utf-8")
                Path(f"{stem}.exit").write_text("0\n", encoding="ascii")
                if not (
                    evidence_mode == "missing_log"
                    and prefix == "f2p"
                    and index == len(plan["commands"])
                ):
                    batch_log = (
                        '{"Action":"pass","Package":"example/internal/api"}\n'
                        if is_go and prefix == "f2p"
                        else "".join(
                            f"PASSED {target}\n"
                            for target in plan["target_batches"][index - 1]
                        )
                        + f"{len(plan['target_batches'][index - 1])} passed in 0.01s\n"
                    )
                    Path(f"{stem}.log").write_text(batch_log, encoding="utf-8")
                proof = plan["proofs"][index - 1]
                if proof.get("kind") == "pytest_structured_reports":
                    nodes = plan["target_batches"][index - 1]
                    events = [
                        {"event": "session_start"},
                        {"event": "collection_finish", "nodeids": nodes},
                    ]
                    for node in nodes:
                        events.extend(
                            {
                                "event": "runtest_logreport",
                                "nodeid": node,
                                "when": phase,
                                "outcome": "passed",
                            }
                            for phase in ("setup", "call", "teardown")
                        )
                    events.append({"event": "session_finish", "exitstatus": 0})
                    proof_path = Path(f"{stem}.proof.{proof_nonce}.jsonl")
                    proof_path.write_text(
                        "".join(json.dumps(event) + "\n" for event in events),
                        encoding="utf-8",
                    )
        return FinishedProcess()

    inspect_calls = 0

    def fake_run(command, timeout=60):
        nonlocal inspect_calls
        if command[1] == "inspect":
            inspect_calls += 1
            if inspect_calls > 1:
                return {"returncode": 1, "stdout": "", "stderr": "No such container"}
            return {
                "returncode": 0,
                "stdout": (f"{container_id}\t{namespace['owner_nonce']}\tdirect-eval-v1"),
                "stderr": "",
            }
        return {"returncode": 0, "stdout": container_id, "stderr": ""}

    monkeypatch.setattr(namespace["subprocess"], "Popen", fake_popen)
    namespace["ensure_image"] = lambda image: {"ok": True}
    namespace["ensure_process_group_quiesced_after_wait"] = lambda proc: True
    namespace["cleanup_eval_container"] = lambda *args, **kwargs: {
        "ok": True,
        "status": "all_references_absent",
    }
    namespace["run"] = fake_run

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": targets,
            "repo_language": "go" if is_go else "python",
        }
    )

    evidence = result["summary"]["tests_status"]["fail_to_pass_evidence"]
    assert len(evidence) == (1 if is_go else 2)
    if evidence_mode == "go_package_pass_only":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "fail_to_pass_evidence" in result["summary"]["technical_reasons"]
        assert evidence[-1]["target_proof_matches_plan"] is False
    elif evidence_mode == "tampered":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "fail_to_pass_evidence" in result["summary"]["technical_reasons"]
        assert evidence[-1]["command_matches_plan"] is False
    elif evidence_mode == "missing_log":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "fail_to_pass_evidence" in result["summary"]["technical_reasons"]
        assert evidence[-1]["log_artifact_safe"] is False
    elif evidence_mode == "unsafe_late_log":
        assert result["status"] == "technical_eval_failed"
        assert result["summary"]["resolved"] is False
        assert "unsafe_or_missing_output_artifact" in result["summary"]["technical_reasons"]
        assert any(
            error.startswith("unsafe:service_bootstrap.log")
            for error in result["summary"]["output_artifact_errors"]
        )
    else:
        assert result["status"] == "eval_done"
        assert result["summary"]["resolved"] is True
        assert all(
            item["command_matches_plan"] and item["log_artifact_safe"] and item["artifact_safe"]
            for item in evidence
        )
        namespace["ensure_image"] = lambda image: pytest.fail(
            "valid persisted evidence should be reused before Docker"
        )
        reused = namespace["eval_for_task"](
            {
                "instance_id": task,
                "fail_to_pass": targets,
                "repo_language": "python",
            }
        )
        assert reused["status"] == "eval_done"
        assert reused["summary"]["resolved"] is True


def test_prolite_eval_marks_ruby_echo_ok_as_technical_red(tmp_path):
    namespace = _remote_namespace(tmp_path)
    task = "ruby-task"
    _seed_remote_completed_generation(namespace, task)
    namespace["ensure_image"] = lambda image: pytest.fail("unverified commands must fail before Docker")

    result = namespace["eval_for_task"](
        {
            "instance_id": task,
            "fail_to_pass": ["spec/widget_spec.rb"],
            "repo_language": "ruby",
            "test_cmd": "echo ok",
            "eval_cmd": "echo ok",
        }
    )

    assert result["status"] == "technical_eval_failed"
    assert result["summary"]["resolved"] is False
    assert result["summary"]["technical_reasons"] == ["no_verified_fail_to_pass_plan"]


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


def test_remote_runner_rejects_identity_only_done_summary_without_test_evidence(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    task = "task-1"
    patch = "diff --git a/src/a.py b/src/a.py\n+fixed\n"
    prediction = {
        "instance_id": task,
        "record_id": "r1",
        "patch_sha256": namespace["patch_sha"](patch),
        "model_patch": patch,
    }
    f2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        ["tests/test_x.py::test_target"],
    )
    p2p_plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [],
    )
    eval_spec_sha256 = namespace["prolite_eval_spec_sha256"](
        {},
        f2p_plan,
        p2p_plan,
    )
    identity_only = {
        "status": "done",
        "task": task,
        "patch_sha256": prediction["patch_sha256"],
        "record_id": "r1",
        "eval_spec_sha256": eval_spec_sha256,
        "resolved": True,
    }

    assert namespace["eval_summary_matches_prediction"](
        identity_only,
        prediction,
        task,
        eval_spec_sha256=eval_spec_sha256,
        f2p_plan=f2p_plan,
        p2p_plan=p2p_plan,
    ) is False
