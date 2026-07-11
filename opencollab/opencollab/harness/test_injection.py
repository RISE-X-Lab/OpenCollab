"""Inject a benchmark test patch into the workspace before a workflow runs.

SWE-bench grades a candidate fix by applying its *own* ``test_patch`` (the real
FAIL_TO_PASS test) on top of the submitted ``model_patch``. To let the workflow
actually run that test while it works — instead of chasing a test that does not
exist yet at the base commit — the harness can apply that same ``test_patch``
into the live workspace up front. The injected test files are then checked out
right before ``model_patch`` extraction so they never leak into the submitted
diff (the grader would otherwise double-apply and conflict).

This module is benchmark plumbing: it lives in ``harness`` (outside
application/domain) and talks to the environment only through the
``EnvironmentPort`` ``exec_cmd``/``write_file`` surface.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

logger = logging.getLogger(__name__)

# Where the patch is staged inside the env before applying. Kept out of the repo
# tree so it is never picked up by a ``git diff``.
_PATCH_PATH = "/tmp/opencollab_test_patch.diff"

# Matches the post-image path on a unified-diff file header line. A test_patch
# only ever touches test files, so every touched path is recorded for later
# diff-exclusion.
_DIFF_HEADER_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def _touched_files(patch: str) -> list[str]:
    """Parse the ``+++ b/<path>`` headers from a unified diff (de-duplicated)."""
    seen: list[str] = []
    for path in _DIFF_HEADER_RE.findall(patch):
        path = path.strip()
        # ``/dev/null`` appears for pure deletions; nothing to check out there.
        if path and path != "/dev/null" and path not in seen:
            seen.append(path)
    return seen


async def apply_test_patch(env: Any, patch: str) -> list[str]:
    """Apply ``patch`` into ``env`` and return the test files it touched.

    Writes the patch to a temp file inside the env, then runs ``git apply``;
    on failure it retries with ``patch -p1``. A bad patch must never abort the
    run, so a non-zero result from BOTH attempts logs the failure and returns
    ``[]`` (the run simply proceeds without injection) rather than raising.
    """
    if not patch or not patch.strip():
        return []

    try:
        await env.write_file(_PATCH_PATH, patch)
    except Exception as exc:  # writing the staging file is best-effort too
        logger.warning("test injection: could not stage patch file: %s", exc)
        return []

    quoted = shlex.quote(_PATCH_PATH)
    git_cmd = f"git apply -v {quoted}"
    result = await env.exec_cmd(git_cmd)
    if getattr(result, "returncode", 1) != 0:
        logger.warning(
            "test injection: `git apply` failed (rc=%s): %s — retrying with patch -p1",
            getattr(result, "returncode", "?"),
            (getattr(result, "stderr", "") or "").strip()[:300],
        )
        fallback = await env.exec_cmd(f"patch -p1 -i {quoted}")
        if getattr(fallback, "returncode", 1) != 0:
            logger.warning(
                "test injection: fallback `patch -p1` also failed (rc=%s): %s "
                "— continuing without injection",
                getattr(fallback, "returncode", "?"),
                (getattr(fallback, "stderr", "") or "").strip()[:300],
            )
            return []

    touched = _touched_files(patch)
    logger.info("test injection: applied patch touching %d file(s)", len(touched))
    return touched
