from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEBENCH_DIR = _REPO_ROOT / "swebench"
if str(_SWEBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_SWEBENCH_DIR))

import gen_prediction_openhands as gpo  # noqa: E402
import openhands_runtime  # noqa: E402
from gen_prediction_snapshot import SolverGitSnapshot  # noqa: E402
from opencollab.harness.swe_eval_decision import (  # noqa: E402
    TaskSnapshot,
    TaskState,
    decide_task,
)


def _install_fake_openhands_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    times_out: bool = False,
    captured: dict | None = None,
) -> None:
    monkeypatch.setattr(
        gpo,
        "_prepare_openhands_container_guard",
        lambda container_id: "/usr/bin/python3",
    )
    monkeypatch.setattr(
        gpo,
        "_quiesce_openhands_container",
        lambda container_id, python_bin: {
            "proven": True,
            "returncode": 0,
            "error": "",
        },
    )

    class FakeProcess:
        pid = 424242

        def __init__(self, code: int):
            self.returncode = None if times_out else code

        def wait(self, timeout=None):
            if times_out:
                raise subprocess.TimeoutExpired("openhands", timeout)
            return self.returncode

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            if captured is not None:
                captured.setdefault("signals", []).append(sig)

    def fake_popen(*args, **kwargs):
        if captured is not None:
            captured.update(kwargs)
            captured["command"] = args[0]
        kwargs["stdout"].write(stdout)
        kwargs["stdout"].flush()
        kwargs["stderr"].write(stderr)
        kwargs["stderr"].flush()
        return FakeProcess(returncode)

    monkeypatch.setattr(gpo.subprocess, "Popen", fake_popen)


def test_prompt_requires_all_repository_work_to_use_the_existing_container() -> None:
    prompt = gpo._prompt(
        {
            "repo": "acme/widget",
            "problem_statement": "Fix the widget.",
            "hints_text": "Inspect parser.py.",
        },
        container_id="container-123",
    )

    assert "docker exec" not in prompt
    assert gpo.gp.DOCKER_WORKDIR in prompt
    assert "isolated, offline workspace" in prompt
    assert "git status --short" in prompt


def test_run_openhands_records_timeout_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_openhands_process(
        monkeypatch,
        returncode=124,
        stdout="partial stdout",
        stderr="timeout stderr",
    )
    result = gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )

    assert result["status"] == "openhands_timeout"
    assert result["returncode"] == 124
    assert result["execution_quiesced"] is True
    assert (tmp_path / "output" / "openhands.stdout.log").read_text() == "partial stdout"
    assert (tmp_path / "output" / "openhands.stderr.log").read_text() == "timeout stderr"


def test_run_openhands_rejects_zero_exit_with_fatal_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_openhands_process(
        monkeypatch,
        stdout="partial events",
        stderr=(
            "Traceback (most recent call last)\n"
            "ModuleNotFoundError: linkify_it"
        ),
    )

    result = gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )

    assert result["status"] == "openhands_failed"
    assert result["returncode"] == 0


def test_run_openhands_passes_effective_runtime_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    leaked_id = "owner__repo-deadbeef"
    monkeypatch.setenv(
        "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR",
        f"/trusted/runs/{leaked_id}/workflow_logs",
    )
    monkeypatch.setenv("SWE_TASK_ID", leaked_id)

    _install_fake_openhands_process(
        monkeypatch,
        stdout="done",
        captured=captured,
    )
    gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
        context_window=400000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32768,
        token_budget=16000000,
        max_steps=60,
    )

    env = captured["env"]
    assert env["OPENHANDS_CONTEXT_WINDOW"] == "400000"
    assert env["OPENHANDS_TEMPERATURE"] == "1.0"
    assert env["OPENHANDS_TOP_P"] == "1.0"
    assert env["OPENHANDS_MAX_OUTPUT_TOKENS"] == "32768"
    assert env["OPENHANDS_TOKEN_BUDGET"] == "16000000"
    assert env["OPENHANDS_MAX_STEPS"] == "60"
    assert env["OPENHANDS_EMPTY_PATCH_REJECTIONS"] == "0"
    assert env["OPENHANDS_CONTAINER_PYTHON"] == "/usr/bin/python3"
    assert env["OPENHANDS_CONTAINER_GUARD_ROOT"] == gpo._CONTAINER_GUARD_ROOT
    assert "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR" not in env
    assert "SWE_TASK_ID" not in env
    assert all(leaked_id not in value for value in env.values())
    assert captured["start_new_session"] is True
    assert captured["shell"] is False
    assert captured["command"][1].endswith(
        "swebench/openhands_process_supervisor.py"
    )


def test_container_guard_preparation_streams_trusted_host_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:3] == ["exec", "container-123"] and argv[-1] == (
            "command -v python3 || command -v python"
        ):
            return SimpleNamespace(returncode=0, stdout="/usr/bin/python3\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gpo.container_guard.subprocess, "run", fake_run)

    python_bin = gpo._prepare_openhands_container_guard("container-123")

    assert python_bin == "/usr/bin/python3"
    helper_argv, helper_kwargs = calls[1]
    assert helper_argv == [
        "docker",
        "exec",
        "-i",
        "container-123",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-",
        "--prepare-guard-root",
        gpo._CONTAINER_GUARD_ROOT,
    ]
    assert "def prepare_guard_root" in helper_kwargs["input"]
    assert helper_kwargs["text"] is True
    assert helper_kwargs["check"] is False


def test_run_openhands_marks_process_group_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openhands_process(
        monkeypatch,
        returncode=125,
        stdout="done",
    )

    result = gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )

    assert result["status"] == "openhands_cleanup_failed"
    assert result["returncode"] == 125
    assert result["execution_quiesced"] is False


def test_run_openhands_container_quiescence_failure_blocks_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openhands_process(monkeypatch, stdout="done")
    monkeypatch.setattr(
        gpo,
        "_quiesce_openhands_container",
        lambda container_id, python_bin: {
            "proven": False,
            "returncode": 125,
            "error": "escaped process remained",
        },
    )

    result = gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )

    assert result["status"] == "openhands_cleanup_failed"
    assert result["returncode"] == 125
    assert result["host_execution_quiesced"] is True
    assert result["container_execution_quiesced"] is False
    assert result["execution_quiesced"] is False
    assert result["container_quiescence_error"] == "escaped process remained"


def test_run_openhands_treats_signalled_supervisor_as_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openhands_process(
        monkeypatch,
        returncode=-gpo.signal.SIGKILL,
        stdout="partial",
    )

    result = gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=5,
    )

    assert result["status"] == "openhands_cleanup_failed"
    assert result["returncode"] == 125
    assert result["execution_quiesced"] is False


def test_run_openhands_outer_supervisor_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _install_fake_openhands_process(
        monkeypatch,
        stdout="partial",
        times_out=True,
        captured=captured,
    )

    result = gpo._run_openhands(
        command_template="openhands --headless --file {prompt_file}",
        container_id="container-123",
        instance={"instance_id": "acme__widget-1"},
        instance_file=tmp_path / "instance.json",
        prompt_file=tmp_path / "prompt.md",
        output_dir=tmp_path / "output",
        timeout=0.1,
    )

    assert captured["signals"] == [gpo.signal.SIGTERM]
    assert result["status"] == "openhands_cleanup_failed"
    assert result["returncode"] == 125
    assert result["execution_quiesced"] is False


@pytest.mark.parametrize(
    ("process_quiesced", "patch_extraction_succeeded", "artifact_exclusion"),
    [
        (False, True, True),
        (True, False, False),
    ],
)
def test_openhands_integrity_failure_never_enters_official_eval(
    process_quiesced: bool,
    patch_extraction_succeeded: bool,
    artifact_exclusion: bool,
) -> None:
    patch = "diff --git a/widget.py b/widget.py\n+fixed = True\n"
    metrics = {"workflow_status": "done"}
    gpo._complete_openhands_integrity(
        metrics,
        patch=patch,
        snapshot_prepared=True,
        process_quiesced=process_quiesced,
        patch_extraction_succeeded=patch_extraction_succeeded,
        harness_artifact_exclusion_proven=artifact_exclusion,
    )
    prediction, metric = gpo.build_output_records(
        instance_id="acme__widget-1",
        model_name="openhands",
        patch=patch,
        metrics=metrics,
        workflow_name="openhands-external",
    )

    decision = decide_task(
        TaskSnapshot(
            task_id="acme__widget-1",
            prediction=prediction,
            metric=metric,
            metric_pairing="record_id_patch_sha_match",
        )
    )

    assert metrics["submission_eligible"] is False
    assert decision.state is TaskState.WORKFLOW_FAILED
    assert decision.ready_for_eval is False


def test_container_stop_failure_forbids_patch_extraction() -> None:
    assert not gpo._openhands_patch_extraction_allowed(
        {
            "status": "done",
            "execution_quiesced": True,
            "host_execution_quiesced": True,
            "container_execution_quiesced": False,
        }
    )


def test_openhands_runtime_settings_update_agent_and_condenser() -> None:
    class Copyable:
        def __init__(self, **values):
            self.__dict__.update(values)

        def model_copy(self, *, update):
            return Copyable(**{**self.__dict__, **update})

    settings = openhands_runtime.RuntimeSettings(
        context_window=400000,
        temperature=1.0,
        top_p=1.0,
        max_output_tokens=32768,
        token_budget=16000000,
        max_steps=60,
    )
    agent = Copyable(
        llm=Copyable(),
        condenser=Copyable(llm=Copyable()),
    )

    configured = openhands_runtime.apply_agent_settings(agent, settings)

    assert configured.llm.max_input_tokens == 400000
    assert configured.llm.temperature == 1.0
    assert configured.llm.top_p == 1.0
    assert configured.llm.max_output_tokens == 32768
    assert configured.condenser.llm.max_input_tokens == 400000
    assert configured.condenser.llm.max_output_tokens == 32768


def test_openhands_runtime_imports_through_swebench_namespace() -> None:
    script = (
        "import sys, types\n"
        "package = types.ModuleType('swebench')\n"
        "package.__path__ = [sys.argv[1]]\n"
        "sys.modules['swebench'] = package\n"
        "import swebench.openhands_runtime\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(_SWEBENCH_DIR)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr


def test_openhands_isolated_tools_keep_only_sdk_terminal_name() -> None:
    agent = SimpleNamespace(
        tools=[
            SimpleNamespace(name="terminal"),
            SimpleNamespace(name="file_editor"),
            SimpleNamespace(name="task_tracker"),
            SimpleNamespace(name="task_tool_set"),
        ]
    )

    tools = openhands_runtime._isolated_agent_tools(agent, "terminal")

    assert [tool.name for tool in tools] == ["terminal"]


def test_openhands_terminal_commands_use_unique_container_guard_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENHANDS_CONTAINER_PYTHON", "/usr/bin/python3")
    monkeypatch.setenv(
        "OPENHANDS_CONTAINER_GUARD_ROOT",
        gpo._CONTAINER_GUARD_ROOT,
    )

    first = openhands_runtime._guarded_terminal_invocation(
        "container-123",
        "pytest -q",
    )
    second = openhands_runtime._guarded_terminal_invocation(
        "container-123",
        "git status --short",
    )

    assert first.argv[:8] == (
        "docker",
        "exec",
        "-i",
        "container-123",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-",
    )
    assert first.argv[8] == "run"
    assert first.argv[9] == first.pidfile
    assert first.argv[10] == first.cancelfile
    assert first.argv[-1] == "pytest -q"
    assert first.pidfile != second.pidfile
    assert first.cancelfile == f"{first.pidfile}.cancel"
    assert "def run(pidfile: Path, cancelfile: Path" in first.source


def test_openhands_token_budget_guard_counts_all_llm_instances() -> None:
    class Usage:
        def __init__(self, prompt_tokens, completion_tokens):
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens

    class Metrics:
        def __init__(self, usage):
            self.accumulated_token_usage = usage

    class FakeLLM:
        def __init__(self, prompt_tokens, completion_tokens):
            self.metrics = Metrics(Usage(prompt_tokens, completion_tokens))

    guard = openhands_runtime.TokenBudgetGuard(100)
    first = FakeLLM(40, 10)
    second = FakeLLM(30, 10)
    first_reservation = guard.reserve(60)
    guard.record(first, reservation=first_reservation)
    second_reservation = guard.reserve(50)
    guard.record(second, reservation=second_reservation)

    assert guard.spent == 90
    assert guard.reserved == 0
    with pytest.raises(RuntimeError, match="cannot cover the next request"):
        guard.reserve(11)


def test_openhands_token_budget_reserves_request_before_api_call() -> None:
    guard = openhands_runtime.TokenBudgetGuard(100)
    first = guard.reserve(70)

    with pytest.raises(RuntimeError, match="cannot cover the next request"):
        guard.reserve(31)

    class Usage:
        prompt_tokens = 40
        completion_tokens = 10

    class Metrics:
        accumulated_token_usage = Usage()

    class FakeLLM:
        metrics = Metrics()

    guard.record(FakeLLM(), reservation=first)
    assert guard.spent == 50
    assert guard.reserve(50) == 50


def test_openhands_usage_is_written_in_eval_layer_ledger_shape(tmp_path: Path) -> None:
    state_dir = tmp_path / "openhands" / "persistence" / "conversations" / "conversation-1"
    state_dir.mkdir(parents=True)
    (state_dir / "base_state.json").write_text(
        json.dumps(
            {
                "stats": {
                    "usage_to_metrics": {
                        "agent": {
                            "accumulated_cost": 0.25,
                            "accumulated_token_usage": {
                                "prompt_tokens": 1000,
                                "completion_tokens": 200,
                                "cache_read_tokens": 300,
                                "cache_write_tokens": 100,
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    usage_values = gpo._openhands_usage(tmp_path / "openhands")
    assert usage_values is not None
    payload = gpo._append_usage_record(
        run_dir=tmp_path,
        instance_id="acme__widget-1",
        model="anthropic/glm-5.2",
        usage_values=usage_values,
    )

    assert payload["input_tokens"] == 1000
    assert payload["uncached_input_tokens"] == 600
    assert payload["cached_input_tokens"] == 300
    assert payload["cache_creation_tokens"] == 100
    assert payload["output_tokens"] == 200
    assert payload["total_tokens"] == 1200
    assert payload["cost_usd"] > 0
    record = json.loads((tmp_path / "api_usage.jsonl").read_text(encoding="utf-8"))
    assert record["schema"] == "opencollab.api_usage.v1"
    assert record["provider"] == "openhands"
    assert record["usage"] == payload


def test_main_writes_generation_contract_for_nonempty_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_file = tmp_path / "instance.json"
    output = tmp_path / "predictions.jsonl"
    metrics = tmp_path / "metrics.jsonl"
    instance_file.write_text(
        json.dumps(
            {
                "instance_id": "acme__widget-1",
                "base_commit": "b" * 40,
                "repo": "acme/widget",
                "problem_statement": "Fix the widget.",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gpo.gp,
        "start_container_with_marker",
        lambda image, name, run_dir: "container-123",
    )
    pending: dict = {}
    finalized: dict = {}
    lifecycle: list[str] = []

    def fake_persist_pending_output(**kwargs):
        lifecycle.append("persist")
        pending.update(kwargs)
        return tmp_path / "pending-output.json"

    def fake_finalize_container_ownership(**kwargs):
        lifecycle.append("cleanup")
        finalized.update(kwargs)
        kwargs["metrics"]["container_cleanup_succeeded"] = True

    def fake_publish_pending_output(run_dir, path):
        lifecycle.append("publish")
        assert path == tmp_path / "pending-output.json"
        gpo.gp.append_output_records(
            pending["predictions_path"],
            pending["metrics_path"],
            pending["prediction"],
            pending["metric"],
        )
        return "published"

    monkeypatch.setattr(
        gpo.gp,
        "persist_pending_output",
        fake_persist_pending_output,
    )
    monkeypatch.setattr(
        gpo.gp,
        "finalize_container_ownership",
        fake_finalize_container_ownership,
    )
    monkeypatch.setattr(
        gpo.gp,
        "publish_pending_output",
        fake_publish_pending_output,
    )
    monkeypatch.setattr(gpo, "anonymous_solver_task_id", lambda: "solver-" + "a" * 32)
    snapshot = SolverGitSnapshot(
        anonymous_head="c" * 40,
        base_tree="d" * 40,
        commit_count=1,
        remote_count=0,
        extra_git_metadata=0,
        removed_git_metadata=0,
    )
    monkeypatch.setattr(
        gpo,
        "prepare_solver_git_snapshot",
        lambda cid, base: snapshot,
    )
    baseline = SimpleNamespace(snapshot=snapshot, cleanup=lambda: None)
    monkeypatch.setattr(
        gpo,
        "prepare_trusted_patch_baseline",
        lambda cid, prepared_snapshot: baseline,
    )
    patch = "diff --git a/widget.py b/widget.py\n+fixed = True\n"
    proof = gpo.gp.gen_prediction_patch.TrustedPatchExtraction(
        fixed_anonymous_base=snapshot.anonymous_head,
        base_tree=snapshot.base_tree,
        baseline_archive_sha256="e" * 64,
        baseline_archive_bytes=10,
        baseline_archive_entries=1,
        baseline_extracted_bytes=1,
        workspace_archive_sha256="f" * 64,
        workspace_archive_bytes=10,
        workspace_archive_entries=1,
        workspace_extracted_bytes=1,
        patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        patch_bytes=len(patch.encode()),
    ).as_dict()
    monkeypatch.setattr(
        gpo,
        "extract_patch_guarded",
        lambda cid, trusted_baseline, **kwargs: (
            patch,
            ["tests/test_widget.py"],
            proof,
        ),
    )
    openhands_call: dict = {}

    def fake_run_openhands(**kwargs):
        openhands_call.update(kwargs)
        return {
            "status": "done",
            "returncode": 0,
            "duration_s": 1.0,
            "execution_quiesced": True,
            "host_execution_quiesced": True,
            "container_execution_quiesced": True,
        }

    monkeypatch.setattr(gpo, "_run_openhands", fake_run_openhands)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gen_prediction_openhands.py",
            "--instance-file",
            str(instance_file),
            "--output",
            str(output),
            "--metrics",
            str(metrics),
            "--command",
            "openhands --headless --file {prompt_file}",
            "--model-name",
            "openhands-1.16.0-glm-5.2",
            "--llm-model",
            "anthropic/glm-5.2",
            "--context-window",
            "400000",
            "--budget",
            "16000000",
            "--max-steps",
            "60",
        ],
    )

    gpo.main()

    prediction = json.loads(output.read_text(encoding="utf-8"))
    metric = json.loads(metrics.read_text(encoding="utf-8"))
    assert prediction["workflow"] == "openhands-external"
    assert prediction["model_patch"].strip()
    assert metric["workflow"] == "openhands-external"
    assert metric["workflow_status"] == "done"
    assert metric["llm_model"] == "anthropic/glm-5.2"
    assert metric["context_window"] == 400000
    assert metric["budget"] == 16000000
    assert metric["max_steps"] == 60
    assert metric["empty_patch_rejections"] == 2
    assert metric["openhands_empty_patch_rejections"] == 2
    assert metric["openhands_command_sha256"] == hashlib.sha256(
        b"openhands --headless --file {prompt_file}"
    ).hexdigest()
    assert metric["solver_git_snapshot"]["commit_count"] == 1
    for field in (
        "submission_eligible",
        "execution_quiesced",
        "patch_extraction_succeeded",
        "injected_path_cleanup_proven",
        "harness_artifact_exclusion_proven",
        "checkpoint_restore_integrity_proven",
        "task_stage_integrity_proven",
        "worktree_integrity_proven",
        "patch_produced",
    ):
        assert metric[field] is True
    assert metric["test_patch_isolation_failed"] is False
    json.dumps(metric)
    attempt_dir = next((tmp_path / "openhands_attempts").glob("solver-*"))
    solver_instance = json.loads(
        (attempt_dir / "solver_instance.json").read_text()
    )
    assert solver_instance["instance_id"] == "solver-" + "a" * 32
    assert "base_commit" not in solver_instance
    assert "acme__widget-1" not in (
        attempt_dir / "solver_instance.json"
    ).read_text()
    assert openhands_call["instance"]["instance_id"] == "solver-" + "a" * 32
    assert "acme__widget-1" not in str(openhands_call["output_dir"])
    assert metric["validation_artifacts_removed"] == ["tests/test_widget.py"]
    assert metric["record_id"] == prediction["record_id"]
    assert metric["patch_sha256"] == prediction["patch_sha256"]
    decision = decide_task(
        TaskSnapshot(
            task_id="acme__widget-1",
            prediction=prediction,
            metric=metric,
            metric_pairing="record_id_patch_sha_match",
        )
    )
    assert decision.state is TaskState.READY_FOR_EVAL
    assert decision.ready_for_eval is True
    assert finalized["completed"] is True
    assert finalized["keep_container"] is False
    assert lifecycle == ["persist", "cleanup", "publish"]
    hook_config = json.loads(
        (attempt_dir / ".openhands" / "hooks.json").read_text()
    )
    hook_command = hook_config["stop"][0]["hooks"][0]["command"]
    assert hook_command.startswith("if [ -f ")
    assert str(_REPO_ROOT / "swebench" / "openhands_require_patch.py") in hook_command
    assert "|| exit 1" in hook_command
    assert "missing_patch_guard_script" in hook_command
