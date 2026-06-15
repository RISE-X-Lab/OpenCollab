"""run_tests — structured test-runner wrapper.

Agents lose budget (and credibility) reading raw pytest dumps and then claiming
"it passes" without proof. This tool wraps the project's test runner with a
FIXED calling convention and returns a *structured* result — pass/fail/error
counts, the failing node-ids, and the head of the first traceback — instead of
raw stdout. That gives the model a small, reliable signal it can act on and
makes an unverified "it passes" claim much harder to make by accident.

Defaults to ``python -m pytest``; override ``runner`` for projects with a custom
entry point (e.g. ``bin/test``). Output is truncated to protect the context.

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
    description = (
        "Run the project's test suite (pytest by default) and return a STRUCTURED "
        "result: pass/fail/error counts, the failing test node-ids, and the head of "
        "the first traceback — not raw stdout. Use it to VERIFY a fix instead of "
        "guessing. Pass `target` (a path or node-id like 'tests/test_x.py::test_y') "
        "to focus the run. Override `runner` only for non-pytest projects. "
        "Prefer this over bash for running the test suite; it returns a structured "
        "pass/fail signal."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Test path or node-id to run (e.g. 'tests/test_x.py::test_y'). "
                "Omit to let the runner discover tests.",
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

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        target = params.get("target", "")
        runner = params.get("runner") or DEFAULT_RUNNER
        extra_args = params.get("extra_args", "")
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        env = runtime.environment
        safety_policy = runtime.safety_policy

        if not env:
            return "Error: no execution environment available."

        cmd = _build_command(runner, target, extra_args)

        # Same safety handshake bash uses — a runner override could be anything.
        if safety_policy:
            await safety_policy.check_cmd_interactive(cmd, runtime.confirm_fn())

        result = await env.exec_cmd(cmd, timeout=timeout)
        combined = result.stdout + ("\n" + result.stderr if result.stderr else "")
        return _format_report(cmd, result.returncode, combined)


def _build_command(runner: str, target: str, extra_args: str) -> str:
    # --tb=short keeps tracebacks compact; -rfE forces a failed/error summary
    # block even under -q so we can list failing node-ids reliably.
    parts = [runner, "--tb=short", "-rfE", "-q"]
    if target:
        parts.append(shlex.quote(target))
    if extra_args:
        parts.append(extra_args)
    return " ".join(parts)


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


def _parse_counts(summary: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not summary:
        return counts
    for m in _COUNT_RE.finditer(summary):
        key = m.group(2).rstrip("s") if m.group(2).startswith("error") else m.group(2)
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts


def _failed_tests(output: str) -> list[str]:
    fails = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED ") or s.startswith("ERROR "):
            fails.append(s)
    return fails


def _traceback_head(output: str) -> str:
    """Head of the first FAILURES/ERRORS section (or a raw Python traceback)."""
    for marker in ("= FAILURES =", "= ERRORS =", "Traceback (most recent call last)"):
        idx = output.find(marker)
        if idx != -1:
            section = output[idx:]
            end = section.find("= short test summary info =")
            if end != -1:
                section = section[:end]
            return truncate(section.strip(), MAX_TRACEBACK_CHARS)
    return ""


def _format_report(cmd: str, returncode: int, output: str) -> str:
    if returncode == 127 or "No module named pytest" in output:
        return (
            f"Command: {cmd}\nExit code: {returncode}\n"
            "Error: test runner not found. Is pytest installed, and is the "
            "'runner' command correct for this project?\n"
            f"{truncate(output.strip(), 1_000)}"
        )

    summary = _summary_line(output)
    counts = _parse_counts(summary)
    failed = _failed_tests(output)

    parts = [f"Command: {cmd}", f"Exit code: {returncode}"]
    if counts:
        parts.append(
            "Counts: "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
    parts.append(f"Summary: {summary}" if summary else "Summary: (could not parse)")

    if failed:
        shown = failed[:25]
        parts.append("Failed/errored tests:")
        parts.extend(f"  - {line}" for line in shown)
        if len(failed) > len(shown):
            parts.append(f"  ... and {len(failed) - len(shown)} more")

    head = _traceback_head(output)
    if head:
        parts.append("First failure detail:\n" + head)

    # No structured signal at all (e.g. collection crash) — fall back to output.
    if not counts and not failed and not head:
        parts.append("Output:\n" + truncate(output.strip(), MAX_TRACEBACK_CHARS))

    return "\n".join(parts)
