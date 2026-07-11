"""Configuration and shared mutable state for the V1 remote runner."""

# ruff: noqa: F401

from __future__ import annotations

import ast
import atexit
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any

from opencollab.harness.swe_eval_records import (
    SUBMISSION_INTEGRITY_INELIGIBLE,
    embedded_workflow_metric,
    metric_submission_integrity,
    open_regular_binary,
    patch_sha,
    patch_sha_matches,
    prediction_patch,
    row_explicit_patch_sha,
    row_patch_sha,
    row_record_id,
    row_task_id,
)

cfg: dict[str, Any] = {}
token = ""
owner_nonce = ""
remote_root = pathlib.Path(".")
remote_repo = pathlib.Path(".")
base_run_dir = pathlib.Path(".")
package_root = pathlib.Path(".")
dataset_path = pathlib.Path(".")
workflow = ""
model_name = ""
session_prefix = ""
image_repository = ""
remote_proxy_base_url = ""
start_index = 0
limit = 0
budget = 0
max_steps = 0
swe_timeout = 0
task_wall_timeout = 0
eval_timeout = 0
checkpoint_interval = 0
max_task_starts = 0
dry_run = False

ACTIVE_CHILD_PGIDS: set[int] = set()
ACTIVE_FIFO_PATHS: set[pathlib.Path] = set()
RUNNER_LOCK_FD: int | None = None
RUNNER_OWNER_RECORD: dict[str, Any] | None = None
RUNNER_STATE_THREAD_LOCK = threading.RLock()
PROCESS_TERM_GRACE_SECONDS = 30.0
PROCESS_KILL_REAP_TIMEOUT_SECONDS = 5.0
PROCESS_CLEANUP_OUTER_SLACK_SECONDS = 1.0
PROCESS_CLEANUP_FAILED_EXIT_CODE = 125
SPAWN_SIGNALS = frozenset((signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_JSONL_RETAINED_BYTES = 64 * 1024 * 1024
MAX_JSONL_RETAINED_ROWS = 10_000
MAX_JSONL_SCAN_BYTES = 256 * 1024 * 1024
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_DATASET_ROWS = 1_000_000
MAX_LOG_TAIL_BYTES = 4 * 1024 * 1024
MAX_TASK_ID_BYTES = 240
MAX_TASKS_PER_RUN = 1_000
MAX_DURABLE_JSONL_BYTES = 256 * 1024 * 1024
MAX_JSON_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_EXIT_STATUS_BYTES = 128
SAFE_FILE_OPEN_RETRIES = 8
HARNESS_LOCK_TIMEOUT_SECONDS = 10.0


def configure(config: dict[str, Any]) -> None:
    """Validate and install one remote-run configuration."""
    global cfg, token, owner_nonce, remote_root, remote_repo, base_run_dir
    global package_root, dataset_path, workflow, model_name, session_prefix
    global image_repository
    global remote_proxy_base_url, start_index, limit, budget, max_steps
    global swe_timeout, task_wall_timeout, eval_timeout, checkpoint_interval
    global max_task_starts, dry_run, ACTIVE_CHILD_PGIDS, ACTIVE_FIFO_PATHS
    global RUNNER_LOCK_FD, RUNNER_OWNER_RECORD, RUNNER_STATE_THREAD_LOCK

    required = (
        "token",
        "owner_nonce",
        "remote_root",
        "remote_repo",
        "base_run_dir",
        "workflow",
        "model_name",
        "session_prefix",
        "image_repository",
        "remote_proxy_base_url",
    )
    missing = [name for name in required if not str(config.get(name) or "").strip()]
    if missing:
        raise ValueError("missing remote runner configuration: " + ", ".join(missing))
    cfg = dict(config)
    token = str(cfg["token"])
    owner_nonce = str(cfg["owner_nonce"])
    remote_root = pathlib.Path(cfg["remote_root"])
    remote_repo = pathlib.Path(cfg["remote_repo"])
    base_run_dir = pathlib.Path(cfg["base_run_dir"])
    package_root = remote_repo / "opencollab"
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    dataset_path = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
    workflow = str(cfg["workflow"])
    model_name = str(cfg["model_name"])
    session_prefix = str(cfg["session_prefix"]).rstrip("_")
    image_repository = str(cfg["image_repository"]).rstrip(":")
    remote_proxy_base_url = str(cfg["remote_proxy_base_url"]).rstrip("/")
    start_index = int(cfg["start_index"])
    limit = int(cfg["limit"])
    budget = int(cfg["budget"])
    max_steps = int(cfg["max_steps"])
    swe_timeout = int(cfg["swe_timeout"])
    task_wall_timeout = int(cfg["task_wall_timeout"])
    eval_timeout = int(cfg["eval_timeout"])
    checkpoint_interval = int(cfg["checkpoint_interval"])
    max_task_starts = int(cfg["max_task_starts"])
    dry_run = bool(cfg["dry_run"])
    ACTIVE_CHILD_PGIDS = set()
    ACTIVE_FIFO_PATHS = set()
    RUNNER_LOCK_FD = None
    RUNNER_OWNER_RECORD = None
    RUNNER_STATE_THREAD_LOCK = threading.RLock()


def state_names() -> tuple[str, ...]:
    return tuple(name for name in globals() if not name.startswith("__") and name not in {"configure", "state_names"})


__all__ = [name for name in globals() if not name.startswith("__")]
