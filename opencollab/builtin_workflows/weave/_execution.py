"""Native tools with execution observations for the weave workflow."""

from __future__ import annotations

import math
import re
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

    def prepare_command(self, command: str) -> str:
        return self._delegate.with_pass_reporting(command)

    def requires_test_evidence(self, command: str) -> bool:
        return any(self._delegate.invokes_pytest(_command_words(shlex.split(segment)))
                   for segment in _command_segments(command))

def _command_spec(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if not isinstance(value, Mapping) or set(value) - {"command", "expected_output"}:
        raise ValueError("each check command must be text or a command/expected_output object")
    command, expected = value.get("command"), value.get("expected_output")
    if not isinstance(command, str) or not isinstance(expected, str) or not expected.strip():
        raise ValueError("a command probe requires command text and nonempty expected_output")
    return command, expected

def _command_segments(command: str) -> list[str]:
    quote = None
    escaped = False
    start = 0
    segments = []
    for index, char in enumerate(command):
        if index < start:
            continue
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char == quote:
            quote = None
        elif quote is None and char in {"'", '"'}:
            quote = char
        elif quote is None and char in "(){}":
            return []
        elif quote is None and char == "#" and (index == 0 or command[index - 1].isspace()):
            if any(line.strip() for line in command[index:].splitlines()[1:]):
                return []
            command = command[:index]
            break
        elif quote != "'" and (char == "`" or command[index:index + 2] == "$("):
            return []
        elif quote is None and command[index:index + 2] == "&&":
            if command[start:index].strip():
                segments.append(command[start:index])
            start = index + 2
        elif quote is None and command[index:index + 3] == "<<<":
            return []
        elif quote is None and char in ";|&":
            return []
        elif quote is None and char in "\n\r" and command[:index].strip() and command[index:].strip():
            return []
    if command[start:].strip():
        segments.append(command[start:])
    return segments

_WRAPPERS = frozenset({
    "exec", "eval", "nice", "nohup", "time", "timeout", "setsid", "sudo",
    "ionice", "chrt", "taskset", "flock", "xargs", "stdbuf", "busybox",
    "unbuffer", "script",
})
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash"})
_STATIC_COMMANDS = frozenset({"true", "false", ":", "echo", "printf", "test", "["})
_TEXT_TOOLS = frozenset({"awk", "gawk", "nawk", "mawk", "sed", "jq"})
_PY_INLINE = re.compile(r"-[bBdEihIOPqRsSuvVx?]*c")
_PERL_INLINE = re.compile(r"-[nplad]*[eE]")
_RUBY_INLINE = re.compile(r"-[npl]*e")


def _command_basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _effective_tokens(tokens: list[str]) -> list[str] | None:
    """Return the program argv, or None when a wrapper can hide a printer.

    Assignments and ``env`` / ``command`` / ``builtin`` are skipped, as before.
    Their options stay in place, so ``env -i python`` remains unverified.
    Shells accept a literal script path with execution options, while inline
    commands and wrappers remain unverified.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        variable, separator, _ = token.partition("=")
        if separator and variable.isidentifier():
            index += 1
            continue
        name = _command_basename(token)
        if name in {"env", "command", "builtin", "--"}:
            index += 1
            continue
        if name in _WRAPPERS:
            return None
        if name in _SHELLS and not _shell_file_script(tokens[index + 1:]):
            return None
        return tokens[index:]
    return []


def _shell_file_script(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option == "--":
            index += 1
            break
        if not option.startswith("-"):
            break
        if re.fullmatch(r"-[eux]*o", option):
            index += 1
            if index == len(arguments) or arguments[index] not in {"errexit", "nounset", "xtrace", "pipefail"}:
                return False
        elif not re.fullmatch(r"-[eux]+", option):
            return False
        index += 1
    return index < len(arguments) and bool(arguments[index]) and not arguments[index].startswith("-") and not any(
        char in arguments[index] for char in "$<>"
    )


def _command_words(tokens: list[str]) -> list[str]:
    words = _effective_tokens(tokens)
    return [] if words is None else words

def _has_behavior_command(tokens: list[str]) -> bool:
    words = _effective_tokens(tokens)
    if not words or words[0].startswith("-"):
        return False
    name = _command_basename(words[0])
    return name not in _STATIC_COMMANDS and name not in _TEXT_TOOLS


def _static_printer_supplies(command: str, expected: str) -> bool:
    """True when a printer or one-liner already contains the expected text.

    ``echo CHECK_OK && python -c 'exit(0)'`` would otherwise match CHECK_OK
    from the printer while the second command only has to exit zero.
    """
    if not expected:
        return False
    for segment in _command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return True
        words = _effective_tokens(tokens)
        printer = not words or _command_basename(words[0]) in _STATIC_COMMANDS | _TEXT_TOOLS
        if printer and any(expected in token for token in tokens):
            return True
        if _contains_inline_script(tokens):
            # Clustered flags (``python -ic``), ``node -p``, ``perl -E``, and a
            # runner such as ``uv run python -c`` can still print arbitrary text.
            # ``python -m pytest -c`` stays a real test command.
            return True
    return False


def _contains_inline_script(tokens: list[str]) -> bool:
    words = _effective_tokens(tokens)
    if words is None:
        return True
    if words and _command_basename(words[0]) == "uv" and words[1:2] == ["run"]:
        words = _effective_tokens(words[2:])
        if words is None or words and words[0].startswith("-"):
            return True
    if not words:
        return False
    name, rest = _command_basename(words[0]), words[1:]
    if name.startswith("python"):
        return _python_inline(rest)
    if name in {"node", "nodejs"}:
        return _node_inline(rest)
    if name == "perl":
        return any(_PERL_INLINE.match(item) for item in _interpreter_options(rest, {"-I", "-M", "-m"}))
    if name == "ruby":
        return any(_RUBY_INLINE.match(item) for item in _interpreter_options(rest, {"-I", "-r", "-C", "-E", "-F"}))
    if name.startswith("php"):
        return any(item.startswith("-r") and not item.startswith("--")
                   for item in _interpreter_options(rest, {"-c", "-d"}))
    if name == "lua":
        return any(item.startswith("-e") and not item.startswith("--")
                   for item in _interpreter_options(rest, {"-l"}))
    return False


def _interpreter_options(rest: list[str], value_options: set[str]) -> list[str]:
    options = []
    arguments = iter(rest)
    for token in arguments:
        if token == "--" or not token.startswith("-"):
            break
        options.append(token)
        if token in value_options:
            next(arguments, None)
    return options


def _node_inline(rest: list[str]) -> bool:
    options = _interpreter_options(rest, {
        "-r", "--require", "--import", "--loader", "--experimental-loader",
        "-C", "--conditions", "--input-type",
    })
    return any(token.startswith(("--eval", "--print")) or (
        not token.startswith("--") and token.startswith(("-e", "-p"))
    ) for token in options)


def _python_inline(rest: list[str]) -> bool:
    for token in _interpreter_options(rest, {"-W", "-X", "--check-hash-based-pycs"}):
        if token == "-m" or (token.startswith("-m") and not token.startswith("--")):
            return False
        if _PY_INLINE.match(token):
            return True
    return False

def _verification_mode(command: str) -> bool:
    try:
        segments = [shlex.split(segment) for segment in _command_segments(command)]
    except ValueError:
        return False
    return any(_has_behavior_command(tokens) for tokens in segments) and not any(
        token in {"-h", "--help", "--collect-only", "--co", "--version"}
        or token.startswith("--collect-only=") for tokens in segments for token in tokens
    )

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
        executed_command = tool.prepare_command(command)
        if executed_command != command:
            row["executed_command"] = executed_command
        params: dict[str, Any] = {"command": executed_command}
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
            completed = result.returncode == 0 and not truncated and not getattr(result, "timed_out", False)
            if records:
                row["ok"] = completed and all(record.get("verified") is True for record in records)
            requires_test_evidence = tool.requires_test_evidence(command)
            if not records and (expected is None or requires_test_evidence):
                row["reason"] = ("pytest command requires per-target test evidence from a supported invocation"
                                 if requires_test_evidence else
                                 "command requires parsed test evidence or an explicit expected_output probe")
            if expected is not None:
                output = result.stdout + ("\n" + result.stderr if result.stderr else "")
                row["expected_output"] = expected
                row["matched"] = expected in output
                row["ok"] = (row["ok"] if records else not requires_test_evidence) and completed and row["matched"]
                if row["ok"] and not records and _static_printer_supplies(command, expected):
                    row["ok"] = False
                    row["reason"] = "expected output is supplied by a static printer"
        checks.append(row)
    return {
        "ok": bool(checks) and all(row["ok"] for row in checks),
        "executed": bool(checks) and all(row["executed"] for row in checks),
        "checks": checks,
    }
