"""Command building, runner selection and native-runner fallback for run_tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from run_tests_tool_test_support import (
    FAIL_OUTPUT,
    PASS_OUTPUT,
    PLAIN_PASS_OUTPUT,
    FakeEnv,
    SpySafetyPolicy,
    run,
    runtime_for,
)

from opencollab.adapters.tools.run_tests import RunTestsTool


def test_run_tests_build_command_includes_determinism_flags():
    # -rA (per-test summary incl PASSED) and -p no:cacheprovider (determinism)
    # ride alongside the existing --tb=short -rfE -q.
    from opencollab.adapters.tools.run_tests import _build_command

    cmd = _build_command("python -m pytest", "tests/test_x.py::test_y", "")

    assert "--tb=short" in cmd and "-rfE" in cmd and "-q" in cmd
    assert "-rA" in cmd
    assert "-p no:cacheprovider" in cmd


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


def test_run_tests_does_not_fallback_from_a_green_pytest_message():
    env = FakeEnv(
        stdout=PLAIN_PASS_OUTPUT + "\nNo module named pytest\n",
        returncode=0,
    )
    target = "tests/test_x.py::test_one"

    result = run(
        RunTestsTool().execute_with_runtime(
            {"target": target},
            runtime_for(env),
        )
    )

    assert "Verdict: GREEN" in result
    assert len(env.exec_calls) == 1


def test_run_tests_does_not_fallback_from_failed_test_output_text():
    env = FakeEnv(
        stdout=FAIL_OUTPUT + "\nNo module named pytest\n",
        returncode=1,
    )

    result = run(RunTestsTool().execute_with_runtime({}, runtime_for(env)))

    assert "Verdict: RED" in result
    assert len(env.exec_calls) == 1


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


def test_run_tests_no_tests_does_not_green_off_go_when_python_suite_present():
    # Mixed repo: a Python-native suite (bin/test) AND go.mod are both present,
    # and pytest collected nothing. bin/test is affirmative evidence that a real
    # Python suite exists that pytest simply did not collect, so falling back to
    # Go would green off unrelated Go tests while the intended suite never ran.
    env = ScriptedEnv([
        ("python -m pytest", 5, "no tests ran in 0.01s"),
        ("test -x bin/test", 0, ""),
        ("test -f go.mod", 0, ""),
        ("go test", 0, '{"Action":"pass","Package":"m","Test":"T"}'),
    ])
    runtime = runtime_for(env)

    result = run(
        RunTestsTool(allow_runner_override=False, allow_extra_args=False).execute_with_runtime(
            {}, runtime
        )
    )

    cmds = [c for c, _ in env.exec_calls]
    assert not any("go test" in c for c in cmds)
    assert "Verdict: GREEN" not in result


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


def test_run_tests_requires_execution_environment():
    runtime = runtime_for(None)

    result = run(RunTestsTool().execute_with_runtime({}, runtime))

    assert result == "Error: no execution environment available."
