"""Observe native Bash execution for workflow evidence; preserve its API and output."""
from __future__ import annotations

from typing import Any

from opencollab.application.ports import ToolPort as Tool

from ._test_results import (
    _is_pytest_runner,
    _passed_tests,
    _target_has_pass_proof,
    _verification_targets_overlap,
    has_pass_evidence,
)
from .command_evidence import parse_test_command, relative_target


class _ExecutionObserver:
    def __init__(self, environment: Any, owner: BashEvidence):
        self._environment = environment
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._environment, name)

    async def exec_cmd(self, command: str, timeout: float = 120.0) -> Any:
        spec = parse_test_command(command, getattr(self._environment, "workspace", None))
        if spec:
            self._owner.invalidate(spec.targets, spec.workspace)
        result = await self._environment.exec_cmd(command, timeout=timeout)
        if spec:
            self._owner.record(command, spec, result)
        return result


class _ObservedRuntime:
    def __init__(self, runtime: Any, owner: BashEvidence):
        self._runtime = runtime
        environment = runtime.environment
        self.environment = _ExecutionObserver(environment, owner) if environment is not None else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)


class BashEvidence:
    """Delegate commands unchanged to OC's Bash and retain actual test results."""

    def __init__(self, delegate: Tool):
        self._delegate = delegate
        self._verified_targets: set[str] = set()
        self._verification_records: list[dict[str, object]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        return await self._delegate.execute_with_runtime(params, _ObservedRuntime(runtime, self))

    def invalidate(self, targets: tuple[str, ...], workspace: str | None) -> None:
        stale = {
            previous for previous in self._verified_targets
            if any(
                _verification_targets_overlap(relative_target(previous, workspace), relative_target(target, workspace))
                for target in targets
            )
        }
        self._verified_targets.difference_update(stale)

    def record(self, command: str, spec: Any, result: Any) -> None:
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        truncated = any(getattr(result, field, False) for field in (
            "stdout_truncated", "stderr_truncated", "stdout_dropped_bytes", "stderr_dropped_bytes",
        ))
        for target in spec.targets:
            proof_target = relative_target(target, spec.workspace) if _is_pytest_runner(spec.runner) else target
            verified = spec.provable and has_pass_evidence(result.returncode, output, runner=spec.runner,
                                         target=proof_target, output_truncated=truncated)
            self._verification_records.append({
                "target": target, "runner": spec.runner, "command": command, "workspace": spec.workspace,
                "exit_code": result.returncode, "verified": verified,
            })
            if verified:
                self._verified_targets.add(target)
                if _is_pytest_runner(spec.runner):
                    self._verified_targets.update(
                        line.removeprefix("PASSED ").strip() for line in _passed_tests(output)
                        if _target_has_pass_proof(proof_target, [line])
                    )

    @property
    def verified_targets(self) -> frozenset[str]:
        return frozenset(self._verified_targets)

    @property
    def verification_records(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(record) for record in self._verification_records)
