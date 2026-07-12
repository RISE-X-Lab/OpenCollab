from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "swe_eval_run.py"
MODEL_ARGUMENTS = [
    "--model-name",
    "public-evaluation-model",
    "--llm-model",
    "provider/model",
]


def _load_entry_module() -> Any:
    spec = importlib.util.spec_from_file_location("swe_eval_run_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules["swe_eval_run_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_base_team_entry_delegates_to_parallel_runner(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda path, name: FakeRunner)

    rc = module.main(
        [
            "--dataset",
            "swe-batch-pro-lite",
            "--indices",
            "51,52",
            "--solver",
            "baseTeam",
            "--workers",
            "2",
            "--run-id",
            "base_team_smoke",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    )

    assert rc == 0
    argv = captured["argv"]
    assert "--workflow" in argv
    assert argv[argv.index("--workflow") + 1] == "base-team"
    assert "--max-workers" in argv
    assert argv[argv.index("--max-workers") + 1] == "2"
    assert "--dry-run" in argv


def test_team_pro_entry_uses_dynamic_workflow_defaults(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda path, name: FakeRunner)

    rc = module.main(
        [
            "--dataset",
            "swe-batch-pro-lite",
            "--indices",
            "7,11",
            "--solver",
            "TeamPro",
            "--workers",
            "2",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    )

    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--workflow") + 1] == "team-pro"
    assert argv[argv.index("--budget") + 1] == "4000000"
    assert argv[argv.index("--max-task-starts") + 1] == "3"
    assert argv[argv.index("--model-name") + 1] == "public-evaluation-model"
    assert argv[argv.index("--llm-model") + 1] == "provider/model"
    assert argv[argv.index("--temperature") + 1] == "1.0"
    assert argv[argv.index("--top-p") + 1] == "1.0"
    assert argv[argv.index("--max-output-tokens") + 1] == "32768"
    assert "--workflow-env" not in argv


def test_team_pro_entry_accepts_explicit_runtime_settings(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda path, name: FakeRunner)
    assert module.main(
        [
            "--indices",
            "7",
            "--solver",
            "TeamPro",
            "--budget=2000000",
            "--max-task-starts",
            "2",
            "--model-name",
            "custom-teampro",
            "--llm-model",
            "provider/custom",
            "--temperature",
            "0",
            "--workflow-env",
            "OPENCOLLAB_TOP_P=0",
            "--dry-run",
        ]
    ) == 0

    argv = captured["argv"]
    assert argv.count("--temperature") == 1
    assert argv[argv.index("--temperature") + 1] == "0"
    assert argv[argv.index("--model-name") + 1] == "custom-teampro"
    assert argv[argv.index("--llm-model") + 1] == "provider/custom"


def test_team_pro_entry_rejects_workflow_override(monkeypatch: Any) -> None:
    module = _load_entry_module()

    with pytest.raises(SystemExit, match="selected by --solver"):
        module.main(
            [
                "--indices",
                "7",
                "--solver",
                "TeamPro",
                "--workflow",
                "validation-council-solve",
                "--dry-run",
            ]
        )


def test_unified_entry_rejects_historical_eval_only_maintenance_options() -> None:
    module = _load_entry_module()

    with pytest.raises(SystemExit, match="single-task maintenance API"):
        module.main(
            [
                "--indices",
                "7",
                "--solver",
                "TeamPro",
                "--eval-only",
                "--parent-output-dir",
                "/tmp/parent",
            ]
        )


def test_openhands_entry_delegates_external_workflow(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda path, name: FakeRunner)

    rc = module.main(
        [
            "--dataset",
            "swe-batch-pro-lite",
            "--indices",
            "1-2",
            "--solver",
            "openhands",
            "--workers",
            "1",
            "--openhands-command",
            "openhands --help",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    )

    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--workflow") + 1] == "openhands-external"
    assert argv[argv.index("--openhands-command") + 1] == "openhands --help"
    assert argv[argv.index("--max-task-starts") + 1] == "2"
    assert argv[argv.index("--budget") + 1] == "16000000"
    assert argv[argv.index("--llm-model") + 1] == "provider/model"


def test_openhands_entry_has_one_command_defaults(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setattr(module, "_load_module", lambda path, name: FakeRunner)
    assert module.main(
        [
            "--indices",
            "51-100",
            "--solver",
            "openhands",
            "--workers",
            "5",
            *MODEL_ARGUMENTS,
            "--dry-run",
        ]
    ) == 0
    argv = captured["argv"]
    assert argv[argv.index("--openhands-command") + 1].endswith("--file {prompt_file}")
    assert argv[argv.index("--max-empty-patch-retries") + 1] == "1"
    assert argv[argv.index("--max-eval-attempts") + 1] == "2"
    assert argv[argv.index("--openhands-empty-patch-rejections") + 1] == "2"
    assert argv[argv.index("--max-steps") + 1] == "120"


def test_solver_entry_fails_fast_without_model_configuration(monkeypatch: Any) -> None:
    module = _load_entry_module()
    monkeypatch.delenv("OPENCOLLAB_SWE_MODEL_NAME", raising=False)
    monkeypatch.delenv("OPENCOLLAB_SWE_LLM_MODEL", raising=False)

    with pytest.raises(
        SystemExit,
        match="requires --model-name or OPENCOLLAB_SWE_MODEL_NAME",
    ):
        module.main(["--indices", "1", "--solver", "g11", "--dry-run"])


def test_solver_entry_reads_model_configuration_from_environment(monkeypatch: Any) -> None:
    module = _load_entry_module()
    captured: dict[str, Any] = {}

    class FakeRunner:
        @staticmethod
        def main() -> int:
            captured["argv"] = list(sys.argv)
            return 0

    monkeypatch.setenv("OPENCOLLAB_SWE_MODEL_NAME", "environment-run")
    monkeypatch.setenv("OPENCOLLAB_SWE_LLM_MODEL", "provider/environment-model")
    monkeypatch.setattr(module, "_load_module", lambda path, name: FakeRunner)

    assert module.main(["--indices", "1", "--solver", "g11", "--dry-run"]) == 0
    argv = captured["argv"]
    assert argv[argv.index("--model-name") + 1] == "environment-run"
    assert argv[argv.index("--llm-model") + 1] == "provider/environment-model"


def test_detached_plist_uses_direct_python_without_wrapper() -> None:
    module = _load_entry_module()
    payload = module._launchd_plist(
        label="com.opencollab.eval.test",
        program_arguments=[sys.executable, str(SCRIPT), "--indices", "51-100"],
        stdout_path=Path("/tmp/stdout.log"),
        stderr_path=Path("/tmp/stderr.log"),
    )

    assert payload["ProgramArguments"][0] == sys.executable
    assert payload["ProgramArguments"][1] == str(SCRIPT)
    assert payload["KeepAlive"] is False
    assert "/bin/bash" not in payload["ProgramArguments"]


def test_detach_options_are_not_forwarded_to_child() -> None:
    module = _load_entry_module()

    assert module._without_launch_options(
        [
            "--indices",
            "51-100",
            "--detach",
            "--launchd-label",
            "com.example.eval",
            "--no-persistent-proxy",
        ]
    ) == ["--indices", "51-100"]


def test_persistent_proxy_fails_fast_without_remote_host(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_entry_module()
    monkeypatch.delenv("OPENCOLLAB_SWE_HOST", raising=False)

    with pytest.raises(RuntimeError, match="--host or OPENCOLLAB_SWE_HOST"):
        module._ensure_proxy_agent(output_dir=tmp_path, remaining=[])


def test_detach_starts_direct_launch_agent_once(monkeypatch: Any, tmp_path: Path) -> None:
    module = _load_entry_module()
    written: list[tuple[Path, dict[str, Any]]] = []
    launchctl_calls: list[tuple[str, ...]] = []

    def fake_launchctl(*arguments: str, check: bool = False) -> SimpleNamespace:
        launchctl_calls.append(arguments)
        return SimpleNamespace(returncode=1 if arguments[0] == "print" else 0, stdout="", stderr="")

    monkeypatch.setattr(module, "_launchctl", fake_launchctl)
    monkeypatch.setattr(module, "_write_plist", lambda path, payload: written.append((path, payload)))
    monkeypatch.setattr(module.shutil, "copy2", lambda source, target: None)

    rc = module.main(
        [
            "--indices",
            "51-100",
            "--solver",
            "openhands",
            "--workers",
            "5",
            "--run-id",
            "openhands_51_100",
            "--output-dir",
            str(tmp_path),
            "--detach",
            "--no-persistent-proxy",
        ]
    )

    assert rc == 0
    assert len(written) == 1
    program = written[0][1]["ProgramArguments"]
    assert program[:2] == [sys.executable, str(SCRIPT)]
    assert "--detach" not in program
    assert "--no-persistent-proxy" not in program
    assert any(call[0] == "bootstrap" for call in launchctl_calls)
