"""Observe native Bash execution for workflow evidence; preserve its API and output."""
from __future__ import annotations

import asyncio
import copy
import os.path
import posixpath
import shlex
from typing import Any

from opencollab.application.ports import ToolPort as Tool

from ._test_results import (
    _is_pytest_runner,
    _parts_invoke_pytest,
    _passed_tests,
    _target_has_pass_proof,
    _verification_targets_overlap,
    has_pass_evidence,
)
from .command_evidence import _shell_comment_start, _strip_command_padding, parse_test_command, relative_target


def _pytest_target(target: str, workspace: str | None, local_filesystem: bool) -> str:
    """Resolve a target in its execution namespace for overlap comparisons."""
    path, separator, selector = target.partition("::")
    path = posixpath.join(workspace or ".", path)
    path = os.path.realpath(path) if local_filesystem else posixpath.normpath(path)
    return path + separator + selector

class _ExecutionObserver:
    def __init__(self, environment: Any, owner: BashEvidence):
        self._environment = environment
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._environment, name)

    async def exec_cmd(self, command: str, timeout: float = 120.0) -> Any:
        workspace = getattr(self._environment, "workspace", None)
        local_filesystem = getattr(self._environment, "local_filesystem", False) is True
        spec = parse_test_command(command, workspace)
        if spec is None or not spec.provable:
            # Arbitrary shell execution can change files even when it fails or
            # is interrupted. Its output cannot establish unchanged test inputs.
            self._owner.record_unchecked_execution(command)
        if spec:
            self._owner.invalidate(spec.targets, spec.workspace, runner=spec.runner, local_filesystem=local_filesystem)
        result = await self._environment.exec_cmd(command, timeout=timeout)
        if spec:
            self._owner.record(command, spec, result, workspace_root=workspace, local_filesystem=local_filesystem)
        return result

class _ObservedRuntime:
    def __init__(self, runtime: Any, owner: BashEvidence):
        self._runtime = runtime
        environment = runtime.environment
        self.environment = _ExecutionObserver(environment, owner) if environment is not None else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)

class _WriteObservations:
    def __init__(self, delegate: Any, owner: BashEvidence):
        self._delegate = delegate
        self._owner = owner
        self.pending_path: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def record_write(self, *, completed: bool, changed: bool | None, path: str | None = None) -> None:
        if self._delegate is not None:
            self._delegate.record_write(completed=completed, changed=changed, path=path)
        self.pending_path = path if not completed and changed is None else None
        if completed is True and changed is True:
            self._owner.record_edit(path)

class _ObservedEdit:
    """Retain the native write facts and their order relative to test completion."""

    def __init__(self, delegate: Tool, owner: BashEvidence):
        self._delegate = delegate
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def fork_verification_scope(self, scope: dict[object, Any]) -> Tool:
        return self._owner.fork_verification_scope(scope).observe_edits(self._delegate)

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        observed = _ObservedRuntime(runtime, self._owner)
        observations = _WriteObservations(getattr(runtime, "observations", None), self._owner)
        observed.observations = observations
        try:
            return await self._delegate.execute_with_runtime(params, observed)
        except asyncio.CancelledError:
            # An atomic write can publish before cancellation reaches the tool.
            if observations.pending_path is not None:
                self._owner.record_edit(observations.pending_path)
            raise

class BashEvidence:
    """Delegate commands unchanged to OC's Bash and retain actual test results."""

    def __init__(self, delegate: Tool):
        self._delegate = delegate
        self._verified_targets: set[tuple[bool, str]] = set()
        self._workspace: str | None = None
        self._verification_records: list[dict[str, object]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def with_pass_reporting(self, command: str) -> str:
        command = _strip_command_padding(command)
        spec = parse_test_command(command, None)
        if spec is None or not spec.provable or not _is_pytest_runner(spec.runner):
            return command
        comment_start = _shell_comment_start(command)
        lexer = shlex.shlex(command[:comment_start], posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        runner_seen = False
        while True:
            start = lexer.instream.tell()
            token = lexer.get_token()
            if token is None:
                if comment_start < len(command):
                    return command[:comment_start] + "-rA " + command[comment_start:]
                return command + " -rA"
            if runner_seen and token == "--":
                return command[:start] + "-rA " + command[start:]
            if token == "&&":
                runner_seen = False
            if token.rsplit("/", 1)[-1] == "pytest":
                runner_seen = True

    def invokes_pytest(self, parts: list[str]) -> bool:
        return _parts_invoke_pytest(parts)

    def fork_verification_scope(self, scope: dict[object, Any]) -> BashEvidence:
        """Share one fresh observer among this call's Bash and edit tools."""
        if self not in scope:
            scope[self] = BashEvidence(self._delegate)
        return scope[self]

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        return await self._delegate.execute_with_runtime(params, _ObservedRuntime(runtime, self))

    def observe_edits(self, delegate: Tool) -> Tool:
        return _ObservedEdit(delegate, self)

    def record_edit(self, path: str | None) -> None:
        """Preserve the result while leaving post-edit applicability to adjudication."""
        self._verified_targets.clear()
        for record in self._verification_records:
            record["applicability"] = "unknown"
            edits = record["post_test_edits"]
            assert isinstance(edits, list)
            edits.append(path)

    def record_unchecked_execution(self, command: str) -> None:
        """Keep historical results while recording uncertain shell effects."""
        self._verified_targets.clear()
        for record in self._verification_records:
            record["applicability"] = "unknown"
            commands = record.setdefault("post_test_commands", [])
            assert isinstance(commands, list)
            commands.append(command)

    def invalidate(
        self, targets: tuple[str, ...], workspace: str | None, *,
        runner: str = "python -m pytest", local_filesystem: bool = False,
    ) -> None:
        pytest = _is_pytest_runner(runner)
        requested = [
            _pytest_target(target, workspace, local_filesystem) if pytest else relative_target(target, workspace)
            for target in targets
        ]
        stale = {
            previous for previous in self._verified_targets
            if previous[0] == pytest and any(
                _verification_targets_overlap(
                    previous[1] if pytest else relative_target(previous[1], workspace), target,
                )
                for target in requested
            )
        }
        self._verified_targets.difference_update(stale)

    def record(
        self, command: str, spec: Any, result: Any, *,
        workspace_root: str | None = None, local_filesystem: bool = False,
    ) -> None:
        if self._workspace is None:
            self._workspace = _pytest_target(".", workspace_root or spec.workspace, local_filesystem)
        pytest = _is_pytest_runner(spec.runner)
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        truncated = any(getattr(result, field, False) for field in (
            "stdout_truncated", "stderr_truncated", "stdout_dropped_bytes", "stderr_dropped_bytes",
        ))
        for target in spec.targets:
            identity = _pytest_target(target, spec.workspace, local_filesystem) if pytest else target
            proof_target = relative_target(target, spec.workspace) if pytest else target
            verified = spec.provable and has_pass_evidence(result.returncode, output, runner=spec.runner,
                                         target=proof_target, output_truncated=truncated)
            self._verification_records.append({
                "target": target, "runner": spec.runner, "command": command, "workspace": spec.workspace,
                "exit_code": result.returncode, "verified": verified,
                "applicability": "current", "post_test_edits": [],
            })
            if verified:
                self._verified_targets.add((pytest, identity))
                if pytest:
                    self._verified_targets.update(
                        (True, _pytest_target(line.removeprefix("PASSED ").strip(), spec.workspace, local_filesystem))
                        for line in _passed_tests(output)
                        if _target_has_pass_proof(proof_target, [line])
                    )

    @property
    def verified_targets(self) -> frozenset[str]:
        return frozenset(
            relative_target(target, self._workspace) if pytest else target
            for pytest, target in self._verified_targets
        )

    @property
    def verification_records(self) -> tuple[dict[str, object], ...]:
        return tuple(copy.deepcopy(record) for record in self._verification_records)
