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

======================= short test summary info =======================
PASSED tests/test_x.py::test_one
PASSED tests/test_x.py::test_two
PASSED tests/test_x.py::test_three
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
PASSED tests/test_x.py::test_one
PASSED tests/test_x.py::test_three
FAILED tests/test_x.py::test_two - assert 2 == 3
===================== 1 failed, 2 passed in 0.06s =====================
"""

WARN_OUTPUT = """\
========================= test session starts =========================
collected 3 items

tests/test_x.py .F.                                              [100%]

=============================== FAILURES ===============================
______________________________ test_two ______________________________
tests/test_x.py:8: in test_two
    assert add(1, 1) == 3
E   assert 2 == 3
======================= short test summary info =======================
PASSED tests/test_x.py::test_one
PASSED tests/test_x.py::test_three
FAILED tests/test_x.py::test_two - assert 2 == 3
================= 1 failed, 2 passed, 3 warnings in 0.07s ==============
"""

COLLECTION_CRASH_OUTPUT = """\
========================= test session starts =========================
collected 0 items / 1 error

=============================== ERRORS ================================
ImportError while importing test module 'tests/test_x.py'.
Traceback (most recent call last):
  File "tests/test_x.py", line 1, in <module>
    import nope
ModuleNotFoundError: No module named 'nope'
"""


def test_run_tests_runs_pytest_and_returns_pass_summary():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

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


def test_run_tests_build_command_includes_determinism_flags():
    # -rA (per-test summary incl PASSED) and -p no:cacheprovider (determinism)
    # ride alongside the existing --tb=short -rfE -q.
    from opencollab.adapters.tools.run_tests import _build_command

    cmd = _build_command("python -m pytest", "tests/test_x.py::test_y", "")

    assert "--tb=short" in cmd and "-rfE" in cmd and "-q" in cmd
    assert "-rA" in cmd
    assert "-p no:cacheprovider" in cmd


def test_run_tests_excludes_warnings_from_counts_with_separate_line():
    # '1 failed, 2 passed, 3 warnings' -> Counts shows passed/failed only,
    # warnings on their own line; the pass/fail decision is unaffected.
    env = FakeEnv(stdout=WARN_OUTPUT, returncode=1)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

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
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

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
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime)
    )

    shown = [ln for ln in result.splitlines() if ln.startswith("  - PASSED ")]
    assert len(shown) == 25
    assert "  ... and 5 more" in result


def test_run_tests_collection_crash_falls_back_to_output():
    env = FakeEnv(stdout=COLLECTION_CRASH_OUTPUT, returncode=2)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime))

    assert "Exit code: 2" in result
    assert "ModuleNotFoundError: No module named 'nope'" in result


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

    expected = "bin/test -k smoke"
    assert env.exec_calls == [(expected, 30)]
    assert safety.cmd_calls == [(expected, None)]


def test_run_tests_can_disable_runner_override_before_exec():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        RunTestsTool(allow_runner_override=False).execute_with_runtime(
            {"runner": "python -c 'open(\"pwned\", \"w\").write(\"x\")'"},
            runtime,
        )
    )

    assert "runner override is disabled" in result
    assert "Omit `runner`" in result
    assert "Go go.mod" in result
    assert env.exec_calls == []


def test_run_tests_can_disable_extra_args_before_exec():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        RunTestsTool(allow_extra_args=False).execute_with_runtime(
            {"extra_args": "; touch pwned"},
            runtime,
        )
    )

    assert result == "Error: extra_args is disabled for this run_tests tool."
    assert env.exec_calls == []


def test_run_tests_pytest_path_emits_green_verdict():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime))
    assert "Verdict: GREEN" in result


def test_run_tests_pytest_failure_emits_red_verdict_and_hint():
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    result = run(RunTestsTool().execute_with_runtime({}, runtime))
    assert "Verdict: RED" in result
    assert "Hint (expected vs got):" in result


class ScriptedEnv:
    """Env returning queued ExecResults matched by command substring."""

    def __init__(self, responses):
        # responses: list of (substring, returncode, stdout)
        self._responses = responses
        self.exec_calls = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.exec_calls.append((cmd, timeout))
        for sub, rc, out in self._responses:
            if sub in cmd:
                return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")


def test_run_tests_falls_back_to_native_runner_when_pytest_missing():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -x bin/test", 0, ""),
        ("python bin/test", 0, "tests passed"),
    ])
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_two"}, runtime
        )
    )
    cmds = [c for c, _ in env.exec_calls]
    assert any("python bin/test" in c for c in cmds)
    native = next(c for c in cmds if "python bin/test" in c)
    assert "::" not in native and "test_two" in native
    assert "Verdict: GREEN" in result


def test_run_tests_falls_back_to_go_runner_when_pytest_missing():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -f go.mod", 0, ""),
        ("go test ./internal/server", 0, "ok\tmodule/internal/server\t0.01s"),
    ])
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {"target": "internal/server"}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    assert "go test ./internal/server" in cmds
    assert "runner override is disabled" not in result
    assert "Verdict: GREEN" in result


def test_run_tests_falls_back_to_go_runner_when_pytest_finds_no_tests():
    env = ScriptedEnv([
        ("python -m pytest", 5, "no tests ran in 0.01s"),
        ("test -f go.mod", 0, ""),
        ("go test ./internal/server", 0, "ok\tmodule/internal/server\t0.01s"),
    ])
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {"target": "internal/server"}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    assert "go test ./internal/server" in cmds
    assert "no tests ran" not in result
    assert "Verdict: GREEN" in result


def test_run_tests_native_fallback_quotes_translated_target():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -x bin/test", 0, ""),
        ("python bin/test", 0, "tests passed"),
    ])
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {"target": "tests/test_x.py::test_two; touch pwned"}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    native = next(c for c in cmds if "python bin/test" in c)
    assert native == "python bin/test tests/test_x.py 'test_two; touch pwned'"
    assert "Verdict: GREEN" in result


def test_run_tests_pinned_runner_suppresses_autodetect():
    env = ScriptedEnv([("bin/test", 0, "ok")])
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    run(RunTestsTool().execute_with_runtime({"runner": "bin/test"}, runtime))
    cmds = [c for c, _ in env.exec_calls]
    assert not any(c.startswith("test -x") for c in cmds)


def test_run_tests_native_green_without_summary_line():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -f tox.ini", 0, ""),
        ("tox", 0, "all environments succeeded"),
    ])
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    result = run(RunTestsTool().execute_with_runtime({}, runtime))
    assert "Verdict: GREEN" in result
    assert "could not parse" not in result


def test_run_tests_escalates_after_repeated_same_target_failures():
    tool = RunTestsTool()
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    last = ""
    for _ in range(3):
        last = run(tool.execute_with_runtime({"target": "tests/test_x.py"}, runtime))
    assert "Escalation:" in last
    green_env = FakeEnv(stdout=PASS_OUTPUT)
    green_runtime = ToolRuntime(environment=green_env, safety_policy=None, permission_policy=None)
    after = run(tool.execute_with_runtime({"target": "tests/test_x.py"}, green_runtime))
    assert "Escalation:" not in after


def test_build_command_native_runner_omits_pytest_flags_and_translates():
    from opencollab.adapters.tools.run_tests import _build_command
    cmd = _build_command("python bin/test", "tests/test_x.py::test_two", "")
    assert "--tb=short" not in cmd and "-rA" not in cmd
    assert "::" not in cmd and "test_two" in cmd


def test_build_command_native_runner_quotes_translated_target():
    from opencollab.adapters.tools.run_tests import _build_command
    cmd = _build_command("python bin/test", "tests/test_x.py::test_two; touch pwned", "")
    assert cmd == "python bin/test tests/test_x.py 'test_two; touch pwned'"


def test_build_command_go_runner_translates_package_and_test_name():
    from opencollab.adapters.tools.run_tests import _build_command
    cmd = _build_command("go test", "internal/server/evaluator_test.go::TestEvaluate", "")
    assert cmd == "go test ./internal/server -run TestEvaluate"


def test_run_tests_requires_execution_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert result == "Error: no execution environment available."
