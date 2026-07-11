"""run_tests — structured test-runner wrapper.

Agents lose budget (and credibility) reading raw pytest dumps and then claiming
"it passes" without proof. This tool wraps the project's test runner with a
FIXED calling convention and returns a *structured* result — pass/fail/error
counts, the failing node-ids, and the head of the first traceback — instead of
raw stdout. That gives the model a small, reliable signal it can act on and
makes an unverified "it passes" claim much harder to make by accident.

Defaults to ``python -m pytest``; override ``runner`` for projects with a custom
entry point (e.g. ``bin/test``). When the caller does NOT pin a runner, the tool
probes the workspace for a project-native runner (Go ``go test``, sympy
``bin/test``, ``tox``, Django ``manage.py test``) and translates the pytest-style
``target`` node-id to the native invocation; it also auto-falls-back to the
native runner if pytest is missing (``No module named pytest``). Output is
truncated to protect the context.

Every run ALWAYS ends with a one-line ``Verdict: GREEN|RED`` (plus, on RED, a
missing-substring hint and — when the same failing target keeps failing — an
escalation nudge) so a downstream gate/model gets a reliable signal even when
the runner prints no pytest-shaped summary line.

Ref:
- bash.py: same env + safety-policy handling, same head/tail truncation idea.
- SWE-bench: verification is the gap — a green/red signal per run beats prose.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from opencollab.adapters.tools._output import truncate
from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

# Keep the traceback head bounded; full dumps explode the context (ref: bash.py).
MAX_TRACEBACK_CHARS = 6_000
DEFAULT_RUNNER = "python -m pytest"
DEFAULT_TIMEOUT = 300.0
GO_PATH_PREFIX = "PATH=/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:$PATH"
# After this many consecutive failing runs of the SAME target, nudge the model
# to change approach instead of re-running the identical failing assertion.
ESCALATE_AFTER = 3
# Project-native runners, probed in order when the caller did not pin ``runner``
# (or when pytest is missing). Each entry: (probe-cmd that exits 0 iff present,
# base runner command). bin/test is sympy's; manage.py is Django's; tox is the
# generic multi-env runner. Pytest is always tried first via DEFAULT_RUNNER.
_NATIVE_PROBES: tuple[tuple[str, str], ...] = (
    ("test -x bin/test", "python bin/test"),
    ("test -f manage.py", "python manage.py test"),
    ("test -f go.mod", "go test"),
    ("test -f tox.ini", "tox"),
)

# Count tokens pytest prints in its final summary line, e.g.
# "===== 1 failed, 2 passed, 1 skipped in 0.12s =====".
_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_SUMMARY_RE = re.compile(
    r"(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
    r"(?:,\s*)?)+\s+in\s+\d+(?:\.\d+)?s(?:\s+\([^)]+\))?",
    re.IGNORECASE,
)
_PYTEST_NO_TESTS_RE = re.compile(
    r"no tests ran in \d+(?:\.\d+)?s(?:\s+\([^)]+\))?",
    re.IGNORECASE,
)


class RunTestsTool(Tool):
    """Run tests and return a structured pass/fail summary (not raw output).

    Use this after an edit to VERIFY the fix before claiming success. Pass a
    specific path or node-id (e.g. ``tests/test_x.py::test_y``) to keep the run
    fast and focused.
    """

    name = "run_tests"
    default_timeout = DEFAULT_TIMEOUT
    description = (
        "Run the project's test suite (pytest by default) and return a STRUCTURED "
        "result: pass/fail/error counts, the failing test node-ids, and the head of "
        "the first traceback — not raw stdout. Use it to VERIFY a fix instead of "
        "guessing. Pass `target` (a path or node-id like 'tests/test_x.py::test_y') "
        "to focus the run. "
        "The runner is auto-detected for non-pytest projects (Go go.mod, sympy "
        "bin/test, tox, Django manage.py test) and pytest node-ids are translated; "
        "override `runner` only to force a specific command. For Go, pass `target` "
        "like './internal/server' or './internal/server::TestEvaluate'. Read the "
        "final 'Verdict: GREEN|RED' line as the authoritative pass/fail signal. "
        "Prefer this over bash for running the test suite; it returns a structured "
        "pass/fail signal. Warnings are NOISE, not failures: the pass/fail decision "
        "is exit-code + failed/error counts only, and warnings are reported on a "
        "separate line. Failures are the signal — do not treat a warning as a "
        "regression."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Test path or node-id (e.g. 'tests/test_x.py::test_y'). "
                "Set this to run just the relevant tests and stay fast; omit only "
                "when you truly need the whole suite.",
            },
            "runner": {
                "type": "string",
                "description": "Base test command (default 'python -m pytest').",
            },
            "extra_args": {
                "type": "string",
                "description": "Extra runner flags appended verbatim (e.g. '-k expr', '-x').",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 300).",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        max_traceback_chars: int = MAX_TRACEBACK_CHARS,
        *,
        allow_runner_override: bool = True,
        allow_extra_args: bool = True,
        require_process_isolation: bool = False,
    ):
        self.max_traceback_chars = max_traceback_chars
        self.allow_runner_override = allow_runner_override
        self.allow_extra_args = allow_extra_args
        self.require_process_isolation = require_process_isolation
        self._verified_targets: set[str] = set()
        # target -> consecutive RED count, for the escalation nudge. The tool
        # instance is shared across a task's workflow sessions (built once in
        # the eval toolset), so this survives across run_tests calls.
        self._consecutive_fail: dict[str, int] = {}

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        target = params.get("target", "")
        pinned_runner = params.get("runner")
        extra_args = params.get("extra_args", "")
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if not env:
            return "Error: no execution environment available."
        if self.require_process_isolation and not getattr(
            env, "process_isolated", False
        ):
            return (
                "Error: run_tests is disabled because this execution environment "
                "does not provide an OS process sandbox."
            )
        if pinned_runner and not self.allow_runner_override:
            return (
                "Error: runner override is disabled for this run_tests tool. "
                "Omit `runner`; run_tests auto-detects pytest and project-native "
                "runners such as Go go.mod, sympy bin/test, Django manage.py, and "
                "tox. For Go, pass `target` like './internal/server' or "
                "'./internal/server::TestEvaluate'."
            )
        if extra_args and not self.allow_extra_args:
            return "Error: extra_args is disabled for this run_tests tool."

        runner = pinned_runner or DEFAULT_RUNNER
        result, cmd, runner = await self._run(
            env, runner, target, extra_args, timeout, safety_policy,
            runtime.confirm_fn(),
        )
        combined = result.stdout + ("\n" + result.stderr if result.stderr else "")

        # Auto-fallback: if the caller did NOT pin a runner and pytest is absent
        # or unsuitable for a Go target, probe once and re-run on the native path.
        if pinned_runner is None:
            native = await _native_fallback_candidate(
                env,
                result.returncode,
                combined,
            )
            if native:
                result, cmd, runner = await self._run(
                    env, native, target, extra_args, timeout, safety_policy,
                    runtime.confirm_fn(),
                )
                combined = result.stdout + (
                    "\n" + result.stderr if result.stderr else ""
                )

        green = _is_green(
            result.returncode,
            combined,
            runner=runner,
            target=target,
        )
        if target:
            if green:
                self._verified_targets.add(target)
            else:
                self._verified_targets.discard(target)
        streak = self._record(target, green)
        return _format_report(
            cmd,
            result.returncode,
            combined,
            target=target,
            runner=runner,
            green=green,
            fail_streak=streak,
            max_chars=self.max_traceback_chars,
        )

    async def _run(
        self,
        env: Any,
        runner: str,
        target: str,
        extra_args: str,
        timeout: float,
        safety_policy: Any,
        confirm_fn: Any,
    ) -> tuple[Any, str, str]:
        """Build + safety-check + exec one command. Returns (result, cmd, runner)."""
        cmd = _build_command(runner, target, extra_args)
        # Same safety handshake bash uses — a runner override could be anything.
        if safety_policy:
            await safety_policy.check_cmd_interactive(cmd, confirm_fn)
        result = await env.exec_cmd(cmd, timeout=timeout)
        return result, cmd, runner

    def _record(self, target: str, green: bool) -> int:
        """Update + return the consecutive-RED streak for ``target``."""
        if green:
            self._consecutive_fail.pop(target, None)
            return 0
        n = self._consecutive_fail.get(target, 0) + 1
        self._consecutive_fail[target] = n
        return n

    @property
    def verified_targets(self) -> frozenset[str]:
        """Exact requested targets whose latest parser-backed verdict was GREEN."""
        return frozenset(self._verified_targets)


def verification_run_tests_tool() -> RunTestsTool:
    """Build the model-facing verifier with command overrides disabled."""
    return RunTestsTool(allow_runner_override=False, allow_extra_args=False)


_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PYTHON_EXECUTABLE_RE = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|pypy\d*)")
_PYTHON_FLAG_OPTIONS = frozenset({"-B", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v"})


def _python_invokes_pytest(parts: list[str]) -> bool:
    executable = parts[0].rsplit("/", 1)[-1]
    if _PYTHON_EXECUTABLE_RE.fullmatch(executable) is None:
        return False
    index = 1
    while index < len(parts):
        part = parts[index]
        if part == "-m":
            return index + 1 < len(parts) and parts[index + 1] == "pytest"
        if part in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
            continue
        if part.startswith(("-W", "-X")) and len(part) > 2:
            index += 1
            continue
        if part in _PYTHON_FLAG_OPTIONS:
            index += 1
            continue
        return False
    return False


def _parts_invoke_pytest(parts: list[str], *, depth: int = 0) -> bool:
    """Recognize supported direct wrappers without accepting shell interpreters."""
    if not parts or depth > 3:
        return False
    executable = parts[0].rsplit("/", 1)[-1]
    if executable == "pytest":
        return True
    if _python_invokes_pytest(parts):
        return True
    if executable == "env":
        index = 1
        while index < len(parts) and _ENV_ASSIGNMENT_RE.fullmatch(parts[index]):
            index += 1
        if index < len(parts) and parts[index] == "--":
            index += 1
        return _parts_invoke_pytest(parts[index:], depth=depth + 1)
    if executable in {"uv", "poetry", "pipenv"} and len(parts) >= 3 and parts[1] == "run":
        index = 3 if parts[2] == "--" else 2
        return _parts_invoke_pytest(parts[index:], depth=depth + 1)
    return (
        executable in {"coverage", "coverage3"}
        and len(parts) >= 4
        and parts[1:4] == ["run", "-m", "pytest"]
    )


def _is_pytest_runner(runner: str) -> bool:
    """Whether ``runner`` directly invokes pytest (so pytest flags are safe)."""
    if any(token in runner for token in ("\n", "\r", ";", "&", "|", "<", ">", "`", "$(")):
        return False
    try:
        parts = shlex.split(runner)
    except ValueError:
        return False
    return _parts_invoke_pytest(parts)


def _is_go_runner(runner: str) -> bool:
    """Whether ``runner`` invokes ``go test``."""
    try:
        parts = shlex.split(runner)
    except ValueError:
        return False
    return len(parts) >= 2 and (parts[0] == "go" or parts[0].endswith("/go")) and parts[1] == "test"


def _go_runner_command(runner: str) -> str:
    """Return a Go runner command with common Go install paths visible."""
    return f"{GO_PATH_PREFIX} {runner}" if runner.strip().startswith("go ") else runner


def _translate_native_target_args(target: str) -> list[str]:
    """Map a pytest node-id to safely quoted native-runner arguments.

    sympy ``bin/test`` and friends take a file path or test name, not a
    ``path::node`` id. Drop the ``::`` selector and keep the leaf node name
    (sympy/unittest match on it) alongside the path.
    """
    if not target:
        return []
    path, sep, node = target.partition("::")
    if not sep:
        return [shlex.quote(target)]
    leaf = node.split("::")[-1]
    args = [shlex.quote(path)]
    if leaf:
        args.append(shlex.quote(leaf))
    return args


def _normalize_go_package(package: str) -> str:
    package = package.strip()
    if not package:
        package = "./..."
    if package.endswith(".go"):
        package = package.rsplit("/", 1)[0] if "/" in package else "."
    if package not in {".", "./..."} and not package.startswith(("./", "../", "/")):
        package = "./" + package.strip("/")
    return package


def _translate_go_single_target(target: str) -> list[str]:
    package, sep, node = target.partition("::")
    package = _normalize_go_package(package)

    args = [shlex.quote(package)]
    if sep and node.strip():
        test_name = node.split("::")[-1].strip()
        if test_name:
            args.extend(["-run", shlex.quote(test_name)])
    return args


def _translate_go_target_args(target: str) -> list[str]:
    """Map pytest-like targets to safe ``go test`` package and ``-run`` args."""
    if not target:
        return ["./..."]
    if "::" not in target:
        try:
            targets = shlex.split(target)
        except ValueError:
            targets = []
        if len(targets) > 1 and all(not item.startswith("-") for item in targets):
            args: list[str] = []
            for item in targets:
                args.extend(_translate_go_single_target(item))
            return args

    return _translate_go_single_target(target)


def _build_command(runner: str, target: str, extra_args: str) -> str:
    # --tb=short keeps tracebacks compact; -rfE forces a failed/error summary
    # block even under -q so we can list failing node-ids reliably. -rA adds a
    # per-test short summary (incl. PASSED) so a downstream gate can confirm a
    # NAMED test went green; -p no:cacheprovider makes runs deterministic. These
    # flags are pytest-specific, so for a native runner (bin/test/tox/manage.py)
    # we omit them and translate the node-id — pytest flags would error there.
    if _is_pytest_runner(runner):
        parts = [runner, "--tb=short", "-rfE", "-rA", "-p", "no:cacheprovider", "-q"]
        if target:
            parts.append(shlex.quote(target))
    elif _is_go_runner(runner):
        parts = [_go_runner_command(runner), "-json"]
        parts.extend(_translate_go_target_args(target))
    else:
        parts = [runner]
        parts.extend(_translate_native_target_args(target))
    if extra_args:
        parts.append(extra_args)
    return " ".join(parts)


async def _detect_native_runner(env: Any) -> str | None:
    """Probe the workspace for a project-native runner; None if none found."""
    for probe, runner in _NATIVE_PROBES:
        try:
            result = await env.exec_cmd(probe, timeout=10.0)
        except Exception:
            continue
        if getattr(result, "returncode", 1) == 0:
            return runner
    return None


async def _native_fallback_candidate(
    env: Any,
    returncode: int,
    output: str,
) -> str | None:
    if _pytest_missing(returncode, output):
        return await _detect_native_runner(env)
    if _pytest_no_tests(returncode, output):
        native = await _detect_native_runner(env)
        if native and _is_go_runner(native):
            return native
    return None


def _pytest_missing(returncode: int, output: str) -> bool:
    """Whether the run failed because pytest itself is absent."""
    return returncode == 127 or "No module named pytest" in output


def _pytest_no_tests(returncode: int, output: str) -> bool:
    """Whether pytest ran successfully enough to report an empty selection."""
    return returncode == 5 and "no tests ran" in output.lower()


def _summary_lines(output: str) -> list[str]:
    """Return every complete pytest result summary in emission order."""
    summaries = []
    for line in output.splitlines():
        candidate = line.strip().strip("= ").strip()
        if _PYTEST_SUMMARY_RE.fullmatch(candidate) or _PYTEST_NO_TESTS_RE.fullmatch(candidate):
            summaries.append(candidate)
    return summaries


def _summary_line(output: str) -> str | None:
    """The last complete pytest result summary, with or without ``====``."""
    summaries = _summary_lines(output)
    return summaries[-1] if summaries else None


def _parse_counts(summary: str | None) -> tuple[dict[str, int], int]:
    """Parse the summary line into (decision-relevant counts, warnings).

    Warnings are kept parseable but pulled OUT of the returned counts: the
    pass/fail decision is exit-code + failed/error counts only, so a noisy
    warning must never masquerade as a regression. They are returned separately
    for a 'Warnings: N (not failures)' line.
    """
    counts: dict[str, int] = {}
    warnings = 0
    if not summary:
        return counts, warnings
    for m in _COUNT_RE.finditer(summary):
        token = m.group(2)
        if token.startswith("warning"):
            warnings += int(m.group(1))
            continue
        key = token.rstrip("s") if token.startswith("error") else token
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts, warnings


def _target_has_pass_proof(target: str, passed_lines: list[str]) -> bool:
    """Whether pytest's per-test summary proves the requested target ran."""
    if not target:
        return True
    normalized = target.removeprefix("./")
    target_path, has_selector, _selector = normalized.partition("::")
    target_path = target_path.rstrip("/")
    for line in passed_lines:
        node_id = line.removeprefix("PASSED ").strip()
        candidate = node_id.removeprefix("./")
        if candidate == normalized or candidate.startswith(normalized + "::"):
            return True
        if has_selector and candidate.startswith(normalized + "["):
            return True
        candidate_path = candidate.partition("::")[0]
        if not has_selector and (
            target_path in {"", "."}
            or candidate_path == target_path
            or candidate_path.startswith(target_path + "/")
        ):
            return True
    return False


def _go_target_specs(target: str) -> list[tuple[str, str | None]]:
    """Return requested Go packages and optional test selectors.

    A target without ``::`` is always a package (or package pattern), matching
    the public tool contract. Tests must use ``package::TestName``.
    """
    if not target:
        return [("./...", None)]
    if "::" in target:
        raw_targets = [target]
    else:
        try:
            raw_targets = shlex.split(target)
        except ValueError:
            raw_targets = [target]
    specs: list[tuple[str, str | None]] = []
    for raw_target in raw_targets:
        package, sep, node = raw_target.partition("::")
        test_name = node.split("::")[-1].strip() if sep else ""
        specs.append((_normalize_go_package(package), test_name or None))
    return specs


def _go_package_matches(requested: str, reported: str) -> bool:
    """Match a CLI package path to ``go test -json``'s import path."""
    requested = requested.rstrip("/")
    reported = reported.rstrip("/")
    if requested in {".", "./..."}:
        return True
    if requested.endswith("/..."):
        prefix = requested[:-4].removeprefix("./").strip("/")
        return not prefix or reported == prefix or f"/{prefix}/" in f"/{reported}/"
    relative = requested.removeprefix("./").strip("/")
    if not relative or requested.startswith(("../", "/")):
        return reported == requested
    return reported == relative or reported.endswith("/" + relative)


def _go_has_pass_proof(target: str, output: str) -> bool:
    passed_events: list[tuple[str, str]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("Action") != "pass":
            continue
        package = event.get("Package")
        test_name = event.get("Test")
        if isinstance(package, str) and package and isinstance(test_name, str) and test_name:
            passed_events.append((package, test_name))

    for requested_package, requested_test in _go_target_specs(target):
        matched = False
        for package, test_name in passed_events:
            if not _go_package_matches(requested_package, package):
                continue
            if requested_test and not (
                test_name == requested_test or test_name.startswith(requested_test + "/")
            ):
                continue
            matched = True
            break
        if not matched:
            return False
    return bool(passed_events)


def _is_green(
    returncode: int,
    output: str,
    *,
    runner: str = DEFAULT_RUNNER,
    target: str = "",
) -> bool:
    """Require positive evidence that at least one requested test executed."""
    summaries = _summary_lines(output)
    if _is_pytest_runner(runner) and len(summaries) != 1:
        # One tool invocation represents one pytest session. Multiple result
        # summaries are ambiguous and let an appended passing summary hide an
        # earlier failure; no summary proves no pytest session completed.
        return False
    summary = summaries[0] if summaries else None
    counts, _ = _parse_counts(summary)
    if counts.get("failed", 0) or counts.get("error", 0):
        return False
    if returncode != 0:
        return False
    if _is_pytest_runner(runner):
        if counts.get("passed", 0) <= 0:
            return False
        return _target_has_pass_proof(target, _passed_tests(output))
    if _is_go_runner(runner):
        return _go_has_pass_proof(target, output)
    # Native runners need an explicit, parser-backed proof adapter before their
    # output can authorize a GREEN verdict. A bare exit code is forgeable via
    # no-op commands and zero-test modes.
    return False


def _missing_substring_hint(output: str) -> str | None:
    """Best-effort 'expected X, got Y' hint from the first assertion diff."""
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("E   ") and "assert" in s:
            return s[len("E   "):].strip()
        if s.startswith("assert "):
            return s
    return None


def _failed_tests(output: str) -> list[str]:
    fails = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED ") or s.startswith("ERROR "):
            fails.append(s)
    return fails


def _passed_tests(output: str) -> list[str]:
    """Node-ids reported as PASSED in the -rA short summary (default pytest)."""
    passes = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("PASSED "):
            passes.append(s)
    return passes


def _traceback_head(output: str, max_chars: int = MAX_TRACEBACK_CHARS) -> str:
    """Head of the first FAILURES/ERRORS section (or a raw Python traceback)."""
    for marker in ("= FAILURES =", "= ERRORS =", "Traceback (most recent call last)"):
        idx = output.find(marker)
        if idx != -1:
            section = output[idx:]
            end = section.find("= short test summary info =")
            if end != -1:
                section = section[:end]
            return truncate(section.strip(), max_chars)
    return ""


def _format_report(
    cmd: str,
    returncode: int,
    output: str,
    target: str = "",
    runner: str = DEFAULT_RUNNER,
    green: bool | None = None,
    fail_streak: int = 0,
    max_chars: int = MAX_TRACEBACK_CHARS,
) -> str:
    if green is None:
        green = _is_green(returncode, output)
    if _is_pytest_runner(runner) and _pytest_missing(returncode, output):
        # Still emit a parseable verdict so the gate/model is never left without
        # a signal. pytest-missing is RED (the named tests could not run) and
        # points the model at auto-detected native runners.
        return (
            f"Command: {cmd}\nExit code: {returncode}\n"
            "Error: pytest not found and no project-native runner detected. "
            "Omit `runner` so run_tests can auto-detect Go go.mod, sympy "
            "bin/test, Django manage.py, or tox. For Go, pass `target` like "
            "'./internal/server' or './internal/server::TestEvaluate'.\n"
            "Verdict: RED (tests could not run)\n"
            f"{truncate(output.strip(), 1_000)}"
        )

    summary = _summary_line(output)
    counts, warnings = _parse_counts(summary)
    failed = _failed_tests(output)
    passed = _passed_tests(output)

    parts = [f"Command: {cmd}", f"Exit code: {returncode}"]
    if counts:
        # Warnings are deliberately excluded — the pass/fail decision is
        # exit-code + failed/error counts only, never a warning count.
        parts.append(
            "Counts: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
    if warnings:
        parts.append(f"Warnings: {warnings} (not failures)")
    if summary:
        parts.append(f"Summary: {summary}")
    else:
        verdict_word = "GREEN" if green else "RED"
        parts.append(
            "Summary: no parser-backed executed-test proof; "
            f"exit code {returncode} -> {verdict_word}"
        )

    if failed:
        shown = failed[:25]
        parts.append("Failed/errored tests:")
        parts.extend(f"  - {line}" for line in shown)
        if len(failed) > len(shown):
            parts.append(f"  ... and {len(failed) - len(shown)} more")

    # List PASSED node-ids only for a focused run (a named target was requested
    # or the -rA summary is present). For a full-suite run the PASSED list is
    # suppressed to protect context — the aggregate count is enough. This lets a
    # downstream gate confirm a NAMED test went green.
    if passed and target:
        shown_pass = passed[:25]
        parts.append("Passed tests:")
        parts.extend(f"  - {line}" for line in shown_pass)
        if len(passed) > len(shown_pass):
            parts.append(f"  ... and {len(passed) - len(shown_pass)} more")

    head = _traceback_head(output, max_chars)
    if head:
        parts.append("First failure detail:\n" + head)

    # No structured signal at all (e.g. collection crash) — fall back to output.
    if not counts and not failed and not head:
        parts.append("Output:\n" + truncate(output.strip(), max_chars))

    # Always emit an authoritative one-line verdict, even with no summary line.
    parts.append(f"Verdict: {'GREEN' if green else 'RED'}")
    if not green:
        hint = _missing_substring_hint(output)
        if hint:
            parts.append(f"Hint (expected vs got): {hint}")
        if fail_streak >= ESCALATE_AFTER:
            parts.append(
                f"Escalation: target {target or '(suite)'} has failed "
                f"{fail_streak} runs in a row — stop re-running the same "
                "assertion and try a different fix or approach."
            )

    return "\n".join(parts)
