"""run_tests — structured test-runner wrapper.

Agents lose budget (and credibility) reading raw pytest dumps and then claiming
"it passes" without proof. This tool wraps the project's test runner with a
FIXED calling convention and returns a *structured* result — pass/fail/error
counts, the failing node-ids, and the head of the first traceback — instead of
raw stdout. That gives the model a small, reliable signal it can act on and
makes an unverified "it passes" claim much harder to make by accident.

Defaults to ``python -m pytest``; override ``runner`` for projects with a custom
entry point (e.g. ``bin/test``). When the caller does NOT pin a runner, the tool
probes the workspace for a project-native runner (sympy ``bin/test``, ``tox``,
Django ``manage.py test``) and translates the pytest-style ``target`` node-id to
the native invocation; it also auto-falls-back to the native runner if pytest is
missing (``No module named pytest``). Output is truncated to protect the context.

Every run ALWAYS ends with a one-line ``Verdict: GREEN|RED`` (plus, on RED, a
missing-substring hint and — when the same failing target keeps failing — an
escalation nudge) so a downstream gate/model gets a reliable signal even when
the runner prints no pytest-shaped summary line.

Ref:
- bash.py: same env + safety-policy handling, same head/tail truncation idea.
- SWE-bench: verification is the gap — a green/red signal per run beats prose.
"""

from __future__ import annotations

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
    ("test -f tox.ini", "tox"),
)

# Count tokens pytest prints in its final summary line, e.g.
# "===== 1 failed, 2 passed, 1 skipped in 0.12s =====".
_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
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
        "The runner is auto-detected for non-pytest projects (sympy bin/test, "
        "tox, Django manage.py test) and pytest node-ids are translated; override "
        "`runner` only to force a specific command. Read the final 'Verdict: "
        "GREEN|RED' line as the authoritative pass/fail signal. "
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
    ):
        self.max_traceback_chars = max_traceback_chars
        self.allow_runner_override = allow_runner_override
        self.allow_extra_args = allow_extra_args
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
        if pinned_runner and not self.allow_runner_override:
            return "Error: runner override is disabled for this run_tests tool."
        if extra_args and not self.allow_extra_args:
            return "Error: extra_args is disabled for this run_tests tool."

        runner = pinned_runner or DEFAULT_RUNNER
        result, cmd, runner = await self._run(
            env, runner, target, extra_args, timeout, safety_policy,
            runtime.confirm_fn(),
        )
        combined = result.stdout + ("\n" + result.stderr if result.stderr else "")

        # Auto-fallback: if the caller did NOT pin a runner and pytest is absent,
        # probe for a project-native runner and re-run once on the native path.
        if pinned_runner is None and _pytest_missing(result.returncode, combined):
            native = await _detect_native_runner(env)
            if native:
                result, cmd, runner = await self._run(
                    env, native, target, extra_args, timeout, safety_policy,
                    runtime.confirm_fn(),
                )
                combined = result.stdout + (
                    "\n" + result.stderr if result.stderr else ""
                )

        green = _is_green(result.returncode, combined)
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


def _is_pytest_runner(runner: str) -> bool:
    """Whether ``runner`` invokes pytest (so pytest-only flags are safe)."""
    return "pytest" in runner


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


def _pytest_missing(returncode: int, output: str) -> bool:
    """Whether the run failed because pytest itself is absent."""
    return returncode == 127 or "No module named pytest" in output


def _summary_line(output: str) -> str | None:
    """The last pytest summary line (``==== ... in 0.1s ====``), if any."""
    summary = None
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("=") and s.endswith("=") and (
            " passed" in s
            or " failed" in s
            or " error" in s
            or " skipped" in s
            or "no tests ran" in s
        ):
            summary = s.strip("= ").strip()
    return summary


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


def _is_green(returncode: int, output: str) -> bool:
    """Runner-agnostic pass decision: exit-code 0 AND no failed/error counts.

    Works even with no pytest summary line — a native runner that exits 0 is
    GREEN. If a pytest-shaped summary IS present, a nonzero failed/error count
    forces RED regardless of exit code (defends against runners that mis-report).
    """
    summary = _summary_line(output)
    counts, _ = _parse_counts(summary)
    if counts.get("failed", 0) or counts.get("error", 0):
        return False
    return returncode == 0


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
    if _pytest_missing(returncode, output):
        # Still emit a parseable verdict so the gate/model is never left without
        # a signal. pytest-missing is RED (the named tests could not run) and
        # points the model at the native-runner override.
        return (
            f"Command: {cmd}\nExit code: {returncode}\n"
            "Error: pytest not found and no project-native runner detected. "
            "Set `runner` to the project's test entry point (e.g. 'python "
            "bin/test', 'tox', 'python manage.py test').\n"
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
            f"Summary: no pytest summary line; decided from exit code "
            f"{returncode} -> {verdict_word}"
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
