"""adopt — check out a commit that already exists, and show what it changes.

A seat that holds ``bash`` brings a teammate's work over with
``git checkout <sha>``. This tool is that one step for a seat that does not
hold ``bash``: it takes a commit sha and nothing else, so it cannot run a
command, edit a file, or reach anything but a commit already in the object
store the team shares.

After a checkout the working tree is clean against HEAD, so ``git_diff`` would
show nothing. What the seat needs in order to choose is what the commit changes
from where this run started, and that is what the result shows: the start is
the HEAD this tool first found, recorded before its first checkout.

Ref:
- git_diff.py: same env handling and head/tail truncation.
"""

from __future__ import annotations

import re
from typing import Any

from opencollab.adapters.tools._output import truncate
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
        "commit changes from where this run started. It only checks out: it cannot "
        "edit files or run commands, and it refuses when uncommitted changes would "
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

    def __init__(self, max_diff_chars: int = MAX_DIFF_CHARS):
        self.max_diff_chars = max_diff_chars
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

        if self._start is None:
            head = await env.exec_cmd("git rev-parse --verify HEAD", timeout=30)
            if head.returncode != 0:
                return "Error: this repository has no HEAD to start from."
            self._start = head.stdout.strip()

        resolved = await env.exec_cmd(
            f"git rev-parse --verify --quiet {sha}^{{commit}}", timeout=30
        )
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not commit:
            return f"Error: no commit {sha} in this repository's object store."

        checkout = await env.exec_cmd(f"git checkout --quiet --detach {commit}", timeout=60)
        if checkout.returncode != 0:
            error = (checkout.stderr or checkout.stdout).strip()
            return "Error: git checkout refused; HEAD is unchanged.\n" + truncate(
                error, MAX_ERROR_CHARS
            )

        subject = await env.exec_cmd(f"git --no-pager log -1 --format=%s {commit}", timeout=30)
        stat = await env.exec_cmd(
            f"git --no-pager diff --stat {self._start} {commit}", timeout=30
        )
        diff = await env.exec_cmd(f"git --no-pager diff {self._start} {commit}", timeout=30)
        parts = [
            f"Checked out {commit} ({subject.stdout.strip()}). "
            "HEAD is now that commit, and its tree is the one read as the answer.",
            f"Changes from where this run started ({self._start[:12]}):",
            stat.stdout.strip() or "(none)",
        ]
        if diff.stdout.strip():
            parts.append(truncate(diff.stdout.strip(), self.max_diff_chars))
        return "\n\n".join(parts)
