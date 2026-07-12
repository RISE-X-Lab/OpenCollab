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
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="openhands",
            timeout=5,
            output="partial stdout",
            stderr="timeout stderr",
        )

    monkeypatch.setattr(gpo.subprocess, "run", raise_timeout)
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
    assert (tmp_path / "output" / "openhands.stdout.log").read_text() == "partial stdout"
    assert (tmp_path / "output" / "openhands.stderr.log").read_text() == "timeout stderr"


def test_run_openhands_rejects_zero_exit_with_fatal_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gpo.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args="openhands",
            returncode=0,
            stdout="partial events",
            stderr="Traceback (most recent call last)\nModuleNotFoundError: linkify_it",
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

    def completed(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args="openhands", returncode=0, stdout="done", stderr=""
        )

    monkeypatch.setattr(gpo.subprocess, "run", completed)
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
    assert "OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR" not in env
    assert "SWE_TASK_ID" not in env
    assert all(leaked_id not in value for value in env.values())


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
    monkeypatch.setattr(gpo.gp, "start_container", lambda image, name: "container-123")
    monkeypatch.setattr(gpo.gp, "write_container_marker", lambda *args: None)
    monkeypatch.setattr(gpo.gp, "remove_container_and_clear_marker", lambda *args: None)
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
    monkeypatch.setattr(
        gpo,
        "extract_patch_guarded",
        lambda cid, **kwargs: (
            "diff --git a/widget.py b/widget.py\n+fixed = True\n",
            ["tests/test_widget.py"],
        ),
    )
    openhands_call: dict = {}

    def fake_run_openhands(**kwargs):
        openhands_call.update(kwargs)
        return {
            "status": "done",
            "returncode": 0,
            "duration_s": 1.0,
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
    hook_config = json.loads(
        (attempt_dir / ".openhands" / "hooks.json").read_text()
    )
    hook_command = hook_config["stop"][0]["hooks"][0]["command"]
    assert hook_command.startswith("if [ -f ")
    assert str(_REPO_ROOT / "swebench" / "openhands_require_patch.py") in hook_command
    assert "|| exit 1" in hook_command
    assert "missing_patch_guard_script" in hook_command
