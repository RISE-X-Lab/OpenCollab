"""Native Bash execution and retained test evidence, including real commands."""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
from types import SimpleNamespace

import pytest

from opencollab import OpenCollab
from opencollab.adapters._env_base import ExecResult
from opencollab.environments import local_environment
from opencollab.tools import BashEvidence, builtin_tools, evidence_tools

TARGET = "tests/test_example.py::test_case"
PASS = f"PASSED {TARGET}\n1 passed in 0.01s"
COMMAND = f"python -m pytest -rA {TARGET}"


class Environment:
    workspace = "/work"
    process_isolated = True

    def __init__(self, output=PASS, code=0, *, truncated=False):
        self.result = ExecResult(code, output, "", truncated)
        self.calls = []

    async def exec_cmd(self, command, timeout=120):
        self.calls.append((command, timeout))
        return self.result


def runtime(env, policy=None):
    return SimpleNamespace(environment=env, safety_policy=policy, confirm_fn=lambda: None)


def execute(tool, env, command=COMMAND, **params):
    return asyncio.run(tool.execute_with_runtime({"command": command, **params}, runtime(env)))


def test_native_bash_schema_output_command_and_timeout_are_unchanged():
    native = builtin_tools("bash")[0]
    observed = evidence_tools("bash")[0]
    assert observed.to_openai_schema() == native.to_openai_schema()
    env, reference = Environment(), Environment()
    result = execute(observed, env, timeout=417)
    assert result == execute(native, reference, timeout=417)
    assert env.calls == reference.calls == [(COMMAND, 417)]
    assert observed.verified_targets == {TARGET}
    assert observed.verification_records[-1]["command"] == COMMAND
    assert "Verdict:" not in result


def test_removed_tool_is_unavailable():
    with pytest.raises(ValueError, match="unsupported built-in"):
        evidence_tools("run_tests")


def test_native_project_command_runs_without_runner_detection():
    env = Environment("build completed")
    tool = evidence_tools("bash")[0]
    assert "build completed" in execute(tool, env, "npm test -- --runInBand")
    assert env.calls == [("npm test -- --runInBand", 120.0)]
    assert not tool.verified_targets


@pytest.mark.parametrize("command", [
    "true", "echo 'PASSED tests/test_example.py::test_case'", "cat saved-test-output.txt",
    "sh -c 'python -m pytest'", "python -c 'print(1)'", "python -m pytest; true",
    "python -m pytest | cat", "python -m pytest > output.txt", "python -m pytest && echo fake",
], ids=["noop", "echo", "replay", "shell-wrapper", "inline-python", "sequence", "pipeline", "redirect", "compound"])
def test_shell_output_forgery_cannot_create_test_evidence(command):
    env = Environment()
    tool = evidence_tools("bash")[0]
    execute(tool, env, command)
    assert env.calls == [(command, 120.0)]
    assert not tool.verified_targets
    assert not tool.verification_records


@pytest.mark.parametrize("output", [
    "", "usage: pytest [options]", "1 test collected in 0.01s", "no tests ran in 0.01s",
    "PASSED tests/test_other.py::test_case\n1 passed in 0.01s", "1 passed in 0.01s",
    PASS + "\n1 failed in 0.01s", PASS + "\n" + PASS,
])
def test_missing_or_ambiguous_execution_cannot_pass(output):
    tool = evidence_tools("bash")[0]
    execute(tool, Environment(output))
    assert not tool.verified_targets
    assert tool.verification_records[-1]["verified"] is False


@pytest.mark.parametrize("failure", ["failed", "truncated", "collected", "help", "exception"])
def test_failed_or_unverified_rerun_invalidates_previous_pass(failure):
    tool = evidence_tools("bash")[0]
    execute(tool, Environment(), command=f"python -m pytest -rA /work/{TARGET}")
    assert TARGET in tool.verified_targets
    env = Environment()
    command = COMMAND
    if failure == "failed":
        env.result = ExecResult(1, "1 failed in 0.01s", "")
    elif failure == "truncated":
        env.result = ExecResult(0, PASS, "", True)
    elif failure == "collected":
        command += " --collect-only"
    elif failure == "help":
        command += " --help"
    else:
        async def raises(*args, **kwargs):
            raise TimeoutError("test interrupted")
        env.exec_cmd = raises
    if failure == "exception":
        with pytest.raises(TimeoutError):
            execute(tool, env, command)
    else:
        execute(tool, env, command)
    assert not tool.verified_targets


def test_directory_evidence_requires_a_descendant_pass():
    tool = evidence_tools("bash")[0]
    execute(tool, Environment(), "python -m pytest -rA tests")
    assert {"tests", TARGET} <= tool.verified_targets
    other = evidence_tools("bash")[0]
    execute(other, Environment(), "python -m pytest -rA test")
    assert not other.verified_targets


def test_multiple_pytest_targets_require_each_actual_result():
    tool = evidence_tools("bash")[0]
    other = "tests/test_example.py::test_other"
    execute(tool, Environment(), f"{COMMAND} {other}")
    assert TARGET in tool.verified_targets
    assert other not in tool.verified_targets
    assert [r["verified"] for r in tool.verification_records] == [True, False]


def test_explicit_directory_and_absolute_target_use_same_execution_root():
    command = f"cd -- /work/subdir && python -m pytest -rA /work/subdir/{TARGET}"
    env = Environment()
    tool = evidence_tools("bash")[0]
    execute(tool, env, command)
    assert env.calls == [(command, 120.0)]
    assert TARGET in tool.verified_targets
    assert tool.verification_records[0]["workspace"] == "/work/subdir"


def test_go_duplicate_selector_cannot_create_ambiguous_evidence():
    env = Environment(json.dumps({"Action": "pass", "Package": "module/pkg1", "Test": "TestB"}))
    tool = evidence_tools("bash")[0]
    command = "go test -json ./pkg1 ./pkg2 -run TestA -run TestB"
    execute(tool, env, command)
    assert env.calls == [(command, 120.0)]
    assert not tool.verified_targets
    assert all(not record["verified"] for record in tool.verification_records)


def test_go_command_requires_correct_named_test():
    tool = evidence_tools("bash")[0]
    command = "go test -json ./pkg1 -run '^TestA$'"
    execute(tool, Environment('{"Action":"pass","Package":"module/pkg1","Test":"TestB"}'), command)
    assert not tool.verified_targets
    execute(tool, Environment('{"Action":"pass","Package":"module/pkg1","Test":"TestA"}'), command)
    assert tool.verified_targets == {"./pkg1::TestA"}


async def test_sandbox_and_command_approval_remain_native():
    env = Environment()
    env.process_isolated = False
    tool = evidence_tools("bash")[0]
    result = await tool.execute_with_runtime({"command": COMMAND}, runtime(env))
    assert "disabled" in result and not env.calls
    env.process_isolated = True
    class Policy:
        async def check_cmd_interactive(self, cmd, confirm):
            assert cmd == COMMAND
            raise PermissionError("denied")
    with pytest.raises(PermissionError):
        await tool.execute_with_runtime({"command": COMMAND}, runtime(env, Policy()))
    assert not env.calls


async def test_real_pytest_pass_then_failure_and_arbitrary_native_shell(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    path = tests / "test_example.py"
    path.write_text("def test_case(): assert 2 + 2 == 4\n")
    env = local_environment(str(tmp_path))
    await env.setup()
    tool = evidence_tools("bash", headless=False)[0]
    command = f"{shlex.quote(sys.executable)} -B -m pytest -p no:cacheprovider -rA {TARGET}"
    try:
        result = await tool.execute_with_runtime({"command": command}, runtime(env))
        assert "Exit code: 0" in result and "1 passed" in result
        assert TARGET in tool.verified_targets
        path.write_text("def test_case(): assert 20 + 20 == 1\n")
        result = await tool.execute_with_runtime({"command": command}, runtime(env))
        assert "Exit code: 1" in result and "1 failed" in result
        assert not tool.verified_targets
        result = await tool.execute_with_runtime({"command": "printf native-command-ok"}, runtime(env))
        assert "native-command-ok" in result
    finally:
        await env.cleanup()


@pytest.mark.parametrize("option", ["-k no_matching_test_name", "--collect-only", "--help"])
async def test_real_empty_collection_and_help_invalidate_previous_pass(tmp_path, option):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("def test_case(): assert True\n")
    env = local_environment(str(tmp_path))
    await env.setup()
    tool = evidence_tools("bash", headless=False)[0]
    command = f"{shlex.quote(sys.executable)} -B -m pytest -p no:cacheprovider -rA {TARGET}"
    try:
        await tool.execute_with_runtime({"command": command}, runtime(env))
        assert TARGET in tool.verified_targets
        await tool.execute_with_runtime({"command": command + " " + option}, runtime(env))
        assert not tool.verified_targets
        assert tool.verification_records[-1]["verified"] is False
    finally:
        await env.cleanup()


async def test_evidence_composition_retains_profile_and_explicit_native_configuration(tmp_path):
    async def flow(ctx, inputs):
        observed = evidence_tools("bash", "file_write", headless=False, allow_file_creation=False)
        assert isinstance(observed[0], BashEvidence)
        assert observed[0].max_output_chars == 10_000
        assert observed[0].require_process_isolation is False
        assert observed[1].allow_create is False
        explicit = evidence_tools("bash", limits={"bash": {"max_output_chars": 1234}})[0]
        assert explicit.max_output_chars == 1234
        assert explicit.require_process_isolation is True
        return observed[0].verified_targets

    result = await OpenCollab(tmp_path).workflow(flow, agent_profile="single2", trace=False)
    assert result.ok and result.output == frozenset()
    assert evidence_tools("bash")[0].max_output_chars == builtin_tools("bash")[0].max_output_chars == 8_000


def test_each_observer_retains_independent_records_and_returns_copies():
    first, second = evidence_tools("bash")[0], evidence_tools("bash")[0]
    execute(first, Environment())
    copied = first.verification_records[0]
    copied["verified"] = False
    assert first.verification_records[0]["verified"] is True
    assert not second.verification_records and not second.verified_targets
