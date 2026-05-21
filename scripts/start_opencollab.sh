#!/usr/bin/env bash
# Bootstrap and start OpenCollab from the repository root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$REPO_ROOT/opencollab"
VENV_DIR="$PROJECT_DIR/.venv"
CONFIG_DIR="$REPO_ROOT/configs"
CONFIG_FILE="$CONFIG_DIR/.env"
EXAMPLE_CONFIG="$CONFIG_DIR/.env.example"

usage() {
    cat <<'EOF'
Usage:
  scripts/start_opencollab.sh [extra opencollab args...]

The unified interactive agent (agent 0, which can spawn child agents) starts
by default. Pass `eval <tasks.jsonl>` for headless evaluation.

Examples:
  scripts/start_opencollab.sh
  scripts/start_opencollab.sh --trace
  scripts/start_opencollab.sh --yolo --no-worktrees
  scripts/start_opencollab.sh eval tasks.jsonl

Configuration:
  Copy configs/.env.example to configs/.env and set OPENCOLLAB_API_KEY.
EOF
}

read_env_value() {
    local key="$1"
    local file="$2"
    [ -f "$file" ] || return 0
    awk -F= -v key="$key" '
        $0 !~ /^[[:space:]]*#/ && $1 == key {
            value = substr($0, index($0, "=") + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            gsub(/^["'\'']|["'\'']$/, "", value)
            print value
            exit
        }
    ' "$file"
}

ensure_config() {
    mkdir -p "$CONFIG_DIR"
    if [ ! -f "$CONFIG_FILE" ]; then
        cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
        echo "Created $CONFIG_FILE from the example."
        echo "Edit it and set OPENCOLLAB_API_KEY before starting OpenCollab."
        exit 1
    fi

    local key_value="${OPENCOLLAB_API_KEY:-}"
    if [ -z "$key_value" ]; then
        key_value="$(read_env_value OPENCOLLAB_API_KEY "$CONFIG_FILE")"
    fi
    if [ -z "$key_value" ]; then
        echo "Missing OPENCOLLAB_API_KEY."
        echo "Set it in $CONFIG_FILE or export it in your shell."
        exit 1
    fi
}

ensure_venv() {
    if [ -x "$VENV_DIR/bin/opencollab" ]; then
        return
    fi

    cd "$PROJECT_DIR"
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV_DIR"
        uv pip install --python "$VENV_DIR/bin/python" -e "$PROJECT_DIR"
        return
    fi

    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install -e "$PROJECT_DIR"
        return
    fi

    echo "Could not find uv or python3 to create a virtual environment."
    exit 1
}

main() {
    if [ "${1:-}" = "help" ]; then
        usage
        exit 0
    fi

    ensure_config
    ensure_venv

    cd "$REPO_ROOT"
    exec "$VENV_DIR/bin/opencollab" --workspace "$REPO_ROOT" "$@"
}

main "$@"
