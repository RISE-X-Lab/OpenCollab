"""Shared configuration and constants for the SWE v1 pro-lite launcher."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = os.environ.get("OPENCOLLAB_SWE_HOST", "")
DEFAULT_REMOTE_ROOT = os.environ.get("OPENCOLLAB_SWE_REMOTE_ROOT", "")
DEFAULT_BASE_RUN_DIR_PREFIX = os.environ.get("OPENCOLLAB_SWE_RUN_PREFIX", "")
DEFAULT_MODEL_NAME = os.environ.get("OPENCOLLAB_SWE_MODEL_NAME", "")
DEFAULT_SESSION_PREFIX = os.environ.get("OPENCOLLAB_SWE_SESSION_PREFIX", "")
DEFAULT_IMAGE_REPOSITORY = os.environ.get("OPENCOLLAB_SWE_IMAGE_REPOSITORY", "")
DEFAULT_REPORT_JSON = REPO_ROOT / "output" / "swe_v1_prolite_report.json"
DEFAULT_REPORT_MD = REPO_ROOT / "output" / "swe_v1_prolite_report.md"
_proxy_env_text = os.environ.get("OPENCOLLAB_PROXY_ENV_FILE", "")
DEFAULT_PROXY_ENV_FILE = Path(_proxy_env_text).expanduser() if _proxy_env_text else None
DEFAULT_LOCAL_PROXY_BASE_URL = os.environ.get("OPENCOLLAB_LOCAL_PROXY_BASE_URL", "")
DEFAULT_REMOTE_PROXY_BASE_URL = os.environ.get("OPENCOLLAB_REMOTE_PROXY_BASE_URL", "")
REMOTE_HEALTH_SSH_TIMEOUT_FLOOR = 15
REMOTE_PROXY_TUNNELS: list[subprocess.Popen[str]] = []
REMOTE_RUNNER = (
    "from opencollab.harness.swe_v1_remote_runner import run_from_stdin\n"
    "raise SystemExit(run_from_stdin())\n"
)
LOCAL_PROCESS_TERM_GRACE_SECONDS = 5.0
LOCAL_PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
LOCAL_PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
REMOTE_CLEANUP_COMMAND_TIMEOUT_SECONDS = 30.0
LOCAL_SPAWN_SIGNALS = frozenset((signal.SIGINT,))
PROXY_PROCESS_LOOKUP_TIMEOUT_SECONDS = 5.0
MAX_PROXY_ENV_BYTES = 1024 * 1024
MAX_TASKS_PER_RUN = 1000
MAX_REMOTE_OUTPUT_TAIL_CHARS = 4 * 1024 * 1024

SYNC_FILES = [
    "scripts/run_swe_v2_one_from_fifo.sh",
    "scripts/swebench_process.py",
    "swebench/gen_prediction.py",
    "swebench/gen_prediction_agent.py",
    "swebench/gen_prediction_bounded_capture.py",
    "swebench/gen_prediction_config.py",
    "swebench/gen_prediction_constants.py",
    "swebench/gen_prediction_docker.py",
    "swebench/gen_prediction_pending.py",
    "swebench/gen_prediction_safe_output.py",
    "swebench/gen_prediction_workflow.py",
    "workflows/validation_council_solve.py",
]

SYNC_DIRS = [
    "opencollab/opencollab",
]


def _redacted(text: str) -> str:
    text = re.sub(r"(GLM_PROXY_CLIENT_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_AUTH_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(OPENCOLLAB_API_KEY=)\S+", r"\1[redacted]", text)
    return re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}", r"\1[redacted]", text)


__all__ = [name for name in globals() if not name.startswith("__")]
