"""Bounded records, dataset, patch identity, and image helpers."""

# ruff: noqa: F403, F405

from opencollab.harness.swe_v1_remote_core import *
from opencollab.harness.swe_v1_remote_state import *


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def iter_jsonl(path, max_scan_bytes=None, max_rows=None):
    try:
        context = open_regular_binary(path)
        handle = context.__enter__()
    except FileNotFoundError:
        return
    try:
        opened = os.fstat(handle.fileno())
        if max_scan_bytes is not None and opened.st_size > max_scan_bytes:
            raise RecordInputLimitError(f"JSONL input exceeds {max_scan_bytes} bytes: {path}")
        remaining = opened.st_size
        physical_rows = 0
        while True:
            if remaining <= 0:
                break
            line = handle.readline(min(MAX_JSONL_LINE_BYTES + 1, remaining))
            if not line:
                break
            remaining -= len(line)
            physical_rows += 1
            if max_rows is not None and physical_rows > max_rows:
                raise RecordInputLimitError(f"JSONL input exceeds {max_rows} physical rows: {path}")
            if len(line) > MAX_JSONL_LINE_BYTES:
                raise RecordInputLimitError(f"JSONL line exceeds {MAX_JSONL_LINE_BYTES} bytes: {path}")
            if not line.strip():
                raise RecordInputFormatError(f"blank JSONL record in {path}")
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RecordInputFormatError(f"invalid JSONL record in {path}") from exc
            if not isinstance(value, dict):
                raise RecordInputFormatError(f"JSONL record must be an object: {path}")
            yield len(line), value
    finally:
        context.__exit__(None, None, None)


def read_jsonl(path):
    rows = deque()
    retained_bytes = 0
    for line_size, value in iter_jsonl(
        path,
        max_scan_bytes=MAX_JSONL_SCAN_BYTES,
    ):
        rows.append((line_size, value))
        retained_bytes += line_size
        if len(rows) > MAX_JSONL_RETAINED_ROWS or retained_bytes > MAX_JSONL_RETAINED_BYTES:
            raise RecordInputLimitError(f"JSONL input exceeds retained row or byte limit: {path}")
    return [value for _size, value in rows]


def read_tail_text(path, limit=4000):
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError, OverflowError):
        return ""
    limit = min(limit, MAX_LOG_TAIL_BYTES)
    if limit == 0:
        return ""
    try:
        with open_regular_binary(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - limit), os.SEEK_SET)
            return handle.read(limit).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def write_json(path, value):
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def append_jsonl(path, value):
    payload = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_JSONL_LINE_BYTES:
        raise RecordInputLimitError(f"JSONL row exceeds byte limit: {path}")
    fd = open_regular_file(path, os.O_RDWR | os.O_APPEND)
    locked = False
    try:
        acquire_lock(fd, f"JSONL output lock {path}")
        locked = True
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_DURABLE_JSONL_BYTES:
            raise RecordInputLimitError(f"JSONL output exceeds byte limit: {path}")
        if needs_separator:
            write_all(fd, b"\n")
        write_all(fd, payload)
        os.fsync(fd)
        fsync_directory(path.parent)
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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


def load_dataset(selected_start, selected_limit):
    if not dataset_path.exists():
        raise RuntimeError(f"missing dataset: {dataset_path}")
    rows = []
    for index, (_line_size, value) in enumerate(
        iter_jsonl(
            dataset_path,
            max_scan_bytes=MAX_DATASET_BYTES,
            max_rows=MAX_DATASET_ROWS,
        ),
        1,
    ):
        if index < selected_start:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"dataset row {index} must be an object")
        row = dict(value)
        row["instance_id"] = validate_task_identity(row.get("instance_id"))
        rows.append(row)
        if len(rows) >= selected_limit:
            break
    return rows


def validate_task_identity(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("instance_id must be one non-empty path component")
    windows_path = pathlib.PureWindowsPath(value)
    if os.path.isabs(value) or windows_path.is_absolute() or windows_path.drive or "/" in value or "\\" in value:
        raise ValueError("instance_id must be one non-empty path component")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value):
        raise ValueError("instance_id must not contain control, format, or surrogate characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("instance_id must be valid UTF-8 text") from exc
    if len(encoded) > MAX_TASK_ID_BYTES:
        raise ValueError(f"instance_id exceeds {MAX_TASK_ID_BYTES} UTF-8 bytes")
    return value


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
    normalized = str(path or "").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else normalized
    if any(part in {"test", "tests", "__tests__"} for part in parts):
        return True
    return (
        name == "conftest.py"
        or name.endswith("_test.go")
        or name.startswith("test_")
        and name.endswith(".py")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


GIT_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def decode_git_c_path(value):
    value = str(value or "")
    quoted = value.startswith('"')
    index = 1 if quoted else 0
    decoded = bytearray()
    while index < len(value):
        char = value[index]
        if quoted and char == '"':
            break
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogatepass"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            decoded.append(ord("\\"))
            break
        escaped = value[index]
        if escaped in "01234567":
            end = index
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        decoded.append(GIT_C_ESCAPES.get(escaped, ord(escaped)))
        index += 1
    return decoded.decode("utf-8", errors="surrogateescape")


def git_header_tokens(header):
    text = str(header or "").strip()
    prefix = "diff --git "
    if not text.startswith(prefix):
        return []
    text = text[len(prefix) :]
    tokens = []
    index = 0
    while index < len(text) and len(tokens) < 2:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        start = index
        if text[index] == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
        else:
            while index < len(text) and not text[index].isspace():
                index += 1
        tokens.append(text[start:index])
    return tokens


def diff_target_path(header):
    match = re.match(r"^diff --git a/(.*) b/(.*)$", str(header or "").strip())
    if match:
        return match.group(2)
    paths = git_header_tokens(header)
    if len(paths) >= 2:
        target = decode_git_c_path(paths[1])
        if target.startswith("b/"):
            return target[2:]
    if paths:
        source = decode_git_c_path(paths[0])
        if source.startswith("a/"):
            return source[2:]
    return ""


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
        path = diff_target_path(header)
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
    patch = prediction_patch(row)
    if patch:
        return patch_sha(patch)
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def row_explicit_patch_sha(row):
    if not isinstance(row, dict):
        return ""
    for key in ("patch_sha256", "patch_sha", "model_patch_sha256"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def embedded_workflow_metric(row):
    if not isinstance(row, dict):
        return None
    metric = row.get("workflow_metric")
    if not isinstance(metric, dict):
        return None
    if row_task_id(metric) != row_task_id(row):
        return None
    if row_record_id(metric) != row_record_id(row):
        return None
    prediction_sha = row_patch_sha(row)
    metric_sha = row_patch_sha(metric)
    if not prediction_sha or not metric_sha or not patch_sha_matches(prediction_sha, metric_sha):
        return None
    return metric


def patch_sha_matches(left, right):
    left = str(left or "")
    right = str(right or "")
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", left) and re.fullmatch(r"[0-9a-fA-F]{64}", right) and left == right)


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
            embedded_metric = embedded_workflow_metric(prediction)
            if embedded_metric is not None:
                return prediction, embedded_metric, "embedded_metric"
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
    return completed_generation_identity(prediction, metric, task), prediction, metric, pairing


def completed_generation_identity(prediction, metric, task):
    if not isinstance(prediction, dict) or not isinstance(metric, dict):
        return False
    original_patch = prediction_patch(prediction)
    if not original_patch.strip() or not eval_model_patch(prediction).strip():
        return False
    if row_task_id(prediction) != task or row_task_id(metric) != task:
        return False
    prediction_record_id = row_record_id(prediction)
    if not prediction_record_id or row_record_id(metric) != prediction_record_id:
        return False
    computed_sha = patch_sha(original_patch)
    if not patch_sha_matches(row_explicit_patch_sha(prediction), computed_sha):
        return False
    if not patch_sha_matches(row_explicit_patch_sha(metric), computed_sha):
        return False
    if metric_submission_integrity(metric) == SUBMISSION_INTEGRITY_INELIGIBLE:
        return False
    returncode = metric.get("runner_returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return False
    status = workflow_status(metric)
    if status == "done":
        return returncode == 0
    if status == "done_with_timeout_patch":
        return returncode == 124
    return False


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
        if "/" in tag:
            return tag
        return image_repository + ":" + tag
    task = str(row.get("instance_id") or "")
    key = task[len("instance_") :] if task.startswith("instance_") else task
    return image_repository + ":" + key


def image_exists(image):
    return run(["docker", "image", "inspect", image], timeout=120)["returncode"] == 0


def ensure_image(image):
    if image_exists(image):
        return {"ok": True, "image": image}
    pulled = run(["docker", "pull", image], timeout=900)
    if pulled["returncode"] == 0 and image_exists(image):
        return {"ok": True, "image": image, "pulled": True}
    return {
        "ok": False,
        "image": image,
        "reason": "missing_image",
        "details": pulled["stderr"] or pulled["stdout"],
    }


PREFLIGHT_OWNER_LABEL = "opencollab.prolite.owner_nonce"
PREFLIGHT_SCHEMA_LABEL = "opencollab.prolite.schema"
PREFLIGHT_SCHEMA = "image-preflight-v1"


def _preflight_container_state(reference):
    result = run(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            (
                '{{.Id}}\t{{index .Config.Labels "'
                + PREFLIGHT_OWNER_LABEL
                + '"}}\t{{index .Config.Labels "'
                + PREFLIGHT_SCHEMA_LABEL
                + '"}}'
            ),
            "--",
            reference,
        ],
        timeout=30,
    )
    details = str(result.get("stderr") or result.get("stdout") or "")
    if result.get("returncode") != 0:
        lowered = details.lower()
        if "no such container" in lowered or "no such object" in lowered:
            return {"ok": True, "absent": True}
        return {"ok": False, "absent": False, "details": details[-1000:]}
    parts = str(result.get("stdout") or "").split("\t")
    if len(parts) != 3 or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None:
        return {"ok": False, "absent": False, "details": "invalid container inspection evidence"}
    return {
        "ok": True,
        "absent": False,
        "container_id": parts[0],
        "owner_nonce": parts[1],
        "schema": parts[2],
    }


def cleanup_preflight_container(cidfile, container_name):
    references = []
    try:
        with open_regular_binary(cidfile) as handle:
            size = os.fstat(handle.fileno()).st_size
            raw = handle.read(129) if size <= 128 else b""
        cid = raw.decode("ascii").strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        cid = ""
    if re.fullmatch(r"[0-9a-f]{64}", cid):
        references.append(cid)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(container_name or "")):
        references.append(str(container_name))
    removed_ids = set()
    attempts = []
    for reference in references:
        state = _preflight_container_state(reference)
        if not state.get("ok"):
            return {"ok": False, "status": "inspect_failed", "reference": reference, "details": state}
        if state.get("absent"):
            attempts.append({"reference": reference, "status": "absent"})
            continue
        container_id = str(state.get("container_id") or "")
        if state.get("owner_nonce") != owner_nonce or state.get("schema") != PREFLIGHT_SCHEMA:
            return {
                "ok": False,
                "status": "ownership_unproven",
                "reference": reference,
                "container_id": container_id,
            }
        if container_id in removed_ids:
            continue
        removal = run(["docker", "rm", "-f", "--", container_id], timeout=60)
        after = _preflight_container_state(container_id)
        if not after.get("ok") or not after.get("absent"):
            return {
                "ok": False,
                "status": "remove_failed",
                "reference": reference,
                "container_id": container_id,
                "remove_returncode": removal.get("returncode"),
                "details": after,
            }
        removed_ids.add(container_id)
        attempts.append({"reference": reference, "container_id": container_id, "status": "removed"})
    try:
        cidfile.unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "status": "cidfile_cleanup_failed", "details": str(exc)}
    return {"ok": True, "status": "absent", "attempts": attempts}


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
    container_name = "opencollab-prolite-preflight-" + uuid.uuid4().hex[:24]
    cidfile = base_run_dir / ("." + container_name + ".cid")
    command = [
        "timeout",
        "120",
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--cidfile",
        str(cidfile),
        "--label",
        f"{PREFLIGHT_OWNER_LABEL}={owner_nonce}",
        "--label",
        f"{PREFLIGHT_SCHEMA_LABEL}={PREFLIGHT_SCHEMA}",
        "--network",
        "none",
        "--entrypoint",
        "",
        image,
        "bash",
        "-lc",
        script,
    ]
    try:
        result = run(command, timeout=150)
    finally:
        cleanup = cleanup_preflight_container(cidfile, container_name)
    return {
        "ok": result["returncode"] == 0 and cleanup.get("ok") is True,
        "image": image,
        "returncode": result["returncode"],
        "details": result["stderr"] or result["stdout"],
        "container_cleanup": cleanup,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
