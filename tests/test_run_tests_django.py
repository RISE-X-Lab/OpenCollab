"""Django source tests run without pytest and require matching verbose proof."""

import sys

import pytest
from run_tests_tool_test_support import runtime_for

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools._run_tests_django import DJANGO_RUNNER, django_command
from opencollab.adapters.tools.run_tests import RunTestsTool, _is_green

PASS = "test_one (admin_utils.tests.Example.test_one) ... ok\n\nRan 1 test in 0.001s\n\nOK\n"


@pytest.mark.parametrize("target", ["admin_utils", "tests/admin_utils", "admin_utils.tests.Example.test_one",
                                   "tests/admin_utils/tests.py::Example::test_one"])
def test_django_accepts_matching_executed_labels(target):
    assert _is_green(0, PASS, runner=DJANGO_RUNNER, target=target)


@pytest.mark.parametrize("output", [
    "Ran 0 tests in 0.001s\nOK\n", "Ran 1 test in 0.001s\nOK\n",
    PASS.replace(" ... ok", " ... skipped 'disabled'").replace("OK", "OK (skipped=1)"),
    PASS.replace(" ... ok", " ... FAIL").replace("OK", "FAILED (failures=1)"),
    PASS + "Ran 0 tests in 0.01s\nOK\n", PASS.replace("Ran 1 test", "Ran 2 tests"),
    PASS + "FAILED (errors=1)\n", "usage: runtests.py --help\n", PASS + PASS,
])
def test_django_rejects_missing_ambiguous_or_nonpassing_execution(output):
    assert not _is_green(0, output, runner=DJANGO_RUNNER, target="admin_utils")


def test_django_rejects_wrong_target_and_nonzero_exit():
    assert not _is_green(0, PASS, runner=DJANGO_RUNNER, target="auth_tests")
    assert not _is_green(1, PASS, runner=DJANGO_RUNNER, target="admin_utils")


@pytest.mark.parametrize(
    "target", ["--help", "admin_utils auth_tests", "../../other", "admin_utils; true", "/admin_utils"]
)
def test_django_command_rejects_flags_and_multiple_targets(target):
    with pytest.raises(ValueError, match="Django target"):
        django_command(DJANGO_RUNNER, target)


async def test_native_source_runner_executes_without_override_or_pytest(tmp_path):
    (tmp_path / "django").mkdir()
    (tmp_path / "django/__init__.py").write_text("# source marker\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "admin_utils.py").write_text(
        "import unittest\nclass Example(unittest.TestCase):\n"
        "    def test_one(self): self.assertEqual(2 + 2, 4)\n"
    )
    (tests / "runtests.py").write_text(
        "import argparse, unittest, sys\n"
        "p = argparse.ArgumentParser()\np.add_argument('--verbosity', type=int)\n"
        "p.add_argument('--parallel')\np.add_argument('label')\na = p.parse_args()\n"
        "suite = unittest.defaultTestLoader.loadTestsFromName(a.label)\n"
        "result = unittest.TextTestRunner(verbosity=a.verbosity).run(suite)\n"
        "sys.exit(not result.wasSuccessful())\n"
    )
    env = LocalEnvironment(workspace=str(tmp_path))
    original = env.exec_cmd
    commands = []

    async def execute(command, timeout=120):
        commands.append(command)
        # Use the test interpreter without changing discovery's environment.
        if command.startswith("python "):
            command = repr(sys.executable) + command[len("python"):]
        return await original(command, timeout=timeout)

    env.exec_cmd = execute
    tool = RunTestsTool(allow_runner_override=False, allow_extra_args=False)
    try:
        report = await tool.execute_with_runtime({"target": "admin_utils"}, runtime_for(env))
        assert "Verdict: GREEN" in report
        assert "passed=1" in report
        assert tool.verified_targets == {"admin_utils"}
        assert not any("pytest" in cmd for cmd in commands)
        assert "python tests/runtests.py --verbosity 2 --parallel 1 admin_utils" in commands
        (tests / "admin_utils.py").write_text(
            "import unittest\nclass Example(unittest.TestCase):\n"
            "    def test_one(self): self.fail('regression')\n"
        )
        failed = await tool.execute_with_runtime({"target": "admin_utils"}, runtime_for(env))
        assert "Verdict: RED" in failed
        assert "failed=1" in failed
        assert not tool.verified_targets
        invalid = await tool.execute_with_runtime({"target": "--help"}, runtime_for(env))
        assert "Command: not executed" in invalid
        assert "Verdict: RED" in invalid
    finally:
        await env.cleanup()
