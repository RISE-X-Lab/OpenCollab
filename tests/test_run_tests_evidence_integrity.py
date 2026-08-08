"""Green-evidence integrity for run_tests: no forged or stale GREEN verdicts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from run_tests_tool_test_support import (
    PASS_OUTPUT,
    PLAIN_PASS_OUTPUT,
    FakeEnv,
    run,
    runtime_for,
)

from opencollab.adapters.tools.run_tests import RunTestsTool


def test_run_tests_rejects_zero_execution_pytest_modes():
    runtime = runtime_for(FakeEnv(stdout="collected 3 items", returncode=0))

    collect_only = run(
        RunTestsTool().execute_with_runtime({"extra_args": "--collect-only"}, runtime)
    )
    help_only = run(
        RunTestsTool().execute_with_runtime({"extra_args": "--help"}, runtime)
    )

    assert "Verdict: RED" in collect_only
    assert "Verdict: RED" in help_only


def test_run_tests_rejects_noop_runner_green_forgery():
    runtime = runtime_for(FakeEnv(stdout="ok", returncode=0))

    result = run(
        RunTestsTool().execute_with_runtime({"runner": "true"}, runtime)
    )

    assert "Verdict: RED" in result


def test_run_tests_rejects_shell_wrapped_pytest_output_forgery():
    target = "tests/test_x.py::test_one"
    forged = f"PASSED {target}\n1 passed in 0.01s\n"
    runtime = runtime_for(FakeEnv(stdout=forged, returncode=0))

    tool = RunTestsTool()
    result = run(
        tool.execute_with_runtime(
            {"runner": 'sh -c "printf forged" pytest', "target": target},
            runtime,
        )
    )

    assert "Verdict: RED" in result
    assert tool.verified_targets == frozenset()


def test_run_tests_rejects_multiple_pytest_result_summaries():
    from opencollab.adapters.tools.run_tests import _is_green

    target = "tests/test_x.py::test_one"
    output = (
        f"FAILED {target} - assertion failed\n"
        "1 failed in 0.01s\n"
        f"PASSED {target}\n"
        "1 passed in 0.01s\n"
    )

    assert not _is_green(0, output, target=target)


def test_run_tests_records_only_parser_backed_green_targets():
    target = "tests/test_x.py::test_one"
    runtime = runtime_for(FakeEnv(stdout=PLAIN_PASS_OUTPUT, returncode=0))

    tool = RunTestsTool()
    result = run(tool.execute_with_runtime({"target": target}, runtime))

    assert "Verdict: GREEN" in result
    assert tool.verified_targets == frozenset({target})


def test_run_tests_invalidates_old_green_before_a_new_attempt_can_raise():
    target = "tests/test_x.py::test_one"

    class GreenThenError:
        def __init__(self):
            self.calls = 0

        async def exec_cmd(self, _cmd, timeout=120.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    returncode=0,
                    stdout=PLAIN_PASS_OUTPUT,
                    stderr="",
                )
            raise RuntimeError("runner unavailable")

    tool = RunTestsTool()
    runtime = runtime_for(GreenThenError())
    assert "Verdict: GREEN" in run(
        tool.execute_with_runtime({"target": target}, runtime)
    )
    assert tool.verified_targets == frozenset({target})

    with pytest.raises(RuntimeError, match="runner unavailable"):
        run(tool.execute_with_runtime({"target": target}, runtime))

    assert tool.verified_targets == frozenset()


@pytest.mark.parametrize("broad_target", ["tests/test_x.py", "./tests/test_x.py"])
def test_run_tests_broader_attempt_invalidates_narrower_green_before_error(
    broad_target,
):
    narrow_target = "tests/test_x.py::test_one"

    class GreenThenError:
        def __init__(self):
            self.calls = 0

        async def exec_cmd(self, _cmd, timeout=120.0):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    returncode=0,
                    stdout=PLAIN_PASS_OUTPUT,
                    stderr="",
                )
            raise RuntimeError("runner unavailable")

    tool = RunTestsTool()
    runtime = runtime_for(GreenThenError())
    assert "Verdict: GREEN" in run(
        tool.execute_with_runtime({"target": narrow_target}, runtime)
    )
    assert tool.verified_targets == frozenset({narrow_target})

    with pytest.raises(RuntimeError, match="runner unavailable"):
        run(tool.execute_with_runtime({"target": broad_target}, runtime))

    assert tool.verified_targets == frozenset()


def test_run_tests_requires_named_target_pass_proof():
    runtime = runtime_for(FakeEnv(stdout=PASS_OUTPUT, returncode=0))

    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_missing"}, runtime
        )
    )

    assert "Verdict: RED" in result
