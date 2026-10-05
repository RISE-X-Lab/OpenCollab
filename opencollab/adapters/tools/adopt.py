"""Adopt an existing commit through the configured command execution policy.

Checkout shares the shell process-isolation setting and command confirmation.
The existing Git configuration checks reject filters and redirected worktrees,
and hooks are disabled for the operation. The result compares the commit with
the first HEAD this tool observed.
"""

from __future__ import annotations

import re
from typing import Any

from opencollab.adapters.git_patch import _git_command_and_config_guard
from opencollab.adapters.tools._output import require_positive_int, truncate
from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

MAX_DIFF_CHARS = 8_000
MAX_ERROR_CHARS = 2_000
#: A full or abbreviated commit id, and nothing git would read as anything else:
#: no ref names, no revision syntax, no option.
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")


class AdoptTool(Tool):
    """Check out a teammate's commit in the tree that is read as the answer."""

    name = "adopt"
    description = (
        "Check out an existing commit in your repository -- the tree that is read "
        "as the answer -- given its sha (7 to 40 hex characters), and show what that "
        "commit changes from where this run started. It uses the configured "
        "command policy and process isolation, and refuses when uncommitted changes would "
        "be overwritten. Calling it again with another sha replaces the first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sha": {
                "type": "string",
                "description": "The commit to check out, as a teammate sent it.",
            },
        },
        "required": ["sha"],
    }

    def __init__(
        self, max_diff_chars: int = MAX_DIFF_CHARS, *, require_process_isolation: bool = True
    ):
        self.max_diff_chars = require_positive_int(max_diff_chars, "max_diff_chars")
        self.require_process_isolation = require_process_isolation
        self._start: str | None = None

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        env = runtime.environment
        if env is None:
            return "Error: no execution environment available."
        sha = str(params.get("sha") or "").strip()
        if not _SHA.match(sha):
            return (
                f"Error: {sha!r} is not a commit sha. Pass the 7-40 hex characters "
                "a teammate sent you; ref names and revision syntax are not accepted."
            )

        if self.require_process_isolation and not getattr(env, "process_isolated", False):
            return "Error: adopt is disabled because this execution environment does not provide an OS process sandbox."
        git_command, config_check = _git_command_and_config_guard()
        git_command += " -c core.hooksPath=/dev/null -c core.fsmonitor=false"
        checkout_command = config_check + f"{git_command} checkout --quiet --detach {sha}"
        if runtime.safety_policy is not None:
            await runtime.safety_policy.check_cmd_interactive(checkout_command, runtime.confirm_fn())
        checked = await env.exec_cmd(config_check, timeout=30)
        if checked.returncode != 0:
            return "Error: adopt refused unsafe Git configuration.\n" + truncate(
                checked.stderr or checked.stdout, MAX_ERROR_CHARS
            )

        if self._start is None:
            head = await env.exec_cmd(f"{git_command} rev-parse --verify HEAD", timeout=30)
            if head.returncode != 0:
                return "Error: this repository has no HEAD to start from."
            self._start = head.stdout.strip()

        resolved = await env.exec_cmd(
            f"{git_command} rev-parse --verify --quiet {sha}^{{commit}}", timeout=30
        )
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not commit:
            return f"Error: no commit {sha} in this repository's object store."

        checkout = await env.exec_cmd(config_check + f"{git_command} checkout --quiet --detach {commit}", timeout=900)
        if checkout.returncode != 0:
            error = (checkout.stderr or checkout.stdout).strip()
            return "Error: git checkout refused; HEAD is unchanged.\n" + truncate(
                error, MAX_ERROR_CHARS
            )

        subject = await env.exec_cmd(f"{git_command} --no-pager log -1 --format=%s {commit}", timeout=30)
        stat = await env.exec_cmd(
            f"{git_command} --no-pager diff --stat --no-ext-diff --no-textconv {self._start} {commit}", timeout=30
        )
        diff = await env.exec_cmd(
            f"{git_command} --no-pager diff --no-ext-diff --no-textconv {self._start} {commit}", timeout=30
        )
        audit_problems = []
        for label, result in (("commit subject", subject), ("change summary", stat), ("full diff", diff)):
            if result.returncode != 0:
                error = (result.stderr or result.stdout).strip()
                detail = f": {truncate(error, 300)}" if error else ""
                audit_problems.append(f"{label} command failed{detail}")
            elif getattr(result, "stdout_truncated", False):
                audit_problems.append(f"{label} output was truncated")
        checkout_message = f"Checked out {commit}. HEAD is now that commit."
        if audit_problems:
            return "\n\n".join(
                (
                    checkout_message,
                    "Change summary unavailable or incomplete: " + "; ".join(audit_problems),
                )
            )

        parts = [
            f"Checked out {commit} ({subject.stdout.strip()}). "
            "HEAD is now that commit, and its tree is the one read as the answer.",
            f"Changes from where this run started ({self._start[:12]}):",
            stat.stdout.strip() or "(none)",
        ]
        if diff.stdout.strip():
            parts.append(truncate(diff.stdout.strip(), self.max_diff_chars))
        return "\n\n".join(parts)
