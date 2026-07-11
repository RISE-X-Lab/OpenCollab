"""Positive execution proof for ProLite language adapters."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from swe_v1_prolite_runner_test_support import _remote_namespace, pytest


def _pytest_proof_text(nodeids, *, exitstatus=0, call_outcome="passed"):
    events = [
        {"event": "session_start"},
        {"event": "collection_finish", "nodeids": list(nodeids)},
    ]
    for nodeid in nodeids:
        for phase in ("setup", "call", "teardown"):
            events.append(
                {
                    "event": "runtest_logreport",
                    "nodeid": nodeid,
                    "when": phase,
                    "outcome": call_outcome if phase == "call" else "passed",
                }
            )
    events.append({"event": "session_finish", "exitstatus": exitstatus})
    return "".join(json.dumps(event) + "\n" for event in events)


def test_eval_runner_dependency_failures_are_infrastructure(tmp_path):
    namespace = _remote_namespace(tmp_path)

    assert namespace["eval_log_has_infra_failure"](127, "No supported JS test runner found for jest") is True
    assert namespace["eval_log_has_infra_failure"](124, "test command timed out") is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected error message 'request timed out'",
    ) is False
    assert namespace["eval_log_has_infra_failure"](
        1,
        "redis.exceptions.ConnectionError: Connection refused",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "MongoDB server unavailable: failed to connect",
    ) is True
    assert namespace["eval_log_has_infra_failure"](
        1,
        "AssertionError: expected 'Connection refused' but got 'accepted'",
    ) is False


def test_prolite_go_command_requires_exact_test_targets_and_json_events(tmp_path):
    namespace = _remote_namespace(tmp_path)

    plan = namespace["prolite_test_plan"](
        {"repo_language": "go"},
        [
            "internal/api/widget_test.go::TestWidget",
            "pkg/server/router_test.go::TestRouter/subcase",
        ],
    )

    assert plan["coverage_verified"] is True
    assert plan["coverage"] == "exact_test_events"
    assert plan["commands"] == [
        "go test -count=1 -json ./internal/api -run '^TestWidget$'",
        "go test -count=1 -json ./pkg/server -run '^TestRouter/subcase$'",
    ]
    assert plan["proofs"] == [
        {"kind": "go_json_test_pass", "test": "TestWidget"},
        {"kind": "go_json_test_pass", "test": "TestRouter/subcase"},
    ]


def test_prolite_go_file_only_target_is_technical_red(tmp_path):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": "go"},
        ["internal/api/widget_test.go"],
    )

    assert plan["coverage_verified"] is False
    assert plan["commands"] == []


@pytest.mark.parametrize("language", ["javascript", "typescript"])
def test_prolite_js_file_target_without_positive_event_parser_is_technical_red(
    tmp_path,
    language,
):
    namespace = _remote_namespace(tmp_path)
    plan = namespace["prolite_test_plan"](
        {"repo_language": language},
        ["test/widget.test.js"],
    )

    assert plan["coverage_verified"] is False
    assert plan["commands"] == []


def test_prolite_go_log_proof_rejects_package_pass_without_test_event(tmp_path):
    namespace = _remote_namespace(tmp_path)
    proof = {"kind": "go_json_test_pass", "test": "TestWidget"}

    assert namespace["_plan_log_proof_matches"](
        proof,
        '{"Action":"pass","Package":"example/internal/api"}\n',
    ) is False
    assert namespace["_plan_log_proof_matches"](proof, "[no test files]\n") is False
    assert namespace["_plan_log_proof_matches"](
        proof,
        '{"Action":"run","Test":"TestWidget"}\n'
        '{"Action":"pass","Test":"TestWidget"}\n',
    ) is True


def test_prolite_test_command_never_falls_back_to_a_passing_noop(tmp_path):
    namespace = _remote_namespace(tmp_path)
    command = namespace["prolite_test_command"]
    is_runnable = namespace["_is_runnable_test_command"]

    assert command({"repo_language": "python"}, []) == ""
    assert command({}, []) == ""
    assert command({"repo_language": "ruby"}, ["spec/widget_spec.rb"]) == ""
    assert command(
        {"repo_language": "ruby", "test_cmd": "echo ok", "eval_cmd": "echo also-ok"},
        ["spec/widget_spec.rb"],
    ) == ""
    assert not is_runnable("")
    assert not is_runnable("true")
    assert not is_runnable(" : ")
    assert not is_runnable("echo ok")
    assert is_runnable(
        "pytest -p opencollab_pytest_proof -q -rA -o addopts= "
        "tests/test_x.py::test_y"
    )


def test_prolite_pytest_console_ignores_shadow_module_and_collect_only_addopts(
    tmp_path,
):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
    )
    (tmp_path / "pytest.py").write_text(
        "print('shadow pytest: no tests')\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --collect-only\n",
        encoding="utf-8",
    )
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert True\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"

    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))
    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
    )
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert "shadow pytest" not in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_path.read_text(encoding="utf-8"),
    ) is True


def test_prolite_pytest_proof_rejects_conftest_exit_status_rewrite(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"](
        {"repo_language": "python"},
        [target],
    )
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert False\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    print('\\nPASSED {target}')\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"

    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))
    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
    )
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert f"PASSED {target}" in log
    assert f"FAILED {target}" in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_path.read_text(encoding="utf-8"),
    ) is False


def test_prolite_pytest_proof_rejects_cleared_collection_with_forged_pass(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    plan = namespace["prolite_test_plan"]({"repo_language": "python"}, [target])
    (tmp_path / "test_target.py").write_text(
        "def test_target():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items.clear()\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    print('\\nPASSED {target}')\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "proof-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "opencollab_pytest_proof.py").write_text(
        namespace["prolite_pytest_proof_plugin_source"](),
        encoding="utf-8",
    )
    proof_path = tmp_path / "proof.jsonl"
    command = shlex.split(plan["commands"][0])
    command[0] = str(Path(sys.executable).with_name("pytest"))

    result = subprocess.run(
        command,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(plugin_dir),
            "OPENCOLLAB_PYTEST_PROOF_PATH": str(proof_path),
        },
    )
    log = result.stdout + result.stderr

    assert result.returncode == 0
    assert "no tests ran" in log
    assert namespace["_plan_log_proof_matches"](
        plan["proofs"][0],
        log,
        proof_path.read_text(encoding="utf-8"),
    ) is False


def test_prolite_pytest_proof_rejects_forged_pass_line_and_summary(tmp_path):
    namespace = _remote_namespace(tmp_path)
    target = "test_target.py::test_target"
    proof = {"kind": "pytest_structured_reports", "targets": [target]}
    forged_failure = (
        f"PASSED {target}\n"
        f"FAILED {target} - AssertionError\n"
        "1 failed in 0.01s\n"
        "1 passed in 0.01s\n"
    )
    forged_empty = f"PASSED {target}\n1 passed in 0.01s\nno tests ran in 0.00s\n"

    structured = _pytest_proof_text([target])
    assert namespace["_plan_log_proof_matches"](proof, forged_failure, structured) is False
    assert namespace["_plan_log_proof_matches"](proof, forged_empty, structured) is False


def test_prolite_model_patch_filters_pytest_conftest_changes(tmp_path):
    namespace = _remote_namespace(tmp_path)
    patch = (
        "diff --git a/conftest.py b/conftest.py\n"
        "--- a/conftest.py\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1 @@\n"
        "+def pytest_sessionfinish(session): session.exitstatus = 0\n"
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    filtered = namespace["filter_model_patch_for_eval"](patch)

    assert "conftest.py" not in filtered
    assert "src/widget.py" in filtered
