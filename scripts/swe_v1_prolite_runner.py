#!/usr/bin/env python3
"""One-command G1.1 runner for SWE-batch-pro-lite slices.

This script is deliberately narrow: it starts G1.1 generation for a contiguous
slice, runs the pro-lite direct evaluator for each non-empty patch, and writes a
machine and Markdown report. It avoids watch-loop restarts; each task has one
bounded generation attempt and one bounded evaluation attempt.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "jinan-aws"
DEFAULT_REMOTE_ROOT = "/nfsEDS/dongyh/data/kaka/docker/opencollab"
DEFAULT_BASE_RUN_DIR_PREFIX = (
    "/nfsEDS/dongyh/data/kaka/docker/opencollab/"
    "eval_work/validation_council_g11_16m_prolite26_35"
)
DEFAULT_MODEL_NAME = "opencollab-glm52-v1-16m-prolite26-35-20260707"
DEFAULT_REPORT_JSON = REPO_ROOT / "docs" / "monitoring" / "swe_g11_16m_prolite26_35_report.json"
DEFAULT_REPORT_MD = REPO_ROOT / "docs" / "monitoring" / "swe_g11_16m_prolite26_35_report.md"
DEFAULT_PROXY_ENV_FILE = Path.home() / ".claude" / "glm52.env"
DEFAULT_LOCAL_PROXY_BASE_URL = "http://127.0.0.1:8878"
REMOTE_HEALTH_SSH_TIMEOUT_FLOOR = 15
REMOTE_COMPLETION_POLL_SECONDS = 120
REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS = 30
REMOTE_TERMINAL_STATUSES = frozenset(
    {"done", "done_with_technical_failures", "dry_run", "preflight_failed"}
)
MAX_TOTAL_EVAL_ATTEMPTS = 2
ALLOWED_WORKFLOW_ENV_KEYS = frozenset(
    {
        "OPENCOLLAB_MAX_OUTPUT_TOKENS",
        "OPENCOLLAB_TEMPERATURE",
        "OPENCOLLAB_THINKING",
        "OPENCOLLAB_THINKING_PARAMS",
        "OPENCOLLAB_TOP_P",
    }
)

SYNC_FILES = [
    "scripts/run_openhands_cli.sh",
    "scripts/run_swe_v2_one_from_fifo.sh",
    "swebench/gen_prediction.py",
    "swebench/gen_prediction_openhands.py",
    "swebench/gen_prediction_snapshot.py",
    "swebench/gen_prediction_snapshot_container.py",
    "swebench/openhands_require_patch.py",
    "swebench/openhands_runtime.py",
    "swebench/gen_prediction_workflow.py",
    "workflows/analyst_solve.py",
    "workflows/base_team.py",
    "workflows/validation_council_solve.py",
]

SYNC_DIRS = [
    "opencollab/opencollab",
]


REMOTE_RUNNER = r'''
import ast
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request


cfg = json.loads(sys.stdin.read())
token = cfg["token"]
remote_root = pathlib.Path(cfg["remote_root"])
remote_repo = pathlib.Path(cfg["remote_repo"])
base_run_dir = pathlib.Path(cfg["base_run_dir"])
dataset_path = remote_root / "datasets" / "swe-batch-pro-lite" / "instances.jsonl"
workflow = cfg["workflow"]
workflow_env = dict(cfg.get("workflow_env") or {})
allowed_workflow_env_keys = {
    "OPENCOLLAB_MAX_OUTPUT_TOKENS",
    "OPENCOLLAB_TEMPERATURE",
    "OPENCOLLAB_THINKING",
    "OPENCOLLAB_THINKING_PARAMS",
    "OPENCOLLAB_TOP_P",
}
unsupported_workflow_env = sorted(set(workflow_env) - allowed_workflow_env_keys)
if unsupported_workflow_env:
    raise ValueError("unsupported workflow env: " + ", ".join(unsupported_workflow_env))
openhands_command = str(cfg.get("openhands_command") or "")
openhands_command_sha256 = hashlib.sha256(openhands_command.encode("utf-8")).hexdigest() if openhands_command else ""
openhands_empty_patch_rejections = max(
    0, int(cfg.get("openhands_empty_patch_rejections", 2))
)
max_empty_patch_retries = min(
    1, max(0, int(cfg.get("max_empty_patch_retries", 1)))
)
model_name = cfg["model_name"]
llm_model = str(cfg.get("llm_model") or "")
context_window = cfg.get("context_window")
temperature = cfg.get("temperature")
top_p = cfg.get("top_p")
max_output_tokens = cfg.get("max_output_tokens")
invocation_id = str(cfg.get("invocation_id") or "").strip()
if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
    raise ValueError("invocation_id must be a 32-character lowercase hex UUID")
session_prefix = cfg["session_prefix"].rstrip("_")
remote_proxy_base_url = cfg["remote_proxy_base_url"].rstrip("/")
start_index = int(cfg["start_index"])
limit = int(cfg["limit"])
budget = int(cfg["budget"])
max_steps = int(cfg["max_steps"])
swe_timeout = int(cfg["swe_timeout"])
task_wall_timeout = int(cfg["task_wall_timeout"])
eval_timeout = int(cfg["eval_timeout"])
max_eval_attempts = min(2, max(1, int(cfg.get("max_eval_attempts", 2))))
checkpoint_interval = int(cfg["checkpoint_interval"])
max_task_starts = max(1, min(3, int(cfg["max_task_starts"])))
dry_run = bool(cfg["dry_run"])
eval_only = bool(cfg.get("eval_only", False))
eval_dir_name = str(cfg.get("eval_dir_name") or "official_eval_v1_prolite26_35_20260707").strip()
if not eval_dir_name or "/" in eval_dir_name or "\\" in eval_dir_name or eval_dir_name in {".", ".."}:
    raise ValueError("eval_dir_name must be a single directory name")
ACTIVE_CHILD_PGIDS = set()


def slice_label():
    end_index = start_index + max(limit, 0) - 1
    return str(start_index) if end_index <= start_index else f"{start_index}-{end_index}"


def terminate_active_children(sig=signal.SIGTERM):
    for pgid in list(ACTIVE_CHILD_PGIDS):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass


def signal_exit(signum, frame):
    terminate_active_children(signal.SIGTERM)
    raise SystemExit(128 + int(signum))


def write_runner_pid():
    base_run_dir.mkdir(parents=True, exist_ok=True)
    (base_run_dir / "runner.invocation").write_text(invocation_id + "\n", encoding="utf-8")
    (base_run_dir / "runner.pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")


for _sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(_sig, signal_exit)

write_runner_pid()


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def read_jsonl(path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def run(args, timeout=60):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"returncode": 124, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def http_health(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 400, "status": response.status, "body": body}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


def load_dataset():
    if not dataset_path.exists():
        raise RuntimeError(f"missing dataset: {dataset_path}")
    rows = []
    for line in dataset_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def parse_literal_list(value):
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except Exception:
            continue
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item)]
    return [text]


def prediction_patch(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("model_patch") or row.get("patch") or "")


def is_eval_test_path(path):
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if any(part in {"test", "tests", "__tests__"} for part in parts):
        return True
    return (
        name.endswith("_test.go")
        or name.startswith("test_") and name.endswith(".py")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def filter_model_patch_for_eval(patch):
    if not patch.strip():
        return patch
    blocks = []
    current = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)
    kept = []
    for block in blocks:
        header = block[0] if block else ""
        parts = header.strip().split()
        path = ""
        if len(parts) >= 4 and parts[3].startswith("b/"):
            path = parts[3][2:]
        elif len(parts) >= 3 and parts[2].startswith("a/"):
            path = parts[2][2:]
        if path and is_eval_test_path(path):
            continue
        kept.extend(block)
    return "".join(kept)


def row_task_id(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("instance_id") or row.get("task_id") or "")


def row_record_id(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("record_id") or row.get("attempt_id") or "")


def patch_sha(patch):
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()


def declared_row_patch_sha(row):
    if not isinstance(row, dict):
        return ""
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def row_patch_sha(row):
    body = prediction_patch(row)
    return patch_sha(body) if body else declared_row_patch_sha(row)


def completed_artifact_identity_matches(prediction, metric, task):
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        return False
    prediction_record = row_record_id(prediction)
    metric_record = row_record_id(metric)
    if not prediction_record or prediction_record != metric_record:
        return False
    if row_task_id(prediction) != task or row_task_id(metric) != task:
        return False
    computed_sha = patch_sha(prediction_patch(prediction))
    prediction_sha = declared_row_patch_sha(prediction)
    metric_sha = declared_row_patch_sha(metric)
    full_sha = re.compile(r"[0-9a-f]{64}")
    return bool(
        computed_sha
        and full_sha.fullmatch(computed_sha)
        and full_sha.fullmatch(prediction_sha)
        and full_sha.fullmatch(metric_sha)
        and computed_sha == prediction_sha == metric_sha
    )


def patch_sha_matches(left, right):
    left = str(left or "")
    right = str(right or "")
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    return len(shorter) >= 12 and longer.startswith(shorter)


def workflow_status(row):
    if not isinstance(row, dict):
        return ""
    result = row.get("workflow_result") if isinstance(row.get("workflow_result"), dict) else {}
    return str(row.get("workflow_status") or result.get("status") or "")


def generation_runtime_identity():
    identity = {
        "budget": budget,
        "max_steps": max_steps,
    }
    for key, value in (
        ("llm_model", llm_model),
        ("context_window", context_window),
        ("temperature", temperature),
        ("top_p", top_p),
        ("max_output_tokens", max_output_tokens),
    ):
        if value not in (None, ""):
            identity[key] = value
    if workflow == "openhands-external":
        identity["openhands_empty_patch_rejections"] = (
            openhands_empty_patch_rejections
        )
        identity["openhands_command_sha256"] = openhands_command_sha256
    return identity


def generation_identity_matches(prediction, metric):
    rows = [row for row in (prediction, metric) if isinstance(row, dict)]
    models = {
        str(row.get("model_name_or_path") or row.get("model_name") or "")
        for row in rows
        if row.get("model_name_or_path") or row.get("model_name")
    }
    workflows = {
        str(row.get("workflow") or row.get("workflow_name") or "")
        for row in rows
        if row.get("workflow") or row.get("workflow_name")
    }
    if models != {model_name} or workflows != {workflow}:
        return False
    if not isinstance(metric, dict):
        return False
    expected_runtime = generation_runtime_identity()
    runtime_matches = all(
        metric.get(key) == value for key, value in expected_runtime.items()
    )
    if not runtime_matches or workflow != "openhands-external":
        return runtime_matches
    snapshot = metric.get("solver_git_snapshot")
    full_object_id = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("enabled") is True
        and full_object_id.fullmatch(str(snapshot.get("anonymous_head") or ""))
        and full_object_id.fullmatch(str(snapshot.get("base_tree") or ""))
        and snapshot.get("commit_count") == 1
        and snapshot.get("remote_count") == 0
        and snapshot.get("extra_git_metadata") == 0
        and isinstance(snapshot.get("removed_git_metadata"), int)
    )


def empty_patch_attempt_count(run_dir, task):
    predictions = {
        row_record_id(row): row
        for row in read_jsonl(run_dir / "predictions.jsonl")
        if row_task_id(row) == task and row_record_id(row)
    }
    record_ids = set()
    for metric in read_jsonl(run_dir / "metrics.jsonl"):
        record_id = row_record_id(metric)
        prediction = predictions.get(record_id)
        if (
            row_task_id(metric) == task
            and prediction is not None
            and workflow_status(metric) == "empty_patch_after_done"
            and not prediction_patch(prediction).strip()
            and generation_identity_matches(prediction, metric)
        ):
            record_ids.add(record_id)
    return len(record_ids)


def empty_patch_retry_count(run_dir, task):
    expected_runtime = generation_runtime_identity()
    return sum(
        1
        for item in read_jsonl(run_dir / "empty_patch_retries.jsonl")
        if item.get("phase") == "empty_patch_retry_started"
        and item.get("task") == task
        and item.get("workflow") == workflow
        and item.get("model_name") == model_name
        and item.get("runtime_identity") == expected_runtime
    )


def latest_pair(run_dir, task):
    predictions = [row for row in read_jsonl(run_dir / "predictions.jsonl") if row_task_id(row) == task]
    metrics = [row for row in read_jsonl(run_dir / "metrics.jsonl") if row_task_id(row) == task]
    if not predictions:
        return None, None, "missing_prediction"
    prediction = predictions[-1]
    record_id = row_record_id(prediction)
    current_sha = row_patch_sha(prediction)
    if record_id:
        matched = [row for row in metrics if row_record_id(row) == record_id]
        if not matched:
            return prediction, None, "missing_metric_for_record_id"
        metric = matched[-1]
        metric_sha = row_patch_sha(metric)
        if current_sha and metric_sha and not patch_sha_matches(metric_sha, current_sha):
            return prediction, None, "record_id_patch_sha_mismatch"
        if current_sha and not metric_sha:
            return prediction, None, "record_id_patch_sha_missing"
        return prediction, metric, "record_id"
    if current_sha:
        for metric in reversed(metrics):
            metric_sha = row_patch_sha(metric)
            if metric_sha and patch_sha_matches(metric_sha, current_sha):
                return prediction, metric, "patch_sha"
    return prediction, metrics[-1] if metrics else None, "legacy_latest"


def generation_done(run_dir, task, require_identity=True):
    prediction, metric, pairing = latest_pair(run_dir, task)
    patch = prediction_patch(prediction)
    status = workflow_status(metric)
    if eval_only and not completed_artifact_identity_matches(prediction, metric, task):
        return False, prediction, metric, pairing + "_artifact_identity_mismatch"
    if require_identity and not generation_identity_matches(prediction, metric):
        return False, prediction, metric, pairing + "_identity_mismatch"
    return bool(patch.strip() and status in {"done", "done_with_timeout_patch"}), prediction, metric, pairing


def generation_done_result(task, prediction, metric, pairing, **extra):
    artifact_workflow = ""
    artifact_model_name = ""
    for row in (prediction, metric):
        if not isinstance(row, dict):
            continue
        artifact_workflow = artifact_workflow or str(
            row.get("workflow") or row.get("workflow_name") or ""
        )
        artifact_model_name = artifact_model_name or str(
            row.get("model_name_or_path") or row.get("model_name") or ""
        )
    result = {
        "status": "generation_done",
        "task": task,
        "pairing": pairing,
        "patch_len": len(prediction_patch(prediction)),
        "workflow_status": workflow_status(metric),
        "record_id": row_record_id(prediction),
        "patch_sha256": row_patch_sha(prediction),
        "artifact_workflow": artifact_workflow,
        "artifact_model_name": artifact_model_name,
        "artifact_identity_status": (
            "recorded" if artifact_workflow and artifact_model_name else "legacy_unknown"
        ),
    }
    if isinstance(metric, dict):
        snapshot = metric.get("solver_git_snapshot")
        if isinstance(snapshot, dict):
            result["solver_git_snapshot"] = snapshot
        for source_key, target_key in (
            ("tokens_used", "tokens_used"),
            ("steps", "steps"),
            ("duration", "duration_s"),
            ("llm_model", "llm_model"),
            ("llm_provider", "llm_provider"),
            ("context_window", "context_window"),
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("max_output_tokens", "max_output_tokens"),
            ("budget", "budget"),
            ("max_steps", "max_steps"),
        ):
            value = metric.get(source_key)
            if value is not None:
                result[target_key] = value
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


def empty_patch_result(task, prediction, metric, pairing, **extra):
    result = generation_done_result(task, prediction, metric, pairing, **extra)
    result.update(
        {
            "status": "empty_patch",
            "patch_len": 0,
            "workflow_status": "empty_patch_after_done",
            "patch_sha256": "",
        }
    )
    return result


def image_for_row(row):
    tag = str(row.get("dockerhub_tag") or row.get("image_tag") or "")
    if tag:
        if tag.startswith("docker."):
            return tag
        return "docker.1panel.live/jefzda/sweap-images:" + tag
    task = str(row.get("instance_id") or "")
    key = task[len("instance_"):] if task.startswith("instance_") else task
    return "docker.1panel.live/jefzda/sweap-images:" + key


def image_exists(image):
    return subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def ensure_image(image):
    if image_exists(image):
        return {"ok": True, "image": image}
    prefix = "docker.1panel.live/"
    alias = image[len(prefix):] if image.startswith(prefix) else ""
    if alias and image_exists(alias):
        tagged = run(["docker", "tag", alias, image], timeout=120)
        if tagged["returncode"] == 0:
            return {"ok": True, "image": image, "aliased_from": alias}
        return {"ok": False, "image": image, "alias": alias, "reason": "tag_failed", "details": tagged["stderr"] or tagged["stdout"]}
    return {"ok": False, "image": image, "alias": alias, "reason": "missing_image"}


def image_repo_workdir_status(image):
    script = r"""
if [ -d /testbed/.git ] || [ -d /app/.git ] || [ -d /workspace/.git ] || [ -d /repo/.git ] || [ -d /src/.git ]; then
  exit 0
fi
found=$(find / -maxdepth 3 -name .git -type d 2>/dev/null | head -1 || true)
if [ -n "$found" ]; then
  exit 0
fi
echo "no repository checkout found under common paths" >&2
exit 2
"""
    result = run(["docker", "run", "--rm", "--entrypoint", "", image, "bash", "-lc", script], timeout=120)
    return {"ok": result["returncode"] == 0, "image": image, "returncode": result["returncode"], "details": result["stderr"] or result["stdout"]}


def task_session(task):
    issue = task.split("__", 1)[1] if "__" in task else task
    issue = re.sub(r"[^A-Za-z0-9_.-]+", "_", issue.replace("-", "_").replace("/", "_"))
    return f"{session_prefix}_{issue}"


def generation_state_path(run_dir):
    return run_dir / "generation.state.json"


def load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def start_count(run_dir):
    state = load_json(generation_state_path(run_dir))
    if not isinstance(state, dict):
        return 0
    starts = state.get("starts") if isinstance(state.get("starts"), list) else []
    matching = [
        item
        for item in starts
        if isinstance(item, dict)
        and item.get("workflow") == workflow
        and item.get("model_name") == model_name
        and item.get("runtime_identity") == generation_runtime_identity()
    ]
    if matching:
        return len(matching)
    if (
        state.get("workflow") != workflow
        or state.get("model_name") != model_name
        or state.get("runtime_identity") != generation_runtime_identity()
    ):
        return 0
    try:
        return int(state.get("start_count") or 0)
    except Exception:
        return 0


def write_start_state(run_dir, task, session):
    state = load_json(generation_state_path(run_dir))
    if not isinstance(state, dict):
        state = {}
    starts = state.get("starts") if isinstance(state.get("starts"), list) else []
    count = start_count(run_dir) + 1
    event = {
        "started_at": now(),
        "session": session,
        "workflow": workflow,
        "model_name": model_name,
        "runtime_identity": generation_runtime_identity(),
    }
    starts.append(event)
    state.update({
        "schema": "opencollab.generation_state.v2",
        "task": task,
        "workflow": workflow,
        "model_name": model_name,
        "runtime_identity": generation_runtime_identity(),
        "start_count": count,
        "last_started_at": event["started_at"],
        "last_session": session,
        "starts": starts[-20:],
    })
    write_json(generation_state_path(run_dir), state)
    return state


def write_fifo_with_timeout(path, text, timeout=45):
    data = text.encode("utf-8")
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.25)
            continue
        try:
            os.write(fd, data)
            return {"ok": True}
        finally:
            os.close(fd)
    return {"ok": False, "error": last_error or "timed out waiting for fifo reader"}


def normalize_python_test_target(target):
    target = str(target)
    if "[" in target and not target.endswith("]"):
        return target.split("[", 1)[0]
    return target


def compact_python_test_targets(tests, selected):
    targets = []
    for item in tests or selected:
        target = normalize_python_test_target(item)
        if target and target not in targets:
            targets.append(target)
    return targets


def python_test_command(targets, max_args=40, max_chars=12000):
    batches = []
    current = []
    current_chars = 0
    for target in targets:
        quoted = shlex.quote(target)
        if current and (
            len(current) >= max_args or current_chars + len(quoted) + 1 > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(target)
        current_chars += len(quoted) + 1
    if current:
        batches.append(current)
    commands = [
        "python3 -m pytest -vv "
        + " ".join(shlex.quote(target) for target in batch)
        for batch in batches
    ]
    return " && ".join(commands)


def python_batch_test_command(target_file, repo):
    batch_runner = """import json
import shutil
import subprocess
import sys

targets = json.loads(open(sys.argv[1], encoding="utf-8").read())
if not isinstance(targets, list) or not targets:
    print("missing Python test targets", file=sys.stderr)
    raise SystemExit(127)
compacted = []
for value in targets:
    target = str(value)
    if "[" in target and not target.endswith("]"):
        target = target.split("[", 1)[0]
    if target and target not in compacted:
        compacted.append(target)
targets = compacted
status = 0
for offset in range(0, len(targets), 40):
    batch = [str(item) for item in targets[offset:offset + 40]]
    command = [sys.executable, "-m", "pytest", "-vv", *batch]
    if %r and shutil.which("xvfb-run"):
        command = ["xvfb-run", "-a", sys.executable, "-m", "pytest", "--no-xvfb", "-vv", *batch]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
""" % (repo == "qutebrowser/qutebrowser")
    return "python3 -c " + shlex.quote(batch_runner) + " " + shlex.quote(target_file)


def js_runner_command(binary, package_script, target, extra_args=""):
    local_binary = f"./node_modules/.bin/{binary}"
    target_part = f" {target}" if target else ""
    extra_part = f" {extra_args}" if extra_args else ""
    package_script = shlex.quote(package_script)
    return "\n".join([
        "if [ -x " + shlex.quote(local_binary) + " ]; then",
        "  " + shlex.quote(local_binary) + extra_part + target_part,
        "elif command -v yarn >/dev/null 2>&1; then",
        f"  yarn {package_script}{extra_part}{target_part}",
        "elif command -v npx >/dev/null 2>&1; then",
        f"  npx {shlex.quote(binary)}{extra_part}{target_part}",
        "elif command -v pnpm >/dev/null 2>&1; then",
        f"  pnpm {package_script} --{extra_part}{target_part}",
        "elif command -v corepack >/dev/null 2>&1; then",
        f"  corepack pnpm {package_script} --{extra_part}{target_part}",
        "else",
        f"  echo 'No supported JS test runner found for {binary}' >&2",
        "  exit 127",
        "fi",
    ])


def canonical_js_test_files(tests, selected):
    selected_files = [str(item) for item in selected if str(item)]
    requested = [
        str(item).split(" | ", 1)[0]
        for item in tests
        if str(item) and ("/" in str(item) or "." in str(item))
    ]
    if not requested:
        requested = list(selected_files)
    canonical = []
    for item in requested:
        matches = [
            candidate
            for candidate in selected_files
            if candidate == item or candidate.endswith("/" + item)
        ]
        resolved = max(matches, key=len) if matches else item
        if resolved not in canonical:
            canonical.append(resolved)
    return canonical


def js_workspace_root(test_file):
    parts = pathlib.PurePosixPath(test_file).parts
    if len(parts) >= 3 and parts[0] in {"applications", "packages"}:
        return "/".join(parts[:2])
    return ""


def jest_test_command(test_files):
    grouped = {}
    for test_file in test_files:
        grouped.setdefault(js_workspace_root(test_file), []).append(test_file)
    commands = []
    for workspace, files in grouped.items():
        target = " ".join(shlex.quote(item) for item in files)
        extra_args = "--json --coverage=false --runInBand --verbose --runTestsByPath"
        if workspace:
            config = shlex.quote(workspace + "/jest.config.js")
            extra_args = "--config " + config + " " + extra_args
        commands.append(js_runner_command("jest", "test", target, extra_args))
    return " &&\n".join(commands)


def mocha_test_command(tests, selected, target_file=""):
    if target_file:
        launcher = """import json
import pathlib
import re
import shutil
import subprocess
import sys

tests = json.loads(open(sys.argv[1], encoding="utf-8").read())
grouped = {}
for value in tests:
    item = str(value)
    if " | " not in item:
        continue
    test_file, title = item.split(" | ", 1)
    grouped.setdefault(test_file, []).append(title)
if not grouped:
    print("missing declared Mocha titles", file=sys.stderr)
    raise SystemExit(127)
status = 0
for test_file in sorted(grouped):
    selector = "^(?:" + "|".join(re.escape(title) for title in grouped[test_file]) + ")$"
    if pathlib.Path("./node_modules/.bin/mocha").is_file():
        command = ["./node_modules/.bin/mocha", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("yarn"):
        command = ["yarn", "test", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("npx"):
        command = ["npx", "mocha", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("pnpm"):
        command = ["pnpm", "test", "--", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    elif shutil.which("corepack"):
        command = ["corepack", "pnpm", "test", "--", "--timeout", "30000", "--reporter", "json-stream", "--grep", selector, test_file]
    else:
        print("No supported JS test runner found for mocha", file=sys.stderr)
        raise SystemExit(127)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="")
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
"""
        return "python3 -c " + shlex.quote(launcher) + " " + shlex.quote(target_file)
    files = canonical_js_test_files(tests, selected)
    requested_by_file = {}
    for item in tests:
        if " | " not in str(item):
            continue
        declared_file, title = str(item).split(" | ", 1)
        matches = [
            candidate
            for candidate in files
            if candidate == declared_file or candidate.endswith("/" + declared_file)
        ]
        resolved = max(matches, key=len) if matches else declared_file
        requested_by_file.setdefault(resolved, []).append(title)
    commands = []
    for test_file in files:
        titles = requested_by_file.get(test_file) or []
        if not titles:
            commands.append(
                js_runner_command(
                    "mocha", "test", shlex.quote(test_file), "--timeout 30000 --reporter json-stream"
                )
            )
            continue
        selector = "^(?:" + "|".join(re.escape(title) for title in titles) + ")$"
        commands.append(
            js_runner_command(
                "mocha",
                "test",
                shlex.quote(test_file),
                "--timeout 30000 --reporter json-stream --grep " + shlex.quote(selector),
            )
        )
    return " &&\n".join(commands)


def tutanota_test_command(tests):
    suite_names = []
    for item in tests:
        file_name = str(item).split(" | ", 1)[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        suite_name = file_name[:-4] if file_name.endswith("Test") else file_name
        if suite_name and suite_name not in suite_names:
            suite_names.append(suite_name)
    suites_json = json.dumps(suite_names, ensure_ascii=True)
    reporter_patch = """from pathlib import Path
path = Path("test/tests/Suite.ts")
text = path.read_text(encoding="utf-8")
needle = "\tconst errCount = o.report(results, stats)"
injected = "\tconst errCount = o.report(results, stats)\\n\tconst opencollabSuites = " + %r + "\\n\tconst opencollabResults = results.filter((result) => opencollabSuites.some((suite) => JSON.stringify({task: result.task, context: result.context}).includes(suite)))\\n\tconsole.log(\\\"OPENCOLLAB_OSPEC_RESULTS \\\" + JSON.stringify(opencollabResults.map((result) => ({task: result.task, context: result.context, pass: result.pass}))))"
if needle not in text:
    raise SystemExit("missing ospec reporter insertion point")
path.write_text(text.replace(needle, injected, 1), encoding="utf-8")
""" % suites_json
    return (
        "python3 -c "
        + shlex.quote(reporter_patch)
        + " && npm_config_nodedir=/usr/local npm run test:app"
    )


def go_test_packages_from_patch(row):
    packages = []
    patch = str(row.get("test_patch") or "")
    for match in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE):
        path = match.group(2)
        if not path.endswith("_test.go"):
            continue
        parent = pathlib.PurePosixPath(path).parent.as_posix()
        package = "." if parent == "." else "./" + parent
        if package not in packages:
            packages.append(package)
    return packages or ["./..."]


def go_test_command(tests):
    names = []
    for item in tests:
        name = str(item).split("/", 1)[0]
        if name and name not in names:
            names.append(name)
    discovery = """import json
import pathlib
import re
import subprocess
import sys

names = json.loads(%r)
packages = {}
for path in pathlib.Path(".").rglob("*_test.go"):
    if ".git" in path.parts:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    matched = [
        name for name in names
        if re.search(r"(?m)^func\\s+" + re.escape(name) + r"\\s*\\(", text)
    ]
    if not matched:
        continue
    parent = path.parent.as_posix()
    package = "." if parent == "." else "./" + parent
    packages.setdefault(package, set()).update(matched)
found = set().union(*packages.values()) if packages else set()
missing = [name for name in names if name not in found]
if missing:
    print("unable to map Go tests to packages: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(127)
status = 0
for package in sorted(packages):
    selected = sorted(packages[package])
    pattern = "^(" + "|".join(re.escape(name) for name in selected) + ")(/.*)?$"
    result = subprocess.run(["go", "test", "-count=1", "-json", package, "-run", pattern])
    if result.returncode != 0:
        status = result.returncode
raise SystemExit(status)
""" % json.dumps(names)
    return "python3 -c " + shlex.quote(discovery)


def ansible_python_test_command(targets, target_file=""):
    probe = """from pathlib import Path
import ansible

loaded = Path(ansible.__file__).resolve()
expected = (Path.cwd() / "lib" / "ansible").resolve()
if expected not in loaded.parents:
    raise SystemExit(f"wrong ansible import root: {loaded}")
"""
    return (
        'export PYTHONPATH="$PWD/lib${PYTHONPATH:+:$PYTHONPATH}" && '
        + "python3 -c "
        + shlex.quote(probe)
        + " && "
        + (python_batch_test_command(target_file, "ansible/ansible") if target_file else python_test_command(targets))
    )


def prolite_test_command(row, tests, target_file=""):
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    selected = parse_literal_list(row.get("selected_test_files_to_run"))
    tests = [str(item) for item in tests if str(item)]
    if repo == "tutao/tutanota":
        if tests:
            return tutanota_test_command(tests)
        return "echo 'no declared Tutanota test targets' >&2; exit 127"
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        names = []
        for item in tests or selected:
            head = str(item).split("/", 1)[0]
            if head and head not in names:
                names.append(head)
        if names:
            return go_test_command(names)
        return "go test -count=1 -json " + " ".join(
            shlex.quote(package) for package in go_test_packages_from_patch(row)
        )
    if language in {"js", "javascript", "typescript", "ts"} or repo in {"nodebb/nodebb", "protonmail/webclients", "element-hq/element-web"}:
        files = canonical_js_test_files(tests, selected)
        if repo == "nodebb/nodebb":
            return mocha_test_command(tests, selected, target_file)
        return jest_test_command(files)
    if language == "python" or any("::" in item for item in tests):
        targets = compact_python_test_targets(tests, selected)
        if targets:
            if repo == "ansible/ansible":
                return ansible_python_test_command(targets, target_file)
            if target_file:
                return python_batch_test_command(target_file, repo)
            return python_test_command(targets)
    return str(
        row.get("test_cmd")
        or row.get("eval_cmd")
        or "echo 'unable to derive executable target test command' >&2; exit 127"
    )


GENERATION_RETRY_STATUSES = {
    "fifo_write_failed",
    "generation_failed",
    "generation_timeout",
}


def generation_for_task_once(row, *, reuse_existing_empty_patch=True):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True, exist_ok=True)
    done, prediction, metric, pairing = generation_done(
        run_dir, task, require_identity=not eval_only
    )
    if done:
        return generation_done_result(task, prediction, metric, pairing)
    if (
        reuse_existing_empty_patch
        and
        workflow_status(metric) == "empty_patch_after_done"
        and row_record_id(prediction)
        and generation_identity_matches(prediction, metric)
    ):
        existing_log = run_dir / "generation_logs" / f"{task}.outer.log"
        return empty_patch_result(
            task,
            prediction,
            metric,
            pairing,
            reused_existing_artifact=True,
            log=str(existing_log) if existing_log.exists() else None,
        )
    previous_record_id = row_record_id(prediction)
    if start_count(run_dir) >= max_task_starts:
        return {"status": "generation_start_limit_reached", "task": task, "start_count": start_count(run_dir)}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {"status": "blocked_missing_generation_image", "task": task, "image_status": image_status}
    if dry_run:
        workdir_status = image_repo_workdir_status(image)
        if not workdir_status.get("ok"):
            return {"status": "blocked_bad_generation_workdir", "task": task, "image_status": image_status, "workdir_status": workdir_status}
        if workflow == "openhands-external" and not openhands_command:
            return {
                "status": "blocked_missing_openhands_command",
                "task": task,
                "image": image,
                "workdir_status": workdir_status,
            }
        return {"status": "would_generate", "task": task, "image": image, "workdir_status": workdir_status}
    fifo = pathlib.Path("/tmp") / f"opencollab_v1_{os.getpid()}_{time.time_ns()}.fifo"
    os.mkfifo(fifo, 0o600)
    session = task_session(task)
    state = write_start_state(run_dir, task, session)
    log_path = run_dir / "generation_logs" / f"{task}.outer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    generator = "openhands" if workflow == "openhands-external" else "workflow"
    env = os.environ.copy()
    env.update({
        "OPENCOLLAB_SWE_GENERATOR": generator,
        "OPENCOLLAB_SWE_WORKFLOW": workflow,
        "OPENCOLLAB_SWE_MODEL_NAME": model_name,
        "OPENCOLLAB_SWE_BUDGET": str(budget),
        "OPENCOLLAB_SWE_MAX_STEPS": str(max_steps),
        "OPENCOLLAB_OPENHANDS_EMPTY_PATCH_REJECTIONS": str(
            openhands_empty_patch_rejections
        ),
        "OPENCOLLAB_SWE_TIMEOUT": str(swe_timeout),
        "OPENCOLLAB_LLM_TIMEOUT": str(cfg["llm_timeout"]),
        "OPENCOLLAB_SWE_DATASET": "swe-batch-pro-lite",
        "OPENCOLLAB_REMOTE_PROXY_BASE_URL": remote_proxy_base_url,
        "OPENCOLLAB_SWE_CHECKPOINT_INTERVAL_SECONDS": str(checkpoint_interval),
        "OPENCOLLAB_REMOTE_ROOT": str(remote_root),
        "OPENCOLLAB_REMOTE_REPO": str(remote_repo),
    })
    env.update({str(key): str(value) for key, value in workflow_env.items()})
    if openhands_command:
        env["OPENCOLLAB_OPENHANDS_COMMAND"] = openhands_command
    cmd = [
        str(remote_repo / "scripts" / "run_swe_v2_one_from_fifo.sh"),
        task,
        image,
        str(fifo),
        str(run_dir),
        llm_model,
        "" if temperature is None else str(temperature),
        "" if top_p is None else str(top_p),
        "" if max_output_tokens is None else str(max_output_tokens),
        "" if context_window is None else str(context_window),
    ]
    with log_path.open("ab") as log:
        log.write(("\n===== generation start " + now() + " =====\n").encode())
        proc = subprocess.Popen(cmd, cwd=str(remote_root), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        ACTIVE_CHILD_PGIDS.add(proc.pid)
        try:
            fifo_write = write_fifo_with_timeout(fifo, token + "\n")
            if not fifo_write.get("ok"):
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return {"status": "fifo_write_failed", "task": task, "details": fifo_write, "log": str(log_path)}
            try:
                returncode = proc.wait(timeout=task_wall_timeout)
            except subprocess.TimeoutExpired:
                log.write(("\nouter generation timeout after " + str(task_wall_timeout) + "s\n").encode())
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                return {"status": "generation_timeout", "task": task, "returncode": 124, "log": str(log_path), "start_state": state}
        finally:
            ACTIVE_CHILD_PGIDS.discard(proc.pid)
            try:
                fifo.unlink()
            except FileNotFoundError:
                pass
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if done:
        return generation_done_result(
            task,
            prediction,
            metric,
            pairing,
            returncode=returncode,
            log=str(log_path),
            start_state=state,
        )
    if (
        generation_identity_matches(prediction, metric)
        and workflow_status(metric) == "empty_patch_after_done"
        and row_record_id(prediction)
        and row_record_id(prediction) != previous_record_id
    ):
        return empty_patch_result(
            task,
            prediction,
            metric,
            pairing,
            returncode=returncode,
            log=str(log_path),
            start_state=state,
        )
    return {
        "status": "generation_failed",
        "task": task,
        "returncode": returncode,
        "log": str(log_path),
        "pairing": pairing,
        "patch_len": len(prediction_patch(prediction)),
        "workflow_status": workflow_status(metric),
        "record_id": row_record_id(prediction),
        "patch_sha256": row_patch_sha(prediction),
        "start_state": state,
    }


def generation_for_task(row):
    attempts = []
    force_new_generation = False
    while True:
        was_empty_patch_retry = force_new_generation
        result = dict(
            generation_for_task_once(
                row,
                reuse_existing_empty_patch=not force_new_generation,
            )
        )
        force_new_generation = False
        attempts.append(result)
        if result.get("status") == "generation_done":
            break
        if was_empty_patch_retry:
            break
        if result.get("status") == "empty_patch":
            persisted_retry_count = empty_patch_retry_count(
                base_run_dir / row["instance_id"], row["instance_id"]
            )
            if persisted_retry_count >= max_empty_patch_retries:
                break
            if start_count(base_run_dir / row["instance_id"]) >= max_task_starts:
                break
            append_jsonl(
                base_run_dir / row["instance_id"] / "empty_patch_retries.jsonl",
                {
                    "time": now(),
                    "phase": "empty_patch_retry_started",
                    "task": row["instance_id"],
                    "workflow": workflow,
                    "model_name": model_name,
                    "runtime_identity": generation_runtime_identity(),
                    "source_record_id": result.get("record_id"),
                },
            )
            force_new_generation = True
            append_jsonl(
                base_run_dir / "events.jsonl",
                {
                    "time": now(),
                    "phase": "empty_patch_retry",
                    "task": row["instance_id"],
                    "attempt": persisted_retry_count + 2,
                    "previous_status": result.get("status"),
                    "start_count": start_count(base_run_dir / row["instance_id"]),
                },
            )
            continue
        if result.get("status") not in GENERATION_RETRY_STATUSES:
            break
        if start_count(base_run_dir / row["instance_id"]) >= max_task_starts:
            break
        append_jsonl(
            base_run_dir / "events.jsonl",
            {
                "time": now(),
                "phase": "generation_retry",
                "task": row["instance_id"],
                "attempt": len(attempts) + 1,
                "previous_status": result.get("status"),
                "start_count": start_count(base_run_dir / row["instance_id"]),
            },
        )
    final = dict(attempts[-1])
    final["generation_attempt_count"] = len(attempts)
    final["max_task_starts"] = max_task_starts
    final["empty_patch_retry_count"] = min(
        max_empty_patch_retries,
        empty_patch_retry_count(
            base_run_dir / row["instance_id"], row["instance_id"]
        ),
    )
    final["max_empty_patch_retries"] = max_empty_patch_retries
    if len(attempts) > 1:
        final["attempts"] = attempts
    return final


def eval_summary_matches_prediction(summary, prediction, task):
    if not isinstance(summary, dict) or summary.get("status") != "done":
        return False
    if summary.get("task") and summary.get("task") != task:
        return False
    current_sha = row_patch_sha(prediction)
    previous_sha = str(summary.get("patch_sha256") or "")
    if not patch_sha_matches(previous_sha, current_sha):
        return False
    current_record = row_record_id(prediction)
    previous_record = str(summary.get("record_id") or "")
    if current_record and previous_record and current_record != previous_record:
        return False
    if current_record and not previous_record:
        return False
    return True


EVAL_INFRA_FAILURE_PATTERNS = (
    "ECONNREFUSED",
    "Connection refused",
    "could not connect to server",
    "redis.exceptions.ConnectionError",
    "ServerSelectionTimeoutError",
    "database is locked",
    "dial tcp: lookup",
    "getaddrinfo EAI_AGAIN",
    "node-gyp install error",
    "XIO:  fatal IO error",
    "Aborted (core dumped)",
    "Argument list too long",
    "Fatal Python error:",
    "The X11 connection broke",
    "i/o timeout",
    "ERROR: usage:",
    "INTERNALERROR",
    "Interrupted:",
)


def eval_log_has_infra_failure(exit_status, log_text):
    if exit_status == 0:
        return False
    if exit_status in {5, 126, 127} or exit_status >= 128:
        return True
    text = str(log_text or "")
    lowered = text.lower()
    import_failure = (
        "importerror while importing test module" in lowered
        or "modulenotfounderror" in lowered
        or "importerror: cannot import name" in lowered
    )
    if any(pattern.lower() in lowered for pattern in EVAL_INFRA_FAILURE_PATTERNS):
        return True
    if re.search(r"Aborted\s+\(core dumped\)", text):
        return True
    if import_failure:
        return False
    return any(
        marker in lowered
        for marker in (
            "collected 0 items",
            "no tests ran",
            "no tests found",
            "error: not found",
        )
    )


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def fail_to_pass_execution_proof(row, tests, exit_status, log_text):
    expected = [str(item) for item in tests if str(item)]
    proof = {
        "required": bool(expected),
        "ok": False,
        "exit_status": exit_status,
        "expected": expected,
        "observed": [],
        "missing": list(expected),
        "passed": [],
        "failed": [],
    }
    if not expected:
        return proof
    text = ANSI_ESCAPE_RE.sub("", str(log_text or ""))
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        executed = set()
        passed = set()
        failed = set()
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            test_name = str(event.get("Test") or "")
            if not test_name:
                continue
            if event.get("Action") == "run":
                executed.add(test_name)
            elif event.get("Action") == "pass":
                passed.add(test_name)
            elif event.get("Action") == "fail":
                failed.add(test_name)
        observed = executed | passed | failed
        proof["observed"] = sorted(observed)
        proof["passed"] = sorted(passed)
        proof["failed"] = sorted(failed)
        proof["missing"] = [item for item in expected if item not in observed]
        not_passed = [item for item in expected if item not in passed]
    elif repo == "tutao/tutanota":
        results = []
        marker = "OPENCOLLAB_OSPEC_RESULTS "
        for line in text.splitlines():
            if marker not in line:
                continue
            payload = line.split(marker, 1)[1].strip()
            try:
                parsed = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, list):
                results.extend(item for item in parsed if isinstance(item, dict))
        observed = []
        passed = []
        failed = []
        for item in expected:
            file_name = item.split(" | ", 1)[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            suite_name = file_name[:-4] if file_name.endswith("Test") else file_name
            matching = [
                result
                for result in results
                if suite_name in str(result.get("task") or "")
                or suite_name in json.dumps(result.get("context"), ensure_ascii=False)
            ]
            if not matching:
                continue
            observed.append(item)
            if all(result.get("pass") is True for result in matching):
                passed.append(item)
            else:
                failed.append(item)
        proof["observed"] = observed
        proof["missing"] = [item for item in expected if item not in observed]
        proof["passed"] = passed
        proof["failed"] = failed
        not_passed = [item for item in expected if item not in proof["passed"]]
    elif language in {"js", "javascript", "typescript", "ts"} or repo == "nodebb/nodebb":
        expected_titles = {
            item: " ".join(part.strip() for part in item.split(" | ")[1:] if part.strip())
            if " | " in item
            else item
            for item in expected
        }
        expected_title_parts = {
            item: [part.strip() for part in item.split(" | ")[1:] if part.strip()]
            if " | " in item
            else [item]
            for item in expected
        }
        passed_items = set()
        failed_items = set()
        jest_passed_fragments = set()
        jest_failed_fragments = set()

        def title_part_matches(expected_part, observed_part):
            expected_value = " ".join(str(expected_part).split())
            observed_value = " ".join(str(observed_part).split())
            if expected_value == observed_value:
                return True
            if not observed_value.startswith(expected_value):
                return False
            suffix = observed_value[len(expected_value) :]
            return bool(suffix) and suffix[0] in " ([:—-"

        def contiguous_title_parts_match(expected_parts, observed_parts):
            expected_values = [part for part in expected_parts if str(part).strip()]
            observed_values = [part for part in observed_parts if str(part).strip()]
            if not expected_values or len(expected_values) > len(observed_values):
                return False
            width = len(expected_values)
            return any(
                all(
                    title_part_matches(expected_part, observed_part)
                    for expected_part, observed_part in zip(
                        expected_values,
                        observed_values[offset : offset + width],
                    )
                )
                for offset in range(len(observed_values) - width + 1)
            )

        def canonical_expected_item(fragment, test_file=""):
            fragment_parts = (
                [" ".join(str(part).split()) for part in fragment]
                if isinstance(fragment, list)
                else []
            )
            normalized = " ".join(str(fragment).split()) if not fragment_parts else ""
            candidates = []
            for item, title in expected_titles.items():
                expected_title = " ".join(str(title).split())
                if (
                    fragment_parts
                    and contiguous_title_parts_match(
                        expected_title_parts[item],
                        fragment_parts,
                    )
                ) or (
                    not fragment_parts
                    and (
                        expected_title == normalized
                        or expected_title.endswith(" " + normalized)
                        or normalized.endswith(" " + expected_title)
                    )
                ):
                    candidates.append(item)
            normalized_file = str(test_file or "").replace("\\", "/")
            if normalized_file:
                file_candidates = []
                for item in candidates:
                    expected_file = item.split(" | ", 1)[0].replace("\\", "/")
                    if (
                        normalized_file == expected_file
                        or normalized_file.endswith("/" + expected_file)
                        or expected_file.endswith("/" + normalized_file)
                    ):
                        file_candidates.append(item)
                candidates = file_candidates
            if len(candidates) == 1:
                return candidates[0]
            return ""

        for line in text.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                stripped = line.strip()
                match = re.match(
                    r"^[✓√]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*ms\))?$",
                    stripped,
                )
                if match:
                    jest_passed_fragments.add(match.group(1).strip())
                    continue
                match = re.match(
                    r"^[✕×]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*ms\))?$",
                    stripped,
                )
                if match:
                    jest_failed_fragments.add(match.group(1).strip())
                    continue
                match = re.match(r"^●\s+(.+)$", stripped)
                if match:
                    jest_failed_fragments.add(
                        re.sub(r"\s*[›>]\s*", " ", match.group(1)).strip()
                    )
                continue
            if isinstance(event, dict) and isinstance(event.get("testResults"), list):
                for test_result in event["testResults"]:
                    if not isinstance(test_result, dict):
                        continue
                    for assertion in test_result.get("assertionResults") or []:
                        if not isinstance(assertion, dict):
                            continue
                        ancestor_titles = (
                            assertion.get("ancestorTitles")
                            if isinstance(assertion.get("ancestorTitles"), list)
                            else []
                        )
                        assertion_title = assertion.get("title") or ""
                        title_value = (
                            [*ancestor_titles, assertion_title]
                            if ancestor_titles or assertion_title
                            else assertion.get("fullName") or ""
                        )
                        item = canonical_expected_item(
                            title_value,
                            test_result.get("name") or "",
                        )
                        status = str(assertion.get("status") or "")
                        if item and status == "passed":
                            passed_items.add(item)
                        elif item and status in {"failed", "pending", "todo"}:
                            failed_items.add(item)
                continue
            if not isinstance(event, list) or len(event) != 2 or not isinstance(event[1], dict):
                continue
            item = canonical_expected_item(event[1].get("fullTitle") or "")
            if not item:
                continue
            if event[0] == "pass":
                passed_items.add(item)
            elif event[0] == "fail":
                failed_items.add(item)

        for fragment in jest_passed_fragments:
            item = canonical_expected_item(fragment)
            if item:
                passed_items.add(item)
        for fragment in jest_failed_fragments:
            item = canonical_expected_item(fragment)
            if item:
                failed_items.add(item)
        observed_items = passed_items | failed_items
        proof["observed"] = [item for item in expected if item in observed_items]
        proof["missing"] = [item for item in expected if item not in proof["observed"]]
        proof["passed"] = [
            item for item in expected if item in passed_items and item not in failed_items
        ]
        proof["failed"] = [item for item in expected if item in failed_items]
        not_passed = [item for item in expected if item not in proof["passed"]]
    elif language == "python" or any("::" in item for item in expected):
        statuses = {}
        base_statuses = {}
        rank = {"PASSED": 0, "SKIPPED": 1, "XFAIL": 1, "XPASS": 2, "FAILED": 3, "ERROR": 4}
        for line in text.splitlines():
            match = re.match(
                r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[|\s*$)",
                line.strip(),
            )
            if not match:
                continue
            nodeid = normalize_python_test_target(match.group(1))
            status = match.group(2)
            previous = statuses.get(nodeid)
            if previous is None or rank[status] > rank[previous]:
                statuses[nodeid] = status
            base_nodeid = nodeid.split("[", 1)[0]
            base_previous = base_statuses.get(base_nodeid)
            if base_previous is None or rank[status] > rank[base_previous]:
                base_statuses[base_nodeid] = status

        def python_expected_status(item):
            value = str(item)
            normalized = normalize_python_test_target(value)
            if "[" in value and not value.endswith("]"):
                return base_statuses.get(normalized)
            return statuses.get(normalized)

        observed = [item for item in expected if python_expected_status(item) is not None]
        proof["observed"] = observed
        proof["missing"] = [item for item in expected if item not in observed]
        proof["passed"] = [
            item
            for item in observed
            if python_expected_status(item) == "PASSED"
        ]
        proof["failed"] = [item for item in observed if item not in proof["passed"]]
        not_passed = [item for item in expected if item not in proof["passed"]]
    else:
        observed = []
        missing = []
        for item in expected:
            parts = [part.strip() for part in item.split(" | ")[1:] if part.strip()]
            if parts and all(part in text for part in parts):
                observed.append(item)
            else:
                missing.append(item)
        proof["observed"] = observed
        proof["missing"] = missing
        proof["passed"] = observed if exit_status == 0 else []
        proof["failed"] = observed if exit_status != 0 else []
        not_passed = [item for item in expected if item not in proof["passed"]]
    proof["not_passed"] = not_passed
    proof["ok"] = exit_status == 0 and not not_passed
    return proof


def eval_result_executed(result):
    if result.get("executed") is False:
        return False
    return str(result.get("status") or "") not in {
        "",
        "would_eval",
        "skipped_no_generation_patch",
        "blocked_missing_eval_image",
    }


def eval_attempt_count(run_dir, prediction, task):
    patch_sha256 = row_patch_sha(prediction)
    record_id = row_record_id(prediction)
    return sum(
        1
        for item in read_jsonl(run_dir / "eval_attempts.jsonl")
        if item.get("phase") == "eval_attempt_started"
        and item.get("task") == task
        and patch_sha_matches(str(item.get("patch_sha256") or ""), patch_sha256)
        and (not record_id or item.get("record_id") == record_id)
    )


def eval_for_task_once(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    done, prediction, metric, pairing = generation_done(
        run_dir, task, require_identity=not eval_only
    )
    if not done:
        return {"status": "skipped_no_generation_patch", "task": task, "pairing": pairing}
    eval_dir = run_dir / eval_dir_name
    report_path = eval_dir / "reports" / task / "report.json"
    summary_path = eval_dir / "summary.json"
    previous = load_json(summary_path)
    if eval_summary_matches_prediction(previous, prediction, task):
        return {
            "status": "eval_done",
            "task": task,
            "summary": previous,
            "report_path": str(report_path),
            "executed": False,
            "reused_summary": True,
        }
    if dry_run:
        return {"status": "would_eval", "task": task, "executed": False}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {
            "status": "blocked_missing_eval_image",
            "task": task,
            "image_status": image_status,
            "executed": False,
        }
    input_dir = eval_dir / "input"
    output_dir = report_path.parent
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o777)
    original_model_patch = prediction_patch(prediction)
    model_patch = filter_model_patch_for_eval(original_model_patch)
    test_patch = str(row.get("test_patch") or "")
    expected_base_commit = str(row.get("base_commit") or row.get("commit") or "").strip()
    fail_to_pass = parse_literal_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    pass_to_pass = parse_literal_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    (input_dir / "f2p.targets.json").write_text(
        json.dumps(fail_to_pass, ensure_ascii=False), encoding="utf-8"
    )
    (input_dir / "p2p.targets.json").write_text(
        json.dumps(pass_to_pass, ensure_ascii=False), encoding="utf-8"
    )
    f2p_cmd = prolite_test_command(row, fail_to_pass, "/eval_input/f2p.targets.json")
    p2p_cmd = (
        prolite_test_command(row, pass_to_pass, "/eval_input/p2p.targets.json")
        if pass_to_pass
        else "echo 'no PASS_TO_PASS targets declared'"
    )
    (input_dir / "model.patch").write_text(model_patch, encoding="utf-8")
    (input_dir / "test.patch").write_text(test_patch, encoding="utf-8")
    inner = f"""#!/usr/bin/env bash
set +e
cd /app 2>/dev/null || cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || cd /
export PATH="/usr/local/go/bin:/usr/lib/go/bin:/opt/go/bin:/root/go/bin:/usr/local/node/bin:/opt/node/bin:/root/.local/share/pnpm:/root/.npm-global/bin:/app/node_modules/.bin:$PATH"
if ! command -v pnpm >/dev/null 2>&1 && command -v corepack >/dev/null 2>&1; then
  corepack enable >/tmp/prolite_corepack.log 2>&1 || true
fi
cat > /tmp/prolite_before_repo.sh <<'BEFORE'
{row.get("before_repo_set_cmd") or ""}
BEFORE
expected_base_commit={shlex.quote(expected_base_commit)}
: > /eval_output/base_commit.log
base_commit_status=0
if [ -z "$expected_base_commit" ]; then
  echo "missing expected base commit" >> /eval_output/base_commit.log
  base_commit_status=1
else
  echo "expected=$expected_base_commit" >> /eval_output/base_commit.log
  if ! git cat-file -e "$expected_base_commit^{{commit}}" >> /eval_output/base_commit.log 2>&1; then
    base_commit_status=1
  elif ! git reset --hard "$expected_base_commit" >> /eval_output/base_commit.log 2>&1; then
    base_commit_status=1
  else
    actual_base_commit="$(git rev-parse HEAD 2>> /eval_output/base_commit.log)"
    echo "actual=$actual_base_commit" >> /eval_output/base_commit.log
    if [ "$actual_base_commit" != "$expected_base_commit" ]; then
      base_commit_status=1
    fi
  fi
fi
echo "$base_commit_status" > /eval_output/base_commit.exit
before_repo_status=99
if [ "$base_commit_status" -eq 0 ]; then
  bash /tmp/prolite_before_repo.sh > /eval_output/before_repo.log 2>&1
  before_repo_status=$?
fi
echo "$before_repo_status" > /eval_output/before_repo.exit
post_before_base_status=99
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ]; then
  actual_after_before="$(git rev-parse HEAD 2>> /eval_output/base_commit.log)"
  echo "actual_after_before=$actual_after_before" >> /eval_output/base_commit.log
  if [ "$actual_after_before" = "$expected_base_commit" ]; then
    post_before_base_status=0
  else
    post_before_base_status=1
  fi
fi
echo "$post_before_base_status" > /eval_output/post_before_base.exit
start_optional_eval_services() {{
  : > /eval_output/service_setup.log
  if command -v redis-server >/dev/null 2>&1; then
    if command -v redis-cli >/dev/null 2>&1 && redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
      echo "redis already responding on 127.0.0.1:6379" >> /eval_output/service_setup.log
      return 0
    fi
    echo "starting redis-server on 127.0.0.1:6379" >> /eval_output/service_setup.log
    redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --dir /tmp --daemonize yes >> /eval_output/service_setup.log 2>&1
    for _redis_wait in 1 2 3 4 5; do
      if command -v redis-cli >/dev/null 2>&1 && redis-cli -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
        echo "redis ready" >> /eval_output/service_setup.log
        return 0
      fi
      sleep 1
    done
    echo "redis-server did not become ready" >> /eval_output/service_setup.log
  else
    echo "redis-server not available" >> /eval_output/service_setup.log
  fi
  return 0
}}
apply_patch_with_fallback() {{
  local patch_file="$1"
  local log_file="$2"
  local apply_mode="${{3:-strict}}"
  local existing_mode="${{4:-reject_already_applied}}"
  local git_apply_args=(--whitespace=nowarn)
  if [ ! -s "$patch_file" ]; then
    return 0
  fi
  if [ "$apply_mode" = "ignore-space-change" ]; then
    git_apply_args+=(--ignore-space-change)
  fi
  git apply "${{git_apply_args[@]}}" "$patch_file" > "$log_file" 2>&1
  local status=$?
  if [ "$status" -eq 0 ]; then
    return 0
  fi
  if [ "$existing_mode" = "verify_already_applied" ] && git apply --reverse --check "${{git_apply_args[@]}}" "$patch_file" >> "$log_file" 2>&1; then
    echo "verified test patch already applied; workspace left unchanged" >> "$log_file"
    return 0
  fi
  if git apply --check --3way "${{git_apply_args[@]}}" "$patch_file" >> "$log_file" 2>&1; then
    git apply --3way "${{git_apply_args[@]}}" "$patch_file" >> "$log_file" 2>&1
    status=$?
    if [ "$status" -eq 0 ]; then
      return 0
    fi
  fi
  if command -v patch >/dev/null 2>&1; then
    local patch_args=(--batch --forward -p1)
    if [ "$apply_mode" = "ignore-space-change" ]; then
      patch_args+=(-l)
    fi
    if patch "${{patch_args[@]}}" --dry-run < "$patch_file" >> "$log_file" 2>&1; then
      patch "${{patch_args[@]}}" < "$patch_file" >> "$log_file" 2>&1
      status=$?
    else
      status=1
    fi
    if grep -Eiq 'Reversed \\(or previously applied\\) patch detected|Assuming -R' "$log_file"; then
      echo "patch fallback rejected reversed or previously applied patch" >> "$log_file"
      return 1
    fi
  fi
  return "$status"
}}
model_status=0
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ -s /eval_input/model.patch ]; then
  apply_patch_with_fallback /eval_input/model.patch /eval_output/model_patch.log
  model_status=$?
fi
echo "$model_status" > /eval_output/model_patch.exit
test_status=0
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ "$model_status" -eq 0 ] && [ -s /eval_input/test.patch ]; then
  apply_patch_with_fallback /eval_input/test.patch /eval_output/test_patch.log ignore-space-change verify_already_applied
  test_status=$?
fi
echo "$test_status" > /eval_output/test_patch.exit
test_patch_mode="failed"
if [ "$test_status" -eq 0 ]; then
  if grep -Fq "verified test patch already applied; workspace left unchanged" /eval_output/test_patch.log 2>/dev/null; then
    test_patch_mode="verified_already_applied"
  else
    test_patch_mode="applied"
  fi
fi
echo "$test_patch_mode" > /eval_output/test_patch.mode
if [ "$base_commit_status" -eq 0 ] && [ "$before_repo_status" -eq 0 ] && [ "$post_before_base_status" -eq 0 ] && [ "$model_status" -eq 0 ] && [ "$test_status" -eq 0 ]; then
  start_optional_eval_services
  echo {shlex.quote(f2p_cmd)} > /eval_output/f2p.command
  bash -c {shlex.quote(f2p_cmd)} > /eval_output/f2p.log 2>&1
  echo "$?" > /eval_output/f2p.exit
  echo {shlex.quote(p2p_cmd)} > /eval_output/p2p.command
  bash -c {shlex.quote(p2p_cmd)} > /eval_output/p2p.log 2>&1
  echo "$?" > /eval_output/p2p.exit
else
  echo 99 > /eval_output/f2p.exit
  echo 99 > /eval_output/p2p.exit
fi
exit 0
"""
    script_path = input_dir / "run_prolite_direct_eval.sh"
    script_path.write_text(inner, encoding="utf-8")
    script_path.chmod(0o755)
    command_log = eval_dir / "command.log"
    append_jsonl(
        run_dir / "eval_attempts.jsonl",
        {
            "time": now(),
            "phase": "eval_attempt_started",
            "task": task,
            "patch_sha256": row_patch_sha(prediction),
            "record_id": row_record_id(prediction),
        },
    )
    docker_cmd = [
        "timeout",
        str(eval_timeout),
        "docker",
        "run",
        "--rm",
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/bash",
        "-v",
        f"{input_dir}:/eval_input:ro",
        "-v",
        f"{output_dir}:/eval_output",
        image,
        "/eval_input/run_prolite_direct_eval.sh",
    ]
    with command_log.open("ab") as log:
        log.write(("\n===== eval start " + now() + " =====\n").encode())
        try:
            proc = subprocess.run(docker_cmd, stdout=log, stderr=subprocess.STDOUT, timeout=eval_timeout + 120)
            docker_exit = proc.returncode
        except subprocess.TimeoutExpired:
            log.write(("\neval timeout after " + str(eval_timeout + 120) + "s\n").encode())
            docker_exit = 124

    def read_exit(name, default=99):
        try:
            return int((output_dir / name).read_text(encoding="utf-8", errors="replace").strip() or default)
        except Exception:
            return default

    def read_text(name, limit=4000):
        path = output_dir / name
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]

    def read_full_text(name, limit=64_000_000):
        path = output_dir / name
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:limit]

    def full_text_truncated(name, limit=64_000_000):
        path = output_dir / name
        return path.exists() and path.stat().st_size > limit

    base_commit_status = read_exit("base_commit.exit")
    before_status = read_exit("before_repo.exit")
    post_before_base_status = read_exit("post_before_base.exit")
    model_status = read_exit("model_patch.exit")
    test_status = read_exit("test_patch.exit")
    f2p_status = read_exit("f2p.exit")
    p2p_status = read_exit("p2p.exit", 0)
    f2p_log_tail = read_text("f2p.log")
    p2p_log_tail = read_text("p2p.log")
    f2p_log_full = read_full_text("f2p.log")
    p2p_log_full = read_full_text("p2p.log")
    f2p_log_truncated = full_text_truncated("f2p.log")
    p2p_log_truncated = full_text_truncated("p2p.log")
    f2p_execution_proof = fail_to_pass_execution_proof(
        row,
        fail_to_pass,
        f2p_status,
        f2p_log_full,
    )
    p2p_execution_proof = fail_to_pass_execution_proof(
        row,
        pass_to_pass,
        p2p_status,
        p2p_log_full,
    )
    technical_reasons = []
    if docker_exit != 0:
        technical_reasons.append("docker_exit")
    if base_commit_status != 0:
        technical_reasons.append("base_commit")
    if before_status != 0:
        technical_reasons.append("before_repo")
    if post_before_base_status != 0:
        technical_reasons.append("post_before_base")
    if model_status != 0:
        technical_reasons.append("model_patch")
    if test_status != 0:
        technical_reasons.append("test_patch")
    if eval_log_has_infra_failure(f2p_status, f2p_log_full):
        technical_reasons.append("fail_to_pass_infra")
    if eval_log_has_infra_failure(p2p_status, p2p_log_full):
        technical_reasons.append("pass_to_pass_infra")
    if f2p_log_truncated:
        technical_reasons.append("fail_to_pass_log_truncated")
    if p2p_log_truncated:
        technical_reasons.append("pass_to_pass_log_truncated")
    if (
        f2p_status == 0
        and f2p_execution_proof.get("required")
        and not f2p_execution_proof.get("ok")
    ):
        technical_reasons.append("fail_to_pass_not_executed")
    if (
        p2p_status == 0
        and p2p_execution_proof.get("required")
        and not p2p_execution_proof.get("ok")
    ):
        technical_reasons.append("pass_to_pass_not_executed")
    technical_error = bool(technical_reasons)
    resolved = bool(not technical_error and f2p_status == 0 and p2p_status == 0)
    summary_status = "technical_eval_failed" if technical_error else "done"
    report = {
        "schema": "opencollab.prolite_direct_eval.v1",
        "status": summary_status,
        "instance_id": task,
        "resolved": resolved,
        "patch_successfully_applied": model_status == 0,
        "error": bool(technical_error),
        "technical_reasons": technical_reasons,
        "docker_exit": docker_exit,
        "patch_sha256": row_patch_sha(prediction),
        "declared_prediction_patch_sha256": declared_row_patch_sha(prediction),
        "declared_metric_patch_sha256": declared_row_patch_sha(metric),
        "eval_model_patch_sha256": patch_sha(model_patch),
        "test_patch_sha256": patch_sha(test_patch),
        "record_id": row_record_id(prediction),
        "reeval_source": prediction.get("_reeval_source") or {},
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "tests_status": {
            "base_commit": expected_base_commit,
            "base_commit_status": base_commit_status,
            "before_repo_status": before_status,
            "post_before_base_status": post_before_base_status,
            "model_patch_status": model_status,
            "test_patch_status": test_status,
            "fail_to_pass_status": f2p_status,
            "pass_to_pass_status": p2p_status,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "f2p_command": read_text("f2p.command", 1000),
            "p2p_command": read_text("p2p.command", 1000),
            "f2p_log_tail": f2p_log_tail,
            "p2p_log_tail": p2p_log_tail,
            "f2p_log_truncated": f2p_log_truncated,
            "p2p_log_truncated": p2p_log_truncated,
            "model_patch_log_tail": read_text("model_patch.log"),
            "test_patch_log_tail": read_text("test_patch.log"),
            "base_commit_log_tail": read_text("base_commit.log"),
            "test_patch_apply_mode": read_text("test_patch.mode", limit=200).strip(),
            "fail_to_pass_execution_proof": f2p_execution_proof,
            "pass_to_pass_execution_proof": p2p_execution_proof,
            "service_setup_log_tail": read_text("service_setup.log"),
        },
    }
    write_json(report_path, {task: report})
    summary = {
        "status": summary_status,
        "task": task,
        "resolved": resolved,
        "patch_sha256": row_patch_sha(prediction),
        "declared_prediction_patch_sha256": declared_row_patch_sha(prediction),
        "declared_metric_patch_sha256": declared_row_patch_sha(metric),
        "eval_model_patch_sha256": patch_sha(model_patch),
        "test_patch_sha256": patch_sha(test_patch),
        "record_id": row_record_id(prediction),
        "reeval_source": prediction.get("_reeval_source") or {},
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "technical_reasons": technical_reasons,
        "report_path": str(report_path),
        "command_log": str(command_log),
        "tests_status": report["tests_status"],
    }
    write_json(summary_path, summary)
    return {
        "status": "eval_done" if not technical_error else "technical_eval_failed",
        "task": task,
        "summary": summary,
        "report_path": str(report_path),
        "executed": True,
    }


def eval_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    done, prediction, metric, pairing = generation_done(
        run_dir, task, require_identity=not eval_only
    )
    if not done:
        result = dict(eval_for_task_once(row))
        result["attempt_count"] = 0
        result["max_eval_attempts"] = max_eval_attempts
        return result
    persisted_attempts = eval_attempt_count(run_dir, prediction, task)
    if persisted_attempts >= max_eval_attempts:
        previous = load_json(run_dir / eval_dir_name / "summary.json")
        status = (
            "eval_done"
            if eval_summary_matches_prediction(previous, prediction, task)
            else "technical_eval_failed"
        )
        return {
            "status": status,
            "task": task,
            "summary": previous,
            "pairing": pairing,
            "executed": False,
            "retry_budget_exhausted": status != "eval_done",
            "attempt_count": persisted_attempts,
            "max_eval_attempts": max_eval_attempts,
        }
    attempts = []
    retry_statuses = {"technical_eval_failed", "blocked_missing_eval_image"}
    for _ in range(max_eval_attempts - persisted_attempts):
        result = dict(eval_for_task_once(row))
        current_attempts = eval_attempt_count(run_dir, prediction, task)
        result["attempt"] = current_attempts or persisted_attempts + 1
        attempts.append(result)
        if result.get("status") not in retry_statuses:
            break
        if current_attempts < max_eval_attempts:
            append_jsonl(
                base_run_dir / "events.jsonl",
                {
                    "time": now(),
                    "phase": "eval_retry",
                    "task": task,
                    "attempt": current_attempts + 1,
                    "previous_status": result.get("status"),
                    "technical_reasons": result.get("summary", {}).get("technical_reasons", []),
                },
            )
    final = dict(attempts[-1])
    final["attempt_count"] = eval_attempt_count(run_dir, prediction, task)
    final["max_eval_attempts"] = max_eval_attempts
    if len(attempts) > 1:
        final["attempts"] = attempts
    return final


def write_markdown(summary):
    lines = [
        f"# SWE Pro-Lite {summary.get('slice', slice_label())} Report",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- base_run_dir: `{summary['base_run_dir']}`",
        f"- remote_runtime_repo: `{summary['remote_runtime_repo']}`",
        f"- workflow: `{summary['workflow']}`",
        f"- solver_attribution: `{summary['solver_attribution']}`",
        f"- llm_model: `{summary['llm_model']}`",
        f"- context_window: `{summary['context_window']}`",
        f"- temperature: `{summary['temperature']}`",
        f"- top_p: `{summary['top_p']}`",
        f"- max_output_tokens: `{summary['max_output_tokens']}`",
        f"- tasks: `{summary['counts']['tasks']}`",
        f"- generation_done: `{summary['counts']['generation_done']}`",
        f"- empty_patch: `{summary['counts'].get('empty_patch', 0)}`",
        f"- eval_done: `{summary['counts']['eval_done']}`",
        f"- eval_attempts: `{summary['counts'].get('eval_attempts')}`",
        f"- eval_retry_tasks: `{summary['counts'].get('eval_retry_tasks')}`",
        f"- resolved: `{summary['counts']['resolved']}`",
        f"- unresolved: `{summary['counts']['unresolved']}`",
        f"- technical_failed: `{summary['counts']['technical_failed']}`",
        "",
        "| idx | task | generation | eval | resolved | patch | report |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["rows"]:
        report = row.get("eval", {}).get("report_path") or ""
        patch_sha = (
            row.get("generation", {}).get("patch_sha256")
            or (row.get("eval", {}).get("summary") or {}).get("patch_sha256")
            or ""
        )
        lines.append(
            "| {idx} | `{task}` | `{gen}` | `{ev}` | `{resolved}` | `{patch}` | `{report}` |".format(
                idx=row["index"],
                task=row["task"],
                gen=row.get("generation", {}).get("status", ""),
                ev=row.get("eval", {}).get("status", ""),
                resolved=(row.get("eval", {}).get("summary") or {}).get(
                    "resolved", ""
                ),
                patch=patch_sha[:12],
                report=report,
            )
        )
    summary["markdown"] = "\n".join(lines) + "\n"


def main():
    proxy_health = (
        {"ok": True, "status": "skipped_eval_only"}
        if eval_only
        else http_health(remote_proxy_base_url + "/healthz", timeout=45)
    )
    preflight = {
        "dataset_exists": dataset_path.exists(),
        "remote_root_exists": remote_root.exists(),
        "remote_repo_exists": remote_repo.exists(),
        "remote_runtime_required": not eval_only,
        "proxy_health": proxy_health,
    }
    required_preflight = [
        preflight["dataset_exists"],
        preflight["remote_root_exists"],
        preflight["proxy_health"].get("ok"),
    ]
    if not eval_only:
        required_preflight.append(preflight["remote_repo_exists"])
    if not all(required_preflight):
        summary = {
            "schema": "opencollab.swe_prolite_runner.v2",
            "status": "preflight_failed",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "invocation_id": invocation_id,
            "workflow": workflow,
            "workflow_env": workflow_env,
            "openhands_command_sha256": openhands_command_sha256,
            "openhands_empty_patch_rejections": openhands_empty_patch_rejections,
            "max_empty_patch_retries": max_empty_patch_retries,
            "model_name": model_name,
            "llm_model": llm_model,
            "context_window": context_window,
            "temperature": temperature,
            "top_p": top_p,
            "max_output_tokens": max_output_tokens,
            "budget": budget,
            "max_steps": max_steps,
            "max_task_starts": max_task_starts,
            "max_eval_attempts": max_eval_attempts,
            "eval_only": eval_only,
            "eval_dir_name": eval_dir_name,
            "solver_attribution": "historical_artifact" if eval_only else "current_run",
            "preflight": preflight,
            "counts": {
                "tasks": 0,
                "generation_done": 0,
                "eval_done": 0,
                "eval_attempts": 0,
                "eval_retry_tasks": 0,
                "resolved": 0,
                "unresolved": 0,
                "technical_failed": 1,
            },
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    rows_all = load_dataset()
    selected = rows_all[start_index - 1 : start_index - 1 + limit]
    base_run_dir.mkdir(parents=True, exist_ok=True)
    result_rows = []
    for offset, row in enumerate(selected, start_index):
        task = row["instance_id"]
        if eval_only:
            run_dir = base_run_dir / task
            done, prediction, metric, pairing = generation_done(
                run_dir, task, require_identity=False
            )
            if done:
                gen = generation_done_result(task, prediction, metric, pairing, eval_only=True)
            else:
                gen = {"status": "skipped_no_generation_patch", "task": task, "pairing": pairing, "eval_only": True}
            append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "generation_observed", "task": task, "result": gen})
        else:
            gen = generation_for_task(row)
            append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "generation", "task": task, "result": gen})
        if gen.get("status") == "empty_patch":
            ev = {
                "status": "skipped_empty_patch",
                "task": task,
                "pairing": gen.get("pairing"),
                "attempt_count": 0,
                "max_eval_attempts": max_eval_attempts,
            }
        elif dry_run and gen.get("status") in {"would_generate", "generation_done"}:
            ev = {"status": "would_eval", "task": task}
        else:
            ev = eval_for_task(row)
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "eval", "task": task, "result": ev})
        result_rows.append({"index": offset, "task": task, "generation": gen, "eval": ev})
    generation_ok_statuses = {"generation_done", "empty_patch"}
    eval_ok_statuses = {"eval_done", "skipped_empty_patch"}
    if dry_run:
        generation_ok_statuses.add("would_generate")
        eval_ok_statuses.add("would_eval")
    counts = {
        "tasks": len(result_rows),
        "generation_done": sum(1 for row in result_rows if row["generation"].get("status") == "generation_done"),
        "empty_patch": sum(1 for row in result_rows if row["generation"].get("status") == "empty_patch"),
        "would_generate": sum(1 for row in result_rows if row["generation"].get("status") == "would_generate"),
        "eval_done": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done"),
        "would_eval": sum(1 for row in result_rows if row["eval"].get("status") == "would_eval"),
        "eval_attempts": sum(int(row["eval"].get("attempt_count") or 0) for row in result_rows),
        "eval_retry_tasks": sum(1 for row in result_rows if int(row["eval"].get("attempt_count") or 0) > 1),
        "resolved": sum(1 for row in result_rows if (row["eval"].get("summary") or {}).get("resolved") is True),
        "unresolved": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done" and (row["eval"].get("summary") or {}).get("resolved") is False),
        "technical_failed": sum(
            1
            for row in result_rows
            if row["generation"].get("status") not in generation_ok_statuses
            or row["eval"].get("status") not in eval_ok_statuses
        ),
    }
    status = "done" if counts["technical_failed"] == 0 else "done_with_technical_failures"
    if dry_run and counts["technical_failed"] == 0:
        status = "dry_run"
    summary = {
        "schema": "opencollab.swe_prolite_runner.v2",
        "status": status,
        "generated_at": now(),
        "slice": slice_label(),
        "base_run_dir": str(base_run_dir),
        "remote_runtime_repo": str(remote_repo),
        "invocation_id": invocation_id,
        "workflow": workflow,
        "workflow_env": workflow_env,
        "openhands_command_sha256": openhands_command_sha256,
        "openhands_empty_patch_rejections": openhands_empty_patch_rejections,
        "max_empty_patch_retries": max_empty_patch_retries,
        "model_name": model_name,
        "llm_model": llm_model,
        "context_window": context_window,
        "temperature": temperature,
        "top_p": top_p,
        "max_output_tokens": max_output_tokens,
        "budget": budget,
        "max_steps": max_steps,
        "max_task_starts": max_task_starts,
        "max_eval_attempts": max_eval_attempts,
        "eval_only": eval_only,
        "eval_dir_name": eval_dir_name,
        "solver_attribution": "historical_artifact" if eval_only else "current_run",
        "preflight": preflight,
        "counts": counts,
        "rows": result_rows,
    }
    write_markdown(summary)
    write_json(base_run_dir / "summary.json", summary)
    (base_run_dir / "summary.md").write_text(summary["markdown"], encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if counts["technical_failed"] == 0 else 1


raise SystemExit(main())
'''


def _redacted(text: str) -> str:
    text = re.sub(r"(GLM_PROXY_CLIENT_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_AUTH_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(OPENCOLLAB_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}", r"\1[redacted]", text)
    return text


def normalize_workflow_env(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for item in values:
        key, separator, value = str(item).partition("=")
        if not separator or key not in ALLOWED_WORKFLOW_ENV_KEYS:
            raise ValueError(f"unsupported --workflow-env: {item}")
        normalized[key] = value
    return normalized


def run_checked(command: list[str], *, timeout: int = 120, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, input=input_text, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        message = exc.stderr or exc.stdout or str(exc)
        raise RuntimeError(_redacted(str(message))) from exc
    if result.returncode != 0:
        raise RuntimeError(_redacted(result.stderr or result.stdout or f"{command[0]} exited {result.returncode}"))
    return result


def run_checked_with_retries(
    command: list[str],
    *,
    timeout: int = 120,
    attempts: int = 3,
    delay_seconds: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return run_checked(command, timeout=timeout)
        except RuntimeError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def load_shell_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.expanduser().read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum():
            continue
        parsed = shlex.split(value, posix=True)
        values[key] = parsed[0] if parsed else ""
    return values


def token_from_values(values: dict[str, str]) -> str:
    for name in ("GLM_PROXY_CLIENT_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENCOLLAB_API_KEY"):
        value = values.get(name)
        if value:
            return value
    return ""


def token_from_env_file(path: Path) -> str:
    if path.expanduser().exists():
        return token_from_values(load_shell_env(path))
    return ""


def proxy_env_file_from_ps(ps_text: str) -> Path | None:
    try:
        parts = shlex.split(ps_text)
    except ValueError:
        return None
    for index, part in enumerate(parts):
        if part == "--env-file" and index + 1 < len(parts):
            return Path(parts[index + 1])
        if part.startswith("--env-file="):
            return Path(part.split("=", 1)[1])
    return None


def get_proxy_token(proxy_env_file: Path) -> str:
    token = token_from_values(dict(os.environ))
    if token:
        return token
    token = token_from_env_file(proxy_env_file)
    if token:
        return token
    pids = subprocess.check_output(["pgrep", "-f", "opencollab_glm_anthropic_proxy.py|glm_anthropic_proxy.py"], text=True).split()
    if not pids:
        raise RuntimeError("glm proxy process not found")
    ps = subprocess.check_output(["ps", "eww", "-p", pids[0]], text=True)
    env_path = proxy_env_file_from_ps(ps)
    if env_path:
        token = token_from_env_file(env_path)
        if token:
            return token
    match = re.search(r"GLM_PROXY_CLIENT_TOKEN=(\S+)", ps)
    if not match:
        raise RuntimeError("proxy token not found in environment, proxy env file, or proxy process")
    return match.group(1)


def url_with_healthz(base_url: str) -> str:
    return base_url.rstrip("/") + "/healthz"


def local_http_ok(base_url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url_with_healthz(base_url), timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def remote_http_ok(*, ssh_command: list[str], host: str, base_url: str, timeout: int = 10) -> bool:
    probe = (
        "import sys,urllib.request;"
        "urllib.request.urlopen(sys.argv[1], timeout="
        + str(timeout)
        + ").read()"
    )
    try:
        result = subprocess.run(
            [*ssh_command, host, "python3 -c " + shlex.quote(probe) + " " + shlex.quote(url_with_healthz(base_url))],
            text=True,
            capture_output=True,
            timeout=max(REMOTE_HEALTH_SSH_TIMEOUT_FLOOR, timeout + 8),
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def loopback_port(base_url: str, *, default: int) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    return int(parsed.port or default)


def loopback_url_with_port(base_url: str, port: int) -> str:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"proxy URL must be loopback: {base_url}")
    if host == "::1":
        netloc = f"[::1]:{port}"
    else:
        netloc = f"{host}:{port}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def remote_forward_port_conflict(message: str) -> bool:
    lowered = message.lower()
    return (
        "remote port forwarding failed" in lowered
        or "address already in use" in lowered
        or "cannot listen to port" in lowered
    )


def ensure_remote_proxy(
    *,
    ssh_command: list[str],
    host: str,
    local_proxy_base_url: str,
    remote_proxy_base_url: str,
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled"}
    if remote_http_ok(ssh_command=ssh_command, host=host, base_url=remote_proxy_base_url):
        return {"status": "already_healthy", "remote_proxy_base_url": remote_proxy_base_url}
    if not local_http_ok(local_proxy_base_url):
        raise RuntimeError(f"local proxy health check failed: {url_with_healthz(local_proxy_base_url)}")
    local_port = loopback_port(local_proxy_base_url, default=8878)
    remote_port = loopback_port(remote_proxy_base_url, default=18788)
    attempts: list[str] = []
    for candidate_port in range(remote_port, remote_port + 21):
        candidate_base_url = loopback_url_with_port(remote_proxy_base_url, candidate_port)
        forward = f"127.0.0.1:{candidate_port}:127.0.0.1:{local_port}"
        command = [
            *ssh_command,
            "-fN",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            forward,
            host,
        ]
        try:
            run_checked_with_retries(command, timeout=30)
        except RuntimeError as exc:
            message = str(exc)
            attempts.append(f"{candidate_port}: {message}")
            if remote_forward_port_conflict(message):
                if remote_http_ok(
                    ssh_command=ssh_command,
                    host=host,
                    base_url=candidate_base_url,
                    timeout=2,
                ):
                    return {
                        "status": "already_healthy",
                        "remote_proxy_base_url": candidate_base_url,
                        "selected_remote_port": candidate_port,
                    }
                continue
            raise
        for _ in range(6):
            if remote_http_ok(ssh_command=ssh_command, host=host, base_url=candidate_base_url, timeout=2):
                return {
                    "status": "started" if candidate_port == remote_port else "started_fallback_port",
                    "local_proxy_base_url": local_proxy_base_url,
                    "remote_proxy_base_url": candidate_base_url,
                    "forward": forward,
                    "selected_remote_port": candidate_port,
                }
            time.sleep(0.5)
        attempts.append(f"{candidate_port}: tunnel started but health check failed")
    detail = "; ".join(attempts[-5:])
    raise RuntimeError(
        f"remote proxy tunnel did not become healthy near port {remote_port}: {detail}"
    )


def sync_runtime(*, ssh_command: list[str], host: str, remote_runtime_repo: str) -> dict[str, Any]:
    synced: list[str] = []
    synced_dirs: list[str] = []
    ssh_part = " ".join(shlex.quote(part) for part in ssh_command)
    with tempfile.TemporaryDirectory(prefix="swe-v1-runtime-") as tmp_dir:
        archive_path = Path(tmp_dir) / "runtime.tgz"

        def archive_filter(tar_info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = Path(tar_info.name).parts
            if "__pycache__" in parts or tar_info.name.endswith((".pyc", ".pyo")):
                return None
            return tar_info

        with tarfile.open(archive_path, "w:gz") as archive:
            for rel in SYNC_FILES:
                local_path = REPO_ROOT / rel
                if not local_path.exists():
                    continue
                archive.add(local_path, arcname=rel, filter=archive_filter)
                synced.append(rel)
            for rel in SYNC_DIRS:
                local_path = REPO_ROOT / rel
                if not local_path.exists():
                    continue
                archive.add(local_path, arcname=rel, filter=archive_filter)
                synced_dirs.append(rel)
        run_checked_with_retries([*ssh_command, host, "mkdir -p " + shlex.quote(remote_runtime_repo)], timeout=60)
        remote_archive = remote_runtime_repo.rstrip("/") + "/runtime.tgz"
        run_checked_with_retries(["rsync", "-az", "-e", ssh_part, str(archive_path), f"{host}:{remote_archive}"], timeout=300)
        run_checked_with_retries(
            [*ssh_command, host, "tar -xzf " + shlex.quote(remote_archive) + " -C " + shlex.quote(remote_runtime_repo)],
            timeout=300,
        )
    sh_files = [rel for rel in synced if rel.endswith(".sh")]
    if sh_files:
        run_checked_with_retries(
            [*ssh_command, host, "cd " + shlex.quote(remote_runtime_repo) + " && chmod +x " + " ".join(shlex.quote(rel) for rel in sh_files)],
            timeout=60,
        )
    compile_targets = [rel for rel in ("scripts", "swebench", "workflows", *SYNC_DIRS) if rel in synced_dirs or any(item == rel or item.startswith(rel + "/") for item in synced)]
    if compile_targets:
        run_checked_with_retries(
            [*ssh_command, host, "cd " + shlex.quote(remote_runtime_repo) + " && python3 -m compileall -q " + " ".join(shlex.quote(rel) for rel in compile_targets)],
            timeout=180,
        )
    return {"remote_runtime_repo": remote_runtime_repo, "synced": synced, "synced_dirs": synced_dirs, "compile_targets": compile_targets}


def configure_run_paths(args: argparse.Namespace) -> None:
    if not args.run_id:
        args.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    if not args.base_run_dir:
        args.base_run_dir = DEFAULT_BASE_RUN_DIR_PREFIX + "_" + args.run_id
    if not args.remote_runtime_repo:
        args.remote_runtime_repo = str(Path(args.base_run_dir) / "_runtime" / "repo")


def terminate_remote_run(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
    timeout: int = 30,
) -> dict[str, Any]:
    cleanup = r'''
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

base = pathlib.Path(sys.argv[1])
needle = str(base)
me = os.getpid()
parent = os.getppid()


def send_pid(pid, sig):
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def send_pgid(pgid, sig):
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def scan():
    try:
        output = subprocess.check_output(["ps", "-eo", "pid=,pgid=,args="], text=True)
    except Exception:
        return []
    rows = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        args = parts[2]
        if pid in {me, parent}:
            continue
        if needle in args:
            rows.append((pid, pgid, args))
    return rows


killed = []
containers = []
try:
    for marker in base.rglob("container.id"):
        cid = marker.read_text(encoding="utf-8", errors="replace").strip()
        if cid:
            containers.append(cid)
except Exception:
    pass
runner_pid_path = base / "runner.pid"
try:
    runner_pid = int(runner_pid_path.read_text(encoding="utf-8").strip())
except Exception:
    runner_pid = 0
if runner_pid > 1 and runner_pid not in {me, parent}:
    try:
        runner_pgid = os.getpgid(runner_pid)
    except ProcessLookupError:
        runner_pgid = runner_pid
    if send_pgid(runner_pgid, signal.SIGTERM):
        killed.append({"pid": runner_pid, "pgid": runner_pgid, "signal": "TERM"})
    send_pid(runner_pid, signal.SIGTERM)

for sig_name, sig_value, delay in (("TERM", signal.SIGTERM, 2.0), ("KILL", signal.SIGKILL, 0.0)):
    for pid, pgid, _args in scan():
        if send_pgid(pgid, sig_value) or send_pid(pid, sig_value):
            killed.append({"pid": pid, "pgid": pgid, "signal": sig_name})
    if delay:
        time.sleep(delay)

container_results = []
for cid in sorted(set(containers)):
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", cid],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        container_results.append(
            {
                "cid": cid,
                "returncode": result.returncode,
                "stdout": result.stdout.strip()[:200],
                "stderr": result.stderr.strip()[:200],
            }
        )
    except Exception as exc:
        container_results.append({"cid": cid, "error": repr(exc)})

print(json.dumps({"killed": killed, "containers": container_results}, ensure_ascii=False))
'''
    result = subprocess.run(
        [*ssh_command, host, "python3 -c " + shlex.quote(cleanup) + " " + shlex.quote(base_run_dir)],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        detail = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = {"stdout": _redacted(result.stdout), "stderr": _redacted(result.stderr)}
    return {"returncode": result.returncode, "detail": detail}


def terminate_local_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        proc.wait()


def probe_terminal_remote_summary(
    *,
    ssh_command: list[str],
    host: str,
    base_run_dir: str,
) -> dict[str, Any] | None:
    probe = r'''
import json
import os
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
summary_path = base / "summary.json"
runner_pid_path = base / "runner.pid"
try:
    runner_pid = int(runner_pid_path.read_text(encoding="utf-8").strip())
except Exception:
    runner_pid = 0
runner_alive = False
if runner_pid > 1:
    try:
        os.kill(runner_pid, 0)
        runner_alive = True
    except ProcessLookupError:
        pass
    except PermissionError:
        runner_alive = True
try:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
except Exception:
    summary = None
print(json.dumps({"runner_alive": runner_alive, "summary": summary}, ensure_ascii=False))
'''
    command = [
        *ssh_command,
        host,
        "python3 -c " + shlex.quote(probe) + " " + shlex.quote(base_run_dir),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=REMOTE_COMPLETION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    summary = observed.get("summary")
    if observed.get("runner_alive") or not isinstance(summary, dict):
        return None
    if summary.get("status") not in REMOTE_TERMINAL_STATUSES:
        return None
    return summary


def remote_summary_matches_payload(
    summary: dict[str, Any], payload: dict[str, Any]
) -> bool:
    start_index = int(payload["start_index"])
    end_index = start_index + max(int(payload["limit"]), 0) - 1
    expected_slice = str(start_index) if end_index <= start_index else f"{start_index}-{end_index}"
    expected = {
        "slice": expected_slice,
        "base_run_dir": payload["base_run_dir"],
        "remote_runtime_repo": payload["remote_repo"],
        "invocation_id": payload["invocation_id"],
        "workflow": payload["workflow"],
        "workflow_env": payload["workflow_env"],
        "model_name": payload["model_name"],
        "llm_model": payload["llm_model"],
        "context_window": payload["context_window"],
        "temperature": payload["temperature"],
        "top_p": payload["top_p"],
        "max_output_tokens": payload["max_output_tokens"],
        "budget": payload["budget"],
        "max_steps": payload["max_steps"],
        "max_task_starts": max(1, min(3, int(payload["max_task_starts"]))),
        "max_empty_patch_retries": min(
            1, max(0, int(payload.get("max_empty_patch_retries", 1)))
        ),
        "max_eval_attempts": min(2, max(1, int(payload["max_eval_attempts"]))),
        "eval_only": payload["eval_only"],
        "eval_dir_name": payload["eval_dir_name"],
        "solver_attribution": "historical_artifact" if payload["eval_only"] else "current_run",
    }
    if payload.get("workflow") == "openhands-external":
        expected["openhands_empty_patch_rejections"] = max(
            0, int(payload.get("openhands_empty_patch_rejections", 2))
        )
    if payload.get("openhands_command"):
        expected["openhands_command_sha256"] = hashlib.sha256(
            payload["openhands_command"].encode("utf-8")
        ).hexdigest()
    return all(summary.get(key) == value for key, value in expected.items())


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    ssh_command = shlex.split(args.ssh_command)
    if args.eval_only:
        proxy_summary = {"status": "skipped_eval_only", "remote_proxy_base_url": args.remote_proxy_base_url}
        sync_summary = {}
    else:
        proxy_summary = ensure_remote_proxy(
            ssh_command=ssh_command,
            host=args.host,
            local_proxy_base_url=args.local_proxy_base_url,
            remote_proxy_base_url=args.remote_proxy_base_url,
            enabled=not args.no_ensure_remote_proxy,
        )
        sync_summary = {} if args.no_sync_runtime else sync_runtime(
            ssh_command=ssh_command,
            host=args.host,
            remote_runtime_repo=args.remote_runtime_repo,
        )
    selected_remote_proxy_base_url = proxy_summary.get(
        "remote_proxy_base_url", args.remote_proxy_base_url
    )
    payload = {
        "token": "" if args.eval_only else get_proxy_token(args.proxy_env_file),
        "remote_root": args.remote_root,
        "remote_repo": args.remote_runtime_repo,
        "base_run_dir": args.base_run_dir,
        "invocation_id": uuid.uuid4().hex,
        "workflow": args.workflow,
        "workflow_env": normalize_workflow_env(args.workflow_env),
        "openhands_command": args.openhands_command,
        "openhands_empty_patch_rejections": max(
            0, getattr(args, "openhands_empty_patch_rejections", 2)
        ),
        "max_empty_patch_retries": min(
            1, max(0, getattr(args, "max_empty_patch_retries", 1))
        ),
        "model_name": args.model_name,
        "llm_model": args.llm_model,
        "context_window": args.context_window,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_output_tokens": args.max_output_tokens,
        "session_prefix": args.session_prefix,
        "remote_proxy_base_url": selected_remote_proxy_base_url,
        "start_index": args.start_index,
        "limit": args.limit,
        "budget": args.budget,
        "max_steps": args.max_steps,
        "swe_timeout": args.swe_timeout,
        "task_wall_timeout": args.task_wall_timeout,
        "eval_timeout": args.eval_timeout,
        "llm_timeout": args.llm_timeout,
        "checkpoint_interval": args.checkpoint_interval,
        "max_task_starts": max(1, min(3, args.max_task_starts)),
        "max_eval_attempts": min(2, max(1, args.max_eval_attempts)),
        "eval_only": args.eval_only,
        "eval_dir_name": args.eval_dir_name,
        "dry_run": args.dry_run,
    }
    encoded = base64.b64encode(REMOTE_RUNNER.encode("utf-8")).decode("ascii")
    wrapper = "import base64; exec(base64.b64decode(%r).decode('utf-8'))" % encoded
    command = [*ssh_command, args.host, "python3 -c " + shlex.quote(wrapper)]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = time.monotonic()
    communicate_input: str | None = json.dumps(payload)
    recovered_summary: dict[str, Any] | None = None
    try:
        while True:
            remaining = args.total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, args.total_timeout)
            try:
                stdout, stderr = proc.communicate(
                    communicate_input,
                    timeout=min(REMOTE_COMPLETION_POLL_SECONDS, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                communicate_input = None
                recovered_summary = probe_terminal_remote_summary(
                    ssh_command=ssh_command,
                    host=args.host,
                    base_run_dir=args.base_run_dir,
                )
                if recovered_summary is not None and not remote_summary_matches_payload(
                    recovered_summary, payload
                ):
                    recovered_summary = None
                if recovered_summary is not None:
                    terminate_local_process_group(proc)
                    stdout = ""
                    stderr = ""
                    break
    except subprocess.TimeoutExpired as exc:
        cleanup = terminate_remote_run(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
        )
        terminate_local_process_group(proc)
        raise RuntimeError(f"remote run timed out after {args.total_timeout}s; cleanup={cleanup}") from exc
    except KeyboardInterrupt:
        cleanup = terminate_remote_run(
            ssh_command=ssh_command,
            host=args.host,
            base_run_dir=args.base_run_dir,
        )
        terminate_local_process_group(proc)
        print("interrupted; remote cleanup requested: " + json.dumps(cleanup, ensure_ascii=False), file=sys.stderr)
        raise
    if recovered_summary is not None:
        summary = recovered_summary
        summary["remote_transport"] = {
            "status": "recovered_terminal_summary",
            "base_run_dir": args.base_run_dir,
        }
        summary["runtime_sync"] = sync_summary
        summary["remote_proxy"] = proxy_summary
        return summary
    result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    if result.returncode not in (0, 1, 2):
        raise RuntimeError(_redacted(result.stderr or result.stdout or f"ssh exited {result.returncode}"))
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(_redacted(result.stdout[-4000:] or result.stderr[-4000:])) from exc
    summary["runtime_sync"] = sync_summary
    summary["remote_proxy"] = proxy_summary
    return summary


def write_local_report(summary: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = summary.get("markdown")
    if not isinstance(markdown, str):
        markdown = "# SWE Pro-Lite Report\n\nNo markdown was returned.\n"
    md_path.write_text(markdown, encoding="utf-8")


def _report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    for result in report.get("results") or []:
        if isinstance(result, dict):
            rows.extend(row for row in result.get("rows") or [] if isinstance(row, dict))
    return rows


def _row_eval_attempt_count(row: dict[str, Any]) -> int:
    evaluation = row.get("eval") if isinstance(row.get("eval"), dict) else {}
    attempts = evaluation.get("attempts")
    if isinstance(attempts, list) and attempts:
        return sum(_eval_result_executed(attempt) for attempt in attempts if isinstance(attempt, dict))
    if not _eval_result_executed(evaluation):
        return 0
    try:
        count = int(evaluation.get("attempt_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return count or 1


def _eval_result_executed(result: dict[str, Any]) -> bool:
    if result.get("executed") is False:
        return False
    return str(result.get("status") or "") not in {
        "",
        "would_eval",
        "skipped_no_generation_patch",
        "blocked_missing_eval_image",
    }


def _report_task_eval_counts(report: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in _report_rows(report):
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        counts[index] = counts.get(index, 0) + _row_eval_attempt_count(row)
    return counts


def _final_report_task_eval_counts(report: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for task in report.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        try:
            index = int(task.get("index"))
            count = int(
                task.get("observed_eval_attempt_count")
                or task.get("eval_attempt_count")
                or 0
            )
        except (TypeError, ValueError):
            continue
        counts[index] = max(counts.get(index, 0), count)
    return counts


class ParentEvalLock:
    def __init__(self, parent_output_dir: Path):
        self.path = parent_output_dir.resolve() / ".eval_only.lock"
        self.handle: Any | None = None

    def __enter__(self) -> "ParentEvalLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def parent_eval_lock(args: argparse.Namespace) -> ParentEvalLock:
    if not args.eval_only or args.parent_output_dir is None:
        raise RuntimeError("eval-only runs require a parent output directory")
    return ParentEvalLock(args.parent_output_dir)


def apply_parent_eval_budget(args: argparse.Namespace) -> dict[str, Any] | None:
    if not (args.eval_only and args.parent_output_dir):
        return None
    parent_summary = args.parent_output_dir.resolve() / "parallel_summary.json"
    if not parent_summary.exists():
        raise RuntimeError(f"missing parent parallel summary: {parent_summary}")
    report = json.loads(parent_summary.read_text(encoding="utf-8", errors="replace"))
    counts_by_index = _report_task_eval_counts(report)
    final_report_path = args.parent_output_dir.resolve() / "final_eval_layer_report.json"
    final_report_counts: dict[int, int] = {}
    if final_report_path.exists():
        final_report = json.loads(final_report_path.read_text(encoding="utf-8", errors="replace"))
        final_report_counts = _final_report_task_eval_counts(final_report)
        for index, count in final_report_counts.items():
            counts_by_index[index] = max(counts_by_index.get(index, 0), count)
    selected = range(args.start_index, args.start_index + max(args.limit, 0))
    remaining_by_index = {
        index: MAX_TOTAL_EVAL_ATTEMPTS - counts_by_index.get(index, 0)
        for index in selected
    }
    exhausted = [index for index, remaining in remaining_by_index.items() if remaining <= 0]
    if exhausted:
        joined = ", ".join(str(index) for index in exhausted)
        raise RuntimeError(
            f"eval retry budget exhausted for task indices: {joined}; max total is {MAX_TOTAL_EVAL_ATTEMPTS}"
        )
    effective_max_attempts = min(args.max_eval_attempts, *remaining_by_index.values())
    args.max_eval_attempts = effective_max_attempts
    return {
        "max_total_eval_attempts": MAX_TOTAL_EVAL_ATTEMPTS,
        "previous_eval_attempts": counts_by_index,
        "final_report_eval_attempts": final_report_counts,
        "remaining_by_index": remaining_by_index,
        "effective_max_eval_attempts": effective_max_attempts,
    }


def update_parent_fact_report(args: argparse.Namespace) -> dict[str, Any]:
    parent_output_dir = args.parent_output_dir.resolve()
    parent_summary = parent_output_dir / "parallel_summary.json"
    if not parent_summary.exists():
        raise RuntimeError(f"missing parent parallel summary: {parent_summary}")

    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "swe_eval_layer_report.py"),
        "--report-json",
        str(parent_summary),
        "--report-json",
        str(args.json_output.resolve()),
        "--max-rounds",
        "2",
        "--allow-over-budget-evidence",
        "--json-output",
        str(parent_output_dir / "final_eval_layer_report.json"),
        "--markdown-output",
        str(parent_output_dir / "final_eval_layer_report.md"),
    ]
    token_cost = parent_output_dir / "parallel_token_cost_summary.json"
    if token_cost.exists():
        command.extend(["--token-cost-json", str(token_cost)])
    if args.usd_cny is not None:
        command.extend(["--usd-cny", str(args.usd_cny)])
    proc = subprocess.run(command, text=True, capture_output=True, cwd=REPO_ROOT)
    log_path = parent_output_dir / "eval_only_reconciliation.log"
    log_path.write_text(proc.stdout + proc.stderr, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"parent fact report failed rc={proc.returncode}; see {log_path}")
    report_path = parent_output_dir / "final_eval_layer_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
    return {
        "status": "done",
        "report_json": str(report_path),
        "report_markdown": str(parent_output_dir / "final_eval_layer_report.md"),
        "counts": report.get("counts") if isinstance(report, dict) else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run G1.1 validation-council on a SWE-batch-pro-lite slice and evaluate it.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--ssh-command", default="ssh")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--remote-runtime-repo", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--base-run-dir", default="")
    parser.add_argument("--start-index", type=int, default=26)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--workflow", default="validation-council-solve")
    parser.add_argument("--workflow-env", action="append", default=[])
    parser.add_argument("--openhands-command", default="")
    parser.add_argument("--openhands-empty-patch-rejections", type=int, default=2)
    parser.add_argument("--max-empty-patch-retries", type=int, default=1)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--session-prefix", default="swe_g11_pro35_16m")
    parser.add_argument("--remote-proxy-base-url", default="http://127.0.0.1:18788")
    parser.add_argument("--local-proxy-base-url", default=DEFAULT_LOCAL_PROXY_BASE_URL)
    parser.add_argument("--proxy-env-file", type=Path, default=DEFAULT_PROXY_ENV_FILE)
    parser.add_argument("--budget", type=int, default=16_000_000)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--swe-timeout", type=int, default=14_400)
    parser.add_argument("--task-wall-timeout", type=int, default=15_300)
    parser.add_argument("--eval-timeout", type=int, default=7_200)
    parser.add_argument("--llm-timeout", type=int, default=900)
    parser.add_argument("--checkpoint-interval", type=int, default=300)
    parser.add_argument("--max-task-starts", type=int, default=3)
    parser.add_argument("--max-eval-attempts", type=int, default=2)
    parser.add_argument("--eval-only", action="store_true")
    parser.add_argument("--eval-dir-name", default="official_eval_v1_prolite26_35_20260707")
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--parent-output-dir", type=Path)
    parser.add_argument("--usd-cny", type=float)
    parser.add_argument("--no-sync-runtime", action="store_true")
    parser.add_argument("--no-ensure-remote-proxy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.eval_only and args.parent_output_dir is None:
        parser.error("--eval-only requires --parent-output-dir")
    configure_run_paths(args)
    if args.eval_only:
        with parent_eval_lock(args):
            parent_eval_budget = apply_parent_eval_budget(args)
            try:
                summary = run_remote(args)
            except KeyboardInterrupt:
                return 130
            write_local_report(summary, args.json_output, args.markdown_output)
            summary["parent_eval_budget"] = parent_eval_budget
            summary["parent_fact_report"] = update_parent_fact_report(args)
            write_local_report(summary, args.json_output, args.markdown_output)
    else:
        try:
            summary = run_remote(args)
        except KeyboardInterrupt:
            return 130
        write_local_report(summary, args.json_output, args.markdown_output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"done", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
