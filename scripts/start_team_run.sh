#!/usr/bin/env bash
# Start one SWE-bench team prediction run.
#
# Drives the OpenCollab team inside the official sweb.eval container by running
# the *existing* `opencollab` CLI inside the container (via docker exec -it), so
# team progress renders live in the TUI. The host's repo and uv-managed Python
# are mounted into the container at the same paths the venv shebangs reference,
# so the venv's `opencollab` binary works out of the box — no in-container
# install. After the team finishes (or hits --timeout), `git diff` from
# /testbed is appended to the predictions JSONL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$REPO_ROOT/opencollab"
VENV_DIR="$PROJECT_DIR/.venv"
DEFAULT_TEAM_FILE="$REPO_ROOT/configs/team.self.collab.yaml"
DEFAULT_ENV_FILE="$REPO_ROOT/configs/.env"

usage() {
    cat <<'EOF'
Usage:
  scripts/start_team_run.sh \
      --instance-file <instance.json> \
      --output <predictions.jsonl> \
      [--team-file <team.yaml>] \
      [--timeout <seconds>] \
      [--model <name>] \
      [--model-name <preds tag>] \
      [--arch <x86_64|arm64>] \
      [--image <override>] \
      [--network <docker-network-mode>] \
      [--session-root <host-dir>] \
      [--mount <host[:container[:ro|rw]]>] \
      [--mount-home-ro] \
      [--no-hints] \
      [--keep-container]

Runs the OpenCollab team inside the official sweb.eval container, with TUI
progress rendered live (via docker exec -it). The team is driven by the
`opencollab` CLI directly — no Python wrapper on the host.

Defaults:
  --team-file  configs/team.self.collab.yaml
  --timeout    1800 seconds
  --arch       x86_64
  --network    host

Configuration:
  configs/.env is mounted into the container as OPENCOLLAB_CONFIG_FILE so the
  same API key + model resolution applies inside.
  /testbed/.opencollab is mounted to .opencollab/swebench/<instance_id> by
  default, so agent session history survives container cleanup.

Extra mounts:
  --mount defaults to read-only and may be repeated. Use --mount-home-ro only
  when the provider or tools really need host-level config/cache files.

Ablation:
  --no-hints omits the issue's maintainer hints_text from the task prompt
  (for measuring how much hints contribute to resolve rate).
EOF
}

die() { echo "error: $*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

find_timeout_bin() {
    if command -v timeout >/dev/null 2>&1; then
        command -v timeout
    elif command -v gtimeout >/dev/null 2>&1; then
        command -v gtimeout
    else
        printf ''
    fi
}

normalize_mount() {
    local spec="$1"
    local host=""
    local dest=""
    local mode=""
    local extra=""

    IFS=: read -r host dest mode extra <<< "$spec"
    [ -z "$extra" ] || die "invalid --mount '$spec' (expected host[:container[:ro|rw]])"
    [ -n "$host" ] || die "invalid --mount '$spec' (missing host path)"
    [ -e "$host" ] || die "mount source not found: $host"

    host="$(readlink -f "$host")"
    dest="${dest:-$host}"
    mode="${mode:-ro}"
    case "$mode" in
        ro|rw) ;;
        *) die "invalid mount mode '$mode' for $spec (expected ro or rw)" ;;
    esac

    printf '%s:%s:%s\n' "$host" "$dest" "$mode"
}

prepare_real_directory() {
    local python_bin="$1"
    local candidate="$2"
    local containment_root="${3:-}"
    "$python_bin" - "$candidate" "$containment_root" <<'PY'
import os
import pathlib
import stat
import sys
import unicodedata

candidate = os.path.abspath(sys.argv[1])
containment = os.path.abspath(sys.argv[2]) if sys.argv[2] else ""
path = pathlib.Path(candidate)
if any(
    unicodedata.category(character) in {"Cc", "Cf", "Cs"}
    for character in candidate
):
    raise SystemExit("directory path contains unsafe characters")
if containment:
    try:
        contained = os.path.commonpath((candidate, containment)) == containment
    except ValueError:
        contained = False
    if not contained or candidate == containment:
        raise SystemExit("directory path escapes its required host root")

flags = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
fd = os.open(path.anchor or os.sep, flags)
try:
    for component in path.parts[1:]:
        if component in {"", ".", ".."}:
            raise SystemExit("directory path contains a dot component")
        try:
            child = os.open(component, flags, dir_fd=fd)
        except FileNotFoundError:
            os.mkdir(component, 0o755, dir_fd=fd)
            child = os.open(component, flags, dir_fd=fd)
        opened = os.fstat(child)
        if not stat.S_ISDIR(opened.st_mode):
            os.close(child)
            raise SystemExit("directory path contains a non-directory component")
        os.close(fd)
        fd = child
finally:
    os.close(fd)
print(candidate)
PY
}

main() {
    local instance_file=""
    local output=""
    local team_file="$DEFAULT_TEAM_FILE"
    local timeout=1800
    local model=""
    local model_name=""
    local arch="x86_64"
    local image=""
    local network="host"
    local session_root=""
    local keep=0
    local mount_home_ro=0
    local include_hints=1
    local -a extra_mounts=()

    while (($#)); do
        case "$1" in
            -h|--help|help) usage; exit 0 ;;
            --instance-file) instance_file="${2:?}"; shift 2 ;;
            --output) output="${2:?}"; shift 2 ;;
            --team-file) team_file="${2:?}"; shift 2 ;;
            --timeout) timeout="${2:?}"; shift 2 ;;
            --model) model="${2:?}"; shift 2 ;;
            --model-name) model_name="${2:?}"; shift 2 ;;
            --arch) arch="${2:?}"; shift 2 ;;
            --image) image="${2:?}"; shift 2 ;;
            --network) network="${2:?}"; shift 2 ;;
            --session-root) session_root="${2:?}"; shift 2 ;;
            --mount) extra_mounts+=("${2:?}"); shift 2 ;;
            --mount-home-ro) mount_home_ro=1; shift ;;
            --no-hints) include_hints=0; shift ;;
            --keep-container) keep=1; shift ;;
            *) die "unknown argument: $1 (use --help)" ;;
        esac
    done

    [ -n "$instance_file" ] || die "--instance-file is required"
    [ -n "$output" ]        || die "--output is required"
    [ -f "$instance_file" ] || die "instance file not found: $instance_file"
    [ -f "$team_file" ]     || die "team file not found: $team_file"

    # The team file is read INSIDE the container, where opencollab runs with cwd
    # /testbed and the repo is bind-mounted at its absolute host path. A relative
    # path would resolve to /testbed/<rel> (which doesn't exist) and silently
    # fall back to the lead-only default team. Force an absolute, mounted path.
    team_file="$(readlink -f "$team_file")"
    case "$team_file" in
        "$REPO_ROOT"/*) ;;
        *) die "team file must live under $REPO_ROOT (it's mounted into the container): $team_file" ;;
    esac

    require_cmd docker
    local timeout_bin
    timeout_bin="$(find_timeout_bin)"
    [ -n "$timeout_bin" ] || die "neither timeout nor gtimeout is installed; refusing an unbounded team run"

    local oc_bin="$VENV_DIR/bin/opencollab"
    [ -x "$oc_bin" ] || die "missing $oc_bin — run scripts/start_opencollab.sh once to build the venv."
    local py_bin="$VENV_DIR/bin/python"
    [ -x "$py_bin" ] || die "missing $py_bin — run scripts/start_opencollab.sh once to build the venv."
    "$py_bin" - "$timeout" <<'PY' >/dev/null
import math
import sys

try:
    timeout = float(sys.argv[1])
except ValueError as exc:
    raise SystemExit("--timeout must be a finite positive number") from exc
if not math.isfinite(timeout) or timeout <= 0:
    raise SystemExit("--timeout must be a finite positive number")
PY

    # uv-managed Python: the venv's bin/python is a symlink into ~/.local/share/uv/python/...
    # The container needs that directory mounted so the symlink resolves inside.
    local py_real
    py_real="$(readlink -f "$py_bin")"
    [ -x "$py_real" ] || die "cannot resolve venv interpreter at $VENV_DIR/bin/python"
    local py_root
    py_root="$(dirname "$(dirname "$py_real")")"

    local task_file
    task_file="$(mktemp -t oc_task.XXXXXX)"
    local iid=""
    if ! iid="$(OC_INCLUDE_HINTS="$include_hints" "$py_bin" - \
        "$instance_file" "$task_file" <<'PY'
import json
import os
import pathlib
import stat
import sys
import unicodedata

MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_INSTANCE_ID_BYTES = 240
MAX_TASK_PROMPT_BYTES = 4 * 1024 * 1024


def read_instance(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INSTANCE_BYTES:
        raise SystemExit("instance file must be a bounded regular JSON file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SystemExit("instance file changed while opening")
        chunks = []
        remaining = MAX_INSTANCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        final_entry = path.lstat()
        expected_identity = (before.st_dev, before.st_ino)
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(final_entry.st_mode)
            or (after.st_dev, after.st_ino) != expected_identity
            or (final_entry.st_dev, final_entry.st_ino) != expected_identity
            or opened.st_size != before.st_size
            or after.st_size != before.st_size
            or final_entry.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or after.st_mtime_ns != before.st_mtime_ns
            or final_entry.st_mtime_ns != before.st_mtime_ns
            or opened.st_ctime_ns != before.st_ctime_ns
            or after.st_ctime_ns != before.st_ctime_ns
            or final_entry.st_ctime_ns != before.st_ctime_ns
            or len(raw) != before.st_size
        ):
            raise SystemExit("instance file changed while reading")
    finally:
        os.close(fd)
    if len(raw) > MAX_INSTANCE_BYTES:
        raise SystemExit("instance file exceeds byte limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("instance file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SystemExit("instance file must contain one JSON object")
    return value


def validate_instance_id(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise SystemExit("instance_id must be one non-empty path component")
    windows_path = pathlib.PureWindowsPath(value)
    if (
        os.path.isabs(value)
        or windows_path.is_absolute()
        or windows_path.drive
        or "/" in value
        or "\\" in value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise SystemExit("instance_id must be one safe path component")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SystemExit("instance_id must be valid UTF-8 text") from exc
    if len(encoded) > MAX_INSTANCE_ID_BYTES:
        raise SystemExit("instance_id exceeds its UTF-8 byte limit")
    return value


def write_task(path, payload):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit("task prompt target must be a regular file")
    flags = (
        os.O_RDWR
        | os.O_TRUNC
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SystemExit("task prompt target changed while opening")
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short task prompt write")
            view = view[written:]
        os.fsync(fd)
        after = os.fstat(fd)
        current = path.lstat()
        identity = (before.st_dev, before.st_ino)
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (after.st_dev, after.st_ino) != identity
            or (current.st_dev, current.st_ino) != identity
            or after.st_size != len(payload)
            or current.st_size != len(payload)
            or os.pread(fd, len(payload) + 1, 0) != payload
        ):
            raise SystemExit("task prompt target changed while writing")
    finally:
        os.close(fd)


instance = read_instance(pathlib.Path(sys.argv[1]))
iid = validate_instance_id(instance.get("instance_id"))
include_hints = os.environ.get("OC_INCLUDE_HINTS", "1") == "1"
repo_name = instance.get("repo") or ""
problem_statement = instance.get("problem_statement") or ""
hints_text = (instance.get("hints_text") or "").strip() if include_hints else ""
fail_to_pass = instance.get("FAIL_TO_PASS") or []
if isinstance(fail_to_pass, str):
    try:
        fail_to_pass = json.loads(fail_to_pass)
    except json.JSONDecodeError:
        fail_to_pass = []
if not isinstance(fail_to_pass, list):
    fail_to_pass = []

lines = [f"# Issue to fix in `{repo_name}`", "", str(problem_statement), ""]
if hints_text:
    lines.extend(
        [
            "## Maintainer hints from the issue thread",
            "",
            "These are real comments from project maintainers / triagers on the",
            "upstream issue. They often name the exact file or class to change.",
            "Read them carefully BEFORE searching the codebase.",
            "",
            hints_text,
            "",
        ]
    )
lines.extend(["## Tests that must pass after your fix"])
if fail_to_pass:
    lines.extend(f"- {test_name}" for test_name in fail_to_pass)
else:
    lines.append("- (project test suite)")
lines.extend(
    [
        "",
        "Note: a FAIL_TO_PASS test that doesn't exist in the repo yet is normal — ",
        "the graders add it as part of the test patch. Do NOT spend time grepping ",
        "for the test definition; focus on the source fix.",
        "",
        "Locate the root cause in the source, apply a minimal fix, and ensure the behavior described above is satisfied.",
        "",
    ]
)
task_payload = "\n".join(lines).encode("utf-8")
if len(task_payload) > MAX_TASK_PROMPT_BYTES:
    raise SystemExit("task prompt exceeds the 4 MiB CLI input bound")
write_task(pathlib.Path(sys.argv[2]), task_payload)

print(iid)
PY
)"; then
        rm -f "$task_file"
        die "instance file failed bounded validation"
    fi

    [ -n "$image" ] || image="sweb.eval.${arch}.${iid}:latest"
    if ! image="$($py_bin - "$image" <<'PY'
import re
import sys
import unicodedata

value = sys.argv[1]
if (
    not value
    or len(value.encode("utf-8")) > 512
    or value.startswith("-")
    or "://" in value
    or any(character.isspace() for character in value)
    or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    )
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*", value) is None
):
    raise SystemExit("image must be a bounded Docker reference, not an option")
print(value)
PY
)"; then
        rm -f "$task_file"
        die "invalid Docker image reference"
    fi
    [ -n "$model_name" ] || model_name="opencollab-team"
    local session_containment=""
    if [ -z "$session_root" ]; then
        session_containment="$REPO_ROOT/.opencollab/swebench"
        session_root="$session_containment/$iid"
    fi
    if ! session_root="$(prepare_real_directory \
        "$py_bin" "$session_root" "$session_containment")"; then
        rm -f "$task_file"
        die "session root failed safe directory validation"
    fi
    local state_key state_base state_root
    state_key="$("$py_bin" - "$session_root" "$iid" <<'PY'
import hashlib
import sys

print(hashlib.sha256((sys.argv[1] + "\0" + sys.argv[2]).encode("utf-8")).hexdigest())
PY
)"
    state_base="$REPO_ROOT/.opencollab/harness_state/team_runs"
    if ! state_root="$(prepare_real_directory \
        "$py_bin" "$state_base/$state_key" "$state_base")"; then
        rm -f "$task_file"
        die "host-only harness state failed safe directory validation"
    fi
    local legacy_state=""
    for legacy_state in \
        "$session_root/team_container.lock" \
        "$session_root/team_container.owner" \
        "$session_root/pending_prediction.record.json" \
        "$session_root/pending_prediction.patch"; do
        if [ -e "$legacy_state" ] || [ -L "$legacy_state" ]; then
            rm -f "$task_file"
            die "legacy container-writable harness state requires manual recovery: $legacy_state"
        fi
    done
    rm -f "$session_root/events.jsonl" "$session_root/loop_monitor.json"
    rm -rf "$session_root/loop_monitor_artifacts"

    local name safe_iid suffix max_iid_chars owner_nonce iid_digest pid_iid
    safe_iid="${iid//[^A-Za-z0-9_.-]/_}"
    owner_nonce="$("$py_bin" -c 'import uuid; print(uuid.uuid4().hex)')"
    suffix="-${owner_nonce:0:12}"
    max_iid_chars=$((63 - 8 - ${#suffix}))
    [ "$max_iid_chars" -gt 0 ] || max_iid_chars=1
    name="oc-team-${safe_iid:0:$max_iid_chars}${suffix}"
    iid_digest="$("$py_bin" - "$iid" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:16])
PY
)"
    pid_iid="${safe_iid:0:80}-${iid_digest}-${owner_nonce:0:12}"

    local cid=""
    local host_uid host_gid
    host_uid="$(id -u)"
    host_gid="$(id -g)"
    local patch_file=""
    local pending_patch_file="$state_root/pending_prediction.patch"
    local pending_record_file="$state_root/pending_prediction.record.json"
    local patch_persisted=0
    local run_pidfile="/tmp/opencollab-team-${pid_iid}.pid"
    local run_cancelfile="${run_pidfile}.cancel"
    local process_guard="$REPO_ROOT/scripts/container_process_guard.sh"
    local owner_lock="$state_root/team_container.lock"
    local owner_marker="$state_root/team_container.owner"
    local lock_held=0
    local lock_guard_pid=""
    local lock_guard_status="$state_root/team_container.lock.status"
    local cleaned=0
    local cleaning=0
    local cleanup_signal=0
    local force_destroy=0
    local docker_control_timeout=5

    docker_bounded() {
        "$timeout_bin" --foreground --kill-after=2 "$docker_control_timeout" docker "$@"
    }

    release_owner_lock() {
        if [ "$lock_held" != "1" ]; then
            return 0
        fi
        if [ -n "$lock_guard_pid" ]; then
            kill "$lock_guard_pid" >/dev/null 2>&1 || true
            wait "$lock_guard_pid" 2>/dev/null || true
        fi
        rm -f "$lock_guard_status"
        lock_guard_pid=""
        lock_held=0
    }

    destroy_container() {
        local container_name="$1"
        local container_id="${2:-$container_name}"
        local expected_nonce="${3:?missing container owner nonce}"
        local attempt inspect_output inspect_rc owned_id
        for attempt in 1 2 3 4 5; do
            if inspect_output="$(docker_bounded inspect --type container \
                --format '{{.Id}}{{printf "\t"}}{{.Name}}{{printf "\t"}}{{index .Config.Labels "opencollab.harness.owner-token"}}' \
                "$container_name" 2>&1)"; then
                inspect_rc=0
            else
                inspect_rc=$?
            fi
            if [ "$inspect_rc" -eq 1 ] && { [[ "$inspect_output" == *"No such object"* ]] || [[ "$inspect_output" == *"No such container"* ]]; }; then
                return 0
            fi
            if [ "$inspect_rc" -ne 0 ]; then
                echo "error: container ownership inspect failed for $container_name (exit $inspect_rc)" >&2
                return 125
            fi
            if ! owned_id="$("$py_bin" - "$inspect_output" "$container_name" \
                "$container_id" "$expected_nonce" <<'PY'
import re
import sys

parts = sys.argv[1].strip().split("\t")
if len(parts) != 3:
    raise SystemExit("container ownership inspect output is malformed")
actual_id, actual_name, actual_nonce = parts
if re.fullmatch(r"[0-9a-fA-F]{64}", actual_id) is None:
    raise SystemExit("container ownership inspect returned an invalid id")
if actual_name != "/" + sys.argv[2] or actual_nonce != sys.argv[4]:
    raise SystemExit("container ownership label or name mismatch")
expected_id = sys.argv[3]
if re.fullmatch(r"[0-9a-fA-F]{64}", expected_id) and actual_id != expected_id:
    raise SystemExit("container ownership id mismatch")
print(actual_id)
PY
)"; then
                echo "error: refusing to remove unowned container $container_name" >&2
                return 125
            fi
            docker_bounded rm -f "$owned_id" >/dev/null 2>&1 || true
            sleep 0.1
        done
        if inspect_output="$(docker_bounded inspect --type container "$container_name" 2>&1)"; then
            inspect_rc=0
        else
            inspect_rc=$?
        fi
        if [ "$inspect_rc" -eq 1 ] && { [[ "$inspect_output" == *"No such object"* ]] || [[ "$inspect_output" == *"No such container"* ]]; }; then
            return 0
        fi
        if [ "$inspect_rc" -eq 0 ]; then
            echo "error: container cleanup left $container_name running" >&2
        else
            echo "error: container cleanup could not prove $container_name absent (inspect exit $inspect_rc)" >&2
        fi
        return 125
    }

    remove_current_owner_marker() {
        "$py_bin" - "$owner_marker" "$state_key" "$name" "$owner_nonce" \
            "${cid:-}" <<'PY'
import json
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
try:
    before = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
    raise SystemExit("owner marker is not a bounded regular file")
fd = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
try:
    opened = os.fstat(fd)
    payload = json.loads(os.read(fd, 4097).decode("utf-8"))
    current = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise SystemExit("owner marker changed while opening")
    if (
        payload.get("schema") == "opencollab.team-owner.v1"
        and payload.get("session_key") == sys.argv[2]
        and payload.get("container_name") == sys.argv[3]
        and payload.get("owner_nonce") == sys.argv[4]
        and (not sys.argv[5] or payload.get("container_id") == sys.argv[5])
    ):
        path.unlink()
finally:
    os.close(fd)
directory_fd = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
    }

    flush_pending_prediction() {
        "$py_bin" - "$pending_record_file" "$pending_patch_file" "$output" <<'PY'
import errno
import fcntl
import hashlib
import json
import math
import os
import pathlib
import stat
import sys
import time

MAX_PENDING_RECORD_BYTES = 64 * 1024 * 1024
MAX_PENDING_PATCH_BYTES = 9 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_JSONL_BYTES = 256 * 1024 * 1024
SAFE_FILE_OPEN_RETRIES = 8
REQUIRED_TRUE_INTEGRITY_FIELDS = (
    "submission_eligible",
    "execution_quiesced",
    "patch_extraction_succeeded",
    "injected_path_cleanup_proven",
    "harness_artifact_exclusion_proven",
    "checkpoint_restore_integrity_proven",
    "task_stage_integrity_proven",
)
REQUIRED_FALSE_INTEGRITY_FIELDS = ("test_patch_isolation_failed",)

try:
    LOCK_TIMEOUT_SECONDS = float(
        os.environ.get("OPENCOLLAB_HARNESS_LOCK_TIMEOUT_SECONDS", "10")
    )
except ValueError as exc:
    raise SystemExit("invalid harness lock timeout") from exc
if not math.isfinite(LOCK_TIMEOUT_SECONDS) or LOCK_TIMEOUT_SECONDS <= 0:
    raise SystemExit("invalid harness lock timeout")


def read_bounded_regular(path: pathlib.Path, limit: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise SystemExit(f"pending artifact is invalid or exceeds {limit} bytes: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise SystemExit(f"pending artifact changed while opening: {path}")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise SystemExit(f"pending artifact exceeds {limit} bytes: {path}")
    return payload


DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def open_secure_parent(path: pathlib.Path, *, create: bool) -> tuple[int, str]:
    absolute = pathlib.Path(os.path.abspath(path))
    if not absolute.name or absolute.name in {".", ".."}:
        raise SystemExit(f"prediction output path is invalid: {path}")
    parent_fd = os.open(absolute.anchor or os.sep, DIRECTORY_FLAGS)
    try:
        for component in absolute.parent.parts[1:]:
            if component in {"", ".", ".."}:
                raise SystemExit(f"prediction output parent is unsafe: {path}")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise SystemExit(f"prediction output parent is not a real directory: {path}")
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd, absolute.name
    except BaseException:
        os.close(parent_fd)
        raise


def stat_at(parent_fd: int, name: str):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def open_regular_output(parent_fd: int, name: str, path: pathlib.Path) -> int:
    flags = (
        os.O_RDWR
        | os.O_APPEND
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(SAFE_FILE_OPEN_RETRIES):
        before = stat_at(parent_fd, name)
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise SystemExit(f"prediction output must be a regular file: {path}")
        try:
            if before is None:
                fd = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=parent_fd,
                )
            else:
                fd = os.open(name, flags, dir_fd=parent_fd)
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = stat_at(parent_fd, name)
            if current is None:
                os.close(fd)
                continue
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
                or (
                    before is not None
                    and (before.st_dev, before.st_ino) != identity
                )
            ):
                os.close(fd)
                continue
            if before is None:
                os.fsync(parent_fd)
            return fd
        except FileNotFoundError:
            os.close(fd)
        except BaseException:
            os.close(fd)
            raise
    raise SystemExit(f"prediction output did not stabilize while opening: {path}")


def acquire_lock(fd: int, path: pathlib.Path) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(
                f"timed out acquiring prediction output lock after "
                f"{LOCK_TIMEOUT_SECONDS:g}s: {path}"
            )
        time.sleep(min(0.01, remaining))


def path_matches_fd(parent_fd: int, name: str, fd: int) -> bool:
    current = stat_at(parent_fd, name)
    if current is None:
        return False
    opened = os.fstat(fd)
    return (
        stat.S_ISREG(current.st_mode)
        and stat.S_ISREG(opened.st_mode)
        and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
    )


def parent_path_matches_fd(path: pathlib.Path, expected_fd: int) -> bool:
    try:
        current_fd, _name = open_secure_parent(path, create=False)
    except (OSError, SystemExit):
        return False
    try:
        current = os.fstat(current_fd)
        expected = os.fstat(expected_fd)
        return (
            stat.S_ISDIR(current.st_mode)
            and stat.S_ISDIR(expected.st_mode)
            and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
        )
    finally:
        os.close(current_fd)


def write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("zero-byte append")
        view = view[written:]


record_path = pathlib.Path(sys.argv[1])
patch_path = pathlib.Path(sys.argv[2])
envelope = json.loads(
    read_bounded_regular(record_path, MAX_PENDING_RECORD_BYTES).decode("utf-8")
)
if (
    not isinstance(envelope, dict)
    or envelope.get("schema") != "opencollab.pending-prediction.v1"
    or not isinstance(envelope.get("record"), dict)
):
    raise SystemExit("pending prediction envelope is malformed")
record = envelope["record"]
output_value = envelope.get("output_path")
if not isinstance(output_value, str) or not output_value:
    raise SystemExit("pending prediction has no output path")
expected_output = os.path.abspath(sys.argv[3])
if output_value != expected_output:
    raise SystemExit("pending prediction output path does not match this run")
output_path = pathlib.Path(expected_output)
patch = record.get("model_patch")
if not isinstance(patch, str):
    raise SystemExit("pending record has no patch text")
if patch_path.exists():
    patch_copy = read_bounded_regular(
        patch_path,
        MAX_PENDING_PATCH_BYTES,
    ).decode("utf-8", errors="strict")
    if patch_copy != patch:
        raise SystemExit("pending patch and record payload disagree")
digest = hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()
if record.get("patch_sha256") != digest or not record.get("record_id"):
    raise SystemExit("pending prediction identity is invalid")
metric = record.get("workflow_metric")
if (
    not isinstance(metric, dict)
    or metric.get("instance_id") != record.get("instance_id")
    or metric.get("record_id") != record.get("record_id")
    or metric.get("patch_sha256") != digest
):
    raise SystemExit("pending workflow metric identity is invalid")
status = metric.get("workflow_status")
returncode = metric.get("runner_returncode")
if isinstance(returncode, bool) or not isinstance(returncode, int):
    raise SystemExit("pending workflow metric has invalid runner return code")
valid_status = (
    (status == "done" and returncode == 0 and bool(patch))
    or (status == "done_with_timeout_patch" and returncode == 124 and bool(patch))
    or (status == "empty_patch" and not patch)
    or (status == "error" and returncode not in {0, 124})
)
if not valid_status:
    raise SystemExit("pending workflow metric status/return code mismatch")
for field in REQUIRED_TRUE_INTEGRITY_FIELDS:
    if metric.get(field) is not True:
        raise SystemExit(f"pending workflow metric lacks true integrity proof: {field}")
for field in REQUIRED_FALSE_INTEGRITY_FIELDS:
    if metric.get(field) is not False:
        raise SystemExit(f"pending workflow metric lacks false integrity proof: {field}")
if metric.get("worktree_integrity_proven") is not True:
    raise SystemExit("pending workflow metric lacks worktree integrity proof")
if metric.get("patch_produced") is not bool(patch.strip()):
    raise SystemExit("pending workflow metric patch_produced disagrees with patch")

payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
if len(payload) > MAX_OUTPUT_JSONL_BYTES:
    raise SystemExit("prediction output row exceeds byte limit")
output_parent_fd, output_name = open_secure_parent(output_path, create=True)
fd = open_regular_output(output_parent_fd, output_name, output_path)
locked = False
try:
    acquire_lock(fd, output_path)
    locked = True
    if os.fstat(fd).st_size > MAX_OUTPUT_JSONL_BYTES:
        raise SystemExit("prediction output exceeds byte limit")
    duplicate = False
    with os.fdopen(os.dup(fd), "rb", closefd=True) as handle:
        handle.seek(0)
        while True:
            line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_JSONL_LINE_BYTES:
                raise SystemExit("prediction output contains an oversized line")
            try:
                existing = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SystemExit(
                    "prediction output contains invalid UTF-8 JSONL"
                ) from exc
            if not isinstance(existing, dict):
                raise SystemExit("prediction output contains a non-object JSONL row")
            if existing.get("record_id") == record["record_id"]:
                if existing != record:
                    raise SystemExit("record_id collision in prediction output")
                duplicate = True
                break
    if not duplicate:
        size = os.fstat(fd).st_size
        needs_separator = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
        if size + int(needs_separator) + len(payload) > MAX_OUTPUT_JSONL_BYTES:
            raise SystemExit("prediction output exceeds byte limit")
        if needs_separator:
            write_all(fd, b"\n")
        write_all(fd, payload)
    os.fsync(fd)
    if not path_matches_fd(output_parent_fd, output_name, fd):
        raise SystemExit("prediction output changed while appending")
    os.fsync(output_parent_fd)
    if not parent_path_matches_fd(output_path, output_parent_fd):
        raise SystemExit("prediction output parent changed while appending")
finally:
    try:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        os.close(output_parent_fd)
patch_path.unlink(missing_ok=True)
pending_dir_fd = os.open(record_path.parent, os.O_RDONLY)
try:
    os.fsync(pending_dir_fd)
finally:
    os.close(pending_dir_fd)
record_path.unlink()
pending_dir_fd = os.open(record_path.parent, os.O_RDONLY)
try:
    os.fsync(pending_dir_fd)
finally:
    os.close(pending_dir_fd)
completed = (
    (status == "done" and returncode == 0 and not isinstance(returncode, bool))
    or (
        status == "done_with_timeout_patch"
        and returncode == 124
        and not isinstance(returncode, bool)
    )
)
print(0 if completed else 1)
PY
    }

    create_pending_prediction() {
        "$py_bin" - "$pending_record_file" "$pending_patch_file" "$patch_file" \
            "$output" "$iid" "$model_name" "$rc" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys
import uuid

MAX_PENDING_RECORD_BYTES = 64 * 1024 * 1024
MAX_PENDING_PATCH_BYTES = 9 * 1024 * 1024


def atomic_create(path: pathlib.Path, payload: bytes, limit: int) -> None:
    if len(payload) > limit:
        raise SystemExit(f"pending artifact exceeds {limit} bytes: {path}")
    if os.path.lexists(path):
        raise SystemExit(f"pending artifact already exists: {path}")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(path):
            raise SystemExit(f"pending artifact already exists: {path}")
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


record_path = pathlib.Path(sys.argv[1])
pending_patch_path = pathlib.Path(sys.argv[2])
source_patch_path = pathlib.Path(sys.argv[3])
source_info = source_patch_path.lstat()
if not stat.S_ISREG(source_info.st_mode) or source_info.st_size > MAX_PENDING_PATCH_BYTES:
    raise SystemExit("extracted patch exceeds pending patch bound")
source_fd = os.open(
    source_patch_path,
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
try:
    opened_source = os.fstat(source_fd)
    current_source = source_patch_path.lstat()
    if (
        not stat.S_ISREG(opened_source.st_mode)
        or not stat.S_ISREG(current_source.st_mode)
        or (opened_source.st_dev, opened_source.st_ino)
        != (source_info.st_dev, source_info.st_ino)
        or (current_source.st_dev, current_source.st_ino)
        != (source_info.st_dev, source_info.st_ino)
    ):
        raise SystemExit("extracted patch changed while opening")
    chunks = []
    remaining = MAX_PENDING_PATCH_BYTES + 1
    while remaining > 0:
        chunk = os.read(source_fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
finally:
    os.close(source_fd)
patch_bytes = b"".join(chunks)
if len(patch_bytes) > MAX_PENDING_PATCH_BYTES:
    raise SystemExit("extracted patch exceeds pending patch bound")
patch = patch_bytes.decode("utf-8", errors="strict")
patch_sha = hashlib.sha256(patch.encode("utf-8", errors="surrogatepass")).hexdigest()
record_id = uuid.uuid4().hex
returncode = int(sys.argv[7])
workflow_status = (
    "empty_patch" if not patch
    else "done" if returncode == 0
    else "done_with_timeout_patch" if returncode == 124
    else "error"
)
metric = {
    "instance_id": sys.argv[5],
    "record_id": record_id,
    "patch_sha256": patch_sha,
    "workflow_status": workflow_status,
    "runner_returncode": returncode,
    "submission_eligible": True,
    "execution_quiesced": True,
    "patch_extraction_succeeded": True,
    "injected_path_cleanup_proven": True,
    "harness_artifact_exclusion_proven": True,
    "checkpoint_restore_integrity_proven": True,
    "task_stage_integrity_proven": True,
    "test_patch_isolation_failed": False,
    "worktree_integrity_proven": True,
    "patch_produced": bool(patch.strip()),
}
record = {
    "instance_id": sys.argv[5],
    "record_id": record_id,
    "patch_sha256": patch_sha,
    "model_name_or_path": sys.argv[6],
    "model_patch": patch,
    "workflow_metric": metric,
}
envelope = {
    "schema": "opencollab.pending-prediction.v1",
    "output_path": os.path.abspath(sys.argv[4]),
    "record": record,
}
record_payload = (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")

# The record is authoritative and is written first. A crash before the optional
# plain patch copy can still replay the exact embedded model_patch idempotently.
atomic_create(record_path, record_payload, MAX_PENDING_RECORD_BYTES)
atomic_create(pending_patch_path, patch_bytes, MAX_PENDING_PATCH_BYTES)
PY
    }

    cleanup() {
        if [ "${cleaned:-0}" = "1" ]; then
            [ -z "${task_file:-}" ] || rm -f "$task_file"
            if [ "${patch_persisted:-0}" = "0" ]; then
                [ -z "${patch_file:-}" ] || rm -f "$patch_file"
            fi
            return 0
        fi
        if [ "${cleaning:-0}" = "1" ]; then
            echo "error: recursive container cleanup for ${name:-unknown}" >&2
            return 125
        fi
        cleaning=1
        cleanup_signal=0
        trap 'cleanup_signal=130; force_destroy=1; keep=0' INT
        trap 'cleanup_signal=143; force_destroy=1; keep=0' TERM
        local cleanup_rc=0
        local kept_container=0
        if [ -n "${cid:-}" ]; then
            docker_bounded exec "$cid" bash -lc \
                "chown -R '$host_uid:$host_gid' /testbed/.opencollab" >/dev/null 2>&1 || true
        fi
        if [ "${keep:-0}" = "1" ] && [ "${force_destroy:-0}" = "0" ] && [ -n "${cid:-}" ]; then
            echo "(container kept running: ${name:-unknown} / ${cid:-unknown})"
            kept_container=1
            cleaned=1
        else
            if destroy_container "$name" "${cid:-$name}" "$owner_nonce"; then
                cleaned=1
            else
                cleanup_rc=125
            fi
        fi
        if [ "$cleanup_signal" -ne 0 ] && [ "$kept_container" = "1" ]; then
            cleaned=0
            if destroy_container "$name" "${cid:-$name}" "$owner_nonce"; then
                cleaned=1
            else
                cleanup_rc=125
            fi
        fi
        if [ "$cleaned" = "1" ] && [ "$kept_container" = "0" ]; then
            remove_current_owner_marker
        fi
        [ -z "${task_file:-}" ] || rm -f "$task_file"
        if [ "${patch_persisted:-0}" = "0" ]; then
            [ -z "${patch_file:-}" ] || rm -f "$patch_file"
        fi
        if [ "$cleaned" = "1" ] && [ "$lock_held" = "1" ]; then
            release_owner_lock
        fi
        cleaning=0
        trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 130; else trap - EXIT INT TERM ERR; exit 125; fi' INT
        trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 143; else trap - EXIT INT TERM ERR; exit 125; fi' TERM
        if [ "$cleanup_signal" -ne 0 ]; then
            echo "error: container cleanup was interrupted by signal $cleanup_signal" >&2
            return 125
        fi
        return "$cleanup_rc"
    }
    trap cleanup EXIT
    trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 130; else trap - EXIT INT TERM ERR; exit 125; fi' INT
    trap 'force_destroy=1; keep=0; if cleanup; then trap - EXIT INT TERM ERR; exit 143; else trap - EXIT INT TERM ERR; exit 125; fi' TERM
    trap 'force_destroy=1; keep=0' ERR

    rm -f "$lock_guard_status"
    "$py_bin" - "$owner_lock" "$lock_guard_status" "$$" <<'PY' &
import fcntl
import json
import os
import pathlib
import stat
import sys
import time
import uuid


def write_status(path, payload):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def open_regular_lock(path):
    flags = (
        os.O_RDWR
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(8):
        try:
            before = path.lstat()
        except FileNotFoundError:
            before = None
        if before is not None and not stat.S_ISREG(before.st_mode):
            raise OSError("owner lock must be a regular file")
        try:
            if before is None:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            else:
                fd = os.open(path, flags)
        except (FileExistsError, FileNotFoundError):
            continue
        try:
            opened = os.fstat(fd)
            current = path.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
                or (
                    before is not None
                    and (before.st_dev, before.st_ino) != identity
                )
            ):
                os.close(fd)
                continue
            return fd
        except BaseException:
            os.close(fd)
            raise
    raise OSError("owner lock did not stabilize while opening")


lock_path = pathlib.Path(sys.argv[1])
status_path = pathlib.Path(sys.argv[2])
parent_pid = int(sys.argv[3])
fd = -1
try:
    fd = open_regular_lock(lock_path)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BaseException as exc:
    write_status(status_path, {"status": "error", "error": str(exc)})
    raise SystemExit(1)

write_status(status_path, {"status": "locked"})
try:
    while os.getppid() == parent_pid:
        try:
            os.kill(parent_pid, 0)
        except OSError:
            break
        time.sleep(0.05)
finally:
    if fd >= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
PY
    lock_guard_pid=$!
    local lock_status=""
    local lock_wait_attempt
    for lock_wait_attempt in $(seq 1 500); do
        if [ -f "$lock_guard_status" ]; then
            lock_status="$("$py_bin" - "$lock_guard_status" <<'PY'
import json
import pathlib
import sys

print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["status"])
PY
            )"
            break
        fi
        if ! kill -0 "$lock_guard_pid" 2>/dev/null; then
            break
        fi
        sleep 0.01
    done
    if [ "$lock_status" != "locked" ]; then
        kill "$lock_guard_pid" >/dev/null 2>&1 || true
        wait "$lock_guard_pid" 2>/dev/null || true
        rm -f "$lock_guard_status"
        die "another team run already owns $session_root"
    fi
    lock_held=1

    if [ -e "$owner_marker" ] || [ -L "$owner_marker" ]; then
        local previous_identity="" previous_name="" previous_id="" previous_nonce=""
        if ! previous_identity="$("$py_bin" - "$owner_marker" "$state_key" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
before = path.lstat()
if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
    raise SystemExit("owner marker is not a bounded regular file")
fd = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
try:
    opened = os.fstat(fd)
    payload = json.loads(os.read(fd, 4097).decode("utf-8"))
    current = path.lstat()
finally:
    os.close(fd)
if (
    not stat.S_ISREG(opened.st_mode)
    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    or payload.get("schema") != "opencollab.team-owner.v1"
    or payload.get("session_key") != sys.argv[2]
    or re.fullmatch(r"oc-team-[A-Za-z0-9_.-]+", str(payload.get("container_name") or "")) is None
    or re.fullmatch(r"[0-9a-f]{32}", str(payload.get("owner_nonce") or "")) is None
    or (
        payload.get("container_id")
        and re.fullmatch(r"[0-9a-fA-F]{64}", str(payload.get("container_id"))) is None
    )
):
    raise SystemExit("owner marker identity is invalid")
print(
    f"{payload['container_name']}|{payload.get('container_id') or ''}|"
    f"{payload['owner_nonce']}"
)
PY
)"; then
            die "invalid stale container owner marker: $owner_marker"
        fi
        IFS='|' read -r previous_name previous_id previous_nonce <<< "$previous_identity"
        if [ -z "$previous_name" ] || [ -z "$previous_nonce" ]; then
            die "invalid stale container owner identity: $owner_marker"
        fi
        if ! destroy_container "$previous_name" "${previous_id:-$previous_name}" "$previous_nonce"; then
            die "could not recover stale container $previous_name"
        fi
        if ! "$py_bin" - "$owner_marker" "$state_key" "$previous_name" \
            "$previous_id" "$previous_nonce" <<'PY'
import json
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
before = path.lstat()
fd = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0),
)
try:
    payload = json.loads(os.read(fd, 4097).decode("utf-8"))
    opened = os.fstat(fd)
    current = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        or payload.get("session_key") != sys.argv[2]
        or payload.get("container_name") != sys.argv[3]
        or (payload.get("container_id") or "") != sys.argv[4]
        or payload.get("owner_nonce") != sys.argv[5]
    ):
        raise SystemExit("owner marker changed during recovery")
    path.unlink()
finally:
    os.close(fd)
PY
        then
            die "could not remove recovered owner marker $owner_marker"
        fi
    fi

    if [ -e "$pending_record_file" ]; then
        local recovered_rc=""
        if ! recovered_rc="$(flush_pending_prediction)"; then
            die "could not replay pending prediction $pending_record_file"
        fi
        echo "Recovered pending prediction from $session_root"
        cleaned=1
        [ -z "${task_file:-}" ] || rm -f "$task_file"
        if [ "$lock_held" = "1" ]; then
            release_owner_lock
        fi
        trap - EXIT INT TERM ERR
        if [ "$recovered_rc" = "0" ]; then
            return 0
        fi
        return 1
    elif [ -e "$pending_patch_file" ]; then
        die "unrecorded pending patch requires recovery: $pending_patch_file"
    fi

    "$py_bin" - "$owner_marker" "$name" "$state_key" "$owner_nonce" <<'PY'
import json
import os
import pathlib
import sys
import uuid

path = pathlib.Path(sys.argv[1])
tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": "opencollab.team-owner.v1",
                "session_key": sys.argv[3],
                "container_name": sys.argv[2],
                "container_id": "",
                "owner_nonce": sys.argv[4],
            },
            handle,
            sort_keys=True,
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.link(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PY

    echo "Instance:  $iid"
    echo "Image:     $image"
    echo "Team file: $team_file"
    echo "Timeout:   ${timeout}s"
    echo "Network:   $network"
    echo "Hints:     $([ "$include_hints" = "1" ] && echo on || echo off)"
    echo "Sessions:  $session_root"

    local -a docker_args=(
        run -d
        --name "$name"
        --label "opencollab.harness.owner-token=$owner_nonce"
        --network "$network"
        --entrypoint ""
        -v "$session_root:/testbed/.opencollab:rw"
    )
    if [ "$mount_home_ro" = "1" ]; then
        [ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "cannot --mount-home-ro: HOME is not a directory"
        docker_args+=(-v "$HOME:$HOME:ro" -e HOME="$HOME")
    fi
    docker_args+=(
        -v "$REPO_ROOT:$REPO_ROOT:ro"
        -v "$py_root:$py_root:ro"
        -e PYTHONDONTWRITEBYTECODE=1
        -e OPENCOLLAB_TEAM_FILE="$team_file"
        -e OPENCOLLAB_CONFIG_FILE="$DEFAULT_ENV_FILE"
        -e OPENCOLLAB_EVENTS_FILE="/testbed/.opencollab/events.jsonl"
        -e TERM="${TERM:-xterm-256color}"
    )
    # Allow secret-bearing runtime config to be supplied by the caller's
    # environment instead of writing configs/.env into the repository.
    for _ocv in \
        OPENCOLLAB_PROVIDER OPENCOLLAB_BASE_URL OPENCOLLAB_MODEL \
        OPENCOLLAB_API_KEY OPENCOLLAB_BUDGET OPENCOLLAB_TEMPERATURE \
        OPENCOLLAB_TOP_P OPENCOLLAB_THINKING OPENCOLLAB_THINKING_PARAMS \
        OPENCOLLAB_LLM_TIMEOUT OPENAI_API_KEY OPENAI_BASE_URL \
        ANTHROPIC_API_KEY ANTHROPIC_BASE_URL DASHSCOPE_API_KEY; do
        if [ -n "${!_ocv:-}" ]; then
            docker_args+=(-e "${_ocv}")
        fi
    done
    # Forward proxy settings so openai/anthropic SDKs can reach the API
    # even when the host's direct route is down.  With --network host the
    # container shares the host network namespace, so 127.0.0.1 proxies work.
    for _pv in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY; do
        if [ -n "${!_pv:-}" ]; then
            docker_args+=(-e "${_pv}")
        fi
    done
    local mount_spec
    if [ "${#extra_mounts[@]}" -gt 0 ]; then
        for mount_spec in "${extra_mounts[@]}"; do
            docker_args+=(-v "$(normalize_mount "$mount_spec")")
        done
    fi
    docker_args+=(-- "$image" tail -f /dev/null)

    local raw_cid
    local docker_run_rc
    set +e
    raw_cid="$("$timeout_bin" --foreground --kill-after=5 120 docker "${docker_args[@]}")"
    docker_run_rc=$?
    set -e
    if [ "$docker_run_rc" -ne 0 ] || [ -z "${raw_cid//[[:space:]]/}" ]; then
        force_destroy=1
        keep=0
        die "docker run failed or exceeded its 120s setup bound (exit $docker_run_rc)"
    fi
    cid="$("$py_bin" - "$raw_cid" <<'PY'
import re
import sys

value = sys.argv[1].strip()
if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
    raise SystemExit("docker run returned an invalid container id")
print(value)
PY
)"
    "$py_bin" - "$owner_marker" "$state_key" "$name" "$owner_nonce" "$cid" <<'PY'
import json
import os
import pathlib
import stat
import sys
import uuid

path = pathlib.Path(sys.argv[1])
before = path.lstat()
if not stat.S_ISREG(before.st_mode) or before.st_size > 4096:
    raise SystemExit("owner marker is not a bounded regular file")
fd = os.open(
    path,
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
try:
    opened = os.fstat(fd)
    payload = json.loads(os.read(fd, 4097).decode("utf-8"))
    current = path.lstat()
finally:
    os.close(fd)
if (
    not stat.S_ISREG(opened.st_mode)
    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    or payload.get("schema") != "opencollab.team-owner.v1"
    or payload.get("session_key") != sys.argv[2]
    or payload.get("container_name") != sys.argv[3]
    or payload.get("owner_nonce") != sys.argv[4]
    or payload.get("container_id") not in {"", sys.argv[5]}
):
    raise SystemExit("owner marker changed before container id binding")
payload["container_id"] = sys.argv[5]
temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
try:
    temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        view = memoryview(raw)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError("short owner marker write")
            view = view[written:]
        os.fsync(temp_fd)
    finally:
        os.close(temp_fd)
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise SystemExit("owner marker changed during container id binding")
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
PY
    echo "Container: ${cid:0:12} ($name)"

    # /testbed is owned by root in the image — let git operate on it.
    docker_bounded exec "$cid" bash -lc \
        "git config --global --add safe.directory /testbed" >/dev/null
    # Keep opencollab's autosave folder out of the prediction patch.
    docker_bounded exec "$cid" bash -lc \
        "printf '/.opencollab/\n' >> /testbed/.git/info/exclude" >/dev/null

    docker_bounded cp "$task_file" "$cid:/tmp/oc_task.txt"
    docker_bounded exec "$cid" bash "$process_guard" prepare \
        "$run_pidfile" "$run_cancelfile"

    # Activate the testbed conda env so the agent's python/pytest resolve to the
    # repo-specific interpreter. Then exec opencollab with --prompt-file (one-shot)
    # and --yolo (no permission prompts in non-interactive mode).
    local inner oc_bin_q model_q
    printf -v oc_bin_q '%q' "$oc_bin"
    inner="source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "
    inner+="exec $oc_bin_q --workspace /testbed --no-worktrees --yolo"
    if [ -n "$model" ]; then
        printf -v model_q '%q' "$model"
        inner+=" --model $model_q"
    fi
    inner+=" --prompt-file /tmp/oc_task.txt"

    # Use -t (allocate TTY) only when our own stdout is a TTY — otherwise
    # `docker exec -it` aborts with "the input device is not a TTY", which
    # makes the team run uninvokable from background/CI contexts.
    local -a docker_exec_flags=(-i)
    if [ -t 0 ] && [ -t 1 ]; then
        docker_exec_flags=(-it)
    fi

    set +e
    "$timeout_bin" --foreground --kill-after=5 "$timeout" \
        docker exec "${docker_exec_flags[@]}" -w /testbed "$cid" \
        bash "$process_guard" run "$run_pidfile" "$run_cancelfile" \
        bash -lc "$inner"
    local rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        local stop_ok=1
        if ! docker_bounded exec "$cid" bash "$process_guard" stop "$run_pidfile" "$run_cancelfile"; then
            stop_ok=0
        fi
        if [ "$rc" -eq 125 ] || [ "$stop_ok" = "0" ]; then
            force_destroy=1
            keep=0
            die "container process group did not quiesce; refusing to extract a racing patch"
        fi
    fi
    if [ "$rc" -eq 124 ]; then
        echo "warn: opencollab hit the ${timeout}s wall-clock timeout — capturing partial diff"
    elif [ "$rc" -ne 0 ]; then
        echo "warn: opencollab exited with code $rc — capturing diff anyway"
    fi

    local bounded_diff
    bounded_diff="$("$py_bin" - "$REPO_ROOT/swebench" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import gen_prediction as gp

print(gp.bounded_container_output_command(
    "git diff --cached --binary",
    max_bytes=gp.MAX_EXTRACTED_PATCH_BYTES,
    label="team staged patch",
    helper_path="/tmp/opencollab_gen_prediction_bounded_capture.py",
))
PY
)"
    docker_bounded cp \
        "$REPO_ROOT/swebench/gen_prediction_bounded_capture.py" \
        "$cid:/tmp/opencollab_gen_prediction_bounded_capture.py"
    patch_file="$(mktemp -t oc_patch.XXXXXX)"
    local diff_pidfile="${run_pidfile}.diff"
    local diff_cancelfile="${diff_pidfile}.cancel"
    docker_bounded exec "$cid" bash "$process_guard" prepare \
        "$diff_pidfile" "$diff_cancelfile"
    set +e
    "$timeout_bin" --foreground --kill-after=5 120 docker exec -w /testbed "$cid" \
        bash "$process_guard" run "$diff_pidfile" "$diff_cancelfile" \
        bash -lc "git add -A && $bounded_diff" > "$patch_file"
    local diff_rc=$?
    set -e
    if [ "$diff_rc" -ne 0 ]; then
        docker_bounded exec "$cid" bash "$process_guard" stop \
            "$diff_pidfile" "$diff_cancelfile" >/dev/null 2>&1 || true
        force_destroy=1
        keep=0
        die "bounded patch extraction failed (exit $diff_rc); prediction was not appended"
    fi

    local extracted_patch_file="$patch_file"
    if ! create_pending_prediction; then
        die "could not persist pending prediction in $session_root"
    fi
    rm -f "$extracted_patch_file"
    patch_file="$pending_patch_file"
    patch_persisted=1

    local patch_size
    patch_size="$(wc -c < "$patch_file" | tr -d '[:space:]')"
    if [ "$patch_size" -gt 0 ]; then
        echo ""
        echo "Patch prepared (${patch_size} bytes)"
    else
        echo ""
        echo "WARNING: empty patch (team made no tracked changes)"
    fi

    local monitor_file="$session_root/loop_monitor.json"
    if "$py_bin" "$REPO_ROOT/scripts/swebench_loop_monitor.py" \
        --instance-id "$iid" \
        --session-root "$session_root" \
        --events-file "$session_root/events.jsonl" \
        --diff-file "$patch_file" \
        --output "$monitor_file"; then
        echo "Loop monitor: $monitor_file"
    else
        echo "warn: loop monitor failed for $iid" >&2
    fi

    local final_rc=0
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ]; then
        final_rc="$rc"
    fi
    if [ "$patch_size" -eq 0 ] && [ "$final_rc" -eq 0 ]; then
        final_rc=1
    fi
    if ! cleanup; then
        trap - EXIT INT TERM ERR
        return 125
    fi

    local flushed_rc=""
    if ! flushed_rc="$(flush_pending_prediction)"; then
        die "prediction append failed; patch preserved at $pending_patch_file"
    fi
    patch_persisted=0
    patch_file=""
    echo "Prediction appended to $output"
    trap - EXIT INT TERM ERR
    return "$final_rc"
}

main "$@"
