import asyncio
from types import SimpleNamespace

from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    def __init__(self, stdout="", stderr="", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self._rc = returncode
        self.exec_calls = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.exec_calls.append((cmd, timeout))
        return SimpleNamespace(returncode=self._rc, stdout=self._stdout, stderr=self._stderr)


class SpySafetyPolicy:
    def __init__(self):
        self.cmd_calls = []

    def check_path(self, target_path: str) -> str:
        return target_path

    async def check_cmd_interactive(self, cmd: str, confirm_fn=None) -> None:
        self.cmd_calls.append((cmd, confirm_fn))


PASS_OUTPUT = """\
========================= test session starts =========================
collected 3 items

tests/test_x.py ...                                              [100%]

========================== 3 passed in 0.05s ==========================
"""

FAIL_OUTPUT = """\
========================= test session starts =========================
collected 3 items

tests/test_x.py .F.                                              [100%]

=============================== FAILURES ===============================
______________________________ test_two ______________________________
tests/test_x.py:8: in test_two
    assert add(1, 1) == 3
E   assert 2 == 3
======================= short test summary info =======================
FAILED tests/test_x.py::test_two - assert 2 == 3
===================== 1 failed, 2 passed in 0.06s =====================
"""


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_run_tests_builds_default_pytest_command():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py::test_y"}, runtime))

    cmd, timeout = env.exec_calls[0]
    # shlex.quote leaves path/node-ids untouched (no shell-unsafe chars).
    assert cmd == "python -m pytest --tb=short -rfE -q tests/test_x.py::test_y"
    assert timeout == 300.0


def test_run_tests_honors_runner_and_extra_args_and_timeout():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    run(
        RunTestsTool().execute_with_runtime(
            {"runner": "bin/test", "extra_args": "-k smoke", "timeout": 30}, runtime
        )
    )

    cmd, timeout = env.exec_calls[0]
    assert cmd == "bin/test --tb=short -rfE -q -k smoke"
    assert timeout == 30


def test_run_tests_passes_command_through_safety_policy():
    env = FakeEnv(stdout=PASS_OUTPUT)
    safety = SpySafetyPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=None)

    run(RunTestsTool().execute_with_runtime({}, runtime))

    assert len(safety.cmd_calls) == 1
    assert safety.cmd_calls[0][0].startswith("python -m pytest")


# ---------------------------------------------------------------------------
# Structured parsing
# ---------------------------------------------------------------------------


def test_run_tests_reports_passing_counts():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert "Exit code: 0" in result
    assert "passed=3" in result
    assert "3 passed in" in result
    assert "Failed/errored tests" not in result
    assert "First failure detail" not in result


def test_run_tests_reports_failures_with_node_ids_and_traceback():
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert "Exit code: 1" in result
    assert "failed=1" in result and "passed=2" in result
    assert "FAILED tests/test_x.py::test_two - assert 2 == 3" in result
    assert "First failure detail:" in result
    assert "assert add(1, 1) == 3" in result
    # The short summary section is trimmed out of the traceback head.
    assert "short test summary info" not in result.split("First failure detail:")[1]


def test_run_tests_handles_missing_runner():
    env = FakeEnv(stderr="No module named pytest", returncode=1)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert "test runner not found" in result


def test_run_tests_without_env_errors():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert result == "Error: no execution environment available."


def test_run_tests_gate_denies_risky_runner_override(tmp_path):
    """A risky runner override is gated: a denying confirm aborts before exec."""
    env = FakeEnv(stdout=PASS_OUTPUT)
    sandbox = SandboxInterceptor(str(tmp_path))

    class DenyingPolicy:
        async def confirm(self, prompt: str) -> bool:
            return False

    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=DenyingPolicy())

    import pytest

    with pytest.raises(PermissionError):
        run(RunTestsTool().execute_with_runtime({"runner": "rm -rf build"}, runtime))
    assert env.exec_calls == []


def test_run_tests_has_native_execute_with_runtime():
    assert RunTestsTool.execute_with_runtime is not Tool.execute_with_runtime
