from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def _load_workflow(name: str):
    path = _WORKFLOWS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_run_tests_controls", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "workflow_name",
    [
        "analyst_solve",
        "scout_solve",
        "self_collab",
        "split_solve",
        "swe_committee_v2",
        "validation_council_solve",
    ],
)
def test_workflow_model_roles_cannot_override_test_commands(workflow_name):
    module = _load_workflow(workflow_name)

    for factory_name in ("_coder_tools", "_tester_tools"):
        tools = getattr(module, factory_name)()
        run_tests = next(tool for tool in tools if tool.name == "run_tests")
        assert run_tests.allow_runner_override is False
        assert run_tests.allow_extra_args is False
