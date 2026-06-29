#!/usr/bin/env bash
# Run the OpenCollab SWE-bench team on a list of instances, one at a time.
#
# Loads the dataset via the swebench Python package (uses HF cache), writes
# each instance to a temp JSON, then invokes scripts/start_team_run.sh.
# Skips instances already present in --output with a non-empty patch.
#
# Usage:
#   scripts/run_team_batch.sh \
#       --dataset SWE-bench/SWE-bench_Lite \
#       --split test \
#       --output /home/xuzhenhua/swebench-eval/predictions-team.jsonl \
#       [--timeout 1500] \
#       [--namespace docker.1panel.live/swebench] \
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
# correct local docker image (mirror-namespaced, with __ → _1776_) is passed
# to start_team_run.sh via --image. This means images pulled by
# /home/xuzhenhua/swebench-eval/pull_all_images.py work out of the box; no
# retagging needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ONE_SHOT="$SCRIPT_DIR/start_team_run.sh"
EVAL_VENV_PY="/home/xuzhenhua/swebench-eval/.venv/bin/python"

die() { echo "error: $*" >&2; exit 1; }

usage() {
    sed -n '2,21p' "$0"
}

dataset="SWE-bench/SWE-bench_Lite"
split="test"
output=""
timeout_secs=1500
namespace="docker.1panel.live/swebench"
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
[ -x "$ONE_SHOT" ] || die "missing $ONE_SHOT"
[ -x "$EVAL_VENV_PY" ] || die "missing $EVAL_VENV_PY (need swebench package installed)"

mkdir -p "$(dirname "$output")"
[ -n "$logs_dir" ] || logs_dir="$REPO_ROOT/.opencollab/swebench/_batch_logs/$(date +%Y-%m-%dT%H-%M-%S)"
mkdir -p "$logs_dir"
summary="$logs_dir/batch.tsv"
[ -f "$summary" ] || printf 'timestamp\tinstance_id\tstatus\tpatch_bytes\twall_seconds\tloop_alert\n' > "$summary"
if ! head -n 1 "$summary" | grep -q $'\tloop_alert$'; then
    "$EVAL_VENV_PY" - "$summary" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
if not lines:
    path.write_text("timestamp\tinstance_id\tstatus\tpatch_bytes\twall_seconds\tloop_alert\n", encoding="utf-8")
    raise SystemExit
lines[0] = lines[0] + "\tloop_alert"
for index in range(1, len(lines)):
    if lines[index].strip():
        lines[index] = lines[index] + "\tunknown"
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

# Decide which instances to run. Emit instance JSON to <logs-dir>/<id>.json
# and a TSV of (instance_id\tjson_path\timage_key) to stdout.
list_file="$logs_dir/_to_run.tsv"
HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 "$EVAL_VENV_PY" - "$dataset" "$split" \
    "${instance_ids:-}" "${limit:-}" "${start_from:-}" "$output" "$retry_empty" \
    "$logs_dir" "$namespace" > "$list_file" <<'PY'
import json, os, sys, pathlib
dataset_name, split, ids_csv, limit_s, start_from, output_path, retry_empty_s, logs_dir, namespace = sys.argv[1:10]
limit = int(limit_s) if limit_s else None
retry_empty = retry_empty_s == "1"
ids_filter = [i.strip() for i in ids_csv.split(",") if i.strip()] if ids_csv else None

from swebench.harness.utils import load_swebench_dataset
from swebench.harness.test_spec.test_spec import make_test_spec
ds = list(load_swebench_dataset(dataset_name, split))

# Build map of last seen prediction per instance_id (last line wins).
seen = {}
out_path = pathlib.Path(output_path)
if out_path.exists():
    with out_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            iid = rec.get("instance_id")
            if iid:
                seen[iid] = (rec.get("model_patch") or "").strip()

def should_skip(iid):
    if iid not in seen:
        return False
    last_patch = seen[iid]
    if not last_patch and retry_empty:
        return False
    return True

started = start_from is None or start_from == ""
count = 0
logs_dir = pathlib.Path(logs_dir)
for inst in ds:
    iid = inst["instance_id"]
    if not started:
        if iid == start_from:
            started = True
        else:
            continue
    if ids_filter is not None and iid not in ids_filter:
        continue
    if should_skip(iid):
        continue
    json_path = logs_dir / f"{iid.replace('__','-')}.instance.json"
    json_path.write_text(json.dumps(inst))
    spec = make_test_spec(inst, namespace=namespace)
    image_key = spec.instance_image_key
    print(f"{iid}\t{json_path}\t{image_key}")
    count += 1
    if limit is not None and count >= limit:
        break
PY

total=$(wc -l < "$list_file" | tr -d '[:space:]')
if [ "$total" = "0" ]; then
    echo "Nothing to run — every requested instance already has a (non-empty) prediction."
    exit 0
fi

echo "Batch plan: $total instances → $output"
echo "Per-instance logs: $logs_dir/<instance_id>.log"
echo "Summary: $summary"
echo

idx=0
while IFS=$'\t' read -r iid json_path image_key <&3; do
    idx=$((idx+1))
    log="$logs_dir/${iid}.log"
    printf '[%d/%d] %s  (image=%s)\n' "$idx" "$total" "$iid" "$image_key"
    started_ts=$(date +%s)
    started_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    set +e
    bash "$ONE_SHOT" \
        --instance-file "$json_path" \
        --output "$output" \
        --image "$image_key" \
        --timeout "$timeout_secs" \
        "${extra[@]}" \
        > "$log" 2>&1 < /dev/null
    rc=$?
    set -e
    ended_ts=$(date +%s)
    wall=$((ended_ts - started_ts))

    patch_bytes="$("$EVAL_VENV_PY" - "$iid" "$output" <<'PY'
import json, sys
iid = sys.argv[1]
path = sys.argv[2]
last = ""
with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
    for line in f:
        line = line.strip()
        if line:
            last = line
if not last:
    print(0); sys.exit()
try:
    rec = json.loads(last)
except Exception:
    print(0); sys.exit()
if rec.get("instance_id") != iid:
    print(0); sys.exit()
print(len((rec.get("model_patch") or "").encode("utf-8")))
PY
    )"
    if [ "$rc" -eq 0 ] && [ "${patch_bytes:-0}" -gt 0 ]; then
        status="ok"
    elif [ "$rc" -eq 0 ]; then
        status="empty_patch"
    else
        status="error_rc${rc}"
    fi
    loop_alert="$("$EVAL_VENV_PY" - "$REPO_ROOT" "$iid" "$log" <<'PY'
import json
import pathlib
import re
import sys

repo, iid, log_path = sys.argv[1:4]
monitor = pathlib.Path(repo) / ".opencollab" / "swebench" / iid / "loop_monitor.json"
if monitor.exists():
    try:
        print(json.loads(monitor.read_text()).get("level") or "unknown")
        raise SystemExit
    except Exception:
        pass
try:
    text = pathlib.Path(log_path).read_text(errors="ignore")
except OSError:
    print("unknown")
    raise SystemExit
matches = re.findall(r"level=(ok|warn|critical)", text)
print(matches[-1] if matches else "unknown")
PY
    )"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$started_iso" "$iid" "$status" "${patch_bytes:-0}" "$wall" "$loop_alert" >> "$summary"
    printf '       → %s (%s bytes, %ss, loop=%s)\n' "$status" "${patch_bytes:-0}" "$wall" "$loop_alert"
done 3< "$list_file"

echo
echo "Done. Summary:"
column -t -s $'\t' "$summary" | tail -n +1
