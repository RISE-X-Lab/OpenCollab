import asyncio
from types import SimpleNamespace

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


def test_run_tests_runs_pytest_and_returns_pass_summary():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py::test_y"}, runtime))

    assert env.exec_calls == [
        ("python -m pytest --tb=short -rfE -q tests/test_x.py::test_y", 300.0)
    ]
    assert "Exit code: 0" in result
    assert "passed=3" in result
    assert "Failed/errored tests" not in result


def test_run_tests_returns_failure_summary_and_traceback_head():
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert "Exit code: 1" in result
    assert "failed=1" in result and "passed=2" in result
    assert "FAILED tests/test_x.py::test_two - assert 2 == 3" in result
    assert "First failure detail:" in result
    assert "assert add(1, 1) == 3" in result
    assert "short test summary info" not in result.split("First failure detail:")[1]


def test_run_tests_honors_runner_options_and_safety_policy():
    env = FakeEnv(stdout=PASS_OUTPUT)
    safety = SpySafetyPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=None)

    run(
        RunTestsTool().execute_with_runtime(
            {"runner": "bin/test", "extra_args": "-k smoke", "timeout": 30},
            runtime,
        )
    )

    assert env.exec_calls == [("bin/test --tb=short -rfE -q -k smoke", 30)]
    assert safety.cmd_calls == [("bin/test --tb=short -rfE -q -k smoke", None)]


def test_run_tests_requires_execution_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert result == "Error: no execution environment available."
