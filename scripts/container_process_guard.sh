#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$(command -v python3 || command -v python || true)"
[ -n "$python_bin" ] || {
    echo "python3/python is required for process-session ownership" >&2
    exit 127
}
exec "$python_bin" "$SCRIPT_DIR/container_process_guard.py" "$@"
