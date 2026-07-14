import asyncio
from types import SimpleNamespace

import pytest
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def runtime_for(environment, *, safety_policy=None):
    return ToolRuntime(
        environment=environment,
        safety_policy=safety_policy,
        permission_policy=None,
    )


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

PLAIN_PASS_OUTPUT = """\
.                                                                        [100%]
==================================== PASSES ====================================
=========================== short test summary info ============================
PASSED tests/test_x.py::test_one
1 passed in 0.04s
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


def test_run_tests_honors_runner_options_and_safety_policy():
    env = FakeEnv(stdout=PASS_OUTPUT)
    safety = SpySafetyPolicy()
    runtime = runtime_for(env, safety_policy=safety)

    result = run(
        RunTestsTool().execute_with_runtime(
            {"runner": "bin/test", "extra_args": "-k smoke", "timeout": 30},
            runtime,
        )
    )

    assert "Command: not executed" in result
    assert "Verdict: RED" in result
    assert env.exec_calls == []
    assert safety.cmd_calls == []


def test_run_tests_can_disable_runner_override_before_exec():
    env = FakeEnv(stdout=PASS_OUTPUT)
    runtime = runtime_for(env)

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
    runtime = runtime_for(env)

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
    runtime = runtime_for(env)
    result = run(RunTestsTool().execute_with_runtime({"target": "tests/test_x.py"}, runtime))
    assert "Verdict: GREEN" in result


def test_run_tests_pytest_failure_emits_red_verdict_and_hint():
    env = FakeEnv(stdout=FAIL_OUTPUT, returncode=1)
    runtime = runtime_for(env)
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
    ])
    runtime = runtime_for(env)
    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_two"}, runtime
        )
    )
    cmds = [c for c, _ in env.exec_calls]
    assert not any("python bin/test" in c for c in cmds)
    assert "Verdict: RED" in result
    assert "Command: not executed" in result
    assert "without an executed-target proof parser" in result


def test_run_tests_falls_back_to_go_runner_when_pytest_missing():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -f go.mod", 0, ""),
        (
            "go test -json ./internal/server",
            0,
            '{"Action":"pass","Package":"module/internal/server",'
            '"Test":"TestEvaluate"}',
        ),
    ])
    runtime = runtime_for(env)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {"target": "internal/server"}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    assert any("go test -json ./internal/server" in c for c in cmds)
    assert "runner override is disabled" not in result
    assert "Verdict: GREEN" in result


def test_run_tests_falls_back_to_go_runner_when_pytest_finds_no_tests():
    env = ScriptedEnv([
        ("python -m pytest", 5, "no tests ran in 0.01s"),
        ("test -f go.mod", 0, ""),
        (
            "go test -json ./internal/server",
            0,
            '{"Action":"pass","Package":"module/internal/server",'
            '"Test":"TestEvaluate"}',
        ),
    ])
    runtime = runtime_for(env)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {"target": "internal/server"}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    assert any("go test -json ./internal/server" in c for c in cmds)
    assert "no tests ran" not in result
    assert "Verdict: GREEN" in result


def test_run_tests_native_fallback_quotes_translated_target():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -x bin/test", 0, ""),
    ])
    runtime = runtime_for(env)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {"target": "tests/test_x.py::test_two; touch pwned"}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    assert not any("python bin/test" in c for c in cmds)
    assert "Command: not executed" in result
    assert "Verdict: RED" in result


def test_run_tests_pinned_runner_suppresses_autodetect():
    env = ScriptedEnv([("bin/test", 0, "ok")])
    runtime = runtime_for(env)
    run(RunTestsTool().execute_with_runtime({"runner": "bin/test"}, runtime))
    cmds = [c for c, _ in env.exec_calls]
    assert cmds == []
    assert not any(c.startswith("test -x") for c in cmds)


def test_run_tests_native_exit_zero_without_proof_is_red():
    env = ScriptedEnv([
        ("python -m pytest", 1, "No module named pytest"),
        ("test -f tox.ini", 0, ""),
    ])
    runtime = runtime_for(env)
    result = run(RunTestsTool().execute_with_runtime({}, runtime))
    assert "Verdict: RED" in result
    assert "Command: not executed" in result
    assert not any(command.startswith("tox") for command, _timeout in env.exec_calls)


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


def test_build_command_rejects_native_runner_without_proof_parser():
    from opencollab.adapters.tools.run_tests import _build_command
    with pytest.raises(ValueError, match="without proof parser"):
        _build_command("python bin/test", "tests/test_x.py::test_two", "")


@pytest.mark.parametrize(
    "runner",
    [
        "uv run pytest",
        "poetry run pytest",
        "pipenv run pytest",
        "coverage run -m pytest",
        "env PYTHONHASHSEED=0 python -m pytest",
        "python -X dev -m pytest",
    ],
)
def test_build_command_supports_safe_pytest_wrappers(runner):
    from opencollab.adapters.tools.run_tests import _build_command, _is_green

    target = "tests/test_x.py::test_two"
    cmd = _build_command(runner, target, "")
    output = f"PASSED {target}\n1 passed in 0.01s\n"

    assert cmd.endswith(f"-q {target}")
    assert _is_green(0, output, runner=runner, target=target)


@pytest.mark.parametrize(
    "runner",
    [
        'sh -c "pytest tests/test_x.py"',
        'python -c "print(\"1 passed in 0.01s\")" -m pytest',
        "env --ignore-environment pytest",
        "uv tool run pytest",
    ],
)
def test_pytest_runner_rejects_unsupported_or_shell_wrappers(runner):
    from opencollab.adapters.tools.run_tests import _is_pytest_runner

    assert not _is_pytest_runner(runner)


def test_build_command_go_runner_translates_package_and_test_name():
    from opencollab.adapters.tools.run_tests import _build_command
    cmd = _build_command("go test", "internal/server/evaluator_test.go::TestEvaluate", "")
    assert cmd == (
        "PATH=/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:$PATH "
        "go test -json ./internal/server -run TestEvaluate"
    )


def test_build_command_go_runner_translates_multiple_packages():
    from opencollab.adapters.tools.run_tests import _build_command
    cmd = _build_command("go test", "./internal/server/... ./rpc/flipt/...", "-count=1")
    assert cmd == (
        "PATH=/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:$PATH "
        "go test -json ./internal/server/... ./rpc/flipt/... -count=1"
    )


def test_build_command_go_runner_treats_bare_target_as_package():
    from opencollab.adapters.tools.run_tests import _build_command, _is_green

    cmd = _build_command("go test", "TestEvaluate", "")
    output = (
        '{"Action":"pass","Package":"module/internal/server","Test":"TestEvaluate"}'
    )

    assert cmd.endswith("go test -json ./TestEvaluate")
    assert " -run " not in cmd
    assert not _is_green(0, output, runner="go test", target="TestEvaluate")


def test_go_runner_requires_pass_proof_from_requested_package():
    from opencollab.adapters.tools.run_tests import _is_green

    unrelated_output = (
        '{"Action":"pass","Package":"module/internal/other","Test":"TestEvaluate"}'
    )
    matching_output = (
        '{"Action":"pass","Package":"module/internal/server","Test":"TestEvaluate"}'
    )

    assert not _is_green(
        0,
        unrelated_output,
        runner="go test",
        target="internal/server",
    )
    assert _is_green(
        0,
        matching_output,
        runner="go test",
        target="internal/server",
    )


def test_go_runner_requires_pass_proof_for_each_requested_package():
    from opencollab.adapters.tools.run_tests import _is_green

    one_package = (
        '{"Action":"pass","Package":"module/internal/server","Test":"TestEvaluate"}'
    )
    both_packages = "\n".join(
        [
            one_package,
            '{"Action":"pass","Package":"module/rpc/flipt","Test":"TestRPC"}',
        ]
    )
    target = "./internal/server ./rpc/flipt"

    assert not _is_green(0, one_package, runner="go test", target=target)
    assert _is_green(0, both_packages, runner="go test", target=target)


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


def test_run_tests_requires_named_target_pass_proof():
    runtime = runtime_for(FakeEnv(stdout=PASS_OUTPUT, returncode=0))

    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": "tests/test_x.py::test_missing"}, runtime
        )
    )

    assert "Verdict: RED" in result


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


def test_run_tests_requires_execution_environment():
    runtime = runtime_for(None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert result == "Error: no execution environment available."
