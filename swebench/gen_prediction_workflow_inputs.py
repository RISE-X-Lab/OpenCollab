"""Prompt and hidden-test input policy for SWE-bench workflow generation."""

from __future__ import annotations

import json

BLIND_BY_DEFAULT_WORKFLOWS = {"validation-council-solve", "swe-committee-v2"}


def _fail_to_pass_ids(instance: dict) -> list[str]:
    """Parse the FAIL_TO_PASS node ids from their JSON or list form."""
    fail_to_pass = instance.get("FAIL_TO_PASS", "[]")
    if isinstance(fail_to_pass, str):
        fail_to_pass = json.loads(fail_to_pass)
    return list(fail_to_pass)


def build_task(instance: dict, *, include_fail_to_pass: bool = True) -> str:
    """Build the solver prompt, optionally including official grading ids."""
    problem = instance["problem_statement"]
    hints = (instance.get("hints_text") or "").strip()
    hints_block = (
        f"\n## Hints (from the issue discussion — may help locate the cause)\n{hints}\n"
        if hints
        else ""
    )
    if include_fail_to_pass:
        fail_to_pass = _fail_to_pass_ids(instance)
        tests = "\n".join(f"- {target}" for target in fail_to_pass)
        tests_block = (
            f"## Tests that must pass after your fix\n{tests or '- (project test suite)'}\n"
            "Note: some of these tests may not exist yet at this commit — they are "
            "added by the grading harness. Do not be surprised if you cannot run "
            "them; verify the fixed behavior directly instead.\n\n"
        )
    else:
        tests_block = (
            "## Blind validation mode\n"
            "Do not use official hidden tests, injected grader patches, or "
            "FAIL_TO_PASS node ids. Infer validation only from the issue text, "
            "repository code, public tests, and public documentation.\n\n"
        )
    return (
        f"# Issue to fix in `{instance['repo']}`\n\n"
        f"{problem}\n{hints_block}\n"
        f"{tests_block}"
        "Locate the root cause in the source, apply a minimal fix, and ensure "
        "the behavior described above is satisfied."
    )


def build_extras(instance: dict, *, include_hidden_tests: bool = True) -> dict:
    """Build EvalTask extras, optionally withholding official grading data."""
    if not include_hidden_tests:
        return {"blind_validation": True}
    return {
        "test_patch": instance.get("test_patch") or "",
        "fail_to_pass": _fail_to_pass_ids(instance),
    }


def _blind_validation_default(
    workflow_name: str,
    explicit: bool | None,
) -> bool:
    if explicit is not None:
        return explicit
    return workflow_name in BLIND_BY_DEFAULT_WORKFLOWS


def _workflow_name(workflow_fn, workflow_label: str | None = None) -> str:
    if workflow_label:
        return workflow_label
    spec = getattr(workflow_fn, "__workflow_spec__", None)
    name = getattr(spec, "name", None)
    if name:
        return str(name)
    return getattr(workflow_fn, "__name__", "")


def _resolve_blind_validation(
    workflow_fn,
    explicit: bool | None,
    workflow_label: str | None = None,
) -> bool:
    return _blind_validation_default(
        _workflow_name(workflow_fn, workflow_label),
        explicit,
    )
