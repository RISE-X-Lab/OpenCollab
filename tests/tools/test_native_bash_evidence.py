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

@pytest.mark.parametrize("command, expected", [
    ("python -m pytest tests.py -q", "python -m pytest tests.py -q -rA"),
    ('python -m pytest -k "$SELECTION" "test file.py" -q',
     'python -m pytest -k "$SELECTION" "test file.py" -q -rA'),
    ('python -m pytest -q -- "test file.py"', 'python -m pytest -q -rA -- "test file.py"'),
    ('cd pytest && env -- python -m pytest -q -- test.py',
     'cd pytest && env -- python -m pytest -q -rA -- test.py'),
    ('uv run -- python -m pytest -q test.py', 'uv run -- python -m pytest -q test.py -rA'),
    ('echo Starting && python -m pytest -q test.py', 'echo Starting && python -m pytest -q test.py'),
    ('python -m pytest test.py > report.txt', 'python -m pytest test.py > report.txt'),
    ('python -m pytest test.py && echo finished', 'python -m pytest test.py && echo finished'),
    ('python -m pytest --collect-only', 'python -m pytest --collect-only'),
    ('python -m pytest --help', 'python -m pytest --help'),
    ('python -m pytest tests.py -q # keep this note',
     'python -m pytest tests.py -q -rA # keep this note'),
    ("python -m pytest tests.py --junit-prefix '#suite'",
     "python -m pytest tests.py --junit-prefix '#suite' -rA"),
    (r"python -m pytest tests.py --junit-prefix \#suite",
     r"python -m pytest tests.py --junit-prefix \#suite -rA"),
])
def test_opt_in_pass_reporting_preserves_shell_text_and_target_separator(command, expected):
    tool = evidence_tools("bash")[0]
    assert tool.with_pass_reporting(command) == expected

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
    assert tool.verified_targets == {f"subdir/{TARGET}"}
    assert tool.verification_records[0]["workspace"] == "/work/subdir"

@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("outcome", ["failed", "error", "empty", "collected", "help", "truncated", "exception"])
def test_pytest_rerun_across_workspaces_invalidates_the_same_target(reverse, outcome):
    tool = evidence_tools("bash")[0]
    nested_target = "test_example.py::test_case"
    nested_command = f"cd tests && python -m pytest -rA {nested_target}"
    first_command, second_command = (nested_command, COMMAND) if reverse else (COMMAND, nested_command)
    first_output, second_output = (PASS.replace(TARGET, nested_target), PASS) if reverse else (
        PASS, PASS.replace(TARGET, nested_target),
    )
    execute(tool, Environment(first_output), first_command)
    assert tool.verified_targets == {TARGET}
    failed = Environment(second_output)
    if outcome in {"failed", "error"}:
        failed.result = ExecResult(1 if outcome == "failed" else 2, "1 error in 0.01s", "")
    elif outcome == "empty":
        failed.result = ExecResult(5, "no tests ran in 0.01s", "")
    elif outcome in {"collected", "help"}:
        second_command += " --collect-only" if outcome == "collected" else " --help"
    elif outcome == "truncated":
        failed.result = ExecResult(0, second_output, "", True)
    else:
        async def interrupted(*args, **kwargs):
            raise TimeoutError("test interrupted")
        failed.exec_cmd = interrupted
    if outcome == "exception":
        with pytest.raises(TimeoutError):
            execute(tool, failed, second_command)
        assert len(tool.verification_records) == 1
    else:
        execute(tool, failed, second_command)
        assert tool.verification_records[-1]["verified"] is False
        assert tool.verification_records[-1]["exit_code"] == failed.result.returncode
    assert not tool.verified_targets
    assert tool.verification_records[0]["verified"] is True
    assert tool.verification_records[0]["exit_code"] == 0
    assert tool.verification_records[0]["command"] == first_command
    assert tool.verification_records[0]["target"] == (nested_target if reverse else TARGET)
    assert tool.verification_records[0]["workspace"] == ("/work/tests" if reverse else "/work")

@pytest.mark.parametrize("first_target", [".", "test_example.py"])
def test_pytest_same_names_in_different_directories_keep_independent_passes(first_target):
    tool = evidence_tools("bash")[0]
    output = "PASSED test_example.py::test_case\n1 passed in 0.01s"
    command = "python -m pytest -rA"
    execute(tool, Environment(output), f"cd alpha && {command} {first_target}")
    alpha = tool.verified_targets
    assert alpha == {"alpha" if first_target == "." else "alpha/test_example.py", "alpha/test_example.py::test_case"}
    execute(tool, Environment(output), f"cd beta && {command} test_example.py")
    assert tool.verified_targets == alpha | {"beta/test_example.py", "beta/test_example.py::test_case"}
    execute(tool, Environment(code=1), f"cd alpha && {command} test_example.py")
    assert tool.verified_targets == {"beta/test_example.py", "beta/test_example.py::test_case"}

@pytest.mark.parametrize("local_filesystem", [False, True])
def test_pytest_host_aliases_are_resolved_only_for_local_environments(tmp_path, local_filesystem):
    directory = tmp_path / "project"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    env = Environment()
    env.workspace = str(directory)
    env.local_filesystem = local_filesystem
    tool = evidence_tools("bash")[0]
    execute(tool, env)
    failed = Environment(code=1)
    failed.workspace = str(directory)
    failed.local_filesystem = local_filesystem
    execute(tool, failed, f"python -m pytest -rA {shlex.quote(str(alias / TARGET))}")
    assert bool(tool.verified_targets) is not local_filesystem

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

@pytest.mark.parametrize("command,target,output", [
    ("go test -json ./pkg1 -run TestA", "./pkg1::TestA",
     '{"Action":"pass","Package":"module/pkg1","Test":"TestA"}'),
    ("python tests/runtests.py admin_utils.tests.Example.test_one", "admin_utils.tests.Example.test_one",
     "test_one (admin_utils.tests.Example.test_one) ... ok\n\nRan 1 test in 0.001s\n\nOK\n"),
])
def test_non_pytest_targets_retain_runner_identity_in_local_workspaces(tmp_path, command, target, output):
    tool = evidence_tools("bash")[0]
    env = Environment(output)
    env.workspace = str(tmp_path)
    env.local_filesystem = True
    execute(tool, env, command)
    assert tool.verified_targets == {target}
    env.result = ExecResult(1, "failed", "")
    execute(tool, env, command)
    assert not tool.verified_targets
    assert [record["target"] for record in tool.verification_records] == [target, target]
    assert [record["verified"] for record in tool.verification_records] == [True, False]

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

@pytest.mark.parametrize("color", ["no", "yes"])
async def test_real_pytest_same_names_keep_the_unrelated_directory_pass(tmp_path, color):
    for name in ("alpha", "beta"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "test_example.py").write_text(
            "import os\ndef test_case(): assert os.environ.get('RUN_MODE') != 'fail'\n"
        )
    env = local_environment(str(tmp_path))
    await env.setup()
    tool = evidence_tools("bash", headless=False)[0]
    command = f"{shlex.quote(sys.executable)} -B -m pytest -p no:cacheprovider -rA --color={color} test_example.py"
    try:
        for name in ("alpha", "beta"):
            output = await tool.execute_with_runtime({"command": f"cd {name} && {command}"}, runtime(env))
            assert "Exit code: 0" in output and "1 passed" in output
            assert ("\x1b[" in output) == (color == "yes")
        assert tool.verified_targets == {
            "alpha/test_example.py", "alpha/test_example.py::test_case",
            "beta/test_example.py", "beta/test_example.py::test_case",
        }
        output = await tool.execute_with_runtime({"command": f"cd alpha && RUN_MODE=fail {command}"}, runtime(env))
        assert "Exit code: 1" in output and "1 failed" in output
        assert tool.verified_targets == {"beta/test_example.py", "beta/test_example.py::test_case"}
        assert [record["exit_code"] for record in tool.verification_records] == [0, 0, 1]
        assert [record["verified"] for record in tool.verification_records] == [True, True, False]
    finally:
        await env.cleanup()

@pytest.mark.parametrize("file_alias", [False, True])
@pytest.mark.parametrize("absolute", [False, True])
async def test_real_pytest_alias_spelling_proves_the_physical_target(tmp_path, file_alias, absolute):
    directory = tmp_path / "tests"
    directory.mkdir()
    path = directory / "test_example.py"
    path.write_text("import os\ndef test_case(): assert os.environ.get('RUN_MODE') != 'fail'\n")
    alias = tmp_path / ("test_alias.py" if file_alias else "alias")
    alias.symlink_to(path if file_alias else directory, target_is_directory=not file_alias)
    target = alias if file_alias else alias / "test_example.py"
    target = str(target if absolute else target.relative_to(tmp_path))
    env = local_environment(str(tmp_path))
    await env.setup()
    tool = evidence_tools("bash", headless=False)[0]
    command = f"{shlex.quote(sys.executable)} -B -m pytest -p no:cacheprovider -rA"
    try:
        output = await tool.execute_with_runtime({"command": f"{command} {shlex.quote(target)}"}, runtime(env))
        assert "Exit code: 0" in output and "1 passed" in output
        assert tool.verified_targets == {"tests/test_example.py", "tests/test_example.py::test_case"}
        record, = tool.verification_records
        assert record["target"] == target and record["verified"] is True
        output = await tool.execute_with_runtime(
            {"command": f"RUN_MODE=fail {command} tests/test_example.py"}, runtime(env),
        )
        assert "Exit code: 1" in output and "1 failed" in output
        assert not tool.verified_targets
    finally:
        await env.cleanup()

async def test_real_pytest_system_alias_pass_is_invalidated_by_the_canonical_path(tmp_path):
    workspace = str(tmp_path.resolve())
    alias = next((alternate + workspace[len(canonical):] for canonical, alternate in (
        ("/private/var", "/var"), ("/private/tmp", "/tmp"),
    ) if workspace.startswith(canonical + "/")), None)
    if sys.platform != "darwin" or alias is None:
        pytest.skip("requires a macOS temporary directory with a system alias")
    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "test_example.py").write_text(
        "import os\ndef test_case(): assert os.environ.get('RUN_MODE') != 'fail'\n"
    )
    env = local_environment(workspace)
    await env.setup()
    tool = evidence_tools("bash", headless=False)[0]
    command = f"{shlex.quote(sys.executable)} -B -m pytest -p no:cacheprovider -rA"
    target = f"{alias}/{TARGET}"
    try:
        output = await tool.execute_with_runtime({"command": f"{command} {shlex.quote(target)}"}, runtime(env))
        assert "Exit code: 0" in output and "1 passed" in output
        assert tool.verified_targets == {TARGET}
        assert tool.verification_records[0]["target"] == target
        output = await tool.execute_with_runtime({"command": f"RUN_MODE=fail {command} {TARGET}"}, runtime(env))
        assert "Exit code: 1" in output and "1 failed" in output
        assert not tool.verified_targets
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

@pytest.mark.parametrize("command", ["printf changed > source.py", "cat source.py", "python -m pytest && true"])
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
def test_later_unverified_shell_leaves_historical_applicability_unknown(command, outcome):
    tool = evidence_tools("bash")[0]
    execute(tool, Environment())
    later = Environment(code=1 if outcome == "failure" else 0)
    if outcome == "exception":
        async def interrupted(*args, **kwargs):
            raise TimeoutError("shell interrupted")
        later.exec_cmd = interrupted
        with pytest.raises(TimeoutError):
            execute(tool, later, command)
    else:
        execute(tool, later, command)
    record = tool.verification_records[0]
    assert record["exit_code"] == 0 and record["verified"] is True
    assert record["applicability"] == "unknown"
    assert record["post_test_commands"] == [command]
    assert not tool.verified_targets
    execute(tool, Environment())
    assert tool.verification_records[-1]["applicability"] == "current"
    assert TARGET in tool.verified_targets
