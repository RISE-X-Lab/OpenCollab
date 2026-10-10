"""Native tools with execution observations for the weave workflow."""

from __future__ import annotations

import math
import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from opencollab.tools import evidence_tools, profile_tool_limits, profile_tool_names


class _Proxy:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _WriteObserver(_Proxy):
    def __init__(self, delegate: Any, observed: Callable[[str | None], None]) -> None:
        super().__init__(delegate)
        self._observed = observed

    def record_write(self, *, completed: bool, changed: bool | None, path: str | None = None) -> None:
        if self._delegate is not None:
            self._delegate.record_write(completed=completed, changed=changed, path=path)
        if completed and changed is True:
            self._observed(path)


class _ProgressTool(_Proxy):
    def __init__(self, delegate: Any, observed: Callable[[str | None], None]) -> None:
        super().__init__(delegate)
        self._observed = observed

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        observed = _Proxy(runtime)
        observed.observations = _WriteObserver(getattr(runtime, "observations", None), self._observed)
        before = len(getattr(self._delegate, "verification_records", ()))
        result = await self._delegate.execute_with_runtime(params, observed)
        records = getattr(self._delegate, "verification_records", ())[before:]
        if any(record.get("verified") for record in records):
            self._observed(None)
        return result


def _coding_tools(observed: Callable[[str | None], None]) -> tuple[Any, ...]:
    tools = evidence_tools(
        *profile_tool_names("single2"), headless=False, limits=profile_tool_limits("single2"),
    )
    return tuple(_ProgressTool(tool, observed) for tool in tools)


class _CaptureEnvironment(_Proxy):
    def __init__(self, delegate: Any, results: list[Any]) -> None:
        super().__init__(delegate)
        self._results = results

    async def exec_cmd(self, command: str, timeout: float = 120.0) -> Any:
        result = await self._delegate.exec_cmd(command, timeout=timeout)
        self._results.append(result)
        return result


class _CommandProbe(_Proxy):
    def __init__(self) -> None:
        super().__init__(evidence_tools("bash", headless=False)[0])
        self.results: list[Any] = []

    async def execute_with_runtime(self, params: dict[str, Any], runtime: Any) -> str:
        captured = _Proxy(runtime)
        environment = runtime.environment
        captured.environment = _CaptureEnvironment(environment, self.results) if environment is not None else None
        return await self._delegate.execute_with_runtime(params, captured)


def _command_spec(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if not isinstance(value, Mapping) or set(value) - {"command", "expected_output"}:
        raise ValueError("each check command must be text or a command/expected_output object")
    command, expected = value.get("command"), value.get("expected_output")
    if not isinstance(command, str) or not isinstance(expected, str) or not expected.strip():
        raise ValueError("a command probe requires command text and nonempty expected_output")
    return command, expected


def _verification_mode(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(tokens) and not any(
        token in {"-h", "--help", "--collect-only", "--co", "--version"}
        or token.startswith("--collect-only=") for token in tokens
    ) and tokens[0] not in {"true", ":", "echo", "printf"}


async def _verify_commands(ctx: Any, commands: Sequence[Any], seconds: float | None) -> dict[str, Any]:
    deadline = math.inf if seconds is None else time.monotonic() + seconds
    checks = []
    for value in commands:
        command, expected = _command_spec(value)
        row: dict[str, Any] = {"command": command, "ok": False, "executed": False}
        remaining = deadline - time.monotonic()
        if not _verification_mode(command) or remaining <= 0:
            row["reason"] = "unverified command or exhausted verification time"
            checks.append(row)
            continue
        tool = _CommandProbe()
        params: dict[str, Any] = {"command": command}
        if math.isfinite(remaining):
            params["timeout"] = remaining
        row["output"] = await ctx.execute_verification(tool, params)
        records = list(tool.verification_records)
        row["test_records"] = records
        if tool.results:
            result = tool.results[-1]
            row["executed"] = True
            row["exit_code"] = result.returncode
            truncated = any(getattr(result, key, False) for key in (
                "stdout_truncated", "stderr_truncated", "stdout_dropped_bytes", "stderr_dropped_bytes",
            ))
            if records:
                row["ok"] = all(record.get("verified") is True for record in records)
            elif expected is not None:
                output = result.stdout + ("\n" + result.stderr if result.stderr else "")
                row["expected_output"] = expected
                row["matched"] = expected in output
                row["ok"] = result.returncode == 0 and not truncated and not getattr(result, "timed_out", False) \
                    and row["matched"]
            else:
                row["reason"] = "command requires parsed test evidence or an explicit expected_output probe"
        checks.append(row)
    return {
        "ok": bool(checks) and all(row["ok"] for row in checks),
        "executed": bool(checks) and all(row["executed"] for row in checks),
        "checks": checks,
    }
