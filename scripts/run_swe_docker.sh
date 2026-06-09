#!/bin/bash
# Run SWE-bench with Docker evaluation
set -e

IMAGE_NAME="swe-collab"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE="$REPO_ROOT/swebench/Dockerfile"

# Build image if needed
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building Docker image $IMAGE_NAME..."
    docker build -t "$IMAGE_NAME" -f "$DOCKERFILE" "$REPO_ROOT"
fi

# logs/ is mounted — output_path defaults to logs/swe_predictions.jsonl
# No need for separate file mount
CONFIG_MOUNTS=()
if [ -d "$REPO_ROOT/configs" ]; then
    CONFIG_MOUNTS+=(-v "$REPO_ROOT/configs:/app/configs:ro")
fi
if [ -f "$REPO_ROOT/.env" ]; then
    CONFIG_MOUNTS+=(-v "$REPO_ROOT/.env:/app/.env:ro")
fi

docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$REPO_ROOT/swe_workdir:/app/swe_workdir" \
    -v "$REPO_ROOT/logs:/app/logs" \
    "${CONFIG_MOUNTS[@]}" \
    "$IMAGE_NAME" "$@"
