#!/usr/bin/env bash
set -euo pipefail

IID="$1"
IMAGE="$2"
TOKEN_FIFO="$3"
RUN="${4:-}"

BASE="${OPENCOLLAB_REMOTE_ROOT:?set OPENCOLLAB_REMOTE_ROOT}"
REPO="${OPENCOLLAB_REMOTE_REPO:?set OPENCOLLAB_REMOTE_REPO}"
if [[ -z "$RUN" ]]; then
  RUN=$BASE/eval_work/swe_v1_default
fi

cleanup_secret_files() {
  local status=$?
  rm -f "$TOKEN_FIFO"
  exit "$status"
}
trap cleanup_secret_files EXIT INT TERM

read -r OC_PROXY_TOKEN < "$TOKEN_FIFO"
rm -f "$TOKEN_FIFO"

export OPENCOLLAB_API_KEY="$OC_PROXY_TOKEN"
export ANTHROPIC_API_KEY="$OC_PROXY_TOKEN"
export PYTHONPATH="$REPO/opencollab:$BASE/pydeps${PYTHONPATH:+:$PYTHONPATH}"
export OPENCOLLAB_PROVIDER=anthropic
export OPENCOLLAB_BASE_URL="${OPENCOLLAB_REMOTE_PROXY_BASE_URL:?set OPENCOLLAB_REMOTE_PROXY_BASE_URL}"
export ANTHROPIC_BASE_URL="$OPENCOLLAB_BASE_URL"
export OPENCOLLAB_MODEL="${OPENCOLLAB_MODEL:?set OPENCOLLAB_MODEL}"
export OPENCOLLAB_THINKING="${OPENCOLLAB_THINKING:-false}"
export OPENCOLLAB_LLM_TIMEOUT="${OPENCOLLAB_LLM_TIMEOUT:-600}"
export OPENCOLLAB_EVAL_WORKFLOW_LOG_DIR="$RUN/workflow_logs"
export PYTHONUNBUFFERED=1

WORKFLOW="${OPENCOLLAB_SWE_WORKFLOW:-validation-council-solve}"
SWE_GENERATOR="${OPENCOLLAB_SWE_GENERATOR:-workflow}"
MODEL_NAME="${OPENCOLLAB_SWE_MODEL_NAME:-$OPENCOLLAB_MODEL}"
SWE_BUDGET="${OPENCOLLAB_SWE_BUDGET:-16000000}"
SWE_MAX_STEPS="${OPENCOLLAB_SWE_MAX_STEPS:-60}"
SWE_TIMEOUT="${OPENCOLLAB_SWE_TIMEOUT:-14400}"
SWE_DATASET="${OPENCOLLAB_SWE_DATASET:-swe-batch-pro-lite}"
CHECKPOINT_INTERVAL="${OPENCOLLAB_SWE_CHECKPOINT_INTERVAL_SECONDS:-300}"

if [[ -n "${OPENCOLLAB_INSTANCE_FILE:-}" ]]; then
  INSTANCE_FILE="$OPENCOLLAB_INSTANCE_FILE"
elif [[ "$SWE_DATASET" == "swe-batch-pro-lite" ]]; then
  INSTANCE_FILE="$BASE/datasets/swe-batch-pro-lite/instances/$IID.json"
elif [[ -f "$BASE/datasets/swe-batch-pro-lite/instances/$IID.json" ]]; then
  INSTANCE_FILE="$BASE/datasets/swe-batch-pro-lite/instances/$IID.json"
else
  INSTANCE_FILE="$BASE/datasets/swe-bench-lite/instances/test/$IID.json"
fi

if [[ ! -f "$INSTANCE_FILE" && "$SWE_DATASET" == "swe-batch-pro-lite" && -f "$BASE/datasets/swe-batch-pro-lite/instances.jsonl" ]]; then
  mkdir -p "$RUN/instance_files"
  INSTANCE_FILE="$RUN/instance_files/$IID.json"
  python3 - "$BASE/datasets/swe-batch-pro-lite/instances.jsonl" "$IID" "$INSTANCE_FILE" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
instance_id = sys.argv[2]
target = Path(sys.argv[3])
for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    row = json.loads(line)
    if row.get("instance_id") == instance_id:
        target.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        raise SystemExit(0)
raise SystemExit(2)
PY
fi

if [[ ! -f "$INSTANCE_FILE" ]]; then
  echo "instance file is missing: $INSTANCE_FILE" >&2
  exit 2
fi

cd "$REPO"
mkdir -p "$RUN/generation_logs" "$RUN/workflow_logs"

checkpoint_args=()
if [[ "$CHECKPOINT_INTERVAL" != "0" ]]; then
  checkpoint_args+=(--checkpoint-interval-seconds "$CHECKPOINT_INTERVAL")
fi
if [[ "${OPENCOLLAB_SWE_RESUME:-false}" == "true" ]]; then
  checkpoint_args+=(--resume)
fi

if [[ "$SWE_GENERATOR" == "single-agent" ]]; then
  python3 -u swebench/gen_prediction.py \
    --instance-file "$INSTANCE_FILE" \
    --output "$RUN/predictions.jsonl" \
    --metrics "$RUN/metrics.jsonl" \
    --image "$IMAGE" \
    --model-name "$MODEL_NAME" \
    --budget "$SWE_BUDGET" \
    --max-steps "$SWE_MAX_STEPS" \
    --timeout "$SWE_TIMEOUT" 2>&1 | tee -a "$RUN/generation_logs/$IID.log"
else
  python3 -u swebench/gen_prediction_workflow.py \
    --workflow "$WORKFLOW" \
    --instance-file "$INSTANCE_FILE" \
    --output "$RUN/predictions.jsonl" \
    --metrics "$RUN/metrics.jsonl" \
    --image "$IMAGE" \
    --model-name "$MODEL_NAME" \
    --budget "$SWE_BUDGET" \
    --max-steps "$SWE_MAX_STEPS" \
    --timeout "$SWE_TIMEOUT" \
    "${checkpoint_args[@]}" 2>&1 | tee -a "$RUN/generation_logs/$IID.log"
fi
