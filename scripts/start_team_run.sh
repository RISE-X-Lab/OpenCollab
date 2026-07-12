#!/usr/bin/env bash
# Legacy interactive team entrypoint retained as an explicit integrity gate.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/start_team_run.sh [legacy team generation arguments]

This legacy entrypoint is disabled because its interactive container mounts do
not provide the solver-isolation and host-trusted patch-extraction evidence
required by the current evaluation layer.

Use swebench/gen_prediction_workflow.py for current team/workflow generation.
It prepares an anonymous Git snapshot before Solver execution and extracts the
resulting patch from a bounded container archive with trusted host Git.
EOF
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "help" ]; then
        usage
        return 0
    fi
    echo "error: legacy team generation is disabled by the evaluation-integrity gate" >&2
    echo "use swebench/gen_prediction_workflow.py for current generation" >&2
    return 125
}

main "$@"
