"""Shared doubles and sample pytest output for the run_tests tool tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
