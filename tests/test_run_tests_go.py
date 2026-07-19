import asyncio
from types import SimpleNamespace

import pytest

from opencollab.adapters.tools.run_tests import (
    GO_PATH_PREFIX,
    RunTestsTool,
    _build_command,
    _go_target_specs,
    _is_green,
)
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


class FakeEnv:
    def __init__(self, stdout="", returncode=0):
        self._stdout = stdout
        self._rc = returncode
        self.exec_calls = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.exec_calls.append((cmd, timeout))
        return SimpleNamespace(returncode=self._rc, stdout=self._stdout, stderr="")


class SpySafetyPolicy:
    def __init__(self):
        self.cmd_calls = []

    async def check_cmd_interactive(self, cmd: str, confirm_fn=None) -> None:
        self.cmd_calls.append((cmd, confirm_fn))


def test_go_path_prefix_remains_importable_from_run_tests_facade():
    assert GO_PATH_PREFIX.startswith("PATH=")


def test_go_runner_rejects_multiple_targets_when_any_has_selector():
    targets = [
        "./pkg1::TestA ./pkg2::TestB",
        "./pkg1::TestA ./pkg2",
        "  ./pkg1::TestA   ./pkg2::TestB  ",
        "'./pkg1::TestA ./pkg2::TestB'",
    ]

    for target in targets:
        with pytest.raises(ValueError, match="selector"):
            _go_target_specs(target)
        with pytest.raises(ValueError, match="selector"):
            _build_command("go test", target, "")


@pytest.mark.parametrize(
    "target",
    ["'./pkg1", "::TestA", "./pkg1::", "./pkg1::TestA::Subtest", "-run"],
)
def test_go_runner_rejects_malformed_targets(target):
    with pytest.raises(ValueError):
        _build_command("go test", target, "")


def test_go_runner_multi_target_forgery_cannot_produce_green():
    forged_output = '{"Action":"pass","Package":"module/pkg1","Test":"TestB"}'

    assert not _is_green(
        0,
        forged_output,
        runner="go test",
        target="./pkg1::TestA ./pkg2::TestB",
    )


def test_run_tests_rejects_go_multi_selector_before_any_command():
    target = "./pkg1::TestA ./pkg2::TestB"
    env = FakeEnv(stdout='{"Action":"pass","Package":"module/pkg1","Test":"TestB"}')
    safety_policy = SpySafetyPolicy()
    runtime = ToolRuntime(
        environment=env,
        safety_policy=safety_policy,
        permission_policy=None,
    )
    tool = RunTestsTool()
    tool._verified_targets.add(target)

    result = run(
        tool.execute_with_runtime(
            {"runner": "go test", "target": target},
            runtime,
        )
    )

    assert "Command: not executed" in result
    assert "multiple targets containing a test selector" in result
    assert "Verdict: RED" in result
    assert env.exec_calls == []
    assert safety_policy.cmd_calls == []
    assert tool.verified_targets == frozenset()
    assert tool._consecutive_fail[target] == 1


def test_run_tests_auto_detect_rejects_go_multi_selector_before_probe():
    target = "./pkg1::TestA ./pkg2::TestB"
    env = FakeEnv(stdout="no tests ran in 0.01s", returncode=5)
    runtime = ToolRuntime(
        environment=env,
        safety_policy=None,
        permission_policy=None,
    )

    result = run(RunTestsTool().execute_with_runtime({"target": target}, runtime))

    assert "Verdict: RED" in result
    assert env.exec_calls == []


def test_go_runner_single_selector_requires_exact_test_proof():
    wrong_test = '{"Action":"pass","Package":"module/pkg1","Test":"TestB"}'
    exact_test = '{"Action":"pass","Package":"module/pkg1","Test":"TestA"}'
    subtest = '{"Action":"pass","Package":"module/pkg1","Test":"TestA/subcase"}'
    target = "./pkg1::TestA"

    assert not _is_green(0, wrong_test, runner="go test", target=target)
    assert _is_green(0, exact_test, runner="go test", target=target)
    assert _is_green(0, subtest, runner="go test", target=target)
