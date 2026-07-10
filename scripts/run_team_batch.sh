#!/usr/bin/env bash
# Run the OpenCollab SWE-bench team on a list of instances, one at a time.
#
# Loads the dataset via the swebench Python package (uses HF cache), writes
# each instance to a temp JSON, then invokes scripts/start_team_run.sh.
# Skips instances only when the latest row has a non-empty patch and a matching
# completed workflow identity/status record.
#
# Usage:
#   scripts/run_team_batch.sh \
#       --dataset SWE-bench/SWE-bench_Lite \
#       --split test \
#       --output /path/to/swebench-eval/predictions-team.jsonl \
#       [--timeout 1500] \
#       --namespace <registry-namespace> \
#       [--instance-ids id1,id2,...] \
#       [--limit N] \
#       [--start-from <instance_id>] \
#       [--logs-dir /tmp/oc-batch] \
#       [--retry-empty]   # also redo instances whose last entry has an empty patch
#
# Defaults match scripts/start_team_run.sh. Per-instance stdout/stderr goes to
# <logs-dir>/<instance_id>.log; a one-line summary per instance is appended to
# <logs-dir>/batch.tsv. Re-running with the same --output is safe: completed
# instances are skipped.
#
# The dataset entry is also resolved via swebench.make_test_spec so that the
# correct local Docker image (with __ → _1776_) is passed to
# start_team_run.sh via --image. Set OPENCOLLAB_EVAL_PYTHON to the Python
# executable that has the swebench package installed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ONE_SHOT="$SCRIPT_DIR/start_team_run.sh"
EVAL_VENV_PY="${OPENCOLLAB_EVAL_PYTHON:-}"
BATCH_IO="$SCRIPT_DIR/swe_team_batch_io.py"

die() { echo "error: $*" >&2; exit 1; }

usage() {
    sed -n '2,21p' "$0"
}

dataset="SWE-bench/SWE-bench_Lite"
split="test"
output=""
timeout_secs=1500
namespace="${OPENCOLLAB_SWEBENCH_NAMESPACE:-}"
instance_ids=""
limit=""
start_from=""
logs_dir=""
retry_empty=0
extra=()

while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --dataset) dataset="${2:?}"; shift 2 ;;
        --split) split="${2:?}"; shift 2 ;;
        --output) output="${2:?}"; shift 2 ;;
        --timeout) timeout_secs="${2:?}"; shift 2 ;;
        --namespace) namespace="${2:?}"; shift 2 ;;
        --instance-ids) instance_ids="${2:?}"; shift 2 ;;
        --limit) limit="${2:?}"; shift 2 ;;
        --start-from) start_from="${2:?}"; shift 2 ;;
        --logs-dir) logs_dir="${2:?}"; shift 2 ;;
        --retry-empty) retry_empty=1; shift ;;
        --) shift; extra+=("$@"); break ;;
        *) die "unknown arg: $1 (use --help)" ;;
    esac
done

[ -n "$output" ] || die "--output is required"
[ -n "$namespace" ] || die "--namespace or OPENCOLLAB_SWEBENCH_NAMESPACE is required"
[ -x "$ONE_SHOT" ] || die "missing $ONE_SHOT"
[ -x "$EVAL_VENV_PY" ] || die "set OPENCOLLAB_EVAL_PYTHON to an executable with swebench installed"
[ -f "$BATCH_IO" ] || die "missing $BATCH_IO"

[ -n "$logs_dir" ] || logs_dir="$REPO_ROOT/.opencollab/swebench/_batch_logs/$(date +%Y-%m-%dT%H-%M-%S)"
prepared_paths="$("$EVAL_VENV_PY" "$BATCH_IO" prepare --output "$output" --logs-dir "$logs_dir")" \
    || die "batch output/log path preparation failed"
IFS=$'\t' read -r output logs_dir summary <<< "$prepared_paths"
[ -n "$output" ] && [ -n "$logs_dir" ] && [ -n "$summary" ] \
    || die "batch path preparation returned an invalid result"

# Decide which instances to run. Instance JSON and the TSV manifest are written
# through a held, non-symlink directory descriptor under logs_dir.
list_file="$logs_dir/_to_run.tsv"
total="$(HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 "$EVAL_VENV_PY" - "$dataset" "$split" \
    "${instance_ids:-}" "${limit:-}" "${start_from:-}" "$output" "$retry_empty" \
    "$logs_dir" "$namespace" "$REPO_ROOT" "$SCRIPT_DIR" "$list_file" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

(
    dataset_name,
    split,
    ids_csv,
    limit_s,
    start_from,
    output_path,
    retry_empty_s,
    logs_dir,
    namespace,
    repo_root,
    script_dir,
    list_file,
) = sys.argv[1:13]
limit = int(limit_s) if limit_s else None
if limit is not None and limit <= 0:
    raise SystemExit("--limit must be a positive integer")

sys.path.insert(0, str(pathlib.Path(repo_root) / "opencollab"))
sys.path.insert(0, script_dir)
from opencollab.harness.swe_eval_records import is_completed_prediction, read_jsonl, row_task_id
from swe_team_batch_io import (
    atomic_write_at,
    open_directory,
    validate_instance_id,
    validate_tsv_field,
)

ids_filter = (
    {validate_instance_id(item.strip()) for item in ids_csv.split(",") if item.strip()}
    if ids_csv
    else None
)
if start_from:
    start_from = validate_instance_id(start_from)

from swebench.harness.utils import load_swebench_dataset
from swebench.harness.test_spec.test_spec import make_test_spec
ds = load_swebench_dataset(dataset_name, split)

# Build map of last seen prediction per instance_id (last line wins).
seen = {}
out_path = pathlib.Path(output_path)
for rec in read_jsonl(out_path):
    iid = row_task_id(rec)
    if iid:
        seen[iid] = rec

def should_skip(iid):
    return is_completed_prediction(seen.get(iid))

started = not bool(start_from)
start_found = started
count = 0
manifest_rows = []
logs_path, logs_fd = open_directory(logs_dir, create=False)
try:
    for inst in ds:
        if not isinstance(inst, dict):
            raise SystemExit("dataset row must be an object")
        iid = validate_instance_id(inst.get("instance_id"))
        if not started:
            if iid == start_from:
                started = True
                start_found = True
            else:
                continue
        if ids_filter is not None and iid not in ids_filter:
            continue
        if should_skip(iid):
            continue
        digest = hashlib.sha256(iid.encode("utf-8")).hexdigest()
        json_name = f"{digest}.instance.json"
        json_path = logs_path / json_name
        atomic_write_at(
            logs_fd,
            json_name,
            json.dumps(inst, ensure_ascii=False).encode("utf-8"),
            label=json_path,
        )
        spec = make_test_spec(inst, namespace=namespace)
        image_key = validate_tsv_field(spec.instance_image_key, label="image key")
        manifest_rows.append(
            "\t".join(
                (
                    validate_tsv_field(iid, label="instance_id"),
                    validate_tsv_field(json_path, label="instance JSON path"),
                    image_key,
                    digest,
                )
            )
        )
        count += 1
        if limit is not None and count >= limit:
            break
    if start_from and not start_found:
        raise SystemExit(f"--start-from instance was not found: {start_from}")
    list_path = pathlib.Path(os.path.abspath(list_file))
    if list_path.parent != logs_path:
        raise SystemExit("batch manifest escaped logs_dir")
    payload = (("\n".join(manifest_rows) + "\n") if manifest_rows else "").encode("utf-8")
    atomic_write_at(logs_fd, list_path.name, payload, label=list_path)
finally:
    os.close(logs_fd)
print(count)
PY
)" || die "batch manifest generation failed"
[[ "$total" =~ ^[0-9]+$ ]] || die "batch manifest returned an invalid count"
if [ "$total" = "0" ]; then
    echo "Nothing to run — every requested instance already has a (non-empty) prediction."
    exit 0
fi
manifest_payload="$("$EVAL_VENV_PY" "$BATCH_IO" display-summary --summary "$list_file")" \
    || die "batch manifest could not be read safely"

echo "Batch plan: $total instances → $output"
echo "Per-instance logs: $logs_dir/<instance_id_sha256>.log"
echo "Summary: $summary"
echo

idx=0
while IFS=$'\t' read -r iid json_path image_key log_stem <&3; do
    idx=$((idx+1))
    log="$logs_dir/${log_stem}.log"
    printf '[%d/%d] %s  (image=%s)\n' "$idx" "$total" "$iid" "$image_key"
    started_ts=$(date +%s)
    started_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    command=(
        bash "$ONE_SHOT"
        --instance-file "$json_path"
        --output "$output"
        --image "$image_key"
        --timeout "$timeout_secs"
        "${extra[@]}"
    )
    set +e
    "$EVAL_VENV_PY" "$BATCH_IO" run-log --log "$log" -- "${command[@]}"
    rc=$?
    set -e
    ended_ts=$(date +%s)
    wall=$((ended_ts - started_ts))

    prediction_info="$("$EVAL_VENV_PY" - "$iid" "$output" "$REPO_ROOT" <<'PY'
import pathlib
import sys

iid, output, repo_root = sys.argv[1:4]
sys.path.insert(0, str(pathlib.Path(repo_root) / "opencollab"))
from opencollab.harness.swe_eval_records import (
    is_completed_prediction,
    prediction_patch,
    read_jsonl,
    row_task_id,
)

latest = None
for row in read_jsonl(pathlib.Path(output)):
    if row_task_id(row) == iid:
        latest = row
patch = prediction_patch(latest)
print(f"{len(patch.encode('utf-8', errors='surrogatepass'))}\t{int(is_completed_prediction(latest))}")
PY
    )"
    IFS=$'\t' read -r patch_bytes prediction_valid <<< "$prediction_info"
    if [ "$rc" -eq 0 ] && [ "${prediction_valid:-0}" = "1" ]; then
        status="ok"
    elif [ "$rc" -eq 0 ]; then
        status="invalid_prediction"
    else
        status="error_rc${rc}"
    fi
    loop_alert="$("$EVAL_VENV_PY" - "$REPO_ROOT" "$iid" "$log" <<'PY'
import pathlib
import re
import sys

repo, iid, log_path = sys.argv[1:4]
sys.path.insert(0, str(pathlib.Path(repo) / "scripts"))
from swebench_loop_monitor import _load_json, _read_tail_text

monitor = pathlib.Path(repo) / ".opencollab" / "swebench" / iid / "loop_monitor.json"
if monitor.exists():
    try:
        value = _load_json(monitor)
        if isinstance(value, dict):
            print(value.get("level") or "unknown")
            raise SystemExit
    except Exception:
        pass
try:
    text = _read_tail_text(pathlib.Path(log_path), 1024 * 1024)
except Exception:
    print("unknown")
    raise SystemExit
matches = re.findall(r"level=(ok|warn|critical)", text)
print(matches[-1] if matches else "unknown")
PY
    )"
    "$EVAL_VENV_PY" "$BATCH_IO" append-summary --summary "$summary" \
        "$started_iso" "$iid" "$status" "${patch_bytes:-0}" "$wall" "$loop_alert"
    printf '       → %s (%s bytes, %ss, loop=%s)\n' "$status" "${patch_bytes:-0}" "$wall" "$loop_alert"
done 3<<< "$manifest_payload"

echo
echo "Done. Summary:"
if command -v column >/dev/null 2>&1; then
    "$EVAL_VENV_PY" "$BATCH_IO" display-summary --summary "$summary" \
        | column -t -s $'\t'
else
    "$EVAL_VENV_PY" "$BATCH_IO" display-summary --summary "$summary"
fi
