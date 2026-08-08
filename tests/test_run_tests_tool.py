"""Pytest output parsing and pass/fail summary reporting for run_tests."""

from __future__ import annotations

from types import SimpleNamespace

from run_tests_tool_test_support import (
    COLLECTION_CRASH_OUTPUT,
    FAIL_OUTPUT,
    PASS_OUTPUT,
    PLAIN_PASS_OUTPUT,
    WARN_OUTPUT,
    FakeEnv,
    run,
    runtime_for,
)

from opencollab.adapters.tools.run_tests import RunTestsTool


def test_run_tests_runs_pytest_and_returns_pass_summary():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = runtime_for(env)

    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py::test_y"}, runtime))

    assert env.exec_calls == [
        (
            "python -m pytest --tb=short -rfE -rA -p no:cacheprovider -q "
            "tests/test_x.py::test_y",
            300.0,
        )
    ]
    assert "Exit code: 0" in result
    assert "passed=3" in result
    assert "Failed/errored tests" not in result
    # Focused run lists the PASSED node-ids so a gate can confirm a NAMED test.
    assert "Passed tests:" in result
    assert "  - PASSED tests/test_x.py::test_one" in result
    assert "  - PASSED tests/test_x.py::test_three" in result


def test_run_tests_accepts_plain_pytest_q_summary():
    env = FakeEnv(stdout=PLAIN_PASS_OUTPUT)
    runtime = runtime_for(env)

    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_one"},
            runtime,
        )
    )

    assert "passed=1" in result
    assert "Verdict: GREEN" in result


def test_run_tests_accepts_pass_proof_from_retained_capture_tail():
    class TruncatedOutputEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0):
            self.exec_calls.append((cmd, timeout))
            return SimpleNamespace(
                returncode=0,
                stdout="captured head\n" + PLAIN_PASS_OUTPUT,
                stderr="",
                stdout_truncated=True,
                stderr_truncated=False,
                stdout_dropped_bytes=1_000_000,
                stderr_dropped_bytes=0,
            )

    runtime = runtime_for(TruncatedOutputEnv())

    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_one"},
            runtime,
        )
    )

    assert "passed=1" in result
    assert "Verdict: GREEN" in result


def test_run_tests_directory_target_requires_a_descendant_pass():
    output = PLAIN_PASS_OUTPUT.replace(
        "tests/test_x.py::test_one",
        "tests/unit/test_x.py::test_one",
    )
    runtime = runtime_for(FakeEnv(stdout=output))

    result = run(
        RunTestsTool().execute_with_runtime({"target": "tests/unit"}, runtime)
    )

    assert "Verdict: GREEN" in result

    root_runtime = runtime_for(FakeEnv(stdout=output))
    root_result = run(
        RunTestsTool().execute_with_runtime({"target": "."}, root_runtime)
    )
    assert "Verdict: GREEN" in root_result

    unrelated_runtime = runtime_for(FakeEnv(stdout=output))
    unrelated = run(
        RunTestsTool().execute_with_runtime({"target": "tests/unitized"}, unrelated_runtime)
    )
    assert "Verdict: RED" in unrelated


def test_run_tests_preserves_spaces_in_parameterized_node_id_proof():
    output = PLAIN_PASS_OUTPUT.replace(
        "tests/test_x.py::test_one",
        "tests/test_x.py::test_one[x y]",
    )
    runtime = runtime_for(FakeEnv(stdout=output))

    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_one[x y]"},
            runtime,
        )
    )

    assert "Verdict: GREEN" in result


def test_run_tests_returns_failure_summary_and_traceback_head():
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = runtime_for(env)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert "Exit code: 1" in result
    assert "failed=1" in result and "passed=2" in result
    assert "FAILED tests/test_x.py::test_two - assert 2 == 3" in result
    assert "First failure detail:" in result
    assert "assert add(1, 1) == 3" in result
    assert "short test summary info" not in result.split("First failure detail:")[1]


def test_run_tests_excludes_warnings_from_counts_with_separate_line():
    # '1 failed, 2 passed, 3 warnings' -> Counts shows passed/failed only,
    # warnings on their own line; the pass/fail decision is unaffected.
    env = FakeEnv(stdout=WARN_OUTPUT, returncode=1)
    runtime = runtime_for(env)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    counts_line = next(ln for ln in result.splitlines() if ln.startswith("Counts:"))
    assert "passed=2" in counts_line and "failed=1" in counts_line
    assert "warning" not in counts_line.lower()
    assert "Warnings: 3 (not failures)" in result
    # Decision is driven by exit code + failed/error counts, not the warnings.
    assert "Exit code: 1" in result
    assert "failed=1" in result


def test_run_tests_full_suite_suppresses_passed_list():
    # No target -> full-suite run: PASSED list is suppressed to protect context,
    # only the aggregate count is reported.
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = runtime_for(env)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert "passed=3" in result
    assert "Passed tests:" not in result
    assert "PASSED tests/test_x.py::test_one" not in result


def test_run_tests_caps_passed_list_at_25():
    passed_lines = "\n".join(
        f"PASSED tests/test_x.py::test_{i}" for i in range(30)
    )
    output = (
        "========================= test session starts =========================\n"
        "collected 30 items\n\n"
        "======================= short test summary info =======================\n"
        f"{passed_lines}\n"
        "========================= 30 passed in 0.10s =========================\n"
    )
    env = FakeEnv(stdout=output)
    runtime = runtime_for(env)

    result = run(
        RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime)
    )

    shown = [ln for ln in result.splitlines() if ln.startswith("  - PASSED ")]
    assert len(shown) == 25
    assert "  ... and 5 more" in result


def test_run_tests_collection_crash_falls_back_to_output():
    env = FakeEnv(stdout=COLLECTION_CRASH_OUTPUT, returncode=2)
    runtime = runtime_for(env)

    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime))

    assert "Exit code: 2" in result
    assert "ModuleNotFoundError: No module named 'nope'" in result


def test_run_tests_pytest_path_emits_green_verdict():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = runtime_for(env)
    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime))
    assert "Verdict: GREEN" in result


def test_run_tests_pytest_failure_emits_red_verdict_and_hint():
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = runtime_for(env)
    result = run(RunTestsTool().execute_with_runtime({}, runtime))
    assert "Verdict: RED" in result
    assert "Hint (expected vs got):" in result


def test_run_tests_escalates_after_repeated_same_target_failures():
    tool = RunTestsTool()
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = runtime_for(env)
    last = ""
    for _ in range(3):
        last = run(tool.execute_with_runtime({"target": "tests/test_x.py"}, runtime))
    assert "Escalation:" in last
    green_env = FakeEnv(stdout=PASS_OUTPUT)
    green_runtime = runtime_for(green_env)
    after = run(tool.execute_with_runtime({"target": "tests/test_x.py"}, green_runtime))
    assert "Escalation:" not in after


def test_go_runner_command_not_found_is_not_reported_as_pytest_missing():
    from opencollab.adapters.tools.run_tests import _format_report
    result = _format_report(
        "go test ./...",
        127,
        "bash: line 2: go: command not found",
        runner="go test",
        green=False,
    )
    assert "pytest not found" not in result
    assert "go: command not found" in result
    assert "Verdict: RED" in result
