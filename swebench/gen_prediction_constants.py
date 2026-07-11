"""Shared constants and embedded container capture code for prediction generation."""

from __future__ import annotations

import re
from pathlib import Path

from opencollab.harness.swe_eval_records import MAX_JSONL_SCAN_BYTES

REPO_ROOT = Path(__file__).resolve().parents[1]

DOCKER_WORKDIR = "/testbed"
# Activate the testbed conda env so the agent's `python`/tests see the repo deps.
_ACTIVATE = "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true"
MAX_EXTRACTED_PATCH_BYTES = 8 * 1024 * 1024
MAX_STATUS_DIAGNOSTIC_BYTES = 64 * 1024
MAX_CAPTURED_STDERR_BYTES = 64 * 1024
CONTAINER_OWNER_SCHEMA_VERSION = 1
CONTAINER_OWNER_LABEL = "opencollab.harness.owner-token"
PENDING_OUTPUT_SCHEMA_VERSION = 1
MAX_PENDING_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_JSONL_SCAN_LINE_BYTES = 64 * 1024 * 1024
MAX_COMPATIBILITY_MARKER_BYTES = 4096
MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
MAX_OWNER_RECORD_BYTES = 1024 * 1024
MAX_OUTPUT_JSONL_BYTES = MAX_JSONL_SCAN_BYTES
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0
AGENT_CANCELLATION_GRACE_SECONDS = 2.0
_MISSING_CONTAINER_RE = re.compile(r"(?:no such (?:container|object)|not found)", re.IGNORECASE)

BOUNDED_CAPTURE_HELPER_SOURCE = REPO_ROOT / "swebench" / "gen_prediction_bounded_capture.py"
CONTAINER_BOUNDED_CAPTURE_HELPER = "/tmp/opencollab_gen_prediction_bounded_capture.py"
# Compatibility alias: callers now receive an importable helper path, never Python source.
_BOUNDED_CAPTURE_SCRIPT = str(BOUNDED_CAPTURE_HELPER_SOURCE)

AGENT_PROMPT = """\
You are an autonomous software engineer fixing a real bug in a Python repository.
The repository is checked out at /testbed and all dependencies are installed.

Rules:
- Explore briefly to find the root cause (a few grep/file_read calls), then ACT.
- As soon as you know the fix, APPLY it with the file_write tool (str_replace
  mode is best for a targeted edit). Diagnosing is not enough — you MUST edit
  the source file. Do not keep exploring once the cause is clear.
- Make the smallest correct change to the SOURCE code that fixes the issue.
- Do NOT edit test files — your fix is graded against the project's own tests.
- After editing, verify with a quick Python snippet that the reported behavior
  is fixed, then stop.
- Do NOT run `git commit`. Just leave your edits in the working tree.
"""
