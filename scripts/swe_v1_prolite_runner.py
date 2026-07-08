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

SYNC_FILES = [
    "scripts/run_swe_v2_one_from_fifo.sh",
    "swebench/gen_prediction.py",
    "swebench/gen_prediction_workflow.py",
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
model_name = cfg["model_name"]
session_prefix = cfg["session_prefix"].rstrip("_")
remote_proxy_base_url = cfg["remote_proxy_base_url"].rstrip("/")
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


def eval_model_patch(prediction):
    return filter_model_patch_for_eval(prediction_patch(prediction))


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


def row_patch_sha(row):
    if not isinstance(row, dict):
        return ""
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return patch_sha(prediction_patch(row))


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


def generation_done(run_dir, task):
    prediction, metric, pairing = latest_pair(run_dir, task)
    patch = eval_model_patch(prediction)
    status = workflow_status(metric)
    return bool(patch.strip() and status in {"done", "done_with_timeout_patch"}), prediction, metric, pairing


def generation_done_result(task, prediction, metric, pairing, **extra):
    result = {
        "status": "generation_done",
        "task": task,
        "pairing": pairing,
        "patch_len": len(eval_model_patch(prediction)),
        "original_patch_len": len(prediction_patch(prediction)),
        "workflow_status": workflow_status(metric),
        "record_id": row_record_id(prediction),
        "patch_sha256": row_patch_sha(prediction),
    }
    result.update({key: value for key, value in extra.items() if value is not None})
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
    event = {"started_at": now(), "session": session, "workflow": workflow}
    starts.append(event)
    state.update({
        "schema": "opencollab.generation_state.v1",
        "task": task,
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


def compact_python_test_targets(tests, selected, max_args=80, max_chars=24000):
    targets = [str(item) for item in (tests or selected) if str(item)]
    if not targets:
        return []
    quoted = " ".join(shlex.quote(item) for item in targets)
    if len(targets) <= max_args and len(quoted) <= max_chars:
        return targets
    files = []
    for item in targets:
        path = item.split("::", 1)[0]
        if path and path not in files:
            files.append(path)
    return files or targets[:max_args]


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


def prolite_test_command(row, tests):
    language = str(row.get("repo_language") or "").lower()
    repo = str(row.get("repo") or "").lower()
    selected = parse_literal_list(row.get("selected_test_files_to_run"))
    tests = [str(item) for item in tests if str(item)]
    if language == "python" or any("::" in item for item in tests):
        targets = compact_python_test_targets(tests, selected)
        if targets:
            return "python3 -m pytest -q " + " ".join(shlex.quote(item) for item in targets)
    if language == "go" or repo.endswith("/vuls") or repo.endswith("/teleport") or repo.endswith("/navidrome"):
        names = []
        for item in tests or selected:
            head = str(item).split("/", 1)[0]
            if head and head not in names:
                names.append(head)
        if names:
            pattern = "^(" + "|".join(re.escape(name) for name in names) + ")(/.*)?$"
            return "go test ./... -run " + shlex.quote(pattern)
        return "go test ./..."
    if language in {"js", "javascript", "typescript"} or repo in {"nodebb/nodebb", "protonmail/webclients", "element-hq/element-web"}:
        files = [item.split(" | ", 1)[0] for item in (selected or tests) if item and ("/" in item or "." in item)]
        seen = []
        for item in files:
            if item not in seen:
                seen.append(item)
        target = " ".join(shlex.quote(item) for item in seen)
        if repo == "nodebb/nodebb":
            return js_runner_command("mocha", "test", target, "--timeout 30000")
        if repo == "element-hq/element-web":
            return js_runner_command("jest", "test", target)
        return js_runner_command("jest", "test", target)
    return str(row.get("test_cmd") or row.get("eval_cmd") or "true")


def generation_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    run_dir.mkdir(parents=True, exist_ok=True)
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if done:
        return generation_done_result(task, prediction, metric, pairing)
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
        return {"status": "would_generate", "task": task, "image": image, "workdir_status": workdir_status}
    fifo = pathlib.Path("/tmp") / f"opencollab_v1_{os.getpid()}_{int(time.time())}.fifo"
    os.mkfifo(fifo, 0o600)
    session = task_session(task)
    state = write_start_state(run_dir, task, session)
    log_path = run_dir / "generation_logs" / f"{task}.outer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "OPENCOLLAB_SWE_GENERATOR": "workflow",
        "OPENCOLLAB_SWE_WORKFLOW": workflow,
        "OPENCOLLAB_SWE_MODEL_NAME": model_name,
        "OPENCOLLAB_SWE_BUDGET": str(budget),
        "OPENCOLLAB_SWE_MAX_STEPS": str(max_steps),
        "OPENCOLLAB_SWE_TIMEOUT": str(swe_timeout),
        "OPENCOLLAB_LLM_TIMEOUT": str(cfg["llm_timeout"]),
        "OPENCOLLAB_SWE_DATASET": "swe-batch-pro-lite",
        "OPENCOLLAB_REMOTE_PROXY_BASE_URL": remote_proxy_base_url,
        "OPENCOLLAB_SWE_CHECKPOINT_INTERVAL_SECONDS": str(checkpoint_interval),
        "OPENCOLLAB_REMOTE_ROOT": str(remote_root),
        "OPENCOLLAB_REMOTE_REPO": str(remote_repo),
    })
    cmd = [
        str(remote_repo / "scripts" / "run_swe_v2_one_from_fifo.sh"),
        task,
        image,
        str(fifo),
        str(run_dir),
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


def eval_summary_matches_prediction(summary, prediction, task):
    if not isinstance(summary, dict) or summary.get("status") != "done":
        return False
    if not eval_model_patch(prediction).strip():
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
)


def eval_log_has_infra_failure(exit_status, log_text):
    if exit_status == 0:
        return False
    text = str(log_text or "")
    return any(pattern.lower() in text.lower() for pattern in EVAL_INFRA_FAILURE_PATTERNS)


def eval_for_task(row):
    task = row["instance_id"]
    run_dir = base_run_dir / task
    eval_dir = run_dir / "official_eval_v1_prolite26_35_20260707"
    report_path = eval_dir / "reports" / task / "report.json"
    summary_path = eval_dir / "summary.json"
    done, prediction, metric, pairing = generation_done(run_dir, task)
    if not done:
        if prediction is not None and metric is not None:
            original_model_patch = prediction_patch(prediction)
            model_patch = eval_model_patch(prediction)
            status = workflow_status(metric)
            if original_model_patch.strip() and not model_patch.strip() and status in {"done", "done_with_timeout_patch"}:
                summary = {
                    "status": "empty_eval_patch_invalid",
                    "task": task,
                    "resolved": False,
                    "patch_sha256": row_patch_sha(prediction),
                    "record_id": row_record_id(prediction),
                    "model_patch_chars": len(original_model_patch),
                    "eval_model_patch_chars": 0,
                    "technical_reasons": ["empty_eval_patch_after_filter"],
                    "pairing": pairing,
                }
                write_json(summary_path, summary)
                return {"status": "empty_eval_patch_invalid", "task": task, "summary": summary}
        return {"status": "skipped_no_generation_patch", "task": task, "pairing": pairing}
    previous = load_json(summary_path)
    if eval_summary_matches_prediction(previous, prediction, task):
        return {"status": "eval_done", "task": task, "summary": previous, "report_path": str(report_path)}
    if dry_run:
        return {"status": "would_eval", "task": task}
    image = image_for_row(row)
    image_status = ensure_image(image)
    if not image_status.get("ok"):
        return {"status": "blocked_missing_eval_image", "task": task, "image_status": image_status}
    input_dir = eval_dir / "input"
    output_dir = report_path.parent
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o777)
    original_model_patch = prediction_patch(prediction)
    model_patch = eval_model_patch(prediction)
    test_patch = str(row.get("test_patch") or "")
    fail_to_pass = parse_literal_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    pass_to_pass = parse_literal_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    f2p_cmd = prolite_test_command(row, fail_to_pass)
    p2p_cmd = prolite_test_command(row, pass_to_pass)
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
bash /tmp/prolite_before_repo.sh > /eval_output/before_repo.log 2>&1
echo "$?" > /eval_output/before_repo.exit
model_status=0
if [ -s /eval_input/model.patch ]; then
  git apply --whitespace=nowarn /eval_input/model.patch > /eval_output/model_patch.log 2>&1
  model_status=$?
  if [ "$model_status" -ne 0 ] && command -v patch >/dev/null 2>&1; then
    patch --batch -p1 < /eval_input/model.patch >> /eval_output/model_patch.log 2>&1
    model_status=$?
  fi
fi
echo "$model_status" > /eval_output/model_patch.exit
test_status=0
if [ "$model_status" -eq 0 ] && [ -s /eval_input/test.patch ]; then
  git apply --whitespace=nowarn /eval_input/test.patch > /eval_output/test_patch.log 2>&1
  test_status=$?
  if [ "$test_status" -ne 0 ] && command -v patch >/dev/null 2>&1; then
    patch --batch -p1 < /eval_input/test.patch >> /eval_output/test_patch.log 2>&1
    test_status=$?
  fi
fi
echo "$test_status" > /eval_output/test_patch.exit
if [ "$model_status" -eq 0 ] && [ "$test_status" -eq 0 ]; then
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
        proc = subprocess.run(docker_cmd, stdout=log, stderr=subprocess.STDOUT, timeout=eval_timeout + 120)
    docker_exit = proc.returncode

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

    before_status = read_exit("before_repo.exit")
    model_status = read_exit("model_patch.exit")
    test_status = read_exit("test_patch.exit")
    f2p_status = read_exit("f2p.exit")
    p2p_status = read_exit("p2p.exit", 0)
    f2p_log_tail = read_text("f2p.log")
    p2p_log_tail = read_text("p2p.log")
    technical_reasons = []
    if docker_exit != 0:
        technical_reasons.append("docker_exit")
    if before_status != 0:
        technical_reasons.append("before_repo")
    if model_status != 0:
        technical_reasons.append("model_patch")
    if test_status != 0:
        technical_reasons.append("test_patch")
    if eval_log_has_infra_failure(f2p_status, f2p_log_tail):
        technical_reasons.append("fail_to_pass_infra")
    if eval_log_has_infra_failure(p2p_status, p2p_log_tail):
        technical_reasons.append("pass_to_pass_infra")
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
        "record_id": row_record_id(prediction),
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "tests_status": {
            "before_repo_status": before_status,
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
            "model_patch_log_tail": read_text("model_patch.log"),
            "test_patch_log_tail": read_text("test_patch.log"),
        },
    }
    write_json(report_path, {task: report})
    summary = {
        "status": summary_status,
        "task": task,
        "resolved": resolved,
        "patch_sha256": row_patch_sha(prediction),
        "record_id": row_record_id(prediction),
        "model_patch_chars": len(original_model_patch),
        "eval_model_patch_chars": len(model_patch),
        "technical_reasons": technical_reasons,
        "report_path": str(report_path),
        "command_log": str(command_log),
        "tests_status": report["tests_status"],
    }
    write_json(summary_path, summary)
    return {"status": "eval_done" if not technical_error else "technical_eval_failed", "task": task, "summary": summary, "report_path": str(report_path)}


def write_markdown(summary):
    lines = [
        f"# SWE G1.1 Pro-Lite {summary.get('slice', slice_label())} Report",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- base_run_dir: `{summary['base_run_dir']}`",
        f"- remote_runtime_repo: `{summary['remote_runtime_repo']}`",
        f"- workflow: `{summary['workflow']}`",
        f"- tasks: `{summary['counts']['tasks']}`",
        f"- generation_done: `{summary['counts']['generation_done']}`",
        f"- eval_done: `{summary['counts']['eval_done']}`",
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
            or row.get("eval", {}).get("summary", {}).get("patch_sha256")
            or ""
        )
        lines.append(
            "| {idx} | `{task}` | `{gen}` | `{ev}` | `{resolved}` | `{patch}` | `{report}` |".format(
                idx=row["index"],
                task=row["task"],
                gen=row.get("generation", {}).get("status", ""),
                ev=row.get("eval", {}).get("status", ""),
                resolved=row.get("eval", {}).get("summary", {}).get("resolved", ""),
                patch=patch_sha[:12],
                report=report,
            )
        )
    summary["markdown"] = "\n".join(lines) + "\n"


def main():
    config_errors = validate_runner_config()
    if config_errors:
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "invalid_config",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "config_errors": config_errors,
            "counts": {"tasks": 0, "generation_done": 0, "eval_done": 0, "resolved": 0, "unresolved": 0, "technical_failed": 1},
            "rows": [],
        }
        write_json(base_run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    preflight = {
        "dataset_exists": dataset_path.exists(),
        "remote_root_exists": remote_root.exists(),
        "remote_repo_exists": remote_repo.exists(),
        "proxy_health": http_health(remote_proxy_base_url + "/healthz", timeout=45),
    }
    if not all([preflight["dataset_exists"], preflight["remote_root_exists"], preflight["remote_repo_exists"], preflight["proxy_health"].get("ok")]):
        summary = {
            "schema": "opencollab.swe_g11_prolite_runner.v1",
            "status": "preflight_failed",
            "generated_at": now(),
            "slice": slice_label(),
            "base_run_dir": str(base_run_dir),
            "remote_runtime_repo": str(remote_repo),
            "workflow": workflow,
            "preflight": preflight,
            "counts": {"tasks": 0, "generation_done": 0, "eval_done": 0, "resolved": 0, "unresolved": 0, "technical_failed": 1},
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
        gen = generation_for_task(row)
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "generation", "task": task, "result": gen})
        if dry_run and gen.get("status") in {"would_generate", "generation_done"}:
            ev = {"status": "would_eval", "task": task}
        else:
            ev = eval_for_task(row)
        append_jsonl(base_run_dir / "events.jsonl", {"time": now(), "phase": "eval", "task": task, "result": ev})
        result_rows.append({"index": offset, "task": task, "generation": gen, "eval": ev})
    generation_ok_statuses = {"generation_done"}
    eval_ok_statuses = {"eval_done"}
    if dry_run:
        generation_ok_statuses.add("would_generate")
        eval_ok_statuses.add("would_eval")
    counts = {
        "tasks": len(result_rows),
        "generation_done": sum(1 for row in result_rows if row["generation"].get("status") == "generation_done"),
        "would_generate": sum(1 for row in result_rows if row["generation"].get("status") == "would_generate"),
        "eval_done": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done"),
        "would_eval": sum(1 for row in result_rows if row["eval"].get("status") == "would_eval"),
        "resolved": sum(1 for row in result_rows if row["eval"].get("summary", {}).get("resolved") is True),
        "unresolved": sum(1 for row in result_rows if row["eval"].get("status") == "eval_done" and row["eval"].get("summary", {}).get("resolved") is False),
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
        "schema": "opencollab.swe_g11_prolite_runner.v1",
        "status": status,
        "generated_at": now(),
        "slice": slice_label(),
        "base_run_dir": str(base_run_dir),
        "remote_runtime_repo": str(remote_repo),
        "workflow": workflow,
        "model_name": model_name,
        "preflight": preflight,
        "counts": counts,
        "rows": result_rows,
    }
    write_markdown(summary)
    write_json(base_run_dir / "summary.json", summary)
    (base_run_dir / "summary.md").write_text(summary["markdown"], encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if counts["technical_failed"] == 0 else 1


def validate_runner_config():
    errors = []
    if start_index < 1:
        errors.append("start_index must be >= 1")
    if limit <= 0:
        errors.append("limit must be > 0")
    if max_task_starts < 1:
        errors.append("max_task_starts must be >= 1")
    return errors


raise SystemExit(main())
'''


def _redacted(text: str) -> str:
    text = re.sub(r"(GLM_PROXY_CLIENT_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_AUTH_TOKEN=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(ANTHROPIC_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(OPENCOLLAB_API_KEY=)\S+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}", r"\1[redacted]", text)
    return text


def run_checked(command: list[str], *, timeout: int = 120, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, input=input_text, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(_redacted(result.stderr or result.stdout or f"{command[0]} exited {result.returncode}"))
    return result


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
            run_checked(command, timeout=30)
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
        run_checked([*ssh_command, host, "mkdir -p " + shlex.quote(remote_runtime_repo)], timeout=60)
        remote_archive = remote_runtime_repo.rstrip("/") + "/runtime.tgz"
        run_checked(["rsync", "-az", "-e", ssh_part, str(archive_path), f"{host}:{remote_archive}"], timeout=300)
        run_checked([*ssh_command, host, "tar -xzf " + shlex.quote(remote_archive) + " -C " + shlex.quote(remote_runtime_repo)], timeout=300)
    sh_files = [rel for rel in synced if rel.endswith(".sh")]
    if sh_files:
        run_checked(
            [*ssh_command, host, "cd " + shlex.quote(remote_runtime_repo) + " && chmod +x " + " ".join(shlex.quote(rel) for rel in sh_files)],
            timeout=60,
        )
    compile_targets = [rel for rel in ("scripts", "swebench", "workflows", *SYNC_DIRS) if rel in synced_dirs or any(item == rel or item.startswith(rel + "/") for item in synced)]
    if compile_targets:
        run_checked(
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


def run_remote(args: argparse.Namespace) -> dict[str, Any]:
    ssh_command = shlex.split(args.ssh_command)
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
        "token": get_proxy_token(args.proxy_env_file),
        "remote_root": args.remote_root,
        "remote_repo": args.remote_runtime_repo,
        "base_run_dir": args.base_run_dir,
        "workflow": args.workflow,
        "model_name": args.model_name,
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
        "max_task_starts": args.max_task_starts,
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
    try:
        stdout, stderr = proc.communicate(json.dumps(payload), timeout=args.total_timeout)
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
        markdown = "# SWE G1.1 Pro-Lite Report\n\nNo markdown was returned.\n"
    md_path.write_text(markdown, encoding="utf-8")


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
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
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
    parser.add_argument("--max-task-starts", type=int, default=1)
    parser.add_argument("--total-timeout", type=int, default=240_000)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--no-sync-runtime", action="store_true")
    parser.add_argument("--no-ensure-remote-proxy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.start_index < 1:
        parser.error("--start-index must be >= 1")
    if args.limit <= 0:
        parser.error("--limit must be > 0")
    if args.max_task_starts < 1:
        parser.error("--max-task-starts must be >= 1")
    configure_run_paths(args)

    try:
        summary = run_remote(args)
    except KeyboardInterrupt:
        return 130
    write_local_report(summary, args.json_output, args.markdown_output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"done", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
